# The generalized secondary-capability dialog seam (design of record)

> **Status: BUILT — Capability-track Phase 7 shipped (the generic seam + telemetry as instance #1).** Built: `scripts/kontroll/service/{promote,observe,capability}.py`, `api/routes/capability.py`, the `capabilities/` registry + `capabilities/telemetry.yml`, `catalog.load_capabilities()`/`registered_capabilities()`, per-descriptor `applies_when` on the telemetry methods, `openCapabilityDialog` + the `/api/capability/<cap>` Flask routes, the registry-driven privileged tripwire, and the INVARIANT D\* pins (`tests/unit/test_invariant_d.py`). **Not yet built (Phase 8 — instance #2):** `scripts/kontroll/service/backup.py`/`scripts/gen-backup.py`, a `backup:` block in any `module.yml`, `capabilities/backup.yml`. This doc is now a built-system reference; the worked **backup** descriptor (§2.2) + §4 remain the Phase-8 plan.
>
> Subordinate to [`docs/observability-onboarding-flow.md` §4.0 SHARED CONTRACTS](../observability-onboarding-flow.md) — **§4.0 wins** on any symbol/schema conflict. The telemetry instance's UX detail lives in [gui-actuatable-flow.md](gui-actuatable-flow.md) (now **instance #1** of this seam).

## 0. Why this exists

The operator's insight: **observability and backup are the same shape** — a *secondary capability* added **after** onboarding, never gating it, each with its own post-onboard dialog. Rather than build two bespoke dialogs, this design generalizes the (design-only, already ~90% capability-agnostic) standalone observability dialog into ONE reusable seam, so **telemetry is instance #1** and **backup is instance #2** (and future caps — secrets-rotation, compliance-scan — are drop-ins).

Every secondary capability has the identical four-part shape:

1. a capability **vector** detects suitability (read-only suggester) — `vectors/telemetry.yml` (Phase 4 ✓) and `vectors/backup.yml` (✓) both already exist;
2. a **declarative block** in `modules/<key>/module.yml` is the declared source of truth (`metrics:` for telemetry; a new `backup:` for backup);
3. a **generator** turns the block into the actuation artifact (`gen-observability.py` → Prometheus targets; a new `gen-backup.py` → a Semaphore backup-schedule spec);
4. a **standalone, anytime, post-onboard dialog** (propose-then-promote, keyed by an already-onboarded class, never gates onboarding, nudge-driven) actuates it.

## 1. Base decision — EXTEND-AS-TEMPLATE

**Lift the already-agnostic spine verbatim; push telemetry-specific bodies down into a per-capability descriptor + service-impl; register telemetry as instance #1.** *Not* a from-scratch rebuild, *not* a telemetry-baking rename-in-place.

- The dialog/route/service is **design-only** — there is zero built code to migrate, so generalizing now is a doc/parameterization pass at **zero migration cost**.
- The spine is **~90% capability-agnostic already**: `promote.py`'s `plan_token(*parts)`/`verify_token` is PURE hashing over arbitrary text (no telemetry coupling); INVARIANT D, the PROPOSE/ENACT boundary, the 6-stage wizard, the render-only nudge, and the keyed-by-onboarded-class 404 are agnostic-by-content and telemetry-**by-naming** only.
- A rebuild discards a §4.0-reconciled design for no structural gain; a naive rename-in-place would leave telemetry bodies (the `metrics:`/`dashboards:` write, the target regen, the exporter/agent enact set) in the **shared** layer, forcing `if cap=="backup"` branches that violate CLAUDE.md's one-dispatch-seam rule and fail the PLAN.md §2.5 "add an X" test.

So: keep the spine shared + named generically (`openCapabilityDialog`, `/api/capability/<cap>`, `promote.py` verbatim) and dispatch telemetry-vs-backup through **one frozen capability descriptor** — the `device_role`/`get_collector()` one-dispatch-seam doctrine applied to secondary capabilities.

**Scope-staging:** the seam machinery (shell-over-descriptor dispatch, the render-table, the registry-parametrized pins) is built in **Capability-track Phase 7** with telemetry as instance #1. **Backup (instance #2 / Phase 8)** is the empirical proof the seam generalizes — registering it must touch **zero** spine file. The PLAN/doc updates land now; the machinery is gated to Phase 7.

## 2. The frozen capability-descriptor schema

One schema, one location, one field name per concept — **pinned in master §4.0** so the three design docs reconcile to it, not to each other.

- **Flat file `capabilities/<cap>.yml`** (not a per-cap dir) — matches the `vectors/*.yml` flat precedent and the byte-identical sorted-glob loader family (`catalog.py:27-76`). The per-capability *method* registry already lives separately (`telemetry/<method>.yml`).
- Loaded by a new **`catalog.load_capabilities()`** — the 5th sorted-glob-by-order loader (mirroring `load_vectors`/`load_telemetry`/`load_overrides`/`load_backends`).
- **`enact_kind` is an enum selecting a CODE-resident enact builder** — never inline YAML command strings (a shell-injection surface + a duplicate of the enact table).

```yaml
# capabilities/<cap>.yml — the secondary-capability descriptor (a drop-in).
# Adding a capability = THIS file + vectors/<cap>.yml + gen-<cap>.py + a suggest_<cap>/declared_<cap>
# pair in classify.py. NEVER an edit to the dialog shell, the /api/capability route, promote.py,
# the generic decoupling pins, or configure-semaphore.py.
name:        <cap>            # registry key == filename stem == the route segment & ?cap= value
label:       "<human one-liner>"   # dialog header + nudge copy
order:       <int>           # load/sort order (telemetry 10, backup 20)

vector:      <cap>           # vectors/<cap>.yml whose cell the nudge reads (PURE; a hint, never a gate)
suggester:                   # resolved by import-by-convention (§3.3), NOT a hand-maintained god-map
  module:    classify        # the python module under scripts/kontroll/service/
  suggest:   suggest_<cap>   #   -> {cell, candidates, note}
  declared:  declared_<cap>  #   -> the DECLARED-side reconciliation reading the block
  hint:      {}              # OPTIONAL per-cap plugin->candidate map (DATA here, so adding a cap never edits classify.py)

block_key:   <metrics|backup>      # the module.yml block this capability declares
block_shape: list | mapping        # metrics: a LIST (a class may declare N methods); backup: a single MAPPING

method_source: registry | role_entrypoint   # registry -> a telemetry/<method>.yml row; role_entrypoint -> the class's tasks/backup.yml
method_registry: telemetry | null
render_method_stage: true | false  # false -> SKIP the picker, inline a one-line confirmation (no empty stage)

resource_stage:                    # the per-capability "second concern" (Stage 3)
  testid:      <cap-<cap>-dashboards|cap-<cap>-policy|null>   # cap-<cap>- then [a-z0-9-], ≤63 (else dropped, T22)
  source_kind: curated_list | policy_fields | none
  fields:      []                  # iff policy_fields: ordered [{name,type,default,enum?,help}] — only ENACTABLE values

generator:
  script:        scripts/gen-<cap>.py
  lockfile_glob: "<the --check set-math glob>"   # validate.sh adds the --check honesty step from this

enact_kind:   prometheus_reload | semaphore_schedule   # selects a CODE-resident enact builder
secret_domain_field: <descriptor_field|block_field|null>   # which field names a SOPS domain; audited by NAME only
trust_note:          "<one line the dialog renders + a SECURITY.md cross-ref>"
```

### 2.1 Worked descriptor — telemetry (instance #1, zero behavior change)

Every field points at an artifact that already exists; nothing telemetry is rewritten.

```yaml
name: telemetry
label: "monitoring (metrics into Prometheus)"
order: 10
vector: telemetry                 # vectors/telemetry.yml (order 4) — Phase 4 ✓
suggester:
  module: classify
  suggest:  suggest_telemetry     # classify.py — PURE, host_node trailing
  declared: declared_metrics_methods
  hint:                           # = today's classify._TELEMETRY_HINT, MOVED into the descriptor as data
    httpapi: ["proxy-exporter (api)", "host_node"]
    cliconf: ["snmp", "blackbox", "host_node"]
    netconf: ["snmp"]
block_key: metrics
block_shape: list                 # proxmox declares host_node AND pve -> list, ADD-ONLY insert
method_source: registry
method_registry: telemetry
render_method_stage: true
resource_stage: {testid: cap-telemetry-dashboards, source_kind: curated_list, fields: []}
generator: {script: scripts/gen-observability.py, lockfile_glob: "prometheus/targets/*/*.generated.yml"}
enact_kind: prometheus_reload     # enact steps carry kind: 'semaphore' (install-node-exporter, reload-observability) / 'operator' (deploy-stack, fetch-dashboards) — item F
secret_domain_field: descriptor_field   # the method descriptor's secret_domain (snmp.yml, pve.yml)
trust_note: "Prometheus targets are non-secret; the exporter's creds stay SOPS, audited by NAME only."
```

### 2.2 Worked descriptor — backup (instance #2) — ✅ BUILT (Capability-track Phase 8)

> **As built:** the real descriptor is [`capabilities/backup.yml`](../../capabilities/backup.yml). It **deviates from the sketch below** in one deliberate way: instead of `render_method_stage: false` + a `policy_fields` form, backup sets **`render_method_stage: true`** and models its single role-entrypoint capture as the one "method" with the **schedule as a closed preset-cron allow-list param** — so it reuses the generic shell's method+param picker with **zero shell edit** (the stronger seam proof; a free-cron `policy_fields` renderer is a deferred shell enhancement). Registering backup touched zero spine file (verified by diff); the registry-parametrized INVARIANT D\* pins auto-cover it. `gen-backup.py` → `config/semaphore/schedules.generated.yml`; `configure-semaphore.py` iterates it (the hand-listed `nightly-backup` hub is removed). All enabled backup-capable classes now carry a `backup:` block.

Backup's ENACT machinery already existed (per-role `tasks/backup.yml` + `backup-configs.yml` + the Semaphore template). New = the `backup:` block, `gen-backup`, this descriptor, the suggester pair. **MVP ships SCHEDULE only** (retention/destination deferred — §4).

```yaml
name: backup
label: "config backup (read-only capture)"
order: 20
vector: backup                    # vectors/backup.yml (order 2) — EXISTS; high-confidence on _config option:backup / cliconf
suggester: {module: classify, suggest: suggest_backup, declared: declared_backup, hint: {}}
block_key: backup
block_shape: mapping              # a class backs up ONE way (its role entrypoint) -> a single mapping, SET
method_source: role_entrypoint    # the ONE option = this class's tasks/backup.yml; backup_capable gates it
method_registry: null
render_method_stage: false        # SKIP the picker -> inline "captures via <role> backup" confirmation
resource_stage:
  testid: cap-backup-policy
  source_kind: policy_fields
  fields:
    - {name: schedule, type: cron, default: "0 2 * * *", help: "cron; today one global nightly"}
generator: {script: scripts/gen-backup.py, lockfile_glob: "config/semaphore/*.generated.yml"}
enact_kind: semaphore_schedule    # gen-backup -> spec -> configure-semaphore.py registers; Semaphore runs backup-configs.yml
secret_domain_field: null         # local-history needs no domain; destination:offsite (deferred) would set block_field
trust_note: "Captures are themselves secret-bearing (running-config hashes/PSKs; 0640+no_log, local 0700, git NEVER pushed — SECURITY.md C8). Offsite destination is DEFERRED (no encryption-at-rest story)."
```

**Honest asymmetry, by design:** `block_shape` is a descriptor field precisely because telemetry is plural (`metrics:` is a list) and backup is singular (one capture path). The seam is shape-agnostic at the block boundary; `declared_<cap>` reads whatever shape `block_key` points at. Forcing backup into a list for false symmetry would be dishonest.

## 3. The generalized dialog / route / service

### 3.1 Byte-identical (lift verbatim — the shared spine)

`plan_token`/`verify_token` (`promote.py`, PURE hashing over arbitrary `*parts`); propose-then-promote semantics; the PROPOSE-vs-ENACT boundary; the 6-stage wizard scaffold + state table; the post-onboard nudge (success-gated on real `ok=rc==0`, render-only, localStorage-dismiss); the keyed-by-onboarded-class 404. The *parts*/content differ per capability; the spine does not.

### 3.2 Parameterized (the descriptor supplies content; ZERO `if cap==...` in the shell)

| Stage | Generic behavior | Descriptor field |
|---|---|---|
| 0 detect | `GET /api/capability/<cap>/suggest` → `eval_vector(descriptor.vector)` cell + candidates | `vector`, `suggester.suggest` |
| 1 method | IF `render_method_stage`: picker fills from `method_source`; ELSE skipped + inline confirmation | `method_source`, `method_registry`, `render_method_stage` |
| 2 params | one input per declared param (closed allow-list from the method descriptor) | the method descriptor / none |
| 3 resource | **render-table keyed by `resource_stage.source_kind`**: `curated_list`→chips, `policy_fields`→typed form (only enactable fields), `none`→skip | `resource_stage` |
| 4 propose | POST `apply:false` → diff of the `block_key` block + the generator's `lockfile_glob` artifact | `block_key`, `generator` |
| 5 promote | the dialog's own control + `plan_token`; 409 on drift | shared `promote.py` |
| 6 enact | hand off the commands from the `enact_kind` builder; "not live until you run these" | `enact_kind` |
| + links | OPTIONAL generic `links: [{label, url}]` on the plan, rendered as a `cap-links` anchor block (propose preview + post-promote). Telemetry emits Grafana deep-links — `Open Grafana` + `/d/<uid>` per curated dashboard (uid from the committed JSON; base from `KONTROLL_MGMT_IP`/`KONTROLL_DOMAIN`), #122. The route passes `links` through with **zero** cap branch, so any capability may emit them | instance's `build_plan` |

Context chip: `cap · kind · key` (e.g. `telemetry · device · cisco_ios` vs `backup · device · cisco_ios`) — a label, not a branch. testids namespaced `cap-<cap>-<stage>`, recorded once in `tests/testid_reference.md`.

### 3.3 Suggester dispatch — import-by-convention (not a god-map)

The capability service resolves `descriptor.suggester.{module, suggest, declared}` via `importlib` over the `capabilities/` dir, so the name→function binding is **derived from the drop-in descriptors** (the `requirements.generated.yml` "never hand-edit the list" rule applied to dispatch). The per-cap picker hint moves into `descriptor.suggester.hint` (telemetry's = today's `classify._TELEMETRY_HINT`), so **adding a capability never edits `classify.py`**. A pin asserts a dropped-in `capabilities/<cap>.yml` alone appears in `registered_capabilities()` — the registry's own "add an X" test.

### 3.4 The picker-filter `applies_when` — a Phase-7 prerequisite (it's vapor today)

`applies_when` is verified **vapor** (only in docs; zero `telemetry/*.yml`). Phase 7 **builds per-descriptor `applies_when` predicate blocks on the telemetry method descriptors** (reusing `predicate.eval_pred` — a true drop-in) BEFORE the generic dialog depends on the filtered picker. Backup needs no filter (`render_method_stage: false`).

### 3.5 Route + privileged tripwire

`/api/capability/<cap>` (+ `/suggest`), privileged by path prefix; 404 `no_capability` on an unknown `<cap>`. `_PRIVILEGED` is extended by a loop over registered descriptors' route segments (a drop-in). The generalized tripwire must be **at least as strict** as the literal-set one: assert every route under `/api/capability/` is in `_PRIVILEGED`, AND `_PRIVILEGED` contains no capability route missing from `registered_capabilities()`; plus a test that a dropped-in capability whose route escapes privileged classification turns the tripwire RED.

## 4. The backup instance in full (Phase 8)

- **The `backup:` block** (`block_shape: mapping`): `{capable, schedule, retention?, destination?}`, sibling of `metrics:`. `capable` mirrors `roles/<role>/defaults/main.yml backup_capable`; a `tests/validate` **equality** check pins `backup.capable == backup_capable` (no two-source drift).
- **`gen-backup.py`** (new; sibling of `gen-observability.py`): JOINs `enabled_modules × the backup: block × {validate the role has tasks/backup.yml}` → `config/semaphore/schedules.generated.yml`. Fail-closed (`capable:true` with no schedule → exit 2; role missing `tasks/backup.yml` → exit 2); `--check` lockfile. **`configure-semaphore.py` stops hand-creating `tpl_backup`/`nightly-backup` and iterates the generated spec** — so a per-class backup schedule becomes a `backup:` drop-in (closing the hub-edit at `configure-semaphore.py:196-210`).
- **Enact** (`enact_kind: semaphore_schedule`): the builder emits strings (the API runs none) — `gen-backup.py`, `configure-semaphore.py` (registers the schedule), + a verify line. PROPOSE (write block + regen spec + commit, audited) vs ENACT (configure-semaphore registers; Semaphore runs the play) — identical to telemetry's boundary.
- **Trust boundary:** captures are secret-bearing (running-config hashes/PSKs; `no_log`, 0640, local 0700, git never pushed — SECURITY.md C8). The dialog never surfaces a capture body; the audit logs the destination + secret-domain NAME only.
- **`destination: offsite` is scoped OUT of MVP** and not rendered as a selectable enum — it has no encryption-at-rest story (captures aren't SOPS-encrypted; C1's `kontroll_backup_deploy` backs up the encrypted *repo*, not device captures). MVP ships `destination: local-history` only; `gen-backup` fail-closes on offsite-without-a-domain; SECURITY.md gets a deferred-risk row.
- **Retention** (`retention:N`) implies a NEW actuation (a history prune); the local git history is currently unbounded. **Schedule-only MVP — no inert UI controls.** Retention joins `resource_stage.fields` only when the prune step lands (inheriting `backup-configs.yml`'s access-chain + idempotence test).

## 5. INVARIANT D\* (generalized) + its pins

**Statement (capability-neutral):** No secondary capability C — telemetry, backup, or any future drop-in — may gate, block, delay, or complicate making a device/service Ansible-usable. For every registered C: (1) no C input required to onboard (the required set stays `{collection,key,group,host}` + creds); (2) the onboard data-write touches zero C blocks; (3) the onboard result is independent of every C's state; (4) a device is fully Ansible-usable after onboard alone; (5) every C route is strictly-after + optional (404s `no_module` on an un-onboarded class); (6) every C nudge is render-only + non-binding. Already enforced by the data layer (`apply_onboard_plan` writes only module/host/fleet/SOPS).

**Pins:**
- **PRIMARY (positive allow-list, cannot vanish):** `test_onboard_apply_writes_only_known_keys` — the written `module.yml` top-level keys are a SUBSET of the onboard-written set, which structurally excludes `metrics:`/`backup:`/any future block **without iterating the registry** (survives an empty/failed registry). Replaces the telemetry-named `test_onboard_apply_writes_no_observability_keys`.
- **SECONDARY (registry-parametrized):** `test_onboard_apply_writes_no_capability_keys[C]` over `registered_capabilities()`, **guarded by `assert len(registered_capabilities()) >= EXPECTED`** (telemetry at Phase 7; +backup at Phase 8) so an empty parametrization is RED, not a vacuous green.
- `test_capability_suggester_is_pure_readonly[C]`; `test_onboard_succeeds_when_capability_suggest_raises[C]`; `test_registry_is_drop_in` (a dropped-in `capabilities/<cap>.yml` alone appears in `registered_capabilities()`).

Parametrization is the modular form — adding a capability grows the registry list the tests read, never edits a test body.

## 6. Phase axis

The secondary-capability phasing is labeled **"Capability-track Phase N"** everywhere (PLAN.md §10 owns an independent **infra** Phase 0–6, so a bare "Phase 7/8" would collide). Capability-track Phases 1–4 (telemetry registry + generators + the vector) **shipped**.

- **Capability-track Phase 7 (recast):** build the generalized seam — `promote.py`; the capability registry (`load_capabilities()` + `registered_capabilities()` + `capabilities/<cap>.yml`); the generic `openCapabilityDialog(cap, key, {root, collection})`; `api/routes/capability.py` (privileged, path-segment dispatch); per-descriptor `applies_when` on the telemetry method descriptors; the generic INVARIANT D\* pins — **and register telemetry as instance #1** (`observe.py` + the `metrics:`/`dashboards:` write + the method/dashboard picker + the telemetry enact builder + `capabilities/telemetry.yml`). Gated (unchanged) on the C9 privileged-mutation enablement.
- **Capability-track Phase 8 (new):** register **backup as instance #2** — the `backup:` block + `gen-backup.py` + `suggest_backup`/`declared_backup` + `capabilities/backup.yml`, MVP schedule-only. **Acceptance: `openCapabilityDialog('backup', key, …)` works with ZERO edit to `promote.py` / the shell / the generic route / the generic pins**; `registered_capabilities()` becomes `['telemetry','backup']` and the parametrized pins auto-cover backup with no new test code. If backup needs any spine edit to register, the seam failed the "add an X" test and Phase 7's design is wrong.

## 7. Doc home & what lands now

- **This doc** is the seam's design-of-record. The capability-neutral contract (the descriptor schema, the registry, `promote.py`, INVARIANT D\*, the generic pins) is also pinned in master [`observability-onboarding-flow.md` §4.0 + §6](../observability-onboarding-flow.md) — **the master is the seam owner**; instance docs own only instance content.
- [`gui-actuatable-flow.md`](gui-actuatable-flow.md) is **instance #1 (telemetry)**; physical relocation/rename happens just-in-time when Phase 7 builds.
- **Lands now (this pass):** this doc; PLAN.md §2.5 (the doctrine row) + §10 pointer + §5 tree; the master §6 phase recast; the `gui-actuatable-flow.md` instance-#1 header; the stale `delivery-runbook.md` §2 Phase 7 reconciliation; CHANGELOG; the SECURITY.md captures deferred-risk row.
- **Lands at Phase 7/8:** all code/tests; the `modules/README.md` `backup:` row (Phase 8); `validate.sh` `gen-backup --check` (Phase 8).

## See also
- [observability-onboarding-flow.md](../observability-onboarding-flow.md) — the master design + §4.0 SHARED CONTRACTS (seam owner)
- [gui-actuatable-flow.md](gui-actuatable-flow.md) — instance #1 (telemetry), incl. §0.1 INVARIANT D\*
- [telemetry-capability-vector.md](telemetry-capability-vector.md) — the telemetry vector (the detection half, built Phase 4)
- [../../PLAN.md](../../PLAN.md) — §2.5 "add an X" test (the secondary-capability drop-in row)
