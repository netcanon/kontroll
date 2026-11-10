# role: backend_netcommon_cli

**Execution backend**, not a device class. Generic `check`/`backup` for *any*
`network_cli` platform that ships a cliconf plugin — uses
`ansible.netcommon.cli_command`, zero vendor-specific code. A device class adopts
it by declaring `role: backend_netcommon_cli` + `network_os` (see
`ansible/backends/netcommon_cli/backend.yml` and `scripts/galaxy.py scaffold`).

## Entrypoints (`tasks_from`)

| Entrypoint | Purpose | Blast radius |
|---|---|---|
| `check` | liveness via `cli_command` (`backend_liveness_command`, default `show version`) | none |
| `backup` | capture config via `cli_command` (`backend_backup_command`, default `show running-config`) | none (read-only) |

## Params (set by the device class / its group_vars)
- `network_os` — e.g. `cisco.ios.ios`, `arista.eos.eos` (drives the cliconf plugin).
- `backend_backup_command` / `backend_liveness_command` — optional overrides.
- `backend_backup_label` — filename prefix (default: `device_role`).

`backup_capable` gates the capture; output is `no_log` (config carries secrets).
This is the reusable target the capability classifier maps `cliconf` collections to.

A command that answers nothing (no output, or only blank lines) has given no config:
the backup is recorded `FAILED`, naming the command, which fails the run at its
summary, and nothing is written, so any earlier capture is kept. `cli_command` runs a
`show` command in a `--check` run too, so a dry-run is held to the same answer. It runs
no other command there (it would fail the host), so a backup command that does not start
with `show` (`display current-configuration`, say) is not read in a dry-run: the run says
`NOT PREVIEWED — … is not a show command`, previews an empty capture and refuses nothing.
A real run always reads.

## See also
- [../../backends/netcommon_cli/backend.yml](../../backends/netcommon_cli/backend.yml) — classifier metadata
- [../../../docs/capability-matrix.md](../../../docs/capability-matrix.md) — the design
