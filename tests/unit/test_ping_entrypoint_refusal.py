"""ping.yml refuses a host whose class has no `check` entrypoint installed before its probe, records it, names it
REFUSED and fails it in its last play, after the key cleanup (I-83).

WHY. The reach play includes `{{ device_role }}` with `tasks_from: check` in the probe's block. An include that cannot
be loaded is no task failure: Ansible dropped the host from every later play, or ended the whole run, so where that was
every host the run ended before `_cleanup-ssh-keys.yml` and left the run's keys on disk. The probe's rescue would name a
refused host a failed check, which a dry run passes (before I-84 it said UNREACHABLE, and the run passed), so the
refusal has a block of its own, and the run's failure comes after the cleanup (the lesson of I-9, where
backup-configs.yml's failing summary came before it). These rows hold that shape;
tests/integration/test_ping_refused_class_runs.py runs it, and tests/unit/test_backup_summary_is_last.py holds that the
play after the cleanup connects to nothing.
"""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "ping.yml")
RECORDED = "kontroll_class_refused"


def _plays():
    with open(PLAYBOOK, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _reach(plays):
    """The play that dispatches by the host's class: the one whose block includes `{{ device_role }}`."""
    found = [play for play in plays if "tasks" in play and "include_role" in yaml.safe_dump(play["tasks"])]
    assert len(found) == 1, found
    return found[0]


def test_the_reach_play_refuses_a_missing_check_entrypoint_before_its_probe():
    """The reach play's tasks are, in order: the `device_role` fact refusal; a block holding only the import of
    tasks/refuse-missing-entrypoint.yml with `kontroll_entrypoint` set to the include's `tasks_from` (`check`), whose
    rescue only sets `kontroll_class_refused: true`; and the probe's block, whose `when` skips a host so recorded and
    which holds the include. Guards the refusal moved after the probe or into its block (the probe's rescue would
    name a refused host a failed check, which a dry run passes); an entrypoint other than the one the include runs; a
    rescue that records nothing, or a probe block with no such `when` (a refused host then reaches the include, which
    cannot be loaded: the host is dropped from every later play, or the run ends, before the key cleanup)."""
    tasks = _reach(_plays())["tasks"]
    assert len(tasks) == 3, tasks
    assert tasks[0].get("ansible.builtin.import_tasks") == "tasks/refuse-device-role-fact.yml", tasks[0]
    refusal, probe = tasks[1], tasks[2]
    assert set(refusal) == {"name", "block", "rescue"}, refusal
    assert len(refusal["block"]) == 1 and set(refusal["block"][0]) == {"name", "ansible.builtin.import_tasks", "vars"}
    include = [t for t in probe["block"] if "ansible.builtin.include_role" in t]
    assert len(include) == 1, probe
    entry = include[0]["ansible.builtin.include_role"]
    assert entry == {"name": "{{ device_role }}", "tasks_from": "check"}, entry
    assert refusal["block"][0]["ansible.builtin.import_tasks"] == "tasks/refuse-missing-entrypoint.yml", refusal
    assert refusal["block"][0]["vars"] == {"kontroll_entrypoint": entry["tasks_from"]}, refusal
    assert refusal["rescue"] == [{"name": refusal["rescue"][0]["name"],
                                  "ansible.builtin.set_fact": {RECORDED: True}}], refusal
    assert set(probe) == {"name", "when", "block", "rescue"}, probe
    assert probe["when"] == "not (%s | default(false))" % RECORDED, probe


def test_the_run_fails_a_refused_host_in_its_last_play_after_the_key_cleanup():
    """ping.yml ends with the summary play, whose line names a recorded host REFUSED before it reads
    `kontroll_reachable`; then the `_cleanup-ssh-keys.yml` import; then one play of one task, an `assert` whose first
    condition is that `kontroll_class_refused` is not set, its message the host's summary line. Guards a failing play
    before the cleanup (where every host is refused it would end the run there and leave the run's keys: I-9's
    lesson), a last play that does not fail a refused host (the run would pass with a class that checks nothing) or
    reads another var, and a summary that names a refused host OK, CHECK FAILED or UNREACHABLE.
    tests/unit/test_ping_check_failed.py holds the play's other conditions (I-84)."""
    plays = _plays()
    summary, cleanup, last = plays[-3:]
    line = summary["tasks"][0]["ansible.builtin.set_fact"]["kontroll_ping_line"]
    assert line.index("REFUSED") < line.index(RECORDED) < line.index("kontroll_reachable"), line
    assert cleanup.get("ansible.builtin.import_playbook") == "_cleanup-ssh-keys.yml", cleanup
    assert len(last["tasks"]) == 1, last
    check = last["tasks"][0]["ansible.builtin.assert"]
    assert check["that"][0] == "not (%s | default(false))" % RECORDED, check
    assert "kontroll_ping_line" in check["fail_msg"], check


def test_the_probes_rescue_fails_a_host_whose_check_could_not_be_loaded():
    """The probe's rescue marks the host unreachable (recording whether its check failed: I-84), then fails it with
    `ansible.builtin.fail` only when `ansible_failed_task is not defined`, its message naming the host and its class.
    Guards the residue the entrypoint check leaves (an installed check that includes a missing file; a class in
    collection form whose collection is not installed): its include is no task failure, so the host reached the rescue
    with no failed task, was marked unreachable and left every later play, and where it was the only host the run ended
    with rc 0. A `fail` with no such `when` would fail every unreachable host and the run with it."""
    rescue = _reach(_plays())["tasks"][2]["rescue"]
    assert [list(t)[1] for t in rescue] == ["ansible.builtin.set_fact", "ansible.builtin.fail"], rescue
    assert rescue[0]["ansible.builtin.set_fact"]["kontroll_reachable"] is False, rescue
    assert set(rescue[1]) == {"name", "ansible.builtin.fail", "when"}, rescue
    assert rescue[1]["when"] == "ansible_failed_task is not defined", rescue
    message = rescue[1]["ansible.builtin.fail"]["msg"]
    assert "{{ inventory_hostname }}" in message and "{{ device_role" in message, message


def test_the_reach_play_overrides_a_callers_any_errors_fatal():
    """The reach play sets `any_errors_fatal: false`, as backup-configs.yml's capture play does (I-12). Guards the
    I-83 reads' B S3 (A N4): under a caller's any_errors_fatal (ANSIBLE_ANY_ERRORS_FATAL, an ansible.cfg) one host's
    failed probe or refusal failed every other host, rescue or not: beside a down host, a reachable one was moved into
    the probe's rescue, whose `fail` ended the run before the key cleanup and left every key; beside a refused host, a
    reachable one was dropped with no summary line. The play only reads, so it does not honour the setting;
    tests/integration/test_ping_refused_class_runs.py runs it under ANSIBLE_ANY_ERRORS_FATAL."""
    assert _reach(_plays()).get("any_errors_fatal") is False
