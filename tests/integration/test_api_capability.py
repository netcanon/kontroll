"""The generalized capability route — /capability/{cap} (+ /suggest), api/routes/capability.py.

The seam's HTTP surface (docs/observability/secondary-capability-dialog.md §3.5, master §6 acceptance). One
route serves every capability via a path segment: GET /suggest is read-only detection; POST proposes (pure)
or promotes (plan-hash-gated, audited, scoped commit). These pin: the surface is fail-closed (401 without a
token); suggest returns the offerable methods for a class; propose writes nothing and hands back a token;
promote with that token commits ONLY the plan's paths and writes a `capability-promote` audit line carrying
NO value; a STALE token is a 409 (the anti-drift gate); and an unregistered cap / un-onboarded class are
clean 404s. The git seam, deep-probe, and matrix cache are stubbed so nothing real is touched.
"""
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import catalog, gitio, paths, probe

pytestmark = pytest.mark.integration

TOKEN = "test-token-xyz"
AUTH = {"Authorization": "Bearer " + TOKEN}


def _fake_probe(collection):
    """A cliconf (network_cli) collection's deep facts — the Cisco-switch shape, enough for the telemetry
    vector + applies_when filter (offers snmp/blackbox/host_node)."""
    return {"collection": collection, "version": "1.0.0", "origin": "local", "depth": "deep",
            "plugins": {"cliconf": ["ios"]}, "modules": ["ios_command"], "module_options": {},
            "description": "", "tags": [], "certified": False}


@pytest.fixture
def cap(tmp_repo, tmp_path, monkeypatch):
    """A privileged TestClient over the capability surface: a token + tmp audit log, the git seam + deep-probe
    stubbed, paths.ROOT on a throwaway tree carrying one onboarded class (demo_sw, no metrics block yet)."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    monkeypatch.setattr(probe, "deep_probe", _fake_probe)
    monkeypatch.setattr(paths, "MATRIX_CACHE", str(tmp_path / "matrix.json"))
    (tmp_repo / "modules" / "demo_sw").mkdir(parents=True)
    (tmp_repo / "modules" / "demo_sw" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo_sw", "collections": [{"name": "cisco.ios"}], "inventory_group": "core_switch"}),
        encoding="utf-8")
    audit_log = str(tmp_path / "audit" / "api-audit.log")
    app = create_app(Settings(api_token=TOKEN, audit_log=audit_log))
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, audit_log=audit_log, repo=tmp_repo, calls=calls)


def _module_text(cap, key="demo_sw"):
    return (cap.repo / "modules" / key / "module.yml").read_text(encoding="utf-8")


# --- fail-closed + 404s ------------------------------------------------------------------------------- #
def test_suggest_requires_a_token(cap):
    """GET /capability/telemetry/suggest with no token is 401 — the whole /capability prefix is privileged,
    even the read-only detection. Guards the surface being open."""
    assert cap.client.get("/capability/telemetry/suggest", params={"key": "demo_sw"}).status_code == 401


def test_unknown_capability_is_404(cap):
    """An unregistered cap segment is a 404 (no such capability), not a 500 — guards a typo'd ?cap= and the
    registry-driven dispatch returning cleanly."""
    assert cap.client.post("/capability/nope", headers=AUTH, json={"key": "demo_sw"}).status_code == 404


def test_suggest_unonboarded_key_is_404(cap):
    """Suggest for a class with no module.yml is a 404 (not onboarded) — the capability surface is
    strictly-after onboarding (INVARIANT D*)."""
    r = cap.client.get("/capability/telemetry/suggest", headers=AUTH, params={"key": "ghost"})
    assert r.status_code == 404


# --- suggest (read-only detection) -------------------------------------------------------------------- #
def test_suggest_offers_methods_for_a_network_class(cap):
    """Suggest returns the offerable methods for the cliconf class — snmp + the universal host_node/blackbox —
    plus the suggester cell. Guards the Stage-0 detection wiring end-to-end (descriptor → suggester +
    applies_when filter)."""
    r = cap.client.get("/capability/telemetry/suggest", headers=AUTH, params={"key": "demo_sw"})
    assert r.status_code == 200
    body = r.json()
    names = {m["name"] for m in body["offerable_methods"]}
    assert {"snmp", "host_node"} <= names
    snmp = next(m for m in body["offerable_methods"] if m["name"] == "snmp")
    assert snmp["params"]["module"]["allowed"] == ["if_mib"]   # the picker gets the closed allow-list
    assert body["suggestion"]["cell"]["state"] in ("yes", "maybe")


# --- propose (pure) ----------------------------------------------------------------------------------- #
def test_propose_writes_nothing_and_returns_a_token(cap):
    """POST apply:false returns the plan + an anti-drift token and leaves the module.yml untouched and git
    silent — the review step. Guards the propose/promote boundary at the route."""
    before = _module_text(cap)
    r = cap.client.post("/capability/telemetry", headers=AUTH,
                        json={"key": "demo_sw", "selection": {"method": "snmp", "params": {"module": "if_mib"}}})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False and body["token"]
    assert body["plan"]["method"] == "snmp" and body["plan"]["enact"]
    assert _module_text(cap) == before          # nothing written
    assert not cap.calls                         # no git


# --- promote (apply, audited, scoped commit) ---------------------------------------------------------- #
def test_promote_with_token_commits_only_plan_paths_and_audits(cap):
    """A propose→promote round-trip: apply:true with the proposed token writes the metrics block, commits
    (git mocked) only the plan's paths, and emits a `capability-promote` audit line. Guards the whole audited
    write path AND that the commit is scoped to the plan's paths (not a blanket `git add -A`)."""
    proposed = cap.client.post("/capability/telemetry", headers=AUTH,
                               json={"key": "demo_sw", "selection": {"method": "snmp",
                                                                     "params": {"module": "if_mib"}}}).json()
    r = cap.client.post("/capability/telemetry", headers=AUTH,
                        json={"key": "demo_sw", "apply": True, "token": proposed["token"],
                              "selection": {"method": "snmp", "params": {"module": "if_mib"}}})
    assert r.status_code == 200
    body = r.json()
    assert body["committed"] is True and "- {method: snmp" in _module_text(cap)
    add = next(c for c in cap.calls if "add" in c)
    assert "modules/demo_sw/module.yml" in add and "-A" not in add   # scoped, not blanket
    rows = read_audit(cap.audit_log, action="capability-promote")
    assert rows and rows[0]["run_id"] == body["run_id"]
    assert "if_mib" not in rows[0]["detail"] or "module" not in rows[0]["detail"]  # method name ok; no secret values


def test_promote_with_a_stale_token_is_409(cap):
    """apply:true with a token that does not match the (recomputed) plan is a 409 and writes nothing — the
    anti-drift gate at the route. Guards a promote landing a plan the operator never reviewed."""
    before = _module_text(cap)
    r = cap.client.post("/capability/telemetry", headers=AUTH,
                        json={"key": "demo_sw", "apply": True, "token": "stale-bogus-token",
                              "selection": {"method": "host_node"}})
    assert r.status_code == 409
    assert _module_text(cap) == before
    assert not any("commit" in " ".join(c) for c in cap.calls)


@pytest.mark.parametrize("key", ["../modules/demo_sw", "Demo_SW", "demo_sw\n"])
def test_capability_refuses_a_bad_key_with_422_and_an_audit_line(cap, key):
    """A malformed class `key` on the capability surface — suggest, propose and promote — is answered 422 with the
    field's code before the service asks the filesystem anything, and each refusal leaves ONE audit line that names
    the route and the field and not the value. Guards a boundary refusal reaching the client as a bare 500 (no
    capability route catches it — the mapping is app-wide), and a refusal that leaves no trace."""
    r = cap.client.get("/capability/telemetry/suggest", headers=AUTH, params={"key": key})
    assert r.status_code == 422 and r.json()["code"] == "invalid_device_class_key", r.text
    for apply in (False, True):
        r = cap.client.post("/capability/telemetry", headers=AUTH,
                            json={"key": key, "selection": {"method": "host_node"}, "apply": apply, "token": "x"})
        assert r.status_code == 422 and r.json()["code"] == "invalid_device_class_key", r.text
        assert "demo_sw" not in r.json()["detail"].lower()
    assert cap.calls == [], "a refused request must never reach git"
    rows = read_audit(cap.audit_log, action="request-refuse")
    assert len(rows) == 3, rows
    for row in rows:
        assert "reason=request-boundary field=device-class key" in row["detail"]
        # the route TEMPLATE, never the concrete path: a path parameter is a request value too
        assert "route=/capability/{cap}" in row["detail"]
        assert "demo_sw" not in row["detail"].lower(), "the audit line names the FIELD, never the refused value"


@pytest.mark.parametrize("name", ["x}\ninventory_group: edge_firewall\nrole: opnsense_firewall\n_pad: {a: 1",
                                  "a, derived: true, gnet: 99999", "../../../../tmp/x"],
                         ids=["newline", "one-line-flow-mapping", "path-traversal"])
def test_capability_refuses_a_hostile_dashboard_name(cap, name):
    """A `dashboards` item whose name is not one path component is 422 on propose AND on promote, and the refusal
    names that item; the class's module file is untouched and git is never called. Guards the route being the
    place that forgets: the selection is handed to the service whole, and the name used to be written into the
    module file by string formatting — where a newline rewrote the class's role and inventory group, and a
    `, key: value` with no newline rewrote the item itself (its board id, its `derived` mark)."""
    before = _module_text(cap)
    selection = {"method": "host_node", "dashboards": [{"gnet": 1860, "name": name}]}
    for apply in (False, True):
        r = cap.client.post("/capability/telemetry", headers=AUTH,
                            json={"key": "demo_sw", "selection": selection, "apply": apply, "token": "x"})
        assert r.status_code == 422, (apply, r.status_code, r.text)
        assert r.json()["detail"].startswith("dashboards[0].name"), r.text      # THIS validator, no other bad_param
        assert "edge_firewall" not in r.text and "tmp/x" not in r.text
    assert _module_text(cap) == before and cap.calls == []


def test_a_field_with_a_carriage_return_does_not_cut_its_audit_row_short(cap):
    """A promote audits `key` and the method name BEFORE validating them. A request whose key holds a carriage
    return leaves ONE whole `capability-promote` row when the log is read back: the text after the carriage return
    and everything the route recorded after the key (down to the push flag) are still in that row's detail. Guards
    a token holder hiding the rest of an audit entry from the audit query: the reader opens the log with universal
    newlines, where a lone `\r` ends a line, and the writer used to flatten only tab and newline."""
    forged = "x\r2026-01-01T00:00:00Z\tadmin\t192.0.2.1\tonboard-apply\tFORGED-RUN\tkey=victim"
    r = cap.client.post("/capability/telemetry", headers=AUTH,
                        json={"key": forged, "selection": {"method": "host_node"}, "apply": True, "token": "x"})
    assert r.status_code == 422                                   # the key is refused — after it was audited
    rows = read_audit(cap.audit_log)
    assert [row["action"] for row in rows if row["action"] != "request-refuse"] == ["capability-promote"]
    assert not any(row["run_id"] == "FORGED-RUN" or row["user"] == "admin" for row in rows)
    assert len(rows) == 2, rows                                   # the promote line and its refusal — nothing else
    promote = next(row for row in rows if row["action"] == "capability-promote")
    assert "FORGED-RUN" in promote["detail"] and "push=False" in promote["detail"], \
        "the row is whole: nothing after the carriage return was cut off"
