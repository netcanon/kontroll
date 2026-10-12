"""The plaintext guard against a document the REAL sops binary wrote.

WHY: scripts/lib/plaintext-guard.sh decides "is this file fully encrypted" from the shape of its lines — the
ciphertext token, the encrypted comment, an empty value written back empty, the metadata block. That shape is
sops's, not ours. tests/unit/test_plaintext_guard.py pins the predicate against a hand-written document in that
shape; this test pins the shape itself, against the binary: if a sops release changes how it writes a value, a
comment or its metadata, the guard would start refusing every healthy secret file — and stop every deploy that
has a new or edited one. The test fails first.

It needs `sops` on PATH and nothing else: encryption needs only a recipient (an age PUBLIC key), never a private
key, and nothing here is decrypted. tests/validate.sh runs this file as a step of its own behind `have sops`, and
that step fails when a test here was SKIPPED — so under `--strict` (CI) neither a missing binary nor a skip can
turn it green; in the plain pytest job, where sops is not installed, it is skipped.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(ROOT, "scripts", "lib", "plaintext-guard.sh")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not all(shutil.which(tool) for tool in ("sops", "sh", "git", "awk")),
                       reason="needs the sops binary (and sh, git, awk) on PATH — tests/validate.sh runs it where "
                              "sops is installed"),
]

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32(hrp, data):
    """`data` (bytes) as a Bech32 string under the prefix `hrp` — the encoding of an age recipient."""
    values, acc, bits = [], 0, 0
    for byte in data:
        acc, bits = (acc << 8) | byte, bits + 8
        while bits >= 5:
            bits -= 5
            values.append((acc >> bits) & 31)
    if bits:
        values.append((acc << (5 - bits)) & 31)
    check = 1
    for value in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + values + [0] * 6:
        top = check >> 25
        check = (check & 0x1ffffff) << 5 ^ value
        for bit, generator in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)):
            check ^= generator if (top >> bit) & 1 else 0
    check ^= 1
    return hrp + "1" + "".join(_BECH32[v] for v in values + [(check >> 5 * (5 - i)) & 31 for i in range(6)])


# The recipient the fixtures are encrypted to. Any 32 bytes are an X25519 public key, and nobody holds the private
# half of an arbitrary one — so nothing encrypted to it can be read by anyone, which is all an is-it-encrypted test
# needs. Computed rather than written out: this file then holds nothing that reads as a key.
RECIPIENT = _bech32("age", bytes(range(1, 33)))

# Every value kind a secrets file of this tool can hold, and the layouts sops has to write back: a comment, a
# comment that is only `#` (a spacer), a note on a value's line — after a string, an empty string and a null —,
# an empty string, null, numbers, a boolean, a multi-line string, a list with empty items, a nested map, a list
# of maps, and keys with characters a naive key pattern trips over (colons — an IPv6-named host — dots, a dash).
# The spacer, the notes and the empty list items are here because the guard refused all three until this file was
# run against the binary: sops writes each back in a shape nobody had guessed.
PLAIN = """# a comment that carries context, and so is encrypted too
#
edge_fw_password: correct-horse-battery-staple   # rotated in the autumn
api_token: "token-token-token"
empty_value: ""   # a note after an empty string
null_value: null   # a note after a null
port: 8443
ratio: 1.5
enabled: true
2001:db8::1_password: v6-host-secret
sw-01.lab.example.test_enable: another
ssh_private_key: |
    line one of a long value
    line two of a long value
community_strings:   # a note after a key that opens a block
    - public-ro   # a note after a list item
    - private-rw
    - ""
    - null
    - []
    - {}
nested:
    inner:
        deepest: bottom

    #
    # a comment inside a block, after a blank line and a spacer
    sibling: beside
servers:
    - name: first
      token: aaa
    - name: second
      token: bbb
"""


def _p(path):
    return str(path).replace("\\", "/")


def _sops_env():
    """The environment sops is run in: the caller's without any `SOPS_*` variable. A key service somebody has
    configured in their shell (a KMS ARN, a PGP fingerprint, a Vault URI) would make sops try it as well — a
    network call to an account, from a test."""
    return {name: value for name, value in os.environ.items() if not name.startswith("SOPS_")}


def _guard(tree, *flags):
    return subprocess.run(["sh", _p(GUARD)] + list(flags) + [_p(tree)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@pytest.fixture
def encrypted(tmp_path):
    """A scratch repository holding instance/secrets/network.sops.yml, encrypted in place by the installed sops."""
    tree = tmp_path / "tree"
    secrets = tree / "instance" / "secrets"
    secrets.mkdir(parents=True)
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True, capture_output=True)
    target = secrets / "network.sops.yml"
    target.write_text(PLAIN, encoding="utf-8", newline="\n")
    done = subprocess.run(["sops", "--encrypt", "--age", RECIPIENT, "--input-type", "yaml", "--output-type", "yaml",
                           "--in-place", _p(target)], capture_output=True, text=True, env=_sops_env(),
                          cwd=str(tmp_path))
    assert done.returncode == 0, "sops could not encrypt the fixture: %s" % done.stderr
    return tree, target


def test_the_predicate_accepts_what_the_installed_sops_writes(encrypted):
    """A document holding every value kind and layout a secrets file can have, encrypted by the installed sops
    binary, passes the guard in both modes — and not one of its plaintext values is left in the file. Guards the
    predicate drifting from the real output format (a sops bump that changes the value token, the comment form,
    where the metadata block sits or how an empty value is written), which would make the gate refuse every
    healthy secret file and stop the deploy."""
    tree, target = encrypted
    text = target.read_text(encoding="utf-8")
    for plaintext in ("correct-horse-battery-staple", "token-token-token", "v6-host-secret", "public-ro",
                      "line one of a long value", "a comment that carries context", "bottom", "rotated in the",
                      "a note after"):
        assert plaintext not in text, "sops left %r in the clear — the fixture does not test what it claims" % plaintext
    assert "\nsops:\n" in text
    for flags in ((), ("--tree",)):
        result = _guard(tree, *flags)
        assert result.returncode == 0, "the guard refused a document sops %s wrote:\n%s" % (
            subprocess.run(["sops", "--version"], capture_output=True, text=True).stdout.split("\n")[0], result.stderr)


@pytest.mark.parametrize("where", ["appended in column 0", "appended with the indent of the last line",
                                   "appended one level deeper than the last line",
                                   "appended under a name sops uses in its own block",
                                   "inserted between two metadata keys", "inserted as the first line",
                                   "inserted before the sops block"])
def test_one_plaintext_key_added_by_hand_to_a_real_document_is_refused(encrypted, where):
    """The same real document with ONE plaintext key added by hand — at the end of the file in column 0, at the
    end with the indent of the file's last line (what an editor does: the metadata block is last, and the new line
    lands INSIDE it) or one level deeper, at the end under a name that is one of the block's own keys (`mac`), between
    two of the block's own keys, at the top, or just before the block — is refused, with
    the line named and the value not printed. Guards the gate against the file a marker test accepts: it still has
    its `sops:` block; for the two in-block positions sops does not even complain — it ignores the line and keeps
    decrypting — and for the others it complains only when somebody next decrypts, by which time the value is in
    the canonical's history."""
    tree, target = encrypted
    text = target.read_text(encoding="utf-8")
    added = "break_glass_password: hunter2-added-by-hand\n"
    last = text.rstrip("\n").split("\n")[-1]
    indent = last[:len(last) - len(last.lstrip())]
    assert indent and last.lstrip().startswith("version:"), "the block is last and ends with its version: %r" % last
    if where == "appended in column 0":
        text = text + added
    elif where == "appended with the indent of the last line":
        added = indent + added
        text = text + added
    elif where == "appended one level deeper than the last line":
        added = indent + indent + added
        text = text + added
    elif where == "appended under a name sops uses in its own block":
        added = indent + "mac: hunter2-added-by-hand\n"
        text = text + added
    elif where == "inserted between two metadata keys":
        added = indent + added
        text = text.replace("\n" + indent + "mac:", "\n" + added + indent + "mac:", 1)
    elif where == "inserted as the first line":
        text = added + text
    else:
        text = text.replace("\nsops:\n", "\n" + added + "sops:\n", 1)
    target.write_text(text, encoding="utf-8", newline="\n")
    line = text.split("\n").index(added.rstrip("\n")) + 1
    result = _guard(tree)
    assert result.returncode == 3
    assert "instance/secrets/network.sops.yml — not-sops-encrypted (line %d)" % line in result.stderr
    assert "hunter2" not in result.stderr and "hunter2" not in result.stdout


def _guard_keys(variable):
    """The metadata keys on one of the guard's two allow-lists (`LISTS`, `SCALARS`), read from the script."""
    source = open(GUARD, encoding="utf-8").read()
    names = "|".join(re.findall(r'^\s*%s = (?:%s )?"\|?([a-z_|]+)"\s*$' % (variable, variable), source, re.M))
    return set(name for name in re.split(r"\|+", names) if name)


def test_every_key_list_the_installed_sops_can_write_is_on_the_metadata_allow_list():
    """The guard reads the first level of the `sops:` block against two allow-lists of the keys sops writes there.
    The lists of key entries among them (`age`, `kms`, `pgp`, …) are the ones a release adds to when it learns a
    new key service, and the installed binary names every one in its metadata type — so this test reads them out
    of the binary and requires the guard's list keys to be exactly those. Guards a sops bump that adds a key service: the guard would call
    its list "not a key sops writes" and refuse every healthy file of an instance that uses it. (A new SCALAR
    key written into every file is caught by the document test above.) A binary in which no such name can be
    found fails here too: the test has stopped proving anything and has to be re-derived."""
    binary = open(shutil.which("sops"), "rb").read()
    lists = {m.decode("ascii") for m in re.findall(rb'mapstructure:"([a-z_]+),omitempty,deep"', binary)}
    assert {"age", "pgp", "kms"} <= lists, "could not read the metadata key lists out of this sops: %r" % sorted(lists)
    allowed = _guard_keys("LISTS")
    assert {"age", "pgp", "kms"} <= allowed, "could not read the guard's list keys: %r" % sorted(allowed)
    assert {"lastmodified", "mac", "version"} <= _guard_keys("SCALARS"), "could not read the guard's scalar keys"
    assert lists == allowed, "the key lists sops can write and the guard's differ: %r" % sorted(lists ^ allowed)


@pytest.mark.parametrize("key,why", [
    ('"a: b"', "a key that holds a colon and a space"),
    ('"%s"' % ("k" * 200), "a key longer than YAML's simple-key limit, which sops writes as `? key` / `: value`"),
])
def test_the_two_key_shapes_the_guard_refuses_on_purpose(tmp_path, key, why):
    """A document whose only oddity is a KEY holding `: `, or a key too long for the one-line form, is encrypted
    by the installed sops and then REFUSED by the guard. This is a documented limit, not a wish: the rule that a
    key may not contain `: ` is what stops `key: value more: ENC[…]` passing as one long key, and the documents
    tell the operator to rename such a key. Guards the documents staying true — if sops starts writing these keys
    another way, or the guard learns to read them, this test fails and the "rename the key" row has to change."""
    tree = tmp_path / "tree"
    secrets = tree / "instance" / "secrets"
    secrets.mkdir(parents=True)
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True, capture_output=True)
    target = secrets / "network.sops.yml"
    target.write_text("%s: a-value\nplain_key: another\n" % key, encoding="utf-8", newline="\n")
    done = subprocess.run(["sops", "--encrypt", "--age", RECIPIENT, "--input-type", "yaml", "--output-type", "yaml",
                           "--in-place", _p(target)], capture_output=True, text=True, env=_sops_env(),
                          cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "a-value" not in target.read_text(encoding="utf-8")
    result = _guard(tree)
    assert result.returncode == 3 and "not-sops-encrypted (line " in result.stderr, (why, result.stderr)
