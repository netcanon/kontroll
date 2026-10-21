# Logging & Observability — analysis + plan of record

The goal the operator set: **full-fidelity troubleshooting of the whole stack from
logs.** When something breaks — a failed onboard, a crash-looping container, a backup
that captured nothing, a device that fell off the fleet — the answer should be
*queryable*, not reconstructed by hand across five silos with mismatched timestamps.

This doc (1) maps the **current** logging surfaces with citations, (2) names the
**gaps** against that goal, and (3) lays out a **layered target architecture** that
fits kontroll's shape: a single-VM homelab control plane, secret-bearing, deeply
modular (drop-in everything), where Grafana is already the planned pane of glass.

Modeled on the netcanon logging doctrine ([engineering-standards.md](engineering-standards.md)
§3) and the plan-of-record style of [qa-and-release-pipeline.md](qa-and-release-pipeline.md).
Status legend: ⬜ todo · 🟡 partial · ✅ done.

---

## 0. Constraints that shape the design

1. **Secret-bearing.** Device running-configs, SOPS values, API tokens flow through
   this system. Logs are a *secret surface* — the design must keep them clean
   (`no_log`, no secret labels, no creds in audit lines) and bounded on trusted media.
2. **Homelab scale, single VM.** No ELK/OpenSearch weight. The store must be cheap,
   filesystem-backed, retention-bounded. Label-indexed (Loki-style), not full-text.
3. **Modular / drop-in.** Adding a log source = a new scrape target file, not an edit
   to a hub. A new dashboard/alert = a new JSON/rule file. Same "add an X" test as the
   rest of the repo (PLAN.md §2.5).
4. **Reversible-first.** The foundations (rotation, structured run-logs, capture
   history) are valuable on their own and carry no new services — land them before any
   aggregation stack, so observability improves even if the Loki layer is deferred.
5. **Never WAN-exposed.** The query UI lives on the mgmt VLAN behind the same boundary
   as Semaphore/Grafana (SECURITY.md C3).

---

## 1. Current state — the logging surfaces today

Every surface, what it captures, where it lands, and whether it survives / is
queryable. Grounded in the source (citations inline).

| Surface | What | Where it lands | Rotation | Survives restart | Queryable | Secret-safe |
|---|---|---|---|---|---|---|
| **Ansible run log** | task exec, results, timing | `/var/lib/kontroll/ansible-log/ansible.log` deployed (`ANSIBLE_LOG_PATH`); `./.ansible/ansible.log` in dev — [ansible.cfg](../ansible/ansible.cfg) | ✓ logrotate `size`×`rotate`, copytruncate — [deploy-stack.yml](../ansible/playbooks/deploy-stack.yml) | ✓ persisted host dir | grep only, unstructured | ✓ `no_log` on secret tasks |
| **Ansible stdout** | `yaml` callback output | Semaphore job → Postgres | ✗ | ✓ (DB) | Semaphore UI only | ✓ `no_log` |
| **Semaphore job output** | per-job stdout/stderr | Postgres `task_output` | ✗ | ✓ (DB) | Semaphore UI / API only | depends on play |
| **Device config captures** | running-config / exports | `/var/lib/kontroll/backups/<label>_<host>.cfg`, **overwritten each run** — [backend_netcommon_cli/tasks/backup.yml:26,42-48](../ansible/roles/backend_netcommon_cli/tasks/backup.yml) | ✗ latest only | ✓ (host 0700) | filesystem | ✓ `no_log` on write |
| **Capture run result** | per-host captured/skipped/failed | printed into the job output — [backup-configs.yml](../ansible/playbooks/backup-configs.yml) | ✗ | only in Semaphore DB | not indexed | ✓ no secrets |
| **Container logs** | each service stdout/stderr; the API's and the GUI's stderr also carry the in-process generators' `ignoring: instance/fleet.yml: …` lines (a fleet line a regeneration skipped — no run id, not an audit entry; a capability change over a fleet file with no list is refused before any generator runs, and that refusal IS an audit entry: `repository-refuse`) | Docker default `json-file` — **no `logging:` block on any fragment** ([semaphore.yaml](../docker/services/semaphore.yaml), homepage/onboard-gui/prometheus/grafana) | ✗ unbounded | ✗ lost on container rm | `docker logs` only | per service |
| **GUI audit log** | auth + onboard + `repository-refuse what=<kind> route=<endpoint>` (the repository's own data or layout stopped the request: a module file, a credential field's declaration, a backend with no role, a write target — 409, never a refused field) + `request-refuse` (a request field the boundary refused, on any route but onboard — which writes `onboard-refuse` with `reason=request-boundary field=<name>`, or `reason=planner-guard` for a refusal that is not about one field — : the endpoint and the FIELD name, **never the value** — SECURITY.md C22) + `capability-promote`/`capability-result` + `secret-apply` (rotation: field NAMES + the `overwrite=` marker — never a value) + `unit-{list,create,result}` (the app-store create-unit flow: collection/kind/name/blast NAMES + run_id, **never a credential** — no create input is a secret) + `pending-discard`/`pending-discard-result` (FF-race Move 2: the reaper un-stages a stale proposal — action run_id + target run_id + the deleted sha, **never a promote**, never a secret) + `backup-{index,revisions,view,diff}` (the C14 capture-viewer reads: file + rev + redacted flag, **never a capture value** — reading a secret-bearing capture is itself the auditable event) action/result (never creds) | `/audit/onboard-gui-audit.log` (`KONTROLL_GUI_AUDIT_DIR` host bind — M11, OFF the `/repo` code/propose clone so it survives a read-only baked `/opt/kontroll` + the clone `reset --hard`), TSV `ts⇥ip⇥action⇥detail` — [gui/app.py](../gui/app.py) (`_audit`); Vector tails it via the `/host/gui-audit` mount | ✓ 5 MB × 5 | ✓ (host mount) | flat file | ✓ creds excluded by design |
| **API audit log** | privileged calls (`onboard-apply`, `capability-promote`, …) + `repository-refuse what=<kind> route=<template>` (the repository's own data or layout stopped a privileged request — 409) + `request-refuse` (a privileged request whose field the boundary refused: the route's path TEMPLATE and the FIELD name, never the value — best-effort, nothing was mutated; the onboard route writes `onboard-refuse` with `reason=request-boundary field=<name>`, or `reason=planner-guard` for a planner refusal that is not about one field; an open inquiry route writes none) + the on-demand `validate-live` drift checks + auth-denials + rate-limit breaches (never creds) | `KONTROLL_API_AUDIT_LOG`, TSV `ts⇥user⇥ip⇥action⇥run_id⇥detail` — [api/audit.py](../api/audit.py); every field is flattened by the one function the GUI's writer shares ([kontroll/auditfmt.py](../scripts/kontroll/auditfmt.py): a tab or any line break in a request value becomes a space, so it cannot add a column or a row); the validate seam reuses the same writer ([scripts/gen-validate-live.py](../scripts/gen-validate-live.py), control-VM) | ✓ 5 MB × 5 | ✓ (host mount) | flat file **+ `GET /audit/log`** | ✓ creds excluded (NAMES only); **run_id-correlated** |
| **Script output** | `update/backup/state-*` + `galaxy.py` | stdout/stderr only — no persistent run-log | n/a | ✗ unless wrapped | ✗ | mixed |
| **Host / Docker daemon** | daemon, crash-loops, systemd | journald (implicit, undocumented) | journald default | ✓ | `journalctl` on the host | ✓ |

What's already **right**: `no_log: true` on every secret-touching task (the ansible log
is safe to ship), a central `log_path`, the GUI audit log deliberately excludes creds,
the `-vvvv`-on-schedules prohibition (CLAUDE.md). The foundation is sound; it's just
**not durable, not correlated, and not queryable from one place.**

---

## 2. Gaps against "full-fidelity troubleshooting"

- **G1 — No persistence/rotation.** Container logs use the default `json-file` driver
  with no `max-size`/`max-file`; a crash-loop (`restart: unless-stopped`) churns logs
  and the evidence rolls off. The ansible log grows unbounded (the GUI + API audit logs are size-rotated).
- **G2 — No correlation.** A single "why did onboard X fail?" spans the GUI audit log,
  galaxy.py stdout (returned to the browser, never stored), the ansible run log,
  Semaphore's DB, and maybe host journald — five silos, no shared job/trace id.
- **G3 — No config history.** Captures overwrite; you can't diff a device's config
  against yesterday's — the single most common "what changed?" question is unanswerable.
- **G4 — No run history off-Semaphore.** Job output lives only in Postgres; scripts log
  nowhere persistent. No host-side "what ran, when, succeeded?" trail.
- **G5 — Host observability undocumented.** Docker-daemon/systemd logs exist in journald
  but aren't part of any documented troubleshooting path or surfaced anywhere.
- **G6 — No alerting.** ✅ CLOSED — Grafana-managed unified alert rules
  (`dashboards/grafana/provisioning/alerting/logging-rules.yaml`) watch container restarts,
  backup failures, onboard errors, and `auth-denied` spikes (Layer 3 below).

---

## 3. Target architecture — four layers

Built bottom-up. **Layer 0 needs no new services and lands first**; Layers 1–3 add the
aggregation stack, naturally alongside the Phase-5 Grafana deployment.

### Layer 0 — Foundations: make every surface durable, bounded, structured  🟡
*No new services. Reversible. Valuable even if Layers 1–3 are deferred.*

- ✅ **Container log rotation** — a **global Docker daemon default** (`/etc/docker/daemon.json`:
  `json-file`, `max-size 10m` × `max-file 5`), set by `deploy-stack.yml`. One place bounds
  every container — current and future — so a new service inherits rotation with zero action
  (more modular than per-fragment `logging:` blocks). Closes engineering-standards §3b (G1).
  **Gotcha:** `systemctl reload docker` does NOT apply log-opts — a **daemon restart** is
  required (the playbook handler does this). **Verified live:** a 55 MB emitter capped at
  5 files × ~10 MB (46 MB total) vs a single unbounded 26 MB file before the restart.
- ✅ **GUI audit-log rotation** — `gui/app.py` `_audit()` now routes through a
  `RotatingFileHandler` (5 MB × 5), format preserved, creds still excluded (test-covered) (G1).
- ✅ **Run correlation key** — a shared `_log-run-id.yml`, imported first by the fleet
  playbooks (ping/backup-configs/deploy-stack/promote), logs a `kontroll_run_id` at the start of
  every run — into the ansible log AND the Semaphore job output (Postgres-backed, queryable
  via the task API), so one operation is greppable by id (G2/G4). Synthetic by default
  (UTC stamp); a caller may inject `-e kontroll_run_id=…`. (Investigated: Semaphore does
  not expose its task id as a runner env var, so an in-playbook id is the reliable mechanism;
  it co-appears with the Semaphore task id in the job output for cross-reference.)
  *Durability enhancement (since shipped):* a persistent `ANSIBLE_LOG_PATH`
  (`/var/lib/kontroll/ansible-log`, surviving the ephemeral job clone) **and** a host
  logrotate bound on it (`deploy-stack.yml` drops `/etc/logrotate.d/kontroll-ansible-log`,
  `copytruncate` so Vector's exact-path tail survives rotation; `test_ansible_log_rotate.py`).
- ✅ **The promote's own line** — before it moves anything, `scripts/kontroll-promote.py` prints
  `kontroll_run_id=<the proposal's run id> promote proposed/<run id> commit=<commit id> -> main (was
  <commit id>); commits=<n>` and then the commits it lands (id and subject, escaped where a subject
  is not text). It goes to the Semaphore job output for the `promote-proposal` task, or to the
  operator's terminal. The run id is the PROPOSAL's — the same id the propose-side audit line
  carries, so the two ends of one change meet on it — and the commit id is what was approved: the
  promote lands exactly that commit or nothing (SECURITY.md C10). No credential is in it. The
  `promote-proposal` job's log also carries the job's OWN stamp from `_log-run-id.yml`
  (`kontroll_run_id=<UTC stamp>`), printed first; the promoter's line is the one that ends in
  `commits=<n>`.
- ✅ **The landing's own line** — `scripts/kontroll-land.py` (what `scripts/update.sh`, `update-from-bundle.sh`,
  the operator CLI's `--commit` and the deploy's `local-canonical.yml` run to take a commit to the canonical's
  `main`, SECURITY.md C10) prints, before it judges anything, `kontroll_run_id=<id> land [--check] HEAD of
  <worktree> -> refs/heads/main of <canonical>` — the id is a minted stamp (`<UTC stamp>-<6 hex>`, the same
  shape as a request's), or the run id of the proposal `--propose` makes — and then its verdict on one line:
  `landed refs/heads/main <commit> (was <commit>)`, `up to date`, `would land` (`--check`), or `REFUSED: …` /
  `kontroll-land: …` on stderr with the exit status the caller reads (0 / 2 / 1). It goes to the deploy's
  Ansible log (the play's `--check` task and its landing task both register it), to the update script's
  run-log (`scripts/lib/run-log.sh`), or to the operator's terminal. No credential is in it; the gate's
  sentence can name a module file and, for a `signature_policy` that is not a word it takes, repeat the
  value — as the promote's does.
- ✅ **Capture history** — `backup-configs.yml`'s final play commits a timestamped
  snapshot of the captures dir into a **local-only git history** (no remote, inside the
  0700/uid-1001 dir, never pushed — as protected as the captures). `git diff` now answers
  "what changed on this device?" (G3). **Verified live** via a real Semaphore backup run:
  `/var/lib/kontroll/backups/.git` created, captures tracked, a `git diff` cleanly shows a
  device config delta.
- ✅ **Capture-exception matrix** — some captures misbehave (the **FortiGate** full-config
  export is non-deterministic: ~570 non-secret lines re-serialize each fetch, so an
  *unchanged* config still diffs ~600 lines). Rather than a per-vendor hack, a **sparse,
  member-only registry** ([config/capture-exceptions.yml](../config/capture-exceptions.yml))
  declares only the anomalies — the `overrides/` pattern applied to capture *behaviour*. The
  history play renders the captures-dir `.gitignore` from the matrix's `exclude_from_history`
  members, so a non-diffable capture is **kept on disk (latest) but not versioned** — no
  noise commits, history stays meaningful for the deterministic fleet. **Verified live:** two
  back-to-back Semaphore backups → one cleanup commit (untrack FortiGate + write `.gitignore`),
  then **zero** further commits. Extensible by one row (by hand, or GUI→git ahead); it's
  instance state (state-manifest.yml), so operator/GUI additions survive updates + DR.
- ✅ **Script run-logs** — a shared `scripts/lib/run-log.sh` (`run_log_init <name>`) tees a
  script's stdout/stderr to a timestamped, 30-day-pruned log in an operator-writable dir
  (`${XDG_STATE_HOME:-~/.local/state}/kontroll/runs`), via an `exec`-redirect (no pipe-subshell
  exit-code bug). Sourced fail-soft by `backup/update/state-snapshot/state-restore` — each
  gains a persistent "what ran, when" trail (G4). Operator-owned (these run as the operator,
  not the uid-1001 runner).

### Layer 1 — Collection: one agent tails everything  🟡 built (`feat/logging-vector`)
- **Vector** (vector.dev) — the collector, **DECIDED 2026-06-16** (was Alloy; the dual-purpose logging
  capability §3.5 made Vector's novel/pull-source breadth load-bearing — see §6). A drop-in
  `docker/services/vector.yaml`, reading: container logs (`docker_logs`), the host journald (docker daemon +
  systemd), the GUI audit log, the API audit log, the ansible run log, and the script run-logs. Sources are
  **drop-in config files** under `docker/vector/config.d/sources/<source>.yaml` (+ a sibling
  `transforms/<source>.yaml` shaping to the label schema), loaded by `--config-dir` implicit namespacing —
  add a source = add a file, never edit a hub. The one Loki sink (`config.d/sinks/loki.yaml`) is the label
  choke-point. Every shape sets each field the sink templates as a label: `run_id` defaults to `""`, which
  Loki stores as no label, and a plain-text container line is `level=info`. A missing field fails the label
  template on every event — a WARN per event and the label dropped — so `tests/unit/test_compose_logging.py`
  holds every input of the sink to it. A journald entry's `level` is its `PRIORITY`'s syslog name (`err`,
  `warning`, …) and one without a `PRIORITY` is `info` (`docker/vector/tests/internal_levels.yaml`). Promtail
  avoided (deprecated). (Built on branch; pending the dogfood deploy.)

### Layer 2 — Store + query: Loki  🟡 built (`feat/logging-vector`)
- **Loki**, single-binary, **filesystem** store (`docker/services/loki.yaml` + `docker/loki/loki-config.yml`),
  TSDB schema v13, **bounded retention** — 30 days global (`${LOKI_RETENTION_PERIOD:-720h}`, compactor-driven),
  with per-stream overrides (debug 7d; the api-audit/gui-audit security streams 90d). **Per-class capability
  retention is ENACTED** (not just declared): a device class's `retention` param (`7d`/`30d`/`90d`) is emitted by
  `gen-logging.py` into a **generated `runtime_config` overrides file** (`docker/loki/overrides/retention.generated.yaml`,
  keyed on tenant `fake`, hot-reloaded — no restart, no hub edit). Fail-closed: absent ⇒ `overrides: {}` ⇒ the
  bounded global default. The seam + its gotchas (tenant `fake` not `*`; 24h period floor; an invalid file is
  fatal at startup so the generator only emits valid ≥24h periods + an always-valid default; a per-tenant
  override REPLACES the static list so the debug/audit baseline is carried forward) were dogfood-verified on
  Loki 3.4.2 — see [reviews/2026-06-16-storage-logging/40-m1-loki-retention-dogfood.md](reviews/2026-06-16-storage-logging/40-m1-loki-retention-dogfood.md).
  The store is a **bind
  mount** at `${KONTROLL_LOGS_DIR}` (default `/var/lib/kontroll/loki`, `0750`/uid-10001 — relocatable to a data
  disk, the chown follows the path; SECURITY.md C8) — inside the C8 crown-jewel-at-rest boundary, since
  log bodies may carry a leaked secret. **Canonical label schema** (low-cardinality, no secrets, the C12 hard
  rule): `source`, `host`, `service`, `level`, `run_id` (the Layer-0 correlation key), `device` (capability
  streams only). Query via **LogQL** in Grafana Explore — the cross-silo "one pane" that G2 needs. (Built on
  branch; pending the dogfood deploy.)

### Layer 3 — Surface + alert: Grafana  🟡 (dashboard + alerts BUILT; runbook pending)
- **Grafana Explore** over Loki (ad-hoc LogQL) + a hand-authored **logs dashboard**
  (`dashboards/grafana/dashboards/logs.json`, auto-loaded by the file provider) and **alert
  rules** as a provisioning drop-in (`provisioning/alerting/logging-rules.yaml` + a default
  contact point): container restart, backup-config failure, onboard error, `auth-denied`
  spike (**G6 CLOSED**). NO separate Loki ruler — Grafana-managed unified alerting. Grafana is
  already the Phase-5 metrics UI → metrics + logs in **one pane**, correlated by time +
  `run_id`. Hermetic shape tests run in CI; live rendering is verified post-deploy. Levels arrive in more
  than one spelling (syslog `err`/`warning` from journald, a JSON line's own `ERROR`/`warn`), so the error/warn
  panel matches all of them, case-insensitive (a plain-text container line is `info`, whatever it says). A
  container stream's `service` is the container's name, so the crash rule picks container lines by the body's
  `container_name`. It drops lines that name the rule itself (Grafana logs its uid at every evaluation) and
  ansible's `TASK [` / `RUNNING HANDLER [` banners; its query is pinned as measured on real traffic, and
  `docs/troubleshooting.md` §2 carries it. The tests hold every rule to selecting only a service some shape emits.

---

## 3.5 — Dual purpose: internal logs AND a logging CAPABILITY  ⬜  (operator requirement, 2026-06-16)

Layers 1–3 scoped the store/collector to kontroll's OWN logs. The operator wants Loki **dual-purpose**: also the
sink for a new **logging capability** — capability-derived device/service log ingestion, isomorphic to the
telemetry capability.

**Two purposes, one store:**
1. **Internal logs** — the kontroll stack's containers + host journald + the GUI/API audit + the ansible run-log +
   the script run-logs (Layers 1–2).
2. **Capability-derived logs** — for each ONBOARDED device/service, detect whether it has a graceful log-export
   path and wire it up: a switch/firewall's **syslog**, a Linux host's **journald**, Proxmox's **task/journal**, an
   app's **REST log API**, a file **tailed over SSH**. The detection is a new **capability-vector dimension**
   (`logging`, alongside actuate/telemetry/backup) — programmatic, from the class's known features OR a dynamic
   probe ("is syslog configurable? journald present? a log API exposed?").

**Does Loki "Do The Thing"? — YES for the STORE; the ingestion is a SEPARATE layer.** Loki is a label-indexed
log-LINE store + LogQL query, Grafana-native, homelab-weight — right for BOTH internal and device logs (both are
log lines). Loki does NOT collect; it receives what the collector pushes. So "capability-derived ingestion" is a
**collector + capability-registry** concern, not a Loki one. Loki stays. (Loki is NOT for SIEM-grade full-text /
high-cardinality event analytics — out of scope for a homelab, and would mean the OpenSearch weight §0 ruled out.)

**Collector — Vector over Alloy. DECIDED (operator, 2026-06-16).** The capability layer's heterogeneity (varied
+ NOVEL device export, incl. PULL-based API/command sources) made **Vector** (vector.dev) the load-bearing pick:
its source breadth (`syslog`, `journald`, `file`, `docker_logs`, `http_server`/`http_client`, `socket`,
**`exec`**) + VRL transforms + a first-class `loki` sink make it the strongest fit for "ingest from available or
novel dynamic sources," AND it handles the internal logs too — one collector, both purposes, → Loki. Alloy was
the Grafana-native alternative (weaker for pull/exec/novel sources; and kontroll already uses Prometheus for
metrics, so Alloy's unified-agent appeal was moot). The decision is now locked and the internal stack is built
on `feat/logging-vector`. (NB: the operator's "vector that determines graceful export" means a
capability-detection FEATURE VECTOR — distinct from, but serendipitously aligned with, the Vector collector.)

**The logging capability — isomorphic to telemetry/backup:**
- `logging/<method>.yml` descriptors (peers of `telemetry/<name>.yml`): each export method — `syslog_push`,
  `journald_remote`, `rest_pull`, `file_tail_ssh`, … — declares how to DETECT applicability + the Vector source
  config + the device-side WIRING (an Ansible role: configure syslog-forwarding / enable remote journald / …).
- Each device-class `module.yml` declares `logs: [{method: <log-method>}]` (peer of `metrics:`).
- A `logging` dimension in the capability vector + the suggester; a probe answers "graceful export? + how?".
- `gen-logging.py` fans methods × inventory → the Vector pipeline drop-ins (`docker/vector/generated/`, a
  disjoint config-dir with explicit component ids) + the device-side wiring roles. Same
  generated-not-hand-maintained, drop-in, "add an X" discipline as metrics/backup.
  - **Data-driven shaping (S9):** the rendered shaping transform is two halves — a GENERIC baseline (structural
    `.source`/`.device` + the canonical `.service`/`.host`/`.level` fallbacks) plus the method's own field
    DERIVATION, declared as a `transform_vrl:` list in `logging/<method>.yml` and spliced in BEFORE the
    defaults. So `syslog_push` maps RFC5424 `.severity` → `.level` and `journald_remote` maps `._SYSTEMD_UNIT`
    → `.service` + `PRIORITY` → `to_syslog_level(...)` — the same derivation the live internal journald
    transform uses — WITHOUT `gen-logging` ever branching on the method name. Adding a derivation is a registry
    edit, not a generator change; the field is shape-validated at generate time; nothing compiles the rendered
    VRL before Vector loads it — the gate's `vector validate` reads the hand-written tree only (SECURITY.md C22,
    known gaps).
  - **Pull fan-out + dedupe (data-driven):** a PULL method that returns `{<field>:[...]}` can declare
    `pull_unnest: <field>` (fan to one event per element via `. = unnest!(.<field>)`, `is_array`-guarded) and
    `dedupe_key: <path>` (a Vector `dedupe` keyed on a unique field, since a recent-window poll re-ingests).
    `gen-logging` renders the chain `src → [unnest] → [dedupe] → shape` — each stage only when its field is
    present, the `inputs:` threaded so the shape consumes the last stage — again with NO method-name branch.
    `proxmox_api` uses this to turn the PVE `/cluster/tasks` array into one deduped event per task. Modularity
    verified by the `docs/reviews/2026-06-16-pull-modularity/` blackboard; the full pull pipeline (unnest →
    dedupe → shape) was dogfood-verified end-to-end into Loki — 50 events, 50 distinct upids, all shaped — after
    which three VRL fallibility fixes (`unnest!`, the infallible `if !is_string` baseline default, dropping an
    unnecessary `encode_json … ?? …`) were folded into the generator/descriptor (see CHANGELOG [Unreleased]).
- Runs the existing **PROPOSE→PROMOTE→ENACT** loop (the capability dialog), exactly like telemetry/backup.

**Label-schema add:** `source` (internal|device), `capability` (logging), `method` (the export method), alongside
Layer 2's `service/host/level/run_id/device`. No secret-valued labels (C8 unchanged); device-side log config that
carries a collector address/credential follows the SOPS path, never a committed plaintext.

---

## 4. Security — logs are a secret surface

- **At the source:** `no_log: true` already keeps creds out of the ansible log (the only
  log that ever sees decrypted SOPS); the GUI audit log logs *who/when/what*, never creds;
  scheduled jobs never run `-vvvv`. Layer 0 changes preserve all three.
- **In transit/store:** Vector ships only already-clean sources; **no secret-valued
  labels** (the label schema is a closed, structural allow-list — SECURITY.md **C12**). Loki + Grafana bind
  to the mgmt VLAN only, behind the existing boundary (SECURITY.md C3) — never WAN. Vector publishes only the
  mgmt-bound syslog ingress (`${KONTROLL_MGMT_IP}:5514` for `syslog_push`; its API stays loopback-internal);
  Loki's `:3100` binds `${KONTROLL_MGMT_IP}` only.
- **At rest:** the captures store is local, `0700`/uid-1001; the run-logs (the Ansible run-log, the API and GUI
  audit logs) are local and uid-owned, in `0755` dirs non-root Vector reads by the bit for others, so under the
  storage root's `0711` every local account can reach them (SECURITY.md C10); all are retention-bounded (C8
  "local state at rest is the trust boundary"). A leaked log/snapshot reveals ops metadata, not credentials. →
  a new **SECURITY.md control** lands with Layer 0.

---

## 5. Modularity mapping (the "add an X" test)

| Add a… | …is a drop-in | not an edit to |
|---|---|---|
| log source (internal) | `docker/vector/config.d/sources/<source>.yaml` (+ a `transforms/` shaper) | a central collector config |
| log source (capability) | generated by `gen-logging.py` into `docker/vector/generated/` (a disjoint config-dir) | a hub |
| device's log export (wiring) | `roles/logging_<method>/` + `wire-logging.yml` (`-e role= -e target=`) | a per-vendor playbook |
| container log rotation | the global `/etc/docker/daemon.json` default (once) | each fragment by hand |
| dashboard | `dashboards/grafana/<name>.json` | a hub dashboard |
| alert rule | a drop-in rule file | a god rules file |
| device's capture history | automatic — keyed by `device`/`host` label | n/a |

Same doctrine as inventory drop-ins, capability vectors, and backends: the collector and
the store are hubs that *read* drop-ins; you never edit the hub to add a source.

---

## 6. Sequenced rollout + what needs an operator decision

1. ✅ **Layer 0 foundations** (rotation, run-log durability + `run_id`, capture history, script run-logs) — DONE
   (no new services; reversible; high value alone).
2. 🟡 **Layer 1+2** (Vector collector → Loki store) as drop-in compose fragments, alongside the deployed Grafana
   — one pane. **Built on `feat/logging-vector`** (the fragments, configs, the Grafana Loki datasource, the
   two Layer-0 durability fixes — API audit off `/tmp`, persistent `ANSIBLE_LOG_PATH`); pending the dogfood
   deploy + the logs dashboard/alerts (Layer 3). **Collector = Vector** (DECIDED, §3.5).
3. ✅ **§3.5 the logging CAPABILITY** — `logs:` module block + `logging/<method>.yml` descriptors + the `logging`
   capability-vector dimension + `gen-logging.py` (Vector pipelines under `docker/vector/generated/`) + the
   device-side wiring roles (`roles/logging_{syslog_push,journald_remote,file_tail_ssh}` + `wire-logging.yml`,
   the mgmt-bound `:5514` syslog ingress) + the PROPOSE→PROMOTE→ENACT dialog — **BUILT on `feat/logging-vector`**.
   `syslog_push` is end-to-end-provable; the **`journald_remote` receiver is BUILT + bench-proven to Loki** (S10):
   `systemd-journal-upload` → `roles/logging_journald_receiver` (systemd-journal-remote on the mgmt IP `:19532`) →
   Vector `exec` journalctl on `/var/log/journal-remote` (NOT an http_server — the upload is the binary export
   format, live-disproven + reverted; `local/s10-journald-receiver-bench-findings.md`). The **`file_tail_ssh`
   remote PULL is BUILT** (S11): Vector's `exec` source runs `ssh root@<host>` and the host's forced-command read
   key runs `journalctl -f -o json` (`roles/logging_file_tail_ssh`), shaped by the same journald derivation — the
   PULL twin of `journald_remote`'s push (no uploader unit, no inbound socket). The read key is the
   `logging_file_tail` SOPS keypair (private half rendered to a file + bind-mounted RO into Vector, the 3rd
   injection shape); because the stock Vector image has no ssh client (bench-verified), deploy-stack builds + uses
   a thin `kontroll/vector` image (stock + openssh-client) ONLY when a class declares the method.
4. ✅ **Layer 3** dashboards + alerts — `dashboards/grafana/dashboards/logs.json` + the Grafana-managed alert
   drop-in (`provisioning/alerting/logging-rules.yaml`; **G6 closed**). Live rendering verified post-deploy.
5. ✅ SECURITY.md **C12** (logs are a secret surface) + the **[`docs/troubleshooting.md`](troubleshooting.md)
   runbook** — the signal map (which `source`/`service` each surface lands under), the common LogQL queries, and
   the **degrade path** (debugging when Loki/Vector is down: `docker logs`/journalctl/the flat audit files).

**Recommendation (rev 2026-06-16):** Loki (store) + **Vector** (collector) + Grafana — single-binary Loki,
filesystem store, bounded retention, label-indexed; Vector for heterogeneous INTERNAL + capability-derived
ingestion (§3.5). Promtail avoided (deprecated).

**Operator decisions — RESOLVED 2026-06-16:** (a) **collector = Vector** — DECIDED (was the load-bearing fork;
now locked, internal stack built). (b) **build scope** — Layer 0 (done) → Layer 1+2 internal stack (built) →
the logging capability (Theme 2). (c) **retention** = 30 days global (`720h`), set via `LOKI_RETENTION_PERIOD`
in `docker/.env` (compactor-driven), per-stream overrides for debug (7d) + audit (90d). (d) **detection model**
= **BOTH** — static `logs:` module declaration is the source of truth (the dominant journald/file/docker sources
are host facts no probe sees), the dynamic graceful-export probe is a non-binding `suggest_logging` enrichment
(the cliconf/netconf `syslog_push` nudge). See §3.5 + the capability design.

## See also
- [engineering-standards.md](engineering-standards.md) §3 — the logging doctrine this implements
- [qa-and-release-pipeline.md](qa-and-release-pipeline.md) — sibling plan-of-record (CI/QA)
- [SECURITY.md](../SECURITY.md) — C3 (reachability), C8 (local state at rest); a logging control lands here
- [PLAN.md](../PLAN.md) — Phase 5 (metrics) — Loki deploys alongside Grafana
- [observability/telemetry-capability-vector.md](observability/telemetry-capability-vector.md) — the capability-vector model §3.5's logging capability extends
- [observability/logging-capability-vector.md](observability/logging-capability-vector.md) — the BUILT logging capability (instance #3): the vector, the `logging/<method>.yml` registry, `gen-logging.py`, the `logs:` block
- [observability/siem-integration-plan.md](observability/siem-integration-plan.md) — PLANNED (not built): where normalization belongs if logs ship to a SIEM — deep parsing stays SIEM-side, build a `destinations/` sink-branch + identity tags, keep raw
- [api-architecture.md](api-architecture.md) — the API integrates Layer 0 (run_id correlation, audit)
- [testing-standards.md](testing-standards.md) §5 — how logging ties into the test-after-change loop
