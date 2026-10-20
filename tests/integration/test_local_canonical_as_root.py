"""`local-canonical.yml` run for real, as root — the installer's mode — on a scratch node: the deploy after a
landing reports 0 changed (CLAUDE.md: "a second apply must report 0 changed"; C10, rule 8 of the canonical's doors).

WHY: the play lands the worktree's HEAD on the canonical's `main` (`scripts/kontroll-land.py`). Run by root, the
landing's canonical-side git is the service uid (rule 5 of `kontroll.canon`), so what it leaves — the pack and its
index under `objects/pack/`, `refs/heads/main`, the reflog — is uid 1001's; run by the operator it is the
operator's; and git writes every pack and its index read-only (`0444`) either way. The play's ownership pass
(`kontroll-canonical.py --normalise`, F-CANON) names all of that: left for the next deploy, that deploy re-owned
and re-moded it and reported `changed` once after every landing (measured 2026-10-20 in the gate image:
`changed=1` on the run after a landing by root). The pass therefore runs a second time, after the landing, in the
same run. These rows run the play itself — a fresh node's `--check --diff`, an apply that lands, a second and a
third apply, a closing `--check --diff` — and hold the counts; a parsed-task row cannot say what a run reports.

It needs ROOT, Linux and ansible-playbook: the play's `become` tasks and the landing's change of identity are
real here. tests/validate.sh runs this file in its root step; in a plain `pytest` by an ordinary user every row
is skipped. As the installer does, the play is run with `KONTROLL_IN_CONTAINER=1` (no host group gesture) and the
operator's identity passed in `KONTROLL_OPERATOR_{USER,UID}`; `become` is through `su` (root to root needs no
password; the image has no sudo), as the reader of this change ran it; and a global `safe.directory '*'` stands in
for the installer image's system-wide entry (root's git in the operator's canonical and worktree).
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tests", "unit"))

from kontroll import canon  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not (hasattr(os, "geteuid") and os.geteuid() == 0 and sys.platform.startswith("linux")
                            and all(shutil.which(tool) for tool in ("git", "ansible-playbook", "su", "sh"))),
                       reason="needs root on Linux with ansible-playbook and su (it runs the canonical play, whose "
                              "landing changes identity) — tests/validate.sh runs it as root"),
]

OPERATOR = 1000                     # the play's `operator_uid`: neither root nor the service
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "local-canonical.yml")
_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
# A tree the promote gate judges clean (the shapes tests/unit/test_land.py uses), plus this tree's `scripts/`:
# the play runs the fleet check, the plaintext guard, the audit and the landing from the worktree's own copy.
_CLEAN = {"modules/_core.yml": "collections: []\n", "config/fleet.yml": "enabled_modules: [alpha, beta]\n",
          "modules/alpha/module.yml": "key: alpha\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n",
          "modules/beta/module.yml": "key: beta\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n"}


@pytest.fixture
def node():
    """A scratch node: a base directory every uid can enter (`tmp_path` is root's alone), the operator's checkout
    in it — this tree's `scripts/` and a clean registry, one commit, `core.autocrlf` off — and the path the
    canonical will have. Nothing of the developer's git reaches the play (`isolated_git`); the play's own gits in
    the worktree run as root in root's repository."""
    base = tempfile.mkdtemp(prefix="kontroll-play-", dir="/tmp")
    os.chmod(base, 0o755)
    wt = os.path.join(base, "checkout")
    shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(wt, "scripts"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in _CLEAN.items():
        path = os.path.join(wt, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    env = dict(os.environ, **_WHO)
    for argv in (["init", "-q", "-b", "main"], ["config", "core.autocrlf", "false"], ["add", "-A"],
                 ["commit", "-q", "-m", "the node's checkout"]):
        subprocess.run(["git", "-C", wt] + argv, check=True, capture_output=True, env=env)
    try:
        yield type("Node", (), {"base": base, "wt": wt, "bare": os.path.join(base, "canonical.git")})()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _play(node, check=False):
    """Run the play as the installer does — root, `KONTROLL_IN_CONTAINER=1`, the operator's identity in the
    environment, `become` through `su` — against the node's canonical and checkout. Returns the recap's counters
    and the output; a failed run fails the row with its output."""
    home = os.path.join(node.base, "ansible-home")
    os.makedirs(home, exist_ok=True)
    cfg = os.path.join(home, "ansible.cfg")                   # nothing of the developer's or the repository's
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write("[defaults]\n")
    # The play's own gits run as root in a canonical it makes the OPERATOR's (`file`: owner operator_uid) and in
    # the operator's worktree: git refuses a repository another uid owns unless `safe.directory` names it (`git
    # config` says "not in a git directory"). The installer image, where the play runs as root, sets
    # `safe.directory '*'` system-wide (`docker/installer/Dockerfile`; pinned by tests/unit/test_local_canonical.py).
    # `isolated_git` names an empty global config, so this stands in for the image's entry.
    gitconfig = _gitconfig(node)
    with open(gitconfig, "w", encoding="utf-8") as fh:
        fh.write("[safe]\n\tdirectory = *\n")
    argv = ["ansible-playbook", "-i", "localhost,", PLAYBOOK,
            "-e", json.dumps({"bare": node.bare, "worktree": node.wt})]
    if check:
        argv += ["--check", "--diff"]
    env = dict(os.environ, ANSIBLE_HOME=home, ANSIBLE_LOCAL_TEMP=os.path.join(home, "tmp"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_PYTHON_INTERPRETER="auto_silent",
               ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_CONFIG=cfg, ANSIBLE_BECOME_METHOD="su",
               KONTROLL_IN_CONTAINER="1", KONTROLL_OPERATOR_USER="operator", KONTROLL_OPERATOR_UID=str(OPERATOR),
               GIT_CONFIG_GLOBAL=gitconfig)
    p = subprocess.run(argv, cwd=node.base, env=env, capture_output=True, text=True, timeout=900)
    out = p.stdout + p.stderr
    recap = re.search(r"localhost\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", p.stdout)
    assert p.returncode == 0 and recap, "ansible-playbook rc=%d\n%s" % (p.returncode, out)
    print("local-canonical.yml%s: %s" % (" --check --diff" if check else "", recap.group(0)))   # on record under -s
    return {"changed": int(recap.group(2)), "failed": int(recap.group(4)), "out": out}


def _gitconfig(node):
    """The global git config the play and the row's own gits run with (`[safe] directory = *`)."""
    home = os.path.join(node.base, "ansible-home")
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "gitconfig")


def _head(wt):
    return subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def _left_by_the_landing(bare):
    """The pack, its index and the ref the landing wrote, with each one's owner and mode."""
    paths = [os.path.join(bare, "objects", "pack", n) for n in os.listdir(os.path.join(bare, "objects", "pack"))]
    paths.append(os.path.join(bare, "refs", "heads", "main"))
    return {os.path.relpath(p, bare): (os.lstat(p).st_uid, os.lstat(p).st_gid, stat.S_IMODE(os.lstat(p).st_mode))
            for p in paths}


def test_the_deploy_after_a_landing_reports_0_changed(node):
    """A fresh node's `--check --diff` does not fail. The first apply lands HEAD as a first seed (`main` is the
    checkout's commit), reports it changed, and leaves the pack, its index and the ref the OPERATOR's
    (`<operator>:1001` — the pass ran again after the landing; the landing itself wrote them as uid 1001), the
    ref group-writable. The second apply reports 0 changed — nothing of the canonical is the pass's to change,
    the landing says `up to date` — and so do the third and a closing `--check --diff`; `main` stays. And a hand
    `git push` of `main` from the checkout is refused by the installed hook, naming the door.

    Guards the rule itself, at the play: without the pass after the landing the second apply re-owned what the
    first left and reported `changed=1` (measured; `tests/unit/test_local_canonical.py` pins the pair's place,
    this holds what a run reports). And it guards the play as a whole being runnable in the installer's mode —
    no `sudo`, no host group gesture — which no parsed-task row can show."""
    assert _play(node, check=True)["failed"] == 0, "the mandated dry-run of a fresh node must not fail"
    assert not os.path.exists(node.bare), "a dry-run made the canonical"
    first = _play(node)
    assert first["changed"] > 0 and first["failed"] == 0
    assert canon.read_ref(node.bare, canon.MAIN) == (_head(node.wt), "commit", ""), first["out"]
    left = _left_by_the_landing(node.bare)                   # (a task's stdout is not in the default callback's output)
    assert any(name.endswith(".pack") for name in left), left
    for name, (uid, gid, mode) in left.items():
        assert (uid, gid) == (OPERATOR, canon.SERVICE_GID), (name, uid, gid, "the pass after the landing re-owns it")
        assert mode & 0o020, (name, oct(mode), "the pass after the landing gives the group its write bit")
    second = _play(node)
    assert second["changed"] == 0 and second["failed"] == 0, (
        "the second apply after a landing must report 0 changed:\n" + second["out"])
    third = _play(node)
    assert third["changed"] == 0 and third["failed"] == 0, third["out"]
    closing = _play(node, check=True)
    assert closing["changed"] == 0 and closing["failed"] == 0, closing["out"]
    assert canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt)
    # a NEW commit pushed by hand (a push of what `main` holds already is "up-to-date" and runs no hook), with
    # the same `safe.directory` stand-in the play ran with — so that it is the HOOK that refuses it, naming the
    # door, and not git's ownership check; `main` does not move
    landed = canon.read_ref(node.bare, canon.MAIN)[0]
    with open(os.path.join(node.wt, "by-hand.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("pushed by hand\n")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=_gitconfig(node), **_WHO)
    subprocess.run(["git", "-C", node.wt, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", node.wt, "commit", "-q", "-m", "by hand"], check=True, capture_output=True, env=env)
    pushed = subprocess.run(["git", "-C", node.wt, "push", "local", "HEAD:refs/heads/main"], capture_output=True,
                            text=True, env=env)
    assert pushed.returncode != 0 and "kontroll-land.py" in pushed.stderr, pushed.stderr
    assert canon.read_ref(node.bare, canon.MAIN)[0] == landed


def test_a_new_commit_is_landed_by_the_next_deploy_and_the_one_after_reports_0_changed(node):
    """After the first apply, a new commit in the checkout: the next apply lands it (changed) — the pack it adds
    is the operator's and group-writable when the run ends — and the apply after that reports 0 changed again.
    Guards the rule on the second landing of a node's life, where the first pair of the pass has nothing to do
    and only the pair after the landing can leave the canonical as the next run expects it."""
    _play(node)
    with open(os.path.join(node.wt, "note.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("a change\n")
    env = dict(os.environ, **_WHO)
    subprocess.run(["git", "-C", node.wt, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", node.wt, "commit", "-q", "-m", "a change"], check=True, capture_output=True, env=env)
    landed = _play(node)
    assert landed["changed"] > 0, landed["out"]
    assert canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt), landed["out"]
    for name, (uid, gid, mode) in _left_by_the_landing(node.bare).items():
        assert (uid, gid) == (OPERATOR, canon.SERVICE_GID) and mode & 0o020, (name, uid, gid, oct(mode))
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]
