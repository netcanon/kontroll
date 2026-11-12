"""backup-configs.yml's summary, whose assert can fail every host, is its last play, after the key cleanup (I-9).

WHY. The summary play's assert fails each host whose capture was recorded as failed (a read that answered nothing, an
API answer that is not 200). When that is every host of the run, Ansible ends the playbook at that play, and no play
after it runs. The history, the prune and the per-run SSH-key cleanup came after it, so such a run (since I-5, a
one-host scheduled backup of a device that answers nothing) left the run's SSH keys in their per-run directory.
SECURITY.md accepts that residue for a run that ends before the cleanup on its own (a hard abort; a failing local
play: I-48). A capture that fails as a task is recorded as failed by the capture play's rescue (I-12), so it is
this case too. These rows hold the order, and what lets the summary follow the key cleanup: nothing after the
cleanup connects to anything. ping.yml's last play, which fails a host whose class has no `check` entrypoint
installed, follows its cleanup for the same reason (I-83), so the row on what follows the cleanup reads it too.
tests/integration/test_backup_failed_run_cleans_up.py and tests/integration/test_ping_refused_class_runs.py run them
for real.
"""
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")
FOLLOWED = ["backup-configs.yml", "ping.yml"]   # the playbooks in which plays follow the key cleanup
FAILERS = {"ansible.builtin.assert", "assert", "ansible.builtin.fail", "fail"}
# The actions that run on the controller and open no connection, on any connection (each declares it needs none).
CONTROLLER_ONLY = FAILERS | {"ansible.builtin.debug", "debug", "ansible.builtin.set_fact", "set_fact",
                             "ansible.builtin.meta", "meta"}
TASK_KEYS = {"name", "when", "tags", "vars", "no_log", "changed_when", "failed_when", "register", "loop",
             "loop_control"}
BLOCK_KEYS = {"block", "rescue", "always"}


def _plays(playbook=PLAYBOOK):
    with open(playbook, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tasks(play):
    """A play's tasks, pre_tasks, post_tasks and handlers, and the tasks of any block in them."""
    out, todo = [], [t for key in ("pre_tasks", "tasks", "post_tasks", "handlers") for t in play.get(key) or []]
    while todo:
        task = todo.pop(0)
        out.append(task)
        todo += [t for key in BLOCK_KEYS for t in task.get(key) or []]
    return out


def _fails_a_failed_capture(play):
    """Whether a play holds a task that can fail a host on `kontroll_backup_failed`."""
    return any("kontroll_backup_failed" in yaml.safe_dump(task) and (set(task) & FAILERS or "failed_when" in task)
               for task in _tasks(play))


def _is_cleanup(play):
    return "_cleanup-ssh-keys" in str(play.get("ansible.builtin.import_playbook", play.get("import_playbook", "")))


def _index(plays, pred):
    found = [i for i, play in enumerate(plays) if pred(play)]
    assert len(found) == 1, found
    return found[0]


def test_the_one_play_that_fails_a_failed_capture_is_the_last_play():
    """Exactly one play can fail a host for a failed capture, and it is the playbook's last, run on every host.
    Guards I-9: anywhere earlier, a run whose every capture was recorded as failed ends there and skips what follows
    it."""
    plays = _plays()
    failing = [i for i, play in enumerate(plays) if _fails_a_failed_capture(play)]
    assert failing == [len(plays) - 1], failing
    assert plays[-1].get("hosts") == "all"


def test_the_history_the_prune_and_the_key_cleanup_come_before_the_summary():
    """The snapshot play, the prune play and the `_cleanup-ssh-keys.yml` import all come before the summary. Guards
    I-9's case directly: the key cleanup must run in a run whose every capture was recorded as failed, and so must
    the history and prune plays that come before it."""
    plays = _plays()
    summary = _index(plays, _fails_a_failed_capture)
    snapshot = _index(plays, lambda p: str(p.get("name", "")).startswith("Snapshot the captures into local history"))
    prune = _index(plays, lambda p: str(p.get("name", "")).startswith("Prune capture history"))
    cleanup = _index(plays, _is_cleanup)
    assert snapshot < prune < cleanup < summary, (snapshot, prune, cleanup, summary)


@pytest.mark.parametrize("name", FOLLOWED)
def test_every_play_after_the_key_cleanup_connects_to_nothing(name):
    """In backup-configs.yml and in ping.yml, every play after the `_cleanup-ssh-keys.yml` import gathers no facts (`gather_facts: false`, written: the
    default gathers), runs no role, and holds only controller-side actions (`debug`, `assert`, `fail`, `set_fact`,
    `meta`), in blocks too, with no `delegate_to`. Guards what lets the summary follow the key cleanup: anything after
    it that reached a device would look for a key the cleanup has removed (MF-4: the cleanup comes after the last
    fleet connection). The row this replaces read the last play only, and its task keys: a fleet play between the
    cleanup and the summary, a summary that gathers facts or runs a role, and a device task in a play split off the
    summary each reached a device after the cleanup and passed it (I-9 reader B's M1, M5-M8). The play's
    `connection: local` is not asserted: these actions open no connection on any connection, and a host's
    `ansible_connection` outranks the keyword (reader B's N2 and N3). ping.yml's last play, which fails a host whose
    class has no `check` entrypoint installed, is held to the same (I-83)."""
    plays = _plays(os.path.join(ROOT, "ansible", "playbooks", name))
    after = plays[_index(plays, _is_cleanup) + 1:]
    assert after, "no play follows the key cleanup in %s: its failing play must" % name
    for play in after:
        assert play.get("gather_facts") is False, play
        assert not play.get("roles"), play
        for task in _tasks(play):
            keys = set(task) - TASK_KEYS
            if keys & BLOCK_KEYS:
                assert keys <= BLOCK_KEYS, task
                continue
            assert len(keys) == 1 and keys <= CONTROLLER_ONLY, task
