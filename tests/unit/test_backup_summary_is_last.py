"""backup-configs.yml's summary, whose assert can fail every host, is its last play, after the key cleanup (I-9).

WHY. The summary play's assert fails each host whose capture failed. When that is every host of the run, Ansible ends
the playbook at that play, and no play after it runs. The history, the prune and the per-run SSH-key cleanup came
after it, so a run whose every capture failed (since I-5, a one-host scheduled backup of a device that answers
nothing) left the run's SSH keys in their per-run directory until the next run or a reboot. SECURITY.md accepts that
residue for a hard-aborted run only. These rows hold the order, and what lets the summary follow the key cleanup: it
connects to nothing. tests/integration/test_backup_failed_run_cleans_up.py runs it for real.
"""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")
FAILERS = {"ansible.builtin.assert", "assert", "ansible.builtin.fail", "fail"}
CONTROLLER_ONLY = {"ansible.builtin.debug", "debug", "ansible.builtin.assert", "assert"}
TASK_KEYS = {"name", "when", "tags", "vars", "no_log", "changed_when"}


def _plays():
    with open(PLAYBOOK, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tasks(play):
    """A play's tasks, pre_tasks, post_tasks and handlers, and the tasks of any block in them."""
    out, todo = [], [t for key in ("pre_tasks", "tasks", "post_tasks", "handlers") for t in play.get(key) or []]
    while todo:
        task = todo.pop(0)
        out.append(task)
        todo += [t for key in ("block", "rescue", "always") for t in task.get(key) or []]
    return out


def _fails_a_failed_capture(play):
    """Whether a play holds a task that can fail a host on `kontroll_backup_failed`."""
    return any("kontroll_backup_failed" in yaml.safe_dump(task) and (set(task) & FAILERS or "failed_when" in task)
               for task in _tasks(play))


def _index(plays, pred):
    found = [i for i, play in enumerate(plays) if pred(play)]
    assert len(found) == 1, found
    return found[0]


def test_the_one_play_that_fails_a_failed_capture_is_the_last_play():
    """Exactly one play can fail a host for a failed capture, and it is the playbook's last, run on every host.
    Guards I-9: anywhere earlier, a run whose every capture failed ends there and skips what follows it."""
    plays = _plays()
    failing = [i for i, play in enumerate(plays) if _fails_a_failed_capture(play)]
    assert failing == [len(plays) - 1], failing
    assert plays[-1].get("hosts") == "all"


def test_the_history_the_prune_and_the_key_cleanup_come_before_the_summary():
    """The snapshot play, the prune play and the `_cleanup-ssh-keys.yml` import all come before the summary. Guards
    I-9's case directly: the key cleanup must run in a run whose every capture failed, and so must the history and
    prune plays that come before it."""
    plays = _plays()
    summary = _index(plays, _fails_a_failed_capture)
    snapshot = _index(plays, lambda p: str(p.get("name", "")).startswith("Snapshot the captures into local history"))
    prune = _index(plays, lambda p: str(p.get("name", "")).startswith("Prune capture history"))
    cleanup = _index(plays, lambda p: "_cleanup-ssh-keys" in str(p.get("ansible.builtin.import_playbook",
                                                                       p.get("import_playbook", ""))))
    assert snapshot < prune < cleanup < summary, (snapshot, prune, cleanup, summary)


def test_the_summary_connects_to_nothing():
    """The summary play runs on the local connection and holds only `debug` and `assert` tasks, which run on the
    controller, with no `delegate_to`. Guards what lets it follow the key cleanup: a task in it that reached a device
    would look for a key the cleanup has removed (MF-4: the cleanup comes after the last fleet connection)."""
    play = _plays()[-1]
    assert play.get("connection") == "local", play
    for task in _tasks(play):
        modules = set(task) - TASK_KEYS
        assert modules and modules <= CONTROLLER_ONLY, task
