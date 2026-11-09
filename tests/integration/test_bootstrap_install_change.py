"""The bootstrap play's collection install reports a change only where what it installs stays (I-3).

WHY. A second apply must report 0 changed (CLAUDE.md), and the installer's `init` never could: bootstrap's `Install
the selected collections` task reported changed whenever galaxy printed "Installing", and in the installer container
every run installs afresh what the image does not bake, into the image's own collections path, which no bind keeps
and which ends with the container (`--rm`). So each `init` said 1 changed where a re-install had changed nothing the
node keeps, and a real change could not be told from it. The task now reports an install changed only off the
container, where it stays under ansible/collections and a second run installs nothing (galaxy's "Nothing to do", or
the L2b wrapper's "already present"). What a container run does keep is the wrapper's record of a digest trusted
for the first time (instance/trust/observed-digests.yml, in the repo bind): its `L2b recorded` line is a change
wherever it is printed, so a first `init`, or one after the fleet gains a collection, still says so.

These rows run the task's own `changed_when` and the play's own `in_container`, both read from bootstrap.yml, by
ansible-playbook in a play of the row's own, whose stand-in for the install wrapper prints what galaxy prints; the
recap says whether the run changed anything. Those rows need ansible-playbook: where it is missing they are skipped
(the gate's images and CI's validate job have it). The rows that read only YAML (the task's shape, the service's
variable) run everywhere, CI's pytest matrix included. Nothing of the developer's Ansible reaches the play: its
configuration, home, temporary directory, vars-plugin path and inventory are the row's own.
"""
import os
import re
import shutil
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.integration

NEEDS_ANSIBLE = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None,
    reason="needs ansible-playbook (it runs bootstrap's install task's change signal for real)")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOOTSTRAP = os.path.join(ROOT, "ansible", "playbooks", "bootstrap.yml")
INSTALL = "Install the selected collections (never-brick wrapper)"
# The install task's keys, and no more: a `when` or a `vars` of its own would run it otherwise than the rows do.
TASK_KEYS = {"name", "ansible.builtin.command", "register", "changed_when"}

# What ansible-galaxy prints, through the wrapper, for a collection it installs and for a set already there.
INSTALLED = ("Starting galaxy collection install process\nProcess install dependency map\nStarting collection install "
             "process\nInstalling 'community.general:9.5.0' to '/usr/share/ansible/collections/ansible_collections/"
             "community/general'\ncommunity.general:9.5.0 was installed successfully")
NOTHING = ("Starting galaxy collection install process\nNothing to do. All requested collections are already "
           "installed. If you want to reinstall them, consider using `--force`.")
# What the L2b wrapper (scripts/install-collections.py) prints around galaxy's install where instance/trust/ exists:
# a digest recorded for the first time, one verified against the lock, and a pinned set already present.
ENGAGED = "L2b digest re-verify: engaged (lock instance/trust/observed-digests.yml) — %d/4 collection(s) to install\n"
L2B_RECORDED = ENGAGED % 1 + "  L2b recorded  community.general==9.5.0\n" + INSTALLED
L2B_OK = ENGAGED % 1 + "  L2b ok        community.general==9.5.0\n" + INSTALLED
L2B_PRESENT = ENGAGED % 0 + "L2b: the pinned set is already present — nothing to fetch or install"


def _tasks(entries):
    """Every task in a list of them, with those inside a block, a rescue or an always."""
    for task in entries or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _tasks(task.get(key))


def _sets_in_container(task):
    """Whether a task (or a block) gives `in_container` a value of its own: in its `vars`, or by a `set_fact` in
    any spelling, as a mapping or as key=value."""
    if "in_container" in (task.get("vars") or {}):
        return True
    for key in ("set_fact", "ansible.builtin.set_fact", "ansible.legacy.set_fact"):
        fact = task.get(key)
        if isinstance(fact, dict) and "in_container" in fact:
            return True
        if isinstance(fact, str) and re.search(r"(^|\s)in_container\s*=", fact):
            return True
    return False


def _from_bootstrap():
    """The install task's `changed_when` and the play's `in_container`, as bootstrap.yml has them. Reads the one
    play that has tasks (an `import_playbook` entry beside it, such as a run-id stamp, is not a play of bootstrap's
    own). Asserts the task is still the wrapper's command registered as `galaxy` with no key beyond its
    `changed_when`, and that no task or block in the play sets `in_container`: so a row cannot pass on a task the
    play runs otherwise, or no longer runs."""
    entries = yaml.safe_load(open(BOOTSTRAP, encoding="utf-8"))
    plays = [entry for entry in entries if "tasks" in entry]
    assert len(plays) == 1, [sorted(entry) for entry in entries]
    play = plays[0]
    every = [t for key in ("pre_tasks", "tasks", "post_tasks", "handlers") for t in _tasks(play.get(key))]
    tasks = [t for t in every if t.get("name") == INSTALL]
    assert len(tasks) == 1, INSTALL
    task = tasks[0]
    assert set(task) == TASK_KEYS, sorted(task)
    assert task["ansible.builtin.command"]["cmd"] == "python3 scripts/install-collections.py", task
    assert task["register"] == "galaxy", task
    writers = [t.get("name", "(unnamed)") for t in every if _sets_in_container(t)]
    assert not writers, writers
    return task["changed_when"], play["vars"]["in_container"]


def _play(tmp_path, stdout, in_container):
    """Run a play of the row's own: bootstrap's `in_container` and the install task's `changed_when`, around a
    stand-in for the wrapper that prints `stdout`, with KONTROLL_IN_CONTAINER set to `in_container` (unset when
    empty). Returns the exit code, the output and the recap's counts for localhost."""
    changed_when, in_container_var = _from_bootstrap()
    home = tmp_path / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n", encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
        "ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
        encoding="utf-8")
    playbook = tmp_path / "install-change.yml"
    playbook.write_text(yaml.safe_dump([{
        "name": "bootstrap's install task's change signal", "hosts": "localhost", "gather_facts": False,
        "vars": {"in_container": in_container_var},
        "tasks": [{"name": "A stand-in for the install wrapper",
                   "ansible.builtin.command": {"argv": ["printf", "%s\n", stdout]},
                   "register": "galaxy", "changed_when": changed_when}]}], sort_keys=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    # The vars-plugin path's default keeps a site-wide directory (/usr/share/ansible/plugins/vars): the row's own.
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_VARS_PLUGINS=str(home / "plugins" / "vars"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1")
    if in_container:
        env["KONTROLL_IN_CONTAINER"] = in_container
    run = subprocess.run(["ansible-playbook", "-i", str(inventory), str(playbook)], capture_output=True, text=True,
                         env=env, cwd=str(tmp_path), timeout=300)
    recap = re.search(r"localhost\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", run.stdout)
    assert recap, run.stdout + run.stderr
    return run.returncode, run.stdout + run.stderr, dict(zip(("ok", "changed", "unreachable", "failed"),
                                                              map(int, recap.groups())))


def test_the_install_task_is_bootstraps_own():
    """bootstrap.yml's install task is the wrapper's command registered as `galaxy`, with no key beyond its
    `changed_when`, and no task sets `in_container`: read from the YAML with no ansible-playbook, so it holds in CI's
    pytest matrix too. Guards the rows below passing on a copy of the task while bootstrap runs it otherwise: a
    `vars: {in_container: false}` on the task, or a `set_fact` of it before, would report every `init` changed
    again (I-3 itself); a `when: not in_container` would skip the install, and its L2b record, in the container."""
    changed_when, in_container = _from_bootstrap()
    assert "in_container" in changed_when, changed_when
    assert "KONTROLL_IN_CONTAINER" in in_container, in_container


@NEEDS_ANSIBLE
@pytest.mark.parametrize("where, in_container, stdout, changed", [
    ("node", "", INSTALLED, 1),
    ("node", "", NOTHING, 0),
    ("container", "1", INSTALLED, 0),
    ("container", "1", NOTHING, 0),
    ("node", "", L2B_RECORDED, 1),
    ("node", "", L2B_PRESENT, 0),
    ("container", "1", L2B_RECORDED, 1),
    ("container", "1", L2B_OK, 0),
    ("container", "1", L2B_PRESENT, 0),
], ids=["node-installs", "node-nothing-to-do", "container-installs", "container-nothing-to-do",
        "node-l2b-records", "node-l2b-present", "container-l2b-records", "container-l2b-verifies",
        "container-l2b-present"])
def test_the_install_is_a_change_only_where_it_stays(tmp_path, where, in_container, stdout, changed):
    """On the node (KONTROLL_IN_CONTAINER unset) a collection installed is a change and nothing installed is none;
    in the installer container (KONTROLL_IN_CONTAINER=1, as docker/services/installer.yaml sets it) an install is
    none, since it lands in a path that ends with the container, but a digest the L2b wrapper records for the first
    time is a change on either, since that record stays in the repo bind. Guards I-3: every `init` reporting 1
    changed in bootstrap where a re-install changed nothing the node keeps, so a second apply could never read 0
    changed; and the fix reaching too far, a host install that does stay, or a first trust record in the
    container (the first `init`, a fleet that gains a collection), reported as no change."""
    rc, out, counts = _play(tmp_path, stdout, in_container)
    assert rc == 0, out
    assert counts == {"ok": 1, "changed": changed, "unreachable": 0, "failed": 0}, (where, counts, out)


def test_the_installer_service_says_it_is_the_container():
    """docker/services/installer.yaml sets KONTROLL_IN_CONTAINER to "1" in the init service's environment, the value
    the rows above give the container. Guards the change signal's seam: were the service to stop setting it, the
    installer's install would be reported changed again on every `init`, and the container rows here would still
    pass on a value nothing sets."""
    service = yaml.safe_load(open(os.path.join(ROOT, "docker", "services", "installer.yaml"), encoding="utf-8"))
    assert service["services"]["init"]["environment"]["KONTROLL_IN_CONTAINER"] == "1"
