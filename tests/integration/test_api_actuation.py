"""The app-store STAGE route — POST /actuation/{key}, api/routes/actuation.py (R5, the FIRST write verb).

The seam's HTTP surface. These pin: the surface is fail-closed (401 without a token); propose (apply:false)
returns a token + the single vars-file path and writes nothing / no git; stage (apply:true) with that token
writes the unit's configure-values file, commits ONLY that path (scoped, not `git add -A`), and emits an
`actuation-stage` audit line carrying NO submitted value; a STALE token is a 409 (the anti-drift gate); staging
is bounded — with KONTROLL_STAGE_PUSHES set the push targets proposed/<run_id>, never main (C10); and an unknown
unit is a clean 404. The git seam is stubbed so nothing real is pushed; the unit descriptor lives in a throwaway
instance overlay.
"""
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import gitio, paths

pytestmark = pytest.mark.integration

TOKEN = "test-token-xyz"
AUTH = {"Authorization": "Bearer " + TOKEN}

UNIT = {
    "schema": 1, "key": "backup-ios", "status": "active",
    "unit": {"kind": "role", "collection": "cisco.ios", "name": "ios_config"},
    "install": {"collections": [{"name": "cisco.ios", "version": "==8.0.4"}],
                "provenance": {"source": "galaxy", "signature": "required"}},
    "target": {"device_class": "cisco_ios", "inventory_group": "core_switch", "blast_radius": "LAN"},
    "knobs": [{"key": "mode", "type": "enum", "allowed": ["fast", "safe"], "required": True}],
}


@pytest.fixture
def act(tmp_repo, tmp_path, monkeypatch):
    """A privileged TestClient over the stage surface: a token + tmp audit log, the git seam stubbed (records the
    argv, never runs git), paths.ROOT on a throwaway tree carrying one actuation unit in the instance overlay."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    (tmp_repo / "instance" / "actuation" / "backup-ios").mkdir(parents=True)
    (tmp_repo / "instance" / "actuation" / "backup-ios" / "unit.yml").write_text(
        yaml.safe_dump(UNIT), encoding="utf-8")
    audit_log = str(tmp_path / "audit" / "api-audit.log")
    app = create_app(Settings(api_token=TOKEN, audit_log=audit_log))
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, audit_log=audit_log, repo=tmp_repo, calls=calls)


def _vars_file(act):
    return act.repo / "instance" / "actuation" / "backup-ios" / "vars.yml"


def test_stage_requires_a_token(act):
    """POST /actuation/backup-ios with no token is 401 — the stage route is privileged (it WRITES). Guards the
    first write verb being reachable unauthenticated."""
    assert act.client.post("/actuation/backup-ios", json={"values": {"mode": "safe"}}).status_code == 401


def test_unknown_unit_is_404(act):
    """A propose for a non-existent unit is a clean 404 (no_unit), not a 500 — guards a typo'd key."""
    r = act.client.post("/actuation/ghost", headers=AUTH, json={"values": {}})
    assert r.status_code == 404


def test_propose_writes_nothing_and_returns_a_token(act):
    """apply:false returns the vars-file path + an anti-drift token, writing no file and touching no git — the
    review step. Guards the propose/stage boundary at the route."""
    r = act.client.post("/actuation/backup-ios", headers=AUTH, json={"values": {"mode": "safe"}})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False and body["token"]
    assert body["paths"] and body["paths"][0].endswith("actuation/backup-ios/vars.yml")
    assert not _vars_file(act).exists() and not act.calls       # nothing written, no git


def test_invalid_values_are_422(act):
    """apply:false with a value outside the knob's allow-list is a 422 (invalid) — the P0a guard at the route.
    Guards staging a proposal from un-validated input."""
    r = act.client.post("/actuation/backup-ios", headers=AUTH, json={"values": {"mode": "bogus"}})
    assert r.status_code == 422


def test_stage_with_token_writes_scoped_commit_and_audits(act):
    """A propose→stage round-trip: apply:true with the proposed token writes the vars file, commits (git mocked)
    ONLY that path (scoped, not `git add -A`), and emits an `actuation-stage` audit line with NO submitted value.
    Guards the whole audited write path + the scoped commit."""
    proposed = act.client.post("/actuation/backup-ios", headers=AUTH, json={"values": {"mode": "safe"}}).json()
    r = act.client.post("/actuation/backup-ios", headers=AUTH,
                        json={"values": {"mode": "safe"}, "apply": True, "token": proposed["token"]})
    assert r.status_code == 200
    body = r.json()
    assert body["committed"] is True and _vars_file(act).exists()
    add = next(c for c in act.calls if "add" in c)
    assert any(p.endswith("actuation/backup-ios/vars.yml") for p in add) and "-A" not in add   # scoped
    rows = read_audit(act.audit_log, action="actuation-stage")
    assert rows and rows[0]["run_id"] == body["run_id"]
    assert "safe" not in rows[0]["detail"]                       # names/counts only — never a submitted value


def test_stage_with_stale_token_is_409(act):
    """apply:true with a token that doesn't match the recomputed plan is a 409 and writes nothing — the anti-drift
    gate at the route. Guards a stage landing a proposal the operator never reviewed."""
    r = act.client.post("/actuation/backup-ios", headers=AUTH,
                        json={"values": {"mode": "safe"}, "apply": True, "token": "stale-bogus-token"})
    assert r.status_code == 409
    assert not _vars_file(act).exists()
    assert not any("commit" in " ".join(c) for c in act.calls)


@pytest.mark.parametrize("crafted", ["ABC", "x.y", "foo_bar", "9bad", "-lead"])
def test_crafted_key_is_404_and_writes_nothing(act, crafted):
    """A crafted but route-reachable {key} (a single path segment that isn't a valid registry key) on the WRITE
    route (apply:true) is a clean 404 and writes/commits NOTHING — the SEC-3 analog for the write path (R5 review
    BLOCKER GAP-1/MF-2). The key guard rejects it as no_unit before any vars-file path is built, so it can never
    escape instance/actuation/<key>/."""
    r = act.client.post("/actuation/" + crafted, headers=AUTH,
                        json={"values": {"mode": "safe"}, "apply": True, "token": "x"})
    assert r.status_code == 404
    assert not _vars_file(act).exists()
    assert not any("commit" in " ".join(c) for c in act.calls)


def test_unchanged_values_is_a_route_noop(act):
    """Re-staging the SAME values (a fresh propose token over identical content) is a route no-op: 200 +
    changed:false + committed:false + NO second commit — the 'no duplicate/empty proposal' property + the
    idempotent-write branch at the route (R5 review GAP-2). Guards an unchanged re-stage parking a redundant
    proposal."""
    p1 = act.client.post("/actuation/backup-ios", headers=AUTH, json={"values": {"mode": "safe"}}).json()
    act.client.post("/actuation/backup-ios", headers=AUTH,
                    json={"values": {"mode": "safe"}, "apply": True, "token": p1["token"]})
    commits_before = sum(1 for c in act.calls if "commit" in c)
    p2 = act.client.post("/actuation/backup-ios", headers=AUTH, json={"values": {"mode": "safe"}}).json()
    r = act.client.post("/actuation/backup-ios", headers=AUTH,
                        json={"values": {"mode": "safe"}, "apply": True, "token": p2["token"]})
    body = r.json()
    assert r.status_code == 200 and body["applied"] is True
    assert body["changed"] is False and body["committed"] is False
    assert sum(1 for c in act.calls if "commit" in c) == commits_before   # no second commit


def test_stage_is_503_when_no_token_configured(tmp_repo, monkeypatch):
    """With NO KONTROLL_API_TOKEN configured, the privileged stage route is DISABLED (503), never open — the
    fail-closed C9 posture (R5 review M5-1). Guards the first write verb being reachable on an unconfigured deploy."""
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: 0)
    app = create_app(Settings(api_token=None, audit_log=str(tmp_repo / "audit.log")))
    with TestClient(app) as c:
        assert c.post("/actuation/backup-ios", json={"values": {"mode": "safe"}}).status_code == 503


def test_stage_targets_proposed_ref_when_staging(act, monkeypatch):
    """With KONTROLL_STAGE_PUSHES set (the deployed staging posture), the push targets proposed/<run_id> — never
    main (C10). Guards the load-bearing two-key property: a leaked token can only STAGE a proposal — it cannot
    promote one."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    proposed = act.client.post("/actuation/backup-ios", headers=AUTH, json={"values": {"mode": "safe"}}).json()
    r = act.client.post("/actuation/backup-ios", headers=AUTH,
                        json={"values": {"mode": "safe"}, "apply": True, "token": proposed["token"]})
    body = r.json()
    assert body["staged"] is True and body["target_ref"] == "proposed/%s" % body["run_id"]
    push = next(c for c in act.calls if "push" in c)
    assert any("proposed/%s" % body["run_id"] in p for p in push)   # the ref, never refs/heads/main


def test_stage_onto_a_target_that_does_not_confine_is_a_409(act, monkeypatch):
    """A stage whose values target the confinement refuses answers 409 `repository_data`: no vars file, no commit,
    and `repository-refuse what=path` in the audit log.

    Guards the app-wide handler being the one answer for this caller of `confined()` too — never a 500, never a
    refused field."""
    proposed = act.client.post("/actuation/backup-ios", headers=AUTH, json={"values": {"mode": "safe"}}).json()

    def refuse(rel_path):
        raise paths.RepositoryDataError("path", "a write target does not resolve inside the repository")
    monkeypatch.setattr(paths, "confined", refuse)
    r = act.client.post("/actuation/backup-ios", headers=AUTH,
                        json={"values": {"mode": "safe"}, "apply": True, "token": proposed["token"]})
    assert r.status_code == 409 and r.json()["code"] == "repository_data", r.text
    assert not _vars_file(act).exists() and not any("commit" in c for c in act.calls)
    rows = read_audit(act.audit_log, action="repository-refuse")
    assert rows and "what=path" in rows[-1]["detail"] and "route=/actuation/" in rows[-1]["detail"]
