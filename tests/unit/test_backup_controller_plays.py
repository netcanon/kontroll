"""backup-configs.yml's controller plays (the capture dir's check, the history commit, the prune) run under any
`--limit`, on the controller (I-90).

WHY. Every Semaphore backup schedule runs backup-configs.yml `--limit <inventory_group>` (scripts/gen-backup.py). A
`--limit` that does not name localhost leaves the implicit localhost out, so the three plays, while `hosts: localhost`,
were skipped on every scheduled run: no capture was committed to the history, no prune ran, the capture dir was never
checked, and the run passed. They now start from `localhost:all` (localhost first, so an unlimited run runs them there,
as before), run once, and delegate every task to localhost, so under a limit they run for the first limited host on
the controller, with its connection (a host's own `ansible_connection` outranks `connection: local`: I-41).
tests/integration/test_backup_limited_run.py runs them under each limit.
"""
import os

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")
CONTROLLER = ("Ensure backup dir exists", "Snapshot the captures into local history (git)",
              "Prune capture history older than the retention window (git)")


def _plays():
    with open(PLAYBOOK, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tasks(tasks):
    """Every task, the tasks of each block, rescue and always included."""
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _tasks(task.get(key))


@pytest.mark.parametrize("title", CONTROLLER)
def test_each_controller_play_runs_under_any_limit_on_the_controller(title):
    """Each controller play's `hosts` is `localhost:all`, it runs once, and every task it holds (in a block or not)
    delegates to localhost. Guards I-90: a `hosts: localhost` play is skipped under a `--limit` that leaves localhost
    out (every schedule's), `all:localhost` would run an unlimited run's tasks for the first device rather than on
    localhost, a play without run_once would run each task once per limited host, and a task without the delegation
    would take the first host's own connection (its `ansible_connection` outranks the play's `connection: local`)."""
    (play,) = [p for p in _plays() if p.get("name") == title]
    assert play["hosts"] == "localhost:all" and play.get("run_once") is True, play
    tasks = list(_tasks(play["tasks"]))
    assert tasks and all(t.get("delegate_to") == "localhost" for t in tasks if "block" not in t), tasks


def test_no_play_runs_on_localhost_alone():
    """No play of backup-configs.yml is `hosts: localhost` (or 127.0.0.1) alone. Guards a controller play added later
    in the old shape, which every schedule's `--limit` would skip, its run passing (I-90)."""
    alone = [p.get("name") for p in _plays() if str(p.get("hosts", "")).strip() in ("localhost", "127.0.0.1")]
    assert not alone, alone
