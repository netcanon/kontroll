"""The privileged API surface — Bearer-token auth + the run_id-correlated audit (ws3).

The security-critical contract (docs/api-architecture.md §4, §7): fail-closed when no token is
configured (503) and on a bad token (401, audited); a configured token admits the low-blast-radius
mutations (POST /refresh, POST /capture-exceptions) and they are AUDITED; the audit is itself
fail-closed (an un-auditable mutation is refused, not performed); the log is queryable. The git seam
and the matrix cache + capture-exception matrix are pointed at throwaway paths so nothing real is
touched and no lab is reached.
"""
import os
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import catalog, gitio

pytestmark = pytest.mark.integration

TOKEN = "test-token-xyz"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def priv(tmp_repo, tmp_path, monkeypatch):
    """A TestClient over a PRIVILEGED app: a configured Bearer token + a tmp audit log, with the git
    seam, the matrix cache, and the capture-exception matrix all on throwaway paths (tmp_repo repoints
    paths.ROOT) and nothing installed. Yields client + token + audit-log path + repo root + git calls."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    audit_log = str(tmp_path / "audit" / "api-audit.log")
    app = create_app(Settings(api_token=TOKEN, audit_log=audit_log))
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, audit_log=audit_log, repo=tmp_repo, calls=calls)


def _matrix_matches(priv):
    """The `match` values currently in the (tmp) capture-exception matrix."""
    doc = yaml.safe_load((priv.repo / "config" / "capture-exceptions.yml").read_text(encoding="utf-8"))
    return [e["match"] for e in (doc or {}).get("exceptions", [])]


# --- fail-closed auth -------------------------------------------------------- #
def test_refresh_503_when_token_unconfigured(tmp_path):
    """No KONTROLL_API_TOKEN configured ⇒ every privileged route is DISABLED (503), never open — the
    fail-closed posture: a privileged surface refuses to operate unauthenticated."""
    app = create_app(Settings(api_token=None, audit_log=str(tmp_path / "a.log")))
    with TestClient(app) as c:
        assert c.post("/refresh").status_code == 503


def test_missing_token_401(priv):
    """A privileged route with NO Authorization header is a 401 (the surface is configured, the
    caller is not authenticated)."""
    assert priv.client.post("/refresh").status_code == 401


def test_bad_token_401_and_audited(priv):
    """A wrong Bearer token is a 401 AND writes an `auth-denied` audit line (denied attempts are
    logged — the token itself never is)."""
    r = priv.client.post("/refresh", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert any(row["action"] == "auth-denied" for row in read_audit(priv.audit_log))


# --- refresh (lowest blast radius) ------------------------------------------ #
def test_refresh_ok_and_audited(priv):
    """A valid token admits POST /refresh: 200 + a run_id, and a `refresh` audit line carrying that
    same run_id (the action is recorded who/when/what)."""
    r = priv.client.post("/refresh", headers=AUTH)
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    rows = read_audit(priv.audit_log, action="refresh")
    assert rows and rows[0]["run_id"] == run_id and rows[0]["user"] == "api-token"


# --- capture-exception (writes + commits + pushes canonical) ---------------- #
def test_capture_exception_adds_commits_pushes(priv):
    """POST /capture-exceptions with a valid token adds the entry, commits, and pushes the LOCAL
    canonical (git mocked) — the audited privileged mutation; the matrix gains the match."""
    r = priv.client.post("/capture-exceptions", headers=AUTH,
                         json={"subject": "acme", "match": "Acme_*"})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] and body["committed"] and body["pushed_canonical"]
    assert "Acme_*" in _matrix_matches(priv)
    joined = [" ".join(c) for c in priv.calls]
    assert any("git add" in j for j in joined) and any("git commit" in j for j in joined)
    assert any("push local HEAD:refs/heads/main" in j for j in joined)
    assert any(row["action"] == "capture-exception" for row in read_audit(priv.audit_log))


def test_capture_exception_idempotent(priv):
    """A second POST with the same match is a no-op (added=False, no second commit) — re-declaring an
    exception never duplicates it or pushes a redundant commit."""
    priv.client.post("/capture-exceptions", headers=AUTH, json={"subject": "a", "match": "Dup_*"})
    priv.calls.clear()
    r = priv.client.post("/capture-exceptions", headers=AUTH, json={"subject": "a", "match": "Dup_*"})
    assert r.json()["added"] is False
    assert _matrix_matches(priv).count("Dup_*") == 1
    assert not any("commit" in " ".join(c) for c in priv.calls)   # nothing committed the second time


def test_capture_exception_bad_match_422(priv):
    """An invalid `match` (not a filename glob) is a 422 from the request model — rejected before the
    service runs, and nothing is committed (guards the .gitignore/`git rm` pathspec corruption)."""
    r = priv.client.post("/capture-exceptions", headers=AUTH,
                         json={"subject": "x", "match": "bad match!"})
    assert r.status_code == 422
    assert not priv.calls                                         # no git touched


# --- audit query + audit fail-closed ---------------------------------------- #
def test_audit_query_filters_by_action(priv):
    """GET /audit/log?action=refresh returns the refresh rows (the privileged read that makes the
    audit queryable) — after a refresh, exactly that action shows up."""
    priv.client.post("/refresh", headers=AUTH)
    r = priv.client.get("/audit/log", headers=AUTH, params={"action": "refresh"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1 and all(row["action"] == "refresh" for row in body["rows"])


def test_mutation_refused_503_when_audit_unwritable(tmp_repo, tmp_path, monkeypatch):
    """If the audit log can't be written, a privileged mutation is REFUSED (503) rather than
    performed unaudited — the fail-closed audit guarantee (§7). Here the audit dir is a FILE, so the
    write raises; /refresh must 503 and never reach the service."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    (tmp_path / "blocker").write_text("not a dir", encoding="utf-8")   # makedirs(dirname) will fail
    bad_audit = str(tmp_path / "blocker" / "audit.log")
    app = create_app(Settings(api_token=TOKEN, audit_log=bad_audit))
    with TestClient(app) as c:
        assert c.post("/refresh", headers=AUTH).status_code == 503
