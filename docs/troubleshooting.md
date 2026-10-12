# Troubleshooting runbook — where each signal lives + how to read it

The operability companion to [logging-architecture.md](logging-architecture.md). When something looks wrong,
this tells you **which label each kontroll surface lands under**, the **queries** to run, and — crucially — how
to keep debugging **when the observability stack itself is down** (the degrade path). All of this is also a pane
in the Grafana **kontroll — Logs** dashboard (`dashboards/grafana/dashboards/logs.json`); the queries here and
the dashboard panels are kept in sync on purpose.

> Conventions: every log line carries the canonical, non-secret label set `{source, host, service, level,
> run_id, device}` (C12). `source` splits **internal** (the stack's own logs) from **capability** (device/service
> logs ingested via the logging capability). `run_id` correlates one operation across surfaces and is set on
> **internal** streams only (device logs are not part of a kontroll run).

## 1. Where each signal lives (the label map)

| Surface | `source` | `service` | Notes |
|---|---|---|---|
| Container stdout/stderr (every stack service) | `internal` | the **container name** (`vector`, `loki`, `semaphore`, `api`, …) | from the Docker source |
| Host journald (systemd) | `internal` | the **unit** (`docker.service`, `sshd.service`, …), else `host` | persistent journal |
| API audit (who/when/what on the API) | `internal` | `api-audit` | kept **90d**; never the credential |
| GUI audit (onboard-GUI actions) | `internal` | `gui-audit` | kept **90d** |
| Ansible run-log (playbook runs) | `internal` | `ansible` | carries the `run_id` of the run |
| Script run-logs (CLI helpers) | `internal` | the **script name**, else `script` | from `~/.local/state/kontroll/runs` |
| A device/service's own logs (logging capability) | `capability` | the **method** (`syslog_push`, …) | + `device=<module key>` (e.g. `cisco_ios`) |

## 2. Common LogQL queries (Grafana → Explore → Loki, or `logcli`)

```logql
# Everything from one operation, greppable by its correlation id (internal only):
{source="internal", run_id="20260616T143000Z"}

# A specific surface (audit trail kept 90d):
{source="internal", service=~"api-audit|gui-audit"}

# Anything wrong, across the whole stack:
{source=~".+", level=~"error|warn"}

# One device class's ingested logs (the logging capability):
{source="capability", device="cisco_ios"}

# Free-text body search (a |~ filter — a query, never a label, so no cardinality cost):
{source="internal"} |~ "(?i)backup.*fail|captured nothing"

# Volume of a chatty emitter (sizing-lever input; pairs with the dashboard "Top services" panel):
topk(10, sum by (service) (count_over_time({source="internal"} [1h])))
```

## 3. The degrade path — debugging when Loki/Vector is down

Observability must survive an observability outage. Loki or Vector being down loses log **shipping**, never the
source logs — every surface persists locally and is readable without the stack. Per the access-chain header in
`docker/services/vector.yaml` ("Fallback required: no … degrade to `docker logs`/journalctl/the flat audit files"):

| You want… | When the stack is up | When Loki/Vector is down |
|---|---|---|
| A container's logs | the `internal` stream by container name | `docker logs <name>` (bounded by daemon.json `max-size 10m ×5`) |
| Host/systemd logs | the journald `internal` stream | `journalctl -u <unit>` on the control VM |
| API audit | `{service="api-audit"}` | the flat TSV at `/var/lib/kontroll/api/audit/` |
| GUI audit | `{service="gui-audit"}` | the flat TSV at `/var/lib/kontroll/onboard-gui/audit/` (M11 — off the `/repo` code/propose clone) |
| Ansible run-log | `{service="ansible"}` | the flat log at `/var/lib/kontroll/ansible-log/` |
| Script run-logs | `{service=<script>}` | `~/.local/state/kontroll/runs/` |

Bring the stack back: `docker compose ... up -d loki vector` (or re-run `deploy-stack.yml` with `loki`/`vector`
in `stack_services`). Vector resumes from its on-disk checkpoint (the `vector-data` volume), so a restart
re-ships from where it stopped rather than re-reading everything.

## 4. Quick checks

- **No logs in Grafana at all** → confirm the Loki datasource provisioned (`uid: loki`); confirm Vector is
  Up and shipping (`docker logs vector | grep -i "Listening\|Healthcheck"`); confirm Loki `/ready`
  (`curl -s ${KONTROLL_MGMT_IP}:3100/ready`).
- **A device's syslog never arrives** (`{source="capability"}` empty) → confirm `docker/services/vector.yaml`
  publishes the mgmt-bound `:5514`, the device is configured to ship there (the `logging_syslog_push` wiring
  role), and the device can reach the collector on the mgmt VLAN.
- **Disk filling** → metrics are bounded by `PROM_RETENTION_SIZE`/`_TIME`; logs by `LOKI_RETENTION_PERIOD`
  (+ per-stream tiers); see the storage paradigm
  ([docs/reviews/2026-06-16-storage-logging/99-synthesis.md](reviews/2026-06-16-storage-logging/99-synthesis.md)).

## 5. An answer that is not about your request — 409 `repository_data`

An onboard, or an identity / capability / unit preview or apply, answers **409** with `code: repository_data`
when what stops it is the repository's own data or layout. Nothing in the request is wrong: the remedy is the
file or the layout. The sentence says where to look:

| The sentence names | The cause | The remedy |
|---|---|---|
| a device class and `modules/<key>/module.yml`, with a rule | the class's module file holds a group, secrets domain, backend or role that is not a name (lower-case letters, digits, `_`, `-`; starting with a letter or digit; at most sixty-four characters; not a word YAML reads as another type), a group Ansible reserves (`all`, `ungrouped`, `localhost`), or — at an onboard or an identity edit — is not a mapping at all | correct that file; a group rename re-homes the class (move its hosts in `instance/inventory/onboarded-<key>.yml`, rename its `group_vars` file if it has one, regenerate, dry-run the deploy) |
| a device class and a backend's credential field | a credential field is declared — in the class's `auth:` block or the backend's registry entry — with a name, or a host var, that is not an identifier, or as `inline` with no host var | correct the declaration |
| a device class whose backend needs a `role:` | the class's backend — adopted from its module file, or chosen from the backend registry — names no role, and `backend_<name>` is not a name (too long, or outside the charset); or the registry entry's own `role` is not a name | give the backend a `role:` in its registry entry (`ansible/backends/<name>/backend.yml`), or correct the one it holds; for a class that already has a module file, a `role:` there works too |
| a write target that does not resolve inside the repository | `instance/` or `modules/` is a symlink out of the write root, or `KONTROLL_WRITE_ROOT` points at another tree | make the directory a real directory of the write root (a bind mount is one), or fix the variable |

The audit log has `repository-refuse what=<kind> route=<route>` for it — not `request-refuse`, which is a field
the caller got wrong (422). `POST /onboard` has another 409, without a `code`: a drop-in that exists with
different content; that one is retried with `overwrite`.

## See also
- [logging-architecture.md](logging-architecture.md) — the layered design + the degrade-path source of truth
- [../SECURITY.md](../SECURITY.md) **C22** — the request boundary, and the refusal that is the repository's own
- [observability/logging-capability-vector.md](observability/logging-capability-vector.md) — the logging capability
- [../SECURITY.md](../SECURITY.md) **C12** — logs are a secret surface (label hygiene, mgmt-only, at-rest)
- `dashboards/grafana/dashboards/logs.json` — the same signals as a Grafana pane
