"""backup-configs.yml's capture play records a capture that fails as a task as a failed capture (I-12).

WHY. A capture that fails as a task (a read that exits non-zero or errors, a module or permission error, the API
credential gate's assert, a write that fails) failed its host at the capture play: the summary printed no line for
it, and when every host failed so, Ansible ended the run there, before the capture history, the prune and the per-run
SSH-key cleanup, so the run's key-auth hosts' keys stayed on disk (the I-9 reads' S1). The capture play's dispatch is
now a block whose rescue records the host as a failed capture, naming the task it stopped at, so the host goes on to
the cleanup, and the summary names it and fails the run. An include that cannot be loaded is no task failure: the
rescue's second task fails that host, so the run fails (the I-12 reads' S1). The play sets `any_errors_fatal: false`,
so a caller's setting cannot drop a host whose read answered (the reads' O1). The block's first task refuses a class
whose backup entrypoint is not installed (no `device_role`, no such role, a role with no tasks/backup.yml) as a task
the rescue records, where the include's load failure had dropped the host or ended the run before the key cleanup
(I-66).
tests/integration/test_backup_failed_run_cleans_up.py runs it for real.
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


def _parse(text):
    return jinja2.Environment().parse(text)


def test_the_capture_play_sets_any_errors_fatal_false_and_no_other_play_keyword():
    """The capture play's keys are exactly its name, `hosts: all`, `gather_facts: false`, `ignore_unreachable: true`,
    `any_errors_fatal: false`, its vars (only `kontroll_via_jump`, which tells its probes a host behind a jump host:
    I-65; a play var outranks a host's own, so any other would rebind it for every class) and its tasks. Guards
    I-12's rescue where a pin of the tasks cannot see: a caller's `any_errors_fatal` (ANSIBLE_ANY_ERRORS_FATAL, an
    ansible.cfg) failed every other host once one host's capture failed, rescue or not, so a host whose read had
    answered was dropped from every later play, uncaptured and with no summary line (the I-12 reads' O1); the play's
    `false` overrides it, and `true` there would do that harm itself. `max_fail_percentage` (beside a refused host,
    which fails outside the rescue, it would fail every host before the key cleanup) and `serial` are refused with it
    (the I-12 read B's N4)."""
    play = _capture_play()
    keys = {"name", "hosts", "gather_facts", "ignore_unreachable", "any_errors_fatal", "vars", "tasks"}
    assert set(play) == keys, sorted(play)
    assert set(play["vars"]) == {"kontroll_via_jump"}, play["vars"]
    assert play["hosts"] == "all" and play["gather_facts"] is False and play["ignore_unreachable"] is True, play
    assert play["any_errors_fatal"] is False, play


def test_the_capture_dispatch_is_rescued_and_the_rescue_records_a_failed_capture():
    """The capture play's tasks are the `device_role` refusal's import, then one block, then the port probe of a host
    its class recorded unreachable and its two records, each outside the block (I-65; test_backup_missed_runs.py runs
    them), and nothing else: the block holds the entrypoint check's import
    (test_the_entrypoint_check_comes_first_and_checks_the_included_entry) then the include_role of `{{ device_role }}`
    with `tasks_from: backup`, and carries no other keyword (no `always`, `when`, `ignore_errors`); its rescue's first
    task is a `set_fact` of `kontroll_backup_failed: true` and a `kontroll_backup_result` that starts "FAILED:" and
    reads nothing but `ansible_failed_task.name`, whose default "(unnamed)" applies to an empty name too (`default(...,
    true)`: an unnamed task's name is "", which a plain default leaves; the I-12 reads' N2/N6). Guards I-12: the
    dispatch outside a block failed its host at the capture play, so the summary had no line for it, and when every host
    failed so the run ended before the key cleanup; a rescue that sets no `kontroll_backup_failed` would let the run
    pass on a capture that failed; and a message that read `ansible_failed_result` (or anything of the failed task but
    its name) would print the read's answer, which its `no_log` hides, in the summary's line, which is not `no_log`. The
    refusal stays outside the block, so a refused host fails as before (test_group_dispatch_refuses_local.py holds it
    first)."""
    tasks = _capture_play()["tasks"]
    assert len(tasks) == 5, tasks
    assert [t["name"] for t in tasks[2:]] == [
        "Probe the port of a host its class recorded as not reached",
        "Record a host that took no connection as not run (the run passes; the summary names it)",
        "Record a host that answered and opened no session as failed (any earlier capture is kept)"], tasks[2:]
    assert not any(set(t) & {"block", "rescue", "always"} for t in tasks[2:]), tasks[2:]
    assert tasks[0].get("ansible.builtin.import_tasks") == "tasks/refuse-device-role-fact.yml", tasks[0]
    block = tasks[1]
    assert set(block) == {"name", "block", "rescue"}, block
    assert len(block["block"]) == 2, block["block"]
    include = block["block"][1]
    assert set(include) == {"name", "ansible.builtin.include_role"}, include
    assert include["ansible.builtin.include_role"] == {"name": "{{ device_role }}", "tasks_from": "backup"}
    assert len(block["rescue"]) == 2, block["rescue"]
    rescue = block["rescue"][0]
    assert set(rescue) == {"name", "ansible.builtin.set_fact"}, rescue
    facts = rescue["ansible.builtin.set_fact"]
    assert set(facts) == {"kontroll_backup_result", "kontroll_backup_failed"}, facts
    assert facts["kontroll_backup_failed"] is True
    assert facts["kontroll_backup_result"].startswith("FAILED:"), facts
    tree = _parse(facts["kontroll_backup_result"])
    assert jinja2.meta.find_undeclared_variables(tree) == {"ansible_failed_task"}
    uses = [n for n in tree.find_all(jinja2.nodes.Name) if n.name == "ansible_failed_task"]
    reads = [n for n in tree.find_all(jinja2.nodes.Getattr) if isinstance(n.node, jinja2.nodes.Name)
             and n.node.name == "ansible_failed_task"]
    assert len(uses) == len(reads) and {n.attr for n in reads} == {"name"}, [n.attr for n in reads]
    defaults = [f for f in tree.find_all(jinja2.nodes.Filter) if f.name == "default"]
    assert len(defaults) == 1 and [a.value for a in defaults[0].args] == ["(unnamed)", True], defaults


CHECK = os.path.join(ROOT, "ansible", "playbooks", "tasks", "refuse-missing-entrypoint.yml")


def test_the_entrypoint_check_comes_first_and_checks_the_included_entry():
    """The capture block's first task is a static import of tasks/refuse-missing-entrypoint.yml whose only keywords are
    its name and `vars`, and those vars are exactly `kontroll_entrypoint` set to the include_role's `tasks_from`
    (`backup`); the file is one `ansible.builtin.assert`, `quiet`, with no keyword that would let a refused host on or
    run it elsewhere (no `when`, `ignore_errors`, `failed_when`, `delegate_to`, `tags`, `loop`, `check_mode`), whose
    first conditions are that `device_role` is defined, a string and not empty. Guards I-66: outside the block a
    refusal would fail its host before the rescue (the run's keys stay where every host is refused, as before); after
    the include it would come too late (the include's load failure is no task failure); an entrypoint name other than
    the include's would check a file the play never loads; and a refusal that a keyword skipped or softened would let
    the include's load failure end the run again. tests/integration/test_refuse_missing_entrypoint_runs.py runs the
    file beside a real include_role."""
    block = _capture_play()["tasks"][1]["block"]
    check, include = block[0], block[1]
    assert set(check) == {"name", "ansible.builtin.import_tasks", "vars"}, check
    assert check["ansible.builtin.import_tasks"] == "tasks/refuse-missing-entrypoint.yml", check
    assert check["vars"] == {"kontroll_entrypoint": include["ansible.builtin.include_role"]["tasks_from"]}, check
    with open(CHECK, encoding="utf-8") as fh:
        tasks = yaml.safe_load(fh)
    assert len(tasks) == 1, tasks
    task = tasks[0]
    assert set(task) == {"name", "ansible.builtin.assert", "vars"}, sorted(task)
    args = task["ansible.builtin.assert"]
    assert set(args) == {"that", "fail_msg", "quiet"} and args["quiet"] is True, args
    assert args["that"][:3] == ["device_role is defined", "device_role is string", "device_role | length > 0"], args


def test_a_host_whose_include_could_not_be_loaded_is_failed_in_the_rescue():
    """The rescue's second task is an `ansible.builtin.fail` that runs only `when: ansible_failed_task is not
    defined`, and its message reads nothing but `inventory_hostname` and `device_role`. Guards the I-12 reads' S1: an
    include that cannot be loaded (a file or role the class's backup includes that is missing, a class in collection
    form whose collection is not installed; the entrypoint check refuses a class that is not installed as a task first,
    I-66) is no task failure: Ansible moves the host into the rescue with no failed task and drops
    it from every later play, and with the record alone the run's iterator no longer counted it failed, so a run of
    that host alone ended with rc 0; a task failure has a failed task, so the record alone runs for it.
    tests/integration/test_backup_failed_run_cleans_up.py runs both for real."""
    rescue = _capture_play()["tasks"][1]["rescue"]
    assert len(rescue) == 2, rescue
    fail = rescue[1]
    assert set(fail) == {"name", "ansible.builtin.fail", "when"}, fail
    assert fail["when"] == "ansible_failed_task is not defined", fail
    assert set(fail["ansible.builtin.fail"]) == {"msg"}, fail
    names = jinja2.meta.find_undeclared_variables(_parse(fail["ansible.builtin.fail"]["msg"]))
    assert names == {"inventory_hostname", "device_role"}, names
