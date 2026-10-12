"""settings — the read-only "what is my instance configured as?" view (#135, GUI-paradigm Phase 1).

These pin service/settings.read_view(): a PURE, value-free, four-group snapshot (identity / backup / fleet /
status) of the platform configuration powering the GUI Settings panel. WHY (the gap this fills): the GUI had no
"show me my instance" surface — mgmt_ip/domain/tls_mode, the enabled fleet, the offsite remotes, the arming
posture, and which secret domains are set were only visible by reading files on the box. They also pin the two
load-bearing properties: it serves NO secret value (C11 — the secret roster is NAMES + set/unset only), and it is
read-only-by-construction (the #133 AST pin) so a future edit can't slip a write into the status surface.
"""
import pytest
import yaml

from _readonly_pins import assert_read_only
from kontroll.service import promote, settings

pytestmark = pytest.mark.unit

_INSTANCE = (
    "source_of_truth: local\n"
    "mgmt_ip: 192.0.2.50\n"
    "domain: example.test\n"
    "trust_mode: separated\n"
    "backup_remotes:\n  - offsite1\n  - offsite2\n"
    "frontend:\n  tls_mode: self_signed\n"
)


def _write_instance(tmp_repo, body=_INSTANCE):
    (tmp_repo / "config" / "instance.yml").write_text(body, encoding="utf-8")


def test_read_view_surfaces_the_four_groups(tmp_repo):
    """read_view returns identity (mgmt_ip/domain/tls_mode/trust_mode/source_of_truth from instance.yml), backup
    (backup_remotes), fleet (enabled_modules from the copied fleet.yml), and a status group — so the panel shows
    the whole instance at a glance. Guards the four-group assembly the Settings panel renders; the `trust_mode`
    surfacing (F3) is the read-only promote posture the Settings panel shows."""
    _write_instance(tmp_repo)
    v = settings.read_view()
    assert v["identity"] == {"mgmt_ip": "192.0.2.50", "domain": "example.test",
                             "tls_mode": "self_signed", "trust_mode": "separated", "source_of_truth": "local"}
    assert v["backup"]["backup_remotes"] == ["offsite1", "offsite2"]
    assert "proxmox" in v["fleet"]["enabled_modules"]            # the tmp_repo copy of fleet.yml
    assert set(v) == {"identity", "backup", "fleet", "status"}


def test_status_arming_reflects_env_and_lists_services(tmp_repo, monkeypatch):
    """The status group's `armed` flag mirrors the GUI's OWN KONTROLL_STAGE_PUSHES env (the C10 staging posture —
    a read of our env, never a toggle), and `services` reuses the list_services read. Guards the arming badge the
    operator reads to know whether the GUI can stage at all."""
    _write_instance(tmp_repo)
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    assert settings.read_view()["status"]["armed"] is True
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    st = settings.read_view()["status"]
    assert st["armed"] is False and isinstance(st["services"], list)


def test_secret_roster_is_names_and_setflag_only_never_a_value(tmp_repo):
    """The secret-domain roster carries ONLY {domain, label, set} — NAMES + a derived set/unset badge, never a
    secret value (C11). `set` is derived from key PRESENCE (offerable_fields' already_set), never a decrypt.
    Guards the unauthenticated-to-a-value leak: a status surface must expose which domains are configured, not
    what they contain. (tmp_repo has no secret files, so every domain reads unset.)"""
    _write_instance(tmp_repo)
    roster = settings.read_view()["status"]["secret_domains"]
    assert isinstance(roster, list)
    for entry in roster:
        assert set(entry) == {"domain", "label", "set"}        # exactly these keys — no value field
        assert isinstance(entry["set"], bool)
    assert all(e["set"] is False for e in roster)              # no secret files in the tmp tree


def test_read_view_degrades_per_group_never_raises(tmp_repo):
    """A missing/malformed source yields THAT group's {"error": ...} and never blanks the others, and read_view
    never raises (service-never-sys.exit — a crash would kill the in-process Flask worker). Here instance.yml is
    absent (tmp_repo ships none): identity reports an error but fleet/status still render. Guards the independent-
    degrade posture the panel relies on."""
    v = settings.read_view()                                    # no instance.yml written
    assert "error" in v["identity"]                            # the missing source surfaces as a group error
    assert isinstance(v["fleet"]["enabled_modules"], list)     # …but fleet still renders from fleet.yml
    assert "armed" in v["status"]                              # …and status still renders


def test_settings_read_view_is_read_only_by_construction():
    """read_view is pure-read: the shared AST pin (#133) asserts it calls no write/actuation verb nor opens a file
    for writing. Guards the Settings status surface ever gaining a stage/write path (the EDIT/GUARD writes are a
    later, separately-reviewed C10-staged phase; THIS surface must stay read-only — P0b/M2)."""
    assert_read_only("scripts/kontroll/service/settings.py", "read_view")


# --- the EDIT half (Phase 5, #140): STAGE a tracked instance.yml / fleet.yml knob change -------------------- #
_FLEET = "enabled_modules:\n  - proxmox\n  - cisco_ios   # the switch\n"


def _write_fleet(tmp_repo, body=_FLEET):
    (tmp_repo / "config" / "fleet.yml").write_text(body, encoding="utf-8")


def test_build_plan_mgmt_ip_is_identity_and_preserves_comments(tmp_repo):
    """Editing mgmt_ip is a `modify` UPGRADED to severity `identity` (the descriptor), the comment-preserving
    write keeps instance.yml's inline annotations, the plan carries the re-IP consequence text, and the token
    gates the bytes. THE danger knob: a re-IP must fire the type-to-confirm, not a plain overwrite."""
    _write_instance(tmp_repo, "mgmt_ip: 192.0.2.50   # the bind\ndomain: example.test\nfrontend:\n  tls_mode: self_signed\n")
    plan = settings.build_plan("mgmt_ip", "192.0.2.77")
    assert plan["error"] is None and plan["severity"] == "identity"
    assert plan["changes"][0]["path"] == "mgmt_ip" and plan["changes"][0]["after"] == "192.0.2.77"
    assert "# the bind" in plan["text_after"] and "DISCONNECT" in plan["consequence"]
    assert promote.verify_token(plan["plan_token"], *plan["token_parts"]) is True
    settings.apply_plan(plan)
    assert yaml.safe_load((tmp_repo / "config" / "instance.yml").read_text(encoding="utf-8"))["mgmt_ip"] == "192.0.2.77"


def test_build_plan_tls_mode_is_redeploy_and_nested(tmp_repo):
    """tls_mode edits the NESTED frontend.tls_mode (severity redeploy), validated against its enum. Guards the
    nested writer + the redeploy classification (the ack gate)."""
    _write_instance(tmp_repo)
    plan = settings.build_plan("tls_mode", "byo_proxy")
    assert plan["error"] is None and plan["severity"] == "redeploy"
    assert yaml.safe_load(plan["text_after"])["frontend"]["tls_mode"] == "byo_proxy"


def test_build_plan_backup_remotes_parses_comma_list_and_validates_each(tmp_repo):
    """backup_remotes accepts a comma-separated list, parses + validates each remote name, writes the list block,
    and is severity `modify` (low blast). A crafted remote name is `bad_value`. Guards the list writer + per-item
    validation (no injection via a remote name)."""
    _write_instance(tmp_repo, "mgmt_ip: 192.0.2.50\nbackup_remotes:\n  - old\nfrontend:\n  tls_mode: self_signed\n")
    plan = settings.build_plan("backup_remotes", "off1, off2")
    assert plan["error"] is None and plan["severity"] == "modify"
    assert yaml.safe_load(plan["text_after"])["backup_remotes"] == ["off1", "off2"]
    assert settings.build_plan("backup_remotes", "bad name!")["error"] == "bad_value"


def test_build_plan_rejects_a_crafted_ip_and_an_unknown_or_forbidden_knob(tmp_repo):
    """A non-IPv4 mgmt_ip is `bad_value` (the P0a ipv4 guard, before any write); a knob NOT in the editable
    descriptor set (e.g. api_privileged, source_of_truth, or a bogus key) is `unknown_knob` — the closed
    allow-list. Guards config-injection + the FORBID boundary (api_privileged/.env have no write path here)."""
    _write_instance(tmp_repo)
    assert settings.build_plan("mgmt_ip", "192.0.2.1; rm -rf")["error"] == "bad_value"
    for forbidden in ("api_privileged", "source_of_truth", "bogus"):
        assert settings.build_plan(forbidden, "x")["error"] == "unknown_knob"


def test_build_plan_no_drop_struct_is_the_full_instance_file(tmp_repo):
    """build_plan feeds verify_no_drop the FULL instance.yml (every top-level key), so a writer dropping a
    co-owner key fails closed — not just the edited knob. Guards the load-bearing no-drop over the settings file
    (the review SHOULD-FIX 1 lesson carried)."""
    _write_instance(tmp_repo)
    plan = settings.build_plan("mgmt_ip", "192.0.2.77")
    assert {"mgmt_ip", "domain", "frontend", "backup_remotes"} <= set(plan["current"])
    from kontroll.service._reconfig import verify_no_drop
    dropped = {k: v for k, v in plan["proposed"].items() if k != "domain"}
    assert verify_no_drop(plan["current"], dropped, plan["changes"]) is False
    assert verify_no_drop(plan["current"], plan["proposed"], plan["changes"]) is True


def test_build_fleet_disable_plan_removes_a_module_and_refuses_an_inactive_one(tmp_repo):
    """build_fleet_disable_plan stages a `remove` of an active module (comment-preserving line removal, severity
    remove), names the drop-monitoring consequence, and refuses an inactive module (`not_enabled`). Guards the
    fleet-removal writer + that disable can't be staged for a module that isn't enabled."""
    _write_fleet(tmp_repo)
    plan = settings.build_fleet_disable_plan("proxmox")
    assert plan["error"] is None and plan["severity"] == "remove"
    assert "- proxmox" not in plan["text_after"] and "# the switch" in plan["text_after"]   # comment survives
    assert "scrape targets" in plan["consequence"]             # the drop-monitoring consequence is named
    assert settings.build_fleet_disable_plan("not_a_module")["error"] == "not_enabled"


def test_apply_plan_is_idempotent(tmp_repo):
    """Applying the same settings plan twice writes once. Guards a non-idempotent settings write."""
    _write_instance(tmp_repo)
    plan = settings.build_plan("domain", "new.test")
    assert settings.apply_plan(plan)["changed"] is True
    assert settings.apply_plan(plan)["changed"] is False


def test_settings_edit_reads_are_read_only_by_construction():
    """current_value + the two configurable settings projections are read-only-by-construction (P0b/M2): the
    EDIT read-back must never mutate. apply_plan IS the writer (not pinned); disable_in_fleet is in WRITE_VERBS."""
    assert_read_only("scripts/kontroll/service/settings.py", "current_value")
    assert_read_only("scripts/kontroll/service/configurable.py", "_settings_knob_view")
    assert_read_only("scripts/kontroll/service/configurable.py", "_settings_fleet_view")


@pytest.mark.parametrize("knob,value", [("domain", "null"), ("domain", "true"), ("domain", "123"), ("domain", "no"),
                                        ("domain", "2026-10-12"), ("domain", "2026-02-30"), ("domain", "0000-00-00"),
                                        ("backup_remotes", "null"), ("backup_remotes", "ok, true"),
                                        ("backup_remotes", "0x_"), ("backup_remotes", "2026-13-45")])
def test_a_settings_value_yaml_reads_as_another_type_is_bad_value(tmp_repo, knob, value):
    """A `domain` or a backup remote name that is inside its charset and that YAML reads as a keyword, a number or
    a date — or as a date that does not exist, or a number with no digits, which the YAML reader cannot even
    build — is `bad_value` at the plan: nothing is written and nothing is raised. Guards three generations of the
    same defect: the value being stored (`domain: null` is no domain); the plan raising a plain ValueError once
    the writers refused it; and the check itself raising for a word the reader resolves and cannot build."""
    _write_instance(tmp_repo)
    plan = settings.build_plan(knob, value)
    assert plan["error"] == "bad_value"
    # ...and it is the VALIDATOR in front that says so, with its own message — not the writers' fallback
    assert ("remote name" if knob == "backup_remotes" else "another type") in plan["detail"]


def test_a_writer_s_own_refusal_is_a_bad_value_not_an_exception(tmp_repo, monkeypatch):
    """With the knob validator replaced by one that accepts anything — a future knob type whose validator is
    looser than its writer — a value the scalar writer refuses still comes back as `bad_value`. Guards the
    writers' fail-closed check surfacing as an uncaught error on a route that has no `try`."""
    _write_instance(tmp_repo)
    monkeypatch.setattr(settings, "validate_value", lambda knob, value: None)
    for knob, value in (("domain", "null"), ("domain", "a" + chr(0x2028) + "? k"), ("tls_mode", "true")):
        plan = settings.build_plan(knob, value)
        assert plan["error"] == "bad_value" and "plain YAML" in plan["detail"], (knob, plan)
