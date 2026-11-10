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
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the mandated dry-run of a fresh node must not fail:\n" + dry["out"]
    assert "holds no canonical yet (it is not there)" in dry["out"], dry["out"]
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


def test_a_first_install_whose_canonical_path_is_an_empty_directory_passes_its_dry_run_and_lands(node):
    """The node's canonical path is an empty root-owned `0755` directory, as Docker leaves a bind source that is
    missing (the installer's compose service binds `/srv/kontroll.git`, so after `fresh-init` a fresh node has
    one). Its `--check --diff` does not fail, says the real run will seed the canonical, names no audit finding,
    and leaves the directory as it found it; the first apply then lands HEAD as a first seed, and the second
    reports 0 changed.

    Guards the dry-run of every first install through the installer: the play took that directory for a
    canonical, audited it (config-unread, head, hook-missing) and judged the landing against it, which cannot
    read a repository that is not one, so the dry-run stopped at the refusal (measured 2026-11-07: the
    quickstart as written, on a GitHub-hosted runner). The row above starts from no path at all, which never
    happens through the installer."""
    os.mkdir(node.bare)
    os.chmod(node.bare, 0o755)
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the mandated dry-run of a first install must not fail:\n" + dry["out"]
    assert "holds no canonical yet (it is an empty directory" in dry["out"] and "first seed" in dry["out"], dry["out"]
    for finding in ("config-unread", "hook-missing", "head: HEAD"):
        assert finding not in dry["out"], (finding, "an empty directory has nothing to audit")
    assert os.listdir(node.bare) == [] and os.lstat(node.bare).st_uid == 0, "a dry-run wrote into the directory"
    assert stat.S_IMODE(os.lstat(node.bare).st_mode) == 0o755
    first = _play(node)
    assert first["changed"] > 0 and first["failed"] == 0
    assert canon.read_ref(node.bare, canon.MAIN) == (_head(node.wt), "commit", ""), first["out"]
    second = _play(node)
    assert second["changed"] == 0 and second["failed"] == 0, second["out"]


def _elsewhere(node):
    """A directory beside the node's canonical path, as a second disk's mount point would be — root's, `0755` —
    and the path in it that a link at the canonical's path names (nothing is made there)."""
    disk = os.path.join(node.base, "disk")
    os.mkdir(disk)
    os.chmod(disk, 0o755)
    return os.path.join(disk, "canonical.git")


def test_a_canonical_path_that_is_a_link_to_an_empty_directory_passes_its_dry_run_and_lands_there(node):
    """The node's canonical path is a symbolic link to an empty root-owned `0755` directory elsewhere (the canonical
    kept on another disk). Its `--check --diff` does not fail: it says the real run will seed the canonical (the
    directory is empty), names no audit finding, refuses nothing and writes nothing. The first apply lands HEAD as a
    first seed IN the directory the link names, the link left a link; the second reports 0 changed.

    Guards the dry run of such a node. The play looked at the path without following it, so a link read as no
    directory, was never listed, and was judged as a repository: the dry run refused ("the canonical could not be
    read") while the real run, whose `file` and `git init` follow the link, landed (I-40, from I-19's read A,
    measured on both cores)."""
    target = _elsewhere(node)
    os.mkdir(target)
    os.chmod(target, 0o755)
    os.symlink(target, node.bare)
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the mandated dry-run must not fail:\n" + dry["out"]
    assert "holds no canonical yet (it is an empty directory" in dry["out"] and "first seed" in dry["out"], dry["out"]
    assert "REFUSING to land" not in dry["out"], dry["out"]
    for finding in ("config-unread", "hook-missing", "head: HEAD", "unread"):
        assert finding not in dry["out"], (finding, "an empty directory has nothing to audit")
    assert os.path.islink(node.bare) and os.listdir(target) == [] and os.lstat(target).st_uid == 0, \
        "a dry-run wrote there"
    first = _play(node)
    assert first["changed"] > 0 and first["failed"] == 0, first["out"]
    assert os.path.islink(node.bare) and os.readlink(node.bare) == target, "the link is left a link"
    assert canon.read_ref(target, canon.MAIN) == (_head(node.wt), "commit", ""), first["out"]
    second = _play(node)
    assert second["changed"] == 0 and second["failed"] == 0, second["out"]


def test_a_canonical_path_that_is_a_link_to_a_landed_canonical_is_audited_judged_and_deploys_0_changed(node):
    """A landed canonical moved to a directory elsewhere, and a symbolic link to it left at its path. Its `--check
    --diff` does not fail: the audit reads it (its message of NO REPORT is not shown), the landing is judged, nothing is refused and nothing
    is said to be missing. The apply after it reports 0 changed, the link is left a link, and `main` stays the
    checkout's commit.

    Guards the fix going too far: before the play's look at the path followed a link, a link to a canonical was
    audited and judged already (the audit and the landing open the path, through the link), and following it must
    leave that so (I-40)."""
    first = _play(node)
    assert first["failed"] == 0 and canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt), first["out"]
    target = _elsewhere(node)
    os.rename(node.bare, target)
    os.symlink(target, node.bare)
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the dry run of a linked canonical must not fail:\n" + dry["out"]
    for said in ("holds no canonical yet", "holds a repository without HEAD", "REFUSING to land",
                 "local-canonical: the audit gave NO REPORT"):
        assert said not in dry["out"], (said, dry["out"])
    real = _play(node)
    assert real["changed"] == 0 and real["failed"] == 0, real["out"]
    assert os.path.islink(node.bare) and os.readlink(node.bare) == target, "the link is left a link"
    assert canon.read_ref(target, canon.MAIN)[0] == _head(node.wt)


def test_a_canonical_path_that_is_a_link_to_nothing_passes_its_dry_run_and_the_real_run_makes_what_it_names(node):
    """The node's canonical path is a symbolic link to a path that is not there (the directory it names lives on a
    disk that is not mounted, say). Its `--check --diff` does not fail: it says the path holds no canonical yet (it
    is not there), audits nothing, refuses nothing and writes nothing (the link is left a link to nothing). The first
    apply makes the directory the link names (`file` follows it) and lands HEAD there as a first seed; the second
    reports 0 changed.

    Guards the dry run saying what the real run does here too. Looked at without following it, the link was a path
    that is no directory: it was audited (`unread: the top directory`) and judged as a repository, and the dry run
    refused while the real run made the directory and landed (I-40). The real run's seeding through such a link is
    as it was; whether the play should refuse a link to nothing is not decided."""
    target = _elsewhere(node)
    os.symlink(target, node.bare)
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the mandated dry-run must not fail:\n" + dry["out"]
    assert "holds no canonical yet (it is not there)" in dry["out"] and "first seed" in dry["out"], dry["out"]
    assert "REFUSING to land" not in dry["out"] and "unread" not in dry["out"], dry["out"]
    assert os.path.islink(node.bare) and not os.path.exists(target), "a dry-run made the canonical"
    first = _play(node)
    assert first["changed"] > 0 and first["failed"] == 0, first["out"]
    assert os.path.islink(node.bare) and os.readlink(node.bare) == target, "the link is left a link"
    assert canon.read_ref(target, canon.MAIN) == (_head(node.wt), "commit", ""), first["out"]
    second = _play(node)
    assert second["changed"] == 0 and second["failed"] == 0, second["out"]


def test_a_canonical_path_that_holds_entries_and_no_repository_is_audited_and_its_dry_run_passes(node):
    """The node's canonical path is a directory that holds an entry and no repository: the root of a filesystem
    mounted at the path, which holds `lost+found/` (root's, `0700`) and nothing else. Its `--check --diff` does not
    fail: the audit runs and names the entry, the ownership pass names it too (the real run makes it the operator's,
    group-writable), the dry run says the real run will seed the canonical, nothing is judged and nothing is
    written. The first apply makes the canonical there and lands HEAD as a first seed, and the second reports 0
    changed.

    Guards the dry run of such a node. Counted as a canonical, it was also judged as a repository, which it is not,
    so the dry run stopped at the refusal ("the canonical could not be read") while the real run landed: I-19's own
    symptom on a path that is not empty (I-19's read B, measured on both cores). And its preview of the ownership
    pass, which the first fold skipped with the judgement (I-19's read A)."""
    os.mkdir(node.bare)
    os.chmod(node.bare, 0o755)
    lost = os.path.join(node.bare, "lost+found")
    os.mkdir(lost)
    os.chmod(lost, 0o700)
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the mandated dry-run must not fail:\n" + dry["out"]
    assert "top-entry: lost+found" in dry["out"], ("the audit names what is there", dry["out"])
    assert re.search(r"\b(owner|mode): lost\+found\b", dry["out"]), ("the ownership pass names it", dry["out"])
    assert "holds no canonical yet (it holds entries but no repository" in dry["out"], dry["out"]
    assert "first seed" in dry["out"] and "REFUSING to land" not in dry["out"], dry["out"]
    assert os.listdir(node.bare) == ["lost+found"] and os.lstat(node.bare).st_uid == 0, "a dry-run wrote there"
    first = _play(node)
    assert first["changed"] > 0 and first["failed"] == 0, first["out"]
    assert canon.read_ref(node.bare, canon.MAIN) == (_head(node.wt), "commit", ""), first["out"]
    second = _play(node)
    assert second["changed"] == 0 and second["failed"] == 0, second["out"]


def test_a_canonical_with_a_top_level_name_that_is_not_utf8_is_audited_judged_and_deployed(node):
    """A landed canonical with a regular file named `b"\\xff\\xfe"` planted at its top level beside `HEAD` (the
    top level is setgid and group-writable for gid 1001, so the service's writer can make one). Its `--check --diff`
    does not fail: the audit names the entry, escaped, the landing is judged and nothing is refused. The apply
    after it re-owns the entry (changed) and fails nothing, and the next reports 0 changed; the entry is left there.

    Guards the deploy of such a canonical. The decision listed the path with `find`, which returns every name, and
    on both cores a name that is not UTF-8 failed the module's result (2.19: "Refusing to deserialize an invalid
    UTF8 string value"; 2.18: "returned non UTF-8 data in the JSON response"), before the audit whose job is to name
    it; `deploy-stack.yml` imports the play, so every deploy stopped (I-19's read A, measured on both cores). The
    listing prints a word, never a name."""
    first = _play(node)
    assert first["failed"] == 0 and canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt), first["out"]
    odd = os.path.join(os.fsencode(node.bare), b"\xff\xfe")
    with open(odd, "wb") as fh:
        fh.write(b"planted\n")
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the dry run of a canonical with that name must not fail:\n" + dry["out"]
    assert re.search(r"top-entry: \\+xff\\+xfe", dry["out"]), ("the audit names the entry", dry["out"])
    assert "holds no canonical yet" not in dry["out"] and "REFUSING to land" not in dry["out"], dry["out"]
    real = _play(node)
    assert real["changed"] > 0 and real["failed"] == 0, real["out"]
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]
    assert os.path.exists(odd) and canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt)


def test_a_canonical_whose_head_is_gone_is_said_so_by_its_dry_run_and_the_real_run_restores_it(node):
    """A landed canonical whose `HEAD` was removed. Its `--check --diff` does not fail: the audit names the missing
    `HEAD` (`head: HEAD`), and the note says the path holds a repository without HEAD and that the real run restores
    HEAD and then judges the landing, not that it holds no canonical or that HEAD lands as a first seed; nothing is
    written. The apply restores `HEAD` (`ref: refs/heads/main`), `main` stays the checkout's commit, and the next
    apply reports 0 changed.

    Guards the words of that dry run: they said "holds no canonical yet … lands HEAD as a first seed" of a repository
    that may hold a promoted commit the worktree lacks, and its real run then refuses the landing (I-19's read A,
    measured on both cores)."""
    first = _play(node)
    assert first["failed"] == 0, first["out"]
    os.unlink(os.path.join(node.bare, "HEAD"))
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the dry run of a repository without HEAD must not fail:\n" + dry["out"]
    assert "head: HEAD" in dry["out"], ("the audit names the missing HEAD", dry["out"])
    assert "holds a repository without HEAD" in dry["out"], dry["out"]
    assert "restores HEAD, then judges the landing" in dry["out"], dry["out"]
    assert "holds no canonical yet" not in dry["out"] and "first seed" not in dry["out"], dry["out"]
    assert not os.path.exists(os.path.join(node.bare, "HEAD")), "a dry-run wrote HEAD"
    real = _play(node)
    assert real["changed"] > 0 and real["failed"] == 0, real["out"]
    with open(os.path.join(node.bare, "HEAD"), encoding="utf-8") as fh:
        assert fh.read() == "ref: refs/heads/main\n"
    assert canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt)
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]


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
