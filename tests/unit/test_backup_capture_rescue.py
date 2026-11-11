"""backup-configs.yml's capture play records a capture that fails as a task as a failed capture (I-12).

WHY. A capture that fails as a task (a read that exits non-zero or errors, a module or permission error, the API
credential gate's assert, a write that fails) failed its host at the capture play: the summary printed no line for
it, and when every host failed so, Ansible ended the run there, before the capture history, the prune and the per-run
SSH-key cleanup, so the run's key-auth hosts' keys stayed on disk (the I-9 reads' S1). The capture play's dispatch is
now a block whose rescue records the host as a failed capture, naming the task it stopped at, so the host goes on to
the cleanup, and the summary names it and fails the run. tests/integration/test_backup_failed_run_cleans_up.py runs
it for real.
"""
import os

import jinja2
import jinja2.meta
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")


def _capture_play():
    with open(PLAYBOOK, encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    found = [p for p in plays if isinstance(p, dict) and str(p.get("name", "")).startswith("Capture device configs")]
    assert len(found) == 1, [p.get("name") for p in plays if isinstance(p, dict)]
    return found[0]


def test_the_capture_dispatch_is_rescued_and_the_rescue_records_a_failed_capture():
    """The capture play's tasks are the `device_role` refusal's import, then one block and nothing else: the block
    holds only the include_role of `{{ device_role }}` with `tasks_from: backup` and carries no other keyword (no
    `always`, `when`, `ignore_errors`); its rescue is one `set_fact` of `kontroll_backup_failed: true` and a
    `kontroll_backup_result` that starts "FAILED:" and reads nothing but `ansible_failed_task.name`. Guards I-12: the
    dispatch outside a block failed its host at the capture play, so the summary had no line for it, and when every
    host failed so the run ended before the key cleanup; a rescue that sets no `kontroll_backup_failed` would let the
    run pass on a capture that failed; and a message that read `ansible_failed_result` (or anything of the failed
    task but its name) would print the read's answer, which its `no_log` hides, in the summary's line, which is not
    `no_log`. The refusal stays outside the block, so a refused host fails as before
    (test_group_dispatch_refuses_local.py holds it first)."""
    tasks = _capture_play()["tasks"]
    assert len(tasks) == 2, tasks
    assert tasks[0].get("ansible.builtin.import_tasks") == "tasks/refuse-device-role-fact.yml", tasks[0]
    block = tasks[1]
    assert set(block) == {"name", "block", "rescue"}, block
    assert len(block["block"]) == 1, block["block"]
    include = block["block"][0]
    assert set(include) == {"name", "ansible.builtin.include_role"}, include
    assert include["ansible.builtin.include_role"] == {"name": "{{ device_role }}", "tasks_from": "backup"}
    assert len(block["rescue"]) == 1, block["rescue"]
    rescue = block["rescue"][0]
    assert set(rescue) == {"name", "ansible.builtin.set_fact"}, rescue
    facts = rescue["ansible.builtin.set_fact"]
    assert set(facts) == {"kontroll_backup_result", "kontroll_backup_failed"}, facts
    assert facts["kontroll_backup_failed"] is True
    assert facts["kontroll_backup_result"].startswith("FAILED:"), facts
    tree = jinja2.Environment().parse(facts["kontroll_backup_result"])
    assert jinja2.meta.find_undeclared_variables(tree) == {"ansible_failed_task"}
    uses = [n for n in tree.find_all(jinja2.nodes.Name) if n.name == "ansible_failed_task"]
    reads = [n for n in tree.find_all(jinja2.nodes.Getattr) if isinstance(n.node, jinja2.nodes.Name)
             and n.node.name == "ansible_failed_task"]
    assert len(uses) == len(reads) and {n.attr for n in reads} == {"name"}, [n.attr for n in reads]
