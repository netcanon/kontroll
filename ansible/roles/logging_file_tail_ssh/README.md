# roles/logging_file_tail_ssh

Device-side **wiring role** for the `file_tail_ssh` logging method (the PULL case). Authorizes the collector's
**read-only, forced-command** SSH key on a remote host so Vector can tail a log file/journal without an
interactive shell. Registry descriptor: [`logging/file_tail_ssh.yml`](../../../logging/file_tail_ssh.yml).

```
# Access chain used:   control VM (mgmt VLAN) -> target host (SSH) authorizes a read-only forced-command key
# May break:           none — adds ONE authorized_keys line scoped to a forced command (no shell, no sudo)
# Fallback required:   no
# Blast radius:        this host (a restricted read-only key; reversible — remove the line)
```

## Scope (the two halves)

- This role is the **host (key) side**: it authorizes the collector's read **public** key on the remote host,
  **forced** to `journalctl -f -o json` (no shell). The matching **collector side** is the Vector `exec` source
  in [`logging/file_tail_ssh.yml`](../../../logging/file_tail_ssh.yml) (`ssh root@<host>` → the forced
  `journalctl`), with the **private** key rendered from the `logging_file_tail` SOPS domain and bind-mounted RO
  into Vector by `deploy-stack.yml`. Both halves are now built (S11) — the method PULLs a remote host's journald
  over SSH (the pull twin of `journald_remote`'s push), shaped by the same `._HOSTNAME`/`._SYSTEMD_UNIT`/
  `PRIORITY` derivation.
- Gated on `logging_tail_pubkey` — empty (the default) makes the role a safe **no-op**, so a promote that
  references it never fails for want of a key.

## How it runs

```bash
ansible-playbook ansible/playbooks/wire-logging.yml -e role=logging_file_tail_ssh -e target=<key> \
  -e logging_tail_pubkey="ssh-ed25519 AAAA… collector" --check --diff
```

`wire-logging.yml` runs this role only over its method's `wiring_connections`
(`logging/file_tail_ssh.yml`); a host on another connection is refused first (SECURITY.md C4).
It probes the host (a `ping`) before the role and re-checks it after its handlers: a host that does not answer the
probe is not wired, and the run's `Wiring run report` names it, and one that answered and then not the re-check
(its key may not be authorized); the run passes for both. See docs/troubleshooting.md (I-92).

## Entrypoints

| `tasks_from:` | Does |
|---|---|
| `main` (default) | Authorize the read-only forced-command key (idempotent `lineinfile`, `--check`-safe). |
| `backup` | Capture the prior `authorized_keys` as the rollback reference (read-only slurp → control-side). |

## Knobs

| var | default | meaning |
|---|---|---|
| `logging_tail_pubkey` | `""` | the collector's read-only public key (empty → no-op) |
| `logging_tail_user` / `logging_tail_home` | `root` / `/root` | the account whose `authorized_keys` is edited |
| `logging_tail_command` | `journalctl -f -o json` | the FORCED command the key is locked to (NDJSON for Vector) |

The forced command + `no-pty,no-*-forwarding` options mean the key can run **only** that read command — never a
shell. See also: [`capabilities/logging.yml`](../../../capabilities/logging.yml),
[`docs/logging-architecture.md`](../../../docs/logging-architecture.md).
