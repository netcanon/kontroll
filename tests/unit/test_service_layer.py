"""Service layer (kontroll.service.*) — the structured-return contract the CLI and the
planned API both consume.

These call the service functions DIRECTLY and assert on the returned data — no capsys /
stdout-parsing. That cleaner shape is the whole point of the galaxy.py service extraction
(docs/api-architecture.md §1): the logic returns dicts, the CLI's cmd_* wrappers are thin
formatters over them. The probe/catalog I/O seams are patched in their HOME modules
(kontroll.probe / kontroll.catalog), the same place the integration tests patch them.
"""
import pytest

from kontroll import catalog, probe
from kontroll.service.audit import service_audit
from kontroll.service.classify import service_classify
from kontroll.service.onboard import build_onboard_plan
from kontroll.service.openapi import build_openapi_recipe
from kontroll.service.probe import service_probe
from kontroll.service.scaffold import build_scaffold
from kontroll.service.search import service_search

pytestmark = pytest.mark.unit


@pytest.fixture
def cliconf_probe(monkeypatch, make_facts):
    """Patch deep_probe (in its home module) so every collection probes as a cliconf device, and
    local_installed as empty — the common 'network_cli device' fixture for the service tests."""
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None: make_facts(
                            collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {})


# --- search: structured records, no stdout ---------------------------------- #
def test_service_search_returns_classified_records(monkeypatch, make_facts, vectors):
    """service_search RETURNS the record list (origin=local, depth=shallow, suggested backend) for a local
    keyword hit — the data the CLI/JSON/GUI all render. Default is SHALLOW (the local_shallow seam); a
    cliconf device classifies identically at shallow depth since cliconf is a file-visible signal."""
    monkeypatch.setattr(catalog, "local_shallow",
                        lambda kw, limit: [make_facts(collection="cisco.ios", version="5.0.0", depth="shallow",
                                                      plugins={"cliconf": ["ios"]}, modules=["ios_command"])])
    monkeypatch.setattr(catalog, "galaxy_search", lambda kw, limit: [])
    recs = service_search(["cisco"], vectors=vectors)
    assert [r["collection"] for r in recs] == ["cisco.ios"]
    assert recs[0]["origin"] == "local" and recs[0]["depth"] == "shallow"
    assert recs[0]["suggested_backend"] == "netcommon_cli"


def test_service_search_vector_filter_drops_nonmatching(monkeypatch, make_facts, vectors):
    """The `vector` filter keeps only records that HAVE every named capability — filtering a cliconf device
    on `bespoke` returns nothing. A vector filter forces the DEEP probe (so the filter is exact, no shallow
    'maybe' false-negatives), so both the local_shallow candidate seam and deep_probe are stubbed."""
    monkeypatch.setattr(catalog, "local_shallow",
                        lambda kw, limit: [make_facts(collection="cisco.ios", depth="shallow",
                                                      plugins={"cliconf": ["ios"]}, modules=["ios_command"])])
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None: make_facts(
                            collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "galaxy_search", lambda kw, limit: [])
    assert service_search(["cisco"], vector=["bespoke"], vectors=vectors) == []


# --- probe / classify: None when not installed ------------------------------ #
def test_service_probe_none_when_not_installed(monkeypatch, make_facts):
    """service_probe returns None (not a crash, not an empty record) when deep_probe finds no
    modules/plugins — the signal the CLI turns into its not-installed exit + install hint."""
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(collection=coll))
    assert service_probe("ns.absent") is None


def test_service_classify_reports_best_and_requires(cliconf_probe):
    """service_classify returns the ordered matches plus the best backend's name + its requirements
    — the structured form cmd_classify formats (a cliconf device -> netcommon_cli, requires network_os)."""
    res = service_classify("cisco.ios")
    assert res["best"] == "netcommon_cli"
    assert "netcommon_cli" in res["matches"]
    assert "network_os" in res["requires"]


def test_service_classify_none_when_not_installed(monkeypatch, make_facts):
    """service_classify returns None when the collection isn't installed locally (empty probe) —
    the CLI maps that None to its 'not installed locally' exit."""
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(collection=coll))
    assert service_classify("ns.absent") is None


# --- onboard: plan as data, secret-hygiene preserved ------------------------ #
def test_build_onboard_plan_structured_no_creds(cliconf_probe):
    """build_onboard_plan returns the full plan (backend, default host name, module + drop-in host
    blocks, repo-rel paths) with an empty creds_to_set when no credentials are passed — the dry-run
    data cmd_onboard prints, verified as a dict instead of by scraping the [1]..[4] plan text."""
    plan = build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50")
    assert plan["error"] is None
    assert plan["backend"] == "netcommon_cli"
    assert plan["host_name"] == "edgeos-1"                       # default <key>-1
    assert plan["mod_path"].endswith("module.yml") and "onboarded-edgeos.yml" in plan["inv_path"]
    assert plan["creds_to_set"] == {}
    assert plan["module"]["role"] == "backend_netcommon_cli"


def test_build_onboard_plan_creds_are_lookups_not_values(cliconf_probe):
    """A passed password is held in creds_to_set (for the apply step's SOPS encrypt) keyed by its
    cred VAR name, but NEVER rendered into the host text — the host carries an inline SOPS lookup
    instead. Pins the secret-hygiene property at the service boundary."""
    plan = build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50", password="s3cret")
    assert plan["creds_to_set"]["edgeos_1_password"] == "s3cret"  # value held for apply only
    assert "s3cret" not in plan["host_text"]                      # never written into the host file
    assert "community.sops.sops" in plan["host_text"]             # an inline SOPS lookup instead


def test_build_onboard_plan_not_installed_error(monkeypatch, make_facts):
    """build_onboard_plan returns error='not_installed' (not a sys.exit) when the probe is empty —
    the service stays exception-free so the API can map it to a 4xx; the CLI owns the message."""
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(collection=coll))
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    assert build_onboard_plan("ns.absent", "x", "g", "192.0.2.1")["error"] == "not_installed"


# --- scaffold / audit / openapi: structured outcomes ------------------------ #
def test_build_scaffold_success_then_not_installed(cliconf_probe, monkeypatch, make_facts):
    """build_scaffold returns a module with its backend role for a classifiable
    collection, and error='not_installed' when the probe is empty — the two outcomes cmd_scaffold
    branches on, as data rather than stdout/exit."""
    ok = build_scaffold("cisco.ios", "ios_x", "core_switch", "network")
    assert ok["error"] is None
    assert ok["module"]["role"] == "backend_netcommon_cli"
    assert ok["path"].endswith("module.yml")
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(collection=coll))
    assert build_scaffold("ns.absent", "x", "g", "network")["error"] == "not_installed"


def test_service_audit_lists_declared_vs_installed(monkeypatch):
    """service_audit returns {enabled, declared, installed}: the enabled modules, the (sorted)
    collections they declare, and the installed map — the data cmd_audit renders installed/MISSING.
    Reads the real fleet.yml; installed is stubbed so the shape is asserted without ansible."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "9.9.9"})
    res = service_audit()
    assert isinstance(res["enabled"], list) and isinstance(res["declared"], list)
    assert res["installed"] == {"cisco.ios": "9.9.9"}
    assert res["declared"] == sorted(res["declared"])           # sorted for stable rendering


def test_build_openapi_recipe_returns_recipe_and_summary():
    """build_openapi_recipe returns {name, recipe, text, summary} from a parsed spec — summary
    carries the derived facts the CLI prints and recipe is the staged YAML, both print-free. Pins
    the OpenAPI ingester's new structured service shape (port/auth/actuate/backup derivation)."""
    spec = {"info": {"title": "Acme FW", "version": "2.1"},
            "servers": [{"url": "https://fw.local:8443/api/v2"}],
            "components": {"securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-Auth"}}},
            "paths": {"/config/backup": {"get": {}}, "/monitor/status": {"get": {}},
                      "/cmdb/policy": {"post": {}}}}
    res = build_openapi_recipe(spec)
    assert res["name"] == "acme_fw"
    assert res["summary"]["port"] == 8443 and res["summary"]["actuate"] is True
    assert res["recipe"]["auth"]["type"] == "apikey_header"
    assert res["recipe"]["backup"]["path"] == "/api/v2/config/backup"


# --- search resilience: galaxy is best-effort, never hangs ------------------ #
def test_galaxy_search_degrades_to_empty_on_network_error(monkeypatch):
    """galaxy_search returns [] (not an exception) when the Galaxy HTTP call times out / errors — guards
    that a slow or unreachable galaxy.ansible.com degrades /search to local-only instead of hanging the
    request (the deployed-container failure mode: Galaxy slow -> /search blocked past the client timeout)."""
    import socket
    import urllib.request

    def boom(*a, **k):
        raise socket.timeout("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert catalog.galaxy_search(["cisco"], 8) == []


def test_service_search_returns_local_when_galaxy_unavailable(monkeypatch, make_facts, vectors):
    """service_search still returns the LOCAL hits when galaxy_search blows up — the search degrades to
    local-only rather than 500ing/hanging, the property the deployed /search needs when Galaxy is slow."""
    monkeypatch.setattr(catalog, "local_shallow",
                        lambda kw, limit: [make_facts(collection="cisco.ios", depth="shallow",
                                                      plugins={"cliconf": ["ios"]}, modules=["ios_command"])])

    def boom(kw, limit):
        raise RuntimeError("galaxy exploded")
    monkeypatch.setattr(catalog, "galaxy_search", boom)
    recs = service_search(["cisco"], origin="both", vectors=vectors)
    assert [r["collection"] for r in recs] == ["cisco.ios"]      # local hit survives; galaxy skipped


def test_service_search_origin_local_never_calls_galaxy(monkeypatch, make_facts, vectors):
    """origin=local must NOT touch galaxy_search at all — guards the network-free local search path (a
    galaxy_search wired to explode is never reached), correcting the assumption that origin=local queries
    Galaxy (the local hang was the un-timed ansible-galaxy subprocess, fixed separately)."""
    monkeypatch.setattr(catalog, "local_shallow",
                        lambda kw, limit: [make_facts(collection="cisco.ios", depth="shallow",
                                                      plugins={"cliconf": ["ios"]}, modules=["ios_command"])])

    def boom(kw, limit):
        raise AssertionError("galaxy_search must not be called for origin=local")
    monkeypatch.setattr(catalog, "galaxy_search", boom)
    assert [r["collection"] for r in service_search(["cisco"], origin="local", vectors=vectors)] == \
        ["cisco.ios"]


def test_service_search_deep_fanout_is_concurrent(monkeypatch, make_facts, vectors):
    """deep=True re-probes the (already capped) shallow candidates CONCURRENTLY — guards the deep opt-in
    against re-introducing the serial-probe hang (N x ~2.6s ansible-doc blocked the deployed /search past
    the client timeout). All candidates classify; deep_probe runs in parallel. The fan-out CAP now lives in
    catalog.local_shallow (tests/unit/test_shallow_search.py::test_local_shallow_caps_fanout_at_limit)."""
    cands = [make_facts(collection="cisco.c%02d" % i, depth="shallow", modules=["ios_command"]) for i in range(8)]
    monkeypatch.setattr(catalog, "local_shallow", lambda kw, limit: cands)
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None: make_facts(
                            collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "galaxy_search", lambda kw, limit: [])
    recs = service_search(["cisco"], origin="local", deep=True, vectors=vectors)
    assert len(recs) == 8 and all(r["origin"] == "local" and r["depth"] == "deep" for r in recs)


def test_shallow_from_galaxy_normalizes_object_tags_to_strings():
    """Galaxy returns collection tags as plain strings OR objects ({"name": ...}); shallow_from_galaxy
    coerces both to a list[str] so the typed Record (meta.tags) serializes — object-tags otherwise 500
    the API's /search (the live failure when Galaxy began returning structured tags)."""
    cv = {"namespace": "cisco", "name": "ios", "version": "1.0.0",
          "tags": [{"name": "networking"}, "switching"], "contents": []}
    assert probe.shallow_from_galaxy(cv, False)["tags"] == ["networking", "switching"]
