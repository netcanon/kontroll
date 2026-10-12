"""gen-backup.py — the backup: block → Semaphore-schedule-spec generator (Capability-track Phase 8).

These pin the generation half of the backup capability instance: a class's `backup:` block becomes a
per-class schedule (limited to its inventory_group), the generator is FAIL-CLOSED (a capable class with no
schedule, or one whose role has no backup entrypoint, exits non-zero rather than emitting a silent/missing
schedule), an incapable declaration is a clean skip, and the committed lockfile matches what the generator
produces (the --check honesty step). Guards a backup schedule silently going missing or a misconfigured class
shipping a broken schedule.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location("gen_backup", os.path.join(ROOT, "scripts", "gen-backup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _gen()


def test_example_fleet_generates_the_cisco_schedule():
    """The shipped example fleet's backup: blocks produce a schedule for cisco_ios, limited to its inventory_group —
    the worked instance #2. Guards the JOIN (enabled module × backup: block × inventory_group) end-to-end. Reads
    instance.example/ by name: the assertion is about a class the EXAMPLE enables, never about an operator's fleet."""
    specs = gen.backup_schedules(gen._load("instance.example/fleet.yml"))
    cisco = next((s for s in specs if s["key"] == "cisco_ios"), None)
    assert cisco and cisco["name"] == "backup-cisco_ios"
    assert cisco["cron"] == "0 2 * * *" and cisco["limit"] == "core_switch"
    assert cisco["playbook"] == "ansible/playbooks/backup-configs.yml"


def test_committed_spec_is_up_to_date():
    """config/semaphore/schedules.generated.yml byte-matches what the generator renders from the shipped example
    fleet — the --check honesty step (a stale committed spec, e.g. after a backup: edit, fails this and the validate
    gate). The committed file is the public, example-derived reference; a node's own schedules are generated on
    that node."""
    specs = gen.backup_schedules(gen._load("instance.example/fleet.yml"))
    committed = open(os.path.join(ROOT, "config", "semaphore", "schedules.generated.yml"),
                     encoding="utf-8").read()
    assert committed == gen.render(specs)


def test_capable_without_schedule_fails_loud(monkeypatch):
    """A class declaring backup.capable:true but no `schedule` exits non-zero — fail-closed: a capable class
    must say WHEN, never default to a silent no-schedule. Guards an unscheduled (so never-running) backup."""
    monkeypatch.setattr(gen, "_module",
                        lambda key: {"role": "cisco_ios", "inventory_group": "g", "backup": {"capable": True}})
    monkeypatch.setattr(gen, "_role_has_backup", lambda role: True)
    with pytest.raises(SystemExit):
        gen.backup_schedules({"enabled_modules": ["x"]})


def test_missing_role_backup_entrypoint_fails_loud(monkeypatch):
    """A capable class whose role has no tasks/backup.yml exits non-zero — fail-closed: scheduling a capture
    that has no per-class capture entrypoint is a misconfiguration caught at generate time, not a broken job."""
    monkeypatch.setattr(gen, "_module", lambda key: {"role": "ghost", "inventory_group": "g",
                                                     "backup": {"capable": True, "schedule": "0 2 * * *"}})
    monkeypatch.setattr(gen, "_role_has_backup", lambda role: False)
    with pytest.raises(SystemExit):
        gen.backup_schedules({"enabled_modules": ["x"]})


def test_incapable_declaration_is_skipped(monkeypatch):
    """A `backup:` block with capable:false yields NO schedule (a declared non-capture) and is NOT an error —
    guards forcing a schedule onto a class that legitimately has nothing meaningful to back up."""
    monkeypatch.setattr(gen, "_module", lambda key: {"role": "r", "backup": {"capable": False}})
    assert gen.backup_schedules({"enabled_modules": ["x"]}) == []


def test_retention_and_destination_are_carried_only_when_declared(monkeypatch):
    """The optional knobs (#123) flow into the schedule spec + rendered YAML ONLY when the `backup:` block
    declares them — so a class without them (every shipped class) keeps a byte-identical spec and the --check
    lockfile stays stable, while a knob-bearing class carries retention/destination through to configure-semaphore."""
    monkeypatch.setattr(gen, "_role_has_backup", lambda role: True)
    monkeypatch.setattr(gen, "_module", lambda key: {"role": "r", "inventory_group": "g",
                                                     "backup": {"capable": True, "schedule": "0 2 * * *",
                                                                "retention": "30d", "destination": "local"}})
    spec = gen.backup_schedules({"enabled_modules": ["x"]})[0]
    assert spec["retention"] == "30d" and spec["destination"] == "local"
    rendered = gen.render([spec])
    assert "retention: 30d" in rendered and "destination: local" in rendered
    # a class WITHOUT the knobs renders no such lines (lockfile stability)
    monkeypatch.setattr(gen, "_module", lambda key: {"role": "r", "inventory_group": "g",
                                                     "backup": {"capable": True, "schedule": "0 2 * * *"}})
    bare = gen.render(gen.backup_schedules({"enabled_modules": ["x"]}))
    assert "retention:" not in bare and "destination:" not in bare
