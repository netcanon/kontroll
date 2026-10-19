# Agent-less monitoring & exporter provisioning

> **DESIGN HISTORY — not a description of the tree.** This is the design record of agent-less monitoring and exporter provisioning, written in June 2026. It was built; [telemetry/README.md](../../telemetry/README.md) and [prometheus/README.md](../../prometheus/README.md) describe what exists. Where it disagrees with the code or with `SECURITY.md`, they are right.

> **Part of the [Option-A observability + onboarding flow](../observability-onboarding-flow.md) design (baseline HEAD `63de129`).**
>
> ⚠️ **Binding contracts (the catalog loader name, the flat `telemetry/<method>.yml` schema, the single clean-cutover dispatcher, the `network` job-vs-inventory-group distinction, the `.env` secret-render mechanism) are pinned in the master design's §4.0 SHARED CONTRACTS.** Where this section names a symbol, schema, or sequencing differently, **§4.0 wins** and this section is reconciled to it before implementation. See the master design's §8 “Resolved conflicts” for the per-section reconciliation list.

> **✅ Phase 2a + 2b + 3a SHIPPED** — proxy-exporter **job** generation (`gen-prometheus-jobs.py` → `jobs.d/`, `prometheus.yml` composition-only via `scrape_config_files`; **gap-b**), exporter **container** generation (`gen-exporters.py`; **gap-b'**), AND **agent-less SNMP for the Cisco core switch** (`telemetry/snmp.yml` + the inline `network` job deleted/generated + `modules/cisco_ios` metrics block + the `snmp_observability` SOPS domain + the config-FILE secret-render (`snmp.yml.j2` → gitignored 0600 `snmp.yml`, `no_log`) + the per-class `module` allow-list injection guard + the `snmp-exporter` container; **gap-d closed for the switch**), with the flipped honesty test + injection test + `--check` guards. Still design here: the **FortiGate + blackbox** methods (Phase 3b) and the env-based `.env` secret-render loop (§4.0.6 — exercised by exportarr in Phase 5; snmp uses a config-file render, not `.env`). Per §4.0.1 the loader is `catalog.load_telemetry()` (this section's `load_telemetry_methods()` is superseded).

This design closes **gap (a)** (the closed `via` enum), **gap (b)** (the hand-written proxy-exporter relabel in `prometheus.yml`), **gap (b')** (the hand-listed exporter compose fragment + `.env` keys), and **gap (d)** (cisco_ios/fortigate unmonitored) for the **proxy-exporter** half of the telemetry-method registry. It assumes the host-agent half (`telemetry/host_node.yml`, the `_entries` dispatcher refactor, `paths.TELEMETRY_DIR`, `catalog.load_telemetry_methods()`) is being designed by the sibling keystone dimension; this section **builds on it** and is explicit about the one shared contract it depends on (the registry loader + the dispatcher signature). Everything here is **CLI/generator + Ansible/Docker** only — **no GUI, no API, no new trust boundary** — per the self-review's blocker consensus.

# Proxy-exporter telemetry methods & generated exporter provisioning

## 0. Scope, phasing, and the shared contract

**This dimension owns:** the `kind: proxy-exporter` telemetry methods (`pve`, `snmp`, `blackbox`, and the generic proxy pattern), and — the load-bearing part — making the **exporter container, its config file, its Prometheus job, and its `relabel_configs` GENERATED from the method descriptor** so `prometheus.yml` stops being hand-edited per exporter. It also delivers the first agent-less devices (`cisco_ios`, `fortigate`) and their SOPS-domain'd, `no_log` exporter secrets.

**Out of scope (deferred, per self-review consensus):** the `vectors/telemetry.yml` capability vector; the OpenAPI service root / `exportarr` for radarr; the GUI/API observability opt-in and its privileged route; the live grafana.com dashboard search. `exportarr` is sketched only as a *future descriptor row* to prove the pattern generalizes.

**The shared contract this dimension depends on** (owned by the keystone/host-agent dimension, restated so this section is self-contained):

1. `scripts/kontroll/paths.py` gains `TELEMETRY_DIR = os.path.join(ROOT, "telemetry")`.
2. `scripts/kontroll/catalog.py` gains `load_telemetry_methods()` mirroring `load_backends()` exactly — globs `telemetry/*.yml` sorted by `order`, returns a list of descriptor dicts. **Note:** `gen-observability.py` is standalone (yaml + stdlib, no `kontroll` import — confirmed at `scripts/gen-observability.py:23-27`), so it cannot `import kontroll.catalog`. It therefore carries its **own** `_load_methods()` reader (below) that reads `telemetry/*.yml` directly; `catalog.load_telemetry_methods()` exists for the service layer / future API. Both read the same `telemetry/` dir — one schema, two import-light readers. This duplication is the price of the standalone-generator convention and is acceptable (the two readers are ~6 lines each).
3. `gen-observability.py:_entries` (lines 47-55) becomes a **dispatcher** keyed on `met["method"]` into the loaded method registry, with a legacy shim mapping `{via: host, port, job}` → `host_node` and `{via: proxy, job: proxmox}` → `pve` so existing `module.yml` files need not change in the keystone commit. **This section specifies the proxy-exporter branch of that dispatcher and the second generator it requires.**

This section is **Phase 3** in the self-review's phase graph: it lands **after** the host-agent keystone (Phase 1) and is itself split into three independently-landable commits (§9).

---

## 1. The telemetry-method descriptor schema (proxy-exporter kind)

A telemetry method is a drop-in `telemetry/<name>.yml`, peer to `vectors/*.yml` and `ansible/backends/*/backend.yml`, **operator-owned** (never written by onboarding input — §8 security). It is pure data the *generator* consumes (like `backend.yml`'s `connection`/`defaults`, which `scripts/kontroll/catalog.py` confirms are generator-data, not classifier-data).

### 1.1 Full schema (proxy-exporter)

```yaml
# telemetry/<name>.yml — a proxy-exporter telemetry method.
name: <str>            # unique; equals the filename stem; referenced by module.yml metrics[].method
label: <str>          # human label (docs / future GUI)
order: <int>          # load/sort order (mirrors backend.yml/vector order); proxy methods 20+
kind: proxy-exporter  # one of: host-agent | proxy-exporter   (push/loki NOT modeled yet — §0)

# --- Prometheus job: GENERATED into prometheus/jobs.d/<job>.generated.yml (§3) ---
job: <str>            # job_name; ALSO the targets subdir: prometheus/targets/<job>/<key>.generated.yml
metrics_path: <str>   # e.g. /pve, /snmp, /probe   (the exporter's scrape path)

# --- The proxy binding (what makes via:proxy actually scrape) — was hand-written in prometheus.yml ---
exporter_address: <str>   # host:port the relabel rewrites __address__ to, e.g. pve-exporter:9221
                          # (a docker service name on the kontroll net; never an IP, never a host port)
target_param: <str|null>  # the ?<name>=<device> query param the exporter reads to know which device to
                          # query. null => no per-target param (single-instance exporter).
                          # pve: "target"; snmp: "target"; blackbox: "target"
extra_params: {}          # OPTIONAL static query params merged into every scrape, e.g. snmp passes the
                          # auth/module. Rendered as __param_<k>=<v> relabels. Values come from the
                          # DESCRIPTOR or a CLOSED per-class params allow-list (§1.3) — never free input.

# --- Target shape: how _entries renders each host's row in the targets file ---
target_shape: device      # device = bare <ansible_host> (no port); the relabel proxies it.
                          # (host-agent kind uses host_port — owned by the keystone dimension)
labels: [host]            # which inventory-derived labels each target row carries ({host} for proxy;
                          # host-agent uses [group, host]). Byte-compat: pve MUST stay [host].

# --- The exporter CONTAINER: GENERATED into docker/services/<name>.generated.yaml (§4) ---
exporter:
  image: <str>                    # pinned-ish image ref (matches pve-exporter.yaml ':latest' convention)
  container_name: <str>           # equals `name` by convention (pve-exporter, snmp-exporter, …)
  listen_port: <int>              # the port inside exporter_address (9221/9116/9115); NO host port published
  config_mount: <str|null>        # repo path mounted :ro at the exporter's config path, or null if envless.
                                  # e.g. prometheus/exporters/snmp/snmp.yml -> /etc/snmp_exporter/snmp.yml
  config_container_path: <str|null>
  env: {}                         # VAR -> ${ENV_KEY:-} blank-tolerant env (pve: PVE_USER etc.). The .env
                                  # keys are rendered by deploy-stack from secret_domain (§5).
  access_chain:                   # the 4-line header block stamped into the generated compose fragment (§4)
    chain: <str>
    may_break: <str>
    fallback: <str>
    blast_radius: <str>

# --- Secrets ---
secret_domain: <str|null>   # instance/secrets/<domain>.sops.yml whose keys feed exporter.env (§5).
                            # null = no secret (blackbox needs none). Resolves to base_recipients via the
                            # .sops.yaml catch-all unless an explicit ops block is added (§5/§8).
secret_env_map: {}          # {.env-key: sops-key}  e.g. SNMP_EXPORTER_... not needed if envless. The
                            # render block deploy-stack appends is DERIVED from this (§5).

# --- TLS verification posture (per-method, per-VLAN deliberate choice — §8) ---
verify_tls: <bool>          # default for cross-VLAN safety; pve sets false (mgmt self-signed, C3).

# --- Curated default dashboard(s) by method (the existing fetch path; NO live search) ---
default_dashboards: [{gnet: <int>, name: <str>}]   # OPTIONAL; a class may still override in module.yml
```

### 1.2 Field semantics — why each exists

| Field | Why it must be data (not code) |
|---|---|
| `job`, `metrics_path` | Was the hard-written `job_name: proxmox` + `metrics_path: /pve` in `prometheus.yml:14-15`. Owning it here means a 2nd/3rd proxy job is a drop-in, not a hub edit (closes gap a/f). |
| `exporter_address`, `target_param`, `extra_params` | **This is gap (b).** The `__address__ -> pve-exporter:9221` + `__param_target` relabel (`prometheus.yml:18-24`) was the *entire* "what makes via:proxy scrape" contract, hand-written. Moving it here lets the relabel be **generated** (§3). |
| `exporter.*` | **This is gap (b').** The container (`pve-exporter.yaml`) and its `.env` keys (hand-listed at `deploy-stack.yml:165-167` and `docker/.env.example:19-21`) become generated from one source. |
| `secret_domain`, `secret_env_map` | The exporter's read-only credential flows via a named SOPS domain, `no_log`. snmp/blackbox introduce *new* secret material (gap: PVE reused an existing token; SNMP communities don't exist yet). |
| `verify_tls` | Stops the `PVE_VERIFY_SSL: false` copy-paste from silently disabling TLS toward the non-mgmt VLANs (self-review security minor). |

### 1.3 Per-class params: a CLOSED allow-list (config-injection guardrail)

A `module.yml` metrics entry may pass `params:` to a method (e.g. `{method: snmp, params: {module: cisco_wan}}`). Because params *can* flow from onboarding input in a later phase, **each method declares the closed set of param keys and their allowed values** in a sibling block; the generator **hard-fails** on an unknown key/value (never interpolates it). For Phase 3 (operator-authored module.yml only) this is still mandatory — it is the regression net for the GUI phase that comes later.

```yaml
# in telemetry/snmp.yml — the closed params contract
params_schema:
  module:                       # which snmp_exporter module (a section in generator.yml/snmp.yml)
    required: true
    enum: [if_mib, cisco_wan, fortigate]   # MUST match a module defined in prometheus/exporters/snmp/snmp.yml
```

The generator validates `params` against `params_schema` and `sys.exit(2)` with a clear message on violation. Values are emitted through `yaml.safe_dump` (§3.3), never `%`-interpolation.

---

## 2. The three seed proxy-exporter descriptors

### 2.1 `telemetry/pve.yml` (NEW) — re-expresses the LIVE pve-exporter byte-for-byte

This descriptor must reproduce `prometheus.yml:14-24` + `docker/services/pve-exporter.yaml` exactly so the migration is a pure data-lift (acceptance §10: `git diff prometheus/targets/` empty; `promtool` still green).

```yaml
# telemetry/pve.yml — Proxmox VM/LXC/storage/cluster metrics via prometheus-pve-exporter (read-only token).
name: pve
label: Proxmox PVE API exporter (read-only)
order: 20
kind: proxy-exporter
job: proxmox                     # MUST stay 'proxmox' (existing targets/proxmox/*.generated.yml + job)
metrics_path: /pve
exporter_address: pve-exporter:9221
target_param: target
extra_params: {}
target_shape: device
labels: [host]                   # MUST stay [host] — test_proxmox asserts {host} only on the proxy target
exporter:
  image: prompve/prometheus-pve-exporter:latest
  container_name: pve-exporter
  listen_port: 9221
  config_mount: null
  config_container_path: null
  env:
    PVE_USER: ${PVE_EXPORTER_USER:-}
    PVE_TOKEN_NAME: ${PVE_EXPORTER_TOKEN_NAME:-}
    PVE_TOKEN_VALUE: ${PVE_EXPORTER_TOKEN_VALUE:-}
    PVE_VERIFY_SSL: "false"
  access_chain:
    chain: control VM (kontroll net) -> https://<pve host>:8006 with the read-only *.Audit API token
    may_break: nothing — a read-only scrape; a token/SSL failure degrades a Grafana panel, not the lab
    fallback: "no"
    blast_radius: control node only (queries the PVE API read-only; actuates NOTHING on the cluster)
secret_domain: proxmox           # reuses the EXISTING read-only token (no new secret)
secret_env_map:
  PVE_EXPORTER_USER: proxmox_api_user
  PVE_EXPORTER_TOKEN_NAME: proxmox_api_token_id
  PVE_EXPORTER_TOKEN_VALUE: proxmox_api_token_secret
verify_tls: false                # mgmt VLAN self-signed (C3 accepted posture)
default_dashboards:
  - {gnet: 10347, name: proxmox}
```

### 2.2 `telemetry/snmp.yml` (NEW) — the first agent-less method (cisco_ios + fortigate)

```yaml
# telemetry/snmp.yml — agent-less SNMP metrics via prometheus/snmp_exporter (multi-target ?target=&module=).
name: snmp
label: SNMP exporter (agent-less; switches, firewalls, appliances)
order: 21
kind: proxy-exporter
job: network                     # the EXISTING (empty) 'network' job — closes gap d into its honest bucket
metrics_path: /snmp
exporter_address: snmp-exporter:9116
target_param: target
extra_params: {}                 # the per-class `module` becomes a __param_module relabel via params (§3.2)
target_shape: device
labels: [host]
exporter:
  image: prom/snmp-exporter:latest
  container_name: snmp-exporter
  listen_port: 9116
  config_mount: prometheus/exporters/snmp/snmp.yml          # the generated MIB->module config (§6)
  config_container_path: /etc/snmp_exporter/snmp.yml
  env:
    # snmp_exporter reads communities/auth from its config file, NOT env. The community is templated
    # INTO snmp.yml at deploy time from SOPS (§6.2), so no scrape-time env secret here.
    {}
  access_chain:
    chain: control VM (kontroll net) -> snmp-exporter -> SNMPv2c/v3 GET to <device>:161 on the mgmt VLAN
    may_break: nothing — a read-only SNMP poll; a community/timeout failure degrades a panel, not the device
    fallback: "no"
    blast_radius: control node only (read-only SNMP GET; the core switch / edge firewall are NOT actuated)
secret_domain: snmp_observability   # NEW base-recipients-only domain (control+break-glass; NOT Semaphore)
secret_env_map: {}                  # community is rendered into snmp.yml, not .env (§6.2)
verify_tls: true                    # SNMP has no TLS; field documents that no cert-skip is inherited
default_dashboards:
  - {gnet: 14857, name: snmp_network}   # "SNMP Exporter / Network Interfaces" — curated default
params_schema:
  module:
    required: true
    enum: [if_mib, cisco_wan, fortigate]
```

### 2.3 `telemetry/blackbox.yml` (NEW, declared; cisco/fortigate reachability) — SSRF-bounded

Declared in the same commit as `snmp` to prove a *second* proxy method is a pure drop-in, and to give reachability monitoring to the high-blast-radius edge/core. **No secret** (`secret_domain: null`).

```yaml
# telemetry/blackbox.yml — agent-less reachability/latency via prometheus/blackbox_exporter (icmp/tcp/http).
name: blackbox
label: Blackbox exporter (reachability — icmp/tcp/http probes)
order: 22
kind: proxy-exporter
job: network
metrics_path: /probe
exporter_address: blackbox-exporter:9115
target_param: target
extra_params: {}                 # the per-class `probe` module -> __param_module relabel (§3.2)
target_shape: device
labels: [host]
exporter:
  image: prom/blackbox-exporter:latest
  container_name: blackbox-exporter
  listen_port: 9115
  config_mount: prometheus/exporters/blackbox/blackbox.yml   # static, hand-authored module list (§6.3)
  config_container_path: /etc/blackbox_exporter/config.yml
  env: {}
  access_chain:
    chain: control VM (kontroll net) -> blackbox-exporter -> ICMP/TCP probe of <device> on mgmt/server VLANs
    may_break: nothing — a read-only probe; the target set is GENERATED from inventory (no caller ?target=)
    fallback: "no"
    blast_radius: control node only (the probe target list is bounded to generated inventory addresses — SSRF-safe)
secret_domain: null
secret_env_map: {}
verify_tls: true
default_dashboards:
  - {gnet: 7587, name: blackbox}
params_schema:
  probe:
    required: true
    enum: [icmp, tcp_connect, http_2xx]
```

> **`exportarr` (future row, NOT shipped here):** `telemetry/exportarr.yml` would be `kind: proxy-exporter`, `job: service`, `exporter_address: exportarr-radarr:9707`, `target_param: null` (single-instance per app), `secret_domain: dashboards` reusing `HOMEPAGE_VAR_RADARR_KEY`. It is the proof that the registry is root-agnostic; it lands with the OpenAPI service-root phase, not here.

---

## 3. Generating the Prometheus job + relabel (closes gap b) — the new `gen-prometheus-jobs.py`

`prometheus.yml` stays **composition-only** (like `compose.yaml`'s `include:` list). The per-method jobs+relabels are generated into a new include dir and pulled in via Prometheus's `scrape_config_files` directive.

### 3.1 The one structural edit to `prometheus.yml` (MODIFIED, once, by hand)

Add the include directive and **remove** the hand-written `proxmox`, `network` jobs (they become generated). Keep `prometheus` (self) static.

```yaml
# prometheus/prometheus.yml — AFTER (composition-only; per-exporter jobs are generated, see jobs.d/)
global:
  scrape_interval: 30s
  evaluation_interval: 30s

# Per-proxy-exporter jobs (job_name + metrics_path + relabel_configs) are GENERATED from the telemetry
# method descriptors into jobs.d/<job>.generated.yml by scripts/gen-prometheus-jobs.py. Add a proxy
# exporter = a telemetry/<name>.yml drop-in, never an edit here (modularity doctrine §2.5).
scrape_config_files:
  - /etc/prometheus/jobs.d/*.generated.yml

scrape_configs:
  - job_name: node            # host metrics (host-agent kind: node_exporter; host:port targets)
    file_sd_configs:
      - files: ["/etc/prometheus/targets/node/*.yml"]

  - job_name: prometheus      # self
    static_configs:
      - targets: ["localhost:9090"]
```

> **`node` job note:** the host-agent (`node`) job has no relabel, so it can stay inline OR be generated too. To keep this dimension's blast radius minimal, **leave `node` inline** — only `proxmox`/`network` (the proxy jobs) move to `jobs.d/`. The keystone dimension may later generate `node` too; that is additive.

The new mount in `docker/services/prometheus.yaml` (MODIFIED):

```yaml
    volumes:
      - ../../prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ../../prometheus/targets:/etc/prometheus/targets:ro
      - ../../prometheus/jobs.d:/etc/prometheus/jobs.d:ro            # NEW — generated per-exporter jobs
      - ../../prometheus/exporters:/etc/prometheus/exporters:ro      # NEW — generated/static exporter configs
      - prometheus-data:/prometheus
```

### 3.2 The relabel template, derived from the descriptor

For a proxy method, `gen-prometheus-jobs.py` emits a job whose relabel chain is built mechanically from `exporter_address`, `target_param`, and any `params` keys promoted to `__param_<k>`:

- `__address__` (the device addr from the targets file) → `__param_<target_param>` (e.g. `?target=<device>`)
- `__param_<target_param>` → `instance` (label series by the queried device)
- for each param key the class supplies (e.g. `module`) → a static `__param_<k>` relabel **per generated job-variant** (see §3.4 — params force a job split)
- `__address__` → `replacement: <exporter_address>` (always scrape the exporter)

### 3.3 `scripts/gen-prometheus-jobs.py` (NEW) — full generator sketch

Standalone (yaml + stdlib), mirrors `gen-observability.py`'s conventions: `GENERATED` header, `.generated.yml` naming, LF-only, `--check` set-math, orphan prune. **Uses `yaml.safe_dump` for the relabel body** (the config-injection guardrail — descriptor + closed params only, but structurally safe regardless).

```python
#!/usr/bin/env python3
"""Generate Prometheus scrape JOBS (job_name + metrics_path + relabel_configs) for every proxy-exporter
telemetry method, from telemetry/<name>.yml. The relabel that makes via:proxy actually scrape — was
hand-written in prometheus.yml — now lives as DATA in the method descriptor. Output:
prometheus/jobs.d/<job>.generated.yml, pulled in by prometheus.yml's scrape_config_files. Standalone
(yaml + stdlib) so CI + deploy-stack run it; a GENERATED lockfile kept honest by --check (tests/validate).

Usage:  python3 scripts/gen-prometheus-jobs.py [--check]
"""
import glob, os, sys, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEMETRY = os.path.join(ROOT, "telemetry")
OUT_DIR = os.path.join(ROOT, "prometheus", "jobs.d")
HEADER = ("# GENERATED by scripts/gen-prometheus-jobs.py from telemetry/<name>.yml (proxy-exporter methods).\n"
          "# Do NOT edit by hand — change a telemetry method descriptor and re-run (deploy-stack does).\n")


def _load_methods():
    """telemetry/*.yml -> list of descriptor dicts (proxy-exporter kind only here)."""
    out = []
    if not os.path.isdir(TELEMETRY):
        return out
    for fn in sorted(os.listdir(TELEMETRY)):
        if fn.endswith((".yml", ".yaml")):
            with open(os.path.join(TELEMETRY, fn), encoding="utf-8") as fh:
                d = yaml.safe_load(fh) or {}
                if d.get("kind") == "proxy-exporter":
                    out.append(d)
    return out


def _job_doc(method):
    """One scrape_config dict for a proxy-exporter method (the safe_dump'd YAML the file holds)."""
    relabels = []
    tp = method.get("target_param")
    if tp:
        relabels.append({"source_labels": ["__address__"], "target_label": "__param_%s" % tp})
        relabels.append({"source_labels": ["__param_%s" % tp], "target_label": "instance"})
    for k, v in (method.get("extra_params") or {}).items():     # static, descriptor-owned params
        relabels.append({"target_label": "__param_%s" % k, "replacement": str(v)})
    relabels.append({"target_label": "__address__", "replacement": method["exporter_address"]})
    job = {
        "job_name": method["job"],
        "metrics_path": method["metrics_path"],
        "file_sd_configs": [{"files": ["/etc/prometheus/targets/%s/*.yml" % method["job"]]}],
        "relabel_configs": relabels,
    }
    return job


def jobs_files():
    """-> {repo_rel_path: rendered_body}. One file per job; multiple methods may share a job (network)."""
    by_job = {}
    for m in _load_methods():
        by_job.setdefault(m["job"], []).append(m)
    out = {}
    for job, methods in by_job.items():
        # A job dir is globbed by ONE file_sd; the relabel must be identical across methods sharing a job,
        # OR the per-class `module` param must be carried on the TARGET (see §3.4) so one job serves all.
        doc = _job_doc(methods[0])                       # representative; §3.4 guarantees relabel-compat
        body = HEADER + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
        out["prometheus/jobs.d/%s.generated.yml" % job] = body
    return out


def main(argv):
    want = {os.path.normpath(os.path.join(ROOT, rel)): body for rel, body in jobs_files().items()}
    have = {os.path.normpath(p) for p in glob.glob(os.path.join(OUT_DIR, "*.generated.yml"))}
    if "--check" in argv:
        stale = [os.path.relpath(p, ROOT) for p, b in want.items()
                 if (open(p, encoding="utf-8").read() if os.path.exists(p) else None) != b]
        stale += [os.path.relpath(p, ROOT) + " (orphaned)" for p in have - set(want)]
        if stale:
            print("STALE prometheus jobs — re-run scripts/gen-prometheus-jobs.py:\n  " + "\n  ".join(stale))
            return 1
        print("prometheus jobs up to date (%d file(s))" % len(want))
        return 0
    for p in have - set(want):
        os.remove(p)
    for p, body in want.items():
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
    print("wrote %d job file(s)" % len(want))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

### 3.4 The `module` param problem — how snmp serves cisco AND fortigate under ONE `network` job

`snmp_exporter` is multi-target via **both** `?target=<device>` and `?module=<mib-module>`. Two classes (`cisco_ios`, `fortigate`) share the `network` job but need *different* modules. Two clean options; **this design picks (B)**:

- **(A)** one job per `(method, module)` — job-name proliferation, breaks the "one file_sd per job dir" simplicity.
- **(B) carry `module` as a TARGET LABEL, relabel it onto `__param_module`.** The `_entries` proxy branch writes each target with a `__param_module` label sourced from the class's `params.module`; the generated job has a relabel `{source_labels: [__param_module], target_label: __param_module}` (a passthrough that lifts the per-target label into the query param). One `network` job serves every SNMP class; the per-class module rides on the target row.

So `_entries` for the proxy branch (the dispatcher's proxy arm, §3.5) emits, for a class using `{method: snmp, params: {module: cisco_wan}}`:

```yaml
- targets: ["192.0.2.2"]
  labels:
    host: my-switch
    __param_module: cisco_wan        # lifted to ?module=cisco_wan by the network job's relabel
```

and the `network` job (generated) gains, before the `__address__` rewrite:

```yaml
  - source_labels: [__param_module]    # per-target module (cisco vs fortigate) -> the ?module= query param
    target_label: __param_module
```

`__param_*` labels are **dropped from the final series by Prometheus automatically** (they're meta-labels), so they don't pollute label cardinality. This is the standard snmp_exporter file_sd pattern, made generated.

### 3.5 The proxy branch of the `gen-observability.py` dispatcher (MODIFIED)

The keystone dimension replaces `_entries`'s if/else with a method dispatch. **This is the proxy-exporter `render` this dimension contributes** (a method-object method, or a function the dispatcher calls when `method["kind"] == "proxy-exporter"`):

```python
def _proxy_entries(method, params, group, hosts):
    """proxy-exporter rows: target = the DEVICE address (no port); the job's relabel proxies it to the
    control-node exporter. Per-class params (validated against params_schema) ride as __param_<k> labels."""
    rows = []
    for name, ip in sorted(hosts.items()):
        labels = {}
        for lbl in method.get("labels", ["host"]):
            labels[lbl] = {"host": name, "group": group}[lbl]
        for k, v in (params or {}).items():            # closed allow-list validated upstream (§1.3)
            labels["__param_%s" % k] = v
        rows.append(([ip], labels))                    # bare device addr (target_shape: device)
    return rows
```

`_render` must be made to emit `__param_*` keys unquoted like other labels (it already does — `scripts/gen-observability.py:64-65` writes `%s: %s`). **No change to `_render`'s label loop**; but see §8.1 for the `safe_dump` hardening that should land for label *values*.

---

## 4. Generating the exporter container (closes gap b') — `gen-exporters.py`

The exporter compose fragment + its `.env`-example keys + (for snmp) its config are generated from the descriptor, so adding an exporter is **data, not three hand-edits**.

### 4.1 `scripts/gen-exporters.py` (NEW) — emits `docker/services/<name>.generated.yaml`

```python
#!/usr/bin/env python3
"""Generate the docker compose fragment for each proxy-exporter telemetry method from telemetry/<name>.yml.
Replaces the hand-authored docker/services/pve-exporter.yaml pattern: image, listen port (NO host port —
kontroll net only, C3), env (${VAR:-} blank-tolerant), config mount, and the 4-line access-chain header are
all DATA in the descriptor. Output: docker/services/<name>.generated.yaml + a one-line include in
compose.yaml (added by hand once per method — the single composition seam, like the include: list).
Standalone; GENERATED lockfile kept honest by --check.  Usage: python3 scripts/gen-exporters.py [--check]
"""
import glob, os, sys, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEMETRY = os.path.join(ROOT, "telemetry")
OUT_DIR = os.path.join(ROOT, "docker", "services")


def _load_proxy_methods():
    out = []
    for fn in sorted(os.listdir(TELEMETRY) if os.path.isdir(TELEMETRY) else []):
        if fn.endswith((".yml", ".yaml")):
            with open(os.path.join(TELEMETRY, fn), encoding="utf-8") as fh:
                d = yaml.safe_load(fh) or {}
                if d.get("kind") == "proxy-exporter":
                    out.append(d)
    return out


def _fragment(m):
    ex = m["exporter"]
    ac = ex["access_chain"]
    header = (
        "# GENERATED by scripts/gen-exporters.py from telemetry/%s.yml. Do NOT edit by hand.\n"
        "# %s exporter — scraped over the kontroll net as %s; NO host port published (C3, mgmt-only).\n"
        "#\n"
        "# Access chain used:   %s\n"
        "# May break:           %s\n"
        "# Fallback required:   %s\n"
        "# Blast radius:        %s\n"
    ) % (m["name"], m["name"], m["exporter_address"], ac["chain"], ac["may_break"],
         ac["fallback"], ac["blast_radius"])
    svc = {
        "image": ex["image"],
        "container_name": ex["container_name"],
        "restart": "unless-stopped",
    }
    if ex.get("env"):
        svc["environment"] = dict(ex["env"])                  # ${VAR:-} strings pass through safe_dump
    if ex.get("config_mount"):
        svc["volumes"] = ["../../%s:%s:ro" % (ex["config_mount"], ex["config_container_path"])]
    svc["networks"] = ["kontroll"]
    doc = {"services": {ex["container_name"]: svc},
           "networks": {"kontroll": {"external": True, "name": "kontroll"}}}
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def exporter_files():
    return {"docker/services/%s.generated.yaml" % m["name"]: _fragment(m) for m in _load_proxy_methods()}

# main(): identical --check / orphan-prune / LF-write shape as gen-prometheus-jobs.py.
```

> **`compose.yaml` include line (MODIFIED, by hand, once per method):** the `include:` list is the *single composition seam* (the file's own header says "Add a service = add a file + one line below"). The generator does NOT edit `compose.yaml` — adding `- services/snmp-exporter.generated.yaml` is the one allowed hub-edit, exactly like adding a backend dir doesn't auto-edit anything. A `--check` step asserts every `*.generated.yaml` has an include line (§7).

### 4.2 `pve-exporter` migration

`docker/services/pve-exporter.yaml` is **deleted** and regenerated as `pve-exporter.generated.yaml`; the `compose.yaml` include line changes from `services/pve-exporter.yaml` to `services/pve-exporter.generated.yaml`. **Acceptance:** `docker compose --env-file docker/.env.example -f docker/compose.yaml config` (the existing `tests/validate.sh:24` step) produces a byte-equivalent `pve-exporter` service (same image, env, no ports, kontroll net).

---

## 5. Exporter secrets via SOPS domains (`no_log`)

### 5.1 New domain: `instance/secrets/snmp_observability.sops.yml` (NEW)

`snmp` introduces new secret material (the SNMPv2c community / v3 creds). Per the self-review security major, it gets its **own** domain resolving to **base_recipients (control + break-glass only)** — *not* `network`/`proxmox` (which the scoped Semaphore key can decrypt). The `.sops.yaml` **catch-all** (`.*\.sops\.ya?ml$` → `base_recipients`, lines 56-57) already covers it — **no `.sops.yaml` edit needed**. (An explicit ops block would only be added if a Semaphore runner had to decrypt it at job time; the exporter renders it at deploy, so it stays base-only.)

Plaintext content (before encryption):

```yaml
# instance/secrets/snmp_observability.sops.yml  (BEFORE encryption)
snmp_community_default: "REDACTED"        # SNMPv2c read-only community for the core switch / edge firewall
# (v3 creds — snmp_v3_username/auth/priv — added here if v3 is adopted)
```

`blackbox` needs **no secret** (`secret_domain: null`) — ICMP/TCP probes carry no credential.

### 5.2 SNMP community rendered into `snmp.yml`, NOT `.env`

`snmp_exporter` reads communities from its config file, not env. So the community is templated into the **generated** `prometheus/exporters/snmp/snmp.yml` at deploy time (§6.2) via a `no_log` task, **not** appended to `docker/.env`. This keeps the secret out of the committed exporter config (only the `.generated` config with a placeholder is committed; the real value is rendered at deploy and the rendered file is gitignored/0600 like `.env`).

### 5.3 `pve` secret render is unchanged

`telemetry/pve.yml`'s `secret_env_map` is now **CONSUMED** (no longer documentary): `gen-secret-env.py` collects it
(+ every logging method's `secret_env_map`) into the unified `config/secret-env.manifest.generated.yml`, and
deploy-stack's ONE generic render loop composes the `PVE_EXPORTER_*` `.env` lines from it via `kontroll_render_token`
(decrypt-by-name, `no_log`, fail-soft) — the hand-written lines are gone. Adding an exporter secret is a descriptor
drop-in, zero `deploy-stack.yml` edit. The values are braced templates now (`PVE_EXPORTER_USER: "{proxmox_api_user}"`),
the SAME shape as a logging token. Design: `docs/reviews/2026-06-17-secret-injection/`.

### 5.4 Bootstrap secrets-domain report bridge

`bootstrap.yml` collects `secrets_domain` per **module**, not per **method** (confirmed gap). A method-introduced domain (`snmp_observability`) is invisible to the report unless bridged. **Bridge:** add a tiny standalone helper the bootstrap calls — `scripts/gen-secret-domains.py` (NEW, ~20 lines) that unions module `secrets_domain` + every enabled-module-referenced method's `secret_domain` → prints the domain set the fleet needs. The bootstrap stat-report iterates that union instead of modules only. (Minimal; can be deferred to a follow-up commit if bootstrap is out of this phase's blast radius — but the domain file itself must exist + be encrypted, which `tests/validate.sh:28` enforces.)

---

## 6. SNMP module/MIB handling and `generator.yml`

`snmp_exporter` needs a `snmp.yml` config that maps **modules** (named MIB walk profiles) to OIDs. Upstream you author a `generator.yml` (declares which MIBs/OIDs each module walks) and run the `snmp_exporter` generator to compile `snmp.yml`. In kontroll this follows the **declare-as-data → generate** doctrine.

> **SHIPPED REALITY (2026-06-17) — this §6 is the original design record; the as-built differs in three ways.**
> (1) The `if_mib` modules block is GENERATED into `prometheus/exporters/snmp/modules.generated.yml` by
> `scripts/gen-snmp.py` (the pinned snmp_exporter generator **v0.30.1** + a vendored MIB closure under
> `prometheus/exporters/snmp/mibs/`, net-snmp v5.9 + IANA) — not a `snmp.yml.generated` lockfile. (2) `snmp.yml.j2`
> keeps only the SNMPv3 `auths:` block + `{% include 'modules.generated.yml' %}`; auth is **SNMPv3 authPriv**, not
> the v2c `${SNMP_COMMUNITY}` sketched below. (3) The module is selected via `params.module.allowed`, not
> `params_schema.module.enum`. Freshness: `gen-snmp.py --check` + a dedicated CGO `snmp-modules` CI byte-gate.
> Authoritative current-state doc: [prometheus/exporters/snmp/README.md](../../prometheus/exporters/snmp/README.md).

### 6.1 `prometheus/exporters/snmp/generator.yml` (NEW, hand-authored source)

The source of truth: which modules exist and what each walks. This is what `params_schema.module.enum` in `telemetry/snmp.yml` must stay in sync with.

```yaml
# prometheus/exporters/snmp/generator.yml — SOURCE for snmp_exporter's compiled snmp.yml.
# Each top-level key is a `module` a device class selects via module.yml metrics params.module.
# Compile with the snmp_exporter generator (docker run; see prometheus/exporters/snmp/README.md).
modules:
  if_mib:                       # generic interface counters — any SNMP device
    walk: [1.3.6.1.2.1.2, 1.3.6.1.2.1.31.1.1]   # IF-MIB ifTable + ifXTable
    version: 2
    auth: {community: "${SNMP_COMMUNITY}"}       # placeholder; the real community is rendered at deploy (§6.2)
  cisco_wan:                    # Cisco IOS-XE: interfaces + CPU/mem + env (the core switch)
    walk: [1.3.6.1.2.1.2, 1.3.6.1.4.1.9.9.109, 1.3.6.1.4.1.9.9.48]
    version: 2
    auth: {community: "${SNMP_COMMUNITY}"}
  fortigate:                    # FortiGate: interfaces + sessions + CPU/mem (FORTINET-FORTIGATE-MIB)
    walk: [1.3.6.1.2.1.2, 1.3.6.1.4.1.12356.101.4]
    version: 2
    auth: {community: "${SNMP_COMMUNITY}"}
```

### 6.2 `prometheus/exporters/snmp/snmp.yml.generated` (GENERATED) + deploy-time secret injection

The compiled `snmp.yml` (with `${SNMP_COMMUNITY}` placeholders) is **committed** (it's large, derived from `generator.yml` + the MIB compiler — too heavy to compile in CI hermetically, so it's a checked-in lockfile with a header naming `generator.yml` as its source). A `--check`-lite step (§7) asserts every `params_schema.module.enum` value is a top-level module in `generator.yml` (the cheap, hermetic invariant — not a full MIB recompile).

At deploy, `deploy-stack.yml` renders the real community into the mounted config with a `no_log` task:

```yaml
# deploy-stack.yml (NEW task, gated `when: 'prometheus' in stack_services and 'snmp' enabled`)
- name: Render the snmp_exporter config with the read-only community (secret — never logged)
  ansible.builtin.template:
    src: "{{ playbook_dir }}/../../prometheus/exporters/snmp/snmp.yml.generated"
    dest: "{{ playbook_dir }}/../../prometheus/exporters/snmp/snmp.yml"
    owner: "{{ lookup('env', 'USER') }}"
    mode: "0600"
  vars:
    snmp_community: "{{ (lookup('community.sops.sops', playbook_dir ~ '/../secrets/snmp_observability.sops.yml') | from_yaml).snmp_community_default }}"
  no_log: true
  # ${SNMP_COMMUNITY} in the .generated file is a Jinja-safe token the template replaces; the rendered
  # snmp.yml is 0600, gitignored (like docker/.env), mounted :ro into the exporter.
```

> **Access chain header for the SNMP scrape path** (carried in `telemetry/snmp.yml` and stamped into the generated compose fragment): mgmt-VLAN-only SNMP GET; **read-only; never actuates**; the core switch / edge firewall are polled, never written. This is the high-blast-radius mitigation — SNMP read is incapable of config change.

### 6.3 `prometheus/exporters/blackbox/blackbox.yml` (NEW, static hand-authored)

Blackbox modules are tiny and stable — hand-authored (not generated), committed plaintext (no secret):

```yaml
# prometheus/exporters/blackbox/blackbox.yml — probe module definitions (no secret).
modules:
  icmp:        {prober: icmp, timeout: 5s}
  tcp_connect: {prober: tcp,  timeout: 5s}
  http_2xx:    {prober: http, timeout: 5s, http: {valid_status_codes: [], method: GET}}
```

---

## 7. The cisco_ios + fortigate metrics blocks (closes gap d) — HIGH BLAST RADIUS

These are the first agent-less devices. They are the **edge firewall and core switch** — the two highest-blast-radius hosts (CLAUDE.md). The mitigation is structural: **SNMP/blackbox are read-only pull**; nothing here can change device state, and no `--check --diff` actuation play touches them (the exporter polls; the device is never written).

### 7.1 `modules/cisco_ios/module.yml` (MODIFIED — append)

```yaml
# (existing fields unchanged: key/description/status/collections/role/inventory_group/secrets_domain)
# Observability — agent-less: an SNMP exporter on the control node polls the switch read-only (it cannot
# run node_exporter). The exporter + its prometheus job + relabel are GENERATED from telemetry/snmp.yml;
# this block only NAMES the method + the per-class MIB module. Read-only — the core switch is never actuated.
metrics:
  - method: snmp
    params: {module: cisco_wan}      # walks IF-MIB + Cisco CPU/mem/env (snmp.yml module 'cisco_wan')
  - method: blackbox
    params: {probe: icmp}            # reachability of the core switch (mgmt VLAN)
dashboards:
  - {gnet: 14857, name: snmp_network}
```

### 7.2 `modules/fortigate/module.yml` (MODIFIED — append)

```yaml
metrics:
  - method: snmp
    params: {module: fortigate}      # FortiGate sessions/CPU/mem/interfaces (snmp.yml module 'fortigate')
  - method: blackbox
    params: {probe: icmp}            # reachability of the edge firewall (mgmt VLAN)
dashboards:
  - {gnet: 14857, name: snmp_network}
```

These generate (after `gen-observability.py`):

```
prometheus/targets/network/cisco_ios.generated.yml
prometheus/targets/network/fortigate.generated.yml
```

### 7.3 Worked example — the generated SNMP target + job for cisco_ios

**`prometheus/targets/network/cisco_ios.generated.yml`** (GENERATED by `gen-observability.py`):

```yaml
# GENERATED by scripts/gen-observability.py from instance/fleet.yml + modules/<key>/module.yml
# + instance/inventory/hosts.yml. Do NOT edit by hand — change a module's `metrics:` block or the
# inventory and re-run (deploy-stack regenerates it before bringing Prometheus up).
- targets: ["192.0.2.2"]
  labels:
    host: my-switch
    __param_module: cisco_wan
```

**`prometheus/jobs.d/network.generated.yml`** (GENERATED by `gen-prometheus-jobs.py` from `telemetry/snmp.yml` — the worked SNMP job):

```yaml
# GENERATED by scripts/gen-prometheus-jobs.py from telemetry/<name>.yml (proxy-exporter methods).
# Do NOT edit by hand — change a telemetry method descriptor and re-run (deploy-stack does).
job_name: network
metrics_path: /snmp
file_sd_configs:
- files:
  - /etc/prometheus/targets/network/*.yml
relabel_configs:
- source_labels:
  - __address__
  target_label: __param_target          # ?target=192.0.2.2 : which device snmp_exporter queries
- source_labels:
  - __param_target
  target_label: instance                # label series by the queried device
- source_labels:
  - __param_module                       # the per-target MIB module (cisco_wan vs fortigate) -> ?module=
  target_label: __param_module
- target_label: __address__
  replacement: snmp-exporter:9116        # but always actually scrape the exporter
```

This is the entire `via:proxy` contract — `metrics_path`, `?target=`, `?module=`, and the exporter rewrite — now **generated from data**, never hand-written. The same generator, fed `telemetry/pve.yml`, reproduces the *current* `proxmox` job byte-for-byte into `prometheus/jobs.d/proxmox.generated.yml` (the migration acceptance).

### 7.4 The regression test FLIPS — same commit

`tests/unit/test_gen_observability.py::test_classes_without_metrics_block_emit_no_target` (lines 59-64) **asserts cisco/fortigate produce NO target**. Adding §7.1/§7.2 **flips** it. It MUST be rewritten in the same commit (§8 test plan), docstring rationale changing from "unmonitored is honest" to "now monitored via the snmp method; openwrt (still no block) preserves the no-block⇒no-target honesty."

---

## 8. Security posture & the config-injection guardrail

### 8.1 Config-injection (the GUI-phase regression net, enforced now)

- **Descriptors are operator-owned drop-ins** (peers of `vectors/`, `ansible/backends/`). The relabel template, `exporter_address`, port, job name come from the **descriptor only** — never onboarding input.
- **Per-class `params` are a CLOSED allow-list** (`params_schema`, §1.3). `gen-observability.py` validates `params` against the named method's `params_schema` and `sys.exit(2)` on an unknown key or out-of-enum value. A test pins this (§8 `test_snmp_param_outside_enum_fails_loud`).
- **YAML emission is structural.** `gen-prometheus-jobs.py`/`gen-exporters.py` use `yaml.safe_dump`. For `gen-observability.py`'s target rows, the label *values* (which now include `params`-derived `__param_module`) should be quoted; the minimal hardening is to route label values through `yaml`-safe quoting. A test pins that a metacharacter-bearing param value is escaped or rejected, never structurally injected (§8 `test_param_metacharacter_is_not_injected`).
- **Unknown method fails closed.** A `module.yml` metrics entry naming a method with no `telemetry/<name>.yml` descriptor must `sys.exit(2)` — never silently emit an empty/wrong target (replaces the old closed-enum's accidental safety). Test: `test_unknown_method_fails_loud`.

### 8.2 Listener posture (mgmt-only, no host port) — covering check

Every generated exporter publishes **no host port** (kontroll net only) — the C3 rule extended to exporters. A new `tests/validate.sh` step asserts no `docker/services/*exporter*.yaml` (generated or not) carries a `ports:` mapping:

```bash
# no exporter publishes a host port (C3: kontroll-net-only, mgmt-bound)
step exporter-no-host-port 0 bash -c '
  if grep -rE "^\s*ports:" docker/services/*exporter*.yaml 2>/dev/null; then
    echo "  an exporter fragment publishes a host port — exporters are kontroll-net-only (C3)"; exit 1; fi
  exit 0'
```

### 8.3 SSRF bounding (blackbox/snmp `?target=`)

`blackbox`/`snmp` are `?target=`-driven (SSRF-shaped). The `?target` set is **structurally bounded to the generated `targets/<job>/*.generated.yml` addresses** (inventory `ansible_host` values on mgmt/server VLANs) — never a caller-supplied param (no API/GUI in this phase). The exporters publish no host port, so the listener is unreachable off the kontroll net. Documented as an accepted-risk-with-control in SECURITY.md (§8.5).

### 8.4 Read-only creds, `no_log`, per-VLAN TLS

- SNMP community is **read-only** (v2c read community / v3 read user). A leak yields read-only device visibility, never actuation — same posture as the pve `*.Audit` token.
- The community-render task is `no_log: true` (§6.2).
- `verify_tls` is a per-method field, defaulting `true`; only `pve` sets `false` (documented mgmt self-signed, C3). No method inherits a cert-skip by copy-paste.

### 8.5 SECURITY.md — C9 amendment (MODIFIED)

Append to C9's **Implements** list (after the host-agent bullet at `SECURITY.md:208-214`):

```markdown
- **Agent-less exporters (snmp_exporter / blackbox_exporter): additive, read-only, generated, mgmt-bound.**
  The core switch (`cisco_ios`) and edge firewall (`fortigate`) cannot run a host agent, so a control-node
  exporter polls them **read-only**: `snmp-exporter` issues SNMPv2c read-community GETs (interface/CPU/mem
  MIBs); `blackbox-exporter` does ICMP reachability. Both publish **no host port** (scraped only over the
  `kontroll` network — covering check: `tests/validate.sh` `exporter-no-host-port`), reachable only on the
  mgmt VLAN, **never WAN (C3)**. The exporter container, its Prometheus job + `relabel_configs`, and its
  config are **GENERATED** from `telemetry/<method>.yml` (`scripts/gen-exporters.py`,
  `scripts/gen-prometheus-jobs.py`) — so `prometheus.yml` is no longer hand-edited per exporter, removing a
  drift surface. The SNMP **read-only community** lives in its own SOPS domain
  (`instance/secrets/snmp_observability.sops.yml`, control + break-glass only — **not** the Semaphore-scoped
  `network`/`proxmox` domains), rendered `no_log` into the exporter's config at deploy; blackbox carries no
  secret. A leaked community yields read-only device visibility, never actuation (the exporter is a metrics
  poller — a scrape failure degrades a panel, not the firewall/switch). The `?target=` set is **bounded to
  the generated inventory addresses** (no caller-supplied target — no API/GUI path in this phase), so the
  multi-target exporters cannot be coerced into probing arbitrary hosts. **Accepted risk:** SNMPv2c sends
  the read community in cleartext on the mgmt VLAN; mitigated by VLAN isolation (C3) — adopt SNMPv3 if the
  mgmt VLAN's trust assumption changes (tracked).
```

Also append to the SECURITY.md "Update this document when…" triggers: *a new exporter class or its secret domain is added* (snmp_observability already trips "a new credential field or secret domain is added").

---

## 9. Tests (first-class, same commit) — files, names, docstrings

### 9.1 `tests/unit/test_telemetry_methods.py` (NEW)

```python
"""telemetry/<method>.yml is a drop-in registry (peer to vectors/ and ansible/backends/) that DATA-fies
the per-exporter wiring — job, relabel, container, secret domain — so adding a proxy exporter is a file,
not a hub edit. These tests pin the loader, the proxy-exporter descriptor schema, and the closed params
contract that is the regression net for the (later) GUI onboarding phase."""

def test_load_telemetry_methods_discovers_dropins():
    """catalog.load_telemetry_methods() (and gen-*.py _load_methods) auto-discover every telemetry/*.yml
    sorted by order — guards the drop-in doctrine: a new telemetry/<name>.yml is picked up with zero loader
    edit, exactly as vectors/backends are. Fails if a method file is silently ignored."""

def test_seed_proxy_methods_have_required_fields():
    """pve/snmp/blackbox each declare the proxy-exporter required keys (kind, job, metrics_path,
    exporter_address, target_shape, labels, exporter.image, exporter.listen_port) — guards a half-authored
    descriptor that would generate a broken job or a portless-but-also-addressless exporter."""

def test_snmp_params_schema_enum_matches_generator_modules():
    """Every value in telemetry/snmp.yml params_schema.module.enum is a top-level module in
    prometheus/exporters/snmp/generator.yml — guards drift where a module.yml asks for an snmp module the
    compiled snmp.yml does not define (a scrape that 404s at the exporter), the hermetic invariant that
    replaces a full MIB recompile in CI."""

def test_pve_descriptor_reproduces_live_proxmox_wiring():
    """telemetry/pve.yml's job/metrics_path/exporter_address/target_param equal the values the hand-written
    prometheus.yml proxmox job carried (proxmox, /pve, pve-exporter:9221, target) — guards that the
    data-lift is byte-equivalent, so migrating to the registry is a refactor, not a live-config change."""
```

### 9.2 `tests/unit/test_gen_prometheus_jobs.py` (NEW)

```python
"""scripts/gen-prometheus-jobs.py generates the per-proxy-exporter Prometheus job (job_name + metrics_path
+ relabel_configs) from telemetry/<method>.yml into prometheus/jobs.d/<job>.generated.yml — closing gap (b):
the relabel that makes via:proxy actually scrape is now DATA, not a hand-written prometheus.yml block. These
tests pin the relabel shape, the proxmox byte-compat, the SNMP worked example, and the --check guard."""

def test_proxmox_job_reproduces_the_handwritten_relabel():
    """The generated proxmox job's relabel chain (__address__ -> __param_target -> instance; __address__ ->
    pve-exporter:9221) byte-matches the relabel that was hand-written in prometheus.yml — guards that moving
    the binding into telemetry/pve.yml does NOT change what Prometheus scrapes (promtool + live parity)."""

def test_snmp_job_emits_target_and_module_relabels():
    """The generated network job (telemetry/snmp.yml) rewrites __address__ to snmp-exporter:9116, passes
    ?target=<device> AND lifts the per-target __param_module label to ?module= — guards the multi-class SNMP
    pattern (cisco_wan vs fortigate under ONE network job) that §3.4 depends on."""

def test_check_mode_passes_when_committed_jobs_are_fresh():
    """gen-prometheus-jobs.py --check returns 0 when prometheus/jobs.d/*.generated.yml match the descriptors
    and non-zero otherwise — the generated-never-hand-maintained guard for the NEW job artifact, mirroring
    gen-observability --check so a drifted prometheus job cannot merge stale."""

def test_relabel_body_is_safe_dumped_not_string_interpolated():
    """The job body is produced by yaml.safe_dump (structural), so a method field containing YAML
    metacharacters is escaped, never injected as a new key — guards the config-injection surface the (later)
    GUI param flow opens."""
```

### 9.3 `tests/unit/test_gen_exporters.py` (NEW)

```python
"""scripts/gen-exporters.py generates docker/services/<name>.generated.yaml from telemetry/<method>.yml —
closing gap (b'): the exporter container (image, env, config mount, access-chain header) is DATA, not a
hand-authored compose fragment. These tests pin the no-host-port rule, the access-chain header, and that
pve regenerates equivalently."""

def test_generated_exporter_publishes_no_host_port():
    """No generated exporter fragment contains a `ports:` mapping — guards C3 (exporters are kontroll-net
    only, mgmt-bound, never 0.0.0.0); a host-port leak would expose a metrics listener off the VLAN."""

def test_generated_exporter_carries_access_chain_header():
    """Every generated fragment stamps the 4-line access-chain header (chain/may_break/fallback/blast_radius)
    from the descriptor — guards the access-chain discipline (CLAUDE.md) for generated state, so a generated
    container is as auditable as a hand-written one."""

def test_pve_fragment_matches_live_service_shape():
    """The generated pve-exporter fragment has the same image, the same three ${PVE_EXPORTER_*:-} env vars,
    PVE_VERIFY_SSL false, no ports, and the kontroll net — guards that deleting the hand-authored
    pve-exporter.yaml for the generated one is shape-equivalent (compose config stays valid)."""
```

### 9.4 `tests/unit/test_gen_observability.py` (MODIFIED)

```python
# REWRITE (was test_classes_without_metrics_block_emit_no_target):
def test_snmp_classes_emit_network_targets_openwrt_stays_empty():
    """cisco_ios + fortigate now declare a {method: snmp} block, so the generator emits a network target per
    device (the device address + a __param_module label) — closing gap (d), the previously-unmonitored core
    switch / edge firewall. openwrt (still no metrics block) emits NO target, preserving the 'no metrics
    block => no target' honesty. This test FLIPPED from asserting cisco/fortigate are unmonitored: the
    rationale moved from 'unmonitored is honest' to 'now monitored agent-lessly via the snmp method'."""

# NEW:
def test_unknown_method_fails_loud():
    """A metrics entry naming a method with no telemetry/<name>.yml descriptor makes the generator exit
    non-zero (never a silent empty/wrong target) — the fail-closed property that replaces the old closed
    via-enum's accidental safety, so a typo'd method name is caught at generate time."""

def test_snmp_param_outside_enum_fails_loud():
    """A {method: snmp, params: {module: <not-in-enum>}} entry fails the params_schema validation and exits
    non-zero — guards the closed-allow-list contract that bounds the (later) GUI param surface; an unknown
    module never reaches the generated job."""
```

### 9.5 `tests/validate.sh` (MODIFIED) — new gate steps (same commit as the generators)

```bash
# generated prometheus jobs are fresh (closes gap b; mirrors gen-observability --check)
if [ -n "$PY" ]; then step gen-prometheus-jobs 0 "$PY" scripts/gen-prometheus-jobs.py --check; fi
# generated exporter fragments are fresh (closes gap b')
if [ -n "$PY" ]; then step gen-exporters 0 "$PY" scripts/gen-exporters.py --check; fi
# every generated exporter has a compose include line (the one allowed hub-edit per method)
step exporter-include 0 bash -c '
  rc=0
  for f in docker/services/*.generated.yaml; do
    [ -e "$f" ] || continue
    grep -q "services/$(basename "$f")" docker/compose.yaml || { echo "  no include for $f"; rc=1; }
  done; exit $rc'
# no exporter publishes a host port (C3)  [the §8.2 step]
step exporter-no-host-port 0 bash -c '...'
```

> The existing `promtool check config prometheus/prometheus.yml` step (`validate.sh:25`) now also validates the `scrape_config_files` include — but `promtool` resolves `/etc/prometheus/jobs.d/*` (a container path) which won't exist at lint time. **Mitigation:** point the validate-time `promtool` at a repo-relative copy, OR accept that `promtool` validates the static part and the generated jobs are covered by `gen-prometheus-jobs --check` + a yamllint pass. Recommend: keep `scrape_config_files` path container-absolute (it must match the mount); `promtool` will warn-not-fail on a missing glob (globs that match nothing are valid). Confirmed acceptable — Prometheus treats an empty `scrape_config_files` glob as zero extra configs, not an error.

---

## 10. Acceptance criteria

1. **No live change from the migration:** after deleting `docker/services/pve-exporter.yaml` → `pve-exporter.generated.yaml`, generating `prometheus/jobs.d/proxmox.generated.yml` from `telemetry/pve.yml`, and removing the inline `proxmox` job from `prometheus.yml`, the rendered scrape behaviour is **equivalent**: `docker compose --env-file docker/.env.example -f docker/compose.yaml config` is valid; the proxmox job's `metrics_path`, relabel chain, and `pve-exporter:9221` rewrite are byte-identical to the pre-change inline job. `git diff prometheus/targets/proxmox/` is **empty**.
2. **gap (a) closed for proxy:** adding `telemetry/blackbox.yml` (a 2nd proxy method) touches **zero hub files** — no `gen-observability.py` edit, no `prometheus.yml` `scrape_configs` edit. Only `telemetry/blackbox.yml` + one `compose.yaml` include line + (its module.yml metrics block).
3. **gap (b) closed:** `prometheus.yml` has **no** per-exporter `relabel_configs` — they live in `prometheus/jobs.d/*.generated.yml`, generated from descriptors, guarded by `gen-prometheus-jobs --check`.
4. **gap (b') closed:** `docker/services/*-exporter*.generated.yaml` are generated from descriptors; adding an exporter requires no hand-written compose fragment.
5. **gap (d) closed:** `cisco_ios` + `fortigate` emit `prometheus/targets/network/*.generated.yml`; the previously-empty `network` job has SNMP targets; the high-blast-radius edge/core are monitored **read-only** (no actuation play touches them).
6. **Secrets:** `instance/secrets/snmp_observability.sops.yml` exists, is SOPS-encrypted (`validate.sh:28` green), resolves to base_recipients (not Semaphore-scoped); the community is rendered `no_log` into `snmp.yml` at deploy, never committed plaintext, never in `.env`, never in a log.
7. **Listeners:** no generated exporter publishes a host port (`exporter-no-host-port` green).
8. **`tests/validate.sh` green** with the 4 new steps; `pytest -m "not e2e and not slow"` green including the new/flipped tests with docstrings (`check-test-docs.py` green).
9. **Worked example reproduces:** running `gen-observability.py` + `gen-prometheus-jobs.py` produces exactly the `cisco_ios.generated.yml` target and `network.generated.yml` job shown in §7.3.

---

## 11. Audit / log lines (no GUI/API in this phase — CLI + Ansible only)

This phase actuates only via **operator CLI / deploy-stack** (no privileged route), so the audit surface is the **Ansible `kontroll_run_id`** path, not the API TSV:

- `deploy-stack.yml` already imports nothing for run-id on the `localhost` play; the **new SNMP-config-render task** runs under deploy-stack. The deploy run's correlation is the operator's invocation. The render task is `no_log: true` — **the community never appears in the log** (only the task name "Render the snmp_exporter config with the read-only community" — action, never credential).
- If a dedicated exporter-provision play is later added (host-agent/exporter parity), it MUST `- import_playbook: _log-run-id.yml` first (the convention at `install-node-exporter.yml:16-17`), emitting `kontroll_run_id=<id>` so the provision is greppable. For Phase 3, provisioning rides `deploy-stack` (which the operator invokes), so no new play is needed.
- **No credential in any audit/log line:** the SNMP community is rendered `no_log`; `snmp.yml`/`.env` are gitignored + 0600; the generated `snmp.yml.generated` holds only the `${SNMP_COMMUNITY}` placeholder.

---

## 12. Documentation-Sync rows (same commit)

| Changed | Doc to update |
|---|---|
| `module.yml` metrics schema (`{job,via,port}` → `{method, params}`); cisco/fortigate gain blocks | `modules/README.md` schema table row 33 (rewrite `metrics:` to `method`/`params`; note agent-less proxy methods); catalog note that cisco/fortigate now monitored |
| New telemetry methods (`pve`/`snmp`/`blackbox`) + the registry | **NEW** `telemetry/README.md` (the method-descriptor schema, proxy-exporter kind, params_schema contract, "add a method" test) — peer of `vectors/`'s inline doc; reciprocal See-also from `modules/README.md` + `prometheus/README.md` |
| Generated prometheus jobs + the `scrape_config_files` include | `prometheus/README.md` (the `jobs.d/*.generated.yml` mechanism + `gen-prometheus-jobs --check`; the relabel-is-generated note); error table row "stale generated jobs" |
| Generated exporter fragments + new exporter services | `prometheus/README.md` / a new `prometheus/exporters/snmp/README.md` (how to recompile `snmp.yml` from `generator.yml`); `docker/.env.example` (no new pve keys — but document that exporter env is descriptor-driven) |
| New secret domain `snmp_observability` | `instance/secrets/README.md` table (add row: domain of `cisco_ios`/`fortigate` SNMP community; base-recipients only); `SECURITY.md` C1 + C9 amendment (§8.5) |
| Security control / trust boundary (agent-less exporters) | `SECURITY.md` C9 amendment (§8.5) + the "Update when… new exporter/secret domain" trigger |
| New generators + validate steps | `tests/validate.sh` (4 new steps); `tests/README.md` (the new test files + the `telemetry/` seam row) |
| Behaviour-affecting | `CHANGELOG.md [Unreleased]` — "feat(observability): agent-less SNMP/blackbox monitoring for the core switch + edge firewall; per-exporter Prometheus jobs/relabels + exporter containers generated from telemetry/<method>.yml (prometheus.yml no longer hand-edited per exporter)" |

---

## 13. New/modified file inventory

**NEW:** `telemetry/pve.yml`, `telemetry/snmp.yml`, `telemetry/blackbox.yml`, `telemetry/README.md`; `scripts/gen-prometheus-jobs.py`, `scripts/gen-exporters.py`, `scripts/gen-secret-domains.py` (§5.4, optional/follow-up); `prometheus/jobs.d/.gitkeep` (+ generated `proxmox.generated.yml`, `network.generated.yml`); `prometheus/exporters/snmp/generator.yml`, `prometheus/exporters/snmp/snmp.yml.generated`, `prometheus/exporters/snmp/README.md`, `prometheus/exporters/blackbox/blackbox.yml`; `docker/services/snmp-exporter.generated.yaml`, `docker/services/blackbox-exporter.generated.yaml`, `docker/services/pve-exporter.generated.yaml`; `instance/secrets/snmp_observability.sops.yml`; `tests/unit/test_telemetry_methods.py`, `tests/unit/test_gen_prometheus_jobs.py`, `tests/unit/test_gen_exporters.py`.

**MODIFIED:** `scripts/gen-observability.py` (proxy branch of the dispatcher §3.5, params validation §8.1); `scripts/kontroll/paths.py` (`TELEMETRY_DIR` — shared w/ keystone); `scripts/kontroll/catalog.py` (`load_telemetry_methods()` — shared w/ keystone); `prometheus/prometheus.yml` (add `scrape_config_files`, remove inline `proxmox`/`network` jobs §3.1); `docker/services/prometheus.yaml` (mount `jobs.d` + `exporters` §3.1); `docker/compose.yaml` (3 include lines: pve→generated, +snmp, +blackbox); `modules/cisco_ios/module.yml`, `modules/fortigate/module.yml` (§7); `ansible/playbooks/deploy-stack.yml` (SNMP config render task §6.2); `tests/unit/test_gen_observability.py` (flip + 2 new §9.4); `tests/validate.sh` (4 steps §9.5); `modules/README.md`, `prometheus/README.md`, `instance/secrets/README.md`, `SECURITY.md`, `CHANGELOG.md`, `tests/README.md` (doc-sync §12).

**DELETED:** `docker/services/pve-exporter.yaml` (→ generated).

**Commit split (within Phase 3):** (1) generators + `telemetry/pve.yml` + `prometheus.yml` include + pve migration (acceptance: `git diff targets` empty, compose config valid); (2) `telemetry/snmp.yml` + `generator.yml`/`snmp.yml` + `snmp_observability` domain + cisco/fortigate metrics + test flip + SECURITY C9 (acceptance: network targets generated, validate green); (3) `telemetry/blackbox.yml` + blackbox config + reachability blocks (proves 2nd proxy method is a pure drop-in).
