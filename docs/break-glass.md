# Break-glass: a second recipient on every secret domain

Every secret in an instance is encrypted to the age recipients `instance/.sops.yaml` names for its domain. The
**control key** (`~/.config/sops/age/keys.txt` on the control node) is one of them. A **break-glass key** is a second
recipient on every domain whose **private half is held by you, offline**: a password-manager entry plus a paper or
removable-media copy, on a machine that is not the control node. It exists for one thing: to decrypt the domain files
after the control key is lost, so that a new control key can be minted and the files re-wrapped to it.

- It is **not a copy of the control key.** A copy shares the control key's single point of failure; a second
  recipient removes it.
- Without one, **losing the control key destroys every secret in the instance** — every device credential, API
  token and service password (SECURITY.md [R-KEY-1](../SECURITY.md#accepted-risks--known-limitations)).
- kontroll can count recipients. **It cannot know whether you kept the private half.** A recipient whose private half
  was thrown away is a count of two and a recovery of zero, so the check that matters is the one you run with the key
  in hand ([Verify](#verify-the-key-opens-every-domain)).

## At install: `kontroll-init --fresh` asks

`kontroll-init --fresh` (the installer's `fresh-init`) will not create an instance until you choose. It applies the
choice before the first secret is written, so a fresh instance never needs a re-wrap.

| Choice | What happens | When |
|---|---|---|
| `--break-glass-recipient <age1…>` | the PUBLIC half of a key you made on another machine is added to every domain; its private half never touches the node | **preferred** |
| `--mint-break-glass` | kontroll mints the key on the node and shows its private half **once**, on the terminal; kontroll writes it to no file, and the installer container keeps no log. It refuses when standard output is not a terminal (a pipe, a file, a captured run), because the key would land in that log | an interactive install with no second machine at hand |
| `--no-break-glass` | every domain has ONE recipient, the control key; the run says what that costs | a scratch instance you will throw away |

To make the key for `--break-glass-recipient`, on a trusted machine that is **not** the control node, in a directory
that is not a git repository:

```bash
age-keygen -o ~/kontroll-breakglass.key        # prints "Public key: age1…"
```

Store the file's content offline (above). Copy only the `age1…` line to the control node and pass it to `fresh-init`.
Then [verify](#verify-the-key-opens-every-domain) once the first secrets exist.

## On an existing instance

An instance created before `--fresh` asked, or with `--no-break-glass`, has one recipient per domain until you run
this. It adds one more recipient to every domain and re-wraps the existing files so the new key can open them. It
changes no secret value, removes no recipient, and needs no redeploy; the control key keeps working throughout.

The commands that touch keys run in the **installer shell** — `scripts/kontroll-installer.sh shell` (`./kontroll
shell` from a launch kit) — where the repository is the working directory, the control key is mounted at
`$SOPS_AGE_KEY_FILE`, and Python, sops and age are present; the host floor (Docker + git) has none of them. Files the
shell writes are owned by root, so step 5 hands them back. After `sudo bash scripts/install-prereqs.sh
--with-host-ansible` (the legacy host toolchain) the same commands also run on the host directly, without the shell
and without the hand-back.

**Before you start.**

1. A quiet window: no deploy running, and no staged proposal pending (the GUI's Pending panel is empty). A proposal
   staged before this change and promoted after it can carry a secrets file wrapped to the old recipient set.
2. An off-box copy of the control key exists **and has been proven to decrypt**: on another machine, with a stock
   sops build, `sops -d <file> >/dev/null` succeeds for every domain file.
3. On the host, the checkout is clean, and a tag marks where you started:
   ```bash
   git status --short                              # expect no output
   git tag pre-break-glass-$(date -u +%Y%m%d)      # the rollback point
   ```

**Step 1 — make the key, off the box.** As [at install](#at-install-kontroll-init---fresh-asks): `age-keygen` on a
trusted machine, the private half stored offline, only the `age1…` line copied to the control node. (The GUI's
**Keys** dialog → "Break-glass recovery key" can mint it on the node instead: copy the private key it shows once,
promote the proposal it stages, bring it into the checkout with `git fetch local && git merge --ff-only
local/main`, and continue at step 3.)

**Step 2 — add the public half** (installer shell):

```bash
scripts/kontroll-installer.sh shell             # on the host; the lines below run inside it
python3 scripts/kontroll-init.py --break-glass-recipient age1…
git diff -- instance/.sops.yaml                  # expect: the same one line added under BOTH anchors, nothing removed
```

kontroll-init refuses — and writes nothing — when the control key file is not where it looks (it never mints one on
this path), when the value is a private key or not an age public key, or when it is the control key itself. It adds
the key under both recipient anchors, `base_recipients` and `ops_recipients`, as `key-roles/break-glass.yml` names
them; a hand edit under one anchor would leave the operational domains with one recipient.

**Step 3 — dry run** (installer shell). `sops updatekeys` prints the change it would make to each file and asks
before writing. Answer **n** to every file:

```bash
sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml
```

For every file expect one added recipient — the new key — and, after `n`, that the file was left unchanged. **Stop if
any file shows a removed recipient:** that file is wrapped to a key `instance/.sops.yaml` no longer names, and going on
would lock that key out.

**Step 4 — apply** (installer shell). The same command, answering **y** to every file. A file that is already current
says so and asks nothing.

**Step 5 — check, and hand the files back** (installer shell):

```bash
# (a) the CONTROL key still opens every domain — the plaintext is discarded, never printed
for f in instance/secrets/*.sops.yml; do sops -d "$f" >/dev/null && echo "ok   $f" || echo "FAIL $f"; done
# (b) every file now lists one more recipient
grep -c 'recipient: age1' instance/secrets/*.sops.yml
chown "$KONTROLL_OPERATOR_UID:$KONTROLL_OPERATOR_GID" instance/.sops.yaml instance/secrets/*.sops.yml
exit
```

Then [verify](#verify-the-key-opens-every-domain) with the new key, on the trusted machine.

**Step 6 — land it** (host). A scoped commit, never a blanket one, then the landing with its dry run first:

```bash
git add -- instance/.sops.yaml instance/secrets
git commit -m "feat(instance): add the break-glass recipient and re-wrap every domain"
scripts/kontroll-installer.sh playbooks/local-canonical.yml --check --diff
scripts/kontroll-installer.sh playbooks/local-canonical.yml
```

No redeploy is needed: the plaintext is unchanged, and the control key and the GUI's key still decrypt.

**Rollback.** Before step 6: `git checkout -- instance/.sops.yaml instance/secrets` (the files return to the tag).
After step 6: `git revert` the commit and land the revert the same way — the canonical refuses rewrites. A rollback is
only needed if a file was damaged: the re-wrap only adds, and the old recipient was never removed.

## Verify the key opens every domain

Until this passes for every domain you have a second recipient, not a break-glass key. On the trusted machine that
holds the private half, copy the `instance/secrets/*.sops.yml` files over (they are ciphertext) and run, with a stock
sops build:

```bash
for f in *.sops.yml; do SOPS_AGE_KEY_FILE=~/kontroll-breakglass.key sops -d "$f" >/dev/null && echo "ok   $f" || echo "FAIL $f"; done
```

Repeat it whenever you rebuild or move the instance, and after any domain is added: kontroll has no way to run this
check for you.

## See also

- [SECURITY.md](../SECURITY.md) C1 (secrets at rest) and R-KEY-1 (key loss) — what is claimed, and what is not.
- [SETUP.md](SETUP.md) §5 — wiring secrets, where this fits.
- [install-from-scratch.md](install-from-scratch.md) — the install sequence that asks the question.
- [key-roles/README.md](../key-roles/README.md) — the GUI's Keys dialog and the break-glass role it shares with
  kontroll-init.
- [instance.example/secrets/README.md](../instance.example/secrets/README.md) — the domain files and their re-wrap.
- [troubleshooting.md](troubleshooting.md) §11 — what each kontroll-init break-glass refusal means.
