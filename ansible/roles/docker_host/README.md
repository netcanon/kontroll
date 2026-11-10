# role: docker_host

Device-class role for **Docker hosts** (module `docker_host`, group
`docker_hosts` — e.g. a NAS and a reverse-proxy pair). Collection: `community.docker`. Secrets:
`compute` domain.

## Entrypoints (`tasks_from`)

| Entrypoint | Purpose | Blast radius |
|---|---|---|
| `backup` | container/stack manifest (read-only, **opt-in per host**) | none |
| `main` (TODO) | converge container stacks | this host |

## Connection
SSH (`ansible_user: root`); container ops via `community.docker`. See
`group_vars/docker_hosts.yml`.

> **Backup: not capable by default** (`backup_capable: false`) — a docker host's
> real state is app-managed appdata/compose outside this seam, so the class
> records a logged skip instead of forcing an empty capture. Set
> `backup_capable: true` in `host_vars/<host>.yml` to capture a container/stack
> **manifest** for one host (an inventory, not a full-config restore).

The manifest script prints a `### ` header before each read. An answer of the
headers alone is no manifest: it is recorded `FAILED`, which fails the run at its
summary, and nothing is written, so any earlier manifest is kept (I-10). A
`--check` run never runs the script (`shell` skips it) and previews an empty
capture.
