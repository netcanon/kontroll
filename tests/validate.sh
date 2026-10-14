#!/usr/bin/env bash
# L1 (lint) + L2 (syntax/config-validate) gate for kontroll — THE gate, one file: CI runs
# it with --strict, the control VM runs it, and Windows runs it through tests/validate.ps1
# (a shim over this file via Git for Windows' bash). Runs each validator whose tool is
# present; any present tool that fails fails the run (fail-closed). With --strict
# (CI), a missing tool is also a failure.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
STRICT="${1:-}"
fail=0; skip=0

have() { command -v "$1" >/dev/null 2>&1; }
step() { # name  present(0/1)  command...
  local name="$1"; local present="$2"; shift 2
  if [ "$present" -ne 0 ]; then
    echo "SKIP  $name (not installed)"; skip=$((skip+1))
    [ "$STRICT" = "--strict" ] && fail=$((fail+1)); return
  fi
  echo "RUN   $name"
  if "$@"; then echo "ok    $name"; else echo "FAIL  $name"; fail=$((fail+1)); fi
}

have yamllint; step yamllint $? yamllint .
have ansible-lint; step ansible-lint $? ansible-lint
have docker; step compose-config $? bash -c 'docker compose --env-file docker/.env.example -f docker/compose.yaml config >/dev/null'
have promtool; step promtool $? promtool check config prometheus/prometheus.yml

# the Vector config (globals + the config.d/ drop-in tree + the loki sink topology) parses and every transform
# the sink references exists. Skipped if the vector CLI is absent (IaC-only control VM; CI installs it and runs
# --strict); the compose-config gate above still parses the loki/vector COMPOSE fragments. --no-environment
# skips health/connectivity checks. The config interpolates deploy-time `${KONTROLL_*}` variables (the operator
# home, the PVE log token), so the gate sources the SHIPPED placeholder env (docker/.env.example — the same
# file compose-config uses) in a subshell; a variable the config needs but the template lacks still fails loud.
have vector; step vector-config $? bash -c '
  set -a; . docker/.env.example; set +a
  vector validate --no-environment -C docker/vector/config.d docker/vector/vector.yaml'

# every file under the secrets directories is a FULLY sops-encrypted document (every value ciphertext — not merely
# "has a `sops:` block"), nothing else sits there, and none holds a private key. The same guard gates the commits
# that take a whole worktree into the canonical (local-canonical.yml, scripts/kontroll.sh). Needs no sops binary
# and no git: this is the one seat that reads files already committed.
step sops-encrypted 0 sh scripts/lib/plaintext-guard.sh --tree .

# no secret artifact staged
step secret-grep 0 bash -c '
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
  if git diff --cached --name-only | grep -E "keys\.txt$|\.agekey$|\.dec$"; then exit 1; fi
  exit 0'

# deep secret scan — catch plaintext keys/creds in committable files before they reach a
# commit / the canonical / a backup / a bundle (SECURITY.md C8). SOPS ciphertext + local/
# are allowlisted in .gitleaks.toml.
have gitleaks; step gitleaks $? gitleaks detect --no-git --source . -c .gitleaks.toml --redact --no-banner

# playbooks parse against the mock inventory
have ansible-playbook; step ansible-syntax $? bash -c '
  rc=0
  for p in ansible/playbooks/*.yml; do
    ansible-playbook -i tests/mock-inventory.yml "$p" --syntax-check || rc=1
  done
  exit $rc'

# L2 — the code-half pytest suite (galaxy.py + the GUI). OFFLINE: the shell-outs are
# mocked, the live lab is never reached (docs/qa-and-release-pipeline.md §1). Skipped
# if pytest isn't installed, so the IaC-only control VM still passes; CI installs it
# (tests/requirements-dev.txt). e2e/slow excluded — the hermetic default.
PY="$(command -v python3 || command -v python || true)"
if [ -n "$PY" ] && "$PY" -m pytest --version >/dev/null 2>&1; then
  step pytest 0 "$PY" -m pytest -m "not e2e and not slow" -q
  # the plaintext guard's predicate against a document the INSTALLED sops writes: the ciphertext token, the
  # encrypted comment and the metadata block are sops's shapes, not ours, and a sops bump that changes one must
  # fail HERE — not refuse every healthy secret file at the next deploy. A step of its own, behind `have sops`:
  # under --strict a missing binary fails the gate. And no test may have been SKIPPED: pytest exits 0 when all
  # of them are, which would be a green step that proved nothing. (`-rs` lists each skip on a line of its own;
  # pytest exits 5, not 0, when it ran no test at all; a developer's PYTEST_ADDOPTS is not let in — it could hide
  # the summary or deselect a test.)
  have sops; step sops-guard-conformance $? bash -c '
    out="$(PYTEST_ADDOPTS= "$0" -m pytest tests/integration/test_plaintext_guard_sops.py -rs --color=no 2>&1)"; rc=$?
    printf "%s\n" "$out"
    [ "$rc" -eq 0 ] && ! printf "%s\n" "$out" | grep -q "^SKIPPED"' "$PY"
  # the change of identity, with REAL uids (rule 5 of kontroll.canon). The rows of tests/integration/test_canon_drop.py
  # chown a canonical to the play's layout and start git as the service, and those of
  # tests/integration/test_content_clone_as_service.py run the deploy's content-clone tasks with ansible-playbook
  # (root's git, and the service uid's), so they need root — and every one of them
  # was SKIPPED in the suite above unless this gate itself runs as root. So: as root, directly; else through
  # `sudo -n` (never a prompt) where that works; else the step is skipped, which --strict counts as a failure — a
  # gate that cannot be root has not shown that root's git is the service's. No row may be skipped (as for the sops
  # step), and nothing is written into the tree as root: no bytecode, no pytest cache.
  as_root=""; how="as root"
  if [ "$(id -u)" -eq 0 ]; then as_root="env"
  elif have sudo && sudo -n true 2>/dev/null; then as_root="sudo -n env"; how="through sudo -n"; fi
  if [ -z "$as_root" ]; then
    # not `step`: its SKIP line says "not installed", and what is missing here is root
    echo "SKIP  canon-drop-as-root (this gate is not root and has no sudo that needs no password)"; skip=$((skip+1))
    [ "$STRICT" = "--strict" ] && fail=$((fail+1))
  else
    echo "      canon-drop-as-root runs $how: it chowns repositories under /tmp and starts git as uid 1001"
    step canon-drop-as-root 0 bash -c '
      # shellcheck disable=SC2086  # $1 is "env" or "sudo -n env": it must word-split
      out="$($1 PYTEST_ADDOPTS= PATH="$PATH" HOME="$HOME" LD_LIBRARY_PATH="${LD_LIBRARY_PATH-}" \
             PYTHONPATH="${PYTHONPATH-}" PYTHONDONTWRITEBYTECODE=1 \
             "$0" -B -m pytest tests/integration/test_canon_drop.py tests/integration/test_content_clone_as_service.py \
               -p no:cacheprovider -rs --color=no 2>&1)"; rc=$?
      printf "%s\n" "$out"
      [ "$rc" -eq 0 ] && ! printf "%s\n" "$out" | grep -q "^SKIPPED"' "$PY" "$as_root"
  fi
else
  echo "SKIP  pytest (not installed)"; skip=$((skip+1))
  [ "$STRICT" = "--strict" ] && fail=$((fail+1))
fi

# every test function carries a docstring (docs/testing-standards.md §2 — machine-enforced)
if [ -n "$PY" ]; then step test-docs 0 "$PY" tests/check-test-docs.py; fi

# every relative link in a tracked Markdown file resolves inside the repository. Hermetic: no URL is fetched, and
# anchors are not checked. (SECURITY.md's own links all climbed out of the tree with `../` and nothing noticed.)
if [ -n "$PY" ]; then step doc-links 0 "$PY" tests/check-doc-links.py; fi

# every published compose port binds the mgmt IP (${KONTROLL_MGMT_IP}) — never a literal IP / 0.0.0.0 (SECURITY.md
# C3), except the NPM-fronted UIs (homepage 3000 / semaphore 3001). Guards the genericization Phase-3 mgmt-IP
# parameterization from regressing to a hardcoded/empty host that exposes a privileged surface on all interfaces.
if [ -n "$PY" ]; then step mgmt-port-binding 0 "$PY" tests/check-mgmt-port-binding.py; fi

# every storage-root-derived compose bind SOURCE has a uid-scoped deploy-stack provisioner (the C8 at-rest
# contract): a relocatable store with no chown would be auto-created root:root 0755 by `up` — a world-readable
# secret-bearing-log hole (G9). Guards a future ${KONTROLL_*_DIR} bind added without its deploy-stack chown.
if [ -n "$PY" ]; then step storage-chown 0 "$PY" tests/check-storage-chown.py; fi

# generated collection lockfile + never-brick trust sidecar are fresh: modules+units -> requirements.generated.yml +
# trust.generated.yml. Both are gitignored (regenerated per-node), so --check is a staleness/self-consistency gate
# (diff on-disk if present) + the BRICK-1 invariant: the generator is OFFLINE — it makes no network call, so an
# air-gapped / Galaxy-down generate never bricks (docs/reviews/2026-06-25-never-brick-supply-chain). HERMETIC.
if [ -n "$PY" ]; then step gen-requirements 0 "$PY" scripts/gen-requirements.py --check; fi

# the committed image digest-lock (docker/images.lock.yml) is well-formed: schema:1, every entry has a string ref +
# tag-or-null + a digest that is null or a valid sha256 (the image-world pin floor — report 22 §4.4). OFFLINE: the
# --check read path makes no network call, mirroring gen-requirements' BRICK-1 (a digest is RESOLVED only by the
# dev/CI --refresh, never at deploy). Guards a hand-edited / malformed lock entry that would mis-pin an image.
if [ -n "$PY" ]; then step gen-image-digests 0 "$PY" scripts/gen-image-digests.py --check; fi

# the derived dashboard-floor pins (dashboards/derived/*.lock.yml, north-star Rung 4a) are coherent OFFLINE: schema:1,
# every telemetry derive_dashboard: selector has a lock, each lock's committed board hashes to its content_sha256 (the
# sha256 tamper floor) + still contains its requires_series (the series-fit honesty guard) + matches its selector.
# HERMETIC (BRICK-1) — no grafana.com call; the network DERIVE (--resolve) is the operator tier, pinned out of the
# deploy path by the dashboard-floor-resolve-not-in-deploy gate below. The dashboard analogue of gen-image-digests --check.
if [ -n "$PY" ]; then step gen-dashboard-floor 0 "$PY" scripts/gen-dashboard-floor.py --check; fi

# the pinned IEEE OUI->vendor lookup (oui/oui-lookup.generated.json, discovery display-hint Rung 3) is coherent
# OFFLINE: schema:1, every key is 6/7/9 lowercase-hex nibbles, len(prefixes)==lock.prefix_count (the honest-count
# invariant), sha256(json)==lock.content_sha256 (the tamper floor), lock.sources == the exact 3 IEEE URLs. HERMETIC
# (BRICK-1) — the network DERIVE (--refresh) is operator-only, pinned out of the deploy path by the
# oui-refresh-not-in-deploy gate below. The OUI analogue of gen-dashboard-floor --check / gen-image-digests --check.
if [ -n "$PY" ]; then step gen-oui 0 "$PY" scripts/gen-oui.py --check; fi

# the baked-code manifest (docker/code-manifest.lock.yml) is in sync with scripts/kontroll/ — the M6 "L2b for code"
# recorded sha256 floor for the importable package baked into the published control image at /opt/kontroll. --check
# is the staleness gate: it fails loudly if a scripts/kontroll/ edit didn't refresh the lock (so the world-pullable
# image's code can never silently drift from the recorded, reviewed manifest). OFFLINE (pure file hashing, no network
# — the BRICK-1 discipline); `--verify <tree>` re-checks a materialized baked tree. (phase-b synthesis §3.)
if [ -n "$PY" ]; then step gen-code-manifest 0 "$PY" scripts/gen-code-manifest.py --check; fi

# generated observability targets are fresh: modules+inventory -> prometheus/targets/*.generated.yml.
# Enforces the "generated, never hand-maintained" rule (CLAUDE.md) for scrape targets, exactly as
# requirements.generated.yml does for collections. Skipped only if python is absent.
if [ -n "$PY" ]; then step gen-observability 0 "$PY" scripts/gen-observability.py --check; fi

# generated proxy-exporter jobs are fresh: telemetry registry -> prometheus/jobs.d/*.generated.yml (the
# proxmox job that used to be hand-written in prometheus.yml). Same generated-never-hand-maintained guard.
if [ -n "$PY" ]; then step gen-prometheus-jobs 0 "$PY" scripts/gen-prometheus-jobs.py --check; fi

# generated exporter container fragments are fresh: telemetry registry -> docker/services/*.generated.yaml.
if [ -n "$PY" ]; then step gen-exporters 0 "$PY" scripts/gen-exporters.py --check; fi

# generated snmp_exporter if_mib modules are present + secret-free: generator.yml + vendored MIBs ->
# prometheus/exporters/snmp/modules.generated.yml. Model-B (pure-Python, NO binary): present, modules-only, no
# auths:/credential, if_mib present. The byte-exact "matches a fresh generator rebuild" gate is the dedicated CGO
# `snmp-modules` CI job (the snmp_exporter generator is NetSNMP/CGO — not runnable in this bare tier or on Windows).
if [ -n "$PY" ]; then step gen-snmp 0 "$PY" scripts/gen-snmp.py --check; fi

# generated backup-schedule spec is fresh: module `backup:` blocks -> config/semaphore/schedules.generated.yml.
if [ -n "$PY" ]; then step gen-backup 0 "$PY" scripts/gen-backup.py --check; fi

# generated Vector logging drop-ins are fresh: module `logs:` blocks x inventory -> docker/vector/generated/.
# Same generated-never-hand-maintained guard as the others (the logging capability; C12 label hygiene fail-closed).
if [ -n "$PY" ]; then step gen-logging 0 "$PY" scripts/gen-logging.py --check; fi

# F2 de-bespoke: a `derived: true` class's metrics:/logs: FLOOR still matches what the derivation recomputes from
# its pinned facts + the registries (a method's derive_default / a backend's confer list churning shows as a diff,
# never silent rot). The metrics/logs analogue of the gen-* checks above, but the "lockfile" is module.yml itself.
if [ -n "$PY" ]; then step gen-class-capabilities 0 "$PY" scripts/gen-class-capabilities.py --check; fi

# generated UNIFIED secret->env manifest is fresh: every telemetry metrics: + logging logs: method's secret_env_map
# -> config/secret-env.manifest.generated.yml (deploy-stack's one render loop). Value-free, fail-closed on a bad
# env name / template / platform-core collision; the same generated-never-hand-maintained guard (C12).
if [ -n "$PY" ]; then step gen-secret-env 0 "$PY" scripts/gen-secret-env.py --check; fi

# the committed Homepage fleet tile span is fresh: enabled modules x inventory -> the `# >>> kontroll fleet`
# marked block inside instance.example/dashboards/homepage/services.yaml. Example-driven (F3): --check validates
# the PUBLIC example span (TEST-NET only, which ships); the live instance/ span is regenerated at deploy. The
# pytest twin (tests/unit/test_gen_homepage.py) additionally proves the span agrees with the GUI editor + is
# secret/topology-free, and covers a host whose gate SKIPs the tool-dependent steps.
if [ -n "$PY" ]; then step gen-homepage 0 "$PY" scripts/gen-homepage.py --check; fi

# every actuation/<key>/unit.yml app-store unit (R2) is VALID: known schema, closed kind enum, key==dir, EXACT '=='
# pin for installable kinds, target.device_class is an enabled module, closed blast/provenance enums. Fail-closed so a
# malformed unit never unions a pin into requirements / never reaches ansible-galaxy. (Push-stage --check of the
# generated template+wrapper lands at R4/R5; today --check == this validation pass.)
if [ -n "$PY" ]; then step gen-actuation 0 "$PY" scripts/gen-actuation.py --check; fi

# no exporter compose fragment publishes a host port (kontroll-net-only; SSRF/exposure guard, SECURITY.md C3).
step exporter-no-host-port 0 bash -c '
  bad=0
  for f in docker/services/*exporter*.yaml; do
    [ -e "$f" ] || continue
    grep -qE "^[[:space:]]*ports:" "$f" && { echo "  exporter fragment declares ports: $f"; bad=1; }
  done
  exit $bad'

# the online-validate seam (scripts/gen-validate-live.py) reaches devices READ-ONLY: its transport is a CLOSED
# allow-list — a TLS handshake, an HTTP GET, an SNMP GETNEXT. A write/actuating verb must not be expressible
# (SECURITY.md C13). Static source gate (the validator itself is control-VM/on-demand — NEVER run here, it reaches
# the lab); fail if a write transport appears. The validator's hermetic dispatch/judgement is covered by pytest.
step validate-live-readonly 0 bash -c '
  f=scripts/gen-validate-live.py
  [ -e "$f" ] || exit 0
  if grep -nE "method=\"(POST|PUT|DELETE|PATCH)\"|requests\.(post|put|delete|patch)|snmpset|snmpbulkset|urlopen\([^)]*data=" "$f"; then
    echo "  gen-validate-live.py declares a NON-read-only transport (C13: only handshake/GET/GETNEXT allowed)"; exit 1; fi
  exit 0'

# the passive-discovery sweep (scripts/kontroll-discover.py) reaches an onboarded device READ-ONLY + PASSIVE across its
# THREE transports (a byte-capped HTTP GET, an SNMPv3 GETBULK WALK, and a forced-command `ssh` read) — NO write/POST/
# SET, NO active scan (nmap/masscan/raw socket), and NO CIDR expansion (it reaches only an onboarded device's own
# address, never the network). Machine-enforced source gate (the sweep is control-VM/on-demand — NEVER run here, it
# reaches the lab; SECURITY C18), in TWO legs: (1) a NEGATIVE leg forbidding the write/scan tokens; (2) a POSITIVE,
# case-SENSITIVE leg for the ssh transport (openwrt rung) — a grep cannot bound ssh with a negative list (its
# read/write/tunnel are ONE binary + options), so it forbids every write/tunnel/remote-exec construct: transfer verbs
# (scp/sftp), the capitalized `Command`/`Forward` option FAMILIES (Proxy/Remote/Local/KnownHostsCommand, *Forward*),
# ProxyJump/Tunnel/Subsystem, the config-INDIRECTION options (-F / Include= point ssh at options this grep can't see),
# and the alternate-spawn primitives (os.system/os.popen/pty/paramiko/os.exec*). Case-SENSITIVE is load-bearing: -i
# would false-trip the committed snmp argv "-l"/"-r". The "no client command word after <user>@<host>" property a grep
# CANNOT see is the load-bearing backstop — the argv-AST twin test_ssh_read_is_forced_command_read_only (a CLOSED argv
# shape) + test_actor_has_no_shell_true_and_no_extra_ssh_spawn. Mirror of validate-live-readonly; the hermetic
# dispatch/parse is covered by pytest (tests/unit/test_discovery.py).
step discovery-passive-only 0 bash -c '
  f=scripts/kontroll-discover.py
  [ -e "$f" ] || exit 0
  if grep -nE "method=\"(POST|PUT|DELETE|PATCH)\"|requests\.(post|put|delete|patch)|snmpset|snmpbulkset|nmap|masscan|SOCK_RAW|socket\.socket|ip_network|\.hosts\(|urlopen\([^)]*data=" "$f"; then
    echo "  kontroll-discover.py declares a NON-passive/NON-read-only transport (C18: only a byte-capped read of an onboarded device)"; exit 1; fi
  if grep -nE "\bscp\b|\bsftp\b|Command|Forward|ProxyJump|Tunnel|Subsystem|Include|\"-[FWLRDJs]\"|os\.system|os\.popen|\bpty\b|paramiko|os\.exec" "$f"; then
    echo "  kontroll-discover.py declares a write/tunnel/indirection/spawn-capable SSH transport (C18: only a byte-capped forced-command read)"; exit 1; fi
  exit 0'

# P-1: committed discovery DATA (the registry + its fixtures/tests) must use ONLY documentation identifiers — RFC-5737
# IPs (192.0.2/198.51.100/203.0.113), RFC-7042 MACs (00:00:5e:00:53:xx) — never a live IP/MAC/domain. The runtime
# artifact is git-ignored under local/. Guards a real lease/neighbour identifier leaking into a committed fixture (P-1).
# The IP leg is OID-AWARE (Rung 3 v4 + IPv6 rung): a net-snmp walk golden carries numeric OIDs whose sub-identifiers
# would false-positive as dotted-quads. Context-aware pipeline, in order: (0) a walk line whose OID carries a v6/v6z
# InetAddress index (addrType 2|4, len 16, +16 address bytes — RFC-4293 ipNetToPhysicalTable) has its WHOLE OID head
# stripped, because its trailing sub-ids are v6 address BYTES (e.g. `…0.0.0.1`), NOT a bare v4 host — else they
# false-trip; (1) else a walk line has its OID prefix stripped to EXPOSE a trailing bare v4 quad (legacy ipNetToMedia
# `<ifIndex>.<a.b.c.d>` AND v4 ipNetToPhysical `…1.4.<a.b.c.d>`, so a live v4 in a suffix is STILL caught); (2) any
# remaining 5+-group dotted-int run (a bare OID) is stripped ENTIRELY; (3) scan for dotted quads not in the RFC-5737
# allowlist. The MAC leg scans BOTH colon (DHCP-JSON) + space Hex-STRING (net-snmp) forms; null/broadcast are
# non-identifiers. The IPv6 leg catches a colon-hex v6 (has `::` OR a >2-hex hextet, so never a 6×2-hex MAC) not in the
# RFC-3849 doc range 2001:db8::/32 (loopback/unspecified/link-local are non-identifiers) — the gate had no v6 check before.
step discovery-p1-fixtures 0 bash -c '
  bad=0
  scope="discovery tests/fixtures/discovery tests/unit/test_discovery.py"
  ips=$(grep -rhE "[0-9]" $scope 2>/dev/null \
        | sed -E "/\.[24]\.16(\.[0-9]{1,3}){16} = /s/^\.?[0-9][0-9.]* = / = /" \
        | sed -E "s/^\.?([0-9]+\.)+([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}) = /\2 = /" \
        | sed -E "s/[0-9]+(\.[0-9]+){4,}//g" \
        | grep -oE "([0-9]{1,3}\.){3}[0-9]{1,3}" \
        | grep -vE "^(192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|0\.0\.0\.0|255\.255\.255\.255)" || true)
  if [ -n "$ips" ]; then echo "  non-documentation IPv4 in committed discovery data (P-1):"; echo "$ips" | sort -u | sed "s/^/    /"; bad=1; fi
  macs=$( { grep -rhoE "([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}" $scope 2>/dev/null | grep -viE "^(00:00:5e:00:53:|00:00:00:00:00:00|ff:ff:ff:ff:ff:ff)";
            grep -rhoE "([0-9a-fA-F]{2} ){5}[0-9a-fA-F]{2}" $scope 2>/dev/null | grep -viE "^(00 00 5e 00 53 |00 00 00 00 00 00|ff ff ff ff ff ff)"; } || true)
  if [ -n "$macs" ]; then echo "  non-documentation MAC in committed discovery data (P-1):"; echo "$macs" | sort -u | sed "s/^/    /"; bad=1; fi
  v6=$(grep -rhoiE "[0-9a-f]{1,4}(:[0-9a-f]{0,4}){2,}" $scope 2>/dev/null \
       | grep -iE "::|[0-9a-f]{3,4}:|:[0-9a-f]{3,4}" \
       | grep -viE "^([0-9a-f]{2}:){5}[0-9a-f]{2}$" \
       | grep -viE "^(2001:0?db8:|fe80:|::1?$|::$)" || true)
  if [ -n "$v6" ]; then echo "  non-documentation IPv6 in committed discovery data (P-1):"; echo "$v6" | sort -u | sed "s/^/    /"; bad=1; fi
  # (the live-domain / hostname leg moved to the tree-wide pii-guard step below — the names are instance tokens
  #  read from the private overlay, never a literal in this file)
  exit $bad'

# the identifier-leak guard over the WHOLE shippable tree (every tracked file minus the private strip set — the same
# scanner the PII-guard workflow and tests/unit/test_leak_guard.py run): no RFC-1918 address in any spelling, no
# non-documentation MAC/IPv6, no real age key, no personal identifier/operator path, and none of THIS instance's
# own names (instance/leak-tokens.txt, or $KONTROLL_LEAK_TOKENS_FILE; the shipped canaries on a public checkout).
# SECURITY.md C21 / docs/public-split.md. HERMETIC: python3 + git only.
if [ -n "$PY" ]; then step pii-guard 0 "$PY" -I tests/_leak_guard.py --tree; fi

# BRICK-1 static gate (north-star Rung 4a): the dashboard-floor DERIVE leg (gen-dashboard-floor.py --resolve) reaches
# grafana.com, so it must NEVER appear in an install-gating path — deploy-stack + CI run only the OFFLINE --check/--list
# (the frozen lock is what deploy reads). Mirror validate-live-readonly: grep the deploy path (SEPARATE files, so this
# gate can't self-match) for a --resolve invocation; fail if one appears. The hermetic --check being genuinely offline
# is additionally pinned by tests/unit/test_gen_dashboard_floor.py::test_check_is_offline.
step dashboard-floor-resolve-not-in-deploy 0 bash -c '
  bad=0
  for f in ansible/playbooks/deploy-stack.yml .github/workflows/*.yml; do
    [ -e "$f" ] || continue
    if grep -nE "gen-dashboard-floor\.py[^\n]*--resolve|--resolve[^\n]*gen-dashboard-floor\.py" "$f"; then
      echo "  $f invokes gen-dashboard-floor.py --resolve (a grafana.com network call in an install path — BRICK-1)"; bad=1; fi
  done
  exit $bad'

# BRICK-1 static gate (OUI Rung 3): the OUI DERIVE leg (gen-oui.py --refresh) reaches standards-oui.ieee.org, so it
# must NEVER appear in an install-gating path — deploy-stack + CI run only the OFFLINE --check (the frozen lookup is
# what runtime reads). Mirror dashboard-floor-resolve-not-in-deploy: grep the deploy path (SEPARATE files, so this
# gate can't self-match) for a --refresh invocation OR the IEEE host; fail if one appears. The hermetic --check being
# genuinely offline is additionally pinned by tests/unit/test_gen_oui.py::test_check_makes_no_network_call.
step oui-refresh-not-in-deploy 0 bash -c '
  bad=0
  for f in ansible/playbooks/deploy-stack.yml .github/workflows/*.yml; do
    [ -e "$f" ] || continue
    if grep -nE "gen-oui\.py[^\n]*--refresh|--refresh[^\n]*gen-oui\.py|standards-oui\.ieee\.org" "$f"; then
      echo "  $f invokes gen-oui.py --refresh / hits standards-oui.ieee.org (a network call in an install path — BRICK-1)"; bad=1; fi
  done
  exit $bad'

# the rendered snmp_exporter config (real SNMPv3 creds) must NEVER be tracked — only the .j2 template is.
step snmp-config-untracked 0 bash -c '
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
  if git ls-files --error-unmatch prometheus/exporters/snmp/snmp.yml >/dev/null 2>&1; then
    echo "  rendered snmp.yml is TRACKED (must be gitignored — it holds the SNMPv3 creds)"; exit 1; fi
  exit 0'

# the shipped tree holds regular files only: no symbolic link (mode 120000) and no submodule entry (160000). The
# promote gate refuses a proposal whose tree holds either (scripts/kontroll/canon.py: read_tree_verified) and it
# judges the WHOLE tree — so one such entry on `main` would refuse every proposal. Nothing in this tree is one; this
# keeps it so.
step tree-holds-regular-files-only 0 bash -c '
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "  not a git work tree: the index could not be listed, so NOTHING was checked here"; exit 0; fi
  bad="$(git ls-files -s | awk "\$1 == \"120000\" || \$1 == \"160000\" {print \"  \" \$0}")"
  if [ -n "$bad" ]; then
    echo "  the tree holds a symbolic link or a submodule entry (the promote gate would refuse every proposal):"
    echo "$bad"; exit 1; fi
  exit 0'

echo "──────────────────────────────────────────"
if [ "$fail" -ne 0 ]; then echo "VALIDATE: FAIL ($fail failed, $skip skipped)"; exit 1; fi
echo "VALIDATE: PASS ($skip skipped)"
