# Custom Semaphore runner = stock image + the two things our playbooks need that
# the base image lacks:
#   1. sops + age  — community.sops shells out to `sops`, which needs `age` to
#      decrypt the SOPS secret files at runtime.
#   2. the device-class Ansible collections, baked into ansible's default system
#      collections path (/usr/share/ansible/collections) so the runner finds them
#      with zero extra config.
#
# The collection list is the bootstrap-GENERATED lockfile (instance/fleet.yml +
# modules/ → requirements.generated.yml). It is gitignored, but docker build
# context ignores .gitignore, so building on the control node (where bootstrap
# has produced it on disk) bakes exactly the enabled fleet's collections.
# When the enabled modules change: re-run bootstrap, then rebuild this image
# (deploy-stack.yml does both). Never hand-edit the lockfile.
#
# Built by ansible/playbooks/deploy-stack.yml. Build context = repo root.
#
# The base is pinned by tag AND digest: the digest is what is pulled, the tag is the label a reader checks it
# against. Never pin a Semaphore older than the one a live box already runs — Semaphore migrates its database
# forward on start, so an older pin is a downgrade over a migrated schema. A bump follows docker/README.md
# § Refreshing a pin, which re-derives the apk constraints and requirements.txt below from the new base.
#
# The base's ansible venv ships ansible-core 2.20.10 on Python 3.12: the core of every Semaphore job and of the API's
# and the onboard GUI's runs. CI's pytest legs run that core (the `tests` matrix in .github/workflows/ci.yml, held to
# this line by tests/unit/test_ci_ansible_cores.py), so a base that moves it moves both (docker/README.md, step 4).
FROM semaphoreui/semaphore:v2.19.17@sha256:008dacd9f1adbd92e8a048f1786ae1dd9bed2a352aca2eb30b9837de5706e7f7

USER root

# age+sops for the SOPS secret pipeline; net-snmp-tools for the net-snmp CLI (snmpbulkwalk/snmpgetnext) that the
# discovery `snmp_walk` transport (scripts/kontroll-discover.py) + gen-validate-live's SNMPv3 GETNEXT shell out to;
# openssh-client for the discovery `ssh_read` transport (dhcp_leases_openwrt — a read-only forced-command `ssh` read
# of an OpenWrt AP's dnsmasq lease file). Absent, the relevant transport raises FileNotFoundError and the source is
# mis-recorded `unreachable` on a baked deploy (net-snmp was live-caught absent on the .52 container). Pinned by
# tests/unit/test_publish_images.py (test_control_image_ships_net_snmp_cli / _ships_openssh_client).
# Each package carries apk's `~=` constraint, the major.minor the pinned base's Alpine branch ships: a patch
# revision inside it may still move (an Alpine branch keeps only its newest revision, so an exact pin would fail
# the next rebuild), and any other version fails the build. The version lines record in the build log what was
# baked. Held by tests/unit/test_image_pins.py.
RUN apk add --no-cache "age~=1.2" "sops~=3.9" "net-snmp-tools~=5.9" "openssh-client~=9.9" \
 && age --version && sops --disable-version-check --version && snmpbulkwalk -V && ssh -V

# The #125 retention-prune (ansible/playbooks/backup-configs.yml) re-roots the local captures history with
# `git filter-branch` (core git; no git-filter-repo dep). Some distros split filter-branch out, so assert it is
# present at BUILD time — a missing primitive then fails the image build, not a 2am cron. (review COR-MF2)
RUN git filter-branch -h >/dev/null 2>&1 || (echo "FATAL: git filter-branch missing from the runner image" && false)

# Install via the never-brick wrapper (C15-a) — the SAME scripts/install-collections.py bootstrap.yml uses
# (the G-4 no-drift gate pins it), so the signature posture can't differ between the two install seams. It
# does an adaptive GPG-signature install WHERE the source serves one + a keyring is held, else a plain install
# on the pin + checksum floor. No keyring is baked here today (public Galaxy serves none) ⇒ byte-identical to
# the prior flat install. BOTH generated files are COPY'd (the lockfile + the trust sidecar the wrapper reads).
COPY ansible/collections/requirements.generated.yml /tmp/k/ansible/collections/requirements.generated.yml
COPY ansible/collections/trust.generated.yml /tmp/k/ansible/collections/trust.generated.yml
COPY scripts/install-collections.py /tmp/k/scripts/install-collections.py
RUN VENV="$(dirname "$(ls /opt/semaphore/apps/ansible/*/venv/bin/ansible-galaxy)")" \
 && KONTROLL_GALAXY_BIN="$VENV/ansible-galaxy" KONTROLL_COLLECTIONS_PATH=/usr/share/ansible/collections \
        "$VENV/python" /tmp/k/scripts/install-collections.py \
 && rm -rf /tmp/k

# The Python packages the base's ansible venv lacks: flask for the onboarding GUI (gui/app.py), fastapi + uvicorn
# for the typed API (api/), and their dependencies. Both are thin surfaces over the kontroll service layer and
# reuse this image's ansible/sops/age/collections toolchain. requirements.txt is the whole set, one exact pin
# each: `--no-deps` installs nothing it does not name and `pip check` fails the build if the set is incomplete
# or conflicts with the base. The base already ships paramiko (network_cli's transport for Cisco IOS) and
# PyYAML; the import line fails the build if a new base stops shipping either, so the file gains them by a
# re-derive (docker/README.md § Refreshing a pin), never by pip quietly moving a base library. Prebuilt
# musllinux wheels — no build toolchain needed.
COPY docker/semaphore-runner/requirements.txt /tmp/k/requirements.txt
RUN VENV="$(dirname "$(ls /opt/semaphore/apps/ansible/*/venv/bin/pip)")" \
 && "$VENV/pip" install --no-cache-dir --no-deps -r /tmp/k/requirements.txt \
 && "$VENV/pip" check \
 && "$VENV/python" -c "import paramiko, yaml, flask, fastapi, uvicorn" \
 && rm -rf /tmp/k

# A failing `no_log: true` task's error message must print nothing it was given (I-75). ansible-core 2.19 and later
# keep a censored result's `exception`, and the default callback prints its message: a `fail` message, a command line,
# a path (CVE-2026-123036, ansible/ansible#85696, no fixed release; the base at this pin ships the core named above,
# which CI's pytest legs run unpatched: I-80). The script drops `exception` from the censor's preserved keys in the venv's ansible-core, under a
# notice that the file was modified, and refuses any other shape of that file, so a base bump that moves it fails the
# build here. Then it runs, with this core, a no_log task that fails with a marker: the build fails if the marker
# prints, on stdout, stderr or in the ansible log, or if the same failure without no_log stops printing it. Warnings,
# include names and -vvv lines still pass the censor (SECURITY.md's accepted risks). No later step may install
# ansible-core again. Held by tests/unit/test_no_log_censor.py and tests/integration/test_no_log_censor_runs.py.
COPY docker/semaphore-runner/no_log_censor.py /tmp/k/no_log_censor.py
RUN VENV="$(dirname "$(ls /opt/semaphore/apps/ansible/*/venv/bin/python)")" \
 && "$VENV/python" /tmp/k/no_log_censor.py \
 && rm -rf /tmp/k

# The local canonical bare repo (instance source of truth) is operator-owned (uid
# 1000) but cloned by the runner (uid 1001) → git's dubious-ownership guard. Trust any
# repo path inside this image (single-tenant control node). See docs/local-source-of-truth.md.
RUN git config --system --add safe.directory '*'

# The privileged API/GUI commit in-container as uid 1001 with no ~/.gitconfig — give git a SYSTEM identity so
# an in-container `git commit` doesn't fail "Author identity unknown" (the C10 propose-then-promote write path,
# docs/privileged-mutation-enablement.md). Matches the install.sh / backup-configs.yml kontroll identity.
RUN git config --system user.name 'kontroll' \
 && git config --system user.email 'kontroll@localhost'

# ── Phase B: bake the immutable kontroll code + data registries (the read-root) ──────────────────────────────────
# The three code-running services (api/onboard-gui/semaphore) historically ran code only from a host /repo clone of
# the canonical. Bake the IMMUTABLE tree here so a baked deploy runs it from the image at /opt/kontroll (paths.ROOT
# derives to it: scripts/kontroll/paths.py is three dirnames up from /opt/kontroll/scripts/kontroll/paths.py). The
# MUTABLE C10 propose tree stays a thin host clone (KONTROLL_WRITE_ROOT=/propose, the paths.write_root() seam). Bake
# ONLY immutable source/data — NEVER instance/ (private overlay + secrets; the repo-root .dockerignore strips it from
# the context) and NEVER a *.generated.* instance-derived artifact (prometheus/ docker/ config/ + the GENERATED
# dashboards/grafana JSONs are NOT copied — they rot, so they stay config-as-data in the canonical). The committed
# dashboards/derived/ board-floor lock pins (gen-dashboard-floor --resolve) ARE immutable data + baked. The COLLECTION lockfile under ansible/collections/
# IS baked (collection names, not instance data — already the runner's behaviour). Build context = repo root (the
# publish-images `control` matrix entry uses context `.`). chmod a-w: nothing in the running stack writes /opt/kontroll
# (writes go to /propose) — make the read-only-by-construction posture physical for the uid-1001 runtime (review 30 §1).
COPY api/          /opt/kontroll/api/
COPY gui/          /opt/kontroll/gui/
COPY scripts/      /opt/kontroll/scripts/
COPY ansible/      /opt/kontroll/ansible/
COPY modules/      /opt/kontroll/modules/
COPY vectors/      /opt/kontroll/vectors/
COPY overrides/    /opt/kontroll/overrides/
COPY telemetry/    /opt/kontroll/telemetry/
COPY logging/      /opt/kontroll/logging/
COPY discovery/    /opt/kontroll/discovery/
COPY oui/          /opt/kontroll/oui/
COPY capabilities/ /opt/kontroll/capabilities/
COPY secret-forms/ /opt/kontroll/secret-forms/
COPY key-roles/    /opt/kontroll/key-roles/
COPY settings/     /opt/kontroll/settings/
# ONLY the committed board-floor lock pins (paths.DASHBOARD_LOCKS_DIR), NOT the generated dashboards/grafana JSONs.
COPY dashboards/derived/ /opt/kontroll/dashboards/derived/
RUN chmod -R a-w /opt/kontroll

# Restore the unprivileged runtime user the base image runs as (uid 1001).
USER 1001
