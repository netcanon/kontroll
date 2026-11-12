"""The installer's runs leave no `__pycache__` in the operator's checkout (I-54).

WHY. The installer runs as root with the operator's checkout bound at its own path (docker/services/installer.yaml),
and its first `init` wrote root-owned `__pycache__` directories into it (the I-3 read A listed them under
scripts/kontroll/ and ansible/filter_plugins/). Two writers: each `python3 scripts/<generator>.py` a play runs
without `-B` that imports scripts/kontroll/ (bootstrap's lockfile task; deploy-stack's generators and its own lockfile
task) compiles it in place, and Ansible compiles the repo's filter plugins when a play loads them. A script run as
itself is not cached, so the frontend and path resolvers, which import only the standard library, write nothing. A
root-owned cache in the operator's tree is the I-23 shape: a later run as the operator cannot replace or remove it.
The installer's environment now sets PYTHONDONTWRITEBYTECODE, a literal, which every Python of the run inherits:
ansible-playbook's own and each command a task runs, so no call needs its own `-B`. A host deploy has no such
variable, and deploy-stack's lockfile task runs there as root (the play's become), so that task carries `-B`.

The run rows take bootstrap.yml's generate task from the playbook as it is (not rewritten), and a task that applies
one of the repo's filters, and run them with ansible-playbook against a copy of the tree: with the installer's value
no `__pycache__` or `.pyc` appears anywhere in the copy; without it (the control row) both writers leave one, so the
rows can see what they guard. Nothing of the developer's Ansible reaches the play: its configuration and home are the
row's own. Needs ansible-playbook.
"""
import os
import shutil
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.integration

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALLER = os.path.join(ROOT, "docker", "services", "installer.yaml")
BOOTSTRAP = os.path.join(ROOT, "ansible", "playbooks", "bootstrap.yml")
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")
LAUNCHERS = [os.path.join(ROOT, *p) for p in (("scripts", "kontroll.sh"), ("scripts", "kontroll-installer.sh"),
                                              ("docker", "installer", "entrypoint.sh"))]
_NEEDS = pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="needs ansible-playbook (it runs the task)")


def _installer_env():
    with open(INSTALLER, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]["init"]["environment"]


def test_the_installer_sets_no_bytecode_as_a_literal():
    """installer.yaml's environment sets PYTHONDONTWRITEBYTECODE to "1", written as a literal: an interpolation
    (`${PYTHONDONTWRITEBYTECODE:-1}`, say) would let an operator's exported `0` turn it off (compose's `:-` covers
    only an empty or unset value, and CPython reads `0` as off), and `${PYTHONDONTWRITEBYTECODE-1}` an exported empty
    one, and the root-owned cache would come back (I-54)."""
    value = _installer_env().get("PYTHONDONTWRITEBYTECODE")
    assert value == "1", value


def test_no_launcher_passes_or_drops_the_value():
    """Neither launcher (scripts/kontroll.sh, the launch kit's; scripts/kontroll-installer.sh, a checkout's) nor the
    installer's entrypoint names PYTHONDONTWRITEBYTECODE: both launchers run installer.yaml's one `init` service, and
    every verb runs in it through the entrypoint, so the fragment's value reaches every verb only while none of the
    three passes its own (`-e PYTHONDONTWRITEBYTECODE=` on the `run` outranks the fragment) or drops it (`unset`, `env
    -u`), each of which would need its name. Guards the claim the row above rests on, which the reads of the change
    found held by reading alone (an `unset` in the entrypoint and an `-e` on a launcher's `run` passed every row)."""
    for path in LAUNCHERS:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert "installer.yaml" in text or path.endswith("entrypoint.sh"), path
        assert "PYTHONDONTWRITEBYTECODE" not in text, path


def test_deploy_stacks_lockfile_task_runs_without_bytecode():
    """deploy-stack.yml's runner-lockfile task, which runs under the play's `become`, runs `python3 -B`. On a host
    deploy that become is sudo to root over the operator's checkout, sudo's reset environment drops any
    PYTHONDONTWRITEBYTECODE, and the generator imports scripts/kontroll/, so without `-B` it left a root-owned
    `scripts/kontroll/__pycache__` there (the reads of I-54: the fix covered the installer alone). Its sibling
    generators run as the operator (`become: false`); a task that leaves the play's become must keep `-B`."""
    with open(DEPLOY, encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    found = [t for p in plays for t in p.get("tasks", []) if str(t.get("name", "")).startswith(
        "Regenerate the runner's collection lockfile")]
    assert len(found) == 1, "deploy-stack's lockfile task moved — move this test with it"
    task = found[0]
    cmd = task["ansible.builtin.command"]["cmd"]
    assert task.get("become") is False or cmd.split()[:2] == ["python3", "-B"], task


def _generate_task():
    with open(BOOTSTRAP, encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    found = [t for p in plays for t in p.get("tasks", []) if str(t.get("name", "")).startswith(
        "Generate the collection lockfile")]
    assert len(found) == 1, "bootstrap's generate task moved — move this test with it"
    return found[0]


def _run(tmp_path, bytecode_off):
    """bootstrap's generate task and a filter task against a copy of the tree; returns the copy's caches (dirs named
    __pycache__ and .pyc files, relative paths)."""
    tree = tmp_path / "tree"
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", "collections")
    for rel in ("scripts", "modules", "actuation", "instance.example", "ansible"):
        shutil.copytree(os.path.join(ROOT, rel), str(tree / rel), ignore=skip)
    (tree / "ansible" / "collections").mkdir()
    play = [{"name": "The installer's bytecode", "hosts": "localhost", "gather_facts": False,
             "vars": {"repo_root": str(tree)},
             "tasks": [_generate_task(),
                       {"name": "Apply one of the repo's filters (Ansible loads the plugin file)",
                        "ansible.builtin.debug": {"msg": "{{ '{a}' | kontroll_render_token({'a': 'x'}) }}"}}]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    home = tmp_path / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nfilter_plugins = %s\nretry_files_enabled = False\ninterpreter_python = auto_silent\n"
                   % (tree / "ansible" / "filter_plugins"), encoding="utf-8")
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
        "ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANSIBLE_", "KONTROLL_", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0")
    if bytecode_off:
        env["PYTHONDONTWRITEBYTECODE"] = _installer_env()["PYTHONDONTWRITEBYTECODE"]
    p = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "hosts.yml"), str(tmp_path / "play.yml")],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0 and "msg\": \"x\"" in p.stdout, p.stdout + p.stderr
    assert (tree / "ansible" / "collections" / "requirements.generated.yml").is_file(), "the task did not generate"
    caches = []
    for dirpath, dirs, files in os.walk(str(tree)):
        rel = os.path.relpath(dirpath, str(tree)).replace(os.sep, "/")
        caches += [rel + "/" + d for d in dirs if d == "__pycache__"]
        caches += [rel + "/" + f for f in files if f.endswith(".pyc")]
    return caches


@_NEEDS
def test_a_run_with_the_installers_value_leaves_no_bytecode_in_the_tree(tmp_path):
    """With PYTHONDONTWRITEBYTECODE as installer.yaml sets it, bootstrap's own generate task (a bare `python3
    scripts/gen-requirements.py`, which imports scripts/kontroll/) and a task applying the repo's
    kontroll_render_token filter leave no `__pycache__` and no `.pyc` anywhere in the tree they ran over (I-54)."""
    assert _run(tmp_path, bytecode_off=True) == []


@_NEEDS
def test_without_it_both_writers_leave_a_cache(tmp_path):
    """The control: the same run without the variable leaves a `__pycache__` under scripts/kontroll/ (the generator's
    imports) and under ansible/filter_plugins/ (Ansible's plugin load), the two the I-3 read A listed. Without this
    row, a run that wrote nothing for another reason (a task that no longer imports, a filter not loaded) would pass
    the row above for nothing."""
    caches = _run(tmp_path, bytecode_off=False)
    assert "scripts/kontroll/__pycache__" in caches, caches
    assert "ansible/filter_plugins/__pycache__" in caches, caches
