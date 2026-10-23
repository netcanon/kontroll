"""The FastAPI surface as a black box — TestClient over the real ASGI app, the service I/O seams
patched in their home modules so it runs offline (no ansible, no network, no lab). Asserts each
read-only route's status + shape, that the typed `/openapi.json` emits, and that kontroll can
ingest its OWN spec (the dogfooding). The L2b route contract of docs/api-architecture.md §6;
the API analogue of test_gui_api.py.
"""
import importlib
import os
import pkgutil
import re

import pytest
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import catalog, probe
from kontroll.service.openapi import build_openapi_recipe

pytestmark = pytest.mark.integration


@pytest.fixture
def client(monkeypatch, make_facts):
    """A TestClient over a fresh app, with the service I/O seams patched in their home modules:
    cisco.ios installed as a cliconf device, everything else absent, no Galaxy hits. The `with`
    block runs the lifespan (which loads the REAL vectors/overrides/backends catalog)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})
    monkeypatch.setattr(catalog, "galaxy_search", lambda kw, limit: [])

    def fake_shallow(kw, limit):                  # the redesigned /search default — fast, files-only
        if any("cisco" in k.lower() for k in kw):
            return [make_facts(collection="cisco.ios", version="5.0.0", depth="shallow",
                               plugins={"cliconf": ["ios"]}, modules=["ios_command"])]
        return []
    monkeypatch.setattr(catalog, "local_shallow", fake_shallow)

    def fake_probe(coll, version=None):
        if coll == "cisco.ios":
            return make_facts(collection=coll, version=version or "5.0.0",
                              plugins={"cliconf": ["ios"]}, modules=["ios_command"])
        return make_facts(collection=coll)        # empty -> "not installed"
    monkeypatch.setattr(probe, "deep_probe", fake_probe)

    # GET /units seams (service.units): cisco.ios installed (its dir under /base), ships one playbook + one role.
    monkeypatch.setattr(catalog, "_installed_json", lambda: {"/base": {"cisco.ios": {"version": "5.0.0"}}})
    monkeypatch.setattr(probe, "units_in_collection",
                        lambda coll, base: {"collection_playbook": ["site"], "role": ["configure"]})

    # POST /units/{key}/configure (R3) + POST /units/preview (R4) seam: one actuation unit 'backup-ios' — a role
    # against core_switch with a required enum + a bounded int.
    monkeypatch.setattr(catalog, "actuation_unit", lambda key: {
        "key": "backup-ios",
        "unit": {"kind": "role", "collection": "cisco.ios", "name": "ios_config"},
        "target": {"device_class": "cisco_ios", "inventory_group": "core_switch", "blast_radius": "LAN"},
        "knobs": [{"key": "mode", "type": "enum", "allowed": ["fast", "safe"], "required": True},
                  {"key": "retries", "type": "int", "range": {"min": 0, "max": 5}}]}
        if key == "backup-ios" else None)

    with TestClient(create_app()) as c:
        yield c


def test_health_ok(client):
    """GET /health returns 200 + {status: ok} — the liveness contract a check probe relies on."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_search_local_hit_is_a_record(client):
    """GET /search?q=cisco returns 200 + a list with the classified cisco.ios record (origin=local,
    depth=shallow by default, suggested backend), serialized against the Record model — the search route
    over the service layer's fast shallow path."""
    r = client.get("/search", params={"q": "cisco"})
    assert r.status_code == 200
    body = r.json()
    assert [rec["collection"] for rec in body] == ["cisco.ios"]
    assert body[0]["origin"] == "local" and body[0]["depth"] == "shallow"
    assert body[0]["suggested_backend"] == "netcommon_cli"


def test_search_returns_the_generic_ssh_card_with_its_class_key(client):
    """GET /search?q=linux returns the Generic SSH host card's record with `suggested_key` `ssh_host`,
    `provenance` and `depth` "declared", and `raw_ssh` as its backend. Guards the typed API stripping the card's
    key: `response_model=list[Record]` drops any field the model does not declare, silently, and a client would
    then propose `ansible_builtin` as the class key."""
    r = client.get("/search", params={"q": "linux"})
    assert r.status_code == 200
    card = next(rec for rec in r.json() if rec["collection"] == "ansible.builtin")
    assert card["suggested_key"] == "ssh_host" and card["provenance"] == "declared" and card["depth"] == "declared"
    assert card["suggested_backend"] == "raw_ssh"


def test_search_deep_param_reprobes(client):
    """GET /search?q=cisco&deep=true re-probes the local match deeply — the record carries depth=deep
    (vs the default shallow), the opt-in that resolves 'maybe' cells via GET /probe-style classification.
    Pins the shallow-default / deep-opt-in split at the HTTP boundary."""
    shallow = client.get("/search", params={"q": "cisco"}).json()
    deep = client.get("/search", params={"q": "cisco", "deep": "true"}).json()
    assert shallow[0]["depth"] == "shallow" and deep[0]["depth"] == "deep"


def test_search_vector_filter(client):
    """GET /search?q=cisco&vector=bespoke drops the cliconf device (it lacks bespoke) -> [] — the
    capability filter exposed as a (repeatable) query param."""
    r = client.get("/search", params={"q": "cisco", "vector": "bespoke"})
    assert r.status_code == 200 and r.json() == []


def test_search_rejects_bad_origin(client):
    """An out-of-enum origin is a 422 (FastAPI validation), not a 500 — the typed query contract."""
    r = client.get("/search", params={"q": "cisco", "origin": "nope"})
    assert r.status_code == 422


def test_probe_found(client):
    """GET /probe/cisco.ios returns 200 + the record and raw module/plugin facts — the deep-probe
    route shaping the service result into ProbeResponse (the dot in the collection name routes fine)."""
    r = client.get("/probe/cisco.ios")
    assert r.status_code == 200
    body = r.json()
    assert body["record"]["collection"] == "cisco.ios"
    assert "ios_command" in body["modules"] and "cliconf" in body["plugins"]


def test_probe_not_installed_404(client):
    """GET /probe/<absent> returns 404 (not a 500 or empty record) — the service's None mapped to a
    clean HTTP error, the API form of the CLI's not-installed exit."""
    assert client.get("/probe/ns.absent").status_code == 404


def test_units_found(client):
    """GET /units/cisco.ios returns 200 + the collection's runnable units (a collection_playbook + a role),
    each with its `<collection>/<kind>/<name>` id — the app-store "search → unit" projection over the service."""
    r = client.get("/units/cisco.ios")
    assert r.status_code == 200
    body = r.json()
    assert body["collection"] == "cisco.ios"
    by_id = {u["id"]: u["kind"] for u in body["units"]}
    assert by_id == {"cisco.ios/collection_playbook/site": "collection_playbook",
                     "cisco.ios/role/configure": "role"}


def test_units_not_installed_404(client):
    """GET /units/<absent> returns 404 — the SEC-3 guard's None (the requested collection is not in the installed
    set) mapped to a clean HTTP error, never a path-traversal read or a 500."""
    assert client.get("/units/ns.absent").status_code == 404


def test_configure_unit_validates_ok(client):
    """POST /units/backup-ios/configure with well-typed values returns 200 + ok:true, no errors — the R3
    configure gate the GUI calls before advancing. A write-free POST (it validates, stages nothing)."""
    r = client.post("/units/backup-ios/configure", json={"values": {"mode": "safe", "retries": 2}})
    assert r.status_code == 200
    assert r.json() == {"key": "backup-ios", "ok": True, "errors": {}}


def test_configure_unit_reports_invalid(client):
    """Invalid values (a required knob missing + an out-of-range int + an un-described key) return 200 + ok:false
    with per-knob errors — fail-closed at the unit boundary (closed allow-list + the P0a _validate guard)."""
    r = client.post("/units/backup-ios/configure", json={"values": {"retries": 99, "rogue": "x"}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert set(body["errors"]) == {"mode", "retries", "rogue"}    # required-missing + out-of-range + unknown


def test_configure_unit_unknown_key_404(client):
    """POST /units/<absent>/configure returns 404 — no actuation unit with that key (the no_unit verdict mapped
    to a clean HTTP error)."""
    assert client.post("/units/ghost/configure", json={"values": {}}).status_code == 404


def test_preview_renders_would_run_play(client):
    """POST /units/preview returns 200 + the would-run play (the role wrapper with hosts:/roles:), the resolved
    target (blast LAN), the --check-first hand-off, and a Dry-Run-first enact — the write-free R4 Review stage."""
    r = client.post("/units/preview", json={"key": "backup-ios", "values": {"mode": "safe", "retries": 2}})
    assert r.status_code == 200
    body = r.json()
    assert body["key"] == "backup-ios" and body["target"]["blast_radius"] == "LAN"
    assert "hosts: core_switch" in body["play"] and "- role: cisco.ios.ios_config" in body["play"]
    assert "--check" in body["check_first"]
    assert any("Dry Run" in (e.get("task") or "") for e in body["enact"])


def test_preview_invalid_values_422(client):
    """Invalid configure values (an enum outside the allow-list) → 422 — the preview FAILS CLOSED and renders no
    play (the P0a guard reused at the Review stage)."""
    assert client.post("/units/preview", json={"key": "backup-ios", "values": {"mode": "bogus"}}).status_code == 422


def test_preview_unknown_unit_404(client):
    """POST /units/preview for an absent unit → 404 (the no_unit verdict mapped to a clean HTTP error)."""
    assert client.post("/units/preview", json={"key": "ghost", "values": {}}).status_code == 404


def test_classify_found(client):
    """GET /classify/cisco.ios returns 200 + best=netcommon_cli with its requirements AND the DECLARED
    telemetry methods (cisco_ios already declares snmp/blackbox for cisco.ios) — the classify route plus the
    detected/declared reconciliation over the service layer; guards the new telemetry_declared field staying
    on the typed contract."""
    r = client.get("/classify/cisco.ios")
    assert r.status_code == 200
    body = r.json()
    assert body["best"] == "netcommon_cli" and "netcommon_cli" in body["matches"]
    decl = {d["key"]: d["methods"] for d in body["telemetry_declared"]}
    assert "cisco_ios" in decl and "snmp" in decl["cisco_ios"]


@pytest.mark.parametrize("requires", ["network_os", ["network_os", 5]], ids=["string", "non-string-entry"])
def test_classify_answers_repository_data_for_a_requires_that_is_not_a_list_of_names(monkeypatch, make_facts,
                                                                                     requires):
    """GET /classify for a collection whose best backend's registry `requires:` is not a list of param names answers
    409 `repository_data` naming `requires`, as the onboard planner and the scaffold refuse the same file.

    Guards the classifier reading the field raw (T18's fold-3 read, N3): the response model refused a string with a
    500, the CLI joined it letter by letter, and a non-string entry raised a TypeError."""
    real = catalog.load_backends

    def load(*a, **k):
        return [dict(b, requires=requires) if b.get("name") == "netcommon_cli" else b for b in real(*a, **k)]
    monkeypatch.setattr(catalog, "load_backends", load)
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, version="5.0.0", plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.get("/classify/cisco.ios")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "repository_data" and "requires" in r.json()["detail"]


def test_classify_not_installed_404(client):
    """GET /classify/<absent> returns 404 when nothing is installed to classify."""
    assert client.get("/classify/ns.absent").status_code == 404


def test_openapi_schema_emits_typed_paths(client):
    """GET /openapi.json is a valid OpenAPI 3 doc exposing the read-only routes + the typed Record
    schema — the contract ws5 publishes and the dogfooding ingests."""
    schema = client.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.")
    assert {"/search", "/probe/{collection}", "/units/{collection}", "/classify/{collection}", "/health"} <= set(schema["paths"])
    assert "Record" in schema["components"]["schemas"]


def test_dogfood_galaxy_ingests_own_spec(client):
    """kontroll's OpenAPI ingester derives a recipe from the API's OWN /openapi.json — the
    self-referential dogfooding the design calls for (the ws1 openapi service reading the ws2 API:
    /health is classified as the liveness check)."""
    spec = client.get("/openapi.json").json()
    recipe = build_openapi_recipe(spec)
    assert recipe["name"]
    assert recipe["summary"]["check"]["path"] == "/health"


@pytest.mark.parametrize("path", ["/probe/--version", "/classify/--version", "/probe/-M..", "/classify/-vvvv",
                                  "/probe/Cisco.IOS", "/classify/cisco.ios%0A"])
def test_the_open_inquiry_routes_refuse_a_name_that_is_not_a_collection(monkeypatch, tmp_path, path):
    """GET /probe/{collection} and GET /classify/{collection} need no token, and both hand the path segment to
    `ansible-doc`. An option-shaped or otherwise malformed segment — including a valid name followed by a newline —
    is answered 422 with the field's code, no process is started, the value is not echoed, and NO audit line is
    written (the refusal handler audits only a request that carries a principal). Guards the one request value an
    unauthenticated caller could put on a command line, the refusal surfacing as a 500, the check being added only
    behind the token, and a refusal giving an unauthenticated caller a new way to write the audit log."""
    def must_not_run(args):
        raise AssertionError("ansible-doc must not be started: %r" % (args,))
    monkeypatch.setattr(probe, "_ad", must_not_run)
    audit_log = str(tmp_path / "audit" / "api-audit.log")
    with TestClient(create_app(Settings(api_token="t", audit_log=audit_log))) as c:
        r = c.get(path)                                         # no Authorization header at all
    assert r.status_code == 422, (path, r.status_code, r.text)
    assert r.json()["code"] == "invalid_collection"
    assert not any(word in r.text for word in ("--version", "-M..", "-vvvv", "Cisco"))
    assert read_audit(audit_log) == []


def test_classify_survives_a_fleet_file_line_that_is_not_a_class_key(tmp_repo, monkeypatch, make_facts):
    """With a fleet file whose `enabled_modules` holds a `- null` (how an unquoted `null` key reads back) and a
    number, GET /classify for an installed collection still answers 200 — the entries that are not a class key are
    skipped. Guards one bad line in a fleet file turning every route that enumerates the fleet into a 422 that names
    a field the caller never sent: the readers used to hand each entry to `paths.module_file`, which raises the
    request-boundary refusal the app now maps for the whole API."""
    fleet = tmp_repo / "config" / "fleet.yml"
    fleet.write_text(fleet.read_text(encoding="utf-8").replace(
        "enabled_modules:\n", "enabled_modules:\n  - null\n  - 123\n", 1), encoding="utf-8")
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, version="5.0.0", plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    with TestClient(create_app()) as c:
        r = c.get("/classify/cisco.ios")
    assert r.status_code == 200, r.text
    assert "code" not in r.json()


def test_every_route_module_is_mounted():
    """Every module under `api/routes/` that defines a `router` has each of its routes — method and path — among
    the app's mounted routes. A route file is registered by hand in `api/main.py` (an import and one entry in a tuple — the module
    docstring used to say it was "a drop-in, never editing a hub", which invited exactly this): a file that is
    written, tested against its own router and never registered would ship as dead code with green tests. It
    passes on the tree as it is; it is a guard for the next route, not a test that was ever red."""
    import api.routes

    # the app's OpenAPI document, not `app.routes`: newer FastAPI keeps an included router there as one opaque entry
    document = create_app(Settings(api_token=None, audit_log="unused")).openapi()["paths"]
    mounted = {(method.upper(), path) for path, operations in document.items() for method in operations}
    modules, unmounted = 0, []
    for info in pkgutil.iter_modules(api.routes.__path__):
        router = getattr(importlib.import_module("api.routes." + info.name), "router", None)
        if router is None:
            continue
        modules += 1
        unmounted += ["%s %s" % (method, route.path) for route in router.routes for method in sorted(route.methods)
                      if (method, route.path) not in mounted]
    assert modules >= 5, "the route modules were not found — this test has stopped reading anything"
    assert unmounted == [], "routes of a module under api/routes/ that api/main.py does not register: %s" % unmounted


def test_the_readme_lists_the_routes_the_app_mounts():
    """`api/README.md`'s route table names every route the app mounts — method and path — and no route that is
    not there (FastAPI's own `/openapi.json`, `/docs` and `/redoc` aside, which the table lists and the schema
    does not). Guards a list in prose rotting the way a count does: the table had lost five routes — the
    capability and secrets routers and one read — while the document beside it said it was the place to look."""
    readme = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                               "api", "README.md"), encoding="utf-8").read()
    listed = {(method, path) for method, path in re.findall(r"`(GET|POST|PUT|PATCH|DELETE) (/[^`?\s,]*)", readme)}
    document = create_app(Settings(api_token=None, audit_log="unused")).openapi()["paths"]
    mounted = {(method.upper(), path) for path, operations in document.items() for method in operations}
    assert mounted - listed == set(), "mounted routes the README's table does not list"
    assert listed - mounted == {("GET", "/openapi.json")}, "rows of the README's table for routes that are not mounted"
