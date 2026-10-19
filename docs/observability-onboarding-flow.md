---
title: Observability Onboarding Flow — Master Design
status: DESIGN HISTORY — largely built; see the banner
baseline: HEAD 63de129 (CI green)
scope: kontroll composed control plane — the telemetry-method seam + GUI-actuatable per-device/service observability
audience: a Claude Opus 4.8 (1M context) implementer in a normal (non-orchestrated) session
authors: synthesized from 7 read-only design dimensions + 3 self-reviews + 1 QA pass
---

# Observability Onboarding Flow — Master Design

> **DESIGN HISTORY — not a description of the tree.** This is the design record of the observability and capability track, written in June 2026. Most of it was built (the telemetry-method registry, the capability seam, the generators); [telemetry/README.md](../telemetry/README.md), [capabilities/README.md](../capabilities/README.md) and [secondary-capability-dialog.md](observability/secondary-capability-dialog.md) describe what exists. Where it disagrees with the code or with `SECURITY.md`, they are right.

## 1. Purpose

This document is the **spine** that wraps seven design sections into one coherent, implementable plan for making per-device and per-service observability a **modular, self-describing, GUI-actuatable** capability of kontroll. Today, telemetry is a closed two-value enum (`via: host | proxy`) hard-wired into an `if/else` in `scripts/gen-observability.py`, the proxy-exporter relabel is hand-written in `prometheus.yml`, agent-less devices (the Cisco core switch, the FortiGate edge firewall) are unmonitored, and onboarding a device does **not** generate its metrics/dashboards/exporter wiring — those were added by hand. This plan promotes `via` into a **drop-in telemetry-method registry** (`telemetry/<method>.yml`), turns `gen-observability.py` into a dispatcher over it, generates the proxy-exporter Prometheus jobs and exporter containers from data, adds a telemetry **capability vector** for detection, converges the **two onboarding roots** (Galaxy-rooted devices + OpenAPI-rooted services) on a shared observability opt-in, adds a **hybrid dashboard discovery** UX, and exposes the whole thing as a **propose-then-promote**, fail-closed, audited GUI/API flow — all while honoring the "deeply modular, no god files; one dispatch seam; config-as-data; generated-never-hand-maintained; propose-then-promote; tests/logging/docs are first-class" doctrine of `CLAUDE.md`.

## 2. Status & scope

> **THIS WAS A DESIGN DOCUMENT WHEN IT WAS WRITTEN** (most of it has since been built — see the banner at the top). At the time no source file had been edited, no Ansible has run, no lab device has been touched. The baseline is **HEAD `63de129`** (CI green). Every file path, schema, code sketch, test name, log line, security control, and doc-sync row below is a *specification for a future implementer*, not a record of work done.

**In scope** (designed in full, with phased delivery): the telemetry-method registry + dispatcher; proxy-exporter Prometheus-job generation; exporter-container generation; agent-less SNMP/blackbox monitoring of the switch + firewall; the telemetry capability vector (honestly scoped); the OpenAPI service-onboarding root + the radarr/exportarr worked example; hybrid dashboard discovery; the GUI/API observability opt-in with a minimal propose-then-promote primitive.

**Explicitly deferred** (named, sequenced, but not the MVP): push/OTel/Loki telemetry kinds; multi-datasource (Loki) dashboard pinning; the live grafana.com search tier's production hardening; the full GUI→API convergence; registry-driven host-set derivation for the agent install path. Each deferral is recorded in the phase roadmap (§5) with its gate.

**Boundary that constrains everything** (the load-bearing decision): the network surfaces (API/GUI) **propose data only** — they write a module's `metrics:`/`dashboards:` block, regenerate the target lockfiles in the working tree, and commit (audited, `run_id`-correlated). They **never** install an agent, provision an exporter container, render a secret into `.env`, run Ansible, or reload Prometheus/Grafana. Those **enact** steps stay operator-CLI / Semaphore / `deploy-stack` — the established delegated path. This keeps the MVP inside the *current* trust boundary and shippable **without** the separately-tracked privileged-mutation enablement (`SECURITY.md` C9).

## 3. The seven section docs

This spine links out to seven companion section docs (each a self-contained, grounded design):

| # | Section doc | Owns |
|---|---|---|
| 1 | [`telemetry-method-registry.md`](./observability/telemetry-method-registry.md) | The keystone seam: `telemetry/<method>.yml`, the `gen-observability.py` dispatcher, the byte-identical host-agent migration |
| 2 | [`agent-less-monitoring.md`](./observability/agent-less-monitoring.md) | Proxy-exporter methods (pve/snmp/blackbox), generated Prometheus jobs + exporter containers, gap-d closure for the switch/firewall |
| 3 | [`telemetry-capability-vector.md`](./observability/telemetry-capability-vector.md) | `vectors/telemetry.yml` detection (honestly scoped as a proxy/API suggester) + the declared↔detected reconciliation |
| 4 | [`two-root-onboarding.md`](./observability/two-root-onboarding.md) | The service (OpenAPI) onboarding root, `kind: device\|service`, the radarr/exportarr worked example, the shared observability opt-in |
| 5 | [`hybrid-dashboard-discovery.md`](./observability/hybrid-dashboard-discovery.md) | Curated-by-method + live grafana.com search, `GET /dashboards/search`, the chosen-board flow-back |
| 6 | [`gui-actuatable-flow.md`](./observability/gui-actuatable-flow.md) | The `/observe` route, propose-then-promote, the GUI opt-in, audit/rate-limit/fail-closed |
| 7 | [`delivery-runbook.md`](./observability/delivery-runbook.md) | Per-behaviour test/log/security/doc-sync specs + the phased commit roadmap |
| 8 | [`secondary-capability-dialog.md`](./observability/secondary-capability-dialog.md) | **The generalized secondary-capability dialog SEAM** (telemetry = instance #1, backup = instance #2): the `capabilities/<cap>.yml` descriptor, `openCapabilityDialog`/`/api/capability`, INVARIANT D\*, Capability-track Phase 7/8 |

> **Binding precedence:** where a section doc and this spine disagree on a contract (a symbol name, a schema field, a sequencing decision), **§4.0 SHARED CONTRACTS below wins** and the section doc must be reconciled before implementation. The §8 "Resolved conflicts" subsection records exactly which sections must change.

---

## 4. How the seven considerations are honored

The operator framed seven considerations. This table maps each to the section(s) that address it and states the honoring decision.

| # | Consideration | Section(s) | How it is honored |
|---|---|---|---|
| 1 | **Two roots** — device = Galaxy-rooted; a service (radarr) = OpenAPI/service-catalog-rooted; radarr metrics need exportarr (a proxy exporter); radarr today is just a Homepage widget | §4 (two-root onboarding), §2 (exportarr method), §6 (shared opt-in) | A new `build_service_plan` (collection-less, recipe-rooted) is a **sibling** of `build_onboard_plan`; both return the same plan shape and converge at `apply_onboard_plan` (the shared mutator). `attach_observability` is invoked by the **observe path** (the standalone observability dialog / `galaxy.py observe`), **never inside `build_onboard_plan`** — `apply_onboard_plan` writes no observability (the onboard↔observe decoupling, pinned as [gui-actuatable-flow.md §0.1 INVARIANT D](observability/gui-actuatable-flow.md)). A `kind: service` module field + a `services` inventory group + the existing `api` backend unify them. exportarr is a `proxy-exporter` telemetry method; radarr's hand-authored Homepage tile becomes generated from its `widget:` block. |
| 2 | **Modular/typed telemetry ingestion** — node_exporter is ONE protocol among many; the data structure must hold host-agent, proxy-exporter, SNMP, API-pull, OTel, Loki modularly and typed | §1 (registry + `kind`), §2 (proxy generation) | `telemetry/<method>.yml` descriptors carry a typed `kind` (`host-agent \| proxy-exporter`, with `push`/`log-shipping` **reserved** and fail-closed-rejected until implemented). `gen-observability.py` dispatches over the registry; **node_exporter becomes one descriptor row (`host_node`), not the generator's default branch.** |
| 3 | **Agent-less monitoring first-class** — how to monitor SSH/SNMP/REST-only appliances | §2 (snmp/blackbox), §7 (gap-d phase) | `proxy-exporter` methods (snmp_exporter, blackbox_exporter) monitor the Cisco switch + FortiGate firewall **read-only** (SNMP GET / ICMP — incapable of actuation). The exporter container, its Prometheus job, and its relabel are **generated from the descriptor**, so adding one is a drop-in, not a hub edit. |
| 4 | **Telemetry capability vector** — detect "does this expose metrics / can it be scraped", parallel to actuate/backup/bespoke | §3 (vector) | `vectors/telemetry.yml` (order 4) is loaded + folded by the existing engine with **zero code change**. Honestly scoped: it is a **proxy/API-exporter suggester** from collection signals (`httpapi`/`cliconf`), **not** a host-agent detector — the probe carries no host-OS fact, so `host_node` stays **operator-declared**. |
| 5 | **Dashboard UX = hybrid** — suggested-default + live search, one-click-if-trusted, degrade-to-manual | §5 (hybrid discovery) | A four-state picker: `suggested` (curated by method, from the descriptor's `suggested_dashboards`), `manual_search` (live grafana.com), `trusted` (one-click, descriptor-blessed only), `degrade_manual` (no curation / empty search → manual or skip; "no board" is a valid honest outcome). |
| 6 | **Dashboard discovery** — today is manual-selection + auto-fetch; design the grafana.com query/suggest tier | §5 (`GET /dashboards/search`) | A new **inquiry** route + `search_dashboards()` over the (undocumented, vendored) grafana.com registry, short-timeout + degrade-to-empty, in-process TTL cache, no SSRF surface. The chosen `{gnet,name}` flows into `module.yml` via the **existing** onboard apply; the actual fetch stays operator-CLI/deploy-stack. |
| 7 | **The GUI gap is the WHY** — infra stand-up stays operator-CLI; per-device/service observability becomes GUI-actuatable; the onboard `--observability` opt-in is the first brick | §6 (GUI/API flow), §7 (delivery) | A `/observe` privileged route + GUI opt-in **propose** the metrics block + regenerate targets + commit (audited); **enact** (agent install / exporter provision / dashboard fetch / reload) is returned as deterministic next-step commands run via Semaphore/`deploy-stack`. Standing up the metrics *infra* stays operator-CLI; *per-device* observability becomes the GUI brick. |

---

## 4.0 SHARED CONTRACTS (binding — all sections reconcile to this)

> These resolve the QA review's cross-section drift blockers/majors. Any section doc that names a symbol or schema differently is **wrong and must be fixed** (see §8).

### 4.0.1 The one loader name and the one Catalog field

- **Loader:** `catalog.load_telemetry()` — returns a list of descriptor dicts sorted by `order`, mirroring `load_vectors()`/`load_backends()` (`scripts/kontroll/catalog.py`). **`load_telemetry_methods()` is forbidden** as a second alias.
- **Catalog field:** `Catalog.telemetry` (in `api/deps.py`), loaded once at startup by `load_catalog()`. **`Catalog.telemetry_methods` is forbidden.**
- **Path constant:** `paths.TELEMETRY_DIR = os.path.join(ROOT, "telemetry")`.
- **The generator does NOT carry a duplicate loader.** *(QA blocker, implementability lens, verified):* `python3 scripts/gen-observability.py` puts `scripts/` on `sys.path[0]`, so `from kontroll import catalog` resolves in all three run contexts (standalone CLI, `deploy-stack`, pytest via `conftest.py`). `gen-observability.py` calls `catalog.load_telemetry()` directly. **There is no `_load_methods()` twin and no `test_loaders_agree`** — both were predicated on a false "cannot import kontroll" constraint and are struck.

### 4.0.2 The one canonical `telemetry/<method>.yml` schema (FLAT)

A single flat schema (matching `backend.yml`'s flat style), covering both kinds. The keystone section's nested `target: {address, port, labels}` encoding is **superseded** by this flat form.

```yaml
# telemetry/<name>.yml — the canonical, flat descriptor.
name: <str>              # REQUIRED. registry key; == filename stem; == module.yml metrics[].method
label: <str>             # REQUIRED. human one-liner (docs / picker)
order: <int>             # REQUIRED. load/sort order (host-agent 1-9, proxy 20+)
kind: <enum>             # REQUIRED. host-agent | proxy-exporter
                         #   (push | log-shipping are RESERVED; the dispatcher rejects them, never silent)
job: <str>              # REQUIRED. prometheus job_name AND the targets subdir
                        #   prometheus/targets/<job>/<key>.generated.yml — declared ONCE per method
labels: [<str>...]      # REQUIRED. ordered label keys per target, from {group, host} (+ params label_as)
                        #   host-agent: [group, host]  ;  proxy-exporter: [host]

# --- target rendering (replaces the via if/else) ---
target_shape: <enum>    # host_port | device
                        #   host_port -> "<ansible_host>:<port>"   (host-agent)
                        #   device    -> "<ansible_host>"          (proxy-exporter; relabel proxies it)
port: <int|null>        # REQUIRED iff target_shape==host_port; null/absent for device

# --- host-agent kind (closes gap c: agent identity is a field) ---
agent_role: <str|null>  # ansible role that installs the agent (node_exporter today). TOP-LEVEL.
                        #   DOCUMENTARY in the scrape generator (read by no generator); the install
                        #   play / enact step reads it. null for proxy-exporter.

# --- proxy-exporter kind (closes gap b: the relabel becomes data) ---
exporter_address: <str|null>   # host:port the relabel rewrites __address__ to (e.g. pve-exporter:9221)
metrics_path: <str|null>       # the exporter's scrape path (e.g. /pve, /snmp, /probe)
target_param: <str|null>       # the ?<name>=<device> query param (multi-target exporters); null if single
relabel: [<dict>...]           # OPTIONAL explicit relabel_configs (rendered into jobs.d/<job>.generated.yml)
exporter:                      # the container fragment gen-exporters.py emits; null for host-agent
  image: <str>
  container_name: <str>        # == name by convention
  listen_port: <int>
  config_mount: <str|null>     # repo path mounted :ro, or null
  config_container_path: <str|null>
  env: {}                      # ${VAR:-} blank-tolerant env keys
  access_chain: {chain, may_break, fallback, blast_radius}   # the 4-line header, stamped into the fragment

# --- secrets (proxy-exporter only; host-agent has none) ---
secret_domain: <str|null>      # instance/secrets/<domain>.sops.yml; base_recipients via the .sops.yaml catch-all
secret_env_map: {}             # {.env-key: sops-key} the deploy-stack .env render consumes (see §6.7)

# --- TLS posture (per-method, per-VLAN deliberate choice) ---
verify_tls: <bool>             # DEFAULT TRUE. pve sets false (mgmt self-signed, C3-documented). A
                               #   cross-VLAN target MUST justify any false in SECURITY.md.

# --- per-class params: a CLOSED allow-list (config-injection guard) ---
params:                        # the schema a module.yml entry's `params:` is validated against
  <param_name>:
    allowed: [<v>...]          # REQUIRED enum; an unknown value is a HARD FAIL, never interpolated
    required: <bool>           # default false
    label_as: <str|null>       # emit the value as an extra label / ?<k>= relabel (e.g. snmp module)

# --- curated dashboards (the hybrid picker's default tier) ---
suggested_dashboards: [{gnet: <int>, name: <str>, title: <str>, trusted: <bool>}]   # OPTIONAL
```

`module.yml` metrics entries reference a method by name: `- {method: <name>, params?: {...}}`. **Legacy `{job, via, port}` is removed** — see 4.0.3.

### 4.0.3 The one dispatcher (clean cutover, fail-closed — NO legacy shim)

*(QA blocker, implementability + coherence lenses):* the document shipped two incompatible dispatcher sketches. **The canonical one is the keystone's clean-cutover, fail-closed variant** (it matches the acceptance criteria "no `module.yml` contains `via:`" and "a legacy `{via,port}` entry exits non-zero"). The Delivery Runbook's 3-arg `_entries` + `_resolve_method` + legacy-shim sketch is **superseded and struck.**

- Canonical signatures: `metrics_files(fleet, inv, methods=None)` and `_entries(method, key, group, hosts, met)`.
- A metrics entry with no `method` key → `sys.exit(2)` (loud). A `method` naming no descriptor → `sys.exit(2)`. A `push`/`log-shipping` kind → `sys.exit(2)`. **Never** a silent host-branch fallthrough.
- `proxmox` + `docker_host` `module.yml` migrate to `{method: ...}` **in the same (Phase-1) commit** — only two files declare `metrics:` today.
- **Collision guard scoping** *(QA major):* the `(job, key)` uniqueness check is **per-module** — `seen_paths` is reset at the top of each `for key in enabled_modules` iteration. Two *different* classes sharing a job is normal and required (`proxmox.generated.yml` and `docker_host.generated.yml` both land under `targets/node/`). `test_duplicate_job_for_one_class_fails_loud` drives **two entries on one class.**

### 4.0.4 YAML-escaping for generator output (one authoritative rule)

*(QA blocker, security lens):*
- **Byte-identity first:** in the Phase-1 migration commit, do **NOT** change `_render`'s emission for the existing `{group, host}` label set — their values (slugs/IPs/hostnames) are known-safe and emitted bare today; blanket `safe_dump` risks a non-empty `git diff`. Keep them bare.
- **Quote-only-if-needed for param-bearing values:** any value that *can* carry onboarding/param input (notably the `__param_<k>` labels the agent-less section adds) is routed through a `_yaml_scalar()` helper that quotes **only if** the value contains YAML-significant characters, leaving safe slugs bare (preserving byte-identity). *(The `_yaml_scalar` `endswith("\n...")` branch is dead — drop it; `.strip()` alone suffices.)*
- **Correct the agent-less §3.4 statement** that the `__param_module` label needs "no change to `_render`'s label loop" — it **must** go through `_yaml_scalar`.
- **One injection test, owned by the keystone:** `test_param_metacharacter_is_escaped_not_injected` drives a metacharacter value through the **label** path (not just the target path) on a **params-bearing class (snmp/Phase-2)**, never on proxmox/docker_host (so byte-identity and injection-hardening are separable commits).

### 4.0.5 The `network` Prometheus job (one reconciliation)

*(QA blocker, security lens):* there is **no inventory group named `network`** — `network` is a Prometheus **job/path bucket** only. The host fan-out for snmp/blackbox targets comes from each class's **existing `inventory_group`** (`cisco_ios → core_switch`, host `my-switch` `192.0.2.2`; `fortigate → edge_firewall`; `openwrt → wireless_ap` `192.0.2.3`). The output **path** is keyed on the method's `job` (`network`), which is why the file lands at `prometheus/targets/network/<key>.generated.yml`. **Delete every phrase implying a "network inventory group."**

Transition rule: the inline `network` job in `prometheus.yml:26-28` (which has zero targets today) is **deleted in the same commit** that introduces the generated `jobs.d/network.generated.yml` (Phase 3, on the Phase-2 `scrape_config_files` mechanism). Acceptance asserts **exactly one** `job_name: network` across `prometheus.yml` + `jobs.d/*` and `promtool check config` passes.

### 4.0.6 The exporter secret-render is GENERATED, not hand-edited (operator decision, 2026-06-14)

*(Operator chose the future-proofed/modular option over a documented per-exporter hub-edit — "more modularity.")* A proxy-exporter method's secret (an SNMP community, a service API key) reaches the running exporter through `.env`, which `deploy-stack` renders at deploy time from SOPS (`no_log`). That render must **not** be a per-exporter hand-edit of `deploy-stack.yml`. Instead:

- **BUILT + UNIFIED across telemetry AND logging** (`docs/reviews/2026-06-17-secret-injection/`). Each `proxy-exporter` method descriptor declares **`secret_domain`** (its `instance/secrets/<domain>.sops.yml`) and **`secret_env_map`** — now `{ENV_VAR: "<str.format template over the domain's SOPS field NAMES>"}` (a 1-field map is the degenerate `"{field}"` case of a logging method's N-field token, so the SAME block + manifest + filter serve both axes).
- `gen-secret-env.py` collects every telemetry `metrics:` + logging `logs:` method's `secret_env_map` into ONE value-free **`config/secret-env.manifest.generated.yml`**. `deploy-stack`'s `.env` render is **ONE generic loop** over it — decrypting the UNION of referenced domains **by name** (`kontroll_sops_domain`, `no_log`, fail-soft per domain) and composing each `ENV=<value>` via `kontroll_render_token` — so **adding an exporter OR log-pull secret is a descriptor drop-in, never a `deploy-stack.yml` edit, never a `_<domain>` lookup, never a hardcoded domain map**. The secret **value** is never committed and never logged; only names appear durably.
- Covering checks (same commit): `gen-secret-env --check` (manifest fresh); fail-closed on a bad env name / template / a **platform-core collision** (a device descriptor can't shadow `SEMAPHORE_*`/`KONTROLL_API_TOKEN`); the value-free manifest pin; the per-domain fail-soft (`test_kontroll_sops.py`). The non-secret exporter env stays in the generated compose fragment / `.env.example`; only the **secret** lines are loop-rendered.
- This **supersedes** the §8 "documented hub-edit" alternative for the deploy-stack `.env` block. (The compose `include:` list + `.env.example` remain composition seams; only the *secret* render is loop-generated. This loop ships in Phase 2 — the first phase with a generated exporter — even though pve reuses an existing domain.)

---

## 5. End-to-end flow narrative

Two walks, search → live dashboard, naming every seam.

### 5.1 DEVICE: the Cisco core switch via SNMP (agent-less)

The switch (`cisco_ios`, group `core_switch`, host `my-switch` at `192.0.2.2`) runs no agent. It is monitored read-only via a control-node `snmp_exporter`.

1. **Search / classify (read-only inquiry).** `galaxy.py search cisco` → `service_search` → `catalog.local_shallow`/`galaxy_search` → `probe.shallow_*` → `record.build_record`. The record now carries a `capabilities.telemetry` cell (§3): `cisco.ios` has a `cliconf` plugin → the telemetry vector reads `telemetry = yes (low)` ("a proxy/agent-less exporter may fit"). The cell is a **hint**, not a declaration.
2. **Onboard plan (PURE).** `build_onboard_plan("cisco.ios", "cisco_ios", "core_switch", "192.0.2.2", …)` deep-probes, `predicate.classify` picks the `netcommon_cli` backend, and the module dict is assembled (`onboard.py:46-54`). **After onboard — a separate, optional dialog/CLI step; observability never gates onboarding** — the operator opts into observability via the standalone dialog ([gui-actuatable-flow.md §5](observability/gui-actuatable-flow.md)): `attach_observability(plan, telemetry_method="snmp", method_params={"module": "if_mib"}, dashboard={gnet, name})` (§6) injects `metrics: [{method: snmp, params: {module: if_mib}}]` and a `dashboards:` block into the plan's module dict — **writing nothing**, and recording the **enact** commands.
3. **Promote (data-write, audited).** `apply_onboard_plan` writes `modules/cisco_ios/module.yml` (with the metrics block), the drop-in inventory host, `enable_in_fleet`, and (for snmp) nothing into SOPS here — the community lives in its own domain. It then **regenerates targets in-process**: `metrics_files`/`_entries` dispatch over `telemetry/snmp.yml` (`kind: proxy-exporter`, `target_shape: device`, `job: network`), fanning hosts from `inventory_group: core_switch`, emitting `prometheus/targets/network/cisco_ios.generated.yml` = `[{targets: ["192.0.2.2"], labels: {host: my-switch, __param_module: if_mib}}]`. `gitio.commit_and_push` commits **only the specific regenerated paths** (§6.6), `run_id`-correlated.
4. **The generated Prometheus job (data, not hand-written).** `gen-prometheus-jobs.py` (§2 / Phase 2) reads `telemetry/snmp.yml` and emits `prometheus/jobs.d/network.generated.yml`: `job_name: network`, `metrics_path: /snmp`, `file_sd` over `targets/network/*.yml`, and the relabel chain `__address__ → __param_target → instance`, `__param_module` passthrough, `__address__ → snmp-exporter:9116`. *(The `_job_doc` sketch must emit the `__param_module` passthrough relabel — see §8.)* `prometheus.yml` is composition-only via `scrape_config_files`.
5. **Enact (NOT the network surface — item F splits it by privilege).** The plan's `enact` list now carries a `kind` per step: **Tier-1 'semaphore'** steps are one-click Semaphore tasks needing no host root (the host-agent install play; the Prometheus config reload — the `reload-observability` task POSTs `/-/reload` over the shared network, no `docker exec`); **Tier-2 'operator'** steps stay control-node commands because they need host Docker (provision `snmp-exporter` + render the SNMP community from the `snmp_observability` SOPS domain via `deploy-stack`; `fetch-dashboards.py` then `deploy-stack` for the curated board). The GUI shows ▶ for the Semaphore tasks, `$` for the operator commands.
6. **Live dashboard.** Grafana's file provider auto-loads the fetched JSON; the switch's interface counters render. The whole click→commit→enact chain is greppable by one `kontroll_run_id`.

**Seams touched:** `service_search` → `record.build_record` (+ telemetry cell) → `build_onboard_plan` → `attach_observability` → `apply_onboard_plan` → `gen-observability.py` (dispatcher) → `gen-prometheus-jobs.py` → `gitio.commit_and_push` → (enact) `deploy-stack` + `fetch-dashboards.py`.

### 5.2 SERVICE: radarr via exportarr (the second root)

Radarr (`192.0.2.20:7878`) has no Ansible collection — it is a REST service.

1. **Discover (EXISTS).** `galaxy.py openapi http://192.0.2.20:7878/api/v3/openapi.json --name radarr --port 7878 --emit` → `openapi.build_openapi_recipe` → `ansible/backends/api/recipes/radarr.yml` (`derive_auth` finds `X-Api-Key`; `--port` overrides the 443 default).
2. **Service plan (NEW, collection-less).** `build_service_plan("radarr", "radarr", "http://192.0.2.20:7878", api_token=…)` (§4) — **skips `deep_probe`** (a service has no collection), returns the **same plan shape** as the device root: a `kind: service` module (`role: backend_api`, `backend: api`, `recipe: radarr`, `inventory_group: services`, `secrets_domain: services`), a localhost-delegated inventory host, and `creds_to_set: {radarr_api_key: …}`.
3. **Observability opt-in — the standalone dialog (a separate, optional step AFTER onboard; never gates onboarding).** `attach_observability(plan, telemetry_method="exportarr", method_params={app: radarr, url_var: radarr_service_url, api_key_var: radarr_api_key}, dashboard={gnet: 12896, name: radarr}, widget={type: radarr, url: …, secret: HOMEPAGE_VAR_RADARR_KEY})` injects `metrics:`/`dashboards:`/`widget:` blocks — the **same** opt-in the device root uses (the method registry is root-agnostic).
4. **Promote (data-write, audited).** `apply_onboard_plan` writes the module + the localhost host, `enable_in_fleet`, encrypts `radarr_api_key` into the `services` SOPS domain, regenerates the exportarr `proxy-exporter` target, and appends the Homepage tile via `gen-homepage.py`. Commits the specific paths, `run_id`-correlated.
5. **Enact (operator-CLI / deploy-stack).** Provision the `exportarr` container, render `RADARR_API_KEY`/`HOMEPAGE_VAR_RADARR_KEY` from SOPS into `.env` (`no_log`), `deploy-stack`, `fetch-dashboards.py` (gnet 12896), reload.
6. **Live.** exportarr queries radarr's API, exposes `/metrics`; Prometheus scrapes via the generated `service` job + relabel; the radarr Grafana board and the Homepage tile go live.

**Seams touched:** `openapi.build_openapi_recipe` → `build_service_plan` → `attach_observability` → `apply_onboard_plan` → `gen-observability.py` (exportarr method) → `gen-prometheus-jobs.py` + `gen-exporters.py` + `gen-homepage.py` → `gitio.commit_and_push` → (enact) `deploy-stack` + `fetch-dashboards.py`.

---

## 6. Phased implementation roadmap

Dependency-strict; each phase = **one reviewable commit** carrying its full first-class-deliverable set. Phases 1–4 cross **no** trust boundary. Phases 5–7 are gated/deferrable.

> **Section-doc reconciliation policy (option C, operator-approved 2026-06-14).** The seven companion section docs predate §4.0 and carry pre-reconciliation drift (loader name, schema shape, dispatcher, etc.). Rather than a big-bang rewrite, each section is reconciled to §4.0 **at the top of its phase**, then kept in sync with the *shipped code* in that phase's commit (doc-sync doctrine — the best moment to make a section clean is against the code that just landed). The **Phase-1 keystone section** (`telemetry-method-registry.md`) carries a precise supersede note **now** (it is the imminent build); the others (Phases 2–7) are reconciled just-in-time when their phase becomes active. This avoids spending effort reconciling gated sections (5–7) that may still change.

### Phase 1 — Telemetry-method registry + host-agent dispatch (the keystone foundation)
**The smallest correct foundation.** Depends on: nothing.
**Deliverables:**
- NEW `telemetry/host_node.yml`, `telemetry/pve.yml` (per §4.0.2 flat schema; reproduce today's output byte-for-byte), `telemetry/README.md`.
- MOD `scripts/kontroll/paths.py` (`TELEMETRY_DIR`), `scripts/kontroll/catalog.py` (`load_telemetry()` — §4.0.1, the **only** loader, no twin).
- MOD `scripts/gen-observability.py` (`metrics_files`/`_entries` dispatcher per §4.0.3; `from kontroll import catalog`; fail-closed unknown-method; per-module collision guard; `_yaml_scalar` per §4.0.4 but bare labels unchanged).
- MOD `modules/proxmox/module.yml`, `modules/docker_host/module.yml` (migrate `{job,via,port}` → `{method: …}`).
- NEW `tests/unit/test_telemetry_methods.py` (4 tests, docstrings per the delivery runbook); existing `test_gen_observability.py` four tests stay green unchanged.
- Doc-sync: `modules/README.md` (metrics schema row), `prometheus/README.md` (job owned by method), `tests/README.md`, `CHANGELOG.md`, reciprocal See-also. **No `SECURITY.md` change** (no new secret/domain/surface). *(Move the `docs/capability-matrix.md` edit to Phase 4 — there is no telemetry vector yet to disambiguate.)*
**Land-order (explicit, to avoid a red `--check`):** (1) write descriptors → (2) migrate the two `module.yml` → (3) run `python3 scripts/gen-observability.py` (non-check) to rewrite targets → (4) confirm `git diff prometheus/targets/` **empty** → (5) commit.
**Acceptance:** `git diff prometheus/targets/` empty; `bash tests/validate.sh` green (existing `--check` at `validate.sh:72` covers it); `gen-observability.py` has **no `if met.get("via")`** branch; node_exporter is one **scrape-generator** descriptor row; unknown/legacy/bad-param/duplicate-job entries exit non-zero.
**Commit theme:** `feat(observability): promote the via host|proxy enum to a telemetry-method registry — adding a method is a drop-in, not a gen-observability if/else hub edit`

### Phase 2 — Proxy-exporter Prometheus-job generation (closes gap-b)
Depends on: Phase 1.
> **Split in delivery — Phase 2a + 2b (✅ SHIPPED):** **2a** = Prometheus-job generation (`gen-prometheus-jobs.py`
> → `prometheus/jobs.d/`, the pve relabel moved into `telemetry/pve.yml`, the `scrape_config_files` include).
> **2b** = exporter-container generation (`gen-exporters.py` → `docker/services/<container>.generated.yaml` from
> the descriptor's `exporter:` block; the hand-authored `pve-exporter.yaml` removed; a no-host-port covering
> check). **Moved to Phase 3:** the descriptor-driven `.env` secret-render loop (§4.0.6) — it lands where the
> SNMP community is the first *new* secret to exercise it (verifiable end-to-end), alongside the
> `test_method_param_metacharacter_is_rejected` injection test (§4.0.4).
**Deliverables:** NEW `scripts/gen-prometheus-jobs.py` (`yaml.safe_dump`, own `--check`, GENERATED header, LF), `prometheus/jobs.d/proxmox.generated.yml`, `tests/unit/test_gen_prometheus_jobs.py`. MOD `prometheus/prometheus.yml` (add `scrape_config_files` with a **validate-resolvable path** — §8; delete the inline `proxmox` job), `docker/services/prometheus.yaml` (mount `jobs.d` + `exporters`), `telemetry/pve.yml` (`relabel`), `tests/validate.sh` (new `--check` step + the no-host-port exporter check), `deploy-stack.yml` (second regen task). NEW `scripts/gen-exporters.py` (+ `tests/unit/test_gen_exporters.py`), regenerate `pve-exporter.generated.yaml`. SECURITY C9 amendment (config-injection guard). Doc-sync.
**Acceptance:** `promtool check config` green **at host lint time with the include present** (§8); `gen-prometheus-jobs.py --check` returns 0/1 correctly; `prometheus.yml` composition-only; `test_method_param_metacharacter_is_rejected` passes; proxmox scrape byte-equivalent.
**Commit theme:** `feat(observability): generate proxy-exporter jobs + exporter containers from telemetry descriptors — the pve relabel leaves prometheus.yml (closes gap-b)`

### Phase 3 — Agent-less SNMP/blackbox monitoring (closes gap-d)
Depends on: Phase 2.
> **Split — Phase 3a (✅ SHIPPED): the Cisco core switch via SNMP.** `telemetry/snmp.yml` (proxy-exporter +
> a `params` allow-list + a static `__param_auth` label), the inline `network` job deleted and generated into
> `jobs.d/`, `modules/cisco_ios` metrics block, the `snmp_observability` SOPS domain (base-recipients) + a
> config-FILE secret-render (`snmp.yml.j2` → gitignored 0600 `snmp.yml`, `no_log`) + its gitignored covering
> check, the `snmp-exporter` container, and the flipped honesty + param-injection tests. **Phase 3b (pending):
> FortiGate + blackbox** (reachability). The env-based `.env` render loop (§4.0.6) lands with **exportarr
> (Phase 5)** — snmp's secret is a config file, not an env var, so the `.env` loop isn't exercised here.
**Deliverables:** NEW `telemetry/snmp.yml`, `telemetry/blackbox.yml`, `docker/services/snmp-exporter.yaml` + `blackbox-exporter.yaml` (no host port, access-chain header), `instance/secrets/snmp_observability.sops.yml`, `prometheus/exporters/snmp/{generator.yml,snmp.yml.generated,README.md}`, `prometheus/exporters/blackbox/blackbox.yml`, `tests/unit/test_telemetry_snmp.py`. MOD `modules/cisco_ios/module.yml` + `modules/fortigate/module.yml` (+ metrics blocks), `docker/compose.yaml` (include lines), `deploy-stack.yml` (snmp.yml community render, `no_log`; **delete the inline `network` job** here — §4.0.5), `.gitignore` (the rendered `prometheus/exporters/snmp/snmp.yml` — §8), `docker/.env.example`. **REWRITE** `test_classes_without_metrics_block_emit_no_target` so it **actually flips and guards** (§8). SECURITY C9 amendment + accepted-risk rows. Doc-sync.
**Acceptance:** `targets/network/cisco_ios.generated.yml` + `fortigate.generated.yml` generated from `inventory_group` fan-out (not a "network group"); exactly one `job_name: network`; `promtool` green; no exporter publishes a host port; the SOPS domain is encrypted and base-recipients-only; the rendered `snmp.yml` is gitignored + absent from the index; the flipped test asserts cisco/fortigate **emit** targets while openwrt (hosts, no metrics block) emits **none**.
**Commit theme:** `feat(observability): monitor the core switch + edge firewall via drop-in snmp/blackbox methods (closes gap-d, read-only pull)`

### Phase 4 — Telemetry capability vector (honest weak-suggester)
Depends on: Phase 1.
**Deliverables:** NEW `vectors/telemetry.yml` (order 4, existing predicate kinds only), `tests/unit/test_telemetry_vector.py`; the reconciliation helpers `classify.declared_metrics_methods` + `suggest_telemetry`; `ClassifyResponse.telemetry_declared`. Doc-sync incl. `docs/capability-matrix.md` (the anchored §2 edit deferred from Phase 1).
**Acceptance:** `?vector=telemetry` works; every record gains a `capabilities.telemetry` cell with zero `predicate.py` change; the vector is depth-stable (a machine assertion that no rule is `module_option` or `any_of`-containing-`module_option` — §8); the scope docstring states "suggester, not host-agent detector."
**Commit theme:** `feat(capability): add the telemetry vector as a proxy/API-exporter suggester (host_node stays operator-declared; the probe carries no host-OS fact)`

### Phase 5 — Service root (OpenAPI) + radarr/exportarr — GATED
Depends on: Phases 2–3.
**Deliverables:** NEW `build_service_plan`, `scripts/kontroll/service/observe.py` (`attach_observability`), `scripts/gen-homepage.py` (+ `--check`), `telemetry/exportarr.yml`, `docker/services/exportarr.yaml`, the `services` inventory group + SOPS domain, `modules/radarr/module.yml`, the `kind: device|service` field. Reconcile the `widget:` schema to `{type, url, secret}` and update `modules/README.md:34` (§8). Tests + doc-sync.
**Acceptance:** radarr onboards end-to-end with no hand-written compose/relabel; the Homepage tile is generated; `git diff` after a no-op re-onboard is empty.
**Commit theme:** `feat(onboard): a second onboarding root — composed services via OpenAPI converge on the shared observability opt-in (radarr/exportarr)`

### Phase 6 — Hybrid dashboard discovery — GATED
Depends on: Phase 1.
**Deliverables:** NEW `api/routes/dashboards.py` (`GET /dashboards/search`, **inquiry**), `search_dashboards()` in `fetch-dashboards.py` (timeout + degrade-to-empty + TTL cache), `DashboardCandidate` model, the GUI picker, `suggested_dashboards` in the seed descriptors. Tests + doc-sync.
**Acceptance:** curated-by-method default works offline; live tier degrades to manual; `/dashboards/search` classifies **inquiry** (privileged set unchanged); the registry-is-undocumented comment is present; single-datasource `prometheus` pin only (Loki deferred).
**Commit theme:** `feat(dashboards): hybrid discovery — curated-by-method + a live grafana.com search tier`

### Capability-track Phase 7 — Generalized secondary-capability dialog SEAM + monitoring as instance #1 — ✅ SHIPPED (live promote gated on C9)
✅ **Built** (the seam machinery, the telemetry instance, the GUI, and the INVARIANT D\* pins all landed + CI-green; the *live* promote against the real repo still needs the C9 privileged-mutation enablement — token + writable clone + age key). Depends on: Capability-track Phases 1–3 (telemetry registry + generators). **Reference of record:** [docs/observability/secondary-capability-dialog.md](observability/secondary-capability-dialog.md); the `capabilities/<cap>.yml` descriptor schema lives in its §2 (loaded by `catalog.load_capabilities()`).
**Deliverables (the SEAM, capability-neutral):** `scripts/kontroll/service/promote.py` (the stateless plan-hash gate, lifted verbatim); the capability registry — `catalog.load_capabilities()` + `registered_capabilities()` + the `capabilities/<cap>.yml` descriptor; the generic `openCapabilityDialog(cap, key, {root, collection})`; `api/routes/capability.py` (privileged, dispatched by path segment) + the route-loop tripwire; per-descriptor `applies_when` predicate blocks on the telemetry method descriptors (resolving the design-only filter); the generic **INVARIANT D\*** pins (a positive allow-list primary + a registry-parametrized secondary guarded by a min-count assert).
**Deliverables (monitoring = instance #1):** `scripts/kontroll/service/observe.py` + the `metrics:`/`dashboards:` write; the method + dashboard pickers; the telemetry enact builder; `capabilities/telemetry.yml`; `tests/{integration/test_api_capability,integration/test_gui_api,e2e/test_capability_flow}.py`; the SECURITY C9 amendment; the `docs/logging-architecture.md` `capability-promote`/`capability-result` audit rows. (The audit action is **`capability-promote`** — capability-neutral, not the telemetry-specific `observe-promote` an earlier draft used.)
**Acceptance:** the dialog opens for an onboarded key via `openCapabilityDialog("telemetry", key, …)`; propose writes nothing; promote is plan-hash-gated, commits only the plan's paths, audited, never a credential; the API runs no plays; onboarding writes no capability block and the nudge is render-only/non-binding (INVARIANT D\*, test-pinned); registering the telemetry instance touched **zero** seam file beyond its descriptor.
**Commit theme:** `feat(capability): generalized secondary-capability dialog seam + monitoring as instance #1`

### Capability-track Phase 8 — Backup as instance #2 — ✅ SHIPPED (live Semaphore registration gated on C9/deploy)
✅ **Built** + CI-green: backup registered as a pure drop-in touching **zero spine file** (verified by diff) — `registered_capabilities() == ['telemetry','backup']`, the registry-parametrized INVARIANT D\* pins auto-cover it with no new test code, and `openCapabilityDialog("backup", key, …)` reuses the generic shell (schedule-as-preset-param) with no shell edit. `gen-backup.py` generates the per-class schedule spec; `configure-semaphore.py` iterates it (the hand-listed `nightly-backup` hub removed); every backup-capable class carries a `backup:` block (no coverage regression). The *live* Semaphore registration is the operator's deploy step. Depends on: Phase 7. **Reference of record:** [secondary-capability-dialog.md §2.2 + §4](observability/secondary-capability-dialog.md).
**Deliverables:** the `backup:` block (`block_shape: mapping`) in `module.yml` + the `backup.capable == backup_capable` validate equality check; `scripts/gen-backup.py` (→ `config/semaphore/schedules.generated.yml`, consumed by `configure-semaphore.py` — removing the hand-listed `tpl_backup`/`nightly-backup` hub at `configure-semaphore.py:196-210`); `classify.suggest_backup`/`declared_backup`; `capabilities/backup.yml`; `validate.sh` `gen-backup --check`; `modules/README.md` `backup:` row. **MVP: schedule-only** (retention deferred until a prune step lands; `destination: offsite` not rendered — see the SECURITY captures deferred-risk).
**Acceptance:** `openCapabilityDialog("backup", key, …)` works with **zero** edit to `promote.py` / the shell / the generic route / the generic pins; `registered_capabilities() == ["telemetry","backup"]` and the parametrized D\* pins auto-cover backup with no new test code.
**Commit theme:** `feat(capability): backup as secondary-capability instance #2 (schedule-only MVP)`

---

## 7. Consolidated checklist (whole effort)

### Tests
- [ ] Every `def test_*` has a docstring (what + the failure it guards) — `tests/check-test-docs.py` enforces (`validate.sh:67`).
- [ ] Phase 1: 4 new registry tests + 4 existing `test_gen_observability.py` green unchanged; **byte-identity** proven by an empty `git diff prometheus/targets/`.
- [ ] Each new generated artifact (`jobs.d/`, exporter fragments, homepage tiles) has its **own** `--check` step in `validate.sh` (copy `gen-observability.py` `main()` set-math).
- [ ] The injection test drives a metacharacter through the **label** path on a params-bearing class.
- [ ] The flipped honesty test (Phase 3) actually flips (cisco/fortigate emit; openwrt does not) and guards a real residual.
- [ ] Phase 7: the full privileged-route auth/audit/dry-run/cred-exclusion battery + the classification tripwire.
- [ ] E2E selects on `data-testid` only; new ids recorded in `tests/testid_reference.md` (grep-verified).

### Logging / audit
- [ ] Generators print non-secret path/count lines only; never a credential.
- [ ] Phase-7 `/observe` apply emits `observe-promote` (6-field, fail-closed, `run_id`, method/dashboards/secret-domain-NAME/plan_token — **never** a value).
- [ ] The enact step receives `-e kontroll_run_id=<id>` so GUI/API click → playbook is greppable.
- [ ] `no_log: true` on every SOPS/secret-render task (`.env`, `snmp.yml`).

### Security
- [ ] No new secret in Phases 1–2 (host_node/pve reuse existing posture).
- [ ] Each proxy-exporter secret in its **own** base-recipients-only SOPS domain (catch-all), `no_log`-rendered, never the Semaphore key.
- [ ] No exporter publishes a host port (validate covering check over `docker/services/*exporter*.yaml`).
- [ ] `?target=`/`?module=` bounded to **generated** inventory addresses (no SSRF; no caller-supplied target).
- [ ] `verify_tls` defaults **true**; any `false` is per-method, mgmt-VLAN, SECURITY.md-documented.
- [ ] The deploy-stack `.env` secret-render is **loop-generated** from each method's `secret_domain`/`secret_env_map` (§4.0.6) — adding an exporter secret is a descriptor drop-in; a covering test asserts no method secret is silently dropped, and `validate` rejects an undefined SOPS domain.
- [ ] The rendered `prometheus/exporters/snmp/snmp.yml` is gitignored + index-absent (covering test).
- [ ] Phase 7: `/observe` is privileged + fail-closed; the commit is scoped to the plan's specific paths; the generator runs in-process, not via subprocess.

### Doc-sync (per `CLAUDE.md` table)
- [ ] `modules/README.md` (metrics schema → `method/params`; `widget:` shape reconciled to `{type,url,secret}`).
- [ ] NEW `telemetry/README.md` (canonical schema, reciprocal See-also).
- [ ] `prometheus/README.md` (`jobs.d/` mechanism + job-owned-by-method).
- [ ] `instance/secrets/README.md` + `.sops.yaml` decision (new domains).
- [ ] `SECURITY.md` C9 amendments (proxy generation, snmp/blackbox, `/observe`) — each control→file→covering-check.
- [ ] `docs/logging-architecture.md` (`observe-*` audit rows).
- [ ] `docs/capability-matrix.md` (telemetry vector vs method registry — Phase 4).
- [ ] `docs/api-architecture.md` (`/dashboards/search`, `/observe`; §11 service lane built).
- [ ] `tests/testid_reference.md`, `tests/README.md`, `CHANGELOG.md` each phase.

---

## 8. Resolved conflicts (how the QA blockers/majors were applied)

Every QA finding and its disposition. **Sections flagged MUST-CHANGE-before-implementation are bolded.**

| QA finding (severity) | Disposition |
|---|---|
| **BLOCKER — "network inventory group" factual error + trivially-passing flipped test** (coherence) | Applied in §4.0.5 + Phase 3. `network` is a job/path bucket; host fan-out is from `inventory_group: core_switch`/`edge_firewall`/`wireless_ap`. **The agent-less and delivery sections MUST delete every "network inventory group" phrase and rewrite `test_classes_without_metrics_block_emit_no_target` to assert cisco/fortigate produce a file while openwrt (hosts, no metrics block) produces none.** |
| **BLOCKER — /observe tripwire conflates two mechanisms** (coherence/security) | Applied in §6 Phase 7 + checklist. Two distinct same-commit edits: (1) `tags=['privileged']` + add the exact `/observe` to the literal set at `test_api_ratelimit.py:182`; (2) **separately** add the prefix `/observe` to `_PRIVILEGED` at `ratelimit.py:53`. The tuple holds `/audit` (prefix), not `/audit/log` — the two sets are intentionally not byte-equal. **The GUI/delivery sections MUST state both edits.** |
| **BLOCKER — two incompatible dispatcher specs** (implementability) | Resolved in §4.0.3: the keystone clean-cutover + fail-closed variant is canonical; **the Delivery Runbook's 3-arg `_entries`/`_resolve_method`/legacy-shim sketch is struck.** Signatures pinned: `metrics_files(fleet, inv, methods=None)`, `_entries(method, key, group, hosts, met)`. |
| **BLOCKER — false "cannot import kontroll" premise → unneeded duplicate loader** (implementability) | Resolved in §4.0.1: verified `from kontroll import catalog` resolves in all run contexts; **the duplicate `_load_methods()` and `test_loaders_agree` are struck.** The keystone section MUST drop them. |
| **BLOCKER — deploy-stack `.env` secret-render is an unspecified hub edit** (security) | **RESOLVED (operator decision 2026-06-14):** generate it — `deploy-stack`'s `.env` render **loops over enabled methods' `secret_domain`/`secret_env_map`** (§4.0.6), so adding an exporter secret is a descriptor drop-in, not a `deploy-stack.yml` edit (the future-proofed/modular option chosen over the documented-hub-edit alternative). Covering test: every method with a non-null `secret_domain` declares a `secret_env_map`; `validate` rejects an undefined domain. The two-roots/agent-less sections MUST stop describing it as a trivial "mirror" and reference §4.0.6. |
| **BLOCKER — `network` job inline-vs-generated + duplicate `job_name`** (security) | Resolved in §4.0.5: the inline `network` job is deleted in the Phase-3 commit that adds `jobs.d/network.generated.yml`; acceptance asserts exactly one `job_name: network` + `promtool` green. |
| MAJOR — loader/Catalog-field named three ways | Resolved in §4.0.1: `load_telemetry()` + `Catalog.telemetry` canonical; aliases forbidden. **All sections referencing `load_telemetry_methods`/`Catalog.telemetry_methods` MUST be renamed.** |
| MAJOR — `telemetry/<method>.yml` schema in three shapes | Resolved in §4.0.2: one flat canonical schema; the nested `target:{…}` encoding superseded; `agent_role` top-level; `params` (not `params_schema`) the field name. **All section code sketches MUST read these field names.** |
| MAJOR — `safe_dump` byte-identity overstated | Resolved in §4.0.4: bare labels unchanged in the migration commit; `_yaml_scalar` quote-only-if-needed for param-bearing values; injection test on a params-bearing class only; golden-bytes assertion in CI before any param class lands. |
| MAJOR — node_exporter demotion only in the scrape generator | Applied in Phase-1 acceptance wording ("…in the **observability-generator** registry") + a deferred item: generalize `install-node-exporter.yml` to derive its host-set from `method: host_node` declarers and add `windows_exporter` to prove it. |
| MAJOR — config-injection guard split/inconsistent (`__param_module` label) | Resolved in §4.0.4: one authoritative escaping rule covering the new `__param_*` labels; **the agent-less §3.4 "no change to `_render`" statement MUST be corrected**; the snmp `module` allow-list MUST be a single canonical enum pinned to `generator.yml` by `test_snmp_params_schema_enum_matches_generator_modules`. |
| MAJOR — promtool + container-absolute `scrape_config_files` path | Applied in Phase-2 acceptance: pin the include glob to a **validate-resolvable** path (repo-relative, or a committed empty `.generated.yml` so the glob matches), and **verify `promtool` returns 0 with the include present** before claiming it. |
| MAJOR — observe commit sweeps unrelated target drift / subprocess exec | Applied in Phase-7 acceptance: call the generator's `metrics_files`/`_render` **in-process** (not `gitio._run` subprocess); scope `commit_and_push` to the plan's **specific** file paths; have it refuse if `git status` shows staged changes outside the plan set. |
| MAJOR — second on-disk secret (`snmp.yml`) gitignore/test | Applied in Phase-3 deliverables + checklist: explicit `.gitignore` entry + a covering test; prefer `.env` env-substitution if `snmp_exporter` supports it, else document a distinct accepted-risk. |
| MAJOR — telemetry vector depth-stability stated as engine guarantee | Resolved in §6 Phase-4 acceptance: reworded to "current rules are depth-stable; the test guards that no `module_option`/`any_of`-containing-`module_option` rule is added without a parity test," plus a machine assertion. |
| MINOR — `_yaml_scalar` dead `endswith` branch | Applied in §4.0.4: drop the branch. |
| MINOR — Phase-1 land-order / stale-lockfile red `--check` | Applied in Phase-1 land-order steps. |
| MINOR — `docs/capability-matrix.md` Phase-1 doc-sync lacks anchor | Applied: moved to Phase 4 (where the vector lands). |
| MINOR — bootstrap secrets-domain visibility (module vs method) | Applied in §7 checklist: module-level `secrets_domain` IS in the bootstrap report; only a **method** `secret_domain` needs the `gen-secret-domains.py` bridge (deferred, named). One cross-reference resolves the apparent contradiction. |
| MINOR — widget schema drift + marker-region splice | Applied in Phase 5: reconcile `widget:` to `{type, url, secret}` and update `modules/README.md:34` same commit; prefer a per-service drop-in over an in-file marker splice, else a `--check` that fails on a hand-edit inside the markers. |
| MINOR — `plan_token` framed as a security control | Applied in Phase 7: stated as a **consistency/anti-drift** gate, not authorization (which is `require_token` + audit); if adversarial properties are ever wanted, key it with the server `api_token` (HMAC, full digest). |
| MINOR — `verify_tls`/`validate_certs` default-unsafe | Applied in §4.0.2: `verify_tls` defaults **true**; the OpenAPI service root surfaces the `validate_certs` choice in the plan view and defaults to verify-on for non-mgmt targets. |

**Net:** the architecture is sound and Phase 1 is a clean, low-risk, fully-specified single commit. Before implementation, the **telemetry-method-registry**, **agent-less-monitoring**, **two-root-onboarding**, **gui-actuatable-flow**, and **delivery-runbook** section docs MUST be reconciled to §4.0 (loader/Catalog name, flat schema, single dispatcher, dropped duplicate loader, the `network` job/group correction, the corrected flipped test, the two distinct tripwire edits, and the owned `.env` secret-render mechanism). The **telemetry-capability-vector** and **hybrid-dashboard-discovery** sections are consistent with §4.0 as written and need only the minor wording fixes noted above.

---

## 9. Risk register & rollback

| Risk | Blast radius | Mitigation | Rollback |
|---|---|---|---|
| **The Phase-1 refactor changes live scrape output** for the 5/5 production node_exporter hosts or pve-exporter | Loss of host/PVE metrics (panels go blank) | Byte-identity is the **acceptance gate** (`git diff prometheus/targets/` empty); the two seed descriptors reproduce today's rows exactly; existing tests pin the bytes | `git revert` the single Phase-1 commit — descriptors + dispatcher + two `module.yml` migrations are one atomic commit; reverting restores the `via` `if/else` and the hand-listed `metrics:` blocks |
| **pve-exporter wiring breaks** when its relabel moves to `jobs.d/` (Phase 2) | Loss of Proxmox cluster metrics | `promtool check config` green + a test that the generated proxmox job byte-matches the prior hand-written relabel; `pve-exporter` reuses the **existing** read-only `*.Audit` token (no new secret) | Revert Phase 2; the inline `proxmox` job + hand-written relabel return; `jobs.d/` is additive |
| **High-blast-radius: Cisco core switch / FortiGate edge firewall** touched by Phase 3 | The switch + firewall are the highest-blast-radius devices in the lab | Monitoring is **read-only pull** — SNMP GET / ICMP probe are **incapable of actuating** the device; the exporter publishes no host port; `?target=` is bounded to generated inventory addresses; no state-changing play touches these classes; the access-chain header documents "Blast radius: control node only" | Revert Phase 3 (delete the metrics blocks, the snmp/blackbox descriptors, the exporter fragments, restore the empty inline `network` job). The devices were never written to, so there is nothing to undo on the lab side — only kontroll-side data |
| **A bad dashboard datasource/JSON** is a fatal Grafana provisioning event | Grafana fails to load (volume recreate needed) | The fetch stays **operator-CLI/deploy-stack** (never the network surface); curated boards are pinned to the fixed `prometheus` uid; the live-search tier never auto-applies | Remove the offending `dashboards/grafana/dashboards/<name>.json`; the file provider reloads |
| **`/observe` promote sweeps unrelated drift** into an audited commit | A single-device confirm commits someone else's target/inventory edit | Phase-7 scopes the commit to the plan's **specific** paths; `commit_and_push` refuses on out-of-plan staged changes; `plan_token` hashes the full write set | `git revert` the observe commit (data-only; nothing actuated) |
| **The privileged surface is enabled prematurely** (token + writable clone + age key) | Network-reachable mutation surface | The MVP (Phases 1–6) crosses **no** trust boundary; Phase 7 is **gated** on the separately-tracked C9 enablement and is **propose-only** even then (no Ansible/Docker from the network surface) | `/observe` 503s without `KONTROLL_API_TOKEN`; the route is disabled by default on the read-only deployment |
| **A new generated artifact rots** (no `--check`) | Silent stale config | Every generator (`gen-observability`, `gen-prometheus-jobs`, `gen-exporters`, `gen-homepage`) ships its own `--check` step in `validate.sh` in the same commit | `--check` fails CI loudly; regenerate + recommit |

**General rollback posture:** every phase is one atomic commit; nothing in Phases 1–6 touches the lab (all writes are kontroll-repo data + operator-run generators). `git revert <phase>` is always a complete rollback for Phases 1–6. Phase 7's promote is also data-only (enact is delegated), so its revert is likewise clean. The only lab-touching steps are the **enact** commands (agent install / exporter provision / deploy-stack), which run through the existing `--check --diff`-gated, idempotent operator playbooks — and which monitor read-only, never actuate, the high-blast-radius devices.

## See also

- [modules/README.md](../modules/README.md) — the `module.yml` self-describing schema this design extends
- [prometheus/README.md](../prometheus/README.md) — the generated scrape-target mechanism
- [dashboards/README.md](../dashboards/README.md) — the curated-dashboard provisioning chain
- [docs/capability-matrix.md](capability-matrix.md) — the capability-vector engine the telemetry vector joins
- [docs/api-architecture.md](api-architecture.md) — §11 services-via-OpenAPI (the service root)
- [docs/reviews/2026-06-14-option-a/00-self-review-and-qa.md](reviews/2026-06-14-option-a/00-self-review-and-qa.md) — the design's adversarial self-review + QA trail
