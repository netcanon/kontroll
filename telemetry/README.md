# Telemetry methods — the ingestion-protocol registry

This directory is the **source of truth for HOW a device/service is scraped**. It is the telemetry analogue
of `vectors/` (capability vectors) and `ansible/backends/` (execution backends): a **drop-in registry** that
[`scripts/gen-observability.py`](../scripts/gen-observability.py) dispatches over to generate Prometheus
scrape targets. Promoting the old hardcoded `via: host | proxy` `if/else` into this registry means **adding a
new ingestion protocol is a new file here, never an edit to the generator** (the "add an X" test) — and
`node_exporter` is now **one descriptor row** (`host_node`), not the generator's default branch.

This is **Phase 1** of the Option-A observability design — full architecture + roadmap:
[`docs/observability/telemetry-method-registry.md`](../docs/observability/telemetry-method-registry.md) and
the master [`docs/observability-onboarding-flow.md`](../docs/observability-onboarding-flow.md).

## The contract

A module declares its observability shape by **referencing a method by name** in `modules/<key>/module.yml`:

```yaml
metrics:
  - {method: host_node}     # host-agent: the host runs node_exporter
  - {method: pve}           # proxy-exporter: a control-node exporter queries the device
```

`gen-observability.py` loads the registry (`catalog.load_telemetry()`), joins enabled modules × their metrics
entries × the inventory hosts in each module's `inventory_group`, and writes
`prometheus/targets/<job>/<key>.generated.yml`. The **method** supplies the job, port, target shape and labels.

## `telemetry/<name>.yml` schema (the implemented subset)

| Field | Meaning |
|---|---|
| `name` | registry key; must equal the filename stem and the `method:` a module references |
| `label` | human one-liner |
| `order` | load/sort order (host-agents low, proxy-exporters 20+) |
| `kind` | `host-agent` (the host runs an exporter) or `proxy-exporter` (a control-node exporter queries the device). `push` / `log-shipping` are reserved — the dispatcher **fails closed** on them until implemented |
| `job` | the Prometheus `job_name` **and** the `prometheus/targets/<job>/` subdir |
| `target_shape` | `host_port` → `"<ansible_host>:<port>"` (host-agent); `device` → `"<ansible_host>"` (proxy-exporter; the job's relabel proxies it onto the exporter) |
| `port` | required iff `target_shape: host_port` |
| `labels` | ordered label keys per target, from `{group, host}` |
| `agent_role` *(host-agent)* | the Ansible role that installs the agent (documentary in the scrape generator; read by the install play / enact step) |
| `metrics_path` *(proxy-exporter)* | the exporter's scrape path (e.g. `/pve`) — used by the generated job |
| `exporter_address` *(proxy-exporter)* | `host:port` the job's relabel rewrites `__address__` to (the control-node exporter, e.g. `pve-exporter:9221`) |
| `target_param` *(proxy-exporter)* | the `?<param>=<device>` query param the multi-target exporter expects (default `target`) |
| `secret_domain` *(proxy-exporter, opt)* | the `instance/secrets/<domain>.sops.yml` whose token the exporter consumes (`null` = no secret, e.g. blackbox). With a `secret_env_map` it is a name the Secrets dialog takes (1-64 characters of `[a-z0-9_-]`, starting with a letter or digit) and not a platform-core domain (`semaphore`, `dashboards`): `gen-secret-env.py` refuses any other, and the deploy's decrypt filter reads no file for one |
| `cap_add` *(proxy-exporter, opt)* | Linux capabilities granted to the exporter container — e.g. `[NET_RAW]` for blackbox's ICMP raw socket; rendered into the compose fragment by `gen-exporters.py` |

`scripts/gen-prometheus-jobs.py` derives each proxy method's Prometheus job (metrics_path + multi-target
relabel) into `prometheus/jobs.d/<job>.generated.yml` from `exporter_address`/`metrics_path`/`target_param` —
the relabel that used to be hand-written in `prometheus.yml`. `scripts/gen-exporters.py` renders the exporter
**container** into `docker/services/<container>.generated.yaml` from the descriptor's `exporter:` block
(`image`, `container_name`, `env`, `access_chain`, optional `config_mount`/`cap_add`) — no host port, kontroll-net-only.
The `params` block is a **closed per-class allow-list** (the snmp `module` — rejected if not in `allowed`: the
config-injection guard) and `static_labels` are method-level target labels (snmp's `__param_auth`); both ship
with the snmp method. A proxy method's secret renders either into a **config file** (snmp_exporter's `snmp.yml`
via the deploy template task) or — for env-based exporters — into `.env` via **`secret_env_map`**: a
`{ENV_VAR: "<str.format template over the secret_domain's SOPS field NAMES>"}` block (e.g.
`PVE_EXPORTER_USER: "{proxmox_api_user}"`). `gen-secret-env.py` collects it (alongside every logging method's
`secret_env_map` — the SAME unified block + manifest) into `config/secret-env.manifest.generated.yml`, and
deploy-stack's ONE generic render loop composes each `.env` line via `kontroll_render_token` (decrypt-by-name,
`no_log`, fail-soft). So adding an exporter secret is a descriptor drop-in — **zero `deploy-stack.yml` edit**.
`suggested_dashboards` lands with the hybrid-dashboard phase.

## Adding a telemetry method (the "add a method" test)

1. `telemetry/<name>.yml` (copy `host_node.yml` for an agent, `pve.yml` for a proxy exporter).
2. Reference `<name>` in a module's `metrics:` list.
3. `python3 scripts/gen-observability.py` regenerates the targets. **No hub file is edited** — not the
   generator, not a central switch.

## See also
- [../docs/observability/telemetry-method-registry.md](../docs/observability/telemetry-method-registry.md) — the design (schema §4.0.2, dispatcher §4.0.3)
- [../prometheus/README.md](../prometheus/README.md) — the generated scrape targets this registry feeds
- [../modules/README.md](../modules/README.md) — the `metrics:` block that references a method
- [../scripts/gen-observability.py](../scripts/gen-observability.py) — the dispatcher (run by `deploy-stack`)
- [../ansible/roles/node_exporter/README.md](../ansible/roles/node_exporter/README.md) — the agent `host_node` installs
