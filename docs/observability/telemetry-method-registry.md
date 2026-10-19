# Telemetry-method registry & generator dispatcher

> **DESIGN HISTORY — not a description of the tree.** This is the design record of the telemetry-method registry and its generator dispatcher, written in June 2026. It was built; [telemetry/README.md](../../telemetry/README.md) and [prometheus/README.md](../../prometheus/README.md) describe what exists. Where it disagrees with the code or with `SECURITY.md`, they are right.

> **Part of the [Option-A observability + onboarding flow](../observability-onboarding-flow.md) design (baseline HEAD `63de129`).**
>
> ⚠️ **Binding contracts (the catalog loader name, the flat `telemetry/<method>.yml` schema, the single clean-cutover dispatcher, the `network` job-vs-inventory-group distinction, the `.env` secret-render mechanism) are pinned in the master design's §4.0 SHARED CONTRACTS.** Where this section names a symbol, schema, or sequencing differently, **§4.0 wins** and this section is reconciled to it before implementation. See the master design's §8 “Resolved conflicts” for the per-section reconciliation list.

> **Reconciliation note (Phase-1 section — read before implementing).** This section predates §4.0 and is the imminent build, so the specific overrides are spelled out here. Where this section differs, **§4.0 wins**:
> - **Loader** → `catalog.load_telemetry()` (not `load_telemetry_methods()`); field **`Catalog.telemetry`**. (§4.0.1)
> - **No duplicate loader** — `gen-observability.py` does `from kontroll import catalog; catalog.load_telemetry()`; the `_load_methods()` twin and `test_loaders_agree` are **struck** (the "can't import kontroll" premise is false — verified standalone). (§4.0.1)
> - **Descriptor schema is FLAT** — use `target_shape`/`port`/`labels`/`agent_role`, not any nested `target: {address, port, labels}` encoding. (§4.0.2)
> - **Dispatcher = clean cutover, fail-closed**: `metrics_files(fleet, inv, methods=None)`, `_entries(method, key, group, hosts, met)`; migrate the two `module.yml` in the same commit; **no legacy `via` shim**; `(job,key)` collision check scoped **per-module**. (§4.0.3)
> - **YAML escaping** per §4.0.4 — bare labels unchanged in the byte-identical migration; `_yaml_scalar` only for param-bearing values; drop the dead `endswith` branch.
>
> **✅ Phase 1 SHIPPED.** The registry (`telemetry/host_node.yml` + `telemetry/pve.yml`), `paths.TELEMETRY_DIR` + `catalog.load_telemetry()`, the clean-cutover fail-closed dispatcher in `scripts/gen-observability.py`, the byte-identical migration of `proxmox`/`docker_host`, and `tests/unit/test_telemetry_methods.py` all landed exactly per §4.0 (`git diff prometheus/targets/` empty; suite green). This section stays the **design of record**; its Phase-2+ content (proxy-job generation, the full proxy/`params`/`exporter` schema) is still design, and §4.0 remains the binding contract.

## 0. Thesis and what this section delivers (and explicitly does NOT)

This section designs the **keystone seam** of Option-A: promoting `via: host|proxy` from a closed 2-value `if/else` (`scripts/gen-observability.py:51`) into a **drop-in registry** of `telemetry/<method>.yml` descriptors, and refactoring `gen-observability.py` from a branch into a **dispatcher** over that registry — while keeping the generated-lockfile + `--check` guarantees byte-identical.

It heeds the self-review's three blockers and re-scopes accordingly:

- **In scope (Layer 1, the keystone — fully specified, single landable commit):** the `telemetry/` registry schema; the `gen-observability.py` dispatcher refactor; `module.yml` `metrics:` referencing a method **by name + per-class params**; the clean cutover of `proxmox`/`docker_host`; the `--check` extension; **node_exporter demoted to one descriptor row**; unknown-method-fails-closed; YAML output hardened to `yaml.safe_dump`-equivalent escaping.
- **In scope (Layer 1b, a second landable commit, same registry):** a **host-agent-only** proof-of-extensibility method that requires **no `prometheus.yml` change** — but I also fully specify the **proxy-exporter generation** mechanism (the `scrape_config_files` include + `gen-prometheus-jobs.py`) and the **snmp** method as a *Phase-2 design appendix* so the implementer has every decision pre-made, while keeping it out of the MVP commit.
- **Explicitly OUT (deferred, named only as registry rows + appendix):** `vectors/telemetry.yml`, `otel`, `loki`, `exportarr`, the GUI/API actuation route, the service (OpenAPI) root, the live grafana.com dashboard search. The self-review's blockers (proxy relabel must move into data *with its own `--check`* before any proxy method ships; the service root is new architecture; the GUI flow depends on un-built propose-then-promote) are honored by phasing.

Per the security self-review's two blockers: **nothing here actuates.** The registry + dispatcher are pure CLI/generator code, run by CI and `deploy-stack` exactly as `gen-observability.py` is today. No agent install, no exporter provision, no `.env` render, no privileged route is added by this section.

---

## 1. The registry: `telemetry/<method>.yml`

### 1.1 Why a separate registry (not a vector, not a backend)

Grounded in the real code:

- `catalog.load_vectors()` (`scripts/kontroll/catalog.py:27-33`) globs `vectors/*.yml`, sorts by `order`. `catalog.load_backends()` (`:50-59`) globs `ansible/backends/<name>/backend.yml`, sorts by `order`. These are the two drop-in idioms to copy.
- A **capability vector** (`vectors/actuate.yml`) is consumed by `predicate.eval_vector` to *detect* a trait from collection facts. A **telemetry method** is consumed by `gen-observability.py` to *generate* a scrape target/exporter wiring. **They are different concepts** (the self-review's "major" is correct: the vector cannot detect host-OS, so host_node is operator-declared, not vector-detected). This section owns only the **method registry** (the generator's input). `vectors/telemetry.yml` is deferred.
- The closest **schema analogue** is `backend.yml` (`ansible/backends/netcommon_cli/backend.yml`): `name`/`label`/`order` + data the generator/role consumes (`confers`, `defaults`, `connection`) that the predicate engine never touches. The telemetry descriptor mirrors this: **descriptor fields are GENERATOR-consumed data, never classifier input.**

The registry is **flat files** `telemetry/<method>.yml` (not dir-per-method like backends), because a method is a single descriptor with no co-located role/recipe dir — it *references* an existing ansible role by name. This matches `vectors/*.yml`'s flat shape.

### 1.2 New file: `scripts/kontroll/paths.py` — add `TELEMETRY_DIR`

**MODIFIED** `scripts/kontroll/paths.py` (insert after line 14, the `VECTORS_DIR` line):

```python
VECTORS_DIR = os.path.join(ROOT, "vectors")
TELEMETRY_DIR = os.path.join(ROOT, "telemetry")          # NEW: telemetry/<method>.yml method registry
BACKENDS_DIR = os.path.join(ROOT, "ansible", "backends")
```

### 1.3 New loader: `catalog.load_telemetry_methods()`

**MODIFIED** `scripts/kontroll/catalog.py` — add this function directly after `load_vectors()` (mirrors it exactly so the drop-in doctrine is visibly the same idiom):

```python
def load_telemetry_methods():
    """telemetry/<method>.yml — the drop-in telemetry-METHOD registry the observability generator
    dispatches over. Returns {name: descriptor}. Mirrors load_vectors/load_backends: glob the dir, parse
    each, sorted-by-order is irrelevant here (keyed by name) but we keep the read deterministic. A method
    descriptor is GENERATOR data (target shape, job, port, exporter wiring) — never classifier input."""
    out = {}
    if not os.path.isdir(paths.TELEMETRY_DIR):
        return out
    for fn in sorted(os.listdir(paths.TELEMETRY_DIR)):
        if fn.endswith((".yml", ".yaml")):
            with open(os.path.join(paths.TELEMETRY_DIR, fn), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
                if doc.get("name"):
                    out[doc["name"]] = doc
    return out
```

> **Import note for the generator.** `gen-observability.py` is *standalone* (stdlib + yaml only, no `kontroll` package import — confirmed: it computes its own `ROOT` and `_load`). To keep that property (CI + `deploy-stack` run it without the package on `sys.path`), the generator does **not** import `catalog.load_telemetry_methods`. Instead it carries a **local, identical** loader (`_load_methods()` below) reading `ROOT/telemetry/*.yml`. `catalog.load_telemetry_methods()` exists for the *future* API/GUI/vector consumers (Layer 2+) that already import `kontroll.catalog`. The two loaders are intentionally parallel; a unit test asserts they return the same set (§5, `test_loaders_agree`).

### 1.4 The descriptor schema (every field, full semantics)

A `telemetry/<method>.yml` file:

```yaml
# ── identity ──────────────────────────────────────────────────────────────
name:        <str>      # REQUIRED. Unique method id. Must equal the filename stem and the value a
                        #   module.yml metrics entry references as `method:`. This is the registry key.
label:       <str>      # REQUIRED. Human one-liner (shown in docs / future GUI picker).
kind:        <enum>     # REQUIRED. One of: host-agent | proxy-exporter
                        #   (push | log-shipping are RESERVED — see §7; the dispatcher rejects them in
                        #    Layer 1 with a clear error, never silently. Open enum: a 3rd kind is a
                        #    code addition in render dispatch, gated by a test, NOT a registry-only change.)

# ── prometheus target shape (the half that REPLACES the via if/else) ───────
job:         <str>      # REQUIRED. The prometheus job_name this method's targets belong to. Determines the
                        #   output path prometheus/targets/<job>/<key>.generated.yml. Declared ONCE here,
                        #   NOT per device class (closes gap f). Two methods MUST NOT share a job unless they
                        #   produce identical target shapes (the generator asserts job uniqueness per class).
target:                 # REQUIRED. How a (targets, labels) row is rendered per host. A small, CLOSED
                        #   data-driven template — NOT a DSL. Fields:
  address: <enum>       #   host_port | device   .
                        #     host_port -> "<ansible_host>:<port>"   (host-agent: the host exposes the port)
                        #     device    -> "<ansible_host>"          (proxy-exporter: bare device addr; the
                        #                                             job relabel proxies it to the exporter)
  port:    <int|null>   #   REQUIRED iff address==host_port; MUST be null/absent for address==device.
  labels:  [<str>...]   #   REQUIRED. Ordered list of label KEYS to emit, chosen from the closed label
                        #     vocabulary {group, host}. Values are filled by the generator: group=<inventory
                        #     group>, host=<hostname>. Order is preserved byte-for-byte in the output.

# ── host-agent kind: the installed agent identity (closes gap c) ───────────
agent:                  # REQUIRED iff kind==host-agent; ABSENT otherwise.
  role:    <str>        #   The ansible/roles/<role> that installs the agent. node_exporter today; a
                        #     windows_exporter / cadvisor method names a DIFFERENT role here. DOCUMENTARY in
                        #     Layer 1 (the generator does not run it); the install play / future opt-in reads it.
  inventory_filter: ''  #   OPTIONAL. Documentary note on which host-set runs this agent (e.g. 'Debian|Alpine').
                        #     Layer 1 does not derive host-sets from this; install-node-exporter.yml stays as-is.

# ── proxy-exporter kind: the exporter wiring (the data that closes gap b) ──
exporter:               # REQUIRED iff kind==proxy-exporter; ABSENT otherwise.
  address:  <str>       #   The exporter's address:port on the kontroll docker network, e.g. pve-exporter:9221.
                        #     This is the `replacement:` the prometheus job relabel rewrites __address__ to.
  metrics_path: <str>   #   The exporter's scrape path, e.g. /pve  (becomes the job's metrics_path).
  param_target: <bool>  #   true  => multi-target exporter: pass ?target=<device> (the pve/snmp/blackbox shape).
                        #     The relabel maps __address__ -> __param_target -> instance, then rewrites
                        #     __address__ to exporter.address.  false => single-target exporter (no ?target).
  service_fragment: <str>  # The docker/services/<x>.yaml compose fragment that runs the exporter (documentary
                        #     in Layer 1: the operator authors it; Layer 3 may generate it). e.g. pve-exporter.yaml
  secret_domain: <str|null>  # The instance/secrets/<domain>.sops.yml the exporter's credential lives in, OR
                        #     null if the exporter needs no secret. pve REUSES the existing `proxmox` domain
                        #     (no new secret). A NEW exporter (snmp) names its OWN base-recipients-only domain.
  tls_verify_env: <str> #   OPTIONAL — the env var (e.g. PVE_VERIFY_SSL) gen-exporters FILLS from the consuming
                        #     device class's TLS posture (no-bespoke-config tenet): never a hardcoded verify bool
                        #     here. The verify default is DERIVED from the class `vendor_defaults.tls_posture`
                        #     (modules/<key>: self_signed -> "false") ⊕ the instance CA pin
                        #     (instance.yml device_trust.<key>.tls_ca_file -> "true"), via scripts/kontroll/
                        #     endpoints.py — the SAME vendor fact the logging pull (logging/proxmox_api.yml
                        #     tls_from_class) derives, so the two consumers can't drift. Fail-closed: a missing/
                        #     unknown class posture exits non-zero. A pinned CA is fully wired: gen-exporters also
                        #     sets REQUESTS_CA_BUNDLE (PVE_VERIFY_SSL is bool-only, not a path) + appends a :ro mount
                        #     of the deploy-stack-assembled CA bundle — the SAME bundle the Vector pull verifies against.

# ── per-class params: the closed allow-list a module may supply ───────────
params:                 # OPTIONAL. Schema for the per-class params a module.yml metrics entry may pass under
                        #   `params:`. Each key declares its allowed values — a CLOSED allow-list (security
                        #   self-review blocker: onboarding-derived params must never be free-interpolated).
  <param_name>:
    allowed: [<v>...]   #   REQUIRED. Enum of permitted values. The generator HARD-FAILS on any value not in
                        #     this list (never interpolates an unknown value into a target/relabel/job).
    required: <bool>    #   OPTIONAL (default false). If true, a module referencing this method MUST supply it.
    label_as: <str|null>  # OPTIONAL. If set, the param's value is emitted as an extra label with this key
                        #     (e.g. snmp `module: if_mib` -> label `module: if_mib`). The label key MUST be in
                        #     the closed label vocabulary extension declared by the method (see snmp example).
```

**Field-validation rules the loader/generator enforce (fail-closed, with a clear message — never silent):**

1. `name`, `label`, `kind`, `job`, `target` are present; `kind ∈ {host-agent, proxy-exporter}` (reserved kinds rejected with `"telemetry method '<name>': kind '<k>' is reserved/not yet implemented"`).
2. `target.address ∈ {host_port, device}`; `host_port` requires `target.port` (int); `device` forbids `target.port`.
3. `target.labels` keys are a subset of the **closed label vocabulary** `{group, host}` plus any `label_as` keys declared in `params`.
4. `kind==host-agent` requires `agent.role`; `kind==proxy-exporter` requires `exporter.address`, `exporter.metrics_path`.
5. A module metrics entry's `params` keys are a subset of the descriptor's `params` schema; each supplied value is in that param's `allowed` list; all `required: true` params are present. **Violation = `sys.exit(1)` with the offending `(class, method, param, value)`** — this is the config-injection guard.

### 1.5 The seed descriptors (re-express today's wiring byte-for-byte)

#### NEW `telemetry/host_node.yml` — node_exporter demoted to ONE row

```yaml
# Telemetry method: host_node — the host runs the Prometheus node_exporter and exposes host
# CPU/mem/disk/net on :9100. This is the `via: host` shape, now a DROP-IN row, not the generator's
# default branch. A windows_exporter / cadvisor method is a SIBLING file with a different agent.role —
# node_exporter is no longer THE pipe, it is one descriptor among the registry.
name: host_node
label: Host node_exporter (:9100 host metrics)
kind: host-agent
job: node                       # -> prometheus/targets/node/<key>.generated.yml
target:
  address: host_port            # "<ansible_host>:<port>"
  port: 9100
  labels: [group, host]         # {group: <inventory_group>, host: <hostname>}  — order is load-bearing
agent:
  role: node_exporter           # ansible/roles/node_exporter (OS-aware: Debian apt/systemd, Alpine apk/OpenRC)
  inventory_filter: 'Debian|Alpine'   # documentary: the OS families roles/node_exporter supports
```

#### NEW `telemetry/pve.yml` — the pve-exporter contract, now data

```yaml
# Telemetry method: pve — VM/LXC/storage/cluster metrics via prometheus-pve-exporter, a control-node
# (proxy) exporter that queries the PVE API read-only. This was the `via: proxy` shape; the exporter
# binding (which exporter, port, ?target, metrics_path) that today lives HAND-WRITTEN in prometheus.yml
# (the `proxmox` job relabel, lines 18-24) is captured here as DATA. In Layer 1 the generator still only
# emits the target file (byte-identical to today) and prometheus.yml keeps its hand-written relabel;
# Phase 2 (gen-prometheus-jobs.py, §6) GENERATES that relabel FROM this descriptor and removes the hand-edit.
name: pve
label: Proxmox VE via prometheus-pve-exporter (read-only API token)
kind: proxy-exporter
job: proxmox                    # -> prometheus/targets/proxmox/<key>.generated.yml
target:
  address: device               # bare "<ansible_host>" — the relabel proxies it to the exporter
  port: null
  labels: [host]                # {host: <hostname>} only (matches the current via:proxy output)
exporter:
  address: pve-exporter:9221    # the `replacement:` the prometheus relabel rewrites __address__ to
  metrics_path: /pve            # the job's metrics_path
  param_target: true            # multi-target: ?target=<pve host>
  service_fragment: pve-exporter.yaml
  secret_domain: proxmox        # REUSES the existing read-only *.Audit token — NO new secret (C9)
  verify_tls: false             # PVE serves a self-signed cert on the mgmt VLAN (C3-accepted)
```

These two descriptors **reproduce the current `_entries` output exactly**:
- `host_node` → `["%s:%s" % (ip, 9100)]` + `{"group": group, "host": name}` = the current `else` branch.
- `pve` → `[ip]` + `{"host": name}` = the current `if via=='proxy'` branch.

The acceptance test is **`git diff prometheus/targets/` is empty** after the refactor (§8).

#### Phase-2 appendix descriptor (specified, NOT in the MVP commit): `telemetry/snmp.yml`

```yaml
# Telemetry method: snmp — agent-less metrics from SSH/SNMP-only gear (cisco_ios core switch, fortigate
# edge) via a control-node snmp_exporter. PROOF that a 3rd method is a pure drop-in: adding it touches NO
# hub file (no gen-observability.py edit, no prometheus.yml scrape_configs edit once §6's include lands).
# DEFERRED out of the keystone MVP — ships with gen-prometheus-jobs.py (§6) + its SOPS domain + C-control.
name: snmp
label: SNMP via snmp_exporter (agent-less switches/firewalls)
kind: proxy-exporter
job: network                    # -> prometheus/targets/network/<key>.generated.yml (the empty bucket today)
target:
  address: device
  port: null
  labels: [host]
exporter:
  address: snmp-exporter:9116
  metrics_path: /snmp
  param_target: true
  service_fragment: snmp-exporter.yaml
  secret_domain: snmp_observability   # NEW base-recipients-only domain (SNMP community); §4
  verify_tls: false
params:
  module:
    allowed: [if_mib, cisco_wlc, mikrotik]   # closed allow-list of snmp_exporter modules
    required: true
    label_as: module                          # emit `module: if_mib` as a label (and as ?module= in relabel)
```

> `snmp`'s `?module=` relabel is part of the Phase-2 `gen-prometheus-jobs.py` design (§6). It is listed here only so the registry's extensibility is concrete and the `params` allow-list is exercised. **Do not add `snmp` to any module.yml in the keystone commit** — that would flip `test_classes_without_metrics_block_emit_no_target` (§5, §8).

> **As-built note (2026-06-17):** snmp shipped with **SNMPv3 authPriv** + `params.module.allowed: [if_mib]`, and the `if_mib` module is now **GENERATED** (`scripts/gen-snmp.py` from a vendored MIB closure → `prometheus/exporters/snmp/modules.generated.yml`). The method *contract* (kind/job/params/`secret_domain`/the config-FILE render) is **unchanged** by that — only the modules *source* moved from hand-authored to generated. See [prometheus/exporters/snmp/README.md](../../prometheus/exporters/snmp/README.md).

#### Reserved rows (named only, no file in Layer 1)

`telemetry/blackbox.yml` (reachability, `kind: proxy-exporter`, SSRF-shaped — §7), `telemetry/otel.yml` (`kind: push`, reserved), `telemetry/loki.yml` (`kind: log-shipping`, reserved), `telemetry/exportarr.yml` (radarr/sonarr, depends on the service root — §7). The dispatcher **rejects** `push`/`log-shipping` kinds in Layer 1 (no silent target emission), so these can be authored later without a generator change beyond a new render branch + its test.

---

## 2. `module.yml` `metrics:` — reference a method BY NAME + per-class params

### 2.1 The new entry shape

A metrics entry changes from the closed `{job, via, port}` to `{method, params?}`:

```yaml
metrics:
  - method: host_node                      # references telemetry/host_node.yml; job/port come from there
  - method: pve                            # references telemetry/pve.yml
```

With params (a future snmp class — Phase 2, not the keystone commit):

```yaml
metrics:
  - method: snmp
    params: {module: if_mib}               # validated against telemetry/snmp.yml params.module.allowed
```

**Ownership shift (closes gap f):** `job` and `port` no longer live in the per-class entry — they live in the method descriptor, declared **once**. A class only says *which method* and *what allowed params*. This is the "node_exporter is ONE row, not THE pipe" property made concrete: every host-agent class is `- method: host_node`, and node_exporter's `job`/`port`/`labels`/`agent.role` are stated once in `telemetry/host_node.yml`.

### 2.2 Migration of `proxmox` / `docker_host` — clean cutover (no compat shim)

Per the implementability self-review's recommendation **(B): migrate the two files in the keystone commit; keep one code path.** Only two files declare `metrics:` today (confirmed: grep of all 8 modules; `proxmox` + `docker_host` only). A back-compat shim would linger; a clean cutover is enforceable by `--check`.

**MODIFIED `modules/proxmox/module.yml`** — replace lines 13–19 (the `metrics:` block) with:

```yaml
metrics:
  - method: host_node          # host CPU/mem/disk — node_exporter on each pve host (job node, :9100)
  - method: pve                # VM/LXC/storage — prometheus-pve-exporter queries the PVE API (read-only)
```

**MODIFIED `modules/docker_host/module.yml`** — replace lines 14–17 with:

```yaml
metrics:
  - method: host_node          # Linux boxes expose host metrics via node_exporter (job node, :9100)
```

`dashboards:` blocks are untouched (out of this section's scope; `fetch-dashboards.py` unchanged).

### 2.3 Backward-compat behaviour: **none — fail closed**

The dispatcher does **not** accept legacy `{via, port, job}` entries. An entry lacking `method` (or naming a method with no descriptor) is a **hard error**, not a silent host-branch fallthrough (which is what today's `met.get("via")` default does). This is the safety property that *replaces* the old closed-enum's accidental safety:

- `met` has no `method` key → `sys.exit("metrics entry in <key> has no `method:` (legacy {via,port} is removed — use a telemetry method name)")`.
- `met["method"]` not in the loaded registry → `sys.exit("metrics entry in <key> references unknown telemetry method '<m>' (no telemetry/<m>.yml)")`.

Because the migration commit converts the only two legacy files in the same commit, and `--check` runs in `validate.sh`, a stale legacy entry **cannot merge**.

---

## 3. The `gen-observability.py` dispatcher refactor

### 3.1 Design constraints carried from the real file

- Standalone (stdlib + `yaml` only) — keep the local `_load`, the `ROOT` computation, the `HEADER`, the LF-only write (`newline="\n"`), the `--check` set-math + orphan prune, the output path `prometheus/targets/<job>/<key>.generated.yml`. **All of this stays.**
- The only changes: (a) load the method registry; (b) replace `_entries`' `if/else` with a per-method render; (c) move `job`/`port` reads from `met` to the descriptor; (d) validate params; (e) harden `_render` against injection.

### 3.2 The refactored code (drop-in replacement for lines 47–82)

**MODIFIED `scripts/gen-observability.py`** — replace `_entries` (47–55), keep `_render` shape but harden it, and rework `metrics_files` (69–82). Also add a local `_load_methods()` and a `_METHODS` module-global, and update the module docstring's `metrics:` example.

```python
# ── method registry (standalone twin of catalog.load_telemetry_methods; no kontroll import) ──
TELEMETRY_DIR = os.path.join(ROOT, "telemetry")


def _load_methods():
    """telemetry/<name>.yml -> {name: descriptor}. Local to keep this generator import-light (CI +
    deploy-stack run it without the kontroll package on sys.path). Mirrors catalog.load_telemetry_methods;
    test_loaders_agree pins that the two stay in lockstep."""
    out = {}
    if not os.path.isdir(TELEMETRY_DIR):
        return out
    for fn in sorted(os.listdir(TELEMETRY_DIR)):
        if fn.endswith((".yml", ".yaml")):
            with open(os.path.join(TELEMETRY_DIR, fn), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
                if doc.get("name"):
                    out[doc["name"]] = doc
    return out


def _die(msg):
    sys.stderr.write("gen-observability: %s\n" % msg)
    raise SystemExit(2)


def _yaml_scalar(v):
    """Quote a label/target value safely so a metacharacter-bearing inventory/param value can never break
    out of the file_sd YAML structure (config-injection guard). We KEEP the hand-rendered LF lockfile shape
    but route every interpolated value through yaml.safe_dump for a single scalar, stripped of its trailing
    newline. Plain values (my-hypervisor, 192.0.2.10:9100, if_mib) round-trip unquoted exactly as today; only a
    value containing YAML-significant chars gets quoted."""
    s = yaml.safe_dump(v, default_flow_style=True).strip()
    # safe_dump of a bare scalar yields e.g. 'my-hypervisor\n...\n' for some inputs; collapse to the scalar form.
    if s.endswith("\n..."):
        s = s[:-4].rstrip()
    return s


def _validate_params(key, method, met):
    """Per-class params are a CLOSED allow-list per method. Any unknown key, disallowed value, or missing
    required param is a HARD fail — onboarding/module-derived params never reach a target/relabel/job
    un-validated (security: config-injection guard)."""
    schema = method.get("params") or {}
    supplied = met.get("params") or {}
    for pk, pv in supplied.items():
        if pk not in schema:
            _die("class %s, method %s: unknown param '%s'" % (key, method["name"], pk))
        if pv not in (schema[pk].get("allowed") or []):
            _die("class %s, method %s: param %s=%r not in allowed %r"
                 % (key, method["name"], pk, pv, schema[pk].get("allowed")))
    for pk, spec in schema.items():
        if spec.get("required") and pk not in supplied:
            _die("class %s, method %s: required param '%s' missing" % (key, method["name"], pk))


def _entries(method, key, group, hosts, met):
    """The (targets, labels) rows for ONE metrics entry, fanned out over a group's hosts — DISPATCHED over
    the method descriptor's `target` shape instead of the old via:host|proxy if/else. node_exporter is no
    longer special-cased: host_node is just a descriptor whose target.address==host_port."""
    tgt = method["target"]
    addr_kind = tgt["address"]
    extra_labels = {}                       # params with label_as add a label (e.g. snmp module: if_mib)
    for pk, spec in (method.get("params") or {}).items():
        if spec.get("label_as") and pk in (met.get("params") or {}):
            extra_labels[spec["label_as"]] = met["params"][pk]

    rows = []
    for name, ip in sorted(hosts.items()):
        if addr_kind == "host_port":
            target = "%s:%s" % (ip, tgt["port"])
        elif addr_kind == "device":
            target = ip
        else:
            _die("method %s: target.address %r not in {host_port, device}" % (method["name"], addr_kind))

        labels = {}
        for lk in tgt["labels"]:            # ordered, closed vocabulary {group, host} (+ label_as keys)
            if lk == "group":
                labels["group"] = group
            elif lk == "host":
                labels["host"] = name
            elif lk in extra_labels:
                labels[lk] = extra_labels[lk]
            else:
                _die("method %s: label key %r not resolvable" % (method["name"], lk))
        labels.update({k: v for k, v in extra_labels.items() if k not in labels})
        rows.append(([target], labels))
    return rows


def _render(entries):
    """Hand-render the file_sd YAML (matches the existing target style; yamllint-clean), but route every
    interpolated value through _yaml_scalar so a hostile inventory/param value cannot inject structure."""
    lines = [HEADER.rstrip("\n")]
    for targets, labels in entries:
        lines.append("- targets: [%s]" % ", ".join('"%s"' % t for t in targets))  # targets are addr/host:port
        lines.append("  labels:")
        for k, v in labels.items():
            lines.append("    %s: %s" % (k, _yaml_scalar(v)))
    return "\n".join(lines) + "\n"


def metrics_files(fleet, inv, methods=None):
    """-> {repo_rel_path: rendered_body} for every enabled module's metrics block. Dispatches each entry
    over the telemetry-method registry (telemetry/<name>.yml). Output path is keyed on the METHOD's job
    (declared once per method) + the class key, asserting (job, key) uniqueness so two entries for one class
    cannot silently overwrite each other."""
    methods = _load_methods() if methods is None else methods
    out = {}
    for key in fleet.get("enabled_modules") or []:
        mp = "modules/%s/module.yml" % key
        if not os.path.exists(os.path.join(ROOT, mp)):
            continue
        module = _load(mp)
        group = module.get("inventory_group")
        hosts = _hosts_in_group(inv, group)
        seen_paths = set()
        for met in (module.get("metrics") or []):
            mname = met.get("method")
            if not mname:
                _die("class %s: a metrics entry has no `method:` (legacy {via,port,job} is removed)" % key)
            method = methods.get(mname)
            if method is None:
                _die("class %s: metrics entry references unknown telemetry method %r "
                     "(no telemetry/%s.yml)" % (key, mname, mname))
            _validate_params(key, method, met)
            if method["kind"] not in ("host-agent", "proxy-exporter"):
                _die("class %s: telemetry method %r kind %r is not yet implemented (push/log-shipping "
                     "are reserved)" % (key, mname, method["kind"]))
            entries = _entries(method, key, group, hosts, met)
            if not entries:
                continue
            path = "prometheus/targets/%s/%s.generated.yml" % (method["job"], key)
            if path in seen_paths:
                _die("class %s: two metrics entries resolve to the same job %r — a class may not declare "
                     "two methods sharing a job" % (key, method["job"]))
            seen_paths.add(path)
            out[path] = _render(entries)
    return out
```

`main()` is **unchanged** (the `want`/`have` set-math, the `--check` diff, the orphan prune, the LF write all operate on `metrics_files()`'s output and the existing `prometheus/targets/*/*.generated.yml` glob). The dispatcher emits **only** into that existing glob in Layer 1, so the existing `--check` (validate.sh:72) covers the whole change with **zero new guard**.

### 3.3 Why this keeps the lockfile + `--check` guarantees

- **Byte-identical output:** `_yaml_scalar("my-hypervisor")` → `my-hypervisor`; `_yaml_scalar("hypervisors")` → `hypervisors`; targets are rendered by the same `'"%s"' % t` path as today. The two seed descriptors reproduce the exact target/label rows, so every committed `prometheus/targets/{node,proxmox}/*.generated.yml` regenerates unchanged.
- **`--check` unchanged:** `main(["--check"])` still diffs committed body vs regenerated and flags orphans over the same glob. The migration commit regenerates the two files (no diff) and `--check` stays green.
- **Fail-closed everywhere:** unknown method, missing method, bad param, push/log-shipping kind, duplicate job → `SystemExit`, never a wrong/empty target.

---

## 4. Secrets (proxy-exporter methods only — none in the keystone MVP)

The keystone (host_node + pve) introduces **no new secret**: `host_node` carries none; `pve` reuses the existing `proxmox` domain's read-only `*.Audit` token (C9, already shipped). **The MVP commit changes no secret, no `.sops.yaml`, no `.env` render.**

For the Phase-2 `snmp` method (and any future proxy-exporter that needs its own credential), the design mandates (per the security self-review's "major"):

- Each proxy-exporter method names its **own** `exporter.secret_domain`, e.g. `snmp_observability`, resolving to **`base_recipients`** (control + break-glass only) via the **existing `.sops.yaml` catch-all** (`.*\.sops\.ya?ml$` → `*base_recipients`, lines 56–57). **Never** co-locate it in `network`/`proxmox` (which the scoped Semaphore key can decrypt). Add an explicit `path_regex` block above the catch-all **only if** a runner must decrypt it (it must not, for a scrape-time-only exporter).
- The secret is rendered `no_log` into `docker/.env` by `deploy-stack` (mirroring `PVE_EXPORTER_*` at `deploy-stack.yml:165-167,169`) and consumed via `${VAR:-}`. **Never** inlined into a committed compose fragment or a generated target.
- **Bootstrap-report bridge (closes the grounding's secret-domain-coupling gap):** `bootstrap.yml` collects `secrets_domain` from *modules* only. A method-introduced domain is invisible. The fix (Phase 2, same commit as `snmp`): the bootstrap secrets-domain collection unions module `secrets_domain` **with** every enabled module's `metrics[].method`'s `exporter.secret_domain`. Until then, `snmp_observability` is operator-created out of band and documented in `instance/secrets/README.md`.

These are specified here so the implementer has the decision pre-made, but they land with `snmp` (Phase 2), not the keystone.

---

## 5. Tests (every file, every test name + docstring)

### 5.1 NEW `tests/unit/test_telemetry_methods.py`

Module docstring: *"The telemetry-method registry (telemetry/<name>.yml) is the drop-in that replaces gen-observability's closed via:host|proxy if/else. These tests pin that methods are auto-discovered like vectors/backends, that the standalone generator loader and the kontroll.catalog loader agree, and that the seed descriptors are well-formed — so adding a 3rd method is a pure drop-in and a malformed/missing descriptor fails loud, never silent."*

| Test name | Docstring (what it verifies + the failure it guards) |
|---|---|
| `test_load_telemetry_methods_discovers_dropins` | *"`catalog.load_telemetry_methods()` returns every `telemetry/*.yml` keyed by `name`, including the seed `host_node` and `pve`. Guards against a regression where the loader stops globbing the dir (which would make every method 'unknown' and break all observability)."* |
| `test_loaders_agree` | *"The generator's standalone `_load_methods()` and `catalog.load_telemetry_methods()` return the SAME name-set. Guards the deliberate twin-loader duplication: if the two drift, CI (which runs the standalone one) and the API/GUI (which run the catalog one) would disagree on which methods exist."* |
| `test_seed_descriptors_are_well_formed` | *"`host_node` is kind host-agent with target.address host_port + port 9100 + labels [group,host] + agent.role node_exporter; `pve` is kind proxy-exporter with target.address device + labels [host] + exporter.address pve-exporter:9221 + metrics_path /pve + secret_domain proxmox. Guards against an edit that silently changes the live scrape shape (these descriptors ARE the contract that reproduces today's targets)."* |
| `test_host_node_demotes_node_exporter_to_one_row` | *"node_exporter's job/port/agent.role live ONLY in telemetry/host_node.yml — no other telemetry/*.yml names node_exporter, and the descriptor's job is `node`. Guards the keystone property 'node_exporter is one row, not the pipe': if a future edit re-hardcodes node_exporter in the generator, the registry would no longer be its single home."* |

### 5.2 NEW `tests/unit/test_gen_observability_dispatch.py`

Module docstring: *"The gen-observability dispatcher — the refactor of the via:host|proxy if/else into a registry dispatch. These tests pin the byte-for-byte equivalence of the cutover (the live targets must NOT change), the fail-closed behaviour for unknown/legacy/bad-param/duplicate-job entries (the safety property that replaces the old closed enum), and the config-injection guard."*

| Test name | Docstring |
|---|---|
| `test_host_agent_method_renders_host_port_target` | *"A `- method: host_node` entry renders `<ansible_host>:9100` + labels {group,host} per host — identical to the old `via: host` branch. Guards that the host-agent dispatch reproduces the node_exporter target shape exactly."* |
| `test_proxy_exporter_method_renders_bare_device_target` | *"A `- method: pve` entry renders the bare `<ansible_host>` (no port) + labels {host} per host, with `pve-exporter` NOT in the file (the exporter lives in the job relabel). Guards that the proxy-exporter dispatch reproduces the old `via: proxy` shape."* |
| `test_unknown_method_fails_loud` | *"A metrics entry naming a method with no `telemetry/<name>.yml` raises SystemExit (non-zero), never emits an empty/wrong target. Guards the safety property that REPLACES the old enum's accidental safety: an unknown telemetry method must fail closed, not fall through to a host target."* |
| `test_legacy_via_entry_fails_loud` | *"A legacy `{job, via, port}` metrics entry (no `method:`) raises SystemExit. Guards the clean-cutover decision: there is no back-compat shim, so a stale legacy entry cannot silently produce a target."* |
| `test_disallowed_param_fails_loud` | *"A metrics entry passing a param value outside the method's `params.<k>.allowed` list raises SystemExit. Guards the config-injection allow-list: onboarding/module-derived params never reach a target/relabel/job un-validated."* |
| `test_param_metacharacter_is_escaped_not_injected` | *"A label value containing YAML metacharacters (e.g. a crafted host or an allow-listed-but-tricky param) is quoted by `_yaml_scalar`, never structurally injected as a new key/list. Guards the generator's hand-rendered YAML against breaking out of the file_sd structure once it is fed semi-trusted input."* |
| `test_duplicate_job_for_one_class_fails_loud` | *"A class declaring two metrics entries whose methods resolve to the same job raises SystemExit (the dict-key collision is caught, not silently overwritten). Guards gap-f's risk: moving job ownership into the method must not let two methods clobber one (job,key) path."* |

### 5.3 MODIFIED `tests/unit/test_gen_observability.py` — keep the existing four GREEN

The existing four tests are the **regression net** and must stay green after the cutover (they assert the live target bytes). Concretely:

- `test_proxmox_class_emits_both_host_and_proxy_targets` — **must pass** (the seed descriptors reproduce the example `192.0.2.10:9100` / bare `"192.0.2.10"` / `host: my-hypervisor` / `pve-exporter` absent — the committed targets derive from the PUBLIC example inventory, never the private overlay; F3). This is the proof the cutover is byte-identical.
- `test_docker_hosts_fan_out_over_every_inventory_host` — **unchanged, must pass.**
- `test_classes_without_metrics_block_emit_no_target` (asserts no fortigate/cisco/openwrt target) — **unchanged in the keystone commit, must pass** (those classes get NO metrics block in the keystone). **This test FLIPS in the Phase-2 `snmp` commit** and must be rewritten there (docstring rationale changes from *"unmonitored is honest"* to *"now monitored via snmp; honesty preserved by a residual no-metrics class/fixture"*). The keystone commit does **not** touch it.
- `test_check_mode_passes_when_committed_targets_are_fresh` — **unchanged, must pass.**

Update the module docstring of `test_gen_observability.py` to add: *"(The via:host|proxy shapes are now produced by the telemetry-method registry — telemetry/host_node.yml and telemetry/pve.yml — dispatched by gen-observability; these tests pin the live target bytes are unchanged by that refactor.)"*

All new tests carry `pytestmark = pytest.mark.unit` and are auto-collected by `validate.sh:60` (`pytest -m "not e2e and not slow"`) and pass `tests/check-test-docs.py` (every `test_*` has a docstring).

---

## 6. Phase-2 appendix (fully specified, NOT the keystone commit): proxy-exporter generation closes gap (b)

The modularity + security self-reviews are emphatic: **promoting `via→method` without moving the proxy relabel into data merely relocates the hub edit.** So the keystone commit (host_node + pve) keeps `prometheus.yml`'s hand-written `proxmox` relabel and adds NO proxy method to any module — it is honestly a host-agent-complete refactor that leaves the existing pve wiring exactly as-is. The proxy-generation half is a **separate, fully-decided** workstream:

### 6.1 Mechanism: `scrape_config_files` include (keeps prometheus.yml composition-only)

- **MODIFIED `prometheus/prometheus.yml`** (one-time, by hand): add at top level `scrape_config_files: ["/etc/prometheus/jobs.d/*.generated.yml"]` and **delete** the hand-written `proxmox` and `network` job blocks (the `node`/`prometheus` self jobs stay inline, hand-authored — they are not method-derived).
- **MODIFIED `docker/services/prometheus.yaml`**: add a `:ro` mount `../../prometheus/jobs.d:/etc/prometheus/jobs.d`.
- **NEW `scripts/gen-prometheus-jobs.py`**: a sibling generator (same standalone shape, same `HEADER`/LF/`--check` set-math copied verbatim from `gen-observability.py:main`) that, for each enabled module's metrics entry whose method is `kind: proxy-exporter`, emits `prometheus/jobs.d/<job>.generated.yml` containing the job from the descriptor: `metrics_path`, `file_sd_configs` glob `targets/<job>/*.yml`, and the relabel built from `exporter.address` + `exporter.param_target` (and `?module=` from a `label_as` param for snmp). One job file per distinct `job` across all proxy methods (deduped; assert identical descriptor when two classes share a job).
- **MODIFIED `tests/validate.sh`**: add a step beside line 72: `if [ -n "$PY" ]; then step gen-prometheus-jobs 0 "$PY" scripts/gen-prometheus-jobs.py --check; fi`. **This is the load-bearing new `--check`** — without it, `prometheus.yml`'s generated jobs can drift silently.
- **MODIFIED `ansible/playbooks/deploy-stack.yml`**: add a second `command:` task beside the existing `gen-observability.py` invocation (deploy-stack.yml:171-179), `become: false`, `changed_when: false`, `when: "'prometheus' in stack_services"`, running `gen-prometheus-jobs.py`.

### 6.2 The relabel, now generated from `telemetry/pve.yml`

`gen-prometheus-jobs.py` emits, for the `proxmox` job, exactly today's relabel (`prometheus.yml:18-24`) — but **derived** from `pve`'s `exporter.address: pve-exporter:9221` + `metrics_path: /pve` + `param_target: true`. The Phase-2 acceptance criterion: `promtool check config` passes and the *effective* scrape config is unchanged from today's hand-written one (a `test_gen_prometheus_jobs.py` pins the rendered relabel matches the descriptor).

This is where adding `snmp` to `cisco_ios` becomes a **pure drop-in**: `telemetry/snmp.yml` (already designed, §1.5) + `- {method: snmp, params: {module: if_mib}}` in `modules/cisco_ios/module.yml` + the `snmp_observability` SOPS domain — touching **zero** hub file (no `gen-observability.py` edit, no `prometheus.yml` scrape_configs edit). That commit also rewrites `test_classes_without_metrics_block_emit_no_target` (§5.3) and adds the C-control below.

---

## 7. Reserved kinds & SSRF posture (decisions pre-made for later phases)

- **`kind: push` (otel) / `kind: log-shipping` (loki):** the dispatcher rejects them in Layer 1 (no Prometheus file_sd target makes sense). When built: `push` no-ops the target file (the collector pushes to a receiver); `loki` provisions a Grafana **datasource** (a new `dashboards/grafana/provisioning/datasources/loki.yml`, its own uid) — not a scrape target. These need a new render branch + test, not a registry-only change. Listed as reserved rows so the schema's `kind` enum is honestly open.
- **`blackbox`/`snmp` SSRF (security self-review "major"):** every proxy-exporter compose fragment publishes **no host port** (kontroll net only — like `pve-exporter.yaml`, which has no `ports:`). The Phase-2 commit adds a `validate.sh`/pytest covering check: **assert no `docker/services/*exporter*.yaml` declares a host `ports:` mapping** (the C3 "never 0.0.0.0" rule, extended to exporters). The `?target=` set is bounded **structurally** to the generated `targets/<job>/*.generated.yml` addresses (inventory `ansible_host` values), never a free-form field — so a via:proxy method cannot become an internal port-scanner.
- **`exportarr` / the service root (radarr):** out of scope entirely — depends on a collection-less onboard path that does not exist (implementability blocker). Named only as a future `kind: proxy-exporter` row.

---

## 8. Acceptance criteria

**Keystone commit (Layer 1 — host_node + pve + dispatcher):**

1. `git diff prometheus/targets/` is **EMPTY** after running `python3 scripts/gen-observability.py` (the cutover is byte-identical; no live scrape change).
2. `python3 scripts/gen-observability.py --check` returns 0; `bash tests/validate.sh` is green (`promtool`, `pytest`, `check-test-docs`, `gen-observability --check` all pass).
3. `telemetry/host_node.yml` and `telemetry/pve.yml` exist; **no `telemetry/*.yml` other than `host_node` references the `node_exporter` role** — node_exporter is one descriptor row.
4. `modules/proxmox/module.yml` and `modules/docker_host/module.yml` use `- method: …`; **no `module.yml` contains `via:` or a per-class `port:`/`job:` in a metrics entry** (the ownership moved to the descriptor).
5. `scripts/gen-observability.py` contains **no `if met.get("via")`** branch — the keystone hub-edit `if/else` is gone, replaced by the registry dispatch.
6. Feeding the dispatcher an unknown method, a legacy `{via,port}` entry, a disallowed param, or a duplicate-job class each **exits non-zero** (fail-closed) — pinned by the new tests.
7. The four existing `test_gen_observability.py` tests pass unchanged.
8. Adding a hypothetical 3rd host-agent method (e.g. a `windows_exporter` descriptor) would require **zero** edit to `gen-observability.py` — provable by a test that loads a synthetic descriptor and dispatches it.

**Phase-2 commit (proxy generation + snmp — separate, specified in §6):** `prometheus.yml` is composition-only (a `scrape_config_files` include); `gen-prometheus-jobs.py --check` is a `validate.sh` step; adding `snmp` to `cisco_ios` touches no hub file; the no-metrics test is rewritten with a residual honesty case; the SNMP SOPS domain + C-control land in that commit.

---

## 9. Logging / audit lines

Layer 1 adds **no actuation** and therefore **no audit line** (the API/GUI flow is deferred). The generators run inside existing run-id-stamped contexts:

- **`deploy-stack`** already imports `_log-run-id.yml` first (emitting `kontroll_run_id=<UTC ts>`), and the `gen-observability` task runs under it. The Phase-2 `gen-prometheus-jobs` task runs in the same play under the same `kontroll_run_id` — no new wiring.
- The generators print their existing run lines (`wrote N target file(s): …` / `STALE observability targets …`), which are **structurally non-secret** (paths + counts only — never a token). `gen-prometheus-jobs.py` prints the analogous `wrote N job file(s): …`. **No credential is ever in generator output** (the exporter secret lives in `.env`, rendered by deploy-stack `no_log`, never read by these generators).

When the GUI/API observability opt-in is eventually built (Layer 2, deferred), it reuses the existing `audit_action(request, principal, "observe-propose", detail="class=<key> method=<m> dashboard=<gnet>")` pattern (`api/auth.py:54`), auditing the **method name and var-NAMES only**, never the SNMP community / API key — with a cred-exclusion test mirroring `tests/integration/test_gui_api.py:162`. That is out of this section's scope and named here only for continuity.

---

## 10. SECURITY.md control text

**Keystone commit:** no control change. host_node is already covered by C9 ("Host agent (node_exporter): additive, read-only, VLAN-scoped"); pve by C9 ("Metrics stack: mgmt-bound, read-only, no new secret"). The refactor is a generator-internal change with no new secret, no new trust boundary, no new exposure — **C9 stays accurate as written.** (Triggers checked against SECURITY.md:240-249: no new credential/domain, no new device class, no new privileged surface → no SECURITY.md edit required in the keystone commit.)

**Phase-2 commit (snmp) — add this C9 amendment bullet** (after the node_exporter bullet at SECURITY.md:208-214):

> - **Agent-less exporters (snmp_exporter): additive, read-only, scoped-domain, no host port.** The `snmp` telemetry method runs `snmp_exporter` as a control-node proxy exporter (`snmp-exporter:9116`, **no published host port** — scraped only over the internal `kontroll` network, C3) that polls SSH/SNMP-only gear (the Cisco core switch, FortiGate edge) read-only. Its SNMP **community string** lives in a dedicated `snmp_observability` SOPS domain encrypted to **control + break-glass only** (the `.sops.yaml` catch-all `*base_recipients` — **never** the scoped Semaphore key, so a Semaphore breach cannot read it), rendered `no_log` into `.env` by `deploy-stack` and consumed via `${VAR:-}`. The `?target=` set is bounded to the **generated** inventory addresses, never caller-supplied (no SSRF surface off the kontroll net). A leaked community string yields read-only device counters, never actuation. **Implements:** `telemetry/snmp.yml` + `instance/secrets/snmp_observability.sops.yml` + `docker/services/snmp-exporter.yaml` (no `ports:`). **Proves:** `gen-prometheus-jobs.py --check` (the relabel is generated, not hand-edited) + the `no-host-port` validate check + `tests/unit/test_gen_observability_dispatch.py::test_disallowed_param_fails_loud`.

Also (Phase-2) the SECURITY.md "Update this document when… a new credential field or secret domain is added" trigger fires → this amendment is the response, in the same commit as the `snmp_observability` domain.

---

## 11. Documentation-Sync rows (per CLAUDE.md checklist)

**Keystone commit (must land in the SAME commit):**

| Doc | Edit |
|---|---|
| `modules/README.md` | Rewrite the `metrics` schema row (line 33): `{job, via, port}` → `{method, params?}` referencing `telemetry/<name>.yml`; note `via:` is removed and job/port now live in the method descriptor. Add a one-line "See `telemetry/README.md` for the method registry." |
| **NEW `telemetry/README.md`** | The registry's own README: the descriptor schema (§1.4 table), the two seed methods, the "add a method = drop a `telemetry/<name>.yml`" test, the reserved kinds, and reciprocal See-also links to `modules/README.md`, `prometheus/README.md`, `scripts/gen-observability.py`. |
| `prometheus/README.md` | In "Architecture" / "Adding a scrape target", note targets are now generated by **dispatch over `telemetry/<method>.yml`** (the method owns the job); add a forward-reference to the Phase-2 `jobs.d/` generated jobs (kept as a TODO line until Phase 2). |
| `scripts/gen-observability.py` | Update the module docstring's `metrics:` example from `{job, via, port}` to `{method: host_node}` / `{method: pve}` and describe the registry dispatch (the docstring is currently a lie after the refactor). |
| `CHANGELOG.md` `[Unreleased]` | "refactor(observability): promote the `via:host\|proxy` enum into a `telemetry/<method>.yml` drop-in registry; gen-observability dispatches over it; node_exporter is one descriptor row, not the generator's default branch; clean cutover of proxmox/docker_host (targets byte-identical, `--check`-enforced)." |
| `tests/README.md` | Add the two new unit files to the inventory; add a row for the telemetry-method registry seam if the mock-seam table lists registries (it lists `kontroll.catalog.*` — note `load_telemetry_methods` joins them). |
| `docs/capability-matrix.md` | §2 names `telemetry` as the example 4th vector — add a one-line note distinguishing the **method registry** (this section, the generator's input) from the future **telemetry vector** (the detector), so the doc doesn't conflate them. |

**Phase-2 commit (with snmp):** `modules/cisco_ios/module.yml` (+ the metrics block), `instance/secrets/README.md` (the `snmp_observability` row), `.sops.yaml` (only if an explicit block is needed; the catch-all covers it — document the decision in the secrets README), `SECURITY.md` (§10 amendment), `prometheus/README.md` (the `jobs.d/` mechanism, remove the TODO), `tests/validate.sh` (the `gen-prometheus-jobs --check` step), `CHANGELOG.md`, and the rewrite of `test_classes_without_metrics_block_emit_no_target`'s docstring.

---

### Files created / modified — quick index

**NEW (keystone):** `telemetry/host_node.yml`, `telemetry/pve.yml`, `telemetry/README.md`, `tests/unit/test_telemetry_methods.py`, `tests/unit/test_gen_observability_dispatch.py`.
**MODIFIED (keystone):** `scripts/kontroll/paths.py` (`TELEMETRY_DIR`), `scripts/kontroll/catalog.py` (`load_telemetry_methods`), `scripts/gen-observability.py` (dispatcher + `_load_methods` + `_validate_params` + hardened `_render` + `metrics_files`), `modules/proxmox/module.yml`, `modules/docker_host/module.yml`, `modules/README.md`, `prometheus/README.md`, `tests/unit/test_gen_observability.py` (docstring only), `tests/README.md`, `docs/capability-matrix.md`, `CHANGELOG.md`.
**NEW (Phase-2, specified not built here):** `telemetry/snmp.yml`, `scripts/gen-prometheus-jobs.py`, `docker/services/snmp-exporter.yaml`, `instance/secrets/snmp_observability.sops.yml`, `tests/unit/test_gen_prometheus_jobs.py`.
**MODIFIED (Phase-2):** `prometheus/prometheus.yml` (`scrape_config_files` include), `docker/services/prometheus.yaml` (jobs.d mount), `ansible/playbooks/deploy-stack.yml` (second gen task), `tests/validate.sh` (new `--check` step), `modules/cisco_ios/module.yml`, `SECURITY.md`, `instance/secrets/README.md`, the no-metrics test rewrite.

The keystone removes the `gen-observability.py:51` `if/else` (the documented gap-a hub edit) with **zero new actuation, zero new network I/O, zero new privileged surface, zero new secret, and a byte-identical live scrape set** — exactly the smallest landable slice the three self-reviews converged on, with node_exporter provably demoted to one registry row.
