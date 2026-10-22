# Operational Semaphore templates — the drop-in registry

Each `*.yml` here declares one **manually-run** Semaphore task template (project *kontroll*).
`scripts/configure-semaphore.py` loads this directory (sorted) and `upsert`s one Semaphore
template per file — so **adding an operational task is a new file here, never an edit to the
configure script** (the "add an X" doctrine; CLAUDE.md). Re-run `configure-semaphore.py` to apply.

This is the **operational** (on-demand) half. The **scheduled** (cron) half is generated separately
into `../schedules.generated.yml` from each module's `backup:` block (`scripts/gen-backup.py`) — do
not hand-edit that file. Two registries, one rule: templates are data, not hub edits.

## Schema

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Semaphore template name (the match key for idempotent upsert). |
| `playbook` | yes | Repo-relative playbook path, e.g. `ansible/playbooks/ping.yml`. Must exist (a test asserts it). |
| `description` | yes | Shown in the Semaphore UI. |
| `arguments` | no | Extra ansible args as a JSON-serialisable list, e.g. `["-e", "k=v"]`. |
| `survey_vars` | no | List of `{name, title, description, type, required}` — prompted at launch and passed to the playbook as extra-vars. `type: ""` = free text. |

Inventory / repository / environment are supplied by the configure script (the project-wide
`common` set), so a template file only declares what is template-specific.

## The registered templates

| File | Template | What it does |
|---|---|---|
| `ping-fleet.yml` | `ping-fleet` | Per-class fleet liveness (`ping.yml`). |
| `promote-proposal.yml` | `promote-proposal` | **The C10 "approve" task.** Fast-forwards `main` into a staged `proposed/<run_id>` (survey var `run_id`, which takes `<run id>@<commit id>` — the Pending panel's copy control) via the tested FF-only promote. The network service can only PROPOSE; this is the trusted promote, reachable only by a Semaphore admin on the mgmt VLAN — never the API token. Requires the gated rw canonical mount (deploy-stack `api_privileged`). See [docs/privileged-mutation-enablement.md](../../../docs/privileged-mutation-enablement.md), SECURITY.md C10, and [ansible/playbooks/promote.yml](../../../ansible/playbooks/promote.yml). |
| `reload-observability.yml` | `reload-observability` | **Tier-1 enact (item F).** Reloads Prometheus config without a restart — POSTs `/-/reload` (HTTP, no docker socket) so a freshly-promoted scrape target/job goes live. See [ansible/playbooks/reload-observability.yml](../../../ansible/playbooks/reload-observability.yml). |
| `install-node-exporter.yml` | `install-node-exporter` | **Tier-1 enact (item F).** Installs the node_exporter host-agent (optional `target` survey var; empty = all eligible hosts). State-changing — tick Semaphore's **Dry Run** for the `--check` pass first, then run to apply. |

## Extending — add an operational task

1. Drop a `<name>.yml` here with `name` + `playbook` + `description` (+ `survey_vars` if it takes input).
2. Make sure the playbook exists and carries the access-chain header if it is state-changing.
3. Re-run `python3 scripts/configure-semaphore.py` on the control node.

## Errors

| Symptom | Cause | Fix |
|---|---|---|
| `configure-semaphore.py` KeyError on `name`/`playbook` | a template file is missing a required key | add it (see schema). |
| Test failure "playbook does not exist" | `playbook:` points at a missing/typo'd path | fix the path; it is repo-relative. |
| Survey var not reaching the playbook | wrong key under `survey_vars` (`name` must match the `{{ var }}`) | align the survey var `name` with the playbook variable. |
| `promote-proposal` job fails "read-only file system" | canonical mounted `:ro` (not armed) | deploy with `api_privileged: true` incl. `semaphore` so the canonical mounts `:rw` (KONTROLL_CANONICAL_MODE). |

## See also
- [../schedules.generated.yml](../schedules.generated.yml) — the generated scheduled (backup) templates.
- [scripts/configure-semaphore.py](../../../scripts/configure-semaphore.py) — the loader/registrar.
- [docs/privileged-mutation-enablement.md](../../../docs/privileged-mutation-enablement.md) — C10 propose-then-promote.
