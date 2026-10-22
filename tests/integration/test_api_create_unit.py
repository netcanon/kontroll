"""The app-store CREATE route — POST /actuation/create, api/routes/actuation.py (the seam that fills the empty
actuation registry the R3/R4/R5 stages read).

The HTTP surface of authoring a unit descriptor from a searched collection. These pin: the surface is fail-closed
(401 without a token, 503 with no token configured); propose (apply:false) returns the DERIVED key + the unit.yml
path + a token and writes nothing / no git; create (apply:true) with that token writes the descriptor, commits
ONLY that path (scoped, not `git add -A`), and emits an `actuation-create` audit line; the Tier-cap is a 403 (an
unsigned install may not be authored onto edge_firewall/core_switch); a collision is a 409 (never clobber); a
STALE token is a 409; and with KONTROLL_STAGE_PUSHES the push targets proposed/<run_id>, never main (C10). The
git seam is stubbed (nothing real is pushed); the collection→class resolver + enabled set are injected so the
test is independent of the real fleet.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import catalog, gitio, paths
from kontroll.service import actuation
from kontroll.service.pending import promote_hint

pytestmark = pytest.mark.integration

TOKEN = "test-token-xyz"
AUTH = {"Authorization": "Bearer " + TOKEN}
BODY = {"collection": "community.docker", "kind": "role", "name": "swarm", "version": "3.10.4",
        "blast_radius": "LAN"}
MODULE_LOW = {"key": "docker_host", "inventory_group": "docker_hosts"}     # low-blast — creatable
MODULE_HIGH = {"key": "cisco_ios", "inventory_group": "core_switch"}       # high-blast — Tier-capped


@pytest.fixture
def cre(tmp_repo, tmp_path, monkeypatch):
    """A privileged TestClient over the create surface: a token + tmp audit log, the git seam stubbed (records the
    argv, never runs git), an active instance/ overlay, and the collection→class resolver + enabled set injected
    (community.docker → docker_host, a low-blast creatable class). Tests override module_for_collection for the
    Tier-cap case."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    (tmp_repo / "instance").mkdir()                                  # overlay active → instance/actuation/<key>/…
    monkeypatch.setattr(catalog, "module_for_collection", lambda c: MODULE_LOW)
    monkeypatch.setattr(actuation, "_enabled_modules", lambda: ["docker_host"])
    audit_log = str(tmp_path / "audit" / "api-audit.log")
    app = create_app(Settings(api_token=TOKEN, audit_log=audit_log))
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, audit_log=audit_log, repo=tmp_repo, calls=calls, monkeypatch=monkeypatch)


def _unit_file(cre):
    return cre.repo / "instance" / "actuation" / "community-docker-swarm" / "unit.yml"


def test_create_requires_a_token(cre):
    """POST /actuation/create with no token is 401 — the create route is privileged (it WRITES a descriptor).
    Guards the create write verb being reachable unauthenticated."""
    assert cre.client.post("/actuation/create", json=BODY).status_code == 401


def test_propose_writes_nothing_and_returns_the_derived_key_and_token(cre):
    """apply:false returns the DERIVED key, the unit.yml path, and an anti-drift token + the resolved target,
    writing no file and touching no git — the review step before the operator authorizes the create."""
    r = cre.client.post("/actuation/create", headers=AUTH, json=BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False and body["token"] and body["key"] == "community-docker-swarm"
    assert body["device_class"] == "docker_host" and body["version"] == "==3.10.4"
    assert body["paths"][0].endswith("actuation/community-docker-swarm/unit.yml")
    assert not _unit_file(cre).exists() and not cre.calls          # nothing written, no git


def test_invalid_inputs_are_422(cre):
    """apply:false with a malformed input (a version range, not an exact pin) is a 422 (invalid) — the create
    flow fails closed at the route. Guards authoring a descriptor from un-validated input."""
    r = cre.client.post("/actuation/create", headers=AUTH, json=dict(BODY, version=">=3.0"))
    assert r.status_code == 422


def test_tier_cap_is_403(cre):
    """A collection whose device-class targets core_switch/edge_firewall is a 403 — an unsigned app-store install
    may not be authored onto the highest-blast tier (never-brick + blast-radius). Guards the create gate refusing
    the firewall / core switch."""
    cre.monkeypatch.setattr(catalog, "module_for_collection", lambda c: MODULE_HIGH)
    cre.monkeypatch.setattr(actuation, "_enabled_modules", lambda: ["cisco_ios"])
    r = cre.client.post("/actuation/create", headers=AUTH,
                        json=dict(BODY, collection="cisco.ios", name="ios_config"))
    assert r.status_code == 403
    assert "highest-blast" in r.json()["detail"]


def test_collision_is_409(cre):
    """A create whose derived key already has a descriptor on disk is a 409 (exists) — never clobber a configured
    unit. Guards an overwrite of an existing app-store install."""
    _unit_file(cre).parent.mkdir(parents=True)
    _unit_file(cre).write_text("schema: 1\n", encoding="utf-8")
    r = cre.client.post("/actuation/create", headers=AUTH, json=BODY)
    assert r.status_code == 409


def test_create_with_token_writes_scoped_commit_and_audits(cre):
    """A propose→create round-trip: apply:true with the proposed token writes the unit.yml, commits (git mocked)
    ONLY that path (scoped, not `git add -A`), and emits an `actuation-create` audit line. Guards the whole
    audited write path + the scoped commit."""
    proposed = cre.client.post("/actuation/create", headers=AUTH, json=BODY).json()
    r = cre.client.post("/actuation/create", headers=AUTH,
                        json=dict(BODY, apply=True, token=proposed["token"]))
    assert r.status_code == 200
    body = r.json()
    assert body["committed"] is True and body["key"] == "community-docker-swarm" and _unit_file(cre).exists()
    assert "==3.10.4" in _unit_file(cre).read_text(encoding="utf-8")
    add = next(c for c in cre.calls if "add" in c)
    assert any(p.endswith("actuation/community-docker-swarm/unit.yml") for p in add) and "-A" not in add   # scoped
    rows = read_audit(cre.audit_log, action="actuation-create")
    assert rows and rows[0]["run_id"] == body["run_id"] and "community.docker" in rows[0]["detail"]


def test_create_with_stale_token_is_409(cre):
    """apply:true with a token that doesn't match the recomputed plan is a 409 and writes nothing — the anti-drift
    gate at the route. Guards a create landing a descriptor the operator never reviewed."""
    r = cre.client.post("/actuation/create", headers=AUTH, json=dict(BODY, apply=True, token="stale-bogus-token"))
    assert r.status_code == 409
    assert not _unit_file(cre).exists()
    assert not any("commit" in " ".join(c) for c in cre.calls)


def test_create_targets_proposed_ref_when_staging(cre):
    """With KONTROLL_STAGE_PUSHES set (the deployed staging posture), the push targets proposed/<run_id> — never
    main (C10). Guards the load-bearing two-key property: a leaked token can only STAGE a create
    proposal, never land a unit on main. Its `next` is the one promote instruction for THAT run id (T19)."""
    cre.monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    proposed = cre.client.post("/actuation/create", headers=AUTH, json=BODY).json()
    r = cre.client.post("/actuation/create", headers=AUTH, json=dict(BODY, apply=True, token=proposed["token"]))
    body = r.json()
    assert body["staged"] is True and body["target_ref"] == "proposed/%s" % body["run_id"]
    assert body["next"].startswith(promote_hint(body["run_id"]))   # the one promote instruction (T19)
    push = next(c for c in cre.calls if "push" in c)
    assert any("proposed/%s" % body["run_id"] in p for p in push)   # the ref, never refs/heads/main


def test_recreate_after_create_is_409_collision(cre):
    """Once a unit is created (its descriptor on disk), a second create of the SAME collection/name is a 409 —
    even a re-propose 409s (the collision guard fires at build). Guards a double-create: a created unit is
    reconfigured/disabled, never re-authored over itself (the #124 reuse-don't-overwrite lesson, end-to-end)."""
    p1 = cre.client.post("/actuation/create", headers=AUTH, json=BODY).json()
    cre.client.post("/actuation/create", headers=AUTH, json=dict(BODY, apply=True, token=p1["token"]))
    commits_before = sum(1 for c in cre.calls if "commit" in c)
    assert cre.client.post("/actuation/create", headers=AUTH, json=BODY).status_code == 409   # re-propose 409s
    assert sum(1 for c in cre.calls if "commit" in c) == commits_before                       # no second commit


def test_create_is_503_when_no_token_configured(tmp_repo, monkeypatch):
    """With NO KONTROLL_API_TOKEN configured, the privileged create route is DISABLED (503), never open — the
    fail-closed C9 posture. Guards the create write verb being reachable on an unconfigured deploy."""
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: 0)
    app = create_app(Settings(api_token=None, audit_log=str(tmp_repo / "audit.log")))
    with TestClient(app) as c:
        assert c.post("/actuation/create", json=BODY).status_code == 503


def test_create_onto_a_target_that_does_not_confine_is_a_409(cre):
    """A create whose descriptor target the confinement refuses answers 409 `repository_data`: no unit file, no
    commit, and `repository-refuse what=path` in the audit log after the `actuation-create` line.

    Guards the app-wide handler being the one answer for this caller of `confined()` too — never a 500, never a
    refused field."""
    proposed = cre.client.post("/actuation/create", headers=AUTH, json=BODY).json()

    def refuse(rel_path):
        raise paths.RepositoryDataError("path", "a write target does not resolve inside the repository")
    cre.monkeypatch.setattr(paths, "confined", refuse)
    r = cre.client.post("/actuation/create", headers=AUTH, json=dict(BODY, apply=True, token=proposed["token"]))
    assert r.status_code == 409 and r.json()["code"] == "repository_data", r.text
    assert not _unit_file(cre).exists() and not any("commit" in c for c in cre.calls)
    rows = read_audit(cre.audit_log, action="repository-refuse")
    assert rows and "what=path" in rows[-1]["detail"] and "route=/actuation/create" in rows[-1]["detail"]
