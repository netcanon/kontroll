"""backup — the BACKUP instance (#2) of the secondary-capability seam + its suggester pair.

The point of Phase 8 is the SEAM PROOF: backup registers as a pure drop-in and the UNCHANGED capability spine
(capability.propose/promote, promote.plan_token) dispatches to it identically to telemetry. These pin that —
plus the instance's own contract: suggest_backup/declared_backup read the backup vector + block; build_plan is
pure, ADD-idempotent, and fail-closed (a non-preset schedule, a non-backup-capable role, an already-declared
class each refuse); the plan_token gates the written mapping block; and the descriptor's wiring resolves to
real callables. The registry now carries [telemetry, backup] and the generic INVARIANT D* pins auto-cover
backup with no new test code (see tests/unit/test_invariant_d.py — parametrized over registered_capabilities).
"""
import os

import pytest
import yaml

from kontroll import catalog, paths, probe
from kontroll.service import backup, capability, classify, promote

pytestmark = pytest.mark.unit

CAPS = catalog.load_capabilities()


def _write_module(tmp_repo, key, role="cisco_ios", with_backup=False):
    d = tmp_repo / "modules" / key
    d.mkdir(parents=True)
    body = "key: %s\nrole: %s\ncollections:\n  - name: cisco.ios\ninventory_group: core_switch\n" % (key, role)
    if with_backup:
        body += 'backup:\n  capable: true\n  schedule: "0 2 * * *"\n'
    (d / "module.yml").write_text(body, encoding="utf-8")
    return d / "module.yml"


# --- the suggester pair --------------------------------------------------------------------------------- #
def test_suggest_backup_flags_a_capturable_device(vectors, make_facts):
    """suggest_backup on a cliconf device returns a yes/maybe cell + a candidate — the backup vector folded
    into a non-binding hint. Guards the detection wiring for instance #2."""
    s = classify.suggest_backup(make_facts(plugins={"cliconf": ["ios"]}, modules=["ios_command"]), vectors)
    assert s["cell"]["state"] in ("yes", "maybe") and s["candidates"]


def test_declared_backup_reads_the_block(tmp_repo):
    """declared_backup finds an enabled class with a backup-capable `backup:` block for the collection — the
    DECLARED side. Guards a false 'not backed up' (a capable block must register)."""
    _write_module(tmp_repo, "demo_bk", with_backup=True)
    got = classify.declared_backup("cisco.ios", fleet={"enabled_modules": ["demo_bk"]})
    assert got == [{"key": "demo_bk", "methods": ["backup"]}]


# --- the instance write half ---------------------------------------------------------------------------- #
def test_offerable_is_the_single_role_entrypoint_with_a_schedule_allowlist(make_facts):
    """backup offers exactly ONE method (the role-entrypoint capture) whose `schedule` param is a closed
    preset allow-list — the modeling that reuses the generic shell's method+param picker with zero shell edit."""
    methods = backup.offerable_methods(make_facts())
    assert len(methods) == 1 and methods[0]["name"] == "backup"
    assert methods[0]["params"]["schedule"]["required"] and methods[0]["params"]["schedule"]["allowed"]


def test_build_plan_is_pure_and_writes_the_mapping_on_apply(tmp_repo):
    """PROPOSE writes nothing; APPLY appends a `backup:` MAPPING block (capable + schedule) preserving the
    file. Guards the propose/promote boundary + the mapping-shaped write (vs telemetry's list)."""
    p = _write_module(tmp_repo, "demo_bk")
    before = p.read_text(encoding="utf-8")
    plan = backup.build_plan("demo_bk", {"method": "backup", "params": {"schedule": "0 2 * * *"}})
    assert plan["error"] is None and p.read_text(encoding="utf-8") == before   # propose pure
    backup.apply_plan(plan)
    after = p.read_text(encoding="utf-8")
    assert "backup:\n  capable: true\n  schedule:" in after
    assert promote.verify_token(plan["plan_token"], *plan["token_parts"]) is True   # [text_after, severity_decision]


def test_build_plan_fail_closed_paths(tmp_repo):
    """A non-preset schedule, a not-onboarded key, and a role with no backup entrypoint each refuse with a
    distinct error (the route turns each into the right 4xx). Guards a backup schedule being written for an
    unschedulable/misconfigured class. (`already_declared` is no longer a fail path — Phase 4a turned a
    declared block into a reconfigure; see test_reconfigure_modifies_a_knob_in_place.)"""
    _write_module(tmp_repo, "demo_bk")
    assert backup.build_plan("demo_bk", {"params": {"schedule": "* * * * *"}})["error"] == "bad_param"
    assert backup.build_plan("ghost", {"params": {"schedule": "0 2 * * *"}})["error"] == "not_onboarded"
    _write_module(tmp_repo, "demo_norole", role="ghostrole")
    assert backup.build_plan("demo_norole", {"params": {"schedule": "0 2 * * *"}})["error"] == "not_backup_capable"


def test_reconfigure_modifies_a_knob_in_place(tmp_repo):
    """Reconfiguring a DECLARED backup block — changing `retention` keep-all → 90d — is a `modify`: the diff
    carries the one changed param in will_overwrite, severity is `modify`, and apply REPLACES the mapping in
    place (the upsert), keeping the schedule + capable lines. THE crux: a value change to an already-configured
    object is surfaced as a clobber (not silently merged, not refused), so the operator's overwrite-confirm gates
    it. The token folds the severity decision (no-quiet-upgrade)."""
    d = tmp_repo / "modules" / "demo_rc"
    d.mkdir(parents=True)
    p = d / "module.yml"
    p.write_text("key: demo_rc\nrole: cisco_ios\ncollections:\n  - name: cisco.ios\ninventory_group: core_switch\n"
                 'backup:\n  capable: true\n  schedule: "0 2 * * *"\n  retention: keep-all\n  destination: local\n',
                 encoding="utf-8")
    plan = backup.build_plan("demo_rc", {"method": "backup",
                                         "params": {"schedule": "0 2 * * *", "retention": "90d"}})
    assert plan["error"] is None and plan["severity"] == "modify"
    assert plan["will_overwrite"] == ["backup.retention"]
    assert any(c["path"] == "backup.retention" and c["before"] == "keep-all" and c["after"] == "90d"
               for c in plan["changes"])
    out = capability.promote("backup", "demo_rc", {"method": "backup",
                             "params": {"schedule": "0 2 * * *", "retention": "90d"}},
                             plan["plan_token"], caps=CAPS)
    assert out["error"] is None and out["changed"] is True
    after = p.read_text(encoding="utf-8")
    assert "retention: 90d" in after and "retention: keep-all" not in after   # replaced in place
    assert after.count("backup:") == 1 and 'schedule: "0 2 * * *"' in after    # not duplicated; siblings kept


def test_retention_and_destination_are_offered_knobs(make_facts):
    """backup now offers `retention` + `destination` alongside `schedule` — closed allow-list Stage-1 params
    (#123), so the generic param picker renders them with ZERO template edit. Guards the operator-requested
    'levers and knobs': the dialog exposes more than a schedule, and each is a bounded allow-list."""
    m = backup.offerable_methods(make_facts())[0]
    assert "keep-all" in m["params"]["retention"]["allowed"]      # the safe default is offered
    assert m["params"]["destination"]["allowed"] == ["local", "offsite"]   # offsite is OFFERED (then gated)


def test_build_plan_writes_retention_and_destination(tmp_repo):
    """APPLY writes the retention + destination knobs into the `backup:` mapping (defaulting to keep-all/local
    when omitted). Guards that the knobs are RECORDED through propose→promote, not dropped."""
    p = _write_module(tmp_repo, "demo_bk")
    plan = backup.build_plan("demo_bk", {"method": "backup",
                                         "params": {"schedule": "0 2 * * *", "retention": "30d"}})
    assert plan["error"] is None
    backup.apply_plan(plan)
    after = p.read_text(encoding="utf-8")
    assert "retention: 30d" in after and "destination: local" in after   # 30d recorded; destination defaulted


def test_offsite_destination_is_refused_as_deferred(tmp_repo):
    """`destination: offsite` is OFFERED but REFUSED at propose — config captures aren't encrypted at rest, so an
    offsite copy needs the encryption-at-rest story first (SECURITY.md C8). Guards a fail-OPEN where an offsite
    backup of secret-bearing captures ships unencrypted; a bad retention preset is likewise a clean bad_param."""
    _write_module(tmp_repo, "demo_bk")
    off = backup.build_plan("demo_bk", {"params": {"schedule": "0 2 * * *", "destination": "offsite"}})
    assert off["error"] == "bad_param" and "offsite" in off["detail"] and "C8" in off["detail"]
    bad = backup.build_plan("demo_bk", {"params": {"schedule": "0 2 * * *", "retention": "forever"}})
    assert bad["error"] == "bad_param" and "retention" in bad["detail"]


# --- THE SEAM PROOF: the unchanged spine dispatches to backup ------------------------------------------- #
def test_registry_now_carries_both_and_backup_wiring_is_real():
    """registered_capabilities() leads with ['telemetry','backup'] (in order) and the backup descriptor's
    suggester/service references resolve to real callables — backup is a true drop-in with no vapor. (The
    generic D* pins in test_invariant_d auto-cover backup via this same registry, with no new test code — the
    seam acceptance.) The registry GROWS as capabilities are added (logging, order 30, now follows), so this
    asserts the telemetry/backup PREFIX, not exact equality — the drop-in property the seam promises."""
    import importlib
    assert catalog.registered_capabilities()[:2] == ["telemetry", "backup"]
    d = capability.get_descriptor("backup", CAPS)
    mod = importlib.import_module("kontroll.service." + d["suggester"]["module"])
    assert callable(getattr(mod, d["suggester"]["suggest"])) and callable(getattr(mod, d["suggester"]["declared"]))
    svc = importlib.import_module("kontroll.service." + d["service"]["module"])
    assert callable(svc.build_plan) and callable(svc.apply_plan)


def test_unchanged_spine_proposes_and_promotes_backup(tmp_repo):
    """capability.propose/promote — the IDENTICAL functions telemetry uses, with NO edit — drive backup end
    to end: propose returns a token, promote with it writes the block. THIS is the proof the seam generalizes
    (registering backup touched zero spine file)."""
    _write_module(tmp_repo, "demo_bk")
    sel = {"method": "backup", "params": {"schedule": "0 2 * * *"}}
    plan = capability.propose("backup", "demo_bk", sel, caps=CAPS)
    assert plan["error"] is None and plan["plan_token"]
    out = capability.promote("backup", "demo_bk", sel, plan["plan_token"], caps=CAPS)
    assert out["error"] is None and out["changed"] is True
    assert "backup:" in (tmp_repo / "modules" / "demo_bk" / "module.yml").read_text(encoding="utf-8")


def test_suggest_view_offers_backup_for_an_onboarded_class(tmp_repo, monkeypatch, make_facts):
    """capability.suggest_view('backup', key) returns the single offerable backup method + the suggester cell
    — the GUI Stage-0 path for instance #2 through the unchanged spine."""
    _write_module(tmp_repo, "demo_bk")
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None: make_facts(collection=coll, plugins={"cliconf": ["ios"]},
                                                              modules=["ios_command"]))
    view = capability.suggest_view("backup", "demo_bk", caps=CAPS)
    assert view["error"] is None
    assert [m["name"] for m in view["offerable_methods"]] == ["backup"]


@pytest.mark.parametrize("key_line", ["backup:", "backup:      # pick a schedule"], ids=["bare", "with-a-comment"])
def test_a_backup_key_with_nothing_under_it_is_filled_where_it_stands(tmp_repo, key_line):
    """A class whose module file holds the `backup:` key with nothing under it gets its block under THAT key line
    — once, comment kept — and the next change of the block is planned, not refused.

    Guards the planner's first-declare branch: an empty key reads as "not declared", and appending the block as
    text wrote the key a second time; the block writer then refuses the file it was handed by the tool itself."""
    d = tmp_repo / "modules" / "demo_bk"
    d.mkdir(parents=True)
    (d / "module.yml").write_text("key: demo_bk\nrole: cisco_ios\n%s\ninventory_group: core_switch\n" % key_line,
                                  encoding="utf-8")
    plan = backup.build_plan("demo_bk", {"method": "backup", "params": {"schedule": "0 2 * * *"}})
    assert plan["error"] is None, plan.get("detail")
    after = plan["text_after"]
    assert [ln for ln in after.splitlines() if ln.startswith("backup:")] == [key_line]
    read = yaml.safe_load(after)
    assert read["backup"]["schedule"] == "0 2 * * *" and read["inventory_group"] == "core_switch"
    (d / "module.yml").write_text(after, encoding="utf-8")          # what an apply writes
    again = backup.build_plan("demo_bk", {"method": "backup", "params": {"schedule": "0 2 * * *", "retention": "90d"}})
    assert again["error"] is None and yaml.safe_load(again["text_after"])["backup"]["retention"] == "90d"


def test_a_backup_key_with_an_empty_flow_mapping_is_refused(tmp_repo):
    """`backup: {}` is refused at the first declare as the repository's own data, naming the key and the rule.

    Guards the same branch from the other side: a text append beside `backup: {}` is a second top-level key."""
    d = tmp_repo / "modules" / "demo_bk"
    d.mkdir(parents=True)
    (d / "module.yml").write_text("key: demo_bk\nrole: cisco_ios\nbackup: {}\n", encoding="utf-8")
    with pytest.raises(paths.RepositoryDataError) as refused:
        backup.build_plan("demo_bk", {"method": "backup", "params": {"schedule": "0 2 * * *"}})
    assert "`backup`" in str(refused.value) and "own line" in str(refused.value)


def test_a_backup_plan_whose_text_would_not_read_back_is_refused(tmp_repo):
    """Planning a new schedule for a class whose module file writes its `backup` key in quotes raises the
    repository's refusal instead of returning a plan, and the file is as it was.

    Guards the backup planner's read-back: the line writer does not see a quoted key, so it appends a second
    block. YAML's reading of that text IS the planned block — the last of two wins — which is exactly why the
    post-condition also asks for the key once: a file with the key twice is what the next change is refused on."""
    d = tmp_repo / "modules" / "demo_bk"
    d.mkdir(parents=True)
    text = ('key: demo_bk\nrole: cisco_ios\n"backup":\n  capable: true\n  schedule: "0 3 * * *"\n'
            'inventory_group: core_switch\n')
    (d / "module.yml").write_text(text, encoding="utf-8")
    with pytest.raises(paths.RepositoryDataError) as refused:
        backup.build_plan("demo_bk", {"method": "backup", "params": {"schedule": "0 2 * * *"}})
    assert "`backup`" in str(refused.value) and (d / "module.yml").read_text(encoding="utf-8") == text
