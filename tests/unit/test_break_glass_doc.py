"""docs/break-glass.md — the procedure an operator runs against an existing instance's secrets — spells the commands
the way the tools take them, levels the checkout before it tags it, and has a Verify block that proves the
break-glass key alone.

WHY. The runbook adds a recipient to every domain and re-wraps the ciphertext. A command in it that the tool spells
differently fails half-way through a procedure on live secrets; a blanket `git add` in it would commit whatever else
the operator's checkout holds alongside the re-wrapped files. The commands come from the code: the re-wrap is
`keygen.SOPS_UPDATEKEYS`, the flag is kontroll-init's own. Verify is the one check SECURITY.md C1 says proves the
key: on a machine that also holds the control key, it must not pass without the break-glass key.
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess

import pytest

from kontroll.service import keygen

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _init_help():
    """kontroll-init's --help text, from its own parser."""
    spec = importlib.util.spec_from_file_location("kontroll_init_doc", os.path.join(ROOT, "scripts", "kontroll-init.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        mod.main(["--help"])
    return out.getvalue()


def test_the_runbook_uses_the_tools_own_commands():
    """docs/break-glass.md carries the re-wrap exactly as `keygen.SOPS_UPDATEKEYS` spells it (the `--config` form),
    the `--break-glass-recipient` flag as kontroll-init's parser spells it, the installer shell it runs them in, the
    landing through `local-canonical.yml` with its `--check --diff` first, and a scoped `git add --` with no blanket
    add or commit-all. This checks spelling, not that a command runs: one spelled right still fails on a host without
    its tools, which is why the runbook runs them in the installer shell. Guards the runbook drifting from the
    code it tells an operator to run against their secrets, and a blanket add committing more than the re-wrap."""
    doc = open(os.path.join(ROOT, "docs", "break-glass.md"), encoding="utf-8").read()
    assert keygen.SOPS_UPDATEKEYS in doc
    assert "--break-glass-recipient" in _init_help()
    assert "kontroll-init.py --break-glass-recipient age1" in doc
    assert "scripts/kontroll-installer.sh shell" in doc
    dry = doc.index("kontroll-installer.sh playbooks/local-canonical.yml --check --diff")
    assert re.search(r"kontroll-installer\.sh playbooks/local-canonical\.yml\n", doc[dry:]), \
        "the landing's apply follows its dry run"
    assert re.search(r"git add -- instance/\.sops\.yaml instance/secrets\b", doc)
    assert not re.search(r"git add (-A|--all|\.)(\s|$)|git commit -a", doc)


def test_the_runbook_levels_the_checkout_first_and_its_dry_run_answers_no():
    """The "Before you start" list fast-forwards the checkout to the canonical, then lists what the checkout holds
    that the canonical lacks (expecting nothing), before the rollback tag; and step 3's dry run tells the operator to
    answer **n** to every file. Guards a re-wrap committed on a stale base — the landing then refuses, and its remedy
    is a rebase over ciphertext a promoted proposal re-encrypted, which cannot merge — a checkout only AHEAD of the
    canonical passing as level (`merge --ff-only` prints "Already up to date." for it: T11 fold 4's read B, N1), and
    a dry run that writes."""
    doc = open(os.path.join(ROOT, "docs", "break-glass.md"), encoding="utf-8").read()
    order = [doc.index(c) for c in ("git fetch local && git merge --ff-only local/main",
                                    "git log --oneline local/main..HEAD", "git tag pre-break-glass")]
    assert order == sorted(order)
    step3 = doc[doc.index("**Step 3"):doc.index("**Step 4")]
    assert "Answer **n** to every file" in step3 and "**y**" not in step3


_FAKE_SOPS = """#!/usr/bin/env python3
# A stand-in sops for `sops -d FILE` (and `sops --help`, exit 0): exit 0 when a key it can find is one of FILE's
# `recipient:` lines. A key is an
# `AGE-SECRET-KEY-<NAME>` line, whose recipient is `age1<name>`. It reads each place the Verify block must empty:
# SOPS_AGE_KEY, SOPS_AGE_KEY_CMD, SOPS_AGE_KEY_FILE, SOPS_AGE_SSH_PRIVATE_KEY_FILE, SOPS_AGE_SSH_PRIVATE_KEY_CMD,
# SOPS_KEYSERVICE (here, a file holding the service's keys), the keys.txt under $XDG_CONFIG_HOME, ~/.config and
# ~/Library, ~/.ssh/id_ed25519 and ~/.ssh/id_rsa. It also reads NEW_PLACE, a path outside the home and the
# environment, as a newer sops reading a new place would.
import os
import subprocess
import sys

if sys.argv[1:] == ["--help"]:
    sys.exit(0)
NEW_PLACE = %(new_place)r
env, home = os.environ, os.environ.get("HOME", "")
held = [env.get("SOPS_AGE_KEY", "")]
for cmd in ("SOPS_AGE_KEY_CMD", "SOPS_AGE_SSH_PRIVATE_KEY_CMD"):
    if env.get(cmd):
        held.append(subprocess.run(env[cmd], shell=True, capture_output=True, text=True).stdout)
places = [env.get(v) for v in ("SOPS_AGE_KEY_FILE", "SOPS_AGE_SSH_PRIVATE_KEY_FILE", "SOPS_KEYSERVICE")] + [NEW_PLACE]
places += [os.path.join(home, *rel) for rel in ((".config", "sops", "age", "keys.txt"), (".ssh", "id_ed25519"),
           ("Library", "Application Support", "sops", "age", "keys.txt"), (".ssh", "id_rsa"))]
if env.get("XDG_CONFIG_HOME"):
    places.append(os.path.join(env["XDG_CONFIG_HOME"], "sops", "age", "keys.txt"))
for place in places:
    if place and os.path.isfile(place):
        with open(place) as fh:
            held.append(fh.read())
keys = {"age1" + w[15:].lower() for w in " ".join(held).split() if w.startswith("AGE-SECRET-KEY-")}
# A recipient whose stanza reads `enc: damaged` opens nothing: the file names that key, and the key cannot open it.
with open(sys.argv[-1]) as fh:
    lines = fh.read().splitlines() + [""]
wrapped = {ln.split("recipient:", 1)[1].strip() for ln, nxt in zip(lines, lines[1:])
           if "recipient:" in ln and "enc: damaged" not in nxt}
sys.exit(0 if keys & wrapped else 1)
"""
_FAKE_AGE_KEYGEN = """#!/usr/bin/env python3
# A stand-in age-keygen. `-o FILE` writes a key that is no file's recipient. `-y FILE` prints the recipient of each
# `AGE-SECRET-KEY-<NAME>` line in FILE (`age1<name>`), and fails when FILE is missing or holds none, as age-keygen does.
import sys

if "-o" in sys.argv:
    with open(sys.argv[sys.argv.index("-o") + 1], "w") as fh:
        fh.write("# public key: age1notarecipient\\nAGE-SECRET-KEY-NOTARECIPIENT\\n")
    sys.exit(0)
try:
    with open(sys.argv[sys.argv.index("-y") + 1]) as fh:
        found = ["age1" + ln.strip()[15:].lower() for ln in fh if ln.startswith("AGE-SECRET-KEY-")]
except OSError:
    sys.exit(1)
print("\\n".join(found))
sys.exit(0 if found else 1)
"""
CONTROL = "AGE-SECRET-KEY-CONTROL\n"                                        # the control key: age1control
BREAK_GLASS = "# public key: age1breakglass\nAGE-SECRET-KEY-BREAKGLASS\n"   # the break-glass key, as age-keygen writes
_BASH = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"),
                           reason="runs the runbook's Verify block in bash")


def _verify_block(bgpub="age1breakglass"):
    """The bash block under docs/break-glass.md's "Verify" heading, with its `BGPUB` set to `bgpub` (None: left as
    the runbook writes it, a placeholder)."""
    doc = open(os.path.join(ROOT, "docs", "break-glass.md"), encoding="utf-8").read()
    block = re.search(r"```bash\n(.*?)```", doc[doc.index("## Verify"):], re.S).group(1)
    line = 'BGPUB="age1…"'
    assert block.count(line) == 1, "the block names the break-glass recipient in one BGPUB line"
    return block if bgpub is None else block.replace(line, 'BGPUB="%s"' % bgpub)


def _wrap(path, *recipients, damaged=()):
    """A domain file whose sops metadata names `recipients`; the stanza of each one in `damaged` is one its key cannot
    open."""
    stanzas = ("    - recipient: %s\n      enc: %s\n" % (r, "damaged" if r in damaged else "ok") for r in recipients)
    path.write_text("sops:\n  age:\n" + "".join(stanzas), encoding="utf-8")


def _hostile_machine(tmp_path, bg_key=BREAK_GLASS):
    """A trusted machine on which each key place the stand-in sops reads holds the CONTROL key, with `bg_key` (None:
    no file) at the path the runbook names, and a directory of two domain files: a-both.sops.yml, first in glob order,
    wrapped to both keys, and one.sops.yml wrapped to the control key alone. Returns (the environment, the files'
    directory, the stand-in's NEW_PLACE, empty)."""
    home, xdg, stubs, files = (tmp_path / d for d in ("home", "xdg", "bin", "files"))
    new_place = tmp_path / "elsewhere" / "keys.txt"
    new_place.parent.mkdir()
    for rel in (".config/sops/age/keys.txt", "Library/Application Support/sops/age/keys.txt", ".ssh/id_ed25519",
                ".ssh/id_rsa"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(CONTROL, encoding="utf-8")
    (xdg / "sops" / "age").mkdir(parents=True)
    (xdg / "sops" / "age" / "keys.txt").write_text(CONTROL, encoding="utf-8")
    (tmp_path / "keyservice").write_text(CONTROL, encoding="utf-8")
    if bg_key is not None:
        (home / "kontroll-breakglass.key").write_text(bg_key, encoding="utf-8")
    stubs.mkdir()
    for name, body in (("sops", _FAKE_SOPS % {"new_place": str(new_place)}), ("age-keygen", _FAKE_AGE_KEYGEN)):
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)
    files.mkdir()
    _wrap(files / "a-both.sops.yml", "age1control", "age1breakglass")
    _wrap(files / "one.sops.yml", "age1control")
    ctl = str(home / ".config" / "sops" / "age" / "keys.txt")
    env = dict(os.environ, PATH="%s:%s" % (stubs, os.environ.get("PATH", "")), HOME=str(home), XDG_CONFIG_HOME=str(xdg),
               SOPS_AGE_KEY=CONTROL.strip(), SOPS_AGE_KEY_CMD="cat '%s'" % ctl, SOPS_AGE_KEY_FILE=ctl,
               SOPS_AGE_SSH_PRIVATE_KEY_FILE=str(home / ".ssh" / "id_ed25519"),
               SOPS_AGE_SSH_PRIVATE_KEY_CMD="cat '%s'" % ctl, SOPS_KEYSERVICE=str(tmp_path / "keyservice"))
    return env, files, new_place


def _bash(script, env, cwd, *opts):
    """`script` run by bash (with `opts`, such as `-e`) in `cwd` with `env`; the completed process."""
    return subprocess.run(["bash", *opts, "-c", script], env=env, cwd=str(cwd), capture_output=True, text=True,
                          timeout=60)


@_BASH
def test_the_verify_check_sees_only_the_break_glass_key(tmp_path):
    """docs/break-glass.md's Verify block, run in bash against a stand-in `sops` that uses every key it can find (each
    place `_FAKE_SOPS` names), on a machine where each of those holds the CONTROL key: the file wrapped to both keys
    reads ok, the file wrapped to the control key alone reads FAIL as not wrapped to the break-glass key, the file
    that names the break-glass key in a stanza that key cannot open reads FAIL, and no line reads BROKEN; the old
    one-line form, on the same machine, opens the control-only file. The first file in glob order is wrapped to both
    keys, so a control line that ran the break-glass key, or any isolation dropped, prints BROKEN for it. Guards the
    false pass measured with real sops — `SOPS_AGE_KEY_FILE=… sops -d` opened every file with the control key that a
    proven off-box copy left in ~/.config, so a break-glass key whose private half was lost read `ok` everywhere —
    any one of the block's isolations being dropped, and the break-glass run itself made with the operator's whole
    environment, where the control key opens the damaged file."""
    env, files, _ = _hostile_machine(tmp_path)
    _wrap(files / "b-damaged.sops.yml", "age1control", "age1breakglass", damaged=("age1breakglass",))
    old = _bash('SOPS_AGE_KEY_FILE="$HOME/kontroll-breakglass.key" sops -d one.sops.yml', env, files)
    assert old.returncode == 0, "the stand-in must reproduce the false pass, or this test proves nothing"
    run = _bash(_verify_block(), env, files)
    assert "ok   a-both.sops.yml" in run.stdout, run.stdout + run.stderr
    assert "FAIL one.sops.yml (not wrapped to age1breakglass)" in run.stdout
    assert "FAIL b-damaged.sops.yml" in run.stdout.splitlines()
    assert "BROKEN" not in run.stdout


@_BASH
@pytest.mark.parametrize("leftover,later", [(CONTROL, None), ("AGE-SECRET-KEY-LEFTOVER\n", "age1leftover")],
                         ids=["the-control-key", "a-key-for-a-later-file-only"])
def test_the_verify_check_says_so_when_sops_finds_a_key_it_did_not_empty(tmp_path, leftover, later):
    """When sops reads a key from a place the Verify block cannot empty — a newer sops with a new key location, which
    the stand-in models as NEW_PLACE, outside the home and the environment — the control, a key that is no
    recipient, opens a file and the block prints BROKEN for that file instead of ok: for the control key there, the
    file wrapped to both keys; for a key that only a later file is wrapped to, that file, while the first still reads
    ok. Guards the control being dropped or run on the first file alone: a key the block cannot see then turns a
    file's line into `ok` and nothing tells the operator."""
    env, files, new_place = _hostile_machine(tmp_path)
    new_place.write_text(leftover, encoding="utf-8")
    if later:
        _wrap(files / "c-later.sops.yml", "age1control", "age1breakglass", later)
    run = _bash(_verify_block(), env, files)
    if later:
        assert "ok   a-both.sops.yml" in run.stdout, run.stdout + run.stderr
        assert "BROKEN c-later.sops.yml: a key other than the one named opened it" in run.stdout
        assert "ok   c-later.sops.yml" not in run.stdout
    else:
        assert "BROKEN a-both.sops.yml: a key other than the one named opened it" in run.stdout, run.stdout + run.stderr
        assert "ok   " not in run.stdout


@_BASH
@pytest.mark.parametrize("bg_key,bgpub,expect", [
    (CONTROL, "age1breakglass", "is not the one key whose public half is age1breakglass"),
    (CONTROL + BREAK_GLASS, "age1breakglass", "is not the one key whose public half is age1breakglass"),
    (BREAK_GLASS + CONTROL, "age1breakglass", "is not the one key whose public half is age1breakglass"),
    (BREAK_GLASS, None, "is not the one key whose public half is age1…"),
    ("# public key: age1breakglass\n", "age1breakglass", "age-keygen could not read a key from"),
    (None, "age1breakglass", "age-keygen could not read a key from"),
], ids=["the-control-key", "both-keys-in-one-file", "both-keys-break-glass-first", "BGPUB-left-as-written",
        "the-public-half-only", "no-file"])
def test_the_verify_check_refuses_a_key_file_that_is_not_the_break_glass_key(tmp_path, bg_key, bgpub, expect):
    """When the file at `BG` is not the break-glass key alone — a copy of the control key, a keys.txt holding both,
    the public half without the private one, or no file — or `BGPUB` was left as the runbook writes it, the Verify
    block prints one BROKEN line and no ok line, whichever of two keys comes first. Guards the check proving whatever
    key sits in that file: with the control key there, every file the control key is a recipient of read `ok`, and
    with both keys in it, sops tried each, so the break-glass key was never shown to open anything; and a check of
    the first key `age-keygen -y` prints alone, which the control-key-first file could not tell apart (T11 fold 4's
    read B, N4)."""
    env, files, _ = _hostile_machine(tmp_path, bg_key)
    run = _bash(_verify_block(bgpub), env, files)
    assert "BROKEN: " in run.stdout and expect in run.stdout, run.stdout + run.stderr
    assert "ok   " not in run.stdout and run.stdout.count("\n") == 1


_NO_SOPS = ("BROKEN: no sops runs here as the check runs it (a stock sops build on PATH; a shell function, an alias "
            "or a version-manager shim does not count)\n")


@_BASH
@pytest.mark.parametrize("sops", ["none", "a-shim-that-needs-home", "a-shim-that-needs-its-own-variable",
                                  "a-shell-function"])
def test_the_verify_check_says_so_when_sops_is_missing(tmp_path, sops):
    """On a machine with age-keygen whose sops cannot run as the block runs it — none at all (a PATH that holds the
    shell's own tools and the stand-in age-keygen only), a version-manager shim first on PATH that runs sops only where
    the operator's own home is set, one that runs it only where a variable of its own is set (a version manager under a
    custom root), or a shell function named `sops` with no sops on PATH — the Verify block prints one line, `BROKEN: no
    sops runs here as the check runs it`, and no FAIL or ok line. Guards a sops the block cannot run reading as a bare
    `FAIL` for every file — the line the runbook gives for a damaged break-glass stanza, whose remedy (a data-key
    rotation on the node) cannot help (T11 fold 4's read B, N3) — and a check that asks only whether the shell finds a
    sops (`command -v`), which a function, an alias or a shim passes while the block's `env -i` runs none (fold 5's
    reads, 70 S1 and 80 N5), or a probe that keeps the environment, which the shim keyed on its own variable passes
    (fold 6's read 100, S2)."""
    env, files, _ = _hostile_machine(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "env", "mktemp", "rm", "grep", "python3"):
        os.symlink(shutil.which(name), str(tools / name))
    shutil.copy(str(tmp_path / "bin" / "age-keygen"), str(tools / "age-keygen"))
    env["PATH"] = str(tools)
    block = _verify_block()
    if sops == "a-shim-that-needs-home":
        (tmp_path / "home" / ".shim-ok").write_text("", encoding="utf-8")
        shim = tools / "sops"
        shim.write_text('#!/usr/bin/env bash\n[ -f "$HOME/.shim-ok" ] || exit 126\nexec python3 "%s" "$@"\n'
                        % (tmp_path / "bin" / "sops"), encoding="utf-8")
        shim.chmod(0o755)
        assert _bash("sops --help", env, files).returncode == 0, "the shim runs in the operator's own shell"
    elif sops == "a-shim-that-needs-its-own-variable":
        env["SHIM_ROOT"] = str(tmp_path)
        shim = tools / "sops"
        shim.write_text('#!/usr/bin/env bash\n[ -n "${SHIM_ROOT:-}" ] || exit 126\nexec python3 "%s" "$@"\n'
                        % (tmp_path / "bin" / "sops"), encoding="utf-8")
        shim.chmod(0o755)
        assert _bash("sops --help", env, files).returncode == 0, "the shim runs in the operator's own shell"
    elif sops == "a-shell-function":
        block = 'sops() { python3 "%s" "$@"; }\n' % (tmp_path / "bin" / "sops") + block
    run = _bash(block, env, files)
    assert run.stdout == _NO_SOPS, run.stdout + run.stderr


def test_the_verify_block_holds_no_comment():
    """The Verify block holds no `#`. Guards the block failing when pasted into an interactive zsh, macOS's default
    shell: with INTERACTIVE_COMMENTS off there, `BG="…"   # …` runs `#` as a command with BG set for it alone, so BG
    is never set and the block blames age-keygen for an empty path (T11 fold 4's read A, N1)."""
    assert "#" not in _verify_block(None)


@_BASH
def test_the_verify_check_runs_without_age_keygen_under_errexit(tmp_path):
    """On a machine with sops but no age-keygen (a stand-in that exits 127, as a missing command does), the Verify
    block run with `bash -euo pipefail` still prints a BROKEN line naming age-keygen, prints no ok line, and exits 0.
    Guards the block stopping with no output under an operator's `set -e`, as a bare `age-keygen -o` did, or reading
    ok with no control and no check of the key file."""
    env, files, _ = _hostile_machine(tmp_path)
    keygen_stub = tmp_path / "bin" / "age-keygen"
    keygen_stub.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    run = _bash(_verify_block(), env, files, "-euo", "pipefail")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "BROKEN: age-keygen could not read a key from" in run.stdout and "ok   " not in run.stdout
