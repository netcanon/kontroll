# role: routeros

Device-class role for the **MikroTik RouterOS** switch (module `routeros` —
*staged* post-cutover core, group `core_switch`). Collection:
`community.routeros`. Secrets: `network` domain.

## Entrypoints (`tasks_from`)

| Entrypoint | Purpose | Blast radius |
|---|---|---|
| `backup` | `/export show-sensitive` capture (read-only) | none |
| `main` (TODO) | apply bridge/VLAN config | this switch / VLANs |

## Connection
RouterOS API / SSH. Creds in `secrets/network.sops.yml`. See
`group_vars/core_switch.yml`.

> **Backup: written, STAGED/unverified** (`backup_capable: true`) —
> `/export show-sensitive` via `community.routeros` (`no_log`); not yet verified
> against a live device (none in the active fleet). Activated by enabling
> `routeros` in `instance/fleet.yml` at cutover (dropping `cisco_ios`). **No serial
> console** — recovery is MAC-Winbox / reset+import. `community.routeros.command`
> has no check mode (from the collection's 3.0.0): a `--check` run skips the export
> and the capture write previews an empty capture. A real run's export with nothing
> in it (no output, or only blank lines) is recorded `FAILED`, which fails the run at
> its summary, and is not written: any earlier capture is kept.
