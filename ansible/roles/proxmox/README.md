# role: proxmox

Device-class role for **Proxmox VE hypervisors** (module `proxmox`, group
`hypervisors`). Collection: `community.proxmox`. Secrets: `proxmox` domain.

## Entrypoints (`tasks_from`)

| Entrypoint | Purpose | Blast radius |
|---|---|---|
| `backup` | capture guest + node config (read-only) | none |
| `main` (TODO) | apply intended VM/host config | this node / cluster |

## Connection
SSH (`ansible_user: root`) for host tasks; `community.proxmox` API token (from
`secrets/proxmox.sops.yml`) for cluster/VM tasks. See `group_vars/hypervisors.yml`.

> **Backup: implemented** (`backup_capable: true`) — guest `.conf` + node
> network/storage via SSH, `no_log`. Config capture only (not VM-image/vzdump).

The capture script prints a `### ` header before each read. A node that answers
none of them (no `pveversion`, `/etc/network/interfaces` or `/etc/pve`: not a
Proxmox node) answers its headers alone; that answer is recorded `FAILED`, which
fails the run at its summary, and nothing is written: any earlier capture is kept
(I-10). A Proxmox node whose cluster filesystem is not mounted still answers
`pveversion` and its network config (neither is under `/etc/pve`): that capture is
written, with no `storage.cfg` or guest `.conf` in it, and the one before it stays
in the capture history (an answer with any config in it is kept). A `--check` run
never runs the script (`shell` skips it), the capture write previews an empty
capture, and nothing it did not read is refused. An unreachable host is not
refused either: the summary says it was not run.
