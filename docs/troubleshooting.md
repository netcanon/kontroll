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

## 6. A fleet line that is not a device-class key

`instance/fleet.yml`'s `enabled_modules` is a list of device-class keys. A line that is not one is skipped by
everything that renders and refused by what installs, and the bootstrap, a deploy and a promote each stop on it.
Every message names the ENTRY by its place among the list's entries, counted from one — comment lines, blank
lines and commented-out entries are not counted — and never repeats the line.

| What you read | Where | What it means | What to do |
|---|---|---|---|
| the task `Check that every enabled_modules entry is a device-class key (…)` fails with `gen-requirements: instance/fleet.yml: enabled_modules entry N is not a device-class key …` | the bootstrap; a deploy started part-way through its stack play (`--start-at-task`) | the play stops before anything walks the list; no generated file was rewritten | correct entry N in the node's checkout: a key is lowercase letters, digits, `_` and `-`, starting with a letter or digit. An unquoted `no`, `on`, `off`, `yes`, `~`, a number or a date is another type to YAML — the message then says "YAML does not read it as text": quote it, or rename the class. Then run the play again |
| `local-canonical: REFUSING to commit — gen-requirements: instance/fleet.yml: …` | a deploy (its canonical play, before the stack play), also the dry-run | the deploy's own commit-and-mirror would have put the line on the canonical's `main`, where it refuses every promote; nothing was committed or pushed to the canonical | the same; then deploy again |
| `REFUSED: promoting proposed/<run_id> — the proposal's tree does not resolve …`, and under it the generator's sentence, `gen-requirements: refusing to resolve the collection set — instance/fleet.yml: enabled_modules entry N …` | the `promote-proposal` job, or `kontroll-promote.py` | the tree the promote would make `main` holds such a line. If the message adds that `main` as it stands is refused as well, the line is on `main` already — and then EVERY proposal is refused, whatever it changes | if the line is in the node's checkout (a hand edit there is how it usually reached `main`): correct it there and deploy — the deploy's commit-and-mirror takes the correction to `main`. Otherwise promote a proposal that removes or corrects it: the Settings view's `disable` control stages the removal of a line that is plain, unquoted text; for any other line (`no`, `~`, a number, a quoted name, one with a slash) make the proposal by hand — in a clone of the canonical correct `instance/fleet.yml`, commit, `git push <canonical> HEAD:refs/heads/proposed/<twelve lowercase hex digits>` as the operator, and promote that id; then bring the node's checkout forward (`git fetch local && git rebase local/main`) before the next deploy. A proposal staged before the correction still carries the old line and is refused with the same sentence: discard it and propose again |
| `<generator>: ignoring: instance/fleet.yml: enabled_modules entry N …` | a generator's stderr: the `kontroll-api` or `onboard-gui` container's log (the `internal` stream) after a capability change; a generator run by hand | that generator skipped the line and rendered the rest | as above |
| `<generator>: not rendering: instance/fleet.yml holds no list of classes …`; from the API or the GUI, a 409 `repository_data` that says `instance/fleet.yml holds no list of classes …` | a generator run by hand; a capability change (telemetry, logging, backup) | the file is not a mapping, or `enabled_modules` is not a list: nothing was written and nothing was pruned, and the capability change was refused before it wrote the module file | restore the file's shape: one `enabled_modules:` key over a list; then make the capability change again |
| `gen-requirements: instance/fleet.yml is not YAML this tool can read` — with `(line L, column C)` when the parser gives a place — or `gen-requirements: instance/fleet.yml cannot be opened (…)` | a deploy, a promote (at the bootstrap its own `Load fleet profile` task fails first, with Ansible's message) | the file does not parse, or is not there to read | correct the file — at that place, when one is given |

A line that IS a class key and has no directory under `modules/` is none of these: the bootstrap stops on it
(its own lookup) and nothing else says a word (SECURITY.md C22, the limits).

## See also
- [logging-architecture.md](logging-architecture.md) — the layered design + the degrade-path source of truth
- [../SECURITY.md](../SECURITY.md) **C22** — the request boundary, the refusal that is the repository's own, and a
  fleet line that is not a class key
- [SETUP.md](SETUP.md) §3 — the fleet file
- [observability/logging-capability-vector.md](observability/logging-capability-vector.md) — the logging capability
- [../SECURITY.md](../SECURITY.md) **C12** — logs are a secret surface (label hygiene, mgmt-only, at-rest)
- `dashboards/grafana/dashboards/logs.json` — the same signals as a Grafana pane
