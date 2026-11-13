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
with `show` (`display current-configuration`, say, or the abbreviation `sh run`) is not read
in a dry-run: the run says `NOT PREVIEWED — … is not a show command`, previews an empty
capture and refuses nothing. A real run always reads.

Under network_cli a device that takes no connection fails the read as a task, not as
unreachable. So a read that fails is rescued, and the device's port probed once: a port that
takes no TCP connection is recorded not run, unreachable, which passes the run, and the
summary and the per-device alert name the device; one that took it is recorded `FAILED`,
naming the command (the session, its credential, or the command), which fails the run. A
device reached through a jump host (a ProxyCommand or ProxyJump, or a paramiko or libssh
proxy command) is not probed, as the controller cannot reach its address: a read that fails
there is `FAILED` (I-65).

The `check` entrypoint runs `cli_command` the same way and has no such skip: under `--check`
a `backend_liveness_command` that does not start with `show` is refused by the module, and
ping.yml names the host CHECK FAILED at that task (a dry run does not fail for it). Give a class a `show` liveness command
(every shipped class has one; I-74 tracks the rest).

## See also
- [../../backends/netcommon_cli/backend.yml](../../backends/netcommon_cli/backend.yml) — classifier metadata
- [../../../docs/capability-matrix.md](../../../docs/capability-matrix.md) — the design
