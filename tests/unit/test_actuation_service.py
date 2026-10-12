"""The actuation unit PREVIEW (R4): build_actuation_plan renders what a configured unit WOULD run, write-free.

WHY (the failures these guard):
  * the Review stage must show the operator EXACTLY what will run before anything is staged (design 24 §5) — a
    role unit's wrapper play (hosts:+roles:), or a collection-playbook/standalone run-spec, each with the derived
    access-chain header carrying the blast radius. A wrong/empty render would let an operator authorize a stage
    blind.
  * build_actuation_plan must FAIL CLOSED on bad input: an absent unit → no_unit (404), values that don't
    validate → invalid (422) — it must never render a play from un-validated values.
  * the preview is a READ: it must never gain a write/stage path (the read-only-by-construction contract; the
    staged write is R5). The AST pin asserts the whole module renders + reads only.
  * the enact hand-off must sequence the --check Dry Run BEFORE the apply (CLAUDE.md --check-first hard rule).
"""
import pytest

from _readonly_pins import WRITE_VERBS, assert_read_only
from kontroll import catalog, paths
from kontroll.service.actuation import apply_actuation_plan, build_actuation_plan, stage_plan

pytestmark = pytest.mark.unit

ROLE_UNIT = {
    "key": "backup-ios",
    "unit": {"kind": "role", "collection": "cisco.ios", "name": "ios_config"},
    "target": {"device_class": "cisco_ios", "inventory_group": "core_switch", "blast_radius": "LAN"},
    "knobs": [{"key": "mode", "type": "enum", "allowed": ["fast", "safe"], "required": True}],
}
COLL_UNIT = {
    "key": "facts-ios",
    "unit": {"kind": "collection_playbook", "collection": "cisco.ios", "name": "ios_facts"},
    "target": {"device_class": "cisco_ios", "inventory_group": "core_switch", "blast_radius": "this-device"},
    "knobs": [],
}


def test_build_plan_role_renders_wrapper_with_access_chain():
    """A role unit renders the role→play WRAPPER: hosts = the inventory_group (the one dispatch seam), the FQCN
    role, the curated non-secret value in a vars: block, and the derived access-chain header naming the blast
    radius. Guards the Review-stage 'show exactly what runs' contract for the wrapper case."""
    plan = build_actuation_plan("backup-ios", {"mode": "safe"}, unit=ROLE_UNIT)
    assert plan["error"] is None
    assert "hosts: core_switch" in plan["play"]
    assert "- role: cisco.ios.ios_config" in plan["play"]
    assert "mode: 'safe'" in plan["play"]                       # the curated value rendered in the vars: block
    assert "Blast radius:        LAN" in plan["play"]            # the GENERATED access-chain header (design 22 §2.4)
    assert plan["target"]["blast_radius"] == "LAN" and plan["unit"]["kind"] == "role"


def test_build_plan_collection_playbook_renders_run_spec():
    """A collection_playbook unit renders the run-spec (no wrapper): the FQCN playbook + the limit group. Guards
    the directly-runnable case — the operator sees the playbook + target without a synthesized wrapper."""
    plan = build_actuation_plan("facts-ios", {}, unit=COLL_UNIT)
    assert plan["error"] is None
    assert "playbook: cisco.ios.ios_facts" in plan["play"]
    assert "limit: core_switch" in plan["play"]
    assert plan["unit"]["kind"] == "collection_playbook"


def test_build_plan_no_unit():
    """An absent actuation unit returns {error:'no_unit'} (the route maps it to 404) — `unit=False` injects an
    explicit miss so the assertion is deterministic. Guards rendering a play for a unit that doesn't exist."""
    assert build_actuation_plan("ghost", {}, unit=False) == {"error": "no_unit", "key": "ghost"}


def test_build_plan_rejects_invalid_values():
    """Values that don't validate (an enum value outside the allow-list) return {error:'invalid', errors} — the
    plan FAILS CLOSED and renders no play. Guards a preview ever being built from un-validated input (the P0a
    guard reused, fail-closed)."""
    plan = build_actuation_plan("backup-ios", {"mode": "bogus"}, unit=ROLE_UNIT)
    assert plan["error"] == "invalid" and "mode" in plan["errors"]
    assert "play" not in plan                                   # no play rendered from invalid values


def test_build_plan_enact_sequences_check_first():
    """The enact hand-off lists the --check Dry Run BEFORE the bare apply, and check_first names the dry-run —
    the CLAUDE.md --check-first hard rule surfaced in the preview (the API runs none of these)."""
    plan = build_actuation_plan("backup-ios", {"mode": "safe"}, unit=ROLE_UNIT)
    tasks = [e.get("task", "") for e in plan["enact"]]
    assert tasks.index("act-backup-ios (Dry Run)") < tasks.index("act-backup-ios")
    assert "--check" in plan["check_first"]


def test_build_actuation_plan_is_read_only():
    """build_actuation_plan + every render helper render + read only — they stage nothing (the propose/build
    read-only contract; the write is apply_actuation_plan). The AST pin asserts none calls a write/actuation verb
    nor opens a file for writing — including the R5 staging-content renderers (_render_vars_file/_vars_rel), which
    BUILD the artifact string but must never WRITE it."""
    for fn in ("build_actuation_plan", "render_play", "_wrapper_play", "_run_spec", "_access_chain_header",
               "actuation_enact_commands", "_yv", "_render_vars_file", "_vars_rel"):
        assert_read_only("scripts/kontroll/service/actuation.py", fn)


def test_api_actuation_gui_route_is_read_only():
    """The GUI Automations-index route (gui/app.py `api_actuation`) is read-only — it lists the registered units
    (over registered_units_view) + audits, but stages/commits nothing. The AST pin asserts it calls no
    write/actuation verb nor opens a file for writing, so the configure READ surface can never silently gain a
    write path (the configure write is the separate `api_actuation_stage` → stage_plan)."""
    assert_read_only("gui/app.py", "api_actuation")


# ── R5: the FIRST write verb — stage a configured unit's values as a proposal ────────────────────────────────────
def test_build_plan_returns_staging_metadata():
    """The plan carries the staging fields the route needs: the single vars-file path, its rendered (generated,
    do-not-hand-edit) content, and an anti-drift token over that content. Guards the propose→stage contract
    (paths + token_parts + plan_token), and that the staged file carries NO secret (SEC-2)."""
    plan = build_actuation_plan("backup-ios", {"mode": "safe"}, unit=ROLE_UNIT)
    assert plan["paths"] == [plan["vars_rel"]]
    assert plan["vars_rel"].replace("\\", "/").endswith("actuation/backup-ios/vars.yml")
    assert "mode: safe" in plan["vars_content"] and "Do NOT hand-edit" in plan["vars_content"]
    assert plan["token_parts"] == [plan["vars_content"]] and plan["plan_token"]


def test_apply_writes_vars_file_and_is_idempotent(tmp_path, monkeypatch):
    """apply_actuation_plan writes the unit's configure-values file under paths.ROOT and is IDEMPOTENT — a second
    apply of identical content reports changed:False (a re-stage of unchanged values is a no-op — the '0 changed'
    contract). The FIRST app-store write verb; the route stages the returned path to proposed/<run_id>."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    plan = build_actuation_plan("backup-ios", {"mode": "safe"}, unit=ROLE_UNIT)
    out = apply_actuation_plan(plan)
    written = tmp_path / plan["vars_rel"]
    assert out["changed"] is True and out["paths"] == [plan["vars_rel"]] and written.exists()
    assert "mode: safe" in written.read_text(encoding="utf-8")
    assert apply_actuation_plan(plan)["changed"] is False        # idempotent — identical content, no rewrite


def test_stage_plan_happy_path_writes_with_a_matching_token(tmp_path, monkeypatch):
    """stage_plan with the propose-time token recomputes the plan, passes the anti-drift gate, and writes the
    vars file (changed:True). Guards the propose→stage round-trip the route drives (the catalog read is the seam
    tests inject)."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(catalog, "actuation_unit", lambda k: ROLE_UNIT if k == "backup-ios" else None)
    token = build_actuation_plan("backup-ios", {"mode": "safe"})["plan_token"]
    out = stage_plan("backup-ios", {"mode": "safe"}, token)
    assert out["error"] is None and out["changed"] is True
    assert (tmp_path / out["paths"][0]).exists()


def test_stage_plan_drift_refuses_and_writes_nothing(tmp_path, monkeypatch):
    """stage_plan with a token that doesn't match the recomputed plan returns {error:'drift'} and writes no file
    — the propose→stage anti-drift gate. Guards a stage landing values the operator never reviewed."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(catalog, "actuation_unit", lambda k: ROLE_UNIT if k == "backup-ios" else None)
    out = stage_plan("backup-ios", {"mode": "safe"}, "stale-bogus-token")
    assert out == {"error": "drift", "key": "backup-ios"}
    assert not (tmp_path / "actuation" / "backup-ios" / "vars.yml").exists()


def test_apply_actuation_plan_is_a_registered_write_verb():
    """apply_actuation_plan opens a file for writing, so it MUST be in WRITE_VERBS (P0b/#134) — else a read view
    could call it invisibly (assert_read_only fails OPEN on an unregistered verb). Guards the registration landing
    in the SAME commit as the verb; the completeness gate (test_readonly_completeness) is the fail-closed twin.
    `stage_plan` (the orchestrator that calls the verb) is registered too (R5 review GAP-3) — defense-in-depth."""
    assert "apply_actuation_plan" in WRITE_VERBS and "stage_plan" in WRITE_VERBS


@pytest.mark.parametrize("crafted", [
    "../../etc/x", "/abs/path", "a/b", "..", "UPPER", "with space", "", "x\x00y", "foo_bar", "9bad", "-lead"])
def test_build_plan_rejects_a_crafted_key_before_building_a_path(crafted, monkeypatch):
    """SEC-3 analog for the WRITE path (R5 review MF-1/MF-2, the BLOCKER): a route {key} that isn't a valid
    registry key (^[a-z][a-z0-9-]*$) is rejected as no_unit BEFORE any path is built — so a crafted key (`../`,
    absolute, slashes, uppercase, NUL, blank, underscore, leading digit/hyphen) can NEVER construct a vars-file
    write target outside instance/actuation/<key>/. Proven by making the path builder EXPLODE if reached AND by
    forcing actuation_unit to 'find' a unit (the key guard must fire FIRST, before the catalog lookup)."""
    import kontroll.service.actuation as act

    def boom(k):
        raise AssertionError("_vars_rel built a path for a crafted key %r" % k)
    monkeypatch.setattr(act, "_vars_rel", boom)
    monkeypatch.setattr(catalog, "actuation_unit", lambda k: ROLE_UNIT)   # even if a unit 'matched', the guard wins
    assert build_actuation_plan(crafted, {"mode": "safe"}) == {"error": "no_unit", "key": crafted}


def test_the_play_preview_renders_the_units_identity_values_as_one_word_each():
    """The would-run play preview — what the operator reads before authorizing a stage — renders a unit's key,
    group, collection and name as ONE scalar each: a group YAML would read as another type is quoted
    (`hosts: "null"`), one the YAML reader cannot even build is quoted too, and a value with a line break cannot
    add a line to the header or the play. (The knob VALUES under `vars:` are shown as Python literals, as before.)
    Guards the preview showing something other than what the descriptor says: an extra `hosts:` line, or a comment
    that ends early."""
    import yaml
    from kontroll.service import actuation
    def unit(group, name="ios_facts", kind="role"):
        return {"key": "u", "unit": {"kind": kind, "collection": "cisco.ios", "name": name},
                "target": {"device_class": "cisco_ios", "inventory_group": group, "blast_radius": "LAN"}}
    benign = actuation.render_play(unit("core_switch"), {})
    assert "  hosts: core_switch\n" in benign + "\n"
    quoted = actuation.render_play(unit("null"), {})
    assert yaml.safe_load(quoted)[0]["hosts"] == "null"
    hostile = actuation.render_play(unit("g\n  hosts: all", name="r\n# Blast radius:        this-device"), {})
    assert len(hostile.splitlines()) == len(benign.splitlines())
    assert yaml.safe_load(hostile)[0]["hosts"] == "g\n  hosts: all"
    spec = actuation.render_play(unit("null", kind="collection_playbook"), {})
    assert yaml.safe_load(spec)["limit"] == "null"
    odd = actuation.render_play(unit("2026-02-30", name="x: y #z", kind="collection_playbook"), {})
    assert yaml.safe_load(odd) == {"playbook": "cisco.ios.x: y #z", "limit": "2026-02-30"}
