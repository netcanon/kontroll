"""deploy-stack.yml's task that makes the operator scripts' run-log directory, run for real by ansible-playbook.

WHY: Vector binds `<operator home>/.local/state/kontroll/runs` read-only, and Docker makes a missing bind source and
each missing parent root's. On a host deploy (deploy-stack.yml run on the control node, not in the installer, whose
launcher makes the directory first) nothing made it, so the first `up` with Vector left `~/.local` root's and every
later operator script ran without its run-log (I-23, 2026-11-08). The task now makes it before `up`, as the operator.
These rows take that task from the playbook as it is and run it, as deploy-stack.yml runs it (become, the operator's
identity in KONTROLL_OPERATOR_*), against a scratch home: on a fresh home, a home whose `~/.local` the operator had
already made with a mode of their own, a home Docker already got to (root only), in a dry-run, and a second time.

The owner is uid 1000 when the suite runs as root (the gate image's case: root makes the directories, and they must
be the operator's), else the suite's own uid. Needs POSIX and ansible-playbook; without them every row is skipped.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest
import yaml

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


def _run(tmp_path, home, check=False, in_container=False):
    """Run the task, registered, in a play shaped as deploy-stack.yml's (become, no facts); return (rc, output, the
    task's result)."""
    task = dict(_the_task(), register="_made")
    result = tmp_path / "result.json"
    play = [{
        "name": "The run-log dir task, as deploy-stack.yml runs it",
        "hosts": "localhost",
        "gather_facts": False,
        "become": os.geteuid() == 0,
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
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1", KONTROLL_OPERATOR_HOME=str(home), KONTROLL_OPERATOR_UID=str(uid),
               KONTROLL_OPERATOR_GID=str(gid))
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
    the operator's (uid and gid) and 0755, so the operator's scripts can write their run-logs there and Vector
    (uid 10002) can read them; and a second run changes nothing. Guards I-23 (Docker made them root's) and the
    file module's handling of the parents it creates, which this task relies on rather than a task per parent."""
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
    again on the next deploy, and the parents are the operator's own repair (docs/troubleshooting.md §4), since the
    task never takes over directories it did not make. Guards the CHANGELOG's claim that a deploy repairs `runs`,
    and a fix that would chown the operator's `~/.local` tree on its own."""
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
    """`--check --diff` (the mandated dry-run before an apply) reports the directory it would make and makes none of
    it. Guards the task changing a host in a dry-run."""
    home = tmp_path / "home"
    home.mkdir()
    rc, out, made = _run(tmp_path, home, check=True)
    assert rc == 0 and made and made.get("changed") is True, out
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
