"""A read the control node's plays make for a later task runs under `--check` too, or says why it need not.

WHY (the failure this guards). A `command` / `shell` task is SKIPPED under `--check`, and a skipped task's register
is not undefined: it holds an empty `stdout` and a zero `rc`. So a resolver whose result a later task reads does not
fail where it is skipped — the mandated `--check --diff` dry-run goes wrong further on, at the task that reads it.
`_secrets_dir` went empty and the `.env` render died (live-caught 2026-06-21); the published-image digests
(`_img_env`) read as none, and `kontroll check` stopped at the fail-closed "no recorded digest" assert. Both were
fixed by `check_mode: false`. The guard before this one was a hand list of
register names, which claimed to catch "a new SOPS-feeding resolver added without it" and did not: `_img_env` was
added without it, and without a row.

The rule reads the plays instead. In every play under ansible/playbooks/ that targets `localhost` (the control node)
— and every task file such a play imports — a `command` / `shell` / `raw` task that declares itself read-only
(`changed_when: false`) and whose `register:` is read elsewhere must carry `check_mode: false`, or have a row in
`_EXEMPT` that says why an empty value in a dry-run is right. A role's tasks are out of scope by rule: a role read
runs against a device, where forcing it to run under `--check` would execute the device command for real.
"""
import glob
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_COMMANDS = ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell", "ansible.builtin.raw", "raw")
_IMPORTS = ("ansible.builtin.import_tasks", "import_tasks", "ansible.builtin.include_tasks", "include_tasks")

# (file, register) → why the read stays skipped in a dry-run. Every row is checked to still name such a read.
_EXEMPT = {
    ("bootstrap.yml", "age_pub"): "the control key is minted by the task before it, which a dry-run skips on a fresh "
                                  "node: forced to run, the read fails on the absent file. Only the summary line "
                                  "prints it",
    ("bootstrap.yml", "ansible_pub"): "the same, for the ansible SSH key the task before it mints",
    ("deploy-stack.yml", "compose_ps"): "the `ps` after the bring-up, for display only; a dry-run brings nothing up",
}


def _walk(tasks):
    """Every leaf task, recursing into block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if any(isinstance(task.get(k), list) for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _walk(task.get(key))
        else:
            yield task


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def control_node_task_files(playbooks_dir):
    """{path: (text, [leaf tasks], [paths of the playbooks that import it])} for every play that targets `localhost`
    and every task file such a play imports."""
    scoped = {}
    for path in sorted(glob.glob(os.path.join(playbooks_dir, "*.yml"))):
        text = _load(path)
        doc = yaml.safe_load(text)
        tasks = []
        for play in doc if isinstance(doc, list) else []:
            if not isinstance(play, dict) or play.get("hosts") != "localhost":
                continue
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                tasks += list(_walk(play.get(section)))
        if not tasks:
            continue
        scoped.setdefault(path, (text, [], []))[1].extend(tasks)
        for task in tasks:
            for key in _IMPORTS:
                target = task.get(key)
                target = target.get("file") if isinstance(target, dict) else target
                if isinstance(target, str) and "{{" not in target:
                    imported = os.path.normpath(os.path.join(os.path.dirname(path), target))
                    if imported not in scoped:
                        body = _load(imported)
                        scoped[imported] = (body, list(_walk(yaml.safe_load(body))), [])
                    scoped[imported][2].append(path)
    return scoped


def unforced_reads(playbooks_dir):
    """{(file name, register)} of every control-node read-only command whose register is read elsewhere and which
    does not carry `check_mode: false`."""
    scoped = control_node_task_files(playbooks_dir)
    found = set()
    for path, (text, tasks, importers) in scoped.items():
        for task in tasks:
            register = task.get("register")
            if not register or not any(k in task for k in _COMMANDS) or task.get("changed_when") is not False:
                continue
            word = re.compile(r"\b%s\b" % re.escape(register))
            reads = len(word.findall(text)) - 1 + sum(len(word.findall(scoped[p][0])) for p in importers)
            if reads > 0 and task.get("check_mode") is not False:
                found.add((os.path.basename(path), register))
    return found


def test_every_control_node_read_runs_in_check_mode_or_is_exempted():
    """Every read-only command a control-node play registers and reads again carries `check_mode: false`, or has
    an `_EXEMPT` row; and every row still names such a read. Guards the next `_img_env` — a resolver whose empty
    value silently changes the mandated dry-run — and an exemption left behind after its read was forced, or
    removed, where it would excuse the next read of that name."""
    found = unforced_reads(os.path.join(ROOT, "ansible", "playbooks"))
    assert found - set(_EXEMPT) == set(), "add `check_mode: false`, or an _EXEMPT row that says why: %r" % sorted(
        found - set(_EXEMPT))
    assert set(_EXEMPT) - found == set(), "stale _EXEMPT rows: %r" % sorted(set(_EXEMPT) - found)


def test_the_resolvers_that_feed_the_render_are_found():
    """The detector's scope reaches the resolvers the `.env` render and the canonical play depend on: removing
    `check_mode: false` from any of them would be reported. Guards a scope that silently stopped reading
    deploy-stack.yml, local-canonical.yml or the task file deploy-stack imports — the sweep would then pass on
    nothing."""
    scoped = control_node_task_files(os.path.join(ROOT, "ansible", "playbooks"))
    names = {os.path.basename(p) for p in scoped}
    assert {"deploy-stack.yml", "local-canonical.yml", "bootstrap.yml", "content-clone.yml"} <= names, names
    forced = {(os.path.basename(p), t.get("register")) for p, (_, tasks, _) in scoped.items() for t in tasks
              if t.get("check_mode") is False and t.get("changed_when") is False}
    for read in (("deploy-stack.yml", "_secrets_dir_resolved"), ("deploy-stack.yml", "_img_env"),
                 ("content-clone.yml", "_clone_foreign")):
        assert read in forced, read


_PLANTED = """- hosts: localhost
  vars:
    _val: "{{ _probe.stdout }}"
  tasks:
    - name: a resolver
      ansible.builtin.command: cat /etc/hostname
      register: _probe
      changed_when: false%s
    - block:
        - name: a nested resolver
          ansible.builtin.shell: echo x
          register: _nested
          changed_when: false
        - ansible.builtin.debug:
            msg: "{{ _nested.stdout }}"
    - name: read once, never used
      ansible.builtin.command: echo unused
      register: _unused
      changed_when: false
    - name: a writer
      ansible.builtin.command: touch /tmp/x
      register: _writer
    - ansible.builtin.debug:
        msg: "{{ _writer.rc }}"
    - ansible.builtin.import_tasks: tasks/more.yml
    - ansible.builtin.debug:
        msg: "{{ _imported.stdout }}"
- hosts: all
  tasks:
    - name: a device read
      ansible.builtin.command: show version
      register: _device
      changed_when: false
    - ansible.builtin.debug:
        msg: "{{ _device.stdout }}"
"""
_PLANTED_TASKS = """- name: an imported resolver
  ansible.builtin.command: echo y
  register: _imported
  changed_when: false
"""


@pytest.mark.parametrize("forced,expect", [
    ("", {("p.yml", "_probe"), ("p.yml", "_nested"), ("more.yml", "_imported")}),
    ("\n      check_mode: false", {("p.yml", "_nested"), ("more.yml", "_imported")}),
], ids=["unforced", "forced"])
def test_the_detector_flags_a_planted_unlisted_resolver(tmp_path, forced, expect):
    """A planted control-node play: a resolver read by a play var, one nested in a block, one in an imported task
    file read by the importing play — each flagged until it carries `check_mode: false`; a read nothing uses, a
    writer (no `changed_when: false`) and a read in a play for `all` are not. Guards a detector that cannot fail,
    one that misses a block or an imported file, and one that reaches device plays."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / "p.yml").write_text(_PLANTED % forced, encoding="utf-8")
    (tmp_path / "tasks" / "more.yml").write_text(_PLANTED_TASKS, encoding="utf-8")
    assert unforced_reads(str(tmp_path)) == expect


def test_no_role_read_is_forced_to_run_in_check_mode():
    """No `command` / `shell` / `raw` task under ansible/roles/ carries `check_mode: false`. A role task runs
    against a device, so forcing it to run under `--check` executes the device command for real during the dry-run
    meant to change nothing. Guards the resolver fix being copied onto a device read."""
    offenders = []
    for path in sorted(glob.glob(os.path.join(ROOT, "ansible", "roles", "**", "*.yml"), recursive=True)):
        try:
            doc = yaml.safe_load(_load(path))
        except yaml.YAMLError:
            continue
        if not isinstance(doc, list):
            continue
        for task in _walk(doc):
            if any(k in task for k in _COMMANDS) and task.get("check_mode") is False:
                offenders.append("%s: %s" % (os.path.relpath(path, ROOT), task.get("name")))
    assert offenders == []
