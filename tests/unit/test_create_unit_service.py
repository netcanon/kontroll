"""The app-store CREATE-UNIT seam (service/actuation.py): author an actuation unit descriptor from a searched
collection — the stage that fills the empty registry the R3/R4/R5 stages read.

WHY (the failures these guard):
  * build_create_unit_plan must RENDER a descriptor that PASSES the SAME fail-closed schema gen-actuation enforces
    (validate-before-stage) — so a GUI-authored unit can never stage something the lockfile-deriving generator
    would reject (a malformed pin / target reaching ansible-galaxy).
  * the Tier-cap must fire AT CREATE, keyed off the module-DERIVED inventory_group (NOT the operator's blast_radius
    label): an unsigned public-Galaxy install may never be authored against edge_firewall/core_switch (the
    never-brick + blast-radius refusal — the whole point of the create gate). A novice must not point an unsigned
    download at the firewall / core switch.
  * it must NEVER clobber an existing unit (the #124 reuse-don't-overwrite lesson) and must fail closed on a
    collection no enabled device-class declares (no orphaned install).
  * the write is apply_create_unit ONLY — the planner is read-only-by-construction (the propose/preview contract);
    apply must be a registered WRITE_VERBS member (else assert_read_only fails open on it) and idempotent.
  * version pins EXACTLY ('=='), never a floor/range — an app-store install pins one reviewed version.
"""
import pytest
import yaml

from _readonly_pins import WRITE_VERBS, assert_read_only
from kontroll import catalog, paths
from kontroll.service import _actuation_schema as schema
from kontroll.service import actuation
from kontroll.service.actuation import (
    apply_create_unit, build_create_unit_plan, stage_create_unit)

pytestmark = pytest.mark.unit

# a LOW-blast device-class (docker_hosts is not edge_firewall/core_switch) — the create flow may author for it.
MODULE_LOW = {"key": "docker_host", "inventory_group": "docker_hosts"}
MODULE_HIGH = {"key": "cisco_ios", "inventory_group": "core_switch"}
EN = ["docker_host", "cisco_ios"]
GOOD = {"collection": "community.docker", "kind": "role", "name": "swarm", "version": "3.10.4",
        "blast_radius": "LAN"}


def _plan(**over):
    """Build a create plan for GOOD (overridable) against the injected low-blast module + enabled set — the pure,
    monkeypatch-free path the route's propose drives."""
    inputs = dict(GOOD, **over)
    return build_create_unit_plan(inputs, module=MODULE_LOW, enabled_modules=EN)


def test_build_renders_a_valid_descriptor():
    """A well-formed create renders a complete unit.yml: the derived key, the EXACT '==' pin, source galaxy +
    signature adaptive (the brick-proof default), the module-derived device_class/inventory_group, and NO knobs
    block (the zero-knob MVP). Guards the create stage producing exactly what configure/preview/stage then read."""
    p = _plan()
    assert p["error"] is None
    assert p["key"] == "community-docker-swarm" and p["version"] == "==3.10.4"
    assert p["device_class"] == "docker_host" and p["inventory_group"] == "docker_hosts"
    assert p["paths"] == [p["unit_rel"]] and p["unit_rel"].replace("\\", "/").endswith(
        "actuation/community-docker-swarm/unit.yml")
    doc = yaml.safe_load(p["unit_content"])
    assert doc["install"]["collections"][0] == {"name": "community.docker", "version": "==3.10.4"}
    assert doc["install"]["provenance"] == {"source": "galaxy", "signature": "adaptive"}
    assert "knobs" not in doc                                  # zero-knob MVP — knob curation is a later surface
    assert p["plan_token"] and p["token_parts"] == [p["unit_content"]]


def test_rendered_descriptor_passes_the_generator_schema():
    """The rendered descriptor validates CLEAN through the SAME `_actuation_schema.validate_unit` gen-actuation
    runs in tests/validate — the validate-before-stage contract: the create flow can never stage a unit the
    lockfile-deriving generator would reject."""
    p = _plan()
    doc = yaml.safe_load(p["unit_content"])
    assert schema.validate_unit(doc, p["key"], EN) == []


@pytest.mark.parametrize("name", ["swarm", "a", "0", "_", "a" * 47, "a" * 48, "a" * 64, "_" * 64, "x__y",
                                  "docker_swarm_v2"])
@pytest.mark.parametrize("collection", ["community.docker", "a.b", "a_.b_", "a" * 64 + "." + "b" * 64])
def test_everything_create_accepts_the_schema_accepts(collection, name):
    """For every collection and unit name the create planner takes, the descriptor it renders has no error from
    the schema — and a pair whose derived key is longer than one path component is answered by the planner
    (`invalid`, naming the key) before a descriptor or a path exists. The rows stand on both sides of the bound
    (`community.docker` with forty-seven letters is a key of sixty-four, with forty-eight one of sixty-five) and
    include names whose underscores the key collapses, so what is expected is the planner's own answer held
    against the rule, never a sum of the inputs' lengths.

    Guards the two rules drifting apart again (the route kept a pattern of its own: a create the schema then
    refuses is answered with a `descriptor` error, which tells the operator nothing about the field they typed),
    and the planner RAISING for a long pair: the path builder refused the derived key as a request field named
    `unit key`, which no request carries."""
    p = _plan(collection=collection, name=name)
    key = actuation._derive_key(collection, name)
    if p["error"] is None:
        doc = yaml.safe_load(p["unit_content"])
        assert doc["unit"]["collection"] == collection and doc["unit"]["name"] == name
        assert schema.validate_unit(doc, p["key"], EN) == []
        assert p["key"] == key and paths.is_component(key)
    else:
        assert p["error"] == "invalid" and set(p["errors"]) == {"key"}, p
        assert len(key) > 64 and not paths.is_component(key)
    assert (p["error"] is None) == (len(key) <= 64)


@pytest.mark.parametrize("name", ["a" * 65, "Swarm", "docker-swarm", "docker.swarm", "docker swarm", "swarm\n",
                                  "", None, 5])
def test_create_refuses_a_unit_name_outside_the_schemas_rule(name):
    """A unit name of sixty-five characters, with a capital, a hyphen, a dot, a space or a trailing line break,
    empty, absent or a number is refused at the create route's own input check, as the `name` field, with a
    message that does not carry it.

    Guards the length bound arriving as a `descriptor` error after the plan was built, and a dot — the one
    character that makes the rendered reference address something else — passing the route."""
    p = _plan(name=name)
    assert p["error"] == "invalid" and set(p["errors"]) == {"name"}
    assert not isinstance(name, str) or not name or name not in p["errors"]["name"]


def test_the_create_route_reads_the_schemas_own_pattern(monkeypatch):
    """The create route's `name` check consults `_actuation_schema.UNIT_NAME_RE` itself: with the schema's pattern
    swapped for one that takes a single word, the route refuses every other name on `name` and takes that word.

    Guards a second pattern in the route, under any name — a copy that is equal today and drifts tomorrow, or a
    looser one, which answers a create with a `descriptor` error the operator cannot act on. (That no attribute
    is called `_NAME_RE` would hold that one name and nothing else.)"""
    import re
    monkeypatch.setattr(schema, "UNIT_NAME_RE", re.compile(r"only_this_word\Z"))
    assert set(_plan(name="swarm")["errors"]) == {"name"}
    assert _plan(name="only_this_word")["error"] is None


def test_a_key_too_long_is_answered_before_the_class_is_looked_up():
    """A pair whose derived key is longer than one path component is `invalid` on the key whatever the class turns
    out to be — on a guarded group, not enabled, or declared by no module.

    Guards the order of the planner's answers: what was sent is judged before the fleet is read, so one request
    does not get a 403, a 409 or a 422 depending on the instance it meets."""
    for module, enabled in ((MODULE_HIGH, EN), (MODULE_LOW, []), ({}, EN)):
        p = build_create_unit_plan(dict(GOOD, name="a" * 60), module=module, enabled_modules=enabled)
        assert p["error"] == "invalid" and set(p["errors"]) == {"key"}, p


def test_tier_cap_refuses_high_blast_class():
    """A collection whose device-class targets core_switch/edge_firewall is REFUSED at create (tier_cap) — an
    unsigned public-Galaxy install may not reach the highest-blast tier (never-brick + blast-radius). Guards a
    novice authoring an app-store install onto the firewall / core switch."""
    out = build_create_unit_plan(dict(GOOD, collection="cisco.ios", name="ios_config"),
                                 module=MODULE_HIGH, enabled_modules=EN)
    assert out["error"] == "tier_cap" and out["inventory_group"] == "core_switch"


def test_tier_cap_keys_off_the_module_group_not_the_client_blast():
    """The Tier-cap fires on the module-DERIVED inventory_group, NEVER the operator-supplied blast_radius label:
    even with blast_radius='this-device' (the lowest severity), a core_switch class is refused. Guards a client
    downgrading the apparent blast to smuggle an unsigned install onto a high-blast tier."""
    out = build_create_unit_plan(dict(GOOD, collection="cisco.ios", name="ios_config", blast_radius="this-device"),
                                 module=MODULE_HIGH, enabled_modules=EN)
    assert out["error"] == "tier_cap"


def test_no_module_for_unresolved_collection():
    """A collection no shipped device-class declares fails closed (no_module) — no orphaned install for a class the
    fleet has no backend for. Uses the REAL resolver (module=None) against a collection no module.yml names."""
    out = build_create_unit_plan(dict(GOOD, collection="zzz.nothing", name="ghost"),
                                 module=None, enabled_modules=EN)
    assert out["error"] == "no_module"


def test_not_enabled_when_class_absent_from_fleet():
    """A collection whose class EXISTS but isn't enabled in instance/fleet.yml is refused (not_enabled) — the
    create flow never authors a unit for a class the fleet doesn't run."""
    out = build_create_unit_plan(GOOD, module=MODULE_LOW, enabled_modules=["proxmox"])
    assert out["error"] == "not_enabled" and out["device_class"] == "docker_host"


def test_collision_never_overwrites_an_existing_unit(tmp_path, monkeypatch):
    """A create whose derived key already has a descriptor on disk is refused (exists) — never clobber a
    configured unit (the #124 reuse-don't-overwrite lesson). The operator must disable/remove the old one."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    rel = actuation._unit_rel("community-docker-swarm")
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("schema: 1\n", encoding="utf-8")
    out = build_create_unit_plan(GOOD, module=MODULE_LOW, enabled_modules=EN)
    assert out["error"] == "exists"


@pytest.mark.parametrize("over,field", [
    ({"collection": "nodot"}, "collection"),
    ({"collection": "BAD.CASE"}, "collection"),
    ({"kind": "standalone"}, "kind"),               # standalone is not app-store-creatable (no install)
    ({"kind": "module"}, "kind"),
    ({"name": "has space"}, "name"),
    ({"name": "Bad-Name"}, "name"),                 # uppercase/hyphen not an ansible unit name
    ({"version": ">=3.0"}, "version"),              # a range, not an exact pin
    ({"version": ""}, "version"),
    ({"blast_radius": "galaxy"}, "blast_radius"),
    ({"collection": "cisco.ios.a..b"}, "collection"),   # not exactly namespace.name
    ({"collection": "cisco.ios\n"}, "collection"),      # a trailing newline (`$` matches before it)
    ({"name": "swarm\n"}, "name"),
    ({"version": '1"x'}, "version"),                    # a quote: the pin is hand-formatted into the lockfile
    ({"version": '1"\ncollections:\n#'}, "version"),    # …where this parsed the lockfile to `collections: null`
    ({"version": "1:2"}, "version"),
    ({"version": "1#2"}, "version"),
])
def test_invalid_inputs_are_rejected_per_field(over, field):
    """Each malformed input is rejected as 'invalid' with a per-field error naming the fault — fail closed BEFORE
    any path is built or any module looked up. Covers the collection shape (exactly `namespace.name`, no trailing
    newline), the closed kind set (no standalone), the unit-name shape, the exact-pin rule, and the blast enum —
    and the version's CHARSET: the pin is written into the unit descriptor and later hand-formatted, inside double
    quotes, into the collection lockfile, where a quote or a line break made that file unparseable or parse to
    something else."""
    out = build_create_unit_plan(dict(GOOD, **over), module=MODULE_LOW, enabled_modules=EN)
    assert out["error"] == "invalid" and field in out["errors"]


@pytest.mark.parametrize("version,pin", [("3.10.4", "==3.10.4"), ("==3.10.4", "==3.10.4"), ("  8.0.4  ", "==8.0.4")])
def test_version_normalizes_to_an_exact_pin(version, pin):
    """A bare or already-'=='-prefixed version normalizes to a single EXACT '==' pin (whitespace stripped) — an
    app-store install pins one reviewed version, the supply-chain invariant gen-actuation also enforces."""
    assert _plan(version=version)["version"] == pin


def test_derive_key_is_a_deterministic_slug():
    """The registry key is a deterministic <collection>-<name> slug (non-alnum → '-', collapsed, lowercased) that
    is a valid ^[a-z][a-z0-9-]*$ key == its dir name — so an idempotent re-create lands on the same path."""
    assert actuation._derive_key("cisco.ios", "ios_config") == "cisco-ios-ios-config"
    assert schema.KEY_RE.match(actuation._derive_key("community.docker", "swarm"))


def test_reserved_key_is_refused(monkeypatch):
    """A derived key colliding with the reserved route action 'create' is refused (invalid) — the static
    POST /actuation/create path must not be shadowable by a unit keyed 'create'. Forced by stubbing the deriver
    (normal collection+name slugs always carry a dash, so this can't arise organically)."""
    monkeypatch.setattr(actuation, "_derive_key", lambda c, n: "create")
    out = build_create_unit_plan(GOOD, module=MODULE_LOW, enabled_modules=EN)
    assert out["error"] == "invalid" and "key" in out["errors"]


def test_apply_writes_descriptor_and_is_idempotent(tmp_path, monkeypatch):
    """apply_create_unit writes the unit.yml under paths.ROOT and is IDEMPOTENT — a second apply of identical
    content reports changed:False (the '0 changed' contract). The route stages the returned path to
    proposed/<run_id>."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    p = _plan()
    out = apply_create_unit(p)
    written = tmp_path / p["unit_rel"]
    assert out["changed"] is True and out["paths"] == [p["unit_rel"]] and written.exists()
    assert "==3.10.4" in written.read_text(encoding="utf-8")
    assert apply_create_unit(p)["changed"] is False           # idempotent — identical content, no rewrite


def test_stage_create_unit_happy_path(tmp_path, monkeypatch):
    """stage_create_unit with the propose-time token recomputes the plan, passes the anti-drift gate, and writes
    the descriptor (changed:True). The module/enabled reads are the seams tests inject; the route then stages the
    paths to proposed/<run_id>."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(catalog, "module_for_collection", lambda c: MODULE_LOW)
    monkeypatch.setattr(actuation, "_enabled_modules", lambda: EN)
    token = build_create_unit_plan(GOOD)["plan_token"]
    out = stage_create_unit(GOOD, token)
    assert out["error"] is None and out["changed"] is True
    assert (tmp_path / out["paths"][0]).exists()


def test_stage_create_unit_drift_refuses_and_writes_nothing(tmp_path, monkeypatch):
    """stage_create_unit with a token that doesn't match the recomputed plan returns {error:'drift'} and writes no
    file — the propose→stage anti-drift gate. Guards a create landing a descriptor the operator never reviewed."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(catalog, "module_for_collection", lambda c: MODULE_LOW)
    monkeypatch.setattr(actuation, "_enabled_modules", lambda: EN)
    out = stage_create_unit(GOOD, "stale-bogus-token")
    assert out == {"error": "drift", "key": "community-docker-swarm"}
    assert not (tmp_path / "actuation" / "community-docker-swarm" / "unit.yml").exists()
    assert not (tmp_path / "instance" / "actuation" / "community-docker-swarm" / "unit.yml").exists()


def test_build_create_unit_plan_is_read_only():
    """build_create_unit_plan + its render/derive helpers render + read only — they stage nothing (the
    propose/build read-only contract; the write is apply_create_unit). The AST pin asserts none calls a write
    verb nor opens a file for writing — including _render_unit_file, which BUILDS the descriptor string but must
    never WRITE it."""
    for fn in ("build_create_unit_plan", "_derive_key", "_normalize_pin", "_unit_rel", "_render_unit_file",
               "_enabled_modules"):
        assert_read_only("scripts/kontroll/service/actuation.py", fn)


def test_apply_create_unit_is_a_registered_write_verb():
    """apply_create_unit opens a file for writing, so it MUST be in WRITE_VERBS (P0b/#134) — else a read view
    could call it invisibly. `stage_create_unit` (the orchestrator) is registered too (defense-in-depth, like
    stage_plan). Guards the registration landing in the SAME commit as the verb."""
    assert "apply_create_unit" in WRITE_VERBS and "stage_create_unit" in WRITE_VERBS


def test_api_units_gui_route_is_read_only():
    """The GUI app-store Pick-stage route (gui/app.py `api_units`) is read-only — it lists a collection's runnable
    units (over service_units) + audits, but stages/commits nothing. The AST pin asserts it calls no write/actuation
    verb nor opens a file for writing, so the app-store READ surface can never silently gain a write path (the
    create write is the separate, WRITE_VERBS-registered `api_actuation_create` → stage_create_unit)."""
    assert_read_only("gui/app.py", "api_units")
