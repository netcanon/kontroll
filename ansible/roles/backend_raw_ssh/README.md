# role: backend_raw_ssh

**Execution backend**, not a device class. Generic `check`/`backup` for bespoke
SSH appliances with no usable collection (OpenWrt over Dropbear, plain Linux,
oddball gear). Captures the output of a device-supplied command over
`ansible.builtin.raw`. Never auto-matched (S24): a class declares `backend: raw_ssh`, or the
operator onboards from the Generic SSH host search card.

## Entrypoints (`tasks_from`)

| Entrypoint | Purpose | Blast radius |
|---|---|---|
| `check` | liveness via `backend_liveness_command` (default `true`) | none |
| `backup` | capture via `backend_backup_command` (e.g. `uci export`) | none (read-only) |

## Params (set by the device class)
- `backend_backup_command` — **required** to back up (else a logged skip).
- `backend_liveness_command` — optional liveness probe.
- `backend_backup_label` — filename prefix (default `device_role`).

Output is `no_log` (captured config may carry secrets). Supply only **read-only**
commands — this backend runs whatever it's given.

## See also
- [../../backends/raw_ssh/backend.yml](../../backends/raw_ssh/backend.yml) — classifier metadata
- [../../../docs/capability-matrix.md](../../../docs/capability-matrix.md) — the design
