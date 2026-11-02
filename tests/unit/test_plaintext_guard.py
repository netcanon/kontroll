"""scripts/lib/plaintext-guard.sh — the content gate in front of a blanket `git add -A`.

WHY: some commits take a whole worktree into the control node's canonical with nobody reading the diff — the
deploy's "instance state" commit and the launch-kit launcher's capture. "Everything committed is ciphertext or
public" was a comment. A secret file written in an editor and never encrypted has the right name; one plaintext
value added by hand to an encrypted file still has a `sops:` block (the old validate predicate accepted it; the
first draft of this guard stopped reading at that block, and the second read past it but not INTO it — and a key
appended at the end of a file, where a person adds one, lands inside the block whenever the editor indents the new
line like the one above); a decrypted copy, an editor backup or a key saved beside the file it opens is staged
like anything else. From the canonical it is cloned by the runner and copied by every backup.

These tests run the real script with the real git, in a scratch repository under tmp_path. They never use a
value that is a secret anywhere: the "ciphertext" tokens are a fixed base64 word, and the key bodies are built at
run time so that neither THIS file nor its compiled form holds a key (the guard, and the repository's own secret
scan, read both).
The document shapes are the ones the pinned sops writes; tests/integration/test_plaintext_guard_sops.py asks the
binary itself, where it is installed.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(ROOT, "scripts", "lib", "plaintext-guard.sh")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not all(shutil.which(tool) for tool in ("sh", "git", "awk")),
                       reason="needs sh, git and awk on PATH (CI has them; on Windows run pytest from Git Bash)"),
]

B64 = "QUJDREVGR0g9"                       # a fixed base64 word standing in for data, iv and tag
SECRET = "hunter2-plaintext-value"         # the plaintext a refusal must never repeat


def enc(kind="str"):
    return "ENC[AES256_GCM,data:%s,iv:%s,tag:%s,type:%s]" % (B64, B64, B64, kind)


# The shape sops writes for a whole-file-encrypted YAML document: an encrypted comment first, a comment that is
# only `#` (sops leaves it), a note kept on its value's line as an encrypted comment, nested maps, a list, empty
# values written back empty — in a map and in a list —, a key with colons in it (an IPv6-named host), and the
# metadata block last.
BODY = """{c}
#
a_password: {e} {c}
nested:
    inner: {e}
    count: {i}
    empty: "" {c}
    also_empty: ''
    nothing: null
    tilde: ~
    no_items: []
    no_keys: {{}}
    #
    items:
        - {e}
        - ""
        - null
        - []
        - {{}}
        - key: {e}
        -
            deeper: {e}
2001:db8::1_password: {e}
""".format(e=enc(), i=enc("int"), c="#" + enc("comment"))

SOPS_BLOCK = """sops:
    age:
        - enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
            YWdlLWVuY3J5cHRpb24ub3JnL3YxCg==
            -----END AGE ENCRYPTED FILE-----
          recipient: age1exampleexampleexampleexampleexampleexampleexampleexa
    lastmodified: "2026-10-12T00:00:00Z"
    mac: {e}
    unencrypted_suffix: _unencrypted
    version: 3.13.1
""".format(e=enc())

GOOD = BODY + SOPS_BLOCK

# The metadata block as a sops release before 3.7 wrote it: every key list present, empty ones as `[]`, and list
# items at the indent of their key.
OLD_SOPS_BLOCK = """sops:
    kms: []
    gcp_kms: []
    azure_kv: []
    hc_vault: []
    age:
    -   recipient: age1exampleexampleexampleexampleexampleexampleexampleexa
        enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
            YWdlLWVuY3J5cHRpb24ub3JnL3YxCg==
            -----END AGE ENCRYPTED FILE-----
    lastmodified: '2021-03-04T05:06:07Z'
    mac: {e}
    pgp: []
    unencrypted_suffix: _unencrypted
    version: 3.6.1
""".format(e=enc())

# Every first-level metadata key the guard allows, each on a line of its own (key groups, a PGP entry with the
# blank line an armor has, the regexes, the flags).
FULL_SOPS_BLOCK = """sops:
    shamir_threshold: 2
    key_groups:
        - hc_vault: []
          age:
            - enc: |
                -----BEGIN AGE ENCRYPTED FILE-----
                YWdlLWVuY3J5cHRpb24ub3JnL3YxCg==
                -----END AGE ENCRYPTED FILE-----
              recipient: age1exampleexampleexampleexampleexampleexampleexampleexa
    kms: []
    gcp_kms: []
    hckms: []
    azure_kv: []
    hc_vault: []
    age: []
    lastmodified: "2026-10-12T00:00:00Z"
    mac: {e}
    pgp:
        - created_at: "2026-10-12T00:00:00Z"
          enc: |-
            -----BEGIN PGP MESSAGE-----

            hQEMAwAAAAAAAAAAAQf+
            =AbCd
            -----END PGP MESSAGE-----
          fp: 0123456789ABCDEF0123456789ABCDEF01234567
    unencrypted_suffix: _unencrypted
    encrypted_suffix: _enc
    unencrypted_regex: ^public_
    encrypted_regex: ^secret_
    unencrypted_comment_regex: ^plain
    encrypted_comment_regex: ^sealed
    mac_only_encrypted: true
    version: 3.13.1
""".format(e=enc())


def _p(path):
    """A path the script and git both take on every platform (forward slashes)."""
    return str(path).replace("\\", "/")


def _git(tree, *args):
    return subprocess.run(["git", "-C", str(tree)] + list(args), check=True, capture_output=True, text=True).stdout


def _repo(tmp_path, name="tree"):
    tree = tmp_path / name
    (tree / "instance" / "secrets").mkdir(parents=True)
    _git(tree, "init", "-q")
    for key, value in (("user.email", "t@example.test"), ("user.name", "t"), ("commit.gpgsign", "false"),
                       ("core.autocrlf", "false")):
        _git(tree, "config", key, value)
    return tree


def _write(tree, rel, text):
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def _run(tree, *flags, **kwargs):
    return subprocess.run(["sh", _p(GUARD)] + list(flags) + [_p(tree)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kwargs)


def _findings(result):
    """The findings, sorted: the guard reports files in the order they were listed, which is git's or find's."""
    return sorted(ln for ln in result.stderr.splitlines() if ln.startswith("plaintext-guard: REFUSED"))


# The awk implementations a control node may have as `awk`. The launcher's seat runs the checker under whatever
# the HOST has, and a program one of them does not parse refuses every launch-kit run there — so the content
# tests below run once under the host's default and once under each of these that is installed.
_AWKS = {"gawk": ["gawk"], "mawk": ["mawk"], "busybox awk": ["busybox", "awk"], "original-awk": ["original-awk"]}


def _stub(directory, name, body):
    """An executable `name` in `directory` (created), to be put first on PATH."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8", newline="\n")
    os.chmod(str(path), 0o755)
    return {"env": dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"])}


@pytest.fixture(params=["the host's awk"] + sorted(_AWKS))
def under(request, tmp_path, isolated_git):
    """Keyword arguments for `_run` that make the guard use one awk implementation: nothing for the host's own,
    or a PATH whose first `awk` hands over to the named one (skipped where it is not installed). It copies the
    environment, so it asks for `isolated_git` by name: the copy must be taken after that fixture ran."""
    if request.param not in _AWKS:
        return {}
    argv = _AWKS[request.param]
    if not shutil.which(argv[0]):
        pytest.skip("%s is not installed here" % argv[0])
    return _stub(tmp_path / "awk-under-test", "awk", 'exec %s "$@"' % " ".join(argv))


def _age_key():
    """The shape of an age secret key, assembled when called. Not `"…" + "Q" * 58`: the compiler folds that into
    one constant, and the secret scan then finds a key in this file's compiled form."""
    return "-".join(("AGE", "SECRET", "KEY", "1" + "".join("Q" for _ in range(58))))


def _pem_header(kind="OPENSSH "):
    return "-----BEGIN " + kind + "PRIVATE KEY-----"


def test_a_plaintext_file_with_the_right_name_is_refused_and_named(tmp_path):
    """`instance/secrets/x.sops.yml` holding a bare `password:` makes the guard exit 3 and name the path and the
    rule. Guards the central case: a secret written in an editor and never encrypted, whose NAME says it is —
    nothing looked wrong, and the blanket add took it into the canonical."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", "password: %s\n" % SECRET)
    result = _run(tree)
    assert result.returncode == 3
    assert _findings(result) == ["plaintext-guard: REFUSED instance/secrets/network.sops.yml — not-sops-encrypted "
                                 "(no sops block)"]


_PLANTED = "extra_password: %s\n" % SECRET


@pytest.mark.parametrize("where,document", [
    ("before the sops block", _PLANTED + GOOD),
    ("after the sops block, in column 0", GOOD + _PLANTED),
    ("after the sops block, indented like its last line", GOOD + "    " + _PLANTED),
    ("after the sops block, indented less than the block", GOOD + "  " + _PLANTED),
    ("after the sops block, behind a tab", GOOD + "\t" + _PLANTED),
    ("after the sops block, one level deeper than its last line", GOOD + "        " + _PLANTED),
    ("after the sops block, behind its indent and a tab", GOOD + "    \t" + _PLANTED),
    ("after the sops block, as a list item with its indent", GOOD + "    - %s\n" % SECRET),
    ("inside the sops block, under a key that has a value",
     GOOD.replace("    mac:", "        " + _PLANTED + "    mac:")),
    ("inside the sops block, under a list key but indented less than the block",
     GOOD.replace("    age:\n", "    age:\n  - %s\n" % SECRET)),
    ("inside the sops block, under a list key but behind tabs",
     GOOD.replace("    age:\n", "    age:\n" + "\t" * 5 + "- %s\n" % SECRET)),
    ("after the sops block, under a planted list key", GOOD + "    kms:\n        " + _PLANTED),
    ("after the sops block, named like one of its own keys", GOOD + "    mac: %s\n" % SECRET),
    ("inside the sops block, under a key whose value was removed",
     GOOD.replace("    version: 3.13.1\n", "    version:\n        " + _PLANTED)),
    ("inside the sops block, as the value of a list key", GOOD.replace("    age:\n", "    age: %s\n    pgp:\n" % SECRET)),
    ("on the last line of the sops block, behind a lone carriage return",
     GOOD.rstrip("\n") + "\rpassword: %s\n" % SECRET),
    ("on the last line of the sops block, behind a line separator",
     GOOD.rstrip("\n") + chr(0x2028) + "password: %s\n" % SECRET),
    ("in a second sops block, behind a first one made to look finished",
     BODY + "sops:\n  age:\n" + SOPS_BLOCK.replace("    version:", "    " + _PLANTED + "    version:")),
    ("inside the sops block, between two of its keys", GOOD.replace("    mac:", "    " + _PLANTED + "    mac:")),
    ("as the first line of the sops block", GOOD.replace("sops:\n", "sops:\n    " + _PLANTED)),
    ("after a block two spaces deep, indented like it",
     BODY + SOPS_BLOCK.replace("\n    ", "\n  ") + "  " + _PLANTED),
    ("after a sops block that is not last", BODY + SOPS_BLOCK + "later: %s\n" % enc() + _PLANTED),
    ("as a list item", GOOD.replace("        - key:", "        - %s\n        - key:" % SECRET)),
    ("under a key that opens a block", "block:\n    %s\n" % SECRET + GOOD),
    ("as a plaintext comment", "# the password is %s\n" % SECRET + GOOD),
    ("after a ciphertext token", GOOD.replace("    inner: %s\n" % enc(), "    inner: %s %s\n" % (enc(), SECRET))),
    ("in front of an encrypted comment", GOOD.replace("a_password: %s " % enc(), "a_password: %s " % SECRET)),
    ("as a block scalar", "note: |\n    %s\n" % SECRET + GOOD),
    ("on a line a YAML reader breaks at a lone carriage return", "password:\r  %s\rx:\n" % SECRET + GOOD),
    ("on a line a YAML reader breaks at a next-line character",
     "password:%s  %s%sx:\n" % (chr(0x85), SECRET, chr(0x85)) + GOOD),
    ("on a line a YAML reader breaks at a line separator",
     "password:%s  %s%sx:\n" % (chr(0x2028), SECRET, chr(0x2028)) + GOOD),
    ("on a line a YAML reader breaks at a paragraph separator",
     "password:%s  %s%sx:\n" % (chr(0x2029), SECRET, chr(0x2029)) + GOOD),
], ids=lambda v: v.replace(" ", "-").replace(",", "") if isinstance(v, str) and len(v) < 75 else "doc")
def test_one_plaintext_value_inside_an_encrypted_file_is_refused(tmp_path, under, where, document):
    """An otherwise valid sops document with ONE plaintext value is refused, with the line number, wherever the
    value sits: before the `sops:` block; after it in column 0; after it with the indent of the block's last line
    (what an editor does when a key is added at the end of the file), one level deeper, less deep, behind a tab,
    or as a list item; inside the block between two metadata keys, under one that has a value, as its first
    line, under a list key but indented less than the block or with tabs, under a list key planted for the
    purpose, on a line named like one of sops's own keys AFTER `version:`, under a key whose value was removed,
    as the value of a list key, behind a lone carriage return or a line separator on the block's last line, and
    in a second block behind a forged first one; after a block that is not last; as a list item, under a block
    key, in a comment, behind a ciphertext token, in front of an encrypted comment, in a block scalar; and on a
    "line" that a YAML reader would split at a lone CR, NEL or line separator. Guards the marker predicate ("has a
    `sops:` block"), which accepted every one of these; a predicate that stops reading at the block; and one that
    reads past the block but treats everything indented under it as metadata — sops ignores such a line and keeps
    decrypting, so nothing else would notice."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", document)
    result = _run(tree, **under)
    assert result.returncode == 3, (where, result.stderr)
    found = _findings(result)
    assert len(found) == 1 and "not-sops-encrypted (line " in found[0], (where, found)
    line = next(n for n, text in enumerate(document.split("\n"), 1) if SECRET in text)       # where the value sits
    assert str(line) in found[0].split("(line ", 1)[1].replace(")", "").replace(",", " ").split(), (where, found)


@pytest.mark.parametrize("variant", [
    "as sops writes it", "with CRLF line ends", "with a byte-order mark", "with an encrypted key after the sops block",
    "named .sops.yaml", "between document markers", "with the block an old sops wrote",
    "with every metadata key sops can write", "with a block two spaces deep", "with a blank line in the block"])
def test_a_fully_encrypted_document_passes(tmp_path, under, variant):
    """A document in the shape sops writes — an encrypted comment first, a bare `#`, a note kept behind its value,
    nested maps, a list, empty values written back empty in a map and in a list, a key with colons in it, the
    metadata block — exits 0; so do the same bytes with CRLF line ends, with a byte-order mark (the first line is
    a comment, so the mark has to be stripped for it to be recognised), with a ciphertext key after the block,
    under the `.sops.yaml` spelling, between `---` and `...`, with the block an old sops release wrote (empty key
    lists, list items at their key's indent), with every metadata key on the allow-list, with a two-space
    indent, and with a blank line inside the block. Guards the worst thing this gate can do: a false refusal,
    which stops every deploy of a healthy instance that has a new or edited secret file."""
    tree = _repo(tmp_path)
    document, name = GOOD, "instance/secrets/network.sops.yml"
    if variant == "with CRLF line ends":
        document = GOOD.replace("\n", "\r\n")
    elif variant == "with a byte-order mark":
        document = b"\xef\xbb\xbf" + GOOD.encode("utf-8")
    elif variant == "with an encrypted key after the sops block":
        document = GOOD + "later: %s\n" % enc()
    elif variant == "named .sops.yaml":
        name = "instance/secrets/network.sops.yaml"
    elif variant == "between document markers":
        document = "---\n" + GOOD + "...\n"
    elif variant == "with the block an old sops wrote":
        document = BODY + OLD_SOPS_BLOCK
    elif variant == "with every metadata key sops can write":
        document = BODY + FULL_SOPS_BLOCK
    elif variant == "with a block two spaces deep":
        document = BODY + SOPS_BLOCK.replace("\n    ", "\n  ")
    elif variant == "with a blank line in the block":
        document = GOOD.replace("    lastmodified:", "\n    lastmodified:")
    _write(tree, name, document)
    _write(tree, "instance/.sops.yaml", "creation_rules:\n  - path_regex: x\n")      # the CONFIG file: public keys
    result = _run(tree, **under)
    assert result.returncode == 0, (variant, result.stderr)
    assert _run(tree, "--tree", **under).returncode == 0


@pytest.mark.parametrize("document,why", [
    ("visible_unencrypted: plain-on-purpose\n" + GOOD, "a value left in the clear by an unencrypted_suffix rule"),
    ("a_password: %s extra: %s\n" % (SECRET, enc()) + SOPS_BLOCK, "a plaintext value hidden in front of a token"),
    ("a_password: %s:\n" % SECRET + GOOD, "a plaintext value that ends in a colon"),
    ("sops: {}\na_password: %s\n" % enc(), "a `sops:` key that is not a block"),
    (BODY, "ciphertext values and no sops block at all"),
    (": %s\n" % enc() + GOOD, "a value with no key in front of it"),
    ("'a: b': %s\n" % enc() + GOOD, "a quoted key holding a colon and a space — a documented limit"),
    ("? %s\n: %s\n" % ("k" * 130, enc()) + GOOD, "a key in the explicit form sops uses for a long one — a limit"),
    ("a_password: ENC[AES256_GCM,data:%s,iv:,tag:,type:str]\n" % SECRET.replace("-", "") + SOPS_BLOCK,
     "a token with no nonce and no tag"),
    (BODY + SOPS_BLOCK.replace("    version:", "    sops_version:"), "a metadata key sops does not write"),
    (BODY + SOPS_BLOCK.replace("    age:\n", "    recipients:\n"), "recipient entries under a key sops does not write"),
    (BODY + SOPS_BLOCK.replace("    lastmodified:", "    lastmodified:\n    mac_only_encrypted:"),
     "a metadata key that always has a value, without one"),
    ("status:\n    null\n" + GOOD, "a bare `null` on a line of its own, which is a value and not a list item"),
])
def test_what_the_predicate_cannot_vouch_for_is_refused(tmp_path, under, document, why):
    """The predicate fails CLOSED on what it cannot vouch for: a value left plain by a partial-encryption rule,
    `key: value more: ENC[…]` (which must not pass as one long key), a plaintext value that ends in a colon (which
    must not pass as "a key that opens a block"), a `sops:` that is not a mapping block, ciphertext with no
    metadata block, a value with no key, the two key shapes that hold or imply a `: ` (a quoted key with one, and
    the explicit `? key` form — sops can write both, and the documents say to rename such a key), a token with an
    empty nonce and tag, a first-level metadata key that is not one of sops's own, recipient entries under such
    a key (each of them is then refused, not only the key's own line), a metadata key that always has a value
    and stands without one, and a bare `null` on a line of its own. Guards the easy way to make the gate pass: a
    rule loose enough to call any of these a key."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", document)
    result = _run(tree, **under)
    assert result.returncode == 3, (why, result.stderr)
    if why.startswith("recipient entries under a key"):
        reported = _findings(result)[0].split("(line ", 1)[1]
        assert "," in reported, "only the key's own line was refused, not what stands under it: %s" % reported


@pytest.mark.parametrize("where,document", [
    ("a line put among the recipient entries",
     GOOD.replace("    age:\n", "    age:\n        - password: %s\n" % SECRET)),
    ("a line named like a scalar metadata key, placed before `version:`",
     GOOD.replace("    version:", "    mac_only_encrypted: %s\n    version:" % SECRET)),
    ("text typed onto the last line of the block", GOOD.rstrip("\n") + " password: %s\n" % SECRET),
    ("a value written by hand in the shape of a ciphertext token",
     "a_password: ENC[AES256_GCM,data:%s,iv:%s,tag:%s,type:str]\n" % (SECRET.replace("-", ""), B64, B64) + GOOD),
    ("a key NAME that is the secret", "%s: %s\n" % (SECRET, enc()) + GOOD),
], ids=lambda v: v.replace(" ", "-").replace(",", "").replace("`", "").replace(":", "") if len(v) < 75 else "doc")
def test_what_the_documents_say_is_not_covered_is_not_refused(tmp_path, under, where, document):
    """The places the documents list under "Not covered" — a line among the recipient entries, the VALUE of one of
    sops's own scalar keys (a line named like one before `version:`, and text typed onto the block's last line), a
    value typed in the shape of a ciphertext token, a secret used as a key NAME — are in fact not refused: exit 0.
    Nobody wants these to pass. They are held here because a limit the documents state and the rule no longer has
    — or has and the documents no longer state — is a false sentence in SECURITY.md either way: whoever closes
    one moves its row to the refused positions above and takes the clause out of the documents, in one commit."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", document)
    result = _run(tree, **under)
    assert result.returncode == 0, (where, result.stderr)


def test_a_document_with_many_plaintext_lines_names_the_first_five(tmp_path):
    """Seven plaintext lines produce ONE finding that lists five line numbers and says there are more. Guards the
    refusal growing with the file — a whole plaintext document appended to an encrypted one would otherwise print
    a line number per line into the deploy log — and guards the count being cut without saying so."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", "".join("k%d: %s\n" % (n, SECRET) for n in range(7)) + GOOD)
    found = _findings(_run(tree))
    assert found == ["plaintext-guard: REFUSED instance/secrets/network.sops.yml — not-sops-encrypted "
                     "(line 1, 2, 3, 4, 5 and more)"]


def test_the_guard_never_prints_content(tmp_path):
    """On every kind of refusal, neither stdout nor stderr holds the plaintext value or the body of the key — only
    a path, a rule and line numbers. Guards the gate becoming the leak: deploy output is kept in run logs and in
    Semaphore's task history, and the launcher prints to a terminal somebody may be sharing."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", GOOD + "    " + _PLANTED)
    _write(tree, "instance/secrets/plain.sops.yml", "password: %s\n" % SECRET)
    _write(tree, "instance/secrets/copy.yml", "password: %s\n" % SECRET)
    _write(tree, "breakglass.txt", "# created\n%s\n" % _age_key())
    result = _run(tree)
    assert result.returncode == 3 and len(_findings(result)) == 4
    for output in (result.stdout, result.stderr):
        assert SECRET not in output and "QQQQ" not in output and "hunter2" not in output


@pytest.mark.parametrize("name", ["network.yml", "network.dec.yml", "network.sops.yml.bak", "age.key", "notes.txt",
                                  "sub/dir/dump.json"])
def test_a_file_that_is_not_a_sops_document_may_not_sit_in_a_secrets_directory(tmp_path, name):
    """A decrypted copy, an editor backup, a key or a notes file under `instance/secrets/` — or the legacy
    `ansible/secrets/` — is refused by its NAME (`secrets-dir-member`), in a scratch tree that has no ignore rule
    at all. Guards those cases independently of `.gitignore`, which stops them quietly and only while nobody
    edits it; README.md and .gitkeep are allowed."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/" + name, "password: %s\n" % SECRET)
    _write(tree, "ansible/secrets/" + name, "password: %s\n" % SECRET)
    _write(tree, "instance/secrets/README.md", "how to add a secret\n")
    _write(tree, "instance/secrets/.gitkeep", "")
    found = _findings(_run(tree))
    assert found == ["plaintext-guard: REFUSED ansible/secrets/%s — secrets-dir-member" % name,
                     "plaintext-guard: REFUSED instance/secrets/%s — secrets-dir-member" % name]


def test_key_material_is_refused_wherever_it_sits(tmp_path, under):
    """A file anywhere in the tree that holds an age secret key IN FULL — alone on its line, after
    `SOPS_AGE_KEY=` in a script, indented, in a file saved with a byte-order mark, or in the hybrid post-quantum
    form age 1.3 writes — or a PEM or PGP private-key header that BEGINS its line (also behind a byte-order
    mark), or such a header anywhere in a line with key material after it (an SSH key inlined in a YAML block
    scalar, a service-account JSON, a quoted string with `\n` in it), is refused (`private-key-material`)
    whatever the file is called. A word that only starts like an age key, a key one character short, a header
    that is merely quoted — inside a string, or indented in a document with a placeholder under it — and a
    certificate are not. Nor — a DOCUMENTED LIMIT, held here so that the documents and the rule cannot part — is
    an armored block INLINED in another file when its body does not follow the header directly: a PGP
    private-key block (a blank line, often armor header lines, come first) and a passphrase-protected legacy PEM
    (`Proc-Type:` / `DEK-Info:` lines). Guards a key made with `age-keygen -o <file>` inside the repository (the
    break-glass step was once documented that way; docs/break-glass.md now makes it on another machine) landing
    in the canonical, the key pasted into a helper script, and a fleet SSH key
    or a TLS key copied in on its way elsewhere; and guards the rule against refusing the fixtures and documents
    that show what a key looks like (which needs no exemption word: they are not keys)."""
    tree = _repo(tmp_path)
    key = _age_key()
    _write(tree, "kontroll-breakglass.txt", "# created: 2026-10-12\n# public key: age1example\n%s\n" % key)
    _write(tree, "deploy.sh", "#!/bin/sh\nexport SOPS_AGE_KEY=%s\n" % key)
    _write(tree, "notes/indented.txt", "the key:\n\n    %s\n" % key)
    _write(tree, "from-windows.txt", b"\xef\xbb\xbf" + (key + "\r\n").encode("ascii"))
    _write(tree, "hybrid.txt", "# created\n%s\n" % key.replace("KEY-1", "KEY-PQ-1"))
    body = "".join("b3BlbnNzaC1rZXktdjEA" for _ in range(3))                  # sixty characters of base64
    _write(tree, "group_vars/all.yml", "fleet_ssh_key: |\n  %s\n  %s\n  -----END OPENSSH PRIVATE KEY-----\n"
           % (_pem_header(), body))
    _write(tree, "cloud/service-account.json", '{\n  "type": "service_account",\n  "private_key": "%s\\n%s\\n"\n}\n'
           % (_pem_header(""), body))
    _write(tree, "host_vars/sw1.yml", 'ansible_ssh_private_key: "%s\\n%s\\n"\n' % (_pem_header(), body))
    _write(tree, "docs/id_fleet", "%s\nb3BlbnNzaA==\n-----END OPENSSH PRIVATE KEY-----\n" % _pem_header())
    _write(tree, "tls/server.txt", "%s\nMIIB\n" % _pem_header(""))
    _write(tree, "tls/legacy.txt", "%s\nMIIB\n" % _pem_header("RSA "))
    _write(tree, "tls/saved-with-a-mark.txt", b"\xef\xbb\xbf" + ("%s\nMIIB\n" % _pem_header("EC ")).encode("ascii"))
    _write(tree, "pgp.asc", "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\n")
    _write(tree, "tests/fixture.py", 'KEY = "%sFAKEFAKE"\nSHORT = "%s"\nx = "%s"\n'
           % (key[:16], key[:-1], _pem_header()))
    _write(tree, "docs/example.md", "A key file starts like this:\n\n    %s\n    ...\n\nand a string like "
           "`\"%s\\n...\"` is one too.\n" % (_pem_header(), _pem_header()))
    _write(tree, "tls/public.pem", "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    # The documented limits: the body does not follow the header DIRECTLY, and the header does not begin its line.
    _write(tree, "group_vars/backup.yml", "backup_signing_key: |\n  -----BEGIN PGP " + "PRIVATE KEY BLOCK-----\n"
           "  Version: an armor header line\n\n  %s\n  -----END PGP PRIVATE KEY BLOCK-----\n" % body)
    _write(tree, "group_vars/legacy.yml", "tls_key: |\n  %s\n  Proc-Type: 4,ENCRYPTED\n  DEK-Info: AES-128-CBC,00\n\n"
           "  %s\n" % (_pem_header("RSA "), body))
    found = _findings(_run(tree, **under))
    assert found == ["plaintext-guard: REFUSED cloud/service-account.json — private-key-material (line 3)",
                     "plaintext-guard: REFUSED deploy.sh — private-key-material (line 2)",
                     "plaintext-guard: REFUSED docs/id_fleet — private-key-material (line 1)",
                     "plaintext-guard: REFUSED from-windows.txt — private-key-material (line 1)",
                     "plaintext-guard: REFUSED group_vars/all.yml — private-key-material (line 2)",
                     "plaintext-guard: REFUSED host_vars/sw1.yml — private-key-material (line 1)",
                     "plaintext-guard: REFUSED hybrid.txt — private-key-material (line 2)",
                     "plaintext-guard: REFUSED kontroll-breakglass.txt — private-key-material (line 3)",
                     "plaintext-guard: REFUSED notes/indented.txt — private-key-material (line 3)",
                     "plaintext-guard: REFUSED pgp.asc — private-key-material (line 1)",
                     "plaintext-guard: REFUSED tls/legacy.txt — private-key-material (line 1)",
                     "plaintext-guard: REFUSED tls/saved-with-a-mark.txt — private-key-material (line 1)",
                     "plaintext-guard: REFUSED tls/server.txt — private-key-material (line 1)"]


def test_only_what_the_commit_would_take_is_a_candidate(tmp_path):
    """An IGNORED plaintext file and an unmodified COMMITTED one produce no finding in the default mode, and a
    file staged earlier with `git add` — neither untracked nor modified — does, once, also when it has been edited
    again since and is therefore listed as staged AND as modified. `--tree` reads BOTH secrets
    directories whatever git thinks of the files, and needs no repository at all. Guards two opposite failures: a
    gate that turns every deploy red over a file git would never stage (or over an old mistake whose only remedy
    is history surgery), and a gate that misses a file because somebody had already staged it; and guards the
    validate seat, which runs `--tree` on an extracted bundle that is not a repository."""
    tree = _repo(tmp_path)
    _write(tree, ".gitignore", "scratch/\n")
    _write(tree, "scratch/dump.sops.yml", "password: %s\n" % SECRET)                 # ignored
    _write(tree, "instance/secrets/old.sops.yml", "password: %s\n" % SECRET)         # committed, then unmodified
    _write(tree, "ansible/secrets/legacy.sops.yml", "password: %s\n" % SECRET)       # the legacy directory, same
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "an old mistake")
    assert _run(tree).returncode == 0
    expected = ["plaintext-guard: REFUSED ansible/secrets/legacy.sops.yml — not-sops-encrypted (no sops block)",
                "plaintext-guard: REFUSED instance/secrets/old.sops.yml — not-sops-encrypted (no sops block)"]
    assert _findings(_run(tree, "--tree")) == expected
    _write(tree, "instance/secrets/new.sops.yml", "password: %s\n" % SECRET)
    _git(tree, "add", "instance/secrets/new.sops.yml")                               # staged earlier, by hand
    assert _findings(_run(tree)) == [
        "plaintext-guard: REFUSED instance/secrets/new.sops.yml — not-sops-encrypted (no sops block)"]
    _write(tree, "instance/secrets/new.sops.yml", "password: %s\nuser: x\n" % SECRET)   # ...and edited since: the
    assert _findings(_run(tree)) == [                                                # path is listed twice, read once
        "plaintext-guard: REFUSED instance/secrets/new.sops.yml — not-sops-encrypted (no sops block)"]

    plain = tmp_path / "an-extracted-bundle"                                         # no .git anywhere above it
    shutil.copytree(str(tree / "ansible"), str(plain / "ansible"))
    result = _run(plain, "--tree", env=dict(os.environ, GIT_CEILING_DIRECTORIES=_p(tmp_path)))
    assert result.returncode == 3 and _findings(result) == expected[:1]


def test_a_deleted_path_is_not_read_and_a_first_capture_sees_everything(tmp_path):
    """On a repository with NO commit yet — the launch kit's first capture — every file is a candidate; and after
    a commit a deleted tracked secrets file (which git lists as modified) is passed over without a finding and
    without a word on stderr. Guards the first capture — the one that takes a freshly scaffolded instance/ —
    being the capture the gate does not check, and guards the gate stopping a deploy with "(unreadable)" because
    somebody removed a secrets file."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", "password: %s\n" % SECRET)
    assert _run(tree).returncode == 3                                                # unborn HEAD
    _write(tree, "instance/secrets/network.sops.yml", GOOD)
    _write(tree, "instance/secrets/retired.sops.yml", GOOD)
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "first")
    os.remove(str(tree / "instance" / "secrets" / "retired.sops.yml"))
    result = _run(tree)
    assert result.returncode == 0 and result.stderr == ""


def test_it_refuses_when_it_cannot_run(tmp_path):
    """A directory that is not a git worktree (with git's own reason passed on), a path that does not exist, a
    directory that is NOT THE TOP of its repository, either listing git cannot produce, and a checker that does
    not finish each give exit 2 with a sentence on stderr — never 0 — although a plaintext secrets file is
    waiting in every one of them. Guards the callers'
    rule (any non-zero is a refusal) having something to act on. The three that are not obvious: below the top
    level `git ls-files` lists only that directory while `git add -A` stages the whole repository, so the gate
    would pass files it never saw (a kit extracted inside another repository did exactly that); a failing
    `git diff --cached` on the left of a pipe would otherwise read as "nothing is staged"; and an awk that dies
    has checked nothing."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    for target in (plain, tmp_path / "absent"):
        result = subprocess.run(["sh", _p(GUARD), _p(target)], capture_output=True, text=True, env=dict(
            os.environ, GIT_CEILING_DIRECTORIES=_p(tmp_path)))
        assert result.returncode == 2 and "plaintext-guard:" in result.stderr
    assert "not a git repository" in result.stderr or "usage:" in result.stderr
    not_a_repo = subprocess.run(["sh", _p(GUARD), _p(plain)], capture_output=True, text=True, env=dict(
        os.environ, GIT_CEILING_DIRECTORIES=_p(tmp_path)))
    assert "not a git repository" in not_a_repo.stderr, "git's own reason must reach the operator"

    outer = _repo(tmp_path, "outer")
    _write(outer, "elsewhere/instance/secrets/network.sops.yml", "password: %s\n" % SECRET)
    _write(outer, "elsewhere/breakglass.txt", _age_key() + "\n")
    (outer / "kit").mkdir()
    result = _run(outer / "kit")
    assert result.returncode == 2 and "not its top level" in result.stderr, result.stderr

    staged = _repo(tmp_path, "staged")
    _write(staged, "instance/secrets/network.sops.yml", "password: %s\n" % SECRET)
    _git(staged, "add", "-A")                                # in the index only: `diff --cached` is what lists it
    _git(staged, "config", "diff.renames", "not-a-boolean")  # ...and this makes `git diff` die
    result = _run(staged)
    assert result.returncode == 2 and "git diff --cached" in result.stderr, result.stderr

    broken = _repo(tmp_path, "broken")
    _write(broken, "instance/secrets/network.sops.yml", "password: %s\n" % SECRET)
    _git(broken, "add", "-A")
    (broken / ".git" / "index").write_bytes(b"not an index")   # ...and this makes `git ls-files` die
    result = _run(broken)
    assert result.returncode == 2 and "git ls-files" in result.stderr, result.stderr

    tree = _repo(tmp_path, "tree")
    _write(tree, "instance/secrets/network.sops.yml", "password: %s\n" % SECRET)
    result = _run(tree, **_stub(tmp_path / "no-awk", "awk", "exit 1"))
    assert result.returncode == 2 and "did not finish" in result.stderr, result.stderr


def _real(tool):
    return shutil.which(tool).replace("\\", "/")


def test_a_stage_that_dies_on_the_way_to_the_checker_is_not_an_empty_list(tmp_path):
    """With a plaintext secrets file waiting, each of these gives exit 2 and never 0: a `git` that cannot say
    where the top of the repository is (asked from a subdirectory); a `git` whose listing works when it is
    probed and fails when its output is the one being checked; a `tr` that dies in the checking pipeline; and,
    under `--tree`, a `find` that fails part-way. Guards the shape this gate has had three times: a stage that
    fails on the way to the checker hands it a short or an empty list, and an empty list reads as "nothing to
    refuse". The checker refuses unless the END of the list reached it — so it does not matter which stage died."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/network.sops.yml", "password: %s\n" % SECRET)
    _write(tree, "sub/readme.txt", "x\n")
    assert _run(tree).returncode == 3, "the fixture is not refused to begin with"

    no_cdup = _stub(tmp_path / "git-no-cdup", "git",
                    'case " $* " in *" --show-cdup "*) echo "fatal: simulated" >&2; exit 128 ;; esac\n'
                    'exec "%s" "$@"' % _real("git"))
    result = _run(tree / "sub", **no_cdup)
    assert result.returncode == 2 and "top of this repository" in result.stderr, result.stderr

    counter = tmp_path / "ls-files.count"
    flaky = _stub(tmp_path / "git-flaky", "git",
                  'case " $* " in *" ls-files "*)\n  n=$(cat "%s" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "%s"\n'
                  '  if [ "$n" -ge 4 ]; then echo "fatal: simulated" >&2; exit 128; fi ;;\nesac\nexec "%s" "$@"'
                  % (_p(counter), _p(counter), _real("git")))
    result = _run(tree, **flaky)
    assert result.returncode == 2 and "did not arrive whole" in result.stderr, result.stderr

    tr_count = tmp_path / "tr.count"
    dying_tr = _stub(tmp_path / "tr-dies", "tr",
                     'n=$(cat "%s" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "%s"\n'
                     'if [ "$n" -ge 3 ]; then exit 1; fi\nexec "%s" "$@"' % (_p(tr_count), _p(tr_count), _real("tr")))
    result = _run(tree, **dying_tr)
    assert result.returncode == 2 and "did not arrive whole" in result.stderr, result.stderr

    half = _stub(tmp_path / "find-half", "find", 'echo instance/secrets/network.sops.yml; exit 1')
    assert _run(tree, "--tree").returncode == 3
    result = _run(tree, "--tree", **half)
    assert result.returncode == 2 and "did not arrive whole" in result.stderr, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="a file name cannot hold a line break on this file system")
def test_a_name_with_a_line_break_is_refused(tmp_path):
    """A candidate whose NAME holds a line break makes the guard exit 3 with `unusual-file-name`. The checker
    reads one path per line, so such a name would arrive as two names that are not the file. Guards a secret
    being hidden from the gate by nothing more than how its file is called."""
    tree = _repo(tmp_path)
    _write(tree, "instance/secrets/a\nb.sops.yml", "password: %s\n" % SECRET)
    result = _run(tree)
    assert result.returncode == 3 and "unusual-file-name" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link needs a privilege on this platform")
def test_a_symlink_is_not_read_but_a_symlinked_secrets_directory_is(tmp_path):
    """In the default mode a symbolic link among the candidates is passed over — git stores the link's target
    NAME, not the file behind it, so there is nothing of it to refuse — and `--tree` reads a secrets directory
    that is itself a symlink. Guards a deploy being refused over a file that is not in the commit, and guards the
    validate seat reporting "every secrets file is encrypted" having read none of them because an operator keeps
    `instance/secrets` somewhere else."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plain.sops.yml").write_text("password: %s\n" % SECRET, encoding="utf-8")

    tree = _repo(tmp_path)
    os.symlink(str(outside / "plain.sops.yml"), str(tree / "instance" / "secrets" / "link.sops.yml"))
    result = _run(tree)
    assert result.returncode == 0 and result.stderr == ""

    linked = _repo(tmp_path, "linked")
    os.rmdir(str(linked / "instance" / "secrets"))
    os.symlink(str(outside), str(linked / "instance" / "secrets"))
    assert _findings(_run(linked, "--tree")) == [
        "plaintext-guard: REFUSED instance/secrets/plain.sops.yml — not-sops-encrypted (no sops block)"]


_BLANKET_ADD = re.compile(
    r"\bgit\b.*\s(?:(?:add|stage)(?:\s+-[-\w=]+)*\s+(?:-A|--all|-u|--update|\.|\./|:/|\*)(?:\s|$)"
    r"|commit(?:\s+-[-\w=]+)*\s+(?:-[A-Za-z]*a[A-Za-z]*|--all)(?:\s|$))")
_EXPLAINED = "NO plaintext gate, on purpose"


def _commands(lines):
    """[(line number, text)] to search: every line that is not a comment, with the punctuation of an argument
    LIST and of a shell command line turned into spaces (`["git", "add", "-A"]` and `git add -A; git commit` both
    read as `git add -A …`), and every YAML `argv:` sequence that
    is written one item per line joined into one text, numbered at its `argv:` line."""
    out, number = [], 0
    while number < len(lines):
        line = lines[number]
        number += 1
        start = number
        if line.lstrip().startswith("#"):
            continue
        text = re.sub(r"[\[\]\"',;&|()<>]", " ", line)
        if re.match(r"\s*argv:\s*$", line):
            items = []
            while number < len(lines) and re.match(r"\s*-\s+\S", lines[number]):
                items.append(re.sub(r"^\s*-\s+", "", re.sub(r"[\"']", "", lines[number])).strip())
                number += 1
            text = " ".join(items)
        out.append((start, text))
    return out


def blanket_adds_without_a_gate(root):
    """[(path, line number)] for every command under the directories a node RUNS that stages a whole worktree and
    is neither preceded, in its file, by a call of the plaintext guard nor explained by the marker comment within
    the eight lines above it. A command is read whether it is written as a line or as an argument list."""
    out = []
    for top in ("scripts", "ansible", "docker", "api", "gui"):
        for directory, _, names in os.walk(os.path.join(root, top)):
            for name in sorted(names):
                path = os.path.join(directory, name)
                try:
                    lines = open(path, encoding="utf-8").read().split("\n")
                except (OSError, UnicodeDecodeError):
                    continue
                for number, text in _commands(lines):
                    if not _BLANKET_ADD.search(text):
                        continue
                    before = "\n".join(lines[:number - 1])
                    if "plaintext-guard.sh" in before or _EXPLAINED in "\n".join(lines[max(0, number - 9):number]):
                        continue
                    out.append((os.path.relpath(path, root).replace(os.sep, "/"), number))
    return sorted(out)


def test_every_blanket_add_a_node_runs_is_gated_or_explained(tmp_path):
    """Every `git add -A` / `--all` / `-u` / `.` and every `git commit -a` under scripts/, ansible/, docker/, api/
    and gui/ either comes after a call of scripts/lib/plaintext-guard.sh in the same file, or carries the comment
    "NO plaintext gate, on purpose" (with its reason) just above it; and the check itself finds a planted one —
    written as a shell line (also with `;` straight after the flag, in a subshell, with a flag in front, as
    `git stage`, as `add -u`, and as a `commit -a` with no add at all), as a hint inside a Python string, as a
    Python argument list, and as an Ansible `argv:` list over several lines — while a scoped add,
    `commit --amend` and a comment are left alone. Guards
    the next commit of a whole worktree being added without the gate: the documents name the gated commits, and
    nothing else would make them wrong out loud."""
    assert blanket_adds_without_a_gate(ROOT) == []
    planted = tmp_path / "scripts"
    planted.mkdir()
    (planted / "capture.sh").write_text("#!/bin/sh\n# git add -A in a comment is not a command\ncd tree\n"
                                        "git add -A\ngit commit -qm x\n", encoding="utf-8")
    (planted / "terse.sh").write_text("cd tree && git add -A; git commit -qm x\n(git stage -f --all)\n"
                                      "git add -u\ngit commit -q -a\ngit add -- one.yml\n", encoding="utf-8")
    (planted / "explained.sh").write_text("# A blanket add with %s: scratch data only.\ngit add --all\n"
                                          % _EXPLAINED, encoding="utf-8")
    (planted / "gated.sh").write_text('sh scripts/lib/plaintext-guard.sh "$ROOT" || exit 1\ngit -C "$ROOT" add -A\n',
                                      encoding="utf-8")
    (planted / "hint.py").write_text('HINT = "git add . && git commit -am regen"\n', encoding="utf-8")
    (planted / "listed.py").write_text('import subprocess\nsubprocess.run(["git", "-C", tree, "add", "-A"])\n'
                                       'subprocess.run(["git", "add"] + list(paths))\n'
                                       'subprocess.run(["git", "commit", "--amend", "--no-edit"])\n', encoding="utf-8")
    plays = tmp_path / "ansible"
    plays.mkdir()
    (plays / "stage.yml").write_text("- name: stage everything\n  ansible.builtin.command:\n    argv:\n"
                                     "      - git\n      - -C\n      - \"{{ tree }}\"\n      - add\n      - --all\n"
                                     "- name: stage one file\n  ansible.builtin.command:\n    argv:\n"
                                     "      - git\n      - add\n      - one.yml\n", encoding="utf-8")
    assert blanket_adds_without_a_gate(str(tmp_path)) == [
        ("ansible/stage.yml", 3), ("scripts/capture.sh", 4), ("scripts/hint.py", 1), ("scripts/listed.py", 2),
        ("scripts/terse.sh", 1), ("scripts/terse.sh", 2), ("scripts/terse.sh", 3), ("scripts/terse.sh", 4)]


def test_the_shipped_tree_passes_the_guard(tmp_path):
    """A default run over a scratch copy of every tracked file outside the two instance-only trees (`instance/`,
    `docs/reviews/`) exits 0 — an unborn repository, so everything is a candidate, which is what a launch kit's
    first capture is. Guards a false positive introduced later — a fixture or a document with a line that reads
    as a key — which would stop the published install path at its first command. It reads neither an operator's
    overlay nor a secrets directory of the checkout it runs in: tests/validate.sh's `sops-encrypted` step is the
    seat for those."""
    listing = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True)
    if listing.returncode != 0 or not listing.stdout:
        pytest.skip("this checkout has no usable git index to list the tracked files from")
    tree = _repo(tmp_path, "copy")
    for rel in listing.stdout.decode("utf-8").split("\0"):
        source = os.path.join(ROOT, rel)
        if (rel and not rel.startswith(("instance/", "docs/reviews/"))
                and os.path.isfile(source) and not os.path.islink(source)):
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, str(target))
    result = _run(tree)
    assert result.returncode == 0, result.stderr
