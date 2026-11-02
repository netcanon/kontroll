# key-roles — age key-generation role descriptors (the keygen analogue of secret-forms)

> **This directory holds key-role SCHEMAS, never key material.** Each file declares one role an age key can
> play so the GUI can offer a guided **generate-on-box** action. **No private key is ever stored here, on the
> box by the service, or in the repo** — a generated private key is shown to the operator **exactly once** and
> then discarded. Only the PUBLIC recipient (`age1…`) is written into [`instance/.sops.yaml`](../instance/.sops.yaml).

One drop-in `key-roles/<role>.yml` per kind of key the keygen dialog can mint — the key analogue of
`secret-forms/<domain>.yml`. Adding a role = drop a file here; the shared keygen dialog (one shell, role is
data) + `scripts/kontroll/service/keygen.py` need **no** edit.

## The split (why these are GUI roles and the first control key is not)

A running GUI already holds the control key (it mounts it to decrypt), so it can mint **break-glass**,
**scoped-Semaphore**, and **rotation** keys — all of which only need to *add* a recipient and show a new
private key once. It **cannot** mint its own **first** control key or the first `GUI_PASSWORD` (it needs them
to run). That bootstrap is the CLI **`kontroll-init`** (item E), which reuses the same
`keygen.generate_keypair()` primitive. See [docs/install-from-scratch.md](../docs/install-from-scratch.md) §2.

## Schema

```yaml
role: break-glass              # the role key (== the file basename)
label: Break-glass recovery key (offline)
description: ...               # one line shown under the dialog title
order: 10                     # sort order in the picker (low = first)
placement: offline            # offline | path — where the operator should put the show-once private key
private_key_path: ~/.config/semaphore/age/keys.txt   # only for placement: path (instructional)
recipient_groups:             # the /.sops.yaml anchor group(s) the PUBLIC key is added to (additive)
  - base_recipients           #   base_recipients = the service/master domains; ops_recipients = network+proxmox
  - ops_recipients
scope_label: every secret domain   # human description of what the key will decrypt
high_blast: true              # true ⇒ already-encrypted secrets must be re-wrapped (sops updatekeys) to use it
warning: >-                   # the show-once safety text the dialog displays
  This is the only time the private key is shown ...
```

`recipient_groups` are the **anchor names** in `/.sops.yaml` (`&base_recipients`, `&ops_recipients`). The
service adds the public key to each named group's `age:` list, **additively and idempotently**, and
**parse-verifies** that no existing recipient was dropped before writing. The exact domains each group grants
are derived from `/.sops.yaml` (shown in the plan), not duplicated here.

## What generating a key does (and does not do)

1. **Mints** a fresh age keypair on the box (`age-keygen`, via `gitio.age_keygen`).
2. **Shows the private key once** (the apply response) — placement tells the operator where to put it. The
   service **never** persists it.
3. **Adds the public recipient** to the named `/.sops.yaml` group(s) — additive, idempotent, parse-verified —
   and **stages** the change as `proposed/<run_id>` (C10 propose-then-promote); nothing lands on `main` until
   the operator promotes.
4. **Returns the deferred re-wrap step** (`sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml`) — **never auto-run**
   (it re-wraps every secret; high blast). Recipient edits are additive; **retiring** an old recipient
   (rotation) is a separate, deliberate operator step.

The keygen surface needs **no decrypting age key** — minting a keypair and adding a public recipient are
public-key operations — so it fits C10's no-key posture (the decrypt-needing `updatekeys` is the operator's).

## See also
- [instance/.sops.yaml](../instance/.sops.yaml) — the authoritative recipiency rules these add to
- [secret-forms/README.md](../secret-forms/README.md) — the sibling secret-onboarding registry
- [docs/install-from-scratch.md](../docs/install-from-scratch.md) §2 — the keys + secrets boundary
- [SECURITY.md](../SECURITY.md) — C11 (guided secret + key onboarding, no-leak)
- [docs/break-glass.md](../docs/break-glass.md) — the break-glass key end to end: the choice `kontroll-init --fresh`
  asks for (its `--mint-break-glass` uses this directory's `break-glass.yml` groups), and the runbook for an instance
  created without one
