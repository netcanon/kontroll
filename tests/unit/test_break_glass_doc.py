"""docs/break-glass.md — the procedure an operator runs against an existing instance's secrets — spells the commands
the way the tools take them.

WHY. The runbook adds a recipient to every domain and re-wraps the ciphertext. A command in it that the tool spells
differently fails half-way through a procedure on live secrets; a blanket `git add` in it would commit whatever else
the operator's checkout holds alongside the re-wrapped files. The commands come from the code: the re-wrap is
`keygen.SOPS_UPDATEKEYS`, the flag is kontroll-init's own.
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
    its tools, which is why the runbook runs them in the installer shell (MF-04). Guards the runbook drifting from the
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
    """The "Before you start" list fast-forwards the checkout to the canonical before the rollback tag, and step 3's
    dry run tells the operator to answer **n** to every file. Guards a re-wrap committed on a stale base — the landing
    then refuses, and its remedy is a rebase over ciphertext a promoted proposal re-encrypted, which cannot merge
    (S4) — and a dry run that writes."""
    doc = open(os.path.join(ROOT, "docs", "break-glass.md"), encoding="utf-8").read()
    assert doc.index("git fetch local && git merge --ff-only local/main") < doc.index("git tag pre-break-glass")
    step3 = doc[doc.index("**Step 3"):doc.index("**Step 4")]
    assert "Answer **n** to every file" in step3 and "**y**" not in step3


_FAKE_SOPS = """#!/usr/bin/env python3
# A stand-in sops for `sops -d FILE`: exit 0 when any key it can find is one of FILE's `recipient:` lines. Like
# sops 3.13 it reads every place a key can be, so a key left in any of them opens the file.
import os
import subprocess
import sys

env, home = os.environ, os.environ.get("HOME", "")
held = [env.get("SOPS_AGE_KEY", "")]
if env.get("SOPS_AGE_KEY_CMD"):
    held.append(subprocess.run(env["SOPS_AGE_KEY_CMD"], shell=True, capture_output=True, text=True).stdout)
places = [env.get("SOPS_AGE_KEY_FILE"), env.get("SOPS_AGE_SSH_PRIVATE_KEY_FILE"), env.get("FAKE_SOPS_NEW_PLACE"),
          os.path.join(home, ".config", "sops", "age", "keys.txt"),
          os.path.join(home, "Library", "Application Support", "sops", "age", "keys.txt"),
          os.path.join(home, ".ssh", "id_ed25519")]
if env.get("XDG_CONFIG_HOME"):
    places.append(os.path.join(env["XDG_CONFIG_HOME"], "sops", "age", "keys.txt"))
for place in places:
    if place and os.path.isfile(place):
        with open(place) as fh:
            held.append(fh.read())
with open(sys.argv[-1]) as fh:
    wrapped = [ln.split("recipient:", 1)[1].strip() for ln in fh if "recipient:" in ln]
sys.exit(0 if any(r in " ".join(held).split() for r in wrapped) else 1)
"""
_FAKE_AGE_KEYGEN = """#!/usr/bin/env python3
# A stand-in age-keygen for `age-keygen -o FILE`: writes a key that is no file's recipient.
import sys

with open(sys.argv[sys.argv.index("-o") + 1], "w") as fh:
    print("# public key: age1notarecipient", file=fh)
"""


def _verify_block():
    """The bash block under docs/break-glass.md's "Verify" heading."""
    doc = open(os.path.join(ROOT, "docs", "break-glass.md"), encoding="utf-8").read()
    return re.search(r"```bash\n(.*?)```", doc[doc.index("## Verify"):], re.S).group(1)


def _hostile_machine(tmp_path):
    """A trusted machine on which every place sops 3.13 reads a key holds the CONTROL key, with the break-glass key at
    the path the runbook names, and a directory of two domain files: one wrapped to the control key alone, one to both.
    Returns (the environment, the files' directory)."""
    home, xdg, stubs, files = (tmp_path / d for d in ("home", "xdg", "bin", "files"))
    control = "# public key: age1control\n"
    for rel in (".config/sops/age/keys.txt", "Library/Application Support/sops/age/keys.txt", ".ssh/id_ed25519"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(control, encoding="utf-8")
    (xdg / "sops" / "age").mkdir(parents=True)
    (xdg / "sops" / "age" / "keys.txt").write_text(control, encoding="utf-8")
    (home / "kontroll-breakglass.key").write_text("# public key: age1breakglass\n", encoding="utf-8")
    stubs.mkdir()
    for name, body in (("sops", _FAKE_SOPS), ("age-keygen", _FAKE_AGE_KEYGEN)):
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)
    files.mkdir()
    (files / "one.sops.yml").write_text("sops:\n  age:\n    - recipient: age1control\n", encoding="utf-8")
    (files / "two.sops.yml").write_text(
        "sops:\n  age:\n    - recipient: age1control\n    - recipient: age1breakglass\n", encoding="utf-8")
    ctl = str(home / ".config" / "sops" / "age" / "keys.txt")
    env = dict(os.environ, PATH="%s:%s" % (stubs, os.environ.get("PATH", "")), HOME=str(home), XDG_CONFIG_HOME=str(xdg),
               SOPS_AGE_KEY="age1control", SOPS_AGE_KEY_CMD="cat '%s'" % ctl, SOPS_AGE_KEY_FILE=ctl,
               SOPS_AGE_SSH_PRIVATE_KEY_FILE=str(home / ".ssh" / "id_ed25519"))
    return env, files


def _bash(script, env, cwd):
    """`script` run by bash in `cwd` with `env`; the completed process."""
    return subprocess.run(["bash", "-c", script], env=env, cwd=str(cwd), capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="runs the runbook's Verify block in bash")
def test_the_verify_check_sees_only_the_break_glass_key(tmp_path):
    """docs/break-glass.md's Verify block, run in bash against a stand-in `sops` that — like sops 3.13 — uses every key
    it can find (SOPS_AGE_KEY_FILE, SOPS_AGE_KEY, SOPS_AGE_KEY_CMD, SOPS_AGE_SSH_PRIVATE_KEY_FILE, the keys.txt under
    $XDG_CONFIG_HOME, ~/.config and ~/Library, ~/.ssh/id_ed25519), on a machine where each of those holds the CONTROL
    key: a file wrapped to the control key alone reads FAIL, a file wrapped to both reads ok, and the control line is
    silent; the old one-line form, on the same machine, opens the control-only file. Guards the false pass the T11
    review measured — `SOPS_AGE_KEY_FILE=… sops -d` opened every file with the control key a proven off-box copy left
    in ~/.config, so a break-glass key whose private half was lost read `ok` everywhere — and any one of the block's
    isolations being dropped."""
    env, files = _hostile_machine(tmp_path)
    old = _bash('SOPS_AGE_KEY_FILE="$HOME/kontroll-breakglass.key" sops -d one.sops.yml', env, files)
    assert old.returncode == 0, "the stand-in must reproduce the false pass, or this test proves nothing"
    run = _bash(_verify_block(), env, files)
    assert "FAIL one.sops.yml" in run.stdout and "ok   two.sops.yml" in run.stdout, run.stdout + run.stderr
    assert "BROKEN" not in run.stdout


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="runs the runbook's Verify block in bash")
def test_the_verify_check_says_so_when_sops_finds_a_key_it_did_not_empty(tmp_path):
    """When sops reads a key from a place the Verify block does not empty — a newer sops with a new key location,
    which the stand-in models as FAKE_SOPS_NEW_PLACE — the block's control line, a key that is no recipient, opens a
    file and prints THIS CHECK IS BROKEN. Guards the control line being dropped or made unable to fail: without it, a
    key the block cannot see turns every line into `ok` and nothing tells the operator."""
    env, files = _hostile_machine(tmp_path)
    env["FAKE_SOPS_NEW_PLACE"] = env["SOPS_AGE_KEY_FILE"]
    run = _bash(_verify_block(), env, files)
    assert "THIS CHECK IS BROKEN HERE: another key opened one.sops.yml" in run.stdout, run.stdout + run.stderr
