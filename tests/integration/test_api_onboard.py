"""The API onboard route — the highest-blast-radius privileged mutation over HTTP (ws3 deferred,
now built). Dry-run returns the plan; apply writes the module + drop-in host + fleet-enable and
commits + pushes the local canonical (git mocked, tmp_repo). The security-critical properties:
token-gated, dry-run by default, credentials NEVER returned or logged (only var names), a re-onboard
with no change commits nothing, and the action is audited. docs/api-architecture.md §3/§8.
"""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import catalog, gitio, probe

pytestmark = pytest.mark.integration

TOKEN = "test-token-xyz"
AUTH = {"Authorization": "Bearer " + TOKEN}


def _body(**over):
    """An onboard request body (a classifiable collection on a host), overridden per test."""
    base = dict(collection="acme.edgeos", key="edgeos", group="edge_router", host="192.0.2.50")
    base.update(over)
    return base


@pytest.fixture
def onb(tmp_repo, tmp_path, monkeypatch, make_facts):
    """A TestClient over a privileged app: a configured token + tmp audit log, the git seam mocked,
    nothing installed, and deep_probe canned as a cliconf device (ns.absent -> empty). tmp_repo
    repoints paths.ROOT so the module/inventory/fleet writes land in a throwaway tree."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    # the cred write now goes whole-domain decrypt -> merge -> stdin re-encrypt (no sops binary in CI/Windows)
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {})
    monkeypatch.setattr(gitio, "sops_write_domain", lambda d, m: True)
    monkeypatch.setattr(catalog, "local_installed", lambda: {})

    def fake_probe(coll, version=None):
        if coll == "ns.absent":
            return make_facts(collection=coll)
        return make_facts(collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"])
    monkeypatch.setattr(probe, "deep_probe", fake_probe)

    audit_log = str(tmp_path / "audit" / "api-audit.log")
    app = create_app(Settings(api_token=TOKEN, audit_log=audit_log))
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, audit_log=audit_log, repo=tmp_repo, calls=calls)


def test_onboard_requires_token(onb):
    """No token ⇒ 401 (the surface is configured; the caller isn't authenticated) — even the
    dry-run plan is privileged."""
    assert onb.client.post("/onboard", json=_body()).status_code == 401


def test_onboard_dryrun_returns_plan_no_git(onb):
    """A dry-run (apply omitted) returns the plan (backend, default host name, repo-rel paths) and
    touches NO git — dry-run by default is the security-relevant invariant, asserted as data."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body())
    assert r.status_code == 200
    b = r.json()
    assert b["applied"] is False
    assert b["plan"]["backend"] == "netcommon_cli" and b["plan"]["host_name"] == "edgeos-1"
    assert b["plan"]["paths"]["module"].endswith("module.yml")
    assert onb.calls == []                         # dry-run never touches git


def test_onboard_dryrun_creds_are_names_not_values(onb):
    """A password in the request surfaces only as the cred VAR NAME in the plan — the value is NEVER
    in the response (it lives only in the in-memory plan for the apply step's SOPS encrypt)."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(password="s3cret-pw"))
    assert "edgeos_1_password" in r.json()["plan"]["creds"]
    assert "s3cret-pw" not in r.text               # the value never leaves the server


def test_onboard_apply_writes_commits_pushes(onb):
    """apply writes the three artifacts (module + drop-in host + fleet-enable), commits, and pushes
    the LOCAL canonical (git mocked) — the full self-completing onboard, audited."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(apply=True))
    assert r.status_code == 200
    b = r.json()
    assert b["applied"] and b["changed"] and b["committed"] and b["pushed_canonical"]
    assert (onb.repo / "modules" / "edgeos" / "module.yml").exists()
    assert (onb.repo / "ansible" / "inventory" / "onboarded-edgeos.yml").exists()
    assert "- edgeos" in (onb.repo / "config" / "fleet.yml").read_text(encoding="utf-8")
    joined = [" ".join(c) for c in onb.calls]
    assert any("git add" in j for j in joined) and any("git commit" in j for j in joined)
    assert any("push local HEAD:refs/heads/main" in j for j in joined)
    assert any(row["action"] == "onboard-apply" for row in read_audit(onb.audit_log))


def test_onboard_apply_audit_excludes_creds(onb):
    """The apply audit records who/what (action, key, the cred VAR name) but NEVER the credential
    value the request carried — the crown-jewel property, enforced at the highest-blast-radius route."""
    onb.client.post("/onboard", headers=AUTH, json=_body(apply=True, password="SUPER-SECRET"))
    text = open(onb.audit_log, encoding="utf-8").read()
    assert "onboard-apply" in text and "edgeos_1_password" in text   # the action + var NAME recorded
    assert "SUPER-SECRET" not in text                                # the value NEVER logged


def test_onboard_not_installed_404(onb):
    """A collection with no probed modules/plugins is a 404 (install it first) — the service's
    not-installed signal mapped to a clean HTTP error."""
    assert onb.client.post("/onboard", headers=AUTH, json=_body(collection="ns.absent")).status_code == 404


def test_onboard_apply_idempotent(onb):
    """A second apply with identical inputs changes nothing and commits nothing (changed=False,
    committed=False) — re-onboarding never duplicates files or pushes a redundant commit."""
    onb.client.post("/onboard", headers=AUTH, json=_body(apply=True))
    onb.calls.clear()
    b = onb.client.post("/onboard", headers=AUTH, json=_body(apply=True)).json()
    assert b["applied"] is True and b["changed"] is False and b["committed"] is False
    assert not any("commit" in " ".join(c) for c in onb.calls)      # nothing committed the second time


# --- F1 (self-describing auth): the cred-fields read + the creds-map onboard path -------------------------------- #
def test_cred_fields_requires_token(onb):
    """The GET /onboard/cred-fields read is privileged like the rest of the onboard router — no token ⇒ 401. Even
    the (value-free) field SHAPE is gated, matching the dry-run-is-privileged posture of this surface."""
    assert onb.client.get("/onboard/cred-fields", params={"backend": "netcommon_cli"}).status_code == 401


def test_cred_fields_derives_ssh_login_for_a_network_backend(onb):
    """GET /onboard/cred-fields?backend=netcommon_cli returns the DERIVED descriptors (SSH login + the enable
    secret, NO api token) — the read the form fetches to render the RIGHT inputs the instant a switch is picked.
    NAMES only: no descriptor carries a value. Guards the per-backend shape + the no-value wire contract."""
    r = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": "netcommon_cli"})
    assert r.status_code == 200
    b = r.json()
    assert b["cred_source"] == "shallow"
    names = {cf["field"] for cf in b["cred_fields"]}
    assert {"username", "password", "ssh_private_key", "enable_password"} <= names and "api_token" not in names
    for cf in b["cred_fields"]:
        assert "value" not in cf and "secret_value" not in cf      # names-only on the wire


def test_cred_fields_for_a_rest_backend_offers_a_token_not_an_ssh_box(onb):
    """THE HEADLINE WIN at the wire: GET /onboard/cred-fields?backend=api derives an api_token and NO
    ssh_private_key — so a REST device's form stops showing a dead SSH box (the operator's exact gap)."""
    b = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": "api"}).json()
    names = {cf["field"] for cf in b["cred_fields"]}
    assert "api_token" in names and "ssh_private_key" not in names


def test_cred_fields_unknown_backend_falls_back_to_the_union(onb):
    """An unknown/blockless backend falls back to the generic union (cred_source=fallback) so the form is never
    blocked (INVARIANT D*) — guards a derivation gap dead-ending onboarding."""
    b = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": "no_such_backend"}).json()
    assert b["cred_source"] == "fallback"
    assert {cf["field"] for cf in b["cred_fields"]} == {"username", "password", "api_token", "ssh_private_key"}


def test_onboard_dryrun_accepts_a_creds_map_and_returns_derived_fields(onb):
    """The form's F1 path end-to-end at the route: a `creds` MAP (not the legacy scalars) is accepted and surfaces
    only as cred VAR NAMES (value never echoed), and the plan carries the derived `cred_fields` descriptors +
    `cred_source` so the form can render them. Guards the new {field:value} contract + the plan-view exposure."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(creds={"password": "pw-via-map"}))
    b = r.json()
    assert "edgeos_1_password" in b["plan"]["creds"] and "pw-via-map" not in r.text
    assert {cf["field"] for cf in b["plan"]["cred_fields"]} >= {"username", "password", "ssh_private_key"}
    assert b["plan"]["cred_source"] == "shallow"


def test_onboard_partial_auth_set_is_a_422_not_a_500(onb, monkeypatch):
    """A planner guard that raises a catchable ValueError — a partially-filled required auth_set (more reachable via
    F1 TIER-B's derived proxmox api-token set) or the MF-S2 domain-confinement refusal — is a clean 422 CLIENT
    error, NOT a 500 server crash. Guards a normal 'you forgot a credential part' operator mistake surfacing as a
    worker error (the route now catches ValueError → 422, mirroring WriteConflict → 409). The guard's own behaviour
    is pinned at the service layer (test_onboard_proxmox_auth.py::test_partial_auth_set_is_refused_for_coherence);
    this pins the ROUTE's mapping of that ValueError to a 422."""
    def _raise(*a, **k):
        raise ValueError("incomplete credential set 'proxmox_api' — provide all of "
                         "['api_user', 'api_token_id', 'api_token_secret'] together (missing ['api_token_secret'])")
    monkeypatch.setattr("api.routes.onboard.build_onboard_plan", _raise)
    r = onb.client.post("/onboard", headers=AUTH, json=_body(collection="community.proxmox", key="proxmox"))
    assert r.status_code == 422
    assert "incomplete credential set" in r.json()["detail"]
    assert r.json()["code"] == "invalid_request"                # not a field refusal: no field code to offer
    audit = open(onb.audit_log, encoding="utf-8").read()
    assert "onboard-refuse" in audit and "reason=planner-guard" in audit and "request-boundary" not in audit


@pytest.mark.parametrize("field,value", [
    ("key", "../../etc"), ("key", "edge/os"), ("key", "Edge"), ("group", "edge router"), ("group", "x;y"),
    ("host", "192.0.2.50; rm -rf /"), ("host", "a..b"), ("host_name", "../h"), ("secrets", "../network"),
    ("collection", "../acme.edgeos"), ("collection", "acme/edgeos"),
    ("host", "2001:db8::1%x}} {{ 7*7 }}"), ("host_name", "2001:db8::1%eth0"), ("key", "edgeos\n"),
    ("host", "192.0.2.50\n"),
])
def test_onboard_refuses_a_path_shaped_field_before_any_write(onb, field, value):
    """THE REQUEST BOUNDARY (2026-10-08 review, finding 1). A `key`/`group`/`host`/`host_name`/`secrets`/`collection`
    carrying a separator, a `..`, a space or a metacharacter is refused 422 by the planner's closed-charset validators
    BEFORE the probe runs or anything is written — git is never touched. So is an address with an IPv6 ZONE ID (the
    standard library accepts any text after the `%`, and `host` becomes an inventory variable Ansible templates) and a
    valid value followed by one newline. The response names the field and never quotes the value. Guards the
    bare-string era, when only the collection's installed-ness was checked and `key` went straight into
    `modules/<key>/`."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(**{field: value}))
    assert r.status_code == 422, (field, value, r.status_code, r.text)
    assert value not in r.json()["detail"] and value.strip() not in r.json()["detail"], "the value is never echoed"
    assert r.json()["code"].startswith("invalid_") and r.json()["code"] != "invalid_request", \
        "the same body every other route gives: the message and a code derived from the FIELD"
    assert field in r.json()["detail"] or field.replace("_", " ") in r.json()["detail"] or "collection" in r.json()["detail"]
    assert onb.calls == [], "a refused request must never reach git"
    audit = open(onb.audit_log, encoding="utf-8").read()
    assert "onboard-refuse" in audit and "reason=request-boundary" in audit, "a refusal must leave an audit trace"
    assert value not in audit, "the audit line names the FIELD, never the refused value"
