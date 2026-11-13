"""The API secret-onboarding route (/secrets/{domain}) — guided service-secret entry over HTTP (D). The
security-critical properties asserted as data: token-gated, dry-run by default, the secret VALUE is NEVER in
a response or the audit (only field NAMES + source), apply encrypts via the mocked sops seam + commits +
STAGES, a missing required field is a clean 422, and an absent age key (decrypt failure) is a clean 500 — never
a clobbered domain. docs: secret-forms/README.md, SECURITY.md.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import gitio
from kontroll.service import secrets as secrets_service

pytestmark = pytest.mark.integration

TOKEN = "test-token-xyz"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A TestClient over a privileged app with the git seams + the sops seams mocked: `_run` and `land_main` record
    what they were asked (the landing mocked too, else an apply lands this checkout's HEAD on the canonical its
    `local` remote names: I-70), sops_decrypt_domain returns an empty domain, sops_write_domain captures the merged
    mapping (so the test can assert WHAT was encrypted without real sops/age). The real secret-forms/ descriptors
    drive the fields."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "land_main",
                        lambda run_id=None, skip_gate=False: calls.append(["kontroll-land", "main"]) or 0)
    written = {}
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {})
    monkeypatch.setattr(gitio, "sops_write_domain",
                        lambda d, m: written.update({"domain": d, "map": dict(m)}) or True)
    # Deterministic "nothing already set" so generated/required behaviour doesn't depend on the real
    # instance/secrets/ file state (which differs workstation vs VM).
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    audit_log = str(tmp_path / "audit.log")
    app = create_app(Settings(api_token=TOKEN, audit_log=audit_log))
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, audit_log=audit_log, calls=calls, written=written)


def test_fields_requires_token(cli):
    """Even reading a domain's field NAMES is privileged (the form shape hints at the secret layout) — no
    token ⇒ 401."""
    assert cli.client.get("/secrets/dashboards/fields").status_code == 401


def test_fields_returns_names_not_values(cli):
    """GET fields returns each field's key/type/required/already_set — names only, never a value."""
    r = cli.client.get("/secrets/dashboards/fields", headers=AUTH)
    assert r.status_code == 200
    keys = {f["key"] for f in r.json()["fields"]}
    assert {"grafana_admin_password", "kontroll_api_token"} <= keys


def test_fields_unknown_domain_404(cli):
    """A domain with no secret-form descriptor is a clean 404, not a 500."""
    assert cli.client.get("/secrets/not_a_domain/fields", headers=AUTH).status_code == 404


def test_dryrun_view_excludes_values(cli):
    """A dry-run POST resolves the plan and returns the VIEW (names + source) — a provided password and the
    generated token are BOTH absent from the response body (the crown-jewel no-leak property over HTTP)."""
    r = cli.client.post("/secrets/dashboards", headers=AUTH,
                        json={"values": {"grafana_admin_password": "s3cret-pw", "gui_admin_password": "gui-pw"}})
    assert r.status_code == 200 and r.json()["applied"] is False
    sources = {v["key"]: v["source"] for v in r.json()["view"]}
    assert sources["grafana_admin_password"] == "provided" and sources["kontroll_api_token"] == "generated"
    assert "s3cret-pw" not in r.text                      # the value never leaves the server
    assert cli.calls == []                                # dry-run touches no git


def test_apply_encrypts_commits_and_audits_without_values(cli):
    """apply encrypts the values into the domain via the sops seam (the captured map holds them — they ARE the
    secret), commits and lands it (the mocked `land_main`), and audits the action + the field NAMES — but the audit
    + response never carry a value."""
    r = cli.client.post("/secrets/dashboards", headers=AUTH,
                        json={"values": {"grafana_admin_password": "SUPER-SECRET-PW", "gui_admin_password": "gui-pw"},
                              "apply": True})
    assert r.status_code == 200
    b = r.json()
    assert b["applied"] and b["changed"] and b["committed"]
    assert ["kontroll-land", "main"] in cli.calls
    assert cli.written["domain"] == "dashboards"
    assert cli.written["map"]["grafana_admin_password"] == "SUPER-SECRET-PW"   # reached sops (in memory only)
    assert len(cli.written["map"]["kontroll_api_token"]) == 64                 # generated on the box
    assert "SUPER-SECRET-PW" not in r.text                                     # never in the response
    rows = read_audit(cli.audit_log)
    text = open(cli.audit_log, encoding="utf-8").read()
    assert any(row["action"] == "secret-apply" for row in rows)
    assert "grafana_admin_password" in text and "SUPER-SECRET-PW" not in text  # NAME yes, VALUE never


def test_apply_missing_required_422(cli):
    """A required field with no value/generator/existing is a clean 422 — never a half-written domain."""
    r = cli.client.post("/secrets/snmp_observability", headers=AUTH, json={"values": {}, "apply": True})
    assert r.status_code == 422


def test_apply_refuses_a_url_its_shape_refuses_422(cli):
    """A notifier URL the field's `http_url` shape refuses is a 422 that names the field and the rule, never the
    value, and nothing is encrypted or committed. Why: a `$` would change the line were it unquoted (T32 read B,
    S1; fold 2 read A, N1); the refusal comes before the domain is touched."""
    r = cli.client.post("/secrets/dashboards", headers=AUTH,
                        json={"values": {"grafana_admin_password": "pw", "gui_admin_password": "pw",
                                         "alert_notifier_url": "https://h/a$tok-xq"},
                              "apply": True})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("field alert_notifier_url must be one http(s) URL")
    assert "tok-xq" not in r.text and cli.written == {} and cli.calls == []


def test_apply_no_age_key_is_clean_500(cli, monkeypatch):
    """If the box can't decrypt the domain (no/invalid age key), apply is a clean 500 — never a clobbered
    domain (the no-key API surfaces this; onboard-gui, which mounts a key, does not hit it)."""
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: None)
    r = cli.client.post("/secrets/acme", headers=AUTH,
                        json={"values": {"acme_dns_token": "tok"}, "apply": True})
    assert r.status_code == 500


# --- rotation (PR-1): the overwrite gate + the actuation enact over HTTP ------------------------------------

def test_apply_overwrite_gate_fails_closed_then_acked(cli, monkeypatch):
    """Rotation = overwriting an already-set field. Without `overwrite:true` the apply fails CLOSED with 409 +
    the NAMES, and NOTHING is written/committed; with the ack it applies. Guards the C9 measure-twice gate on
    clobbering a live secret AND that the gate is the SERVER's — a client can't bypass it by omitting the
    confirm. The value never appears in the 409."""
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    r = cli.client.post("/secrets/dashboards", headers=AUTH,
                        json={"values": {"grafana_admin_password": "ROT-PW"}, "apply": True})
    assert r.status_code == 409 and "grafana_admin_password" in r.json()["detail"]["overwrite"]   # NAMES as DATA
    assert cli.written == {} and cli.calls == []                       # fail-closed: no encrypt, no git
    assert "ROT-PW" not in r.text
    r2 = cli.client.post("/secrets/dashboards", headers=AUTH,
                         json={"values": {"grafana_admin_password": "ROT-PW"}, "apply": True, "overwrite": True})
    assert r2.status_code == 200 and r2.json()["changed"]
    assert cli.written["map"]["grafana_admin_password"] == "ROT-PW"     # the ack let it through


def test_dryrun_reports_overwrite_names(cli, monkeypatch):
    """A dry-run reports which already-set fields a save WOULD clobber (`overwrite`: NAMES) so the client can
    pre-warn — names only, never a value. Rotating ONLY gui_admin_password flags ONLY it: a blank already-set
    `generate:` field (kontroll_api_token) is KEPT, not silently re-minted (#141), so it is NOT in the set."""
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    r = cli.client.post("/secrets/dashboards", headers=AUTH, json={"values": {"gui_admin_password": "x"}})
    assert r.status_code == 200 and r.json()["overwrite"] == ["gui_admin_password"]   # ONLY the rotated field
    assert "x" not in r.json()["overwrite"]


def test_regenerate_remints_only_listed_fields(cli, monkeypatch):
    """`regenerate: [key]` re-mints ONLY the listed already-set generated field (the GUI 'gen' button); other
    blank already-set fields are KEPT. Guards the explicit-signal contract (#141) — a targeted token rotation
    doesn't disturb gui/grafana, and it still needs the overwrite ack (it clobbers a live value)."""
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    r = cli.client.post("/secrets/dashboards", headers=AUTH,
                        json={"values": {}, "regenerate": ["kontroll_api_token"], "apply": True, "overwrite": True})
    assert r.status_code == 200 and r.json()["changed"]
    assert set(cli.written["map"]) == {"kontroll_api_token"}        # ONLY the token re-written; gui/grafana KEPT
    assert len(cli.written["map"]["kontroll_api_token"]) == 64


def test_apply_audit_carries_overwrite_marker_not_value(cli, monkeypatch):
    """The secret-apply audit line carries the NAMES-only `overwrite=` marker + run_id (for the Loki re-index)
    — never a value. Guards the C11/C12 audit discipline on the new marker."""
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    cli.client.post("/secrets/dashboards", headers=AUTH,
                    json={"values": {"grafana_admin_password": "AUDIT-SECRET"}, "apply": True, "overwrite": True})
    text = open(cli.audit_log, encoding="utf-8").read()
    assert "overwrite=grafana_admin_password" in text and "AUDIT-SECRET" not in text


def test_apply_returns_actuation_enact_without_values(cli):
    """A successful apply returns the post-promote `enact` hand-off (NAMES + command strings) so the operator
    can make the staged value live — carrying NO value. gui_admin_password → a recreate of onboard-gui; the
    GUI/API execute none of it (hand-off only)."""
    r = cli.client.post("/secrets/dashboards", headers=AUTH,
                        json={"values": {"gui_admin_password": "ENACT-PW", "grafana_admin_password": "g"},
                              "apply": True})
    assert r.status_code == 200
    enact = {e["field"]: e for e in r.json()["enact"]}
    assert "onboard-gui" in enact["gui_admin_password"]["cmd"]
    assert "ENACT-PW" not in r.text
