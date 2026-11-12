"""A fleet playbook's controller plays (backup-configs.yml's capture dir check, history commit and prune among them)
run under any `--limit`, once, on the controller (I-90).

WHY. Every Semaphore backup schedule runs backup-configs.yml `--limit <inventory_group>` (scripts/gen-backup.py). A
`--limit` that does not name localhost leaves the implicit localhost out, so backup-configs.yml's three plays, while
`hosts: localhost`, were skipped on every scheduled run: no capture was committed to the history, no prune ran, the
capture dir was never checked, and the run passed. They now start from `localhost:all` (localhost first, so an
unlimited run runs them there, as before), run once under the linear strategy, and delegate every task to localhost,
so under a limit they run for the first limited host on the controller, with its connection (a host's own
`ansible_connection` outranks `connection: local`: I-41). The rule reads every play of every fleet playbook (each one
that imports `_fleet-preamble.yml`, which a caller may limit) whose `hosts` names localhost or the IPv4 loopback name,
as a string (split at `:` and `,`, a `&` or `!` prefix dropped) or a list; a templated `hosts` is a device play's.
wire-logging.yml's two localhost plays are the named exceptions: its I-88 guard fails a limited run that skips them.
tests/integration/test_backup_limited_run.py runs backup-configs.yml's plays under each limit.
"""
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(ROOT, "ansible", "playbooks")
LOCAL = ("localhost", "127.0.0.1")
# backup-configs.yml's controller plays: the rule must find each (it would pass vacuously over a renamed one).
BACKUP = ("Ensure backup dir exists", "Snapshot the captures into local history (git)",
          "Prune capture history older than the retention window (git)")
# Plays that stay `hosts: localhost`: wire-logging.yml's validation (later plays read its facts through
# `hostvars['localhost']`) and its refused-request failure; its guard after the key cleanup fails a limited run that
# skips them (I-88).
EXCEPT = {("wire-logging.yml", "Validate the wiring request + resolve the target group and collector "
                               "(control-side, no device contact)"),
          ("wire-logging.yml", "Fail a refused wiring request")}


def _load(name):
    with open(os.path.join(PLAYBOOKS, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _fleet_playbooks():
    """Every playbook under ansible/playbooks/ (top level) that imports `_fleet-preamble.yml`."""
    out = []
    for name in sorted(os.listdir(PLAYBOOKS)):
        if name.endswith(".yml") and name != "_fleet-preamble.yml":
            doc = _load(name)
            if isinstance(doc, list) and any(isinstance(e, dict) and "_fleet-preamble.yml" in str(
                    e.get("ansible.builtin.import_playbook", e.get("import_playbook", ""))) for e in doc):
                out.append(name)
    return out


def _names_localhost(hosts):
    """Whether a play's `hosts` names localhost or the IPv4 loopback name: a list holding one, or a string not
    templated whose `:`/`,`-separated patterns (a `&` or `!` prefix dropped) hold one."""
    if isinstance(hosts, list):
        return any(str(h).strip() in LOCAL for h in hosts)
    if not isinstance(hosts, str) or "{{" in hosts or "{%" in hosts:
        return False
    return any(p.strip().lstrip("&!") in LOCAL for p in re.split(r"[:,]", hosts))


def _tasks(tasks):
    """Every task, the tasks of each block, rescue and always included."""
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _tasks(task.get(key))


def _controller_plays(name):
    return [p for p in _load(name) if isinstance(p, dict) and "hosts" in p and _names_localhost(p["hosts"])]


def test_the_rule_reads_the_fleet_playbooks_and_finds_the_backup_plays():
    """The fleet playbooks the rule reads include backup-configs.yml, ping.yml, install-node-exporter.yml and
    wire-logging.yml, backup-configs.yml's three controller plays are among the plays it selects, and each named
    exception is still a `hosts: localhost` play. Guards a rule that passes vacuously: a preamble import renamed, a
    controller play retitled, or an exception left behind by a play that changed."""
    fleet = _fleet_playbooks()
    assert {"backup-configs.yml", "ping.yml", "install-node-exporter.yml", "wire-logging.yml"} <= set(fleet), fleet
    titles = [p.get("name") for p in _controller_plays("backup-configs.yml")]
    assert set(BACKUP) <= set(titles), titles
    for name, title in EXCEPT:
        assert [p["hosts"] for p in _load(name) if p.get("name") == title] == ["localhost"], (name, title)


@pytest.mark.parametrize("name", _fleet_playbooks())
def test_each_controller_play_runs_under_any_limit_once_on_the_controller(name):
    """Every play of a fleet playbook whose `hosts` names localhost (named exceptions aside) is `localhost:all`, runs
    once under the linear strategy, and every task it holds (in a block or not) delegates to localhost. Guards I-90:
    a `hosts: localhost` play (or `[localhost]`, or `all:&localhost`) is skipped under a `--limit` that leaves localhost
    out (every schedule's); `all:localhost` would run an unlimited run's tasks for the first device rather than on
    localhost; a play without `run_once` would run each task once per limited host, and so would the free strategy,
    which does not honour `run_once` (a caller's `ANSIBLE_STRATEGY`; the play's keyword outranks it: the I-88/I-90
    reads' S1); a task without the delegation would take the first host's own connection (its `ansible_connection`
    outranks the play's `connection: local`)."""
    for play in _controller_plays(name):
        if (name, play.get("name")) in EXCEPT:
            continue
        assert play["hosts"] == "localhost:all" and play.get("run_once") is True, play
        assert play.get("strategy") == "linear", play
        tasks = list(_tasks(play.get("tasks")))
        assert tasks and all(t.get("delegate_to") == "localhost" for t in tasks if "block" not in t), tasks
        assert not set(play) & {"pre_tasks", "post_tasks", "roles", "handlers"}, play
