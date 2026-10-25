#!/usr/bin/env python3
"""kontroll-init — the from-scratch bootstrap a RUNNING GUI cannot do for itself (the keygen split's CLI half).

Run LOCALLY on a fresh control node, as the trusted operator, BEFORE the stack is up. It does the three steps
that are chicken-and-egg for the GUI (it needs them to run) or that were manual in SETUP.md §5:
  1. ensures the **control age key** (`age-keygen -o ~/.config/sops/age/keys.txt`, 0600) — the day-to-day
     decryptor the GUI/Semaphore mount read-only;
  2. adds its PUBLIC half to /.sops.yaml (base + ops groups) via the shared, parse-verified
     keygen.add_recipient (additive + idempotent — closes the manual SETUP §5.1 step);
  2b. with `--break-glass-recipient <age1…>` (or, with --fresh, `--mint-break-glass`), adds a break-glass recovery
     recipient to every domain BEFORE step 3, so a fresh node's first secrets are written to two recipients and
     never need a re-wrap. `--fresh` requires the choice (or `--no-break-glass`, which says what it costs) — the
     same never-guess rule as --trust-mode. A minted key's private half is shown once and written to no file.
     Without --fresh, a `--break-glass-recipient` run ends after this step: it mints, prints and re-encrypts no
     secret (step 3 is a new node's bootstrap, not part of adding a recipient);
  3. mints the `dashboards` BOOTSTRAP secrets if absent — the onboard-GUI password (rendered to GUI_PASSWORD;
     the genuine chicken-and-egg — the GUI can't set the password it needs to start), the Grafana admin
     password, and the C10 API token — encrypted into the dashboards SOPS domain (the recipients from step 2).

Unlike the GUI keygen (show-once, never persisted), this WRITES the control private key to its canonical 0600
path: the operator IS the trusted actor here and a running GUI mounts that file read-only, so only a local CLI
can create it. The minted GUI/Grafana passwords are shown ONCE for the operator to record. Idempotent: a second
run on a configured node makes no change. Reuses keygen.add_recipient + the secrets service (both tested).
See docs/SETUP.md §5 + docs/install-from-scratch.md §2 (the keygen split) + SECURITY.md C11.

A BRAND-NEW node instead runs `--fresh`: it scaffolds a private `instance/` overlay from the shipped
`instance.example/` skeleton, writes a `.sops.yaml` naming ONLY the freshly-minted control key (never inheriting
another instance's recipients — the default path is additive, which a fresh Joe must NOT do), and fills
`instance.yml`'s mgmt_ip/domain from flags. See docs/install-from-scratch.md (the genericization overlay), and
docs/break-glass.md for the recovery key on an instance created without one.

Usage:  python3 scripts/kontroll-init.py                                   # existing overlay: add your key (additive)
        python3 scripts/kontroll-init.py --break-glass-recipient <age1…>   # existing overlay: docs/break-glass.md
        python3 scripts/kontroll-init.py --fresh --mgmt-ip <ip> --domain <d> --trust-mode separated \
            --break-glass-recipient <age1…>        # brand-new node (or --mint-break-glass, or --no-break-glass)
"""
import argparse
import os
import re
import secrets as _sysrandom
import shutil
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `from kontroll import …` resolves
from kontroll import catalog, paths  # noqa: E402
from kontroll.service import keygen  # noqa: E402
from kontroll.service import secrets as secrets_service  # noqa: E402

AGE_KEY_FILE = os.path.join(os.path.expanduser("~"), ".config", "sops", "age", "keys.txt")
CONTROL_ANCHORS = ["base_recipients", "ops_recipients"]   # the control key must decrypt EVERY domain
BOOTSTRAP_DOMAINS = ["dashboards", "semaphore"]            # auto-mintable bootstrap domains deploy-stack's .env needs
#   dashboards: GUI/Grafana passwords + the C10 API token; semaphore: DB pw + admin login + access-key key.
#   Device-cred domains (proxmox, network, …) stay operator-provided (real creds) — minted via onboarding.
_AGE_RECIPIENT_RE = re.compile(r"age1[0-9a-z]{20,}")         # an age PUBLIC key, the shape _parse_age_public reads
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"                  # the bech32 data alphabet (BIP-173) age encodes keys in
_BREAK_GLASS_CHOICES = (
    "--break-glass-recipient <age1…> (preferred: the PUBLIC half of a key you made on another machine; its private "
    "half never touches this node), --mint-break-glass (mint it here and show its private half once), or "
    "--no-break-glass (ONE recipient per domain — for a scratch instance)")


def _parse_age_public(text):
    """The age PUBLIC recipient (age1…) from an age keys.txt — the `# public key:` comment, else the first
    age1 token. Returns None if absent. Pure (testable)."""
    m = re.search(r"#\s*public key:\s*(age1[0-9a-z]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\bage1[0-9a-z]{20,}\b", text)
    return m.group(0) if m else None


def _read_public(key_file):
    """The age public key in key_file, or None when the file is absent, unreadable or not UTF-8. Reads only — never
    mints."""
    try:
        with open(key_file, encoding="utf-8") as fh:
            return _parse_age_public(fh.read())
    except (OSError, UnicodeDecodeError):
        return None


def _is_age_recipient(s):
    """Whether `s` is an X25519 age recipient as age-keygen prints one: lower-case bech32 whose human part is `age`,
    the bech32 alphabet, a valid BIP-173 checksum and 32 bytes (58 data characters) — so a character mistyped or lost
    in a hand copy is caught before anything is written, where sops would refuse it ("invalid checksum") only at the
    first encrypt. The length catches what the checksum cannot, a `q` gained or lost just before a final `p`. A plugin
    or post-quantum recipient (human part `age1<name>`) is not one (`_is_plugin_recipient`). Pure (testable)."""
    if s != s.lower() or "1" not in s:
        return False
    hrp, data = s.rsplit("1", 1)
    if hrp != "age" or len(data) != 58 or any(c not in _BECH32 for c in data):
        return False
    gen, chk = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3), 1
    for v in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + [_BECH32.index(c) for c in data]:
        b, chk = chk >> 25, ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk == 1


def _is_plugin_recipient(s):
    """Whether `s` is shaped as a plugin or post-quantum age recipient: `age1<name>1…`, a longer human part than an
    X25519 recipient's `age`. sops encrypts to a plugin recipient only where `age-plugin-<name>` is installed, and no
    kontroll image ships one; a post-quantum one only where its sops vendors an age that knows it. Pure (testable)."""
    return isinstance(s, str) and s.startswith("age1") and s.rsplit("1", 1)[0].startswith("age1")


def _x25519_low_order(s):
    """Whether the X25519 recipient `s` (one `_is_age_recipient` takes) is a point of small order: its shared secret
    with every key is zero, so age refuses to encrypt to it ("low order point"), and no key pair has it as its public
    half. Read as age reads it (RFC 7748: the top bit masked, the value taken mod p), then run through the X25519
    ladder with a fixed clamped scalar, a multiple of 8: the result is zero exactly for such a point. Pure
    (testable)."""
    bits = [_BECH32.index(c) for c in s.rsplit("1", 1)[1][:-6]]
    u = int("".join("{:05b}".format(b) for b in bits)[:256], 2).to_bytes(32, "big")
    p, x1 = 2 ** 255 - 19, int.from_bytes(u, "little") & ((1 << 255) - 1)
    k = (int.from_bytes(bytes(range(32)), "little") & ~7 & ((1 << 255) - 1)) | (1 << 254)
    x1 %= p
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b, c, d = x2 + z2, x2 - z2, x3 + z3, x3 - z3
        aa, bb, da, cb = a * a % p, b * b % p, d * a % p, c * b % p
        e = (aa - bb) % p
        x3, z3 = (da + cb) ** 2 % p, x1 * (da - cb) ** 2 % p
        x2, z2 = aa * bb % p, e * (aa + 121665 * e) % p
    if swap:
        x2, z2 = x3, z3
    return x2 * pow(z2, p - 2, p) % p == 0


def _rule_recipients(rule):
    """The age recipients sops encrypts to under a .sops.yaml creation rule — those of its key groups when it has any
    (sops then ignores a rule-level `age:`), else the rule's own; as a list or a comma-separated string, every item
    split at its commas as sops splits it. Pure (testable)."""
    found = set()
    if not isinstance(rule, dict):
        return found
    for holder in list(rule.get("key_groups") or []) or [rule]:
        age = holder.get("age") if isinstance(holder, dict) else None
        for item in ([age] if isinstance(age, str) else age or []):
            found.update(k.strip() for k in str(item).split(",") if k.strip())
    return found


def _control_everywhere(public):
    """Whether `public` is a recipient of every rule in instance/.sops.yaml that names an age recipient — the test that
    the key kontroll-init read is this instance's control key before step 2 adds it beside the break-glass key.
    True when the file is absent or unreadable: the write step then refuses on its own."""
    try:
        with open(paths.resolve(".sops.yaml", write=True), encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return True
    rules = (doc.get("creation_rules") or []) if isinstance(doc, dict) else []
    return all(public in keys for keys in map(_rule_recipients, rules) if keys)


def _stdout_is_terminal():
    """Whether standard output is a terminal: the one place a minted break-glass private key may be shown. A pipe,
    a file or a captured run would put it in a log nobody chose to keep. The seam the tests patch."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _break_glass_refusal(args, tty, control_public, control_everywhere=True, key_file_exists=False):
    """Why the break-glass flags as given are refused, or None. Runs before anything is written, so every
    refusal leaves the tree and the key directory as they were. `tty` is whether stdout is a terminal;
    `control_public` the control key's public half if its file exists, else None; `control_everywhere` whether that
    key is a recipient of every rule in instance/.sops.yaml (`_control_everywhere`); `key_file_exists` whether
    anything is at the key file's path, so one kontroll-init cannot read a `# public key:` line from is named as
    such. Pure (testable).

    Refused: `--fresh` without a choice (kontroll never silently creates an instance whose secrets one key opens);
    `--mint-break-glass` / `--no-break-glass` without `--fresh` (an existing instance adds a recipient it made
    elsewhere); a recipient that is a PRIVATE key, not an age public key, a plugin or post-quantum one (kontroll's
    images ship no age plugin), one whose bech32 checksum or X25519 length fails, or a low-order X25519 point age
    refuses (the value is never echoed back); a recipient, or a `--fresh` run, where something is at the key file's
    path but kontroll-init cannot read a `# public key:` line from it (its key cannot be checked, and `--fresh`
    would scaffold the overlay before failing to read it); a recipient without `--fresh` where no control key file
    exists (the non-fresh path would otherwise MINT a control key and add it to every domain before reaching the
    break-glass step), or where it holds a key that is not a recipient of every rule (step 2 would add THAT key to
    every domain beside the break-glass one); a recipient equal to the control key (a copy is not a second
    recipient); and a mint whose output is not a terminal."""
    recipient = args.break_glass_recipient
    if args.fresh and recipient is None and not args.mint_break_glass and not args.no_break_glass:
        return ("ERROR: --fresh requires a break-glass choice — kontroll never silently creates an instance whose "
                "secrets only one key can open. Pass one of: %s. Without a second recipient, losing the control "
                "key destroys every secret (SECURITY.md R-KEY-1). See docs/break-glass.md." % _BREAK_GLASS_CHOICES)
    if (args.mint_break_glass or args.no_break_glass) and not args.fresh:
        return ("ERROR: --mint-break-glass and --no-break-glass are choices for --fresh. On an existing instance, "
                "add a recipient you made on another machine with --break-glass-recipient <age1…>; the procedure, "
                "with its re-wrap, is docs/break-glass.md.")
    if recipient is not None:
        if recipient.strip().upper().startswith("AGE-SECRET-KEY"):
            return ("ERROR: --break-glass-recipient was given a PRIVATE key. Pass the PUBLIC half (the age1… line "
                    "age-keygen prints) and keep the private half off this machine — and out of your shell history. "
                    "Nothing was written.")
        if not _AGE_RECIPIENT_RE.fullmatch(recipient):
            return ("ERROR: --break-glass-recipient is not an age public key (age1 then lower-case letters and "
                    "digits, as age-keygen prints it). Nothing was written.")
        if _is_plugin_recipient(recipient):
            return ("ERROR: --break-glass-recipient is a plugin or post-quantum age recipient (age1<name>1…). kontroll "
                    "takes an X25519 recipient (age1…, as age-keygen prints it): sops encrypts to a plugin recipient "
                    "only where age-plugin-<name> is installed, and kontroll's images ship none, so the first encrypt "
                    "would fail. Nothing was written.")
        if not _is_age_recipient(recipient):
            return ("ERROR: --break-glass-recipient is not a valid age public key (its checksum or length does not "
                    "match — a character was mistyped, added or lost in the copy). Nothing was written.")
        if _x25519_low_order(recipient):
            return ("ERROR: --break-glass-recipient is a low-order X25519 point: no key pair has it as its public "
                    "half, and age refuses to encrypt to it. Nothing was written.")
        if control_public is None and key_file_exists:
            return _unread_key_file(args.key_file)
        if not args.fresh and control_public is None:
            return ("ERROR: no control key at %s, and without --fresh kontroll-init never mints one — run this where "
                    "the control key is (the installer shell, `scripts/kontroll-installer.sh shell`: "
                    "docs/break-glass.md), or name it with --key-file. Nothing was written." % args.key_file)
        if not args.fresh and not control_everywhere:
            return ("ERROR: the key in %s is not a recipient of every rule in instance/.sops.yaml, so it is not this "
                    "instance's control key (where a keys.txt holds several keys, kontroll-init reads the first "
                    "`# public key:` line). Run this where the control key is, or name it with --key-file. Nothing "
                    "was written." % args.key_file)
        if recipient == control_public:
            return ("ERROR: the break-glass recipient is this node's control key — a copy of the control key is not "
                    "a second recipient. Nothing was written.")
    if args.fresh and control_public is None and key_file_exists:
        return _unread_key_file(args.key_file)
    if args.mint_break_glass and not tty:
        return ("ERROR: --mint-break-glass shows a private key once, and standard output is not a terminal — the key "
                "would land in a pipe or a log nobody chose to keep. For an unattended run, make the key on another "
                "machine and pass --break-glass-recipient <age1…> (docs/break-glass.md). Nothing was written.")
    return None


def _unread_key_file(key_file):
    """The refusal for a key file kontroll-init cannot read a `# public key:` line from. Pure (testable)."""
    return ("ERROR: kontroll-init could not read a `# public key:` line from %s (the line is missing, or the file is "
            "empty, not a file, unreadable or not UTF-8), so it cannot tell which key the file holds. If it is the "
            "control key, print its public half with `age-keygen -y %s` and add it to the file as "
            "`# public key: age1…`; otherwise name the right file with --key-file. Nothing was written."
            % (key_file, key_file))


def _show_break_glass_once(private, warning):
    """Print the minted break-glass PRIVATE key — the only time it exists outside this process — in a delimited
    block with the break-glass role's warning (the GUI dialog shows the same text), then flush. Called straight after
    its public half is written to .sops.yaml, before any later step can fail, so no recipient is left that nobody
    was shown. Never logged, never written to a file."""
    bar = "=" * 76
    print(bar)
    print("BREAK-GLASS PRIVATE KEY — shown ONCE and NOT saved anywhere. Copy it to offline storage now:")
    print()
    print(private)
    print()
    if warning:
        print(" ".join(str(warning).split()))
    print("Then prove it opens every domain, from the machine that holds it: docs/break-glass.md (verify).")
    print(bar)
    sys.stdout.flush()


def bootstrap_secret_values(form, existing):
    """Values kontroll-init must SUPPLY for a bootstrap domain: for each required field that is absent AND has
    no `generate:` spec, its declared `default` if it has one (e.g. the Semaphore admin username/email — config,
    not a secret to randomise), else a freshly generated login password. The service mints the `generate:`
    fields (e.g. the API token); an already-set field is kept. Returns {key: value}. Pure but for the CSPRNG."""
    out = {}
    for f in form.get("fields", []):
        if f["key"] in existing or f.get("generate") or not f.get("required"):
            continue
        out[f["key"]] = f["default"] if f.get("default") is not None else _sysrandom.token_urlsafe(18)
    return out


def ensure_control_key(key_file):
    """Ensure the control age key exists at key_file (age-keygen -o if absent; dir 0700, file 0600). Returns
    (public, created). Never prints the private key."""
    created = False
    if not os.path.exists(key_file):
        os.makedirs(os.path.dirname(key_file), mode=0o700, exist_ok=True)
        subprocess.run(["age-keygen", "-o", key_file], check=True, capture_output=True)
        os.chmod(key_file, 0o600)
        created = True
    with open(key_file, encoding="utf-8") as fh:
        return _parse_age_public(fh.read()), created


def scaffold_overlay():
    """Copy the shipped instance.example/ skeleton into instance/ for a BRAND-NEW node (`--fresh`). NEVER
    overwrites an existing instance/ file (idempotent / safe to re-run) and NEVER copies the secrets/ subtree —
    a fresh node mints its OWN secrets, encrypted to its OWN key; it must not inherit ciphertext. The top-level
    README is skeleton docs, not overlay config, so it is skipped. Returns (created, kept) lists of overlay-
    relative paths, or None if the tool shipped without the skeleton. Reads paths.ROOT dynamically (tmp-repo
    testable)."""
    src = os.path.join(paths.ROOT, "instance.example")
    if not os.path.isdir(src):
        return None
    dst = os.path.join(paths.ROOT, paths.INSTANCE_DIR_NAME)
    created, kept = [], []
    for root, _dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src)
        parts = [] if rel_root == "." else rel_root.split(os.sep)
        if parts and parts[0] == "secrets":
            continue                                       # never scaffold secret ciphertext (there is none)
        for fn in files:
            if rel_root == "." and fn == "README.md":
                continue                                   # skeleton docs, not overlay config
            if rel_root == "." and fn == "leak-tokens.example.txt":
                fn_out = "leak-tokens.txt"                 # the identifier-token list lands under its LIVE name
            else:                                          # (tests/_leak_guard.py reads instance/leak-tokens.txt)
                fn_out = fn
            rel = fn_out if rel_root == "." else os.path.join(rel_root, fn_out)
            target = os.path.join(dst, rel)
            relposix = rel.replace(os.sep, "/")
            if os.path.exists(target):
                kept.append(relposix)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(root, fn), target)
            created.append(relposix)
    return created, kept


def _personalize_instance_yml(mgmt_ip, domain, tls_mode, trust_mode=None):
    """Set the scaffolded instance/instance.yml's mgmt_ip / domain / frontend.tls_mode / trust_mode from the
    supplied flags (each optional — an omitted value leaves the template as-is). Value-only, line-oriented
    substitution so the file's comments survive (no YAML round-trip). `trust_mode` (F3) ships COMMENTED in the
    template (`# trust_mode: separated`), so the match tolerates an optional `# ` prefix and writes the ACTIVE
    form — uncommenting the operator's chosen posture while leaving an unchosen key commented (the never-defaulted
    contract). Returns the keys set."""
    path = paths.resolve("config/instance.yml", write=True)   # -> instance/instance.yml (overlay present post-scaffold); a WRITER, never the example
    if not os.path.exists(path):
        return []
    subs = {"mgmt_ip": mgmt_ip, "domain": domain, "tls_mode": tls_mode, "trust_mode": trust_mode}
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    set_keys = []
    for i, ln in enumerate(lines):
        for key, val in subs.items():
            if val is None:
                continue
            # `(#\s*)?` tolerates a COMMENTED template line (trust_mode ships commented); the write drops the
            # comment marker + keeps the indent, so a commented key is uncommented-and-set, an active key is replaced.
            pat = re.compile(r"^(\s*)(#\s*)?(%s:\s*)\S+" % re.escape(key))
            if pat.match(ln):
                lines[i] = pat.sub(lambda m, v=val: m.group(1) + m.group(3) + v, ln, count=1)
                if key not in set_keys:
                    set_keys.append(key)
    if set_keys:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    return set_keys


def _personalize_homepage(mgmt_ip, domain):
    """Fill the scaffolded homepage services.yaml's __MGMT_IP__ / __DOMAIN__ sentinels from the --fresh flags, so a
    brand-new node's :3000 shows the real control-plane tiles on first boot (closes the #72 stub leak where a fresh
    portal pointed every tile at RFC-5737 TEST-NET / example.com). Literal token replacement — the file is operator-
    editable YAML (not a key-value config), so a string `.replace` preserves its comments + structure with no YAML
    round-trip. Each flag optional: an omitted value leaves its sentinel for the operator to edit by hand. Returns
    the tokens replaced (empty if the file is absent — a tool shipped without the homepage skeleton, or a partial
    overlay). Idempotent: a re-run after the operator edited the file finds no sentinels and changes nothing. The
    homepage file is NOT in paths._OVERLAY_MAP, so resolve it directly under the (dynamically read) overlay root."""
    hp = os.path.join(paths.ROOT, paths.INSTANCE_DIR_NAME, "dashboards", "homepage", "services.yaml")
    if not os.path.exists(hp):
        return []
    with open(hp, encoding="utf-8") as fh:
        text = fh.read()
    done = []
    for token, val in (("__MGMT_IP__", mgmt_ip), ("__DOMAIN__", domain)):
        if val is not None and token in text:
            text = text.replace(token, val)
            done.append(token)
    if done:
        with open(hp, "w", encoding="utf-8") as fh:
            fh.write(text)
    return done


def _fresh_guard():
    """Refuse `--fresh` on an ALREADY-CONFIGURED node — instance/.sops.yaml exists, names a real recipient, and
    carries no placeholder. Protects an existing overlay (the source instance, or a re-run after setup) from
    being treated as brand-new. Returns an error string to print, or None to proceed."""
    sops = paths.resolve(".sops.yaml")
    if not os.path.exists(sops):
        return None
    with open(sops, encoding="utf-8") as fh:
        txt = fh.read()
    if keygen.FRESH_PLACEHOLDER not in txt and re.search(r"\bage1[0-9a-z]{20,}\b", txt):
        return ("ERROR: %s already names real recipients — this node is configured.\n"
                "       --fresh is for a BRAND-NEW node. To add your key to the existing overlay, run "
                "kontroll-init WITHOUT --fresh." % os.path.relpath(sops, paths.ROOT))
    return None


def main(argv):
    ap = argparse.ArgumentParser(description="Bootstrap a fresh kontroll control node (keygen split, CLI half).")
    ap.add_argument("--key-file", default=AGE_KEY_FILE, help="control age key path (default: %(default)s)")
    bg = ap.add_mutually_exclusive_group()
    bg.add_argument("--break-glass-recipient", metavar="AGE1…",
                    help="the PUBLIC half of a recovery key you generated on another machine; added as a recipient "
                         "of every domain. The private half never touches this node (preferred). Without --fresh: "
                         "adds the recipient and stops (no secret is minted) — the existing-instance procedure, "
                         "docs/break-glass.md")
    bg.add_argument("--mint-break-glass", action="store_true",
                    help="(with --fresh) mint the recovery key here and show its private half ONCE; kontroll "
                         "writes it to no file. Store it offline before you continue. Needs a terminal")
    bg.add_argument("--no-break-glass", action="store_true",
                    help="(with --fresh) create the instance with ONE recipient per domain. Losing the control key "
                         "then destroys every secret (SECURITY.md R-KEY-1). For scratch instances")
    ap.add_argument("--fresh", action="store_true",
                    help="scaffold a BRAND-NEW instance overlay from instance.example/ with YOUR key only "
                         "(never inherits another instance's recipients)")
    ap.add_argument("--mgmt-ip", dest="mgmt_ip", help="(with --fresh) this node's management IP, written to instance.yml")
    ap.add_argument("--domain", help="(with --fresh) this instance's base domain, written to instance.yml")
    ap.add_argument("--tls-mode", dest="tls_mode", choices=["byo_proxy", "self_signed", "acme"],
                    help="(with --fresh) frontend.tls_mode in instance.yml")
    ap.add_argument("--trust-mode", dest="trust_mode", choices=["solo", "separated"],
                    help="(with --fresh, REQUIRED) how this instance promotes staged proposals: 'separated' (a "
                         "human runs the promote-proposal Semaphore task — prod) or 'solo' (declares intent to "
                         "auto-promote; this edition has no auto-promoter, so it behaves like 'separated'). NEVER "
                         "defaulted — choose. See docs/trust-mode.md.")
    args = ap.parse_args(argv)

    # The break-glass choice is judged — and a minted key minted — before anything is written, so every refusal and
    # every failure to mint leaves the tree and the key directory exactly as they were. The --fresh checks come before
    # the mint too, so a run they refuse has minted nothing.
    control_public = _read_public(args.key_file) if args.break_glass_recipient or args.fresh else None
    everywhere = (_control_everywhere(control_public) if control_public and args.break_glass_recipient
                  and not args.fresh else True)
    refusal = _break_glass_refusal(args, _stdout_is_terminal(), control_public, everywhere,
                                   os.path.exists(args.key_file))
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    if args.fresh:
        # F3 — the "never a silent default" rule enforced at BIRTH (the only place a fresh overlay is created):
        # --fresh MUST name a trust posture. kontroll fail-LOUDS here (deploy-stack asserts again, belt-and-braces)
        # rather than guess whether you want human-reviewed (separated) or auto (solo) promotion. (tls_mode fail-SAFEs
        # to self_signed; trust posture is the opposite — guessing it is never safe.)
        if not args.trust_mode:
            print("ERROR: --fresh requires --trust-mode {separated|solo} — kontroll never silently picks your trust "
                  "posture (how staged proposals are promoted). 'separated' = a human reviews+promotes each (prod); "
                  "'solo' = declares intent to auto-promote (this edition has no auto-promoter, so it behaves like "
                  "'separated'). See docs/trust-mode.md.",
                  file=sys.stderr)
            return 1
        guard = _fresh_guard()
        if guard:
            print(guard, file=sys.stderr)
            return 1
    bg_role = bg_public = bg_private = None
    if args.break_glass_recipient or args.mint_break_glass:
        bg_role = catalog.key_role("break-glass")        # the groups it joins + the warning, as the GUI dialog's
        if not bg_role or not bg_role.get("recipient_groups"):
            print("ERROR: the break-glass key role (key-roles/break-glass.yml) is missing or names no recipient "
                  "groups. Nothing was written.", file=sys.stderr)
            return 1
        if args.mint_break_glass:
            pair = keygen.generate_keypair()             # age-keygen into memory only; nothing on disk (gitio.age_keygen)
            if not pair:
                print("ERROR: could not mint the break-glass key (is age-keygen installed?). Nothing was written.",
                      file=sys.stderr)
                return 1
            bg_public, bg_private = pair["public"], pair["private"]
        else:
            bg_public = args.break_glass_recipient

    # 0. --fresh: scaffold a BRAND-NEW overlay (YOUR key only) before the keygen steps -------------------
    if args.fresh:
        sc = scaffold_overlay()
        if sc is None:
            print("ERROR: --fresh needs the shipped instance.example/ skeleton, which is absent.", file=sys.stderr)
            return 1
        created_files, kept = sc
        print("==> instance overlay scaffolded from instance.example/ (%d new, %d kept)"
              % (len(created_files), len(kept)))
        set_keys = _personalize_instance_yml(args.mgmt_ip, args.domain, args.tls_mode, args.trust_mode)
        if set_keys:
            print("    instance.yml set: %s" % ", ".join(set_keys))
        else:
            print("    edit instance/instance.yml: set mgmt_ip + domain (left as placeholders).")
        hp_done = _personalize_homepage(args.mgmt_ip, args.domain)
        if hp_done:
            print("    homepage tiles personalized: %s" % ", ".join(hp_done))
        else:
            print("    edit instance/dashboards/homepage/services.yaml: replace __MGMT_IP__/__DOMAIN__ sentinels.")

    # 1. control age key ---------------------------------------------------------------------------------
    public, created = ensure_control_key(args.key_file)
    if not public:
        print("ERROR: could not read the age public key from %s" % args.key_file, file=sys.stderr)
        return 1
    print("==> control age key: %s  (%s)" % (public, "GENERATED" if created else "already present"))
    print("    private: %s (0600) — keep a copy in your password manager; it decrypts every secret." % args.key_file)

    # 2. .sops.yaml recipient — SEED (fresh: your key only, replace) vs ADD (existing overlay: additive) ----
    if args.fresh:
        w = keygen.seed_fresh_sops(public)               # REPLACE the placeholder — never inherit other recipients
    else:
        w = keygen.add_recipient(public, CONTROL_ANCHORS)   # additive, parse-verified, idempotent
    if w["error"]:
        print("ERROR: could not write the control recipient to .sops.yaml: %s" % w["error"], file=sys.stderr)
        return 1
    if args.fresh:
        status = "SEEDED (your key only)" if w["changed"] else "already seeded"
    else:
        status = "ADDED" if w["changed"] else "already present"
    print("==> .sops.yaml recipient: %s (domains: %s)" % (status, ", ".join(w["recipient_domains"]) or "(none)"))
    if w["changed"] and not args.fresh:
        print("    NEXT: re-wrap any pre-existing secrets to this recipient: %s" % keygen.SOPS_UPDATEKEYS)

    # 2b. break-glass recipient — added BEFORE the first secret is written, so a fresh node never needs a re-wrap ----
    if bg_public:
        if bg_public == public:                          # belt: the check above could compare only a key that existed
            print("ERROR: the break-glass recipient is this node's control key — a copy of the control key is not a "
                  "second recipient. It was not added.", file=sys.stderr)
            return 1
        w2 = keygen.add_recipient(bg_public, bg_role["recipient_groups"])   # additive, parse-verified, idempotent
        if w2["error"]:
            print("ERROR: could not add the break-glass recipient to .sops.yaml: %s. It was not added%s; add one "
                  "later per docs/break-glass.md." % (w2["error"], ", and the minted key is discarded unshown"
                                                      if bg_private else ""), file=sys.stderr)
            return 1
        print("==> break-glass recipient: %s (%s; domains: %s)"
              % (bg_public, "ADDED" if w2["changed"] else "already present",
                 ", ".join(w2["recipient_domains"]) or "(none)"))
        if bg_private:
            _show_break_glass_once(bg_private, bg_role.get("warning"))   # the ONLY place the private half is emitted
        if w2["changed"] and not args.fresh:
            print("    NEXT: re-wrap the existing secrets to it — the dry run, the apply and the checks are "
                  "docs/break-glass.md: %s" % keygen.SOPS_UPDATEKEYS)
    elif args.fresh and args.no_break_glass:
        print("==> NO break-glass recipient (--no-break-glass): every domain has ONE recipient, the control key. "
              "Losing %s destroys every secret in this instance (SECURITY.md R-KEY-1). Add one before real "
              "secrets: docs/break-glass.md." % args.key_file)
    if bg_public and not args.fresh:
        # An existing instance's break-glass run adds a recipient and nothing else: step 3 would mint, print and
        # re-encrypt an absent bootstrap secret the runbook says this procedure does not touch.
        print("\n==> kontroll-init done: the break-glass recipient only (no secret was minted or changed). Next: the "
              "re-wrap's dry run, the apply, the checks and the landing — docs/break-glass.md, steps 3 to 6.")
        return 0

    # 3. bootstrap secrets for the auto-mintable domains — deploy-stack's docker/.env render REQUIRES the
    #    dashboards (GUI/Grafana pw + C10 token) AND semaphore (DB pw + admin login + access-key) domains BEFORE
    #    the GUI (where the remaining, operator-provided secrets are entered) can come up. ------------------
    for domain in BOOTSTRAP_DOMAINS:
        form = catalog.secret_form(domain)
        if form is None:
            print("WARNING: no '%s' secret-form descriptor; skipping its bootstrap secrets." % domain)
            continue
        existing = secrets_service._existing_keys(domain)
        field_keys = {f["key"] for f in form.get("fields", [])}
        if field_keys and field_keys <= existing:
            # Fully provisioned already — skip (fast no-op + a clear log line). Since #141 build_secret_plan no
            # longer re-mints an already-set `generate:` field on a blank/no-`regenerate` call (it KEEPS it), so a
            # re-run is idempotent either way; this stays as the explicit "nothing to do" path. (Partial domains
            # still fall through below to mint only the ABSENT fields.)
            print("==> %s bootstrap secrets: already set (no change)" % domain)
            continue
        supplied = bootstrap_secret_values(form, existing)
        plan = secrets_service.build_secret_plan(domain, supplied)
        if plan["error"]:
            print("ERROR: %s plan: %s" % (domain, plan["error"]), file=sys.stderr)
            return 1
        applied = secrets_service.apply_secret_plan(plan)
        if applied["error"]:
            print("ERROR: could not write the %s domain: %s (is the age key present to encrypt?)"
                  % (domain, applied["error"]), file=sys.stderr)
            return 1
        if applied["changed"]:
            print("==> %s bootstrap secrets written (encrypted to every recipient .sops.yaml names). "
                  "SAVE these now:" % domain)
            for v in plan["view"]:
                if v["source"] in ("provided", "generated"):
                    shown = supplied.get(v["key"])
                    print("    %-28s %s" % (v["key"] + ":", shown if shown else "(generated, stored — used by the deploy)"))
        else:
            print("==> %s bootstrap secrets: already set (no change)" % domain)

    print("\n==> kontroll-init done. Next:")
    print("    - SAVE the GUI/Grafana passwords above (your onboard-GUI + Grafana logins).")
    if bg_private:
        print("    - Store the break-glass key shown above OFFLINE, then prove it opens every domain from the machine")
        print("      that holds it: docs/break-glass.md.")
    elif bg_public:
        print("    - Prove the break-glass key opens every domain, from the machine that holds it: docs/break-glass.md.")
    elif args.fresh:
        print("    - NO break-glass recipient: add one before real secrets — docs/break-glass.md.")
    else:
        print("    - A break-glass recovery key, if this instance has none: docs/break-glass.md.")
    print("    - A scoped Semaphore key: the GUI 'Keys' dialog (after deploy).")
    print("    - Enter the remaining service secrets: the GUI 'Secrets' dialog, or `sops instance/secrets/<d>.sops.yml`.")
    print("    - Next: edit instance/fleet.yml and instance/inventory/hosts.yml, then the dry-run —")
    print("      `scripts/kontroll-installer.sh check` (`./kontroll check` from a launch kit).")
    print("      The whole sequence is the quickstart at the top of docs/install-from-scratch.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
