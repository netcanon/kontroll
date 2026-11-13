# roles/logging_journald_remote

Device-side **wiring role** for the `journald_remote` logging method. Guarantees the target host's systemd
journal is **persistent** (so Vector reads a complete journal across reboots) and — opt-in — ships a **remote**
host's journal to the collector via `systemd-journal-upload`. Registry descriptor:
[`logging/journald_remote.yml`](../../../logging/journald_remote.yml).

```
# Access chain used:   control VM (mgmt VLAN) -> target host (SSH, root key ~/.ssh/ansible_ed25519)
# May break:           none on the LOCAL host; REMOTE upload adds one uploader unit
# Fallback required:   no
# Blast radius:        this host (a persistent journal dir + optionally a journal-upload unit; reversible)
```

## Scope (MVP — read before enabling the remote path)

- **Persistent journald (always on):** the default + only-by-default action. Universal and safe — it sets
  `Storage=persistent` in `journald.conf`. This is what the **control-VM-local** journald source needs (Vector
  bind-mounts the host's journal and reads it directly), so the local case is wired by this and one more deploy:
  deploy-stack binds `/var/log/journal` only where the host has it, else the volatile `/run/log/journal` (a bind to
  a missing path would make Docker create it, I-25). The role's handlers restart journald and then run
  `journalctl --flush`, which makes `/var/log/journal` at once (since systemd 252 a restart alone leaves it to the
  next boot), so re-run deploy-stack after this role first makes the control VM's journal persistent.
- **Remote upload (opt-in, `logging_journald_upload_enabled: true`):** installs + points
  `systemd-journal-upload` at `http://<collector>:<port>`. ⚠ The **collector-side receiver**
  (`systemd-journal-remote` → a Vector source, or a Vector `http_server` ingest) is **not built yet** — a
  tracked follow-up in [`docs/logging-architecture.md`](../../../docs/logging-architecture.md). Leave this OFF
  until the receiver lands; the role provisions only the host (sender) side.

## How it runs

```bash
ansible-playbook ansible/playbooks/wire-logging.yml -e role=logging_journald_remote -e target=<key> --check --diff
ansible-playbook ansible/playbooks/wire-logging.yml -e role=logging_journald_remote -e target=<key>   # then apply
```

`wire-logging.yml` runs this role only over its method's `wiring_connections`
(`logging/journald_remote.yml`); a host on another connection is refused first (SECURITY.md C4).
It probes the host (a `ping`) before the role and re-checks it after its handlers: a host that does not answer the
probe is not wired, and the run's `Wiring run report` names it, and one that answered and then not the re-check
(its wiring may not be done); the run passes for both. See docs/troubleshooting.md (I-92).

## Entrypoints

| `tasks_from:` | Does |
|---|---|
| `main` (default) | Ensure persistent journald (+ opt-in remote upload). Idempotent, `--check`-safe. |
| `backup` | Capture `journald.conf` as the rollback reference (read-only slurp → control-side). |

## Knobs

| var | default | meaning |
|---|---|---|
| `logging_journald_upload_enabled` | `false` | turn on the remote-upload block (needs the receiver) |
| `logging_collector_host` | `""` | collector mgmt IP (supplied by `wire-logging.yml`) |
| `logging_journald_upload_port` | `19532` | `systemd-journal-remote` ingest port |

See also: [`capabilities/logging.yml`](../../../capabilities/logging.yml),
[`docs/logging-architecture.md`](../../../docs/logging-architecture.md).
