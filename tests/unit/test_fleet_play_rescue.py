"""install-node-exporter.yml and wire-logging.yml record a failure instead of failing at the play that dispatches, and
fail it after the key cleanup (I-64).

WHY. Both are fleet playbooks: their preamble renders each key-auth host's SSH key into the run's key dir, and their
last import removes it. Ansible runs no later play once every host of a play's batch has failed, so an install or a
wiring that failed on every host, or a wiring request the validation play refused, ended the run before the cleanup,
and the run's keys stayed in its key dir until the next run that completed. The dispatching play now runs its work in
a block whose rescue records the host's failure by the failed task's name, fails a host that reaches it with no failed
task of its own (its role could not be loaded, or another host's failure stopped it; no later play sees it), and ends
the play for the host, as its failure did (a handler it notified does not run); wire-logging.yml's validation play
records a refusal, which the device play reads first, so a refused request targets no host. Plays after the cleanup
fail what was recorded; their one task is a `fail`, which connects to nothing.
tests/integration/test_fleet_play_failure_cleans_up.py runs both playbooks for real.
"""
import os
import re

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(ROOT, "ansible", "playbooks")
# playbook: (the dispatching play's name, the record its rescue sets, the work its block holds, as (module, arg))
DISPATCH = {
    "install-node-exporter.yml": ("Install node_exporter on the metrics-eligible hosts", "kontroll_install_failed",
                                  [("ansible.builtin.gather_facts", None),
                                   ("ansible.builtin.include_role", {"name": "node_exporter"})]),
    "wire-logging.yml": ("Wire the logging method onto the target device class", "kontroll_wire_failed",
                         [("ansible.builtin.include_role", {"name": "{{ role }}"})]),
}
NAMED = "ansible_failed_task.name | default('(unnamed)', true)"
# The device play's and the summary's `hosts:` in wire-logging.yml: a refused request first, then the resolved group.
WIRE_HOSTS = ("{{ '__no_target__' if hostvars['localhost']._wire_refused is defined "
              "else hostvars['localhost']._wire_group | default('__no_target__') }}")
# Keys a play after the cleanup may hold: nothing that skips it or its task, or reaches a device. `run_once` runs its
# tasks for one host (wire-logging.yml's guard, I-88); a task's `tags` may only be `[always]`, which keeps it in a
# `--tags` run.
AFTER_PLAY_KEYS = {"name", "hosts", "gather_facts", "connection", "run_once", "tasks"}
FAILERS = {"ansible.builtin.assert", "assert", "ansible.builtin.fail", "fail"}
CONTROLLER_ONLY = FAILERS | {"ansible.builtin.debug", "debug", "ansible.builtin.set_fact", "set_fact",
                             "ansible.builtin.meta", "meta"}
TASK_KEYS = {"name", "when", "tags", "vars", "no_log", "changed_when", "failed_when", "register", "loop",
             "loop_control"}
BLOCK_KEYS = {"block", "rescue", "always"}


def _plays(name):
    with open(os.path.join(PLAYBOOKS, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _play(name, title):
    found = [p for p in _plays(name) if p.get("name") == title]
    assert len(found) == 1, (name, title)
    return found[0]


def _cleanup_at(plays):
    found = [i for i, p in enumerate(plays) if "_cleanup-ssh-keys" in str(p.get("ansible.builtin.import_playbook"))]
    assert len(found) == 1, found
    return found[0]


def _tasks(play):
    """A play's tasks, pre_tasks, post_tasks and handlers, and the tasks of any block in them."""
    out, todo = [], [t for key in ("pre_tasks", "tasks", "post_tasks", "handlers") for t in play.get(key) or []]
    while todo:
        task = todo.pop(0)
        out.append(task)
        todo += [t for key in BLOCK_KEYS for t in task.get(key) or []]
    return out


def _exprs(value):
    """The Jinja expressions in a templated string, stripped."""
    return [e.strip() for e in re.findall(r"\{\{(.*?)\}\}", " ".join(str(value).split()))]


def _module(task):
    keys = set(task) - TASK_KEYS
    assert len(keys) == 1, task
    return keys.pop()


@pytest.mark.parametrize("name", sorted(DISPATCH))
def test_the_dispatching_play_refuses_first_then_runs_its_work_in_one_rescued_block(name):
    """The dispatching play holds two tasks: the connection refusal's import (first, outside the block, as
    tests/unit/test_group_dispatch_refuses_local.py requires, so a refused host fails there with nothing run on it) and
    one block holding the work, with a rescue and no `always`. Its keys are the ones it had, so no
    `any_errors_fatal`, `max_fail_percentage` or `serial` changes which hosts a failure stops, and `ignore_unreachable`
    is true (a down host is tolerated, not fatal: CLAUDE.md). Guards the block dropped (an install or wiring that fails
    on every host ends the run before the key cleanup again: I-64), the work moved out of the block, the refusal moved
    into it (the rescue would record a refused host, and the scan's first-task rule would no longer hold), an `always`
    that runs work on a failed host, and `ignore_unreachable` turned off (a down host would fail the run, and with
    every host down end it before the cleanup)."""
    title, _record, work = DISPATCH[name]
    play = _play(name, title)
    assert set(play) == {"name", "hosts", "gather_facts", "ignore_unreachable", "tasks"}, play
    assert play["ignore_unreachable"] is True and play["gather_facts"] is False, play
    tasks = play["tasks"]
    assert len(tasks) == 2, tasks
    assert "ansible.builtin.import_tasks" in tasks[0] and "block" not in tasks[0], tasks[0]
    block = tasks[1]
    assert set(block) == {"name", "block", "rescue"}, block
    assert [(_module(t), t[_module(t)]) for t in block["block"]] == work, block["block"]


@pytest.mark.parametrize("name", sorted(DISPATCH))
def test_the_rescue_records_the_task_fails_a_host_with_no_failed_task_and_ends_the_host(name):
    """The rescue is three tasks, in order, each holding its name, its module and (the first two) its `when`, and
    nothing else: a `set_fact` of the playbook's record alone, `when: ansible_failed_task is defined`, whose value's
    one expression is `ansible_failed_task.name | default('(unnamed)', true)`, reading nothing of
    `ansible_failed_result`; an `ansible.builtin.fail` `when: ansible_failed_task is not defined`, whose message names
    both causes its condition cannot tell apart (the role could not be loaded, or another host's failure stopped it);
    and `meta: end_host`. Guards: a record that reads the failed task's result (a role's task may handle what its log
    must not show; the result is printed at that task already); an unnamed task's "" kept by a `default` without its
    `true`; a host with no failed task of its own left to the record alone (it is dropped from every later play, so the
    run would end with rc 0: the I-12 reads' S1); the fail running for a task failure too (the host would end the run
    before the cleanup again); a `failed_when`, `ignore_errors`, `tags` or `loop` that keeps a task from running or
    failing; a message that blames the role for another host's failure (the I-64 reads' S1); and the play going on for
    the host past the rescue, which would run a handler a task notified before it failed, which its failure did not."""
    _title, record, _work = DISPATCH[name]
    rescue = _play(name, DISPATCH[name][0])["tasks"][1]["rescue"]
    assert [_module(t) for t in rescue] == ["ansible.builtin.set_fact", "ansible.builtin.fail",
                                            "ansible.builtin.meta"], rescue
    sets, fails, ends = rescue
    assert set(sets) == {"name", "ansible.builtin.set_fact", "when"}, sets
    assert set(fails) == {"name", "ansible.builtin.fail", "when"}, fails
    assert set(ends) == {"name", "ansible.builtin.meta"}, ends
    assert list(sets["ansible.builtin.set_fact"]) == [record], sets
    assert _exprs(sets["ansible.builtin.set_fact"][record]) == [NAMED], sets
    assert "ansible_failed_result" not in yaml.safe_dump(rescue), rescue
    assert sets["when"] == "ansible_failed_task is defined", sets
    assert fails["when"] == "ansible_failed_task is not defined", fails
    msg = " ".join(str(fails["ansible.builtin.fail"]["msg"]).split())
    assert "could not be loaded" in msg and "another host's failure stopped it" in msg, msg
    assert ends["ansible.builtin.meta"] == "end_host", ends


@pytest.mark.parametrize("name", sorted(DISPATCH))
def test_a_play_after_the_cleanup_fails_each_recorded_host_and_connects_to_nothing(name):
    """After the `_cleanup-ssh-keys.yml` import come only plays that gather no facts, run no role, hold no key but
    their name, `hosts`, `gather_facts`, `connection` and `tasks`, and hold only controller-side actions (`debug`,
    `assert`, `fail`, `set_fact`, `meta`) with no `delegate_to`, `failed_when`, `ignore_errors` or `loop`, and no
    `tags` but `[always]` (I-88's guard; it keeps the task in a `--tags` run, never skips it); one
    of them fails a host on the playbook's record, over the dispatching play's own `hosts:` pattern, with one task: its
    name, the `fail` and its `when`. Guards the summary moved before the cleanup (a run whose every host failed ends
    before the cleanup again), dropped or kept from failing (a failed install or wiring would end the run green), run
    over another pattern (a recorded host it does not reach is never failed), or joined by a play that reaches a device
    after its key is gone (MF-4)."""
    title, record, _work = DISPATCH[name]
    plays = _plays(name)
    after = plays[_cleanup_at(plays) + 1:]
    assert after, "no play follows the key cleanup"
    for play in after:
        assert play.get("gather_facts") is False and set(play) <= AFTER_PLAY_KEYS, play
        for task in _tasks(play):
            assert not set(task) & {"failed_when", "ignore_errors", "loop", "loop_control"}, task
            assert task.get("tags", ["always"]) == ["always"], task
            keys = set(task) - TASK_KEYS
            if keys & BLOCK_KEYS:
                assert keys <= BLOCK_KEYS, task
                continue
            assert len(keys) == 1 and keys <= CONTROLLER_ONLY, task
    summaries = [p for p in after if any(record in yaml.safe_dump(t) and _module(t) in FAILERS for t in _tasks(p))]
    assert len(summaries) == 1, summaries
    assert summaries[0]["hosts"] == _play(name, title)["hosts"], summaries[0]
    (task,) = summaries[0]["tasks"]
    assert set(task) == {"name", "ansible.builtin.fail", "when"}, task
    assert task["when"] == "%s is defined" % record and record in str(task["ansible.builtin.fail"]["msg"]), task


def test_the_validation_play_records_a_refusal_which_the_device_play_reads_first():
    """wire-logging.yml's validation play holds one block, every validation task inside it, whose rescue is one
    `set_fact` of `_wire_refused` alone, whose one expression names the failed task as the dispatch rescues do; the
    device play's `hosts:` reads `_wire_refused` first (no host) and only then `_wire_group`, and the summary's is the
    same (the summary test holds that); a localhost play after the cleanup fails on `_wire_refused`. Guards a refused
    request (no role, a role with no tasks/main.yml, no logging method whose wiring role is the requested role naming
    its connections, a target with no module) ending the run at the validation play, before the key cleanup (I-64); a
    record that reads the failed task's result; a device play that a `_wire_group` from an `-e` or a vars file
    dispatches after a refusal (an extra var outranks any `set_fact` retarget: the I-64 reads' N1); and a refusal
    recorded but never failed, so the run would end green."""
    play = _play("wire-logging.yml", "Validate the wiring request + resolve the target group and collector "
                                     "(control-side, no device contact)")
    (block,) = play["tasks"]
    assert set(block) == {"name", "block", "rescue"}, block
    names = [t["name"] for t in block["block"]]
    assert names[0] == "Require the role + target extra-vars" and \
        names[-1] == "Resolve the target class's inventory group + the collector host", names
    (rescue,) = block["rescue"]
    sets = rescue["ansible.builtin.set_fact"]
    assert set(rescue) == {"name", "ansible.builtin.set_fact"} and set(sets) == {"_wire_refused"}, rescue
    assert _exprs(sets["_wire_refused"]) == [NAMED] and "ansible_failed_result" not in yaml.safe_dump(rescue), sets
    device = _play("wire-logging.yml", DISPATCH["wire-logging.yml"][0])
    assert " ".join(device["hosts"].split()) == WIRE_HOSTS, device["hosts"]
    plays = _plays("wire-logging.yml")
    after = plays[_cleanup_at(plays) + 1:]
    refused = [p for p in after if p["hosts"] == "localhost"]
    assert len(refused) == 1, after
    (task,) = refused[0]["tasks"]
    assert task["when"] == "_wire_refused is defined" and "_wire_refused" in task["ansible.builtin.fail"]["msg"], task


def test_a_wiring_request_no_validation_play_read_fails_the_run_after_the_cleanup():
    """wire-logging.yml holds, after the key cleanup, one play over `all` that runs once with one task: a `fail` whose
    `when` is that neither `_wire_group` nor `_wire_refused` is defined on localhost (the validation play sets one or
    the other), tagged `always`, whose message names `--limit <host>:localhost`; and the header's usage line names that
    limit. Guards I-88: under a `--limit` that leaves localhost out the validation play does not run, so the device
    play matches no host, and the run wired nothing and passed; over `all`, the guard runs under that limit, and once
    (one failure, not one per host); without `always`, a `--tags` run that skips the validation's tasks would skip it
    too. tests/integration/test_fleet_play_failure_cleans_up.py runs it."""
    plays = _plays("wire-logging.yml")
    after = plays[_cleanup_at(plays) + 1:]
    guards = [p for p in after if "_wire_group is not defined" in yaml.safe_dump(p)]
    assert len(guards) == 1, after
    (guard,) = guards
    assert guard["hosts"] == "all" and guard["run_once"] is True, guard
    (task,) = guard["tasks"]
    assert set(task) == {"name", "ansible.builtin.fail", "when", "tags"} and task["tags"] == ["always"], task
    assert task["when"] == ["hostvars['localhost']._wire_group is not defined",
                            "hostvars['localhost']._wire_refused is not defined"], task
    assert "--limit <host>:localhost" in " ".join(task["ansible.builtin.fail"]["msg"].split()), task
    with open(os.path.join(PLAYBOOKS, "wire-logging.yml"), encoding="utf-8") as fh:
        assert "add --limit <host>:localhost" in fh.read()
