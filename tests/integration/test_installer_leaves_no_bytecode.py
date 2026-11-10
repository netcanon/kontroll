"""The installer's runs leave no `__pycache__` in the operator's checkout (I-54).

WHY. The installer runs as root with the operator's checkout bound at its own path (docker/services/installer.yaml),
and its first `init` wrote root-owned `__pycache__` directories into it (the I-3 read A listed them under
scripts/kontroll/ and ansible/filter_plugins/). Two writers: each `python3 scripts/<generator>.py` a play runs
without `-B` (bootstrap's lockfile task; deploy-stack's frontend resolver, its generators and its own lockfile task)
compiles scripts/kontroll/ in place, and Ansible compiles the repo's filter plugins when a play loads them. A
root-owned cache in the operator's tree is the I-23 shape: a later run as the operator cannot replace or remove it.
The installer's environment now sets PYTHONDONTWRITEBYTECODE, a literal, which every Python of the run inherits:
ansible-playbook's own and each command a task runs, so no call needs its own `-B`.

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
_NEEDS = pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="needs ansible-playbook (it runs the task)")


def _installer_env():
    with open(INSTALLER, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]["init"]["environment"]


def test_the_installer_sets_no_bytecode_as_a_literal():
    """installer.yaml's environment sets PYTHONDONTWRITEBYTECODE to "1", written as a literal: an interpolation
    (`${PYTHONDONTWRITEBYTECODE:-1}`, say) would let an operator's exported empty value turn it off, and the root-owned
    cache would come back (I-54). Both launchers run this one fragment, so the value reaches every verb."""
    value = _installer_env().get("PYTHONDONTWRITEBYTECODE")
    assert value == "1", value


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
