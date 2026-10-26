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
| `--mint-break-glass` | kontroll mints the key on the node and shows its private half **once**, on the terminal; kontroll writes it to no file, and the installer keeps no container log of `fresh-init`. It refuses when standard output is not a terminal (a pipe, a file, a captured run), because the key would land in that log. A terminal you record yourself (`script`, a multiplexer's log) keeps it | an interactive install with no second machine at hand |
| `--no-break-glass` | every domain has ONE recipient, the control key; the run says what that costs | a scratch instance you will throw away |

To make the key for `--break-glass-recipient`, on a trusted machine that is **not** the control node, in a directory
that is not a git repository:

```bash
age-keygen -o ~/kontroll-breakglass.key        # prints "Public key: age1…"
```

Store the file's content offline (above). Copy only the `age1…` value (after `Public key:`) to the control node
and pass it to `fresh-init`.
Then [verify](#verify-the-key-opens-every-domain) once the first secrets exist.

## On an existing instance

An instance created before `--fresh` asked, or with `--no-break-glass`, has one recipient per domain until you run
this. It adds one more recipient to every domain and re-wraps the existing files so the new key can open them. It
changes no secret value, removes no recipient, and needs no redeploy; the control key keeps working throughout.

The commands that touch keys run in the **installer shell** — `scripts/kontroll-installer.sh shell` (`./kontroll
shell` from a launch kit) — where the repository is the working directory, the control key is mounted at
`$SOPS_AGE_KEY_FILE`, and Python, sops and age are present; the host floor (Docker + git) has none of them.
kontroll-init and sops rewrite existing files in place, so they keep their owner; only a file the shell creates is
root's, and step 5's hand-back is the belt for that case. After `sudo bash scripts/install-prereqs.sh
--with-host-ansible` (the legacy host toolchain) the same commands also run on the host directly, without the shell
and without the hand-back.

**Before you start.**

1. A quiet window: no deploy running, and no staged proposal pending (the GUI's Pending panel is empty). A proposal
   staged before this change and promoted after it can carry a secrets file wrapped to the old recipient set.
2. An off-box copy of the control key exists **and has been proven to decrypt**: on another machine, with a stock
   sops build, `sops -d <file> >/dev/null` succeeds for every domain file.
3. On the host, the checkout is level with the canonical and clean, and a tag marks where you started:
   ```bash
   git fetch local && git merge --ff-only local/main   # level with the canonical; refused? troubleshooting.md §10
   git log --oneline local/main..HEAD                  # expect no output: nothing here the canonical lacks
   git status --short                                  # expect no output
   git tag pre-break-glass-$(date -u +%Y%m%d)          # the rollback point
   ```
   A re-wrap committed on a stale base is refused at the landing, and a rebase cannot merge ciphertext that a
   promoted proposal re-encrypted.

**Step 1 — make the key, off the box.** As [at install](#at-install-kontroll-init---fresh-asks): `age-keygen` on a
trusted machine, the private half stored offline, only the `age1…` value (after `Public key:`) copied to the
control node. (The GUI's
**Keys** dialog → "Break-glass recovery key" can mint it on the node instead: copy the private key it shows once,
promote the proposal it stages, bring it into the checkout with `git fetch local && git merge --ff-only
local/main`, and continue at step 3.)

**Step 2 — add the public half** (installer shell):

```bash
scripts/kontroll-installer.sh shell             # on the host; the lines below run inside it
python3 scripts/kontroll-init.py --break-glass-recipient age1…
git diff -- instance/.sops.yaml                  # expect: + the break-glass key under each anchor, nothing else
git status --short                               # expect: only instance/.sops.yaml
```

kontroll-init refuses — and writes nothing — when the control key file is not where it looks (it never mints one on
this path), has no `# public key:` line it can read, or holds a key that is not a recipient of every rule (so not this
instance's control key), or holds a `# public key:` line that is not the public half of the key age reads from the file,
or from which age reads no key (or where `age-keygen` gives no answer); when the value is a private key, is not an age
public key, is a plugin recipient (`age1<name>1…`: kontroll's images ship no age plugin) or a post-quantum one
(`age1pq1…`: it needs a sops whose age knows it on every machine that encrypts or decrypts, the trusted machine's
included), fails its checksum or length (a character mistyped, added or lost in the copy, an `l` read as `1` among them)
or has padding bits set (a value no age-keygen prints), or is a low-order point age refuses; or when it is the control
key itself. It adds the key under both recipient anchors, `base_recipients` and `ops_recipients`, as
`key-roles/break-glass.yml` names them; a hand edit under one anchor would leave the operational domains with one
recipient. Then it stops: it mints, prints and changes no secret, and names step 3.

**Step 3 — dry run** (installer shell). `sops updatekeys` prints the change it would make to each file and asks
before writing. Answer **n** to every file. The glob matches the name kontroll gives each domain file,
`instance/secrets/<domain>.sops.yml`; a file named `.sops.yaml` by hand is neither re-wrapped nor verified until it is
renamed.

```bash
sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml
```

For every file expect one added recipient — the new key — and, after `n`, that the file was left unchanged. **Stop if
any file shows a removed recipient, or an added one other than the break-glass key:** a removed one is a key
`instance/.sops.yaml` no longer names, and going on would lock it out; any other added key would open every file once
you apply.

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

From a launch kit the landing is `./kontroll playbooks/local-canonical.yml --check --diff`, then the same without
`--check --diff`.

No redeploy is needed: the plaintext is unchanged, and the control key and the GUI's key still decrypt.

**Rollback.** Before step 6: `git checkout -- instance/.sops.yaml instance/secrets` (the files return to the tag).
After step 6: `git revert` the commit and land the revert the same way — the canonical refuses rewrites. A rollback is
only needed if a file was damaged: the re-wrap only adds, and the old recipient was never removed.

## Verify the key opens every domain

Until this passes for every domain you have a second recipient, not a break-glass key. On the trusted machine that
holds the private half — in a Linux or macOS shell, with a stock sops build and `age-keygen` (it comes with age) —
copy the `instance/secrets/*.sops.yml` files over (they are ciphertext) and run the block below in their directory.
Set `BG` to the file that holds the private half (on the mint or GUI path, paste the `AGE-SECRET-KEY-1…` line it
showed into that file first, alone on its line) and `BGPUB` to the break-glass recipient as the control node showed
it: the `age1…` on kontroll-init's `==> break-glass recipient:` line or, on the GUI path, the Keys dialog's
`public recipient:`. `BGPUB` is the check's one anchor: take it from the control node, never from the key file you
are testing, and check it is not the `==> control age key:` value. `ISO` is an empty home, so sops sees only the key
the block names. The block holds no comment, so an interactive zsh (macOS's shell) runs it too; or save it to a
file and run `bash verify.sh`, which you can keep for the next time.

sops uses every key it can find — `SOPS_AGE_KEY_FILE`, `~/.config/sops/age/keys.txt` (where a proven copy of the
control key may live), `SOPS_AGE_KEY`, `SOPS_AGE_KEY_CMD`, an SSH key, a key service — so a plain
`SOPS_AGE_KEY_FILE=… sops -d` passes on the control key alone. The block first checks that `mktemp -d` made it a
directory and that sops runs as the block runs it (no environment, that new directory its home), then that `BG` holds
one key and that it is the break-glass key (`age-keygen -y` prints one line per key). Then, for each file, it checks
that the file is wrapped to that key, runs sops with an empty home and no environment but the key it names, and proves
that a key which is not a recipient fails on that file before it tries the break-glass key:

```bash
BG="$HOME/kontroll-breakglass.key"
BGPUB="age1…"
ISO="$(mktemp -d)"
v() { env -i PATH="$PATH" HOME="$ISO" XDG_CONFIG_HOME="$ISO" SOPS_AGE_KEY_FILE="$1" sops -d "$2" >/dev/null 2>&1; }
if [ ! -d "$ISO" ]; then
  echo "BROKEN: no temporary directory here (mktemp -d made none)"
elif ! env -i PATH="$PATH" HOME="$ISO" XDG_CONFIG_HOME="$ISO" sops --help >/dev/null 2>&1; then
  echo "BROKEN: no sops runs here as the check runs it (a stock sops build on PATH; a shell function, an alias or a version-manager shim does not count)"
elif ! PUB="$(age-keygen -y "$BG" 2>/dev/null)"; then
  echo "BROKEN: age-keygen could not read a key from $BG (install age, or check the file: one AGE-SECRET-KEY-1… line)"
elif [ "$PUB" != "$BGPUB" ]; then
  echo "BROKEN: $BG is not the one key whose public half is $BGPUB"
else
  age-keygen -o "$ISO/not-a-recipient.key" 2>/dev/null
  for f in *.sops.yml; do
    if ! grep -q "recipient: $BGPUB" "$f"; then echo "FAIL $f (not wrapped to $BGPUB)"
    elif v "$ISO/not-a-recipient.key" "$f"; then echo "BROKEN $f: a key other than the one named opened it"
    elif v "$BG" "$f"; then echo "ok   $f"
    else echo "FAIL $f"; fi
  done
fi
rm -rf "$ISO"
```

Expect `ok` for every file and no `BROKEN` line.

- `FAIL … (not wrapped to …)`: the re-wrap did not reach that file. Run it again (steps 3 and 4).
- A bare `FAIL`: the file names the break-glass key and that key does not open it. First run `sops --version`: a sops
  older than 3.7 reads no age key, so install a current stock build and run the block again. If the `FAIL` stays, the
  file's break-glass stanza is damaged, and a re-wrap leaves it as it is (`already up to date`): rotate that file's
  data key on the node, in the installer shell (`sops -r -i instance/secrets/<domain>.sops.yml`), hand it back and
  land it as steps 5 and 6 do, then verify again.
- `BROKEN: no temporary directory here`: `mktemp -d` made no directory (the block keeps sops' empty home and its
  throwaway key in one); the line above it says why. Most often `TMPDIR` names a directory that is missing or not
  yours: run the block with `TMPDIR` unset, or set to a directory you can write. With no `mktemp` on `PATH`, install
  coreutils (macOS has it).
- `BROKEN: no sops runs here`: this machine cannot check anything yet. The block runs sops with no environment and an
  empty home, so a shell function or an alias named `sops`, or a version-manager shim that needs your own home or a
  variable of its own, does not count: put a stock sops build on `PATH`.
  `I=$(mktemp -d); env -i PATH="$PATH" HOME="$I" XDG_CONFIG_HOME="$I" sops --help >/dev/null && echo runs; rm -rf "$I"`
  should then print `runs` (it runs sops as the block does: no environment, an empty home of its own). A `PATH`
  entry written `~/bin` counts for bash but not for this check: write it `$HOME/bin`.
- A `BROKEN` line about `$BG`: the file is not the break-glass key alone (a copy of the control key or a keys.txt
  holding both, so the check would prove the wrong key; or a key line with something before it, which `age-keygen`
  cannot read); or `BGPUB` is not the break-glass recipient (left as `age1…`, with a stray space or carriage return,
  or set to the control key's).
- A `BROKEN` line about a domain file: sops found a key the block did not empty out (a newer sops may read another
  place), and that file's result proves nothing until it is gone.

Repeat it whenever you rebuild or move the instance, and after any domain is added: kontroll has no way to run this
check for you.

## See also

- [SECURITY.md](../SECURITY.md) C1 (secrets at rest) and R-KEY-1 (key loss) — what is claimed, and what is not.
- [SETUP.md](SETUP.md) §5 — wiring secrets, where this fits.
- [install-from-scratch.md](install-from-scratch.md) — the install sequence that asks the question.
- [key-roles/README.md](../key-roles/README.md) — the GUI's Keys dialog and the break-glass role it shares with
  kontroll-init.
- [instance.example/secrets/README.md](../instance.example/secrets/README.md) — the domain files and their re-wrap.
- [troubleshooting.md](troubleshooting.md) §10 — a refused fast-forward or landing; §11 — what each
  kontroll-init break-glass refusal means.
