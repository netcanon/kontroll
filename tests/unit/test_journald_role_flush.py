"""roles/logging_journald_remote flushes the journal to /var/log/journal after it restarts journald (I-25).

WHY: deploy-stack binds Vector's host-journal read to /var/log/journal only where the host has that directory, and the
role's README says to re-run deploy-stack once the role has first made the journal persistent. The role writes
`Storage=persistent` and restarts journald. A restarted journald opens /var only on a flush request, and since
systemd 252 systemd-journal-flush.service no longer restarts with journald (it `Wants=` journald, where v247 and v249
`Requires=` it), so the directory appeared only at the next boot. A deploy run in between bound the volatile journal,
which that boot's flush then emptied: Vector read nothing until the next deploy (the I-25 reads, A S3 / B S3). These
rows read the role's own files; no journald runs here.
"""
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROLE = os.path.join(ROOT, "ansible", "roles", "logging_journald_remote")

pytestmark = pytest.mark.unit


def _load(rel):
    with open(os.path.join(ROLE, rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_the_persistence_change_restarts_journald_and_then_flushes_it():
    """The task that sets `Storage=persistent` notifies both the restart and the flush; the handlers file defines the
    flush (`journalctl --flush`, with become) after the restart, so Ansible runs it second, since handlers run in the
    order the file defines them. Guards the flush left out or notified by no task (the directory then waits for the
    next boot, and a deploy run before it binds the volatile journal), and a flush run before the restart, which the
    old journald would answer under its old setting while the new one, started without a flush request, writes /run
    again."""
    persist = [t for t in _load("tasks/main.yml")
               if (t.get("ansible.builtin.lineinfile") or {}).get("line") == "Storage=persistent"]
    assert len(persist) == 1, persist
    notify = persist[0].get("notify")
    notify = [notify] if isinstance(notify, str) else list(notify or [])
    handlers = _load("handlers/main.yml")
    names = [h.get("name") for h in handlers]
    flush = [i for i, h in enumerate(handlers) if str(h.get("ansible.builtin.command", "")).split() ==
             ["journalctl", "--flush"]]
    assert len(flush) == 1, names
    assert flush[0] > names.index("Restart systemd-journald"), names
    assert handlers[flush[0]].get("become") is True, handlers[flush[0]]
    assert sorted(notify) == sorted(["Restart systemd-journald", names[flush[0]]]), notify
