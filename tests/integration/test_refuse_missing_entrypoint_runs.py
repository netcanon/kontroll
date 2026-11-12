"""ansible-playbook runs ansible/playbooks/tasks/refuse-missing-entrypoint.yml beside the include_role it guards, for
each place a class's entrypoint can be, or not be, where ansible-core looks: the check passes exactly where the
include runs the entrypoint, and refuses exactly where it does not (I-66).

WHY. backup-configs.yml's capture play includes `{{ device_role }}` with `tasks_from: backup` inside a block whose
rescue records a failed capture. An include that cannot load its entrypoint is no task failure: a role no search path
holds moved the host into the rescue with no failed task and out of every later play, a role with no tasks/backup.yml
ended the whole run (rc 4), and a host with no `device_role` ended it on 2.18; where that was every host the run ended
before the key cleanup and left the run's SSH keys on disk (I-66, the I-12 reads' S1). The check, the block's first
task, fails each of those as a task the rescue records. It mirrors ansible-core's role search
(RoleDefinition._load_role_path, Role._load_role_yaml), so a mirror that looked in the wrong place, in the wrong order,
or for the wrong file names would refuse a class that runs (a good host's backup recorded as failed) or pass one that
does not (the old abort). Each row runs the real check and the real include in one play and compares them.

Nothing contacts a host: the one host is `local`, the play gathers no facts, and every task is an `assert` or a `debug`
on the controller, in a tree under `tmp_path` with an Ansible configuration of the row's own. It needs ansible-playbook
on PATH; tests/validate.sh runs it in the validate image. tests/unit/test_backup_capture_rescue.py holds the check's
place in the capture play; tests/integration/test_backup_failed_run_cleans_up.py runs backup-configs.yml with it.
"""
import json
import os
import re
import shutil
import subprocess

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook on PATH"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHECK = os.path.join(ROOT, "ansible", "playbooks", "tasks", "refuse-missing-entrypoint.yml")
ENTRY_YAML = "- ansible.builtin.debug:\n    msg: LOADED\n"
ENTRY_JSON = json.dumps([{"ansible.builtin.debug": {"msg": "LOADED"}}])
MAIN = "- ansible.builtin.debug:\n    msg: MAIN\n"
_ABSENT = object()

# name: (device_role, {path under the row's tree: file content, or None for a directory}, extra env)
# The tree: pb/ holds the probe playbook (its directory is playbook_dir), cfg1/ and cfg2/ are the ansible.cfg's
# roles_path in that order, cwd/ is the run's working directory, coll/ the collections path. "@TMP@" is the tree.
CASES = {
    "roles-path-first-entry": ("zz", {"cfg1/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "roles-path-second-entry": ("zz", {"cfg2/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "the-playbooks-roles-dir": ("zz", {"pb/roles/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "the-playbook-dir-itself": ("zz", {"pb/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "a-relative-env-roles-path": ("zz", {"cwd/envroles/zz/tasks/backup.yml": ENTRY_YAML},
                                 {"ANSIBLE_ROLES_PATH": "envroles"}),
    "the-env-roles-path-replaces-the-cfgs": ("zz", {"cfg1/zz/tasks/backup.yml": ENTRY_YAML, "cwd/envroles": None},
                                             {"ANSIBLE_ROLES_PATH": "envroles"}),
    "the-name-as-a-relative-path": ("rel/zz", {"cwd/rel/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "the-name-as-an-absolute-path": ("@TMP@/abs/zz", {"abs/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "a-yaml-entry": ("zz", {"cfg1/zz/tasks/backup.yaml": ENTRY_YAML}, {}),
    "a-json-entry": ("zz", {"cfg1/zz/tasks/backup.json": ENTRY_JSON}, {}),
    "a-bare-entry": ("zz", {"cfg1/zz/tasks/backup": ENTRY_YAML}, {}),
    "no-role-of-that-name": ("zz", {"cfg1/other/tasks/backup.yml": ENTRY_YAML}, {}),
    "a-role-without-the-entry": ("zz", {"cfg1/zz/tasks/main.yml": MAIN}, {}),
    "a-role-without-a-tasks-dir": ("zz", {"cfg1/zz/defaults/main.yml": "x: 1\n"}, {}),
    "an-entry-that-is-a-directory": ("zz", {"cfg1/zz/tasks/backup.yml/main.yml": ENTRY_YAML}, {}),
    "shadowed-by-the-playbooks-roles-dir": ("zz", {"pb/roles/zz/tasks/main.yml": MAIN,
                                                   "cfg1/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "shadowed-by-an-earlier-roles-path-entry": ("zz", {"cfg1/zz/tasks/main.yml": MAIN,
                                                       "cfg2/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "shadowed-by-a-file-of-that-name": ("zz", {"cfg1/zz": "not a role\n", "cfg2/zz/tasks/backup.yml": ENTRY_YAML},
                                        {}),
    "a-dotted-name-with-the-entry": ("zz.one", {"cfg1/zz.one/tasks/backup.yml": ENTRY_YAML}, {}),
    "a-dotted-name-without-the-entry": ("zz.one", {"cfg1/zz.one/tasks/main.yml": MAIN}, {}),
    "no-device-role": (_ABSENT, {"cfg1/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "an-empty-device-role": ("", {"cfg1/zz/tasks/backup.yml": ENTRY_YAML}, {}),
    "a-device-role-that-is-a-list": (["zz"], {"cfg1/zz/tasks/backup.yml": ENTRY_YAML}, {}),
}


def _run(tmp_path, device_role, files, extra_env):
    """The run's output and exit code: a play on one `local` host whose inventory names `device_role` (none for
    _ABSENT) runs a block of the check (imported from the shipped file, `kontroll_entrypoint: backup`) and a debug
    `CHECK=passed`, whose rescue prints `CHECK=refused`, then the include_role of `{{ device_role }}` with `tasks_from:
    backup`; every entry file prints LOADED. `files` is written under `tmp_path` first."""
    base = str(tmp_path)
    for rel, content in files.items():
        path = os.path.join(base, rel)
        if content is None:
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    for d in ("pb", "cfg1", "cfg2", "cwd", "coll", "tmp"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    play = [{"name": "Probe", "hosts": "h", "gather_facts": False, "tasks": [
        {"name": "Check", "block": [
            {"name": "Refuse a class with no entrypoint", "ansible.builtin.import_tasks": CHECK,
             "vars": {"kontroll_entrypoint": "backup"}},
            {"name": "Passed", "ansible.builtin.debug": {"msg": "CHECK=passed"}}],
         "rescue": [{"name": "Refused", "ansible.builtin.debug": {"msg": "CHECK=refused"}}]},
        {"name": "Include the entrypoint",
         "ansible.builtin.include_role": {"name": "{{ device_role }}", "tasks_from": "backup"}}]}]
    with open(os.path.join(base, "pb", "probe.yml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(play, fh, sort_keys=False)
    host = {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}
    if device_role is not _ABSENT:
        host["device_role"] = device_role.replace("@TMP@", base) if isinstance(device_role, str) else device_role
    with open(os.path.join(base, "inv.yml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"all": {"hosts": {"h": host}}}, fh)
    with open(os.path.join(base, "ansible.cfg"), "w", encoding="utf-8") as fh:
        fh.write("[defaults]\nroles_path = %s:%s\ncollections_path = %s\nretry_files_enabled = False\n"
                 % (os.path.join(base, "cfg1"), os.path.join(base, "cfg2"), os.path.join(base, "coll")))
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    env.update(ANSIBLE_CONFIG=os.path.join(base, "ansible.cfg"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCAL_TEMP=os.path.join(base, "tmp"), ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_DEPRECATION_WARNINGS="0", PYTHONDONTWRITEBYTECODE="1", **extra_env)
    p = subprocess.run(["ansible-playbook", "-i", os.path.join(base, "inv.yml"), os.path.join(base, "pb", "probe.yml")],
                       cwd=os.path.join(base, "cwd"), env=env, capture_output=True, text=True, timeout=300)
    return p.stdout + p.stderr, p.returncode


def _verdicts(out):
    """(the check's verdict, whether the include ran the entrypoint) read from a run's output."""
    checked = re.findall(r'"msg": "CHECK=(passed|refused)"', out)
    assert len(checked) == 1, out
    return checked[0], '"msg": "LOADED"' in out


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_check_passes_exactly_where_include_role_runs_the_entrypoint(tmp_path, case):
    """For each case the check's verdict matches what the include_role after it did: it passes where the include ran
    the role's tasks/backup entry (printing LOADED), and refuses where the include did not, whether the include then
    failed, ended the run or loaded a role that runs nothing. The cases are each place ansible-core searches, in its
    order (the playbook's roles/, each roles_path entry, from ansible.cfg or from a relative ANSIBLE_ROLES_PATH, which
    replaces it, as Semaphore's Environment sets it; the playbook directory; the name as a relative or absolute path),
    each entry file name (`.yml`, `.yaml`, `.json`, bare), and each way a host's class has no entry (no role, a role
    without the file or without tasks/, an entry that is a directory, a role shadowed by an earlier one or by a file of
    that name, no, an empty or a non-string `device_role`). Guards I-66's check against a mirror of the search that
    would refuse a class that runs (its good host's capture recorded as failed) or pass one that cannot (the run
    ending before the key cleanup, as before)."""
    device_role, files, extra_env = CASES[case]
    out, _ = _run(tmp_path, device_role, files, extra_env)
    checked, loaded = _verdicts(out)
    assert (checked == "passed") == loaded, (case, checked, loaded, out)


def test_the_refusal_names_the_host_its_class_and_the_roles_path(tmp_path):
    """A host whose `device_role` names no installed role is refused with a message naming the host, the class, the
    entrypoint and the roles path as the run resolved it; a host with no `device_role` is named as such. Guards the
    message an operator reads in the run's output (the summary line names only the task): a refusal that named no class
    or no path would leave them to guess which inventory entry or roles path to fix."""
    out, _ = _run(tmp_path / "missing", "zz", {}, {})
    roles = "%s:%s" % (tmp_path / "missing" / "cfg1", tmp_path / "missing" / "cfg2")
    want = ("h: the class zz has no `backup` entrypoint installed (no tasks/backup.yml, .yaml or .json in the first "
            "role of that name on the roles path: %s). Refused for this host; the other hosts go on." % roles)
    assert '"msg": "%s"' % want in out, out
    out, _ = _run(tmp_path / "absent", _ABSENT, {}, {})
    assert '"msg": "h: the class (none: the host names no device_role) has no `backup` entrypoint' in out, out


def test_a_class_in_collection_form_is_left_to_the_include(tmp_path):
    """A `device_role` in collection form (`ns.collection.role`) passes the check unread: one whose collection is
    installed on the collections path runs its entry, and one whose collection is not installed passes the check and
    fails at the include, as before the check. Guards against refusing a class a collection ships (the check does not
    search the collections, which ansible-core looks in first for such a name), and records the case the check leaves
    to the capture play's rescue (backup-configs.yml's second rescue task fails it)."""
    out, _ = _run(tmp_path / "installed", "zz.lab.raw",
                  {"coll/ansible_collections/zz/lab/roles/raw/tasks/backup.yml": ENTRY_YAML}, {})
    assert _verdicts(out) == ("passed", True), out
    out, _ = _run(tmp_path / "absent", "zz.lab.missing", {}, {})
    assert _verdicts(out) == ("passed", False), out
