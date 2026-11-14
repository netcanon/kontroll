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

And the worktree is the OPERATOR'S (T25): the play writes it — the `local` remote, the instance-state commit,
the sync that brings it forward after a promote — as the operator (`setpriv`), so nothing of root's is left in it
for the operator's own git to trip on. The rows after the first two bring forward a promote the worktree never
saw — the run that does it STOPS, and the next one lands — and hold what the play refuses before it writes there.

It needs ROOT, Linux and ansible-playbook: the play's `become` tasks and the landing's change of identity are
real here. tests/validate.sh runs this file in its root step; in a plain `pytest` by an ordinary user every row
is skipped. As the installer does, the play is run with `KONTROLL_IN_CONTAINER=1` (no host group gesture) and the
operator's identity passed in `KONTROLL_OPERATOR_{USER,UID,GID}`; `become` is through `su` (root to root needs
no password; the image has no sudo), as the reader of this change ran it; a global `safe.directory '*'` stands in
for the installer image's system-wide entry (root's git in the operator's canonical and worktree); and the rows'
own commits in the checkout are the operator's too (`_operator_git`), as an operator's are.
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
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tests", "unit"))

from kontroll import canon, gitio  # noqa: E402

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
STOP_PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "_stop-after-sync.yml")    # bootstrap.yml's, after the stamp
STOPPED = "STOPPED before landing, deploying or running the worktree's scripts"       # the stop after a sync's lead
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
    canonical will have — the checkout owned by the operator (`OPERATOR:OPERATOR`), as an operator's is. Nothing
    of the developer's git reaches the play (`isolated_git`)."""
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
    for where, dirs, names in os.walk(wt):
        for name in [where] + [os.path.join(where, n) for n in dirs + names]:
            os.lchown(name, OPERATOR, OPERATOR)
    try:
        yield type("Node", (), {"base": base, "wt": wt, "bare": os.path.join(base, "canonical.git")})()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _run(node, check=False, gid=OPERATOR, playbook=PLAYBOOK, extra=None, cfg=None):
    """Run the play (`playbook`: local-canonical.yml unless named) as the installer does — root,
    `KONTROLL_IN_CONTAINER=1`, the operator's identity in the environment (`gid`: `KONTROLL_OPERATOR_GID`),
    `become` through `su` — against the node's canonical and checkout; `extra`, more `-e` variables (a caller's
    `kontroll_run_id`); `cfg`, an Ansible config of the row's own in place of an empty one. Returns (the exit
    status, the recap's match or None, the output)."""
    home = os.path.join(node.base, "ansible-home")
    os.makedirs(home, exist_ok=True)
    if cfg is None:
        cfg = os.path.join(home, "ansible.cfg")               # nothing of the developer's or the repository's
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
    argv = ["ansible-playbook", "-i", "localhost,", playbook,
            "-e", json.dumps({"bare": node.bare, "worktree": node.wt})]
    if extra:
        argv += ["-e", json.dumps(extra)]
    if check:
        argv += ["--check", "--diff"]
    env = dict(os.environ, ANSIBLE_HOME=home, ANSIBLE_LOCAL_TEMP=os.path.join(home, "tmp"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_PYTHON_INTERPRETER="auto_silent",
               ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_CONFIG=cfg, ANSIBLE_BECOME_METHOD="su",
               KONTROLL_IN_CONTAINER="1", KONTROLL_OPERATOR_USER="operator", KONTROLL_OPERATOR_UID=str(OPERATOR),
               KONTROLL_OPERATOR_GID=str(gid), GIT_CONFIG_GLOBAL=gitconfig)
    p = subprocess.run(argv, cwd=node.base, env=env, capture_output=True, text=True, timeout=900)
    recap = re.search(r"localhost\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", p.stdout)
    return p.returncode, recap, p.stdout + p.stderr


def _play(node, check=False, gid=OPERATOR, playbook=PLAYBOOK):
    """`_run`, for a run that must succeed: returns the recap's counters and the output; a failed run fails the
    row with its output."""
    rc, recap, out = _run(node, check=check, gid=gid, playbook=playbook)
    assert rc == 0 and recap, "ansible-playbook rc=%d\n%s" % (rc, out)
    print("%s%s: %s" % (os.path.basename(playbook), " --check --diff" if check else "", recap.group(0)))   # under -s
    return {"changed": int(recap.group(2)), "failed": int(recap.group(4)), "out": out}


def _gitconfig(node):
    """The global git config the play and the row's own gits run with (`[safe] directory = *`)."""
    home = os.path.join(node.base, "ansible-home")
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "gitconfig")


def _operator_git(wt, *args, check=True):
    """Git in the checkout AS THE OPERATOR — the rows' own commits, as an operator makes them (root's would leave
    root's objects in the operator's repository, which the play refuses to write over). No config of anyone's."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", **_WHO}
    p = subprocess.run(["setpriv", "--reuid=%d" % OPERATOR, "--regid=%d" % OPERATOR, "--clear-groups", "--",
                        "git", "-C", wt] + list(args), capture_output=True, text=True, env=env, cwd="/")
    if check and p.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr))
    return p


def _commit(wt, rel, text, subject):
    """Write `rel` in the checkout and commit it, as the operator; return HEAD."""
    path = os.path.join(wt, rel)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.lchown(path, OPERATOR, OPERATOR)
    _operator_git(wt, "add", "-A")
    _operator_git(wt, "commit", "-q", "-m", subject)
    return _head(wt)


def _head(wt):
    return _operator_git(wt, "rev-parse", "HEAD").stdout.strip()


def _not_the_operators(wt):
    """Every path under the checkout's git directory that is not the operator's."""
    return [os.path.join(where, n) for where, dirs, names in os.walk(os.path.join(wt, ".git"))
            for n in dirs + names if os.lstat(os.path.join(where, n)).st_uid != OPERATOR]


def _flat(out):
    """The play's output with every run of whitespace one space: a folded message wraps where the callback likes."""
    return " ".join(out.split())


def _came_in(flat):
    """The stop's `What came in …` sentence in the flattened output — from the first place it is printed to the
    `Nothing was landed` after it (the mark's own sentence between them)."""
    at = flat.find("What came in")
    assert at >= 0, flat
    return flat[at:flat.find(" Nothing was landed", at)]


def _mark(node):
    """Where a sync leaves its mark: the checkout's own git directory (`gitio.SYNC_MARK`)."""
    return os.path.join(node.wt, ".git", gitio.SYNC_MARK)


def _sync_by_hand(node):
    """`kontroll-land.py --sync` run BY HAND by the operator in their checkout, as `scripts/update.sh` runs it: the
    checkout's own copy, as the operator, with nothing of root's environment or config."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", **_WHO}
    return subprocess.run(["setpriv", "--reuid=%d" % OPERATOR, "--regid=%d" % OPERATOR, "--clear-groups", "--",
                           sys.executable, "-B", os.path.join(node.wt, "scripts", "kontroll-land.py"), "--sync",
                           "--worktree", node.wt, "--repo", node.bare], capture_output=True, text=True, env=env,
                          cwd=node.wt)


def _with_marker(node, where, script):
    """`scripts/<script>` of the checkout with a first line that appends `uid=<euid>` to `<where>/<script>` when
    it is run — what a promote of that script could do as whoever runs it."""
    with open(os.path.join(node.wt, "scripts", script), encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith("import "))
    lines.insert(at, "import os as _o; open(%r, 'a').write('uid=%%d\\n' %% _o.geteuid())" % os.path.join(where, script))
    return "\n".join(lines)


def _markers(node):
    """A directory every uid may write a marker in (the operator's sync check runs a promoted script too)."""
    where = os.path.join(node.base, "markers")
    os.mkdir(where)
    os.chmod(where, 0o1777)
    return where


def _task_status(out, name_start):
    """The status word the default callback printed for the task whose name starts with `name_start` (`ok`,
    `changed`, `skipping`, `fatal`): the first status line after its header (ansible-core 2.19 prints an `[ERROR]`
    block between the header and `fatal:`), or None when the task was not printed."""
    m = re.search(r"TASK \[" + re.escape(name_start) + r"[^\]]*\]", out)
    word = re.compile(r"^(ok|changed|skipping|fatal): \[", re.M).search(out, m.end()) if m else None
    return word.group(1) if word else None


def _sync_result(out):
    """The result of the play's sync — `kontroll-land.py --sync --stop-as local-canonical` — in a run it ended: the
    JSON the default callback printed after `fatal: … FAILED! =>`, decoded (`rc`, `stdout`, `stderr`). Fails the
    row unless the sync's task is the last the run printed and it failed: a stop or a refusal in a later task
    forks an Ansible worker after the tree changed (read A-f3, B1)."""
    tasks = re.findall(r"^TASK \[([^\]]*)\]", out, re.M)
    assert tasks and tasks[-1].startswith("Bring the worktree forward"), (tasks[-3:], out)
    fatal = re.compile(r"^fatal: \[localhost\]: FAILED! => ", re.M).search(out, out.rfind("TASK [Bring the worktree"))
    assert fatal, out
    return json.JSONDecoder().raw_decode(out, fatal.end())[0]


def _promote(node, files, subject="a promote", fillers=0, executable=()):
    """What a promote leaves: a commit on the canonical's `main` the worktree has never seen, landed from root's
    own clone — under the node's base (every uid may enter it) and not hard-linked to the canonical's objects
    (`--no-local`): a clone sharing the canonical's files would be the service's to write, and the gate would read
    it as the service uid (rule 5) — so that its objects are uid 1001's, as the service's write leaves them.
    `fillers` commits (each a change to `filler.txt`) come before it, and the paths in `executable` are made
    executable in it. Returns the commit."""
    svc = os.path.join(node.base, "svc-%d" % len([n for n in os.listdir(node.base) if n.startswith("svc-")]))
    env = dict(os.environ, GIT_CONFIG_GLOBAL=_gitconfig(node), **_WHO)
    subprocess.run(["git", "clone", "--no-local", "-q", node.bare, svc], check=True, capture_output=True, env=env)
    for i in range(fillers):
        with open(os.path.join(svc, "filler.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("%d\n" % i)
        subprocess.run(["git", "-C", svc, "add", "-A"], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", svc, "commit", "-q", "-m", "filler %d" % i], check=True, capture_output=True,
                       env=env)
    for rel, text in files.items():
        path = os.path.join(svc, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    for rel in executable:
        os.chmod(os.path.join(svc, *rel.split("/")), 0o755)
    subprocess.run(["git", "-C", svc, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", svc, "commit", "-q", "-m", subject], check=True, capture_output=True, env=env)
    p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "scripts", "kontroll-land.py"), "--worktree", svc,
                        "--repo", node.bare], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and "landed refs/heads/main" in p.stdout, p.stdout + p.stderr
    return canon.read_ref(node.bare, canon.MAIN)[0]


def _write_state(node, rel="state.txt", text="instance state\n"):
    """An uncommitted file in the checkout, the operator's, as instance state the deploy commits."""
    path = os.path.join(node.wt, rel)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.lchown(path, OPERATOR, OPERATOR)


def _sync_as_root(node):
    """`kontroll-land.py --sync` run by root itself (no `setpriv`) on the operator's checkout."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=_gitconfig(node), **_WHO)
    return subprocess.run([sys.executable, "-B", os.path.join(node.wt, "scripts", "kontroll-land.py"), "--sync",
                           "--worktree", node.wt, "--repo", node.bare], capture_output=True, text=True, env=env)


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
    assert _not_the_operators(node.wt) == [], "the play left root's files in the operator's repository"
    _commit(node.wt, "by-hand.txt", "pushed by hand\n", "by hand")
    pushed = _operator_git(node.wt, "push", "local", "HEAD:refs/heads/main", check=False)
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


def _ran(out, task):
    """Whether the task whose name begins with `task` ran in this play's output: its first status line after its
    TASK banner is ok or changed, not skipping (the default callback, as `_play` runs it)."""
    at = out.find("TASK [" + task)
    assert at >= 0, ("no such task in the run", task)
    status = re.search(r"^(ok|changed|skipping|fatal|failed):", out[at:], re.M)
    return bool(status) and status.group(1) in ("ok", "changed")


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
    --diff` does not fail: the audit runs and reads it (its NO REPORT sentence is not shown), the judgement of the
    landing runs, nothing is refused and nothing is said to be missing. The apply after it reports 0 changed, the
    link is left a link, and `main` stays the checkout's commit.

    Guards the fix going too far: before the play's look at the path followed a link, a link to a canonical was
    audited and judged already (the audit and the landing open the path, through the link), and following it must
    leave that so (I-40); the audit and the judgement are each held to having run, which the row's absent sentences
    alone did not show (the I-40 read B's N2)."""
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
    assert _ran(dry["out"], "Audit the canonical") and _ran(dry["out"], "Judge the working tree's commit"), dry["out"]
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
    that is no directory: the audit ran and gave NO REPORT (the path is not a directory), and the dry run then
    stopped at the ownership pass's look, which cannot open a path that is no directory, while the real run made the
    directory and landed (I-40; measured by both of its reads on main, on both cores). The real run's seeding through
    such a link is as it was; whether the play should refuse a link to nothing is not decided (I-49)."""
    target = _elsewhere(node)
    os.symlink(target, node.bare)
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the mandated dry-run must not fail:\n" + dry["out"]
    assert "holds no canonical yet (it is not there)" in dry["out"] and "first seed" in dry["out"], dry["out"]
    assert "REFUSING to land" not in dry["out"], dry["out"]
    assert "local-canonical: the audit gave NO REPORT" not in dry["out"], dry["out"]
    assert not _ran(dry["out"], "Audit the canonical"), \
        ("a path that is not there is not audited", dry["out"])
    assert os.path.islink(node.bare) and not os.path.exists(target), "a dry-run made the canonical"
    first = _play(node)
    assert first["changed"] > 0 and first["failed"] == 0, first["out"]
    assert os.path.islink(node.bare) and os.readlink(node.bare) == target, "the link is left a link"
    assert canon.read_ref(target, canon.MAIN) == (_head(node.wt), "commit", ""), first["out"]
    second = _play(node)
    assert second["changed"] == 0 and second["failed"] == 0, second["out"]


@pytest.mark.parametrize("through", [False, True], ids=["at-the-path", "through-a-link"])
def test_a_canonical_path_that_holds_entries_and_no_repository_is_audited_and_its_dry_run_passes(node, through):
    """The node's canonical path is a directory that holds an entry and no repository: the root of a filesystem
    mounted at the path, which holds `lost+found/` (root's, `0700`) and nothing else; and the same directory
    elsewhere, a symbolic link to it at the path, the link left a link by every run. Its `--check --diff` does not
    fail: the audit runs and names the entry, the ownership pass names it too (the real run makes it the operator's,
    group-writable), the dry run says the real run will seed the canonical, nothing is judged and nothing is
    written. The first apply makes the canonical there and lands HEAD as a first seed, and the second reports 0
    changed.

    Guards the dry run of such a node. Counted as a canonical, it was also judged as a repository, which it is not,
    so the dry run stopped at the refusal ("the canonical could not be read") while the real run landed: I-19's own
    symptom on a path that is not empty (I-19's read B, measured on both cores). And its preview of the ownership
    pass, which the first fold skipped with the judgement (I-19's read A). Through a link the play judged it too,
    before it followed one, and refused the same way (the I-40 read B's N4)."""
    root = _elsewhere(node) if through else node.bare
    os.mkdir(root)
    os.chmod(root, 0o755)
    if through:
        os.symlink(root, node.bare)
    lost = os.path.join(root, "lost+found")
    os.mkdir(lost)
    os.chmod(lost, 0o700)
    dry = _play(node, check=True)
    assert dry["failed"] == 0, "the mandated dry-run must not fail:\n" + dry["out"]
    assert "top-entry: lost+found" in dry["out"], ("the audit names what is there", dry["out"])
    assert re.search(r"\b(owner|mode): lost\+found\b", dry["out"]), ("the ownership pass names it", dry["out"])
    assert "holds no canonical yet (it holds entries but no repository" in dry["out"], dry["out"]
    assert "first seed" in dry["out"] and "REFUSING to land" not in dry["out"], dry["out"]
    assert os.listdir(root) == ["lost+found"] and os.lstat(root).st_uid == 0, "a dry-run wrote there"
    first = _play(node)
    assert first["changed"] > 0 and first["failed"] == 0, first["out"]
    assert canon.read_ref(root, canon.MAIN) == (_head(node.wt), "commit", ""), first["out"]
    assert os.path.islink(node.bare) == through, "the link is left a link"
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


@pytest.mark.parametrize("through", [False, True], ids=["at-the-path", "through-a-link"])
def test_a_canonical_whose_head_is_gone_is_said_so_by_its_dry_run_and_the_real_run_restores_it(node, through):
    """A landed canonical whose `HEAD` was removed, at the path, or moved elsewhere with a symbolic link to it left at
    the path (the link left a link by every run). Its `--check --diff` does not fail: the audit names the missing
    `HEAD` (`head: HEAD`), and the note says the path holds a repository without HEAD and that the real run restores
    HEAD and then judges the landing, not that it holds no canonical or that HEAD lands as a first seed; nothing is
    written. The apply restores `HEAD` (`ref: refs/heads/main`), `main` stays the checkout's commit, and the next
    apply reports 0 changed.

    Guards the words of that dry run: they said "holds no canonical yet … lands HEAD as a first seed" of a repository
    that may hold a promoted commit the worktree lacks, and its real run then refuses the landing (I-19's read A,
    measured on both cores). Through a link, before the play followed one, the dry run refused ("the canonical could
    not be read") while the real run restored HEAD and landed (the I-40 read B's N4)."""
    first = _play(node)
    assert first["failed"] == 0, first["out"]
    if through:
        target = _elsewhere(node)
        os.rename(node.bare, target)
        os.symlink(target, node.bare)
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
    assert os.path.islink(node.bare) == through, "the link is left a link"
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]


def test_the_play_prints_one_run_id_alone_and_under_another_stamp(node):
    """The real play, run alone (as the recovery paths run it), prints one `kontroll_run_id=` line, and so does a
    run of a playbook shaped as deploy-stack.yml: the stamp, then an import of this play, whose own import of the
    stamp says nothing. Both are a fresh node's `--check --diff`, which makes nothing. Guards I-41 at the play
    itself: local-canonical.yml started a run with no id, and a plain import of the stamp would print a second line
    under deploy-stack.yml (`integration/test_run_id_stamp_runs.py` holds the stamp's rows on small playbooks).
    """
    ids = re.compile(r'"msg": "kontroll_run_id=([^"]+)"')
    alone = _play(node, check=True)
    assert len(ids.findall(alone["out"])) == 1, alone["out"]
    outer = os.path.join(node.base, "outer.yml")
    with open(outer, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("- name: Stamp the run correlation id\n  ansible.builtin.import_playbook: %s\n"
                 "- name: The canonical\n  ansible.builtin.import_playbook: %s\n"
                 % (os.path.join(ROOT, "ansible", "playbooks", "_log-run-id.yml"), PLAYBOOK))
    under = _play(node, check=True, playbook=outer)
    assert len(ids.findall(under["out"])) == 1, under["out"]
    assert not os.path.exists(node.bare), "a dry-run made the canonical"


def test_a_new_commit_is_landed_by_the_next_deploy_and_the_one_after_reports_0_changed(node):
    """After the first apply, a new commit in the checkout: the next apply lands it (changed) — the pack it adds
    is the operator's and group-writable when the run ends — and the apply after that reports 0 changed again.
    Guards the rule on the second landing of a node's life, where the first pair of the pass has nothing to do
    and only the pair after the landing can leave the canonical as the next run expects it. The Report prints the
    landing's own lines (`kontroll_run_id=… land …`, `landed refs/heads/main`), which only a run can show."""
    _play(node)
    _commit(node.wt, "note.txt", "a change\n", "a change")
    landed = _play(node)
    assert landed["changed"] > 0, landed["out"]
    flat = _flat(landed["out"])
    assert "kontroll_run_id=" in flat and " land " in flat and "landed refs/heads/main" in flat, landed["out"]
    assert canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt), landed["out"]
    for name, (uid, gid, mode) in _left_by_the_landing(node.bare).items():
        assert (uid, gid) == (OPERATOR, canon.SERVICE_GID) and mode & 0o020, (name, uid, gid, oct(mode))
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]


def test_a_promote_is_brought_forward_as_the_operator_the_run_stops_and_the_next_one_lands(node):
    """After the first apply, a promote moves the canonical's `main` (uid 1001's objects, as the service's write leaves
    them) and the operator commits a change of their own. The `--check --diff` does not fail and says the real run would
    bring the worktree forward. The apply after it brings the worktree forward AS THE OPERATOR and STOPS IN THE SYNC'S
    OWN TASK, the run's last, which fails with exit 4; its output — kontroll-land's, `--stop-as local-canonical`,
    nothing on standard error — is the sync's `kontroll_run_id=… sync …` line, `brought forward` with `(HEAD was <the
    operator's commit>)`, `local-canonical: STOPPED after bringing the worktree forward to the canonical's main. What
    came in since <that HEAD>:`, what came in (the promote's subject, its file — not the operator's own commit, replayed
    on top), and `The mark is removed, so the next run goes on. Nothing was landed`; the mark is gone; `main` is still
    the promote; the dry-run's question is skipped and its note is not printed; HEAD descends from the promote and holds
    the operator's change; and nothing under the worktree's git directory is not the operator's. The next `--check
    --diff` does not fail and has nothing to bring forward, the apply after it lands (`main` is HEAD; the sync's task
    "ok"), and the one after that reports 0 changed. Guards the deploy after every promote, end to end as the installer
    runs it: before the sync it was refused (not a fast-forward) until the operator fetched and rebased by hand; a
    root-run state commit or replay would have left root's files in the operator's repository; a deploy that went on, in
    the same run, to land and run the tree the sync brought in ran a tree its own dry-run had not seen; and a stop in a
    later task forked an Ansible worker after the tree changed, which can import the tree's filter plugins (read A-f3,
    B1)."""
    _play(node)
    promoted = _promote(node, {"promoted.txt": "from a promote\n"}, "promote-t25-subject")
    mine = _commit(node.wt, "mine.txt", "the operator's\n", "the operator's own change")
    dry = _play(node, check=True)
    assert dry["failed"] == 0 and "would bring forward" in dry["out"], dry["out"]
    assert canon.read_ref(node.bare, canon.MAIN)[0] == promoted and _head(node.wt) == mine
    rc, recap, out = _run(node)
    result = _sync_result(out)
    lines = result["stdout"].split("\n")
    assert rc != 0 and result["rc"] == 4 and result["stderr"] == "", out
    assert lines[0].startswith("kontroll_run_id=") and " sync %s <- " % node.wt in lines[0], lines
    assert lines[1].startswith("kontroll-land: brought forward") and "(HEAD was %s)" % mine in lines[1], lines
    assert lines[2] == ("local-canonical: STOPPED after bringing the worktree forward to the canonical's main. What "
                        "came in since %s:" % mine), lines
    came = "\n".join(lines[3:-1])
    assert "t: promote-t25-subject" in came and "promoted.txt" in came, lines
    assert "mine.txt" not in came and "own change" not in came, lines
    assert lines[-1].startswith("The mark is removed, so the next run goes on. Nothing was landed"), lines
    assert not os.path.lexists(_mark(node))
    assert canon.read_ref(node.bare, canon.MAIN)[0] == promoted, "the run that brought the worktree forward landed"
    head = _head(node.wt)
    assert head != mine and _operator_git(node.wt, "merge-base", "--is-ancestor", promoted, head,
                                          check=False).returncode == 0
    assert _operator_git(node.wt, "log", "-1", "--format=%s", head).stdout.strip() == "the operator's own change"
    assert _not_the_operators(node.wt) == [], "the sync left another uid's files in the operator's repository"
    assert _task_status(out, "Say whether the canonical's main has moved") == "skipping", out
    assert "none of that has happened" not in _flat(out), out
    again = _play(node, check=True)
    assert again["failed"] == 0 and "would bring forward" not in again["out"], again["out"]
    landed = _play(node)
    assert landed["changed"] > 0 and canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt) == head
    assert _task_status(landed["out"], "Bring the worktree forward") == "ok", landed["out"]
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]


def test_a_promoted_script_does_not_run_in_the_run_that_brought_it_in(node):
    """A promote changes `scripts/kontroll-canonical.py` (the audit's script) and `scripts/kontroll-land.py` (the
    sync's own, and the landing's, whose `--check` is the first command after the sync) so that each writes a
    marker naming the uid that ran it. The apply that brings the worktree forward stops in the sync's task, and
    neither marker is there: nothing of the tree that came in ran in that run — the sync that stops it is the copy
    it started with. The next dry-run runs both — as root — which is what the stop's message says before it (`runs
    that tree's own playbooks and scripts as root`). Guards the stop's reason and its place: before it, the same
    apply went on to run the promoted tree's scripts as root (measured by the reader of this change), after a
    dry-run of the tree before it; and a stop moved after the landing's judgement would let the landing's script
    run (the audit's marker, reached only after the landing, does not show that)."""
    _play(node)
    where = _markers(node)
    _promote(node, {"scripts/" + n: _with_marker(node, where, n)
                    for n in ("kontroll-canonical.py", "kontroll-land.py")}, "a promote of two scripts")
    rc, recap, out = _run(node)
    said = _sync_result(out)["stdout"]
    assert rc != 0 and "STOPPED after bringing the worktree forward" in said, out
    assert "runs that tree's own playbooks and scripts as root" in said, said
    assert os.listdir(where) == [], "a promoted script ran in the run that brought it in"
    _run(node, check=True)
    for name in ("kontroll-canonical.py", "kontroll-land.py"):
        with open(os.path.join(where, name), encoding="utf-8") as fh:
            assert "uid=0" in fh.read(), name


def test_a_promoted_filter_plugin_is_not_imported_in_the_run_that_brought_it_in(node):
    """The checkout holds this tree's `ansible/filter_plugins/`, and the play runs with a config naming that
    directory (`filter_plugins`, as the tree's own `ansible/ansible.cfg` names it for a run from `ansible/`), first
    in a playbook that runs local-canonical.yml on its own (as the installer's passthrough does), then a play that
    templates. A promote adds a filter plugin whose import writes a marker naming the uid. The apply that brings the
    worktree forward fails in the sync's own task (exit 4, the run's last), and no marker is written: no Ansible
    process imported the plugin. The next dry-run imports it as root (`uid=0`) — the control that the config
    reaches the plugin. Guards the stop's place (read A-f3, B1, measured on both cores): stopped in a later task, as
    it was, the run forked a worker after the tree changed, and a worker can import the filter plugins on disk when
    it first templates — the promoted tree's code, as root, in the run whose dry-run had previewed the tree before
    it. Not deploy-stack.yml's own plays: there a template the controller renders before the sync (its `meta:
    end_play`'s `when`) loads the filter plugins first, and the old shape left no marker in read B-f4's probes, so a
    wrapper built from deploy-stack.yml would leave this check blind (read B-f4, S2)."""
    plugins = os.path.join(node.wt, "ansible", "filter_plugins")
    shutil.copytree(os.path.join(ROOT, "ansible", "filter_plugins"), plugins,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for where, dirs, names in os.walk(os.path.join(node.wt, "ansible")):
        for name in [where] + [os.path.join(where, n) for n in dirs + names]:
            os.lchown(name, OPERATOR, OPERATOR)
    _operator_git(node.wt, "add", "-A")
    _operator_git(node.wt, "commit", "-q", "-m", "the tree's filter plugins")
    cfg = os.path.join(node.base, "tree.cfg")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write("[defaults]\nfilter_plugins = %s\n" % plugins)
    wrapper = os.path.join(node.base, "local-canonical-alone.yml")
    with open(wrapper, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump([{"name": "Ensure the local canonical git", "ansible.builtin.import_playbook": PLAYBOOK},
                        {"name": "The play after the canonical's", "hosts": "localhost", "connection": "local",
                         "gather_facts": False,
                         "tasks": [{"name": "Template", "ansible.builtin.debug": {"msg": "{{ worktree }}"}}]}],
                       fh, sort_keys=False)
    rc, recap, out = _run(node, playbook=wrapper, cfg=cfg)
    assert rc == 0, out
    where = _markers(node)
    plugin = ("import os\n\nwith open(%r, 'a') as fh:\n    fh.write('uid=%%d;' %% os.geteuid())\n\n\n"
              "class FilterModule:\n    def filters(self):\n        return {}\n" % os.path.join(where, "zz_promoted"))
    _promote(node, {"ansible/filter_plugins/zz_promoted.py": plugin}, "a promote of a filter plugin")
    rc, recap, out = _run(node, playbook=wrapper, cfg=cfg)
    result = _sync_result(out)
    assert rc != 0 and result["rc"] == 4 and "STOPPED after bringing the worktree forward" in result["stdout"], out
    assert os.listdir(where) == [], "a promoted filter plugin was imported in the run that brought it in"
    _run(node, check=True, playbook=wrapper, cfg=cfg)
    with open(os.path.join(where, "zz_promoted"), encoding="utf-8") as fh:
        assert "uid=0;" in fh.read()


def test_root_running_the_sync_on_the_operators_checkout_is_refused_and_writes_nothing(node):
    """After the first apply, a promote, and a tracked file's mtime moved (so that any `git status` would refresh
    the index and write it): `kontroll-land.py --sync` run by root itself on the operator's checkout exits 1 with
    `runs as uid 0`; HEAD is as it was, the checkout does not hold the promote, and nothing under its git directory
    is not the operator's. Guards the owner check with a real root, not a patched uid: it comes before any git of
    the sync's that can write there (root's index or objects there are what the operator's git cannot write over)."""
    _play(node)
    promoted = _promote(node, {"promoted.txt": "p\n"})
    head = _head(node.wt)
    os.utime(os.path.join(node.wt, "modules", "_core.yml"), (1, 1))
    p = _sync_as_root(node)
    assert p.returncode == 1 and "runs as uid 0" in p.stderr, p.stdout + p.stderr
    assert _head(node.wt) == head and _not_the_operators(node.wt) == []
    assert _operator_git(node.wt, "cat-file", "-e", promoted, check=False).returncode != 0


def test_roots_files_in_the_operators_git_stop_the_deploy_and_the_named_remedy_works(node):
    """After the first apply, root commits in the operator's checkout (as the state commit of an earlier deploy did,
    inside the installer): the dry-run and the real run each fail with `REFUSING to commit or land`, naming the
    checkout's git directory and the one-time `sudo chown -R <uid>:<gid> <that directory>`; after that remedy, run
    by hand, the next apply lands HEAD. Guards the upgrade path from a box an older deploy wrote as root: the play
    names what it cannot write over, before it writes anything, and the remedy it names is enough — on the git
    directory itself, not a file under it."""
    _play(node)
    env = dict(os.environ, GIT_CONFIG_GLOBAL=_gitconfig(node), **_WHO)
    with open(os.path.join(node.wt, "rootstate.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("state\n")
    subprocess.run(["git", "-C", node.wt, "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", node.wt, "commit", "-q", "-m", "root's state commit"], check=True,
                   capture_output=True, env=env)
    assert _not_the_operators(node.wt)
    for check in (True, False):
        rc, recap, out = _run(node, check=check)
        flat = _flat(out)
        assert rc != 0 and "REFUSING to commit or land" in flat, (check, out)
        assert "sudo chown -R %d:%d %s/.git`" % (OPERATOR, OPERATOR, node.wt) in flat, out   # the directory
    subprocess.run(["chown", "-R", "%d:%d" % (OPERATOR, OPERATOR), os.path.join(node.wt, ".git")], check=True)
    os.lchown(os.path.join(node.wt, "rootstate.txt"), OPERATOR, OPERATOR)
    landed = _play(node)
    assert landed["failed"] == 0 and canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt), landed["out"]


def test_the_state_commit_is_the_operators_and_is_replayed_onto_a_promote(node):
    """After the first apply, uncommitted instance state and a promote: the apply commits the state AS THE
    OPERATOR, replays it onto the promote (committed as `kontroll`) and stops; nothing under the git directory is
    not the operator's and nothing is left uncommitted; the next apply lands it, and the one after reports 0
    changed. Guards the order of the state commit and the sync: the state is committed before the sync (a sync
    refuses uncommitted changes) and carried across the promote, not dropped."""
    _play(node)
    promoted = _promote(node, {"promoted.txt": "p\n"})
    _write_state(node)
    rc, recap, out = _run(node)
    assert rc != 0 and _sync_result(out)["rc"] == 4, out
    head = _head(node.wt)
    who = _operator_git(node.wt, "log", "-1", "--format=%s|%cn", head).stdout.strip()
    assert who.startswith("kontroll: instance state") and who.endswith("|kontroll"), who
    assert _operator_git(node.wt, "merge-base", "--is-ancestor", promoted, head, check=False).returncode == 0
    assert _not_the_operators(node.wt) == [] and _operator_git(node.wt, "status", "--porcelain").stdout == ""
    landed = _play(node)
    assert canon.read_ref(node.bare, canon.MAIN)[0] == head == _head(node.wt), landed["out"]
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]


def test_a_sync_that_is_refused_stops_the_play_before_the_landing(node):
    """After the first apply, a promote and a commit of the operator's own that change the same file differently: the
    sync's task — the run's last — fails with exit 1, nothing on standard error, its standard output the sync's
    `kontroll_run_id=…` line and then one line: `local-canonical: REFUSING to land — the sync of the worktree to the
    canonical's main exited 1: HEAD's commits do not replay …`, naming the file and ending `then run again.`; `main` is
    still the promote, HEAD is the operator's commit, no mark is left, and nothing under the git directory is not the
    operator's. Guards the refusal's place — the play stops at the sync, before the landing and with the replay undone —
    and that a real run's refusal is the sync's own (`--stop-as`), said as the play's, not a later task's: no Ansible
    worker starts after the sync (read A-f3, B1)."""
    _play(node)
    promoted = _promote(node, {"note.txt": "promoted\n"})
    mine = _commit(node.wt, "note.txt", "mine\n", "mine")
    rc, recap, out = _run(node)
    result = _sync_result(out)
    lines = result["stdout"].split("\n")
    assert rc != 0 and result["rc"] == 1 and result["stderr"] == "" and len(lines) == 2, out
    assert lines[0].startswith("kontroll_run_id=") and lines[1].startswith(
        "local-canonical: REFUSING to land — the sync of the worktree to the canonical's main exited 1: HEAD's "
        "commits do not replay"), lines
    assert "note.txt" in lines[1] and lines[1].endswith("then run again."), lines
    assert canon.read_ref(node.bare, canon.MAIN)[0] == promoted and _head(node.wt) == mine
    assert _not_the_operators(node.wt) == [] and not os.path.lexists(_mark(node))


@pytest.mark.parametrize("busy", ["MERGE_HEAD", "rebase-merge"])
def test_an_operation_in_progress_stops_the_dry_run_and_the_run_before_the_state_commit(node, busy):
    """After the first apply, an operation in progress in the checkout — a merge (`MERGE_HEAD`, a file) or a rebase
    (`rebase-merge/`, a directory), the operator's — and uncommitted instance state: the dry-run and the real run
    each fail with `operation in progress`, naming it, and the state is not committed — HEAD is as it was and the
    state file is still untracked. Guards the check before the state commit: a state commit onto a merge or a
    rebase in progress (a replay a killed deploy left among them) would commit into the operator's half-done
    operation, and an abort would take the state away again; and the check's test for a DIRECTORY, which a file's
    alone does not hold."""
    _play(node)
    head = _head(node.wt)
    path = os.path.join(node.wt, ".git", busy)
    if busy == "MERGE_HEAD":
        with open(path, "w", encoding="ascii", newline="\n") as fh:
            fh.write(head + "\n")
    else:
        os.mkdir(path)
    os.lchown(path, OPERATOR, OPERATOR)
    _write_state(node)
    for check in (True, False):
        rc, recap, out = _run(node, check=check)
        flat = _flat(out)
        assert rc != 0 and "operation in progress" in flat and busy in flat, (check, out)
    assert _head(node.wt) == head
    assert _operator_git(node.wt, "status", "--porcelain").stdout.strip() == "?? state.txt"


def test_a_head_on_no_branch_stops_the_dry_run_and_the_run_before_the_state_commit(node):
    """After the first apply, the checkout's HEAD detached (as `git rebase --quit` leaves it, nothing in progress) and
    uncommitted instance state: the dry-run and the real run each fail saying HEAD is on no branch, and the state is
    not committed — HEAD is as it was, still on no branch, and the state file still untracked. Guards the deploy
    against a state commit and a landing on a detached HEAD: a replay quit part-way left HEAD on main with half of the
    operator's commit in the tree, and the next run committed that as instance state and landed it, the rest of the
    operator's commits deployed by no run (read A-f9, N3)."""
    _play(node)
    head = _head(node.wt)
    _operator_git(node.wt, "checkout", "-q", "--detach")
    _write_state(node)
    for check in (True, False):
        rc, recap, out = _run(node, check=check)
        flat = _flat(out)
        assert rc != 0 and "HEAD is on no branch" in flat, (check, out)
    assert _head(node.wt) == head
    assert _operator_git(node.wt, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    assert _operator_git(node.wt, "status", "--porcelain").stdout.strip() == "?? state.txt"


def test_a_dry_run_whose_sync_check_refuses_says_so(node):
    """The canonical's `main` is a symbolic ref (to a branch holding a promote): the dry-run's `--sync --check` exits 1,
    and the dry-run fails with `REFUSING to land — the sync of the worktree to the canonical's main (--check) exited 1:`
    and the sync's lines, each ended by one full stop (the run-id line's `….git.`), the sentence naming the `symbolic
    ref`, with no backslash in it. Guards the refusal's `when` for a refused CHECK — the one refusal the play still says
    itself (a real run's sync says its own): no other row has the dry-run's check refuse (the busy rows are refused by
    the play's own check first) — and its sentence: a backreference in this message printed `\\1.` in place of each
    line's last letter (measured in the gate image)."""
    _play(node)
    promoted = _promote(node, {"promoted.txt": "p\n"})
    env = dict(os.environ, GIT_CONFIG_GLOBAL=_gitconfig(node), **_WHO)
    subprocess.run(["git", "-C", node.bare, "update-ref", "refs/heads/other", promoted], check=True, env=env)
    subprocess.run(["git", "-C", node.bare, "symbolic-ref", "refs/heads/main", "refs/heads/other"], check=True,
                   env=env)
    rc, recap, out = _run(node, check=True)
    flat = _flat(out)
    assert rc != 0 and ("REFUSING to land — the sync of the worktree to the canonical's main (--check) exited 1:"
                        in flat), out
    assert "symbolic ref" in flat and "%s. kontroll-land: " % node.bare in flat and "\\1" not in flat, out


def test_a_sync_by_hand_stops_the_next_dry_run_and_run_before_they_land_deploy_or_run_its_scripts(node):
    """After the first apply, a promote changes the audit's script and the landing's (`kontroll-canonical.py`,
    `kontroll-land.py`) so that each writes a marker naming the uid that ran it, and the operator brings the worktree
    forward BY HAND (`kontroll-land.py --sync`, as the operator, as `update.sh` does): the sync says what came in and
    leaves the mark, and no marker is written. The next `--check --diff` and the next apply each fail with
    `local-canonical: STOPPED before landing, deploying or running the worktree's scripts`, naming what came in since
    HEAD before the sync; the dry-run leaves the mark (`Under --check the mark … is left`), the apply removes it (`The
    mark is removed`); neither writes a marker — nothing of the worktree ran — and `main` is still the promote. The
    dry-run after that runs the promoted audit script as root (its marker says `uid=0`), the apply after it lands, and
    the one after that reports 0 changed. Guards the stop at the top of the play: before it, the run after a hand sync
    found its own sync "in step", never stopped, and previewed, landed and ran the tree that came in, as root, without a
    word of what it was. (Here the play is this tree's own; after a real hand sync the next run's playbooks are the tree
    that came in, and whether it stops is that tree's to say — SECURITY.md C10.)"""
    _play(node)
    where = _markers(node)
    before = _head(node.wt)
    promoted = _promote(node, {"scripts/" + n: _with_marker(node, where, n)
                               for n in ("kontroll-canonical.py", "kontroll-land.py")}, "a promote of two scripts")
    p = _sync_by_hand(node)
    assert p.returncode == 0 and "kontroll-land: brought forward" in p.stdout, p.stdout + p.stderr
    assert "what came in since %s:" % before in p.stdout and "scripts/kontroll-land.py" in p.stdout, p.stdout
    assert os.path.isfile(_mark(node)) and os.listdir(where) == [], os.listdir(where)
    for check in (True, False):
        rc, recap, out = _run(node, check=check)
        flat = _flat(out)
        assert rc != 0 and "local-canonical: " + STOPPED in flat, (check, out)
        said = _came_in(flat)
        assert said.startswith("What came in since %s: " % before) and "a promote of two scripts" in said, said
        assert ("Under --check the mark" in said) == check and ("The mark is removed" in said) == (not check), said
        assert os.path.isfile(_mark(node)) == check and os.listdir(where) == [], (check, os.listdir(where))
    assert canon.read_ref(node.bare, canon.MAIN)[0] == promoted == _head(node.wt)
    _play(node, check=True)
    with open(os.path.join(where, "kontroll-canonical.py"), encoding="utf-8") as fh:
        assert "uid=0" in fh.read()
    landed = _play(node)
    assert landed["failed"] == 0 and canon.read_ref(node.bare, canon.MAIN)[0] == _head(node.wt), landed["out"]
    after = _play(node)
    assert after["changed"] == 0 and after["failed"] == 0, after["out"]


def test_bootstraps_first_play_stops_on_the_mark_and_says_what_came_in_escaped(node):
    """`_stop-after-sync.yml` — bootstrap.yml's entry after the stamp — run as the installer runs it, on the node's
    checkout: with
    no mark it succeeds and changes nothing. After a promote of 22 commits — the newest makes a tracked file executable
    and has an ESC and a C1 character in its subject — brought in by hand, its `--check` fails with `bootstrap: STOPPED
    before landing, deploying or running the worktree's scripts` and leaves the mark; the real run fails the same way
    and removes it; each names what came in since HEAD before the sync — the newest commit first, its subject with `?`
    for each of the two characters, `and 2 older commit(s) not shown`, the path and its mode change; and the run after
    that succeeds. Guards the stop's own play and its message as Ansible renders it: the escape of what a terminal acts
    on (its class written in YAML and read through Jinja), the count of the rest, the listing's reads as the operator in
    the installer's mode."""
    _play(node)
    quiet = _play(node, playbook=STOP_PLAYBOOK)
    assert quiet["changed"] == 0 and quiet["failed"] == 0, quiet["out"]
    before = _head(node.wt)
    promoted = _promote(node, {"promoted.txt": "p\n"}, "newest esc\x1b[2J c1\x9b end", fillers=21,
                        executable=["modules/_core.yml"])
    p = _sync_by_hand(node)
    assert p.returncode == 0 and os.path.isfile(_mark(node)), p.stdout + p.stderr
    short = _operator_git(node.wt, "rev-parse", "--short", promoted).stdout.strip()
    for check in (True, False):
        rc, recap, out = _run(node, check=check, playbook=STOP_PLAYBOOK)
        flat = _flat(out)
        assert rc != 0 and "bootstrap: " + STOPPED in flat, (check, out)
        said = _came_in(flat)
        assert said.startswith("What came in since %s: %s t: newest esc?[2J c1? end; " % (before, short)), said
        assert "; and 2 older commit(s) not shown; " in said, said
        assert "mode change 100644 => 100755 modules/_core.yml" in said and "promoted.txt" in said, said
        assert os.path.isfile(_mark(node)) == check, check
    again = _play(node, playbook=STOP_PLAYBOOK)
    assert again["changed"] == 0 and again["failed"] == 0, again["out"]


def _plant(node, text):
    """A mark at the sync's path holding `text`, the operator's file, as a sync leaves one."""
    with open(_mark(node), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.lchown(_mark(node), OPERATOR, OPERATOR)


def _link_at_the_mark(node, text):
    """A link at the mark's path to a file elsewhere holding `text` — no sync writes one."""
    target = os.path.join(node.base, "elsewhere")
    with open(target, "w", encoding="ascii", newline="\n") as fh:
        fh.write(text)
    os.symlink(target, _mark(node))
    os.lchown(_mark(node), OPERATOR, OPERATOR)


def test_a_link_at_the_marks_path_stops_the_run_as_a_mark_that_could_not_be_read_and_is_kept(node):
    """A link at the mark's path to a file holding HEAD: `_stop-after-sync.yml`'s play (bootstrap's, after the stamp)
    fails with `the mark a sync of the worktree leaves could not be read` and `is not a file a sync writes`, lists
    nothing (`What came in: git could not list it.`, one space before `Nothing was landed`), names HEAD before as `not
    known`, and leaves the link where it is. Guards the read's link test and its fail-closed branch: read through the
    link, a planted file
    would choose what the stop names; read as no mark, the run would go on; removed, the evidence of who planted it
    would go with it (read B-f2, S3: T02, T09, T11; N7, the double space)."""
    _play(node)
    _link_at_the_mark(node, _head(node.wt) + "\n")
    rc, recap, out = _run(node, playbook=STOP_PLAYBOOK)
    flat = _flat(out)
    assert rc != 0 and "the mark a sync of the worktree leaves could not be read" in flat, out
    assert "is not a file a sync writes" in flat and "HEAD before the sync: not known;" in flat, out
    assert "What came in: git could not list it. Nothing was landed" in out, "one space, as the message is folded"
    assert os.path.islink(_mark(node)), "the link is left where it is"


def test_a_mark_that_cannot_be_removed_says_so_and_stays(node):
    """A mark of HEAD in a git directory the operator cannot write (0555): the real run fails, says the mark `was
    NOT removed` and to remove it by hand, and the mark is still there. Guards the remove's own status: "The mark
    is removed" over a mark that is not would let the operator expect the next run to go on (read B-f2, S3: T12)."""
    _play(node)
    _plant(node, _head(node.wt) + "\n")
    gitdir = os.path.join(node.wt, ".git")
    os.chmod(gitdir, 0o555)
    try:
        rc, recap, out = _run(node, playbook=STOP_PLAYBOOK)
    finally:
        os.chmod(gitdir, 0o755)
    flat = _flat(out)
    assert rc != 0 and "was NOT removed" in flat and "The mark is removed" not in flat, out
    assert os.path.isfile(_mark(node))


def test_a_mark_whose_first_line_is_not_a_commit_id_lists_nothing_and_names_no_head(node):
    """A mark whose first line is not a commit id and carries an ESC: the run stops `before running anything of the
    worktree`, lists nothing (`What came in: git could not list it`), names HEAD before as `not known`, and prints
    no ESC. Guards the first-line rule: anything but a commit id would go into `git diff "$before" HEAD`, where a
    word starting with `--` is an option (`--output=<path>` writes a file, as the operator) (read B-f2, S3: T16,
    T18)."""
    _play(node)
    _plant(node, "not-a-commit \x1b[2J\n")
    rc, recap, out = _run(node, playbook=STOP_PLAYBOOK)
    flat = _flat(out)
    assert rc != 0 and STOPPED in flat, out
    assert "What came in: git could not list it" in flat and "HEAD before the sync: not known;" in flat, out
    assert "\x1b" not in out


def test_the_stop_run_alone_ends_with_the_callers_run_id_and_has_none_when_none_was_passed(node):
    """A mark of HEAD, `_stop-after-sync.yml` run alone (no stamp before it): the dry-run with no `kontroll_run_id`
    stops with no run id in its message; the run given `-e kontroll_run_id=…` (a caller may pass one; no API route does
    yet) stops with the message ending in it. Guards the fallback the stop names when no stamp ran: the caller's id, and
    no `kontroll_run_id=` with nothing after it (read B-f2, S3: T17; N5). Under bootstrap.yml and deploy-stack.yml the
    stamp runs first and the stop names the id it printed: the deploy-stack row below."""
    _play(node)
    _plant(node, _head(node.wt) + "\n")
    rc, recap, out = _run(node, check=True, playbook=STOP_PLAYBOOK)
    assert rc != 0 and "STOPPED" in out and "kontroll_run_id" not in out, out
    rc, recap, out = _run(node, playbook=STOP_PLAYBOOK, extra={"kontroll_run_id": "row-run-id"})
    assert rc != 0 and "the next run goes on. Nothing was landed" in _flat(out), out
    assert "; and in `git -C %s reflog`. kontroll_run_id=row-run-id" % node.wt in _flat(out), out


@pytest.mark.parametrize("passed", [None, "{{ now(fmt='%H%M%S%f') }}"], ids=["none-passed", "a-template-passed"])
def test_the_stop_right_after_deploy_stacks_stamp_stops_under_its_name_and_the_stamps_id(node, passed):
    """A mark of HEAD, and a playbook whose first two entries are deploy-stack.yml's own — read from it, the stamp's
    import and the stop play's tasks file made absolute: the run-id stamp, then the stop play, `stop_play: deploy-stack`
    — and whose third play writes a marker, run as the installer runs it, `--check --diff`, with no `kontroll_run_id`
    passed, and again passed one that is a template of the time to the microsecond: it fails with `deploy-stack: STOPPED
    before landing, deploying or running the worktree's scripts`, not `bootstrap:`; the message ends with
    `kontroll_run_id=` and the one id the stamp printed; the third play writes nothing; the mark is left (a dry-run).
    Guards the stop at deploy-stack's head, as tests/unit/test_stop_after_sync.py pins those entries:
    local-canonical.yml's own stop came after deploy-stack's root Docker play, which a host-direct deploy after a sync
    by hand ran first (read A-f2, N4); the name the stop says it is (deploy-stack's copy of the stop play sets its own);
    and the id it names — the stamp's, which a default deploy has though it passed none, so the stop is found in the log
    under the run it ended (I-41), and the stamp's before a caller's: a caller's that renders anew names an id the log
    does not hold (read B-r21, N4: the stop with the two swapped passed the plain row). The entries are read from
    deploy-stack.yml, so the row follows a change to them (read B-f3, N5); deploy-stack.yml itself does not load in this
    image: Ansible resolves every module of every play it imports before it runs one, and the image has no
    community.docker under the row's own Ansible home."""
    _play(node)
    _plant(node, _head(node.wt) + "\n")
    marker = os.path.join(node.base, "next-play-ran")
    wrapper = os.path.join(node.base, "deploy-stack-head.yml")
    with open(os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8") as fh:
        stamp, head = yaml.safe_load(fh)[:2]
    assert stamp.get("ansible.builtin.import_playbook") == "_log-run-id.yml", stamp
    assert [t.get("ansible.builtin.import_tasks") for t in head["tasks"]] == ["tasks/stop-after-sync.yml"], head
    stamp["ansible.builtin.import_playbook"] = os.path.join(ROOT, "ansible", "playbooks", "_log-run-id.yml")
    head["tasks"][0]["ansible.builtin.import_tasks"] = os.path.join(ROOT, "ansible", "playbooks", "tasks",
                                                                     "stop-after-sync.yml")
    with open(wrapper, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump([stamp, head, {"name": "The play after the stop", "hosts": "localhost", "connection": "local",
                                      "gather_facts": False,
                                      "tasks": [{"name": "Write the marker",
                                                 "ansible.builtin.command": "touch %s" % marker,
                                                 "check_mode": False}]}], fh, sort_keys=False)
    rc, recap, out = _run(node, check=True, playbook=wrapper, extra={"kontroll_run_id": passed} if passed else None)
    flat = _flat(out)
    assert rc != 0 and "deploy-stack: " + STOPPED in flat, out
    assert "bootstrap: STOPPED" not in flat and not os.path.exists(marker), out
    printed = re.findall(r'"msg": "kontroll_run_id=([^"]+)"', out)
    assert len(printed) == 1 and printed[0], out
    assert "; and in `git -C %s reflog`. kontroll_run_id=%s" % (node.wt, printed[0]) in flat, out
    assert os.path.isfile(_mark(node))


def test_a_depth_clone_past_the_canonicals_main_is_said_shallow_by_the_dry_run_and_refused_so_by_the_run(node):
    """The node landed, then a commit of the operator's on top (an upgrade, no promote), and a `--depth 1` clone of
    that checkout, the operator's, run as the node's worktree: the canonical's `main` is below where the clone stops.
    The dry-run passes, its note saying the canonical's main has commits the worktree lacks — "in a shallow clone,
    possibly history below where the clone stops" — beside the sync's line that the worktree is a shallow clone and
    main not in it; the real run fails with the shallow sentence naming `fetch --unshallow`, never "share no history",
    and leaves no mark. Guards the shallow sync end to end (read A-r21, S1): its refusal said the worktree shares no
    history with the canonical, and the dry-run said nothing of it."""
    _play(node)
    _commit(node.wt, "upgrade.txt", "u\n", "an upgrade")
    clone = os.path.join(node.base, "shallow")
    os.mkdir(clone)
    os.lchown(clone, OPERATOR, OPERATOR)
    _operator_git(clone, "clone", "-q", "--depth", "1", "file://" + node.wt, ".")
    _operator_git(clone, "config", "core.autocrlf", "false")
    shallow = type("Node", (), {"base": node.base, "wt": clone, "bare": node.bare})()
    rc, recap, out = _run(shallow, check=True)
    flat = _flat(out)
    assert rc == 0 and "in a shallow clone, possibly history below where the clone stops" in flat, out
    assert "The worktree is a shallow clone, and main is not in it" in flat, out
    rc, recap, out = _run(shallow)
    flat = _flat(out)
    assert rc != 0 and "is a shallow clone" in flat and "fetch --unshallow" in flat, out
    assert "share no history" not in flat and not os.path.exists(_mark(shallow)), out


def test_what_the_play_writes_in_the_checkout_carries_the_operators_gid(node):
    """The operator's gid is not their uid (`KONTROLL_OPERATOR_GID` 1002, the checkout `1000:1002`): after a
    promote, the apply that brings the worktree forward leaves everything under the git directory with gid 1002.
    Guards `operator_gid`: a play that took the uid for the gid (or root's) would leave files the operator's group
    does not hold."""
    gid = 1002
    for where, dirs, names in os.walk(node.wt):
        for name in [where] + [os.path.join(where, n) for n in dirs + names]:
            os.lchown(name, OPERATOR, gid)
    _play(node, gid=gid)
    _promote(node, {"promoted.txt": "p\n"})
    rc, recap, out = _run(node, gid=gid)
    assert rc != 0 and _sync_result(out)["rc"] == 4, out
    wrong = [os.path.join(w, n) for w, d, ns in os.walk(os.path.join(node.wt, ".git")) for n in d + ns
             if os.lstat(os.path.join(w, n)).st_gid != gid]
    assert wrong == [], wrong[:10]


def test_a_state_commit_that_fails_fails_the_play_at_that_task(node):
    """After the first apply, the operator's own `pre-commit` hook refuses every commit, and there is instance
    state to commit: the apply fails AT the state commit's task. Guards `set -e` in that task: without it the task
    reported "changed" with the state staged and never committed, and the landing after it said `up to date`."""
    _play(node)
    hook = os.path.join(node.wt, ".git", "hooks", "pre-commit")
    with open(hook, "w", encoding="ascii", newline="\n") as fh:
        fh.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    os.lchown(hook, OPERATOR, OPERATOR)
    _write_state(node)
    rc, recap, out = _run(node)
    assert rc != 0 and _task_status(out, "Commit uncommitted instance state") == "fatal", out


def test_the_operators_git_takes_none_of_the_plays_environment(node, monkeypatch):
    """The play's environment names another author and committer (`GIT_AUTHOR_NAME`, `GIT_COMMITTER_NAME`, as a
    job runner's may): the state commit, made as the operator through `env -i`, is authored and committed by
    `kontroll`. Guards `env -i` in `as_operator`: the operator's git must take nothing of the caller's environment
    but what the play hands it."""
    _play(node)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "leaked")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "leaked")
    _write_state(node)
    _play(node)
    who = _operator_git(node.wt, "log", "-1", "--format=%s|%an|%cn").stdout.strip()
    assert who.startswith("kontroll: instance state") and who.endswith("|kontroll|kontroll"), who
