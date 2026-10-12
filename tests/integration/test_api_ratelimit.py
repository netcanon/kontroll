"""Rate-limiting the API surface (api/ratelimit.py; ws6 hardening, docs/api-architecture.md §9 item 6).

Verifies the two-class throttle (per-IP inquiry / per-token-digest privileged), the 429 + Retry-After
contract, that a breach is enforced BEFORE auth/route I/O, that a breach writes a best-effort
`rate-limited` audit line carrying NO credential, that the limiter FAILS OPEN on its own malfunction
(while auth/audit stay fail-closed), the window reset, exemption of liveness/meta paths, and that no
privileged route can silently bypass the privileged budget. The ws6 analogue of test_api_auth.py.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import ratelimit
from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import catalog, gitio, probe

pytestmark = pytest.mark.integration

TOKEN = "test-token-xyz"
AUTH = {"Authorization": "Bearer " + TOKEN}
TIGHT = "inquiry=2/min;privileged=2/min"          # the 3rd request in a window trips the limit


@pytest.fixture
def inquiry_client(monkeypatch, make_facts, tmp_path):
    """A TestClient with a TIGHT inquiry limit and the service seams patched offline, so the 3rd inquiry
    request in a window trips the per-IP throttle with no real probe I/O. No token (inquiry needs none)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})
    monkeypatch.setattr(catalog, "galaxy_search", lambda kw, limit: [])
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None:
                        make_facts(collection=coll, version=version or "5.0.0",
                                   plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    s = Settings(api_token=None, audit_log=str(tmp_path / "a.log"), ratelimit=TIGHT)
    with TestClient(create_app(s)) as c:
        yield c


@pytest.fixture
def priv_rl(tmp_repo, tmp_path, monkeypatch):
    """A privileged TestClient with a TIGHT privileged limit + a token + tmp audit log; git and matrix
    seams on throwaway paths. For the token-keyed throttle + breach-audit assertions."""
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: 0)
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    audit_log = str(tmp_path / "audit" / "api-audit.log")
    s = Settings(api_token=TOKEN, audit_log=audit_log, ratelimit=TIGHT)
    with TestClient(create_app(s)) as c:
        yield SimpleNamespace(client=c, audit_log=audit_log)


# --- inquiry class (per-IP) ------------------------------------------------- #
def test_inquiry_allows_under_limit(inquiry_client):
    """Two inquiry requests (== the limit) both succeed — guards against the limiter over-blocking
    legitimate interactive use."""
    xff = {"X-Forwarded-For": "198.51.100.1"}
    assert inquiry_client.get("/classify/cisco.ios", headers=xff).status_code == 200
    assert inquiry_client.get("/classify/cisco.ios", headers=xff).status_code == 200


def test_inquiry_429_over_limit_with_retry_after(inquiry_client):
    """The 3rd inquiry request in the window returns 429 with an integer Retry-After header and the
    {detail, retry_after} body — the core throttle + the client-contract header shape."""
    xff = {"X-Forwarded-For": "198.51.100.2"}
    for _ in range(2):
        inquiry_client.get("/classify/cisco.ios", headers=xff)
    r = inquiry_client.get("/classify/cisco.ios", headers=xff)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    body = r.json()
    assert body["detail"] == "rate limit exceeded" and body["retry_after"] >= 1


def test_inquiry_keyed_per_ip(inquiry_client):
    """A different source IP gets its OWN budget — exhausting IP A's limit does not 429 IP B. Guards
    that inquiry keys per-IP, not globally."""
    a, b = {"X-Forwarded-For": "198.51.100.3"}, {"X-Forwarded-For": "198.51.100.4"}
    for _ in range(3):
        inquiry_client.get("/classify/cisco.ios", headers=a)        # exhaust A (3rd is a 429)
    assert inquiry_client.get("/classify/cisco.ios", headers=b).status_code == 200


def test_window_resets_after_retry_after(inquiry_client, monkeypatch):
    """Advancing the clock past the window lets requests flow again — guards against a stuck window (a
    silent outage). Uses the _clock seam, no sleeping."""
    now = [1_000_000.0]
    monkeypatch.setattr(ratelimit, "_clock", lambda: now[0])
    xff = {"X-Forwarded-For": "198.51.100.5"}
    for _ in range(2):
        inquiry_client.get("/classify/cisco.ios", headers=xff)
    assert inquiry_client.get("/classify/cisco.ios", headers=xff).status_code == 429
    now[0] += 61                                                     # past the 60s window
    assert inquiry_client.get("/classify/cisco.ios", headers=xff).status_code == 200


def test_health_exempt(inquiry_client):
    """/health never 429s even past the limit — a liveness probe is exempt (it does no I/O)."""
    codes = [inquiry_client.get("/health").status_code for _ in range(6)]
    assert codes == [200] * 6


def test_openapi_exempt(inquiry_client):
    """/openapi.json is exempt — the API's own spec tooling (the dogfood) must never be throttled."""
    assert all(inquiry_client.get("/openapi.json").status_code == 200 for _ in range(6))


# --- privileged class (per-token-digest) ------------------------------------ #
def test_privileged_keyed_by_token_not_ip(priv_rl):
    """Two DIFFERENT source IPs presenting the SAME token share the privileged budget — the 3rd request
    (across both IPs) is 429. Guards that privileged keys by token-digest, not IP."""
    h1 = dict(AUTH, **{"X-Forwarded-For": "198.51.100.6"})
    h2 = dict(AUTH, **{"X-Forwarded-For": "198.51.100.7"})
    assert priv_rl.client.post("/refresh", headers=h1).status_code == 200
    assert priv_rl.client.post("/refresh", headers=h2).status_code == 200    # same token, other IP
    assert priv_rl.client.post("/refresh", headers=h1).status_code == 429    # shared token bucket


def test_privileged_429_before_auth_and_audit(priv_rl):
    """A breach 429s WITHOUT running the route's audited mutation — after 2 allowed + 1 breach, exactly
    2 `refresh` audit lines exist (not 3). Guards the ordering: a breach can't force the service or spam
    the fail-closed mutation audit."""
    for _ in range(3):
        priv_rl.client.post("/refresh", headers=AUTH)
    assert len(read_audit(priv_rl.audit_log, action="refresh")) == 2


def test_breach_emits_best_effort_audit(priv_rl):
    """A privileged breach writes a `rate-limited` audit row (queryable via read_audit) carrying the
    token DIGEST — never the raw token — and the class. Guards the abuse-path-is-audited rule + C9."""
    for _ in range(3):
        priv_rl.client.post("/refresh", headers=AUTH)
    rows = read_audit(priv_rl.audit_log, action="rate-limited")
    assert rows and rows[0]["user"] == "api-token"
    assert TOKEN not in " ".join(rows[0].values())                  # the raw token NEVER appears
    assert "class=privileged" in rows[0]["detail"]


def test_breach_audit_unwritable_still_429_not_503(make_facts, tmp_path, monkeypatch):
    """With the audit log unwritable, a breach is still 429 — never escalated to a 503. The breach-audit
    is best-effort (an availability event must not become a hard outage), unlike the fail-closed mutation
    audit. Uses the inquiry class (no mutation audit_action in the path)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})
    monkeypatch.setattr(catalog, "galaxy_search", lambda kw, limit: [])
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None:
                        make_facts(collection=coll, version="5.0.0",
                                   plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    (tmp_path / "blocker").write_text("not a dir", encoding="utf-8")
    bad_audit = str(tmp_path / "blocker" / "audit.log")             # makedirs(dirname) will fail
    s = Settings(api_token=None, audit_log=bad_audit, ratelimit="inquiry=2/min")
    with TestClient(create_app(s)) as c:
        xff = {"X-Forwarded-For": "198.51.100.8"}
        codes = [c.get("/classify/cisco.ios", headers=xff).status_code for _ in range(3)]
    assert codes[-1] == 429 and 503 not in codes


def test_limiter_internal_error_fails_open(inquiry_client, monkeypatch):
    """If the limiter store raises internally, the request PASSES THROUGH (fail-open) to the route
    instead of 429/500 — guards the operator-ratified fail-open-on-malfunction (auth/audit downstream
    stay fail-closed; an inquiry route here simply succeeds despite being well past the limit)."""
    def boom(*a, **k):
        raise RuntimeError("store exploded")
    monkeypatch.setattr(ratelimit._FixedWindowStore, "hit", boom)
    xff = {"X-Forwarded-For": "198.51.100.9"}
    codes = [inquiry_client.get("/classify/cisco.ios", headers=xff).status_code for _ in range(5)]
    assert codes == [200] * 5


def test_every_privileged_route_is_classified():
    """Every route tagged 'privileged' classifies as privileged (and the inquiry routes as inquiry) —
    guards against a NEW privileged route silently bypassing the per-token budget when someone adds one.
    Tags are read from the OpenAPI schema (the stable, version-independent contract) rather than the
    Starlette route objects' `.tags`, whose exposure regressed in newer Starlette — the runtime container
    tracks Starlette ≥1.3, where `app.routes[*].tags` is empty though the OpenAPI tags are intact."""
    app = create_app(Settings(api_token=TOKEN, audit_log="x"))
    paths = app.openapi()["paths"]
    privileged = {p for p, ops in paths.items()
                  if any("privileged" in op.get("tags", []) for op in ops.values() if isinstance(op, dict))}
    assert privileged == {"/refresh", "/capture-exceptions", "/onboard", "/onboard/cred-fields", "/audit/log",
                          "/capability/{cap}", "/capability/{cap}/suggest",
                          "/secrets/{domain}", "/secrets/{domain}/fields",
                          "/actuation/{key}",              # R5 — the first app-store write verb (its own privileged prefix)
                          "/actuation/create"}             # the create-unit write verb (same privileged prefix)
    # /onboard/cred-fields is the F1 read the form fetches — value-free schema, but auth-gated under the onboard
    # router, so it classifies privileged like the rest of that prefix (the per-token budget covers it too).
    for path in privileged:
        assert ratelimit._classify(path) == "privileged"


def test_every_registered_capability_route_is_privileged():
    """Every registered capability's route classifies as privileged — AND so does an as-yet-unregistered cap
    segment, because the WHOLE /capability/<cap> prefix is privileged (one entry, not per-cap). This is the
    seam's registry-parametrized tripwire (DoR §3.5): a future capabilities/<cap>.yml drop-in inherits
    privileged classification automatically, so a new cap can never escape the token budget. The assertion
    grows with the registry it reads — never a hand-edited per-cap list."""
    for cap in catalog.registered_capabilities() + ["some-future-cap"]:
        assert ratelimit._classify("/capability/%s" % cap) == "privileged"
        assert ratelimit._classify("/capability/%s/suggest" % cap) == "privileged"
    for inquiry in ("/search", "/probe/{collection}", "/classify/{collection}"):
        assert ratelimit._classify(inquiry) == "inquiry"
    assert ratelimit._classify("/health") is None                   # exempt
