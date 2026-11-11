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
> **manifest** for one host (an inventory, not a full-config restore). Its read
> and write are `no_log`, as every class's capture is: `docker ps -a` prints each
> container's command line (truncated), where a credential passed as an argument
> can show, and a dry-run's diff would print it.

The manifest script prints a `### ` header before each read. An answer of the
headers alone is no manifest: it is recorded `FAILED`, which fails the run at its
summary, and nothing is written, so any earlier manifest is kept (I-10). A
`--check` run never runs the script (`shell` skips it) and previews an empty
capture.

The compose plugin is optional: a host without it is captured with an empty
stacks section (so a host whose plugin stops answering is captured without its
stacks, over a manifest that listed them; the history keeps the earlier one).
Any other read that fails (no docker, no python for the module, a daemon that is
down or refuses a read, no access to its socket) stops the script there, and the
read's exit code is recorded `FAILED` (the summary prints it and fails the run;
nothing is written, so any earlier manifest is kept). An unreachable host is
tolerated, as an unreachable raw-SSH, OPNsense or Proxmox host is: its summary
line says it was not run (I-62).
