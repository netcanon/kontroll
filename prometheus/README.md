# prometheus — metrics scrape config

Config-as-code for the Prometheus instance (Phase 5; not deployed yet).

## Architecture
- `prometheus.yml` — inline scrape *jobs* + `scrape_config_files: [jobs.d/*.generated.yml]` (the generated proxy-exporter jobs). Targets are NOT listed here.
- `targets/<job>/<key>.generated.yml` — **GENERATED** by `scripts/gen-observability.py` from each enabled
  device module's `metrics:` block (`modules/<key>/module.yml`, which references a **telemetry method** by
  name) × the inventory hosts in its group. The generator **dispatches over the telemetry-method registry**
  (`telemetry/<name>.yml`); the method supplies the job/target-shape/port/labels, so `node_exporter` is one
  descriptor row (`host_node`), not a hardcoded branch. The metrics analogue of `requirements.generated.yml`:
  targets are config-as-data *derived from the fleet*, never hand-maintained. `deploy-stack` regenerates them
  before bringing Prometheus up; `tests/validate`'s `gen-observability --check` fails on a stale committed file.
- `targets/<job>/*.yml` (non-generated) — still **file-based SD**, for any ad-hoc/manual target that isn't
  a managed device class. Prometheus globs the whole dir (`node`, `proxmox`, `network`).
- `jobs.d/<job>.generated.yml` — **GENERATED** by `scripts/gen-prometheus-jobs.py` from the telemetry
  registry: one scrape *job* (metrics_path + multi-target relabel) per **proxy-exporter** method an enabled
  module uses (pve; snmp/blackbox later). The relabel that used to be hand-written in `prometheus.yml` is now
  DATA in `telemetry/<name>.yml`, so adding a proxy exporter is a descriptor drop-in (closes gap-b). Included
  via `scrape_config_files` (relative to the config dir → resolves at host lint **and** in the container).

## Adding a scrape target (the "add an X" test)
**Managed device class:** add a `metrics:` block to its `modules/<key>/module.yml` referencing a telemetry
method (`- {method: host_node}`; a new ingestion protocol is a `telemetry/<name>.yml` drop-in) — with the
class in `instance/fleet.yml` and its hosts in the inventory, the target is **generated**: no target-file,
scrape-config, or generator edit. **Ad-hoc target:** drop a non-`.generated.yml` file under `targets/<job>/`.
New job: add a `job_name` with a `file_sd_configs` glob, then its `targets/<job>/` dir.

## Errors / behaviour
| Condition | Behaviour |
|---|---|
| Malformed target file | that file's targets are skipped; others keep loading |
| Bad `prometheus.yml` | `promtool check config` fails — `tests/validate` catches it |
| Stale generated targets | `gen-observability --check` (in `tests/validate`) fails — re-run `scripts/gen-observability.py` (or `deploy-stack`) |
| Stale generated jobs | `gen-prometheus-jobs --check` (in `tests/validate`) fails — re-run `scripts/gen-prometheus-jobs.py` (or `deploy-stack`) |
| Stale generated snmp modules | `gen-snmp --check` (+ the `snmp-modules` CGO CI byte-gate) fails — re-run `scripts/gen-snmp.py` (CI/VM; see [exporters/snmp/README.md](exporters/snmp/README.md)) |

## Testing
Covered by `tests/validate`'s `promtool` check (runs where promtool is installed).

## See also
- [../PLAN.md](../PLAN.md) §8b (metrics stack)
- [../dashboards/README.md](../dashboards/README.md) — Grafana reads this Prometheus, and alerts on its targets
  (`dashboards/grafana/provisioning/alerting/metrics-rules.yaml` — Grafana-managed; no `rule_files` here)
- [../modules/README.md](../modules/README.md) — the `metrics:` block that generates these targets
- [../scripts/gen-observability.py](../scripts/gen-observability.py) — the generator/dispatcher (run by `deploy-stack`)
- [../telemetry/README.md](../telemetry/README.md) — the telemetry-method registry the generator dispatches over
- [../docs/observability-onboarding-flow.md](../docs/observability-onboarding-flow.md) — Option-A design: the registry (Phase 1, **shipped**) + generated proxy-exporter jobs (later phases, design)
