# role: opnsense

Device-class role for the **OPNsense** firewall (module `opnsense` — *staged*
post-cutover edge, group `edge_firewall`). Collection: `ansibleguy.opnsense`.
Secrets: `network` domain. **High blast radius — becomes the live edge at cutover.**

## Entrypoints (`tasks_from`)

| Entrypoint | Purpose | Blast radius |
|---|---|---|
| `backup` | capture `/conf/config.xml` (read-only) | none |
| `main` (TODO) | apply firewall/interface config | **WAN / all LAN** — `--check` first |

## Connection
OPNsense API preferred, SSH fallback. Creds in `secrets/network.sops.yml`. See
`group_vars/edge_firewall.yml`.

> **Backup: written, STAGED/unverified** (`backup_capable: true`) — captures
> `/conf/config.xml` via SSH (`no_log`); logic not yet verified against a live device
> (none in the active fleet). Activated by enabling the `opnsense` module in
> `instance/fleet.yml` at cutover (and dropping `fortigate`). A `config.xml` with
> nothing in it (empty, or only blank lines) is recorded `FAILED`, which fails the run
> at its summary, and is not written: any earlier capture is kept. `slurp` reads in a
> `--check` run too, so a dry-run is held to the same answer. An unreachable host is
> not refused: it is recorded not run, unreachable, and the summary and the per-device
> alert name it. Ansible calls a refused key or password unreachable too, so the
> capture play probes the host's port: where it took the connection, the host is
> recorded `FAILED` (its session did not open), which fails the run; a host behind a
> jump host is recorded `FAILED` unprobed (I-65).
