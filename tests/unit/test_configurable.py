"""configurable — the read-only PROJECTION behind the GUI's one knob renderer (Phase 3, #137).

These pin service/configurable.py: that each EXISTING descriptor shape (a method's params, a secret-form's
fields, a capability's resource_stage) projects into the SAME uniform knob/group model, so "expose a knob" stays
a descriptor edit and never UI code (the no-bespoke-config tenet). WHY (the failures they guard):
  * a secret knob must NEVER carry a value/default and a password/token/textarea field must be `secret: true`
    (write-only) — a regression here would pre-fill or echo a credential in the GUI (C11);
  * the Stage-3 render-table must project `curated_list`/`none`/`policy_fields` WITHOUT authoring speculative
    knobs (design 21 §10) — `curated_list` is honestly empty until suggested_dashboards lands;
  * the view must DEGRADE (a missing kind/form → an error string), never raise (service-never-sys.exit);
  * it must be read-only-by-construction (the shared AST pin) — a projection must never gain a write path.
The capability/secret loaders are monkeypatched so these pin the PROJECTION logic deterministically (the
loaders + the live probe are covered by their own suites).
"""
import pytest

from _readonly_pins import assert_read_only
from kontroll import catalog
from kontroll.service import capability, configurable, secrets

pytestmark = pytest.mark.unit


# --- the pure projection helpers (no I/O) ------------------------------------------------------------------

def test_param_knob_infers_enum_from_an_allow_list():
    """A method-param spec ({allowed, required}) projects to an `enum` knob carrying the allow-list (type
    inferred — design 21 §2.3). Guards that a legacy telemetry/backup/logging param renders through the shared
    renderer with no descriptor edit."""
    assert configurable.param_knob("module", {"allowed": ["if_mib"], "required": True}) == {
        "key": "module", "label": "module", "type": "enum", "allowed": ["if_mib"], "required": True}


def test_secret_field_knob_never_carries_a_value_and_flags_secret_types():
    """A password/token/textarea field is `secret: true` with `default: None` (NEVER pre-filled — C11); a `text`
    field is non-secret and keeps its default; a textarea is `multiline`. Guards the load-bearing secret-safety
    invariant: the projection surfaces NAMES + set/unset, never a credential value."""
    pw = configurable.secret_field_knob({"key": "token", "type": "password", "required": True,
                                         "default": "SHOULD-NOT-LEAK", "already_set": True})
    assert pw["secret"] is True and pw["default"] is None and pw["already_set"] is True
    txt = configurable.secret_field_knob({"key": "user", "type": "text", "default": "admin"})
    assert txt["secret"] is False and txt["default"] == "admin"
    pem = configurable.secret_field_knob({"key": "key", "type": "textarea"})
    assert pem["secret"] is True and pem["multiline"] is True and pem["default"] is None


def test_stage_group_projects_source_kind_without_speculative_knobs():
    """A capability's resource_stage projects to a knob group keyed by source_kind; `curated_list`/`none` carry
    NO knobs (suggested_dashboards is a later phase — design 21 §10), `policy_fields` projects its `fields`.
    Guards that the render-table CASE exists without authoring speculative dashboard/policy knobs."""
    tel = configurable._stage_group({"name": "telemetry", "resource_stage": {
        "testid": "cap-telemetry-dashboards", "source_kind": "curated_list", "fields": []}})
    assert tel["source_kind"] == "curated_list" and tel["testid"] == "cap-telemetry-dashboards" and tel["knobs"] == []
    pol = configurable._stage_group({"name": "x", "resource_stage": {
        "testid": "cap-x-policy", "source_kind": "policy_fields",
        "fields": [{"key": "level", "allowed": ["info", "debug"], "required": True}]}})
    assert pol["knobs"][0]["type"] == "enum" and pol["knobs"][0]["key"] == "level"


# --- configurable_view dispatch ----------------------------------------------------------------------------

def test_secret_domain_view_projects_one_fields_group(monkeypatch):
    """configurable_view('secret-domain', d) returns ONE `fields` group whose knobs are the projected secret
    fields (NAMES only). Guards the secrets-dialog convergence: the dialog renders this group through the one
    renderer instead of its bespoke loop, with no value ever in the payload."""
    monkeypatch.setattr(secrets, "offerable_fields", lambda d: {
        "error": None, "domain": d, "label": "Sem", "description": "creds", "recipients": "base",
        "fields": [{"key": "admin_password", "label": "Admin password", "type": "password", "required": True,
                    "generate": "hex32", "default": None, "help": "", "already_set": False}]})
    view = configurable.configurable_view("secret-domain", "semaphore")
    assert view["error"] is None and view["object"]["kind"] == "secret-domain"
    grp = view["groups"][0]
    assert grp["source_kind"] == "fields" and grp["testid"] == "secret-fields"
    assert grp["knobs"][0]["key"] == "admin_password" and grp["knobs"][0]["secret"] is True
    assert "value" not in grp["knobs"][0] and grp["knobs"][0]["default"] is None   # never a value/default


def test_secret_domain_view_degrades_on_unknown_form(monkeypatch):
    """An unknown domain (offerable_fields → no_form) degrades to {error:'no_form'}, never raises. Guards the
    route's 404 path + the service-never-sys.exit posture."""
    monkeypatch.setattr(secrets, "offerable_fields", lambda d: {"error": "no_form"})
    assert configurable.configurable_view("secret-domain", "bogus")["error"] == "no_form"


def test_capability_view_projects_param_groups_and_stage(monkeypatch):
    """configurable_view('capability:telemetry', key) wraps the read-only suggest_view and adds (a) a `params`
    group per offerable method (enum knobs from the method's allow-list params) and (b) the resource_stage group.
    Guards the cap-dialog convergence: Stage-1 params AND Stage-3 both render through the one renderer, and the
    {method, params} write payload is unchanged."""
    monkeypatch.setattr(capability, "get_descriptor", lambda cap: {
        "name": "telemetry", "label": "monitoring",
        "resource_stage": {"testid": "cap-telemetry-dashboards", "source_kind": "curated_list", "fields": []}})
    monkeypatch.setattr(capability, "suggest_view", lambda cap, key: {
        "error": None, "suggestion": {"note": "n"}, "declared": [],
        "offerable_methods": [{"name": "snmp", "label": "SNMP", "secret_domain": "snmp_observability",
                               "params": {"module": {"allowed": ["if_mib"], "required": True}}}]})
    view = configurable.configurable_view("capability:telemetry", "cisco_ios")
    assert view["error"] is None
    m = view["methods"][0]
    assert m["name"] == "snmp" and m["secret_domain"] == "snmp_observability"
    assert m["group"]["source_kind"] == "params" and m["group"]["testid"] == "cap-params"
    assert m["group"]["knobs"][0] == {"key": "module", "label": "module", "type": "enum",
                                      "allowed": ["if_mib"], "required": True}
    assert view["resource_stage"]["testid"] == "cap-telemetry-dashboards"


def test_capability_view_passes_through_suggest_errors(monkeypatch):
    """A capability whose suggest_view errors (e.g. not_onboarded) passes that error through — the cap surface is
    strictly after onboarding (INVARIANT D*). Guards the route's 404 path for an un-onboarded key."""
    monkeypatch.setattr(capability, "get_descriptor", lambda cap: {"name": "telemetry", "resource_stage": {}})
    monkeypatch.setattr(capability, "suggest_view", lambda cap, key: {"error": "not_onboarded"})
    assert configurable.configurable_view("capability:telemetry", "ghost")["error"] == "not_onboarded"


def test_actuation_unit_view_projects_one_curated_knob_group(monkeypatch):
    """configurable_view('actuation-unit', key) returns ONE `unit` group whose knobs are the unit's CURATED
    configure descriptors projected to the renderer's shape (R3). Guards the worked-extraction MVP: the configure
    surface is the operator-declared `knobs:` block (not argspec extraction), rendered through the one renderer."""
    monkeypatch.setattr(catalog, "actuation_unit", lambda key: {
        "key": key, "knobs": [{"key": "mode", "type": "enum", "allowed": ["fast", "safe"], "default": "safe",
                               "help": "speed/safety", "required": True}]})
    view = configurable.configurable_view("actuation-unit", "backup-ios")
    assert view["error"] is None and view["object"]["kind"] == "actuation-unit"
    grp = view["groups"][0]
    assert grp["source_kind"] == "unit" and grp["testid"] == "unit-config-fields"
    assert grp["knobs"][0] == {"key": "mode", "label": "mode", "type": "enum", "required": True,
                               "help": "speed/safety", "allowed": ["fast", "safe"], "default": "safe"}


def test_actuation_unit_view_with_no_knobs_is_an_empty_group(monkeypatch):
    """A unit with no `knobs:` block projects an honestly EMPTY `unit` group (nothing to configure), never an
    error — so the configure stage renders a valid, empty form, not a crash."""
    monkeypatch.setattr(catalog, "actuation_unit", lambda key: {"key": key})
    grp = configurable.configurable_view("actuation-unit", "x")["groups"][0]
    assert grp["source_kind"] == "unit" and grp["knobs"] == []


def test_actuation_unit_view_degrades_no_unit(monkeypatch):
    """An absent actuation key (actuation_unit → None) degrades to {error:'no_unit'} (the route's 404), never
    raises — the service-never-sys.exit posture + the closed-dispatch contract."""
    monkeypatch.setattr(catalog, "actuation_unit", lambda key: None)
    assert configurable.configurable_view("actuation-unit", "ghost")["error"] == "no_unit"


def test_unknown_kind_degrades_to_no_kind():
    """An unrecognized kind (NOT the closed {secret-domain, capability:*} set — design 21 §10 / synthesis M5 cut
    the universal registry) returns {error:'no_kind'}, never raises. Guards the closed-dispatch posture."""
    assert configurable.configurable_view("device", "x")["error"] == "no_kind"
    assert configurable.configurable_view("", "x")["error"] == "no_kind"


def test_configurable_view_is_read_only_by_construction():
    """configurable_view + its two projections are pure-read: the shared AST pin (#133) asserts none calls a
    write/actuation verb nor opens a file for writing. Guards the projection ever gaining a write path — it must
    only ever READ descriptors + the read-only offer/suggest views (the C10/read-only contract)."""
    assert_read_only("scripts/kontroll/service/configurable.py", "configurable_view")
    assert_read_only("scripts/kontroll/service/configurable.py", "_secret_view")
    assert_read_only("scripts/kontroll/service/configurable.py", "_capability_view")
    # The pin scans only a function's OWN AST (no recursion), so pin the projection HELPERS the views call too —
    # else a future write added to a pure dict-builder would slip past (closes the adversarial-review coverage gap).
    assert_read_only("scripts/kontroll/service/configurable.py", "param_knob")
    assert_read_only("scripts/kontroll/service/configurable.py", "secret_field_knob")
    assert_read_only("scripts/kontroll/service/configurable.py", "_stage_group")
    assert_read_only("scripts/kontroll/service/configurable.py", "_unit_view")     # R3 — the curated-knob projection
    assert_read_only("scripts/kontroll/service/configurable.py", "_unit_knob")


@pytest.mark.parametrize("testid", ["a b", "x]", 'x"]', "Cap-Promote", "1st", "", "-x", "cap-x-" + "y" * 58, 5,
                                    ["x"], "cap-promote", "cap-params", "knob-level", "cap-y-policy", "cap-x-",
                                    "cap-x-a b", "cap-x-Y", "cap-x-y_z"])
def test_a_stage_testid_outside_its_namespace_is_dropped(testid):
    """Verifies: a capability descriptor whose `resource_stage.testid` is not `cap-<its name>-` then [a-z0-9-], 63
    characters at most, projects a group with no testid (the page renders no container), and one that is passes
    through — the 63rd character included, and no printable ASCII character outside [a-z0-9-] past the prefix; and
    that a capability named after a one-hyphen control (`promote`) cannot name it, the prefix's own trailing hyphen
    holding. Guards against (T22 / F9): a value the page's selector cannot
    parse — the lookup throws and both dialog buttons are left unwired — one that carries a selector of its own, and
    one naming an element the page creates in the dialog (a control, the method container, a knob row, another
    capability's group — unless one name is a hyphen-prefix of the other, `cred` of `cred-x`, which share
    `cap-cred-x-…`; one dialog never holds two capabilities' groups), which the first-match lookup would find
    instead."""
    def stage(t):
        return configurable._stage_group({"name": "x", "resource_stage": {"testid": t, "source_kind": "none"}})
    assert stage(testid)["testid"] is None
    assert stage("cap-x-policy")["testid"] == "cap-x-policy" and stage(None)["testid"] is None
    longest = "cap-x-" + "y" * 57
    assert len(longest) == 63 and stage(longest)["testid"] == longest
    assert configurable.group_testid("cap-promote", "promote") is None
    assert configurable.group_testid("cap-xy", "x") is None
    outside = [c for c in map(chr, range(32, 127)) if not (c.islower() or c.isdigit() or c == "-")]
    assert [c for c in outside if configurable.group_testid("cap-x-y" + c, "x") is not None] == []


def test_a_dropped_stage_testid_is_logged_by_its_descriptor_never_its_value(caplog):
    """Verifies: dropping a descriptor's Stage-3 testid logs a warning that names the descriptor and never the
    refused value. Guards against (T22): the log line being the only trace of a dropped group — it must say which
    descriptor to correct — and against it echoing descriptor bytes into the server log."""
    import logging
    with caplog.at_level(logging.WARNING, logger="kontroll.service.configurable"):
        configurable._stage_group({"name": "x", "resource_stage": {"testid": "zq9 refused]", "source_kind": "none"}})
    assert "knob group of x" in caplog.text and "zq9" not in caplog.text and "refused]" not in caplog.text
