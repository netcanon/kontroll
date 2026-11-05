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
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: calls.append(["kontroll-land", "main"]) or 0)
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


def test_a_token_with_a_non_ascii_character_is_a_401_and_audited(priv):
    """A Bearer token holding a non-ASCII character is a 401 with an `auth-denied` audit line, as any wrong token is.
    Guards the compare: `hmac.compare_digest` raises TypeError on a non-ASCII str, so the request answered a bare 500
    and left no audit line of the denied attempt."""
    r = priv.client.post("/refresh", headers={"Authorization": "Bearer töken".encode("utf-8")})
    assert r.status_code == 401
    assert [row["action"] for row in read_audit(priv.audit_log)] == ["auth-denied"]


def _post_raw(app, path, authorization):
    """POST `path` to `app` with an Authorization header of exactly the bytes given (a list: one header for each, in
    order). Starlette's TestClient re-encodes a header that is not UTF-8 (httpx reads it as ISO-8859-1, the test
    transport writes it as UTF-8), so a Latin-1 spelling would reach the app as UTF-8; httpx's ASGITransport hands the
    app the bytes as they are."""
    import asyncio

    import httpx

    async def post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            values = authorization if isinstance(authorization, list) else [authorization]
            return await c.post(path, headers=[("Authorization", v) for v in values])
    return asyncio.run(post())


def test_a_non_ascii_configured_token_admits_its_utf8_bytes_alone(priv, monkeypatch):
    """A configured API token holding a non-ASCII character admits the client that sends its UTF-8 bytes and refuses
    its ISO-8859-1 spelling 401, the bytes reaching the app as sent. Guards the compare of the bytes sent: Starlette
    decodes a header as ISO-8859-1, so a UTF-8 encode of what it hands on is not what came over the wire, and the
    token admitted its Latin-1 spelling alone while the GUI admits a password's UTF-8 (AUTH-NA read A, N1)."""
    monkeypatch.setattr(priv.client.app.state.settings, "api_token", "töken")
    for spelling, want in (("utf-8", 200), ("latin-1", 401)):
        r = _post_raw(priv.client.app, "/refresh", "Bearer töken".encode(spelling))
        assert r.status_code == want, (spelling, r.status_code)


def test_a_configured_token_that_is_not_utf8_is_a_401_not_a_500(priv, monkeypatch):
    """A configured API token that is not valid UTF-8 (a hand-written .env holding a Latin-1 byte, which the
    environment decodes `surrogateescape`) answers a wrong token 401 with an `auth-denied` audit line. Guards the
    configured side's encode: a strict UTF-8 encode raised on the escaped byte, so every Bearer request answered a bare
    500 and left no audit line (AUTH-NA read A, N2)."""
    monkeypatch.setattr(priv.client.app.state.settings, "api_token", "t\udcf6ken")
    r = priv.client.post("/refresh", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert [row["action"] for row in read_audit(priv.audit_log)] == ["auth-denied"]


def test_a_non_utf8_configured_token_admits_its_own_bytes_alone(priv, monkeypatch):
    """A configured API token that is not valid UTF-8 admits the client that sends its own bytes and refuses the
    spellings a lossy encode of it gives ('t?ken', 'tken'). Guards the configured side's error handler: a lossy one
    never raises either, but admits a credential that is not the configured one (AUTH-NA read A2, N2)."""
    monkeypatch.setattr(priv.client.app.state.settings, "api_token", "t\udcf6ken")
    for sent, want in ((b"t\xf6ken", 200), (b"t?ken", 401), (b"tken", 401)):
        r = _post_raw(priv.client.app, "/refresh", b"Bearer " + sent)
        assert r.status_code == want, (sent, r.status_code)


def test_a_configured_token_ending_in_a_strip_byte_admits_its_utf8_bytes(priv, monkeypatch):
    """A configured API token whose last character's UTF-8 ends in 0xA0 or 0x85 ('tokà', 'tokÅ') admits the client
    that sends those UTF-8 bytes. Guards the read of the token from the header: FastAPI's parse trims it with
    `str.strip()`, which takes U+00A0/U+0085 (that last byte, as Starlette decodes it) off, so the compare saw one
    byte short and no spelling admitted (AUTH-NA read A2, N1)."""
    for tok in ("tokà", "tokÅ"):
        monkeypatch.setattr(priv.client.app.state.settings, "api_token", tok)
        r = _post_raw(priv.client.app, "/refresh", ("Bearer " + tok).encode("utf-8"))
        assert r.status_code == 200, (tok, r.status_code)


def test_the_bearer_token_is_read_with_only_ows_trimmed_and_under_bearer_alone(priv):
    """The Bearer token is compared as sent with only spaces and tabs trimmed around it (RFC 9110's separator and
    OWS), and only under the Bearer scheme (in any case). Guards the read of the header itself: trimming as
    `str.strip()` does admits the token with a stray 0xA0 or 0x85 after it, and reading it with `split()` admits one
    before it, bytes that are not the token's; trimming one end only refuses a header RFC 9110 allows; and reading the
    header without FastAPI's scheme check admits the token sent under another scheme (read A2 N1, read B N3)."""
    tok = TOKEN.encode()
    for sent, want in ((b"Bearer " + tok, 200), (b"Bearer  " + tok, 200), (b"Bearer " + tok + b" \t", 200),
                       (b"bearer " + tok, 200), (b"Bearer " + tok + b"\xa0", 401),
                       (b"Bearer " + tok + b"\x85", 401), (b"Bearer \xa0" + tok, 401), (b"Bearer \x85" + tok, 401),
                       (b"Basic " + tok, 401), (b"Token " + tok, 401)):
        r = _post_raw(priv.client.app, "/refresh", sent)
        assert r.status_code == want, (sent, r.status_code)


def test_the_token_compared_is_the_one_under_the_scheme_checked(priv):
    """With two Authorization headers, the right token in the second does not admit a request whose first header holds
    another token or another scheme. Guards the token's read from the header: FastAPI's `creds` gates on the first
    header, so a read of any other would pass the scheme check on one header and compare the token of another
    (AUTH-NA read A3, N1)."""
    tok = TOKEN.encode()
    for values in ([b"Bearer wrong", b"Bearer " + tok], [b"Basic eDp5", b"Bearer " + tok]):
        r = _post_raw(priv.client.app, "/refresh", values)
        assert r.status_code == 401, (values, r.status_code)


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
    assert "kontroll-land main" in joined and not any("push local" in j for j in joined)   # landed, never pushed
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
