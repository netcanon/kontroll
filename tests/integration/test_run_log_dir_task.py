"""deploy-stack.yml's task that makes the operator scripts' run-log directory, run for real by ansible-playbook.

WHY: Vector binds `<operator home>/.local/state/kontroll/runs` read-only, and Docker makes a missing bind source and
each missing parent root's. On a host deploy (deploy-stack.yml run on the control node, not in the installer, whose
launcher makes the directory first) nothing made it, so the first `up` with Vector left `~/.local` root's and every
later operator script ran without its run-log (I-23, 2026-11-08). The task now makes it before `up`, as the operator.
These rows take that task from the playbook as it is and run it, as deploy-stack.yml runs it (become), against a
scratch home. The operator's identity comes one of two ways, as the task reads it: from KONTROLL_OPERATOR_HOME/UID/GID,
which the launchers and tests/fresh-node-check.sh export (most rows), or, on a host deploy run by hand where nothing
exports them, from HOME and USER (the host-deploy row). The homes: a fresh one, one whose `~/.local` the operator had
already made with a mode of their own, one Docker already got to (root only), a dry-run, and a second run.

With KONTROLL_OPERATOR_*, the owner is uid 1000 when the suite runs as root (the gate image's case: root makes the
directories, and they must be the operator's), else the suite's own uid. The host-deploy row names the operator by
USER, so as root it takes a non-root account that has a group of its own name, else the suite's own user. Needs POSIX
and ansible-playbook; without them every row is skipped.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest
import yaml

try:
    import grp
    import pwd
except ImportError:  # not POSIX: every row is skipped
    grp = pwd = None

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")
RUNS = ".local/state/kontroll/runs"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix" or not shutil.which("ansible-playbook"),
                       reason="needs POSIX and ansible-playbook (it runs deploy-stack.yml's own task)"),
]


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _the_task():
    with open(DEPLOY, encoding="utf-8") as fh:
        found = [t for t in _tasks(yaml.safe_load(fh.read()))
                 if str(t.get("name", "")).startswith("Ensure the operator scripts' run-log dir exists")]
    assert len(found) == 1, "the run-log dir task moved — move this test with it"
    return found[0]


def _operator():
    return (1000, 1000) if os.geteuid() == 0 else (os.getuid(), os.getgid())


def _named_operator():
    """(name, uid, gid) of an account the host-deploy row can name in USER: one with a group of its own name, as the
    task's `group: USER` needs; as root a non-root one, so a directory left root's cannot pass for the operator's."""
    if pwd is None:
        return None
    if os.geteuid() != 0:
        me = pwd.getpwuid(os.getuid())
        try:
            return me.pw_name, me.pw_uid, grp.getgrnam(me.pw_name).gr_gid
        except KeyError:
            return None
    for entry in sorted(pwd.getpwall(), key=lambda e: e.pw_uid):
        if entry.pw_uid == 0:
            continue
        try:
            return entry.pw_name, entry.pw_uid, grp.getgrnam(entry.pw_name).gr_gid
        except KeyError:
            continue
    return None


def _run(tmp_path, home, check=False, in_container=False, user=None):
    """Run the task, registered, in a play shaped as deploy-stack.yml's (become, no facts); return (rc, output, the
    task's result). With `user`, the run is a host deploy by hand: no KONTROLL_OPERATOR_*, HOME the scratch home and
    USER/LOGNAME `user`; without it, the launcher's KONTROLL_OPERATOR_HOME/UID/GID."""
    task = dict(_the_task(), register="_made")
    result = tmp_path / "result.json"
    play = [{
        "name": "The run-log dir task, as deploy-stack.yml runs it",
        "hosts": "localhost",
        "gather_facts": False,
        # ansible-core takes USER for the user it runs as, so with `user` it would become root by sudo, which the
        # image lacks. The task's lookups read the controller's environment either way, and as root the module runs
        # as root without become.
        "become": os.geteuid() == 0 and user is None,
        "vars": {"stack_services": ["vector"], "in_container": in_container},
        "tasks": [task, {"name": "Write down the result",
                         "ansible.builtin.copy": {"content": "{{ _made | to_json }}", "dest": str(result),
                                                  "mode": "0600"},
                         "check_mode": False}],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    home_a = tmp_path / "ansible-home"
    home_a.mkdir(exist_ok=True)
    cfg = home_a / "ansible.cfg"
    cfg.write_text("""[defaults]
retry_files_enabled = False
interpreter_python = auto_silent
""", encoding="utf-8")
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
        "ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
        encoding="utf-8")
    uid, gid = _operator()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home_a), ANSIBLE_LOCAL_TEMP=str(home_a / "tmp"),
               ANSIBLE_REMOTE_TEMP=str(home_a / "rtmp"), ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0",
               ANSIBLE_LOCALHOST_WARNING="0", PYTHONDONTWRITEBYTECODE="1")
    if user is None:
        env.update(KONTROLL_OPERATOR_HOME=str(home), KONTROLL_OPERATOR_UID=str(uid), KONTROLL_OPERATOR_GID=str(gid))
    else:
        env.update(HOME=str(home), USER=user, LOGNAME=user)
    argv = ["ansible-playbook", "-i", str(tmp_path / "hosts.yml"), str(tmp_path / "play.yml")]
    p = subprocess.run(argv + (["--check", "--diff"] if check else []), cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    return p.returncode, out, (json.loads(result.read_text(encoding="utf-8")) if result.exists() else None)


def _owned(path):
    st = os.stat(path)
    return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)


def test_a_fresh_home_gets_the_run_log_dir_and_its_parents_as_the_operators(tmp_path):
    """On a home with no `~/.local`, the task makes `~/.local`, `~/.local/state`, `…/kontroll` and `…/runs`, each
    the operator's (uid and gid) and 0755: `runs` so the operator's scripts write their run-logs there and Vector
    (uid 10002) reads them through the bind, which needs `runs` o+rx and nothing of the parents; the parents so the
    operator's own tools write under `~/.local`. A second run changes nothing. Guards I-23 (Docker made them root's)
    and the file module's handling of the parents it creates, which this task relies on rather than a task per
    parent."""
    home = tmp_path / "home"
    home.mkdir()
    rc, out, made = _run(tmp_path, home)
    assert rc == 0 and made and made.get("changed") is True, out
    uid, gid = _operator()
    path = str(home)
    for part in RUNS.split("/"):
        path = os.path.join(path, part)
        assert _owned(path) == (uid, gid, 0o755), (path, _owned(path))
    rc, out, again = _run(tmp_path, home)
    assert rc == 0 and again and again.get("changed") is False, out


def test_a_parent_the_operator_already_made_keeps_its_owner_and_mode(tmp_path):
    """A `~/.local` that is already there, 0700, is left as it is: the task owns the directories it makes and the
    run-log directory itself, never the operator's own parents. Guards a fix that widens or takes over the
    operator's `~/.local`."""
    home = tmp_path / "home"
    (home / ".local").mkdir(parents=True)
    os.chmod(str(home / ".local"), 0o700)
    before = _owned(str(home / ".local"))
    rc, out, _ = _run(tmp_path, home)
    assert rc == 0, out
    assert _owned(str(home / ".local")) == before, out
    uid, gid = _operator()
    assert _owned(os.path.join(str(home), RUNS)) == (uid, gid, 0o755)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="making Docker's root-owned directories needs root")
def test_a_run_log_dir_docker_already_made_is_given_back_and_its_parents_are_left(tmp_path):
    """On a node where Docker got there first (`~/.local` through `runs` all root:root 0755, as an `up` with Vector
    leaves them), the task gives `runs` back to the operator and leaves the parents as they are: the run-logs work
    again on the next host deploy, and the parents are the operator's own repair (docs/troubleshooting.md §4), since
    the task never takes over directories it did not make. Guards the CHANGELOG's claim that a host deploy repairs
    `runs`, and a fix that would chown the operator's `~/.local` tree on its own."""
    home = tmp_path / "home"
    home.mkdir()
    path = str(home)
    for part in RUNS.split("/"):
        path = os.path.join(path, part)
        os.mkdir(path)
        os.chown(path, 0, 0)
        os.chmod(path, 0o755)
    rc, out, made = _run(tmp_path, home)
    assert rc == 0 and made and made.get("changed") is True, out
    uid, gid = _operator()
    assert _owned(os.path.join(str(home), RUNS)) == (uid, gid, 0o755)
    for parent in (".local", ".local/state", ".local/state/kontroll"):
        assert _owned(os.path.join(str(home), parent)) == (0, 0, 0o755), parent


def test_a_dry_run_reports_the_dir_and_makes_nothing(tmp_path):
    """`--check --diff` (the mandated dry-run before an apply) reports the directory it would make, `runs` as a
    directory in the diff, and makes none of it. Guards the task changing a host in a dry-run, and a dry-run that
    says "changed" without saying what it would make."""
    home = tmp_path / "home"
    home.mkdir()
    rc, out, made = _run(tmp_path, home, check=True)
    assert rc == 0 and made and made.get("changed") is True, out
    after = (made.get("diff") or {}).get("after") or {}
    assert after.get("state") == "directory" and str(after.get("path", "")).endswith(RUNS), made.get("diff")
    assert not (home / ".local").exists(), sorted(os.listdir(str(home)))


def test_the_task_does_not_run_inside_the_installer(tmp_path):
    """In the installer container (`in_container`) the operator's home is not mounted, so the task is skipped there
    and the launcher makes the directory instead (tests/unit/test_launcher_operator_home_binds.py). Guards a task
    that would make the directory inside the container, where nothing would see it."""
    home = tmp_path / "home"
    home.mkdir()
    rc, out, made = _run(tmp_path, home, in_container=True)
    assert rc == 0 and made and made.get("skipped") is True, out
    assert not (home / ".local").exists()


@pytest.mark.skipif(_named_operator() is None, reason="needs an account with a group of its own name")
def test_a_host_deploy_by_hand_takes_the_home_and_owner_from_its_shell(tmp_path):
    """With no KONTROLL_OPERATOR_* (deploy-stack.yml run by hand on the control node, where no launcher exported
    them), the task takes the home from HOME and the owner and group from USER: the four directories are made under
    the scratch HOME, each that account's and 0755, and a second run changes nothing. Guards the fallbacks the host
    deploy runs on: without HOME's the task makes `/.local/…` at the filesystem's root (or fails) and the operator's
    home gets nothing; without USER's the directories are left the deploying root's, I-23's failure again."""
    name, uid, gid = _named_operator()
    home = tmp_path / "home"
    home.mkdir()
    rc, out, made = _run(tmp_path, home, user=name)
    assert rc == 0 and made and made.get("changed") is True, out
    assert str(made.get("path", "")) == os.path.join(str(home), RUNS), made.get("path")
    path = str(home)
    for part in RUNS.split("/"):
        path = os.path.join(path, part)
        assert _owned(path) == (uid, gid, 0o755), (path, _owned(path), name)
    rc, out, again = _run(tmp_path, home, user=name)
    assert rc == 0 and again and again.get("changed") is False, out
