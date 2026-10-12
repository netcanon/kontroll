"""The Flask GUI as a black box — auth gate + the thin /api/* layer.

The GUI is a PRIVILEGED surface, so the auth gate is tested as hard as the happy path: no creds ->
401, wrong creds -> 401, only correct Basic creds reach a route. ALL of /api/search, /api/classify, AND
/api/onboard now call the kontroll service layer **in-process** (no subprocess), so their I/O seams are
mocked in the home modules (`kontroll.catalog`/`kontroll.probe`/`kontroll.gitio`). Nothing spawns galaxy.py
or touches the repo/lab. The conftest sets GUI_PASSWORD before import (the GUI is fail-closed and won't
import a usable app without it).
"""
import base64
from types import SimpleNamespace

import pytest
import yaml

import app as gui          # gui/ is on sys.path (conftest); app.py is the GUI
from kontroll import catalog, gitio, paths, probe

pytestmark = pytest.mark.integration


def _auth(user="admin", pw="test-password"):
    """HTTP Basic auth header for the test creds (the conftest's GUI_USER/GUI_PASSWORD)."""
    raw = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
    return {"Authorization": "Basic " + raw}


@pytest.fixture
def client():
    """A Flask test client for the GUI app (TESTING mode), used to drive routes in-process."""
    gui.app.config["TESTING"] = True
    return gui.app.test_client()


@pytest.fixture
def mock_services(monkeypatch):
    """Patch the service I/O seams in their HOME modules so /api/search + /api/classify run offline:
    cisco.ios as a cliconf device via local_shallow (the redesigned search's fast default) and deep_probe
    (classify + the search deep opt-in), anything else absent, no Galaxy hits. Resets the GUI's cached
    catalog so the (real) registries reload fresh. Returns the collections DEEP-probed (empty for a plain
    shallow search — the perf win)."""
    probed = []

    def fake_probe(coll, version=None):
        probed.append(coll)
        f = probe._facts(coll, version or "5.0.0", "local", "deep")
        if coll != "ns.absent":
            f["plugins"] = {"cliconf": ["ios"]}
            f["modules"] = ["ios_command"]
        return f

    def fake_shallow(kw, limit):
        if not any("cisco" in k.lower() for k in kw):
            return []
        f = probe._facts("cisco.ios", "5.0.0", "local", "shallow")
        f["plugins"] = {"cliconf": ["ios"]}
        f["modules"] = ["ios_command"]
        return [f]

    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})
    monkeypatch.setattr(catalog, "local_shallow", fake_shallow)
    monkeypatch.setattr(probe, "deep_probe", fake_probe)
    monkeypatch.setattr(catalog, "galaxy_search", lambda kw, limit: [])
    monkeypatch.setattr(gui, "_CATALOG", None)
    return probed


@pytest.fixture
def onboard_env(tmp_repo, monkeypatch, make_facts):
    """The in-process onboard seams mocked, mirroring tests/integration/test_api_onboard.py::onb so the
    GUI and API onboard the SAME way: gitio._run recorded (commit_and_push goes through it), nothing
    installed, deep_probe canned as a cliconf device (ns.absent -> empty), and tmp_repo repoints
    paths.ROOT so the module/inventory/fleet writes land in a throwaway tree (backends/vectors still load
    from the real read-only tree). Resets the GUI's cached catalog so the registries reload fresh."""
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
    monkeypatch.setattr(gui, "_CATALOG", None)
    return SimpleNamespace(calls=calls, repo=tmp_repo)


# --- auth gate (fail-closed) ------------------------------------------------- #
def test_no_auth_is_401(client):
    """A request with no credentials is rejected 401 — the privileged surface is fail-closed."""
    assert client.get("/api/search?q=cisco").status_code == 401


def test_wrong_password_is_401(client):
    """Correct user but wrong password -> 401 (constant-time compare, no partial access)."""
    assert client.get("/api/search?q=cisco", headers=_auth(pw="wrong")).status_code == 401


def test_wrong_user_is_401(client):
    """Wrong username -> 401, even with a plausible password."""
    assert client.get("/api/search?q=cisco", headers=_auth(user="root")).status_code == 401


# --- the read API surface (in-process over the service layer) ---------------- #
def test_search_returns_records(client, mock_services):
    """/api/search runs the service layer in-process and returns the record list (cisco.ios, origin local,
    depth shallow, suggested backend) — the GUI is a thin shell over the service, not a subprocess scraper.
    The default search is SHALLOW, so it does NOT deep-probe (mock_services stays empty) — the perf win."""
    r = client.get("/api/search?q=cisco", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body[0]["collection"] == "cisco.ios" and body[0]["depth"] == "shallow"
    assert body[0]["suggested_backend"] == "netcommon_cli"
    assert mock_services == []                      # shallow search never deep-probes (fast path)


def test_search_deep_toggle_reprobes(client, mock_services):
    """/api/search?deep=true re-probes the local hit deeply (depth=deep) — the GUI 'deep' toggle (the
    search-deep testid) wired through to the service; the default stays shallow. Proves the toggle resolves
    the fast-path '?' cells on demand."""
    body = client.get("/api/search?q=cisco&deep=true", headers=_auth()).get_json()
    assert body[0]["depth"] == "deep" and mock_services == ["cisco.ios"]   # deep path DID deep-probe


def test_search_empty_query_short_circuits(client, mock_services):
    """An empty query returns [] WITHOUT calling the service — avoids a pointless probe/search."""
    r = client.get("/api/search?q=", headers=_auth())
    assert r.status_code == 200 and r.get_json() == []
    assert mock_services == []                      # never probed for an empty query


def test_classify_returns_structured(client, mock_services):
    """/api/classify returns the STRUCTURED service result (best backend + matches), not a scraped
    text blob — a cliconf device classifies to netcommon_cli."""
    r = client.get("/api/classify?collection=cisco.ios", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["best"] == "netcommon_cli" and "netcommon_cli" in body["matches"]


def test_classify_not_installed_404(client, mock_services):
    """/api/classify for a collection that isn't installed locally is a 404 (the service returns
    None) — a clean not-found, not a 200 with empty text."""
    assert client.get("/api/classify?collection=ns.absent", headers=_auth()).status_code == 404


# --- onboard (in-process over the service layer — the actuation path, mirrors the API route) ------ #
def test_onboard_missing_field_is_400(client):
    """/api/onboard rejects a request missing a required field (group/host) with 400, BEFORE any plan is
    built or git touched — input validation guards the privileged actuation path."""
    r = client.post("/api/onboard", json={"collection": "x", "key": "k"},  # no group/host
                    headers=_auth())
    assert r.status_code == 400
    assert "missing required field" in r.get_json()["error"]


def test_onboard_partial_auth_set_is_a_422_not_a_500(client, onboard_env, monkeypatch, tmp_path):
    """Parity with the API route: a planner guard's catchable ValueError — a partially-filled required auth_set
    (more reachable via F1 TIER-B's derived proxmox api-token set) or the MF-S2 domain-confinement refusal — is a
    clean 422 CLIENT error, NOT a 500 worker crash, with the code `invalid_request`, and it is audited as
    `reason=planner-guard` — not as a request-boundary refusal, which names a field. Guards the GUI onboard relay
    500-ing on a normal operator credential mistake, and the audit log blaming a field for a refusal that is about
    the combination of fields. The guard is pinned at the service layer (test_onboard_proxmox_auth.py); this pins
    the GUI route's mapping."""
    def _raise(*a, **k):
        raise ValueError("incomplete credential set 'proxmox_api' — provide all of "
                         "['api_user', 'api_token_id', 'api_token_secret'] together (missing ['api_token_secret'])")
    monkeypatch.setattr(gui, "build_onboard_plan", _raise)
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.post("/api/onboard",
                    json={"collection": "community.proxmox", "key": "proxmox",
                          "group": "hypervisors", "host": "192.0.2.40"}, headers=_auth())
    assert r.status_code == 422
    assert "incomplete credential set" in r.get_json()["error"]
    assert r.get_json()["code"] == "invalid_request"
    refusals = [ln for ln in (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines() if "onboard-refuse" in ln]
    assert len(refusals) == 1 and "reason=planner-guard" in refusals[0] and "request-boundary" not in refusals[0]


def test_onboard_dryrun_returns_plan_no_git(client, onboard_env):
    """A dry-run (apply omitted) returns the PLAN (backend, default host name, repo-rel paths) and touches
    NO git — dry-run by default is the security-relevant invariant, asserted as data."""
    r = client.post("/api/onboard",
                    json={"collection": "acme.edgeos", "key": "edgeos",
                          "group": "edge_router", "host": "192.0.2.50"},
                    headers=_auth())
    assert r.status_code == 200
    b = r.get_json()
    assert b["applied"] is False
    assert b["plan"]["backend"] == "netcommon_cli" and b["plan"]["host_name"] == "edgeos-1"
    assert b["plan"]["paths"]["module"].endswith("module.yml")
    assert onboard_env.calls == []                  # dry-run never touches git


def test_onboard_dryrun_creds_are_names_not_values(client, onboard_env):
    """A password in the request surfaces only as the cred VAR NAME in the plan — the value is NEVER in
    the response (it lives only in the in-memory plan for the apply step's SOPS encrypt)."""
    r = client.post("/api/onboard",
                    json={"collection": "acme.edgeos", "key": "edgeos", "group": "edge_router",
                          "host": "192.0.2.50", "password": "s3cret-pw"},
                    headers=_auth())
    assert "edgeos_1_password" in r.get_json()["plan"]["creds"]
    assert "s3cret-pw" not in r.get_data(as_text=True)   # the value never leaves the server


def test_onboard_apply_writes_commits_and_audits_no_creds(client, onboard_env, tmp_path, monkeypatch):
    """apply writes the three artifacts (module + drop-in host + fleet-enable) + commits the canonical
    (git mocked), and the audit records the action + cred VAR name but NEVER the value — the crown-jewel
    property, enforced at the highest-blast-radius route."""
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath))
    monkeypatch.setattr(gui, "_audit_logger", None)        # rebuild against the temp path
    r = client.post("/api/onboard",
                    json={"collection": "acme.edgeos", "key": "edgeos", "group": "edge_router",
                          "host": "192.0.2.50", "apply": True, "password": "SUPER-SECRET-PW"},
                    headers=_auth())
    b = r.get_json()
    assert b["applied"] and b["changed"] and b["committed"]
    assert (onboard_env.repo / "modules" / "edgeos" / "module.yml").exists()
    assert (onboard_env.repo / "ansible" / "inventory" / "onboarded-edgeos.yml").exists()
    joined = [" ".join(c) for c in onboard_env.calls]
    assert any("git add" in j for j in joined) and any("git commit" in j for j in joined)
    text = logpath.read_text(encoding="utf-8")
    assert "onboard-apply" in text and "edgeos_1_password" in text   # the action + var NAME recorded
    assert "SUPER-SECRET-PW" not in text                             # the value NEVER logged


def test_onboard_apply_stages_when_armed(client, onboard_env, monkeypatch):
    """With KONTROLL_STAGE_PUSHES set (the privileged deploy's propose-then-promote, C10), an apply STAGES
    `proposed/<run_id>` — never main. The GUI threads run_id through commit_and_push exactly like the API
    route, so the uniform-staging invariant holds: an abused GUI can only park a rejectable proposal."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    r = client.post("/api/onboard",
                    json={"collection": "acme.edgeos", "key": "edgeos",
                          "group": "edge_router", "host": "192.0.2.50", "apply": True},
                    headers=_auth())
    b = r.get_json()
    assert b["staged"] is True and b["target_ref"].startswith("proposed/")
    assert b["target_ref"].endswith(b["run_id"])
    joined = [" ".join(c) for c in onboard_env.calls]
    assert any("push local HEAD:refs/heads/proposed/" in j for j in joined)
    assert not any("HEAD:refs/heads/main" in j for j in joined)   # never main when staging


def test_onboard_apply_idempotent(client, onboard_env):
    """A second apply with identical inputs changes nothing and commits nothing (changed=False) — a
    re-onboard never duplicates files or pushes a redundant commit."""
    body = {"collection": "acme.edgeos", "key": "edgeos", "group": "edge_router",
            "host": "192.0.2.50", "apply": True}
    client.post("/api/onboard", json=body, headers=_auth())
    onboard_env.calls.clear()
    b = client.post("/api/onboard", json=body, headers=_auth()).get_json()
    assert b["applied"] is True and b["changed"] is False and b["committed"] is False
    assert not any("commit" in " ".join(c) for c in onboard_env.calls)   # nothing committed the 2nd time


# --- F1 (self-describing auth): the cred-fields read the onboard form fetches to render its inputs ------ #
def test_onboard_cred_fields_requires_auth(client):
    """GET /api/onboard/cred-fields with no creds is 401 — the read is auth-gated like every /api/* (it mirrors
    the privileged API route; even the value-free field SHAPE is gated)."""
    assert client.get("/api/onboard/cred-fields?backend=netcommon_cli").status_code == 401


def test_onboard_cred_fields_derives_per_backend_names_only(client):
    """GET /api/onboard/cred-fields returns the DERIVED descriptors for a backend — a network_cli switch gets SSH
    login (+ enable) and NO api token; an api/REST backend gets a token and NO ssh key (the headline win). NAMES
    only: no descriptor carries a value. Mirrors api/routes/onboard.onboard_cred_fields; the form fetches this on
    open to render the RIGHT inputs for the picked device instead of a static one-size-fits-all union."""
    sw = client.get("/api/onboard/cred-fields?backend=netcommon_cli", headers=_auth()).get_json()
    sw_names = {cf["field"] for cf in sw["cred_fields"]}
    assert sw["cred_source"] == "shallow" and {"username", "ssh_private_key", "enable_password"} <= sw_names
    assert "api_token" not in sw_names
    rest = client.get("/api/onboard/cred-fields?backend=api", headers=_auth()).get_json()
    rest_names = {cf["field"] for cf in rest["cred_fields"]}
    assert "api_token" in rest_names and "ssh_private_key" not in rest_names
    for cf in sw["cred_fields"] + rest["cred_fields"]:
        assert "value" not in cf and "secret_value" not in cf      # names-only on the wire


def test_onboard_cred_fields_unknown_backend_falls_back(client):
    """An unknown/blockless backend falls back to the generic union (cred_source=fallback) so the form is never
    blocked (INVARIANT D*) — guards a derivation gap dead-ending onboarding in the GUI."""
    b = client.get("/api/onboard/cred-fields?backend=no_such_backend", headers=_auth()).get_json()
    assert b["cred_source"] == "fallback"
    assert {cf["field"] for cf in b["cred_fields"]} == {"username", "password", "api_token", "ssh_private_key"}


# --- the standalone secondary-capability dialog (in-process over the service layer) ------------------- #
@pytest.fixture
def cap_class(tmp_repo, monkeypatch):
    """An onboarded class (demo_sw, cisco.ios, no metrics block) on a throwaway tree + a cliconf deep-probe,
    so the GUI capability routes run offline against the service layer without touching the real repo."""
    (tmp_repo / "modules" / "demo_sw").mkdir(parents=True)
    (tmp_repo / "modules" / "demo_sw" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo_sw", "collections": [{"name": "cisco.ios"}], "inventory_group": "core_switch"}),
        encoding="utf-8")

    def fake_probe(coll, version=None):
        f = probe._facts(coll, version or "5.0.0", "local", "deep")
        f["plugins"] = {"cliconf": ["ios"]}
        f["modules"] = ["ios_command"]
        return f
    monkeypatch.setattr(probe, "deep_probe", fake_probe)
    monkeypatch.setattr(gui, "_CATALOG", None)
    return tmp_repo


def test_capability_suggest_offers_addable_methods(client, cap_class):
    """GET /api/capability/telemetry/suggest returns the methods the class can ADD (snmp/host_node), the
    suggester cell, and the param allow-list — the Stage-0 detection wired through the GUI to the service."""
    r = client.get("/api/capability/telemetry/suggest?key=demo_sw", headers=_auth())
    assert r.status_code == 200
    names = {m["name"] for m in r.get_json()["offerable_methods"]}
    assert {"snmp", "host_node"} <= names


def test_capability_suggest_unknown_cap_and_unonboarded_are_404(client, cap_class):
    """An unregistered capability and an un-onboarded key are clean 404s — the surface is strictly-after
    onboarding (INVARIANT D*) and never 500s on a bad ?cap=/key."""
    assert client.get("/api/capability/nope/suggest?key=demo_sw", headers=_auth()).status_code == 404
    assert client.get("/api/capability/telemetry/suggest?key=ghost", headers=_auth()).status_code == 404


def test_capability_propose_is_pure(client, cap_class):
    """POST apply:false returns a plan + token and writes nothing — the review step through the GUI."""
    before = (cap_class / "modules" / "demo_sw" / "module.yml").read_text(encoding="utf-8")
    r = client.post("/api/capability/telemetry", headers=_auth(),
                    json={"key": "demo_sw", "selection": {"method": "host_node"}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["applied"] is False and body["token"] and body["enact"]
    assert (cap_class / "modules" / "demo_sw" / "module.yml").read_text(encoding="utf-8") == before


def test_capability_promote_commits_scoped_and_audits(client, cap_class, monkeypatch, tmp_path):
    """POST apply:true with the proposed token writes the block, commits ONLY the plan's paths (git mocked),
    and audits `capability-promote` with no value — the GUI's audited write path."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath))
    monkeypatch.setattr(gui, "_audit_logger", None)
    token = client.post("/api/capability/telemetry", headers=_auth(),
                        json={"key": "demo_sw", "selection": {"method": "host_node"}}).get_json()["token"]
    r = client.post("/api/capability/telemetry", headers=_auth(),
                    json={"key": "demo_sw", "apply": True, "token": token,
                          "selection": {"method": "host_node"}})
    assert r.status_code == 200 and r.get_json()["changed"] is True
    assert "- {method: host_node}" in (cap_class / "modules" / "demo_sw" / "module.yml").read_text(encoding="utf-8")
    add = next(c for c in calls if "add" in c)
    assert "modules/demo_sw/module.yml" in add and "-A" not in add
    assert "capability-promote" in logpath.read_text(encoding="utf-8")


@pytest.mark.parametrize("failing", ["add", "commit"])
def test_capability_promote_that_does_not_commit_is_an_error_not_a_success(client, cap_class, monkeypatch, tmp_path,
                                                                           failing):
    """When the stage does not commit — `git add` refuses a path, or `git commit` fails — the capability route
    answers 500 with an `error`, audits `committed=False` under the same run id, and issues no push. Guards the one
    route that did not read the seam's result: it answered 200 `{applied: true, changed: true, committed: false}`
    and the page printed "committed to main", with the paths and the enact steps, for a change that was staged
    nowhere."""
    calls = []

    def run(cmd, cwd=None, quiet_args=0):
        calls.append(list(cmd))
        return 1 if cmd[1] == failing else 0

    monkeypatch.setattr(gitio, "_run", run)
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath))
    monkeypatch.setattr(gui, "_audit_logger", None)
    body = {"key": "demo_sw", "selection": {"method": "host_node"}}
    token = client.post("/api/capability/telemetry", headers=_auth(), json=body).get_json()["token"]
    r = client.post("/api/capability/telemetry", headers=_auth(), json=dict(body, apply=True, token=token))
    assert r.status_code == 500
    answer = r.get_json()
    assert "commit failed" in answer["error"] and answer["run_id"] and "applied" not in answer
    assert [c[1] for c in calls if c[1] in ("commit", "push")] == (["commit"] if failing == "commit" else [])
    result = [ln for ln in logpath.read_text(encoding="utf-8").splitlines() if "capability-result" in ln]
    assert len(result) == 1 and "committed=False" in result[0] and answer["run_id"] in result[0]


def test_capability_reconfigure_propose_surfaces_the_diff(client, cap_class):
    """Re-proposing a DECLARED backup block with a CHANGED knob (retention keep-all → 90d) is a reconfigure
    (Phase 4a): the propose response carries `changes`/`will_overwrite`/`severity` (the diff the dialog renders
    + the overwrite-confirm keys off) — NOT an `already_declared` refusal. Guards the wall→reconfigure switch +
    that the route surfaces the field-level diff so the UI can gate Promote behind the right-weight confirm."""
    (cap_class / "modules" / "demo_bk").mkdir(parents=True)
    (cap_class / "modules" / "demo_bk" / "module.yml").write_text(
        "key: demo_bk\nrole: cisco_ios\ncollections:\n  - name: cisco.ios\ninventory_group: core_switch\n"
        'backup:\n  capable: true\n  schedule: "0 2 * * *"\n  retention: keep-all\n  destination: local\n',
        encoding="utf-8")
    sel = {"method": "backup", "params": {"schedule": "0 2 * * *", "retention": "90d"}}
    r = client.post("/api/capability/backup", headers=_auth(), json={"key": "demo_bk", "selection": sel})
    assert r.status_code == 200
    body = r.get_json()
    assert body["severity"] == "modify" and body["will_overwrite"] == ["backup.retention"]
    assert any(c["path"] == "backup.retention" and c["after"] == "90d" for c in body["changes"])


def test_capability_reconfigure_stale_token_409s_with_the_diff(client, cap_class, monkeypatch, tmp_path):
    """A reconfigure PROMOTE with a stale/wrong token is a 409 that NOW carries the change-set (G6) — so the UI
    shows 'here's what moved under you', not an opaque 'drifted'. Refuses BEFORE any git/commit (verify_token
    gate). Guards the anti-drift 409 surfacing the diff + that a bad token never reaches the write."""
    monkeypatch.setattr(gitio, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git must not run")))
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    (cap_class / "modules" / "demo_bk2").mkdir(parents=True)
    (cap_class / "modules" / "demo_bk2" / "module.yml").write_text(
        "key: demo_bk2\nrole: cisco_ios\ncollections:\n  - name: cisco.ios\ninventory_group: core_switch\n"
        'backup:\n  capable: true\n  schedule: "0 2 * * *"\n  retention: keep-all\n  destination: local\n',
        encoding="utf-8")
    r = client.post("/api/capability/backup", headers=_auth(),
                    json={"key": "demo_bk2", "apply": True, "token": "stale-token",
                          "selection": {"method": "backup", "params": {"schedule": "0 2 * * *", "retention": "90d"}}})
    assert r.status_code == 409
    body = r.get_json()
    assert "drifted" in body["error"] and any(c["path"] == "backup.retention" for c in body["changes"])


# --- module-IDENTITY reconfigure (Phase 4b, #139): the GUI surface, in-process over service/identity.py ------ #
_IDENTITY_MODULE = (
    "# hand-written, with comments\n"
    "key: demo_id\nstatus: active\ncollections:\n  - name: cisco.ios\n"
    "role: backend_network_cli\nbackend: network_cli\n"
    "inventory_group: core_switch   # the dispatch seam — must survive\nsecrets_domain: network\n")
_IDENTITY_CUR = {"status": "active", "role": "backend_network_cli", "backend": "network_cli",
                 "inventory_group": "core_switch", "secrets_domain": "network"}


def _write_identity_module(root, key="demo_id"):
    (root / "modules" / key).mkdir(parents=True)
    (root / "modules" / key / "module.yml").write_text(_IDENTITY_MODULE.replace("demo_id", key), encoding="utf-8")


def test_identity_propose_surfaces_the_identity_diff(client, cap_class):
    """POST /api/identity/<key> apply:false on a CHANGED identity key (inventory_group) surfaces the field diff
    with `severity: identity` (the descriptor knob_meta upgrade, design 22 §10) + `will_overwrite`, and writes
    nothing. Guards: a re-home rendered as a plain modify (under-gated — would skip the type-to-confirm), and the
    propose mutating the repo."""
    _write_identity_module(cap_class)
    before = (cap_class / "modules" / "demo_id" / "module.yml").read_text(encoding="utf-8")
    sel = {"values": dict(_IDENTITY_CUR, inventory_group="edge_firewall")}
    r = client.post("/api/identity/demo_id", headers=_auth(), json={"selection": sel})
    assert r.status_code == 200
    body = r.get_json()
    assert body["applied"] is False and body["severity"] == "identity" and body["token"]
    assert body["will_overwrite"] == ["inventory_group"]
    assert any(c["path"] == "inventory_group" and c["after"] == "edge_firewall" and c["severity"] == "identity"
               for c in body["changes"])
    assert (cap_class / "modules" / "demo_id" / "module.yml").read_text(encoding="utf-8") == before


def test_identity_promote_commits_module_only_and_audits_server_severity(client, cap_class, monkeypatch, tmp_path):
    """apply:true with the proposed token writes the identity key (comment-preserving), commits ONLY module.yml
    (the plan's scoped path — git mocked), and audits `identity-result` with the SERVER-derived severity (never a
    client field, M1). Guards the scoped commit + the server-side severity in the audit."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    _write_identity_module(cap_class)
    sel = {"values": dict(_IDENTITY_CUR, inventory_group="edge_firewall")}
    token = client.post("/api/identity/demo_id", headers=_auth(), json={"selection": sel}).get_json()["token"]
    r = client.post("/api/identity/demo_id", headers=_auth(),
                    json={"apply": True, "token": token, "selection": sel})
    assert r.status_code == 200 and r.get_json()["changed"] is True
    txt = (cap_class / "modules" / "demo_id" / "module.yml").read_text(encoding="utf-8")
    assert "inventory_group: edge_firewall" in txt and "# the dispatch seam" in txt   # written + comment kept
    add = next(c for c in calls if "add" in c)
    assert "modules/demo_id/module.yml" in add and "-A" not in add                    # scoped, not git add -A
    assert "severity=identity" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_identity_two_identity_keys_at_once_is_422(client, cap_class):
    """Changing two identity-severity keys in one proposal is a 422 (`multi_identity`) — one re-home/re-classify
    per proposal so the type-to-confirm is unambiguous. Guards a single confirm covering two re-classifies."""
    _write_identity_module(cap_class)
    sel = {"values": dict(_IDENTITY_CUR, inventory_group="edge_firewall", secrets_domain="semaphore")}
    assert client.post("/api/identity/demo_id", headers=_auth(), json={"selection": sel}).status_code == 422


def test_identity_stale_token_409s_with_the_diff_before_any_git(client, cap_class, monkeypatch, tmp_path):
    """A promote with a stale token is a 409 carrying the change-set (G6), refused BEFORE any git runs (the
    shared run_promote token gate). Guards a bad token reaching the identity write + the opaque-drift UX."""
    monkeypatch.setattr(gitio, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git must not run")))
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    _write_identity_module(cap_class, "demo_id2")
    sel = {"values": dict(_IDENTITY_CUR, inventory_group="edge_firewall")}
    r = client.post("/api/identity/demo_id2", headers=_auth(),
                    json={"apply": True, "token": "stale", "selection": sel})
    assert r.status_code == 409 and any(c["path"] == "inventory_group" for c in r.get_json()["changes"])


# --- inventory HOST-VAR reconfigure (Phase 4b, #139): the GUI surface, in-process over service/hostvars.py --- #
from kontroll.service.onboard import _INV_BANNER  # noqa: E402


def _write_host_drop(root, key="demo_sw", group="core_switch", host="sw-1"):
    d = root / "ansible" / "inventory"
    d.mkdir(parents=True, exist_ok=True)
    body = _INV_BANNER + yaml.safe_dump(
        {group: {"hosts": {host: {"ansible_host": "192.0.2.10", "device_role": "backend_network_cli"}}}},
        sort_keys=False)
    (d / ("onboarded-%s.yml" % key)).write_text(body, encoding="utf-8")


def test_host_propose_surfaces_the_var_diff(client, cap_class):
    """POST /api/host/<key>/<host> apply:false on a changed ansible_host surfaces the field diff (severity
    `modify` + will_overwrite) and writes nothing. Guards the host-var diff being surfaced for the overwrite-
    confirm + the propose staying pure."""
    _write_host_drop(cap_class)
    r = client.post("/api/host/demo_sw/sw-1", headers=_auth(),
                    json={"selection": {"values": {"ansible_host": "192.0.2.99"}}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["applied"] is False and body["severity"] == "modify" and body["token"]
    assert body["will_overwrite"] == ["ansible_host"]
    assert any(c["path"] == "ansible_host" and c["after"] == "192.0.2.99" for c in body["changes"])


def test_host_promote_commits_scoped_and_preserves_other_vars(client, cap_class, monkeypatch, tmp_path):
    """apply:true writes the new ansible_host (device_role preserved), commits ONLY the drop-in (git mocked),
    and audits `host-result` with the server-derived severity. Guards the scoped commit + that a re-IP doesn't
    drop the host's other vars."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    _write_host_drop(cap_class)
    sel = {"selection": {"values": {"ansible_host": "192.0.2.99"}}}
    token = client.post("/api/host/demo_sw/sw-1", headers=_auth(), json=sel).get_json()["token"]
    r = client.post("/api/host/demo_sw/sw-1", headers=_auth(),
                    json={"apply": True, "token": token, **sel})
    assert r.status_code == 200 and r.get_json()["changed"] is True
    txt = (cap_class / "ansible" / "inventory" / "onboarded-demo_sw.yml").read_text(encoding="utf-8")
    assert "192.0.2.99" in txt and "device_role: backend_network_cli" in txt
    add = next(c for c in calls if "add" in c)
    assert any("onboarded-demo_sw.yml" in a for a in add) and "-A" not in add
    assert "host-result" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_host_stale_token_409s_with_the_diff_before_any_git(client, cap_class, monkeypatch, tmp_path):
    """A host-var promote with a stale token is a 409 carrying the change-set, refused before any git runs (the
    shared run_promote token gate). Guards a bad token reaching the inventory write."""
    monkeypatch.setattr(gitio, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git must not run")))
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    _write_host_drop(cap_class)
    r = client.post("/api/host/demo_sw/sw-1", headers=_auth(),
                    json={"apply": True, "token": "stale", "selection": {"values": {"ansible_host": "192.0.2.99"}}})
    assert r.status_code == 409 and any(c["path"] == "ansible_host" for c in r.get_json()["changes"])


def test_configurable_inventory_host_projects_prefilled_editable_vars(client, cap_class):
    """GET /api/configurable?kind=inventory-host&id=<key>:<host> returns the `host` knob group with ansible_host
    pre-filled from the drop-in + severity `modify`; the TEMPLATED cred vars are absent (read-only). Guards the
    projection route + that no credential rides into the editable surface."""
    _write_host_drop(cap_class)
    r = client.get("/api/configurable?kind=inventory-host&id=demo_sw:sw-1", headers=_auth())
    assert r.status_code == 200
    knobs = r.get_json()["groups"][0]["knobs"]
    ah = next(k for k in knobs if k["key"] == "ansible_host")
    assert ah["default"] == "192.0.2.10" and ah["type"] == "ipv4" and ah["severity"] == "modify"
    assert client.get("/api/configurable?kind=inventory-host&id=demo_sw:ghost",
                      headers=_auth()).status_code == 404


# --- platform-settings EDIT (Phase 5, #140): the GUI surface, in-process over service/settings.py ----------- #
@pytest.fixture
def settings_repo(tmp_repo, monkeypatch):
    """An instance.yml + fleet.yml on a throwaway tree so the settings EDIT routes run offline."""
    (tmp_repo / "config" / "instance.yml").write_text(
        "mgmt_ip: 192.0.2.50   # the bind\ndomain: example.test\nbackup_remotes: []\n"
        "frontend:\n  tls_mode: self_signed\n", encoding="utf-8")
    (tmp_repo / "config" / "fleet.yml").write_text("enabled_modules:\n  - proxmox\n  - cisco_ios\n", encoding="utf-8")
    monkeypatch.setattr(gui, "_CATALOG", None)
    return tmp_repo


def test_settings_edit_propose_surfaces_the_diff_and_consequence(client, settings_repo):
    """POST /api/settings/mgmt_ip apply:false surfaces the field diff (severity `identity`) + the re-IP
    consequence text + writes nothing. Guards the danger-knob gate being fed the right severity + consequence."""
    before = (settings_repo / "config" / "instance.yml").read_text(encoding="utf-8")
    r = client.post("/api/settings/mgmt_ip", headers=_auth(),
                    json={"selection": {"values": {"mgmt_ip": "192.0.2.77"}}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["applied"] is False and body["severity"] == "identity" and body["token"]
    assert "DISCONNECT" in body["consequence"]
    assert any(c["path"] == "mgmt_ip" and c["after"] == "192.0.2.77" for c in body["changes"])
    assert (settings_repo / "config" / "instance.yml").read_text(encoding="utf-8") == before


def test_settings_edit_promote_stages_proposed_ref_and_audits_server_severity(client, settings_repo, monkeypatch, tmp_path):
    """apply:true (with KONTROLL_STAGE_PUSHES armed) writes instance.yml, commits ONLY it, STAGES proposed/<run_id>
    (C10 — never main), and audits `settings-result` with the SERVER-derived severity. Guards the C10 staging +
    the server-side severity in the audit."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    sel = {"selection": {"values": {"mgmt_ip": "192.0.2.77"}}}
    token = client.post("/api/settings/mgmt_ip", headers=_auth(), json=sel).get_json()["token"]
    r = client.post("/api/settings/mgmt_ip", headers=_auth(), json={"apply": True, "token": token, **sel})
    assert r.status_code == 200
    body = r.get_json()
    assert body["changed"] is True and body["staged"] is True and body["target_ref"].startswith("proposed/")
    assert "192.0.2.77" in (settings_repo / "config" / "instance.yml").read_text(encoding="utf-8")
    push = next(c for c in calls if "push" in c)
    assert any("proposed/" in a for a in push)                 # staged to proposed/<run_id>, never main
    assert "severity=identity" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_settings_edit_forbidden_and_unknown_knob_are_404(client, settings_repo):
    """A knob NOT in the editable descriptor set — api_privileged (the C10 arming switch, FORBID), source_of_truth
    (SURFACE), or a bogus key — is a 404 `unknown_knob`. Guards the FORBID boundary: the arming switch + .env have
    NO write route here."""
    for knob in ("api_privileged", "source_of_truth", "bogus"):
        r = client.post("/api/settings/%s" % knob, headers=_auth(),
                        json={"selection": {"values": {knob: "x"}}})
        assert r.status_code == 404, knob


def test_settings_fleet_disable_propose_and_stale_token(client, settings_repo, monkeypatch, tmp_path):
    """POST /api/settings/fleet/<module> apply:false surfaces a `remove` of the module + the drop-monitoring
    consequence; a promote with a stale token is a 409 with the diff, refused before any git. Guards the fleet
    removal flow + that a bad token can't reach the fleet.yml write."""
    r = client.post("/api/settings/fleet/proxmox", headers=_auth(), json={})
    assert r.status_code == 200 and r.get_json()["severity"] == "remove"
    assert "scrape targets" in r.get_json()["consequence"]
    monkeypatch.setattr(gitio, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git must not run")))
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    bad = client.post("/api/settings/fleet/proxmox", headers=_auth(), json={"apply": True, "token": "stale"})
    assert bad.status_code == 409 and any(c["path"] == "proxmox" for c in bad.get_json()["changes"])


def test_configurable_settings_knob_and_fleet_projections(client, settings_repo):
    """GET /api/configurable?kind=settings-knob projects one knob pre-filled (mgmt_ip ipv4, severity identity);
    kind=settings-fleet projects a no-knob removal dialog (the gate fires on Propose). Guards the projection
    route for the settings dialog."""
    r = client.get("/api/configurable?kind=settings-knob&id=mgmt_ip", headers=_auth())
    knob = r.get_json()["groups"][0]["knobs"][0]
    assert knob["default"] == "192.0.2.50" and knob["severity"] == "identity"
    fr = client.get("/api/configurable?kind=settings-fleet&id=proxmox", headers=_auth())
    assert fr.status_code == 200 and fr.get_json()["groups"] == []
    assert client.get("/api/configurable?kind=settings-fleet&id=ghost",
                      headers=_auth()).status_code == 404         # not-enabled module → 404


# --- secret-onboarding dialog (D): the GUI surface, in-process over service/secrets.py ------------------ #
from kontroll.service import secrets as secrets_service  # noqa: E402  (the dialog backend under test)


@pytest.fixture
def secret_env(monkeypatch):
    """Mock the secret seams so the GUI secret routes run offline: gitio._run recorded (commit_and_push goes
    through it), the sops decrypt/encrypt seams stubbed (no real sops/age), and `already-set` forced empty so
    generated/required behaviour is deterministic regardless of the real instance/secrets/ state."""
    calls = []
    written = {}
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {})
    monkeypatch.setattr(gitio, "sops_write_domain", lambda d, m: written.update({"domain": d, "map": dict(m)}) or True)
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    return SimpleNamespace(calls=calls, written=written)


def test_secrets_list_returns_domains(client, secret_env):
    """GET /api/secrets lists the registered secret-form domains (label + description) for the picker —
    names/labels only, never a value."""
    r = client.get("/api/secrets", headers=_auth())
    assert r.status_code == 200
    domains = {d["domain"] for d in r.get_json()}
    assert {"dashboards", "semaphore"} <= domains


def test_secret_fields_unknown_domain_404(client, secret_env):
    """A domain with no secret-form descriptor is a clean 404, never a 500."""
    assert client.get("/api/secrets/not_a_domain/fields", headers=_auth()).status_code == 404


# --- the uniform knob-group projection (Phase 3, #137): GET /api/configurable -------------------------- #

def test_configurable_secret_domain_projects_a_fields_group(client, secret_env):
    """GET /api/configurable?kind=secret-domain&id=<d> returns ONE `fields` group whose knobs are the projected
    secret fields (NAMES + set/unset, NEVER a value). Wires the secrets dialog through the one renderer; guards
    the projection route + that no secret value rides in the response (C11)."""
    r = client.get("/api/configurable?kind=secret-domain&id=dashboards", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    grp = body["groups"][0]
    assert grp["source_kind"] == "fields" and grp["testid"] == "secret-fields"
    knob = next(k for k in grp["knobs"] if k["key"] == "grafana_admin_password")
    assert knob["secret"] is True and knob["default"] is None and "value" not in knob


def test_configurable_capability_projects_param_groups_and_stage(client, cap_class):
    """GET /api/configurable?kind=capability:telemetry&id=<key> returns per-method `params` groups (enum knobs
    from the allow-list) + the resource_stage group (cap-telemetry-dashboards). Wires the cap dialog through the
    one renderer + closes the Stage-3 render-table gap; guards the projection route."""
    r = client.get("/api/configurable?kind=capability:telemetry&id=demo_sw", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    names = {m["name"] for m in body["methods"]}
    assert {"snmp", "host_node"} <= names
    snmp = next(m for m in body["methods"] if m["name"] == "snmp")
    assert snmp["group"]["source_kind"] == "params"
    assert snmp["group"]["knobs"][0]["type"] == "enum"          # widened renderer still server-projected
    assert body["resource_stage"]["testid"] == "cap-telemetry-dashboards"


def test_configurable_module_identity_projects_prefilled_identity_knobs(client, cap_class):
    """GET /api/configurable?kind=module-identity&id=<key> returns one `identity` knob group, each declared
    identity knob PRE-FILLED from the class's current module.yml value and carrying its `severity`/`blast_radius`
    (the §10 metadata the reconfigure gate reads). Guards the projection route + the renderer opening on current
    values, so a re-classify edits the live identity, not a blank form."""
    _write_identity_module(cap_class)
    r = client.get("/api/configurable?kind=module-identity&id=demo_id", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    grp = body["groups"][0]
    assert grp["source_kind"] == "identity"
    ig = next(k for k in grp["knobs"] if k["key"] == "inventory_group")
    assert ig["default"] == "core_switch" and ig["severity"] == "identity" and ig["confirm_text"]
    assert client.get("/api/configurable?kind=module-identity&id=ghost",
                      headers=_auth()).status_code == 404         # un-onboarded → 404, never 500


def test_configurable_unknown_kind_and_form_are_404(client, secret_env):
    """An unrecognized kind (the closed {secret-domain, capability:*} dispatch — synthesis M5 cut the universal
    registry) and an unknown secret form are clean 404s, never 500s. Guards the closed-dispatch + degrade
    posture."""
    assert client.get("/api/configurable?kind=device&id=x", headers=_auth()).status_code == 404
    assert client.get("/api/configurable?kind=secret-domain&id=not_a_domain", headers=_auth()).status_code == 404


def test_secret_apply_stages_and_never_leaks_value(client, secret_env, tmp_path, monkeypatch):
    """POST apply encrypts the values via the (mocked) sops seam, commits, and STAGES proposed/<run_id> when
    KONTROLL_STAGE_PUSHES is set — and the secret VALUE appears in neither the response nor the audit (only
    field NAMES). The crown-jewel no-leak property at the GUI secret surface."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath))
    monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.post("/api/secrets/dashboards", headers=_auth(),
                    json={"values": {"grafana_admin_password": "SUPER-SECRET-PW", "gui_admin_password": "gui-pw"},
                          "apply": True})
    assert r.status_code == 200
    b = r.get_json()
    assert b["committed"] and b["staged"] and b["target_ref"].startswith("proposed/")
    assert secret_env.written["map"]["grafana_admin_password"] == "SUPER-SECRET-PW"   # reached sops (in memory)
    assert len(secret_env.written["map"]["kontroll_api_token"]) == 64                 # generated on the box
    assert "SUPER-SECRET-PW" not in r.get_data(as_text=True)                          # never in the response
    text = logpath.read_text(encoding="utf-8")
    assert "secret-apply" in text and "grafana_admin_password" in text               # NAME recorded
    assert "SUPER-SECRET-PW" not in text                                             # VALUE never logged
    joined = [" ".join(c) for c in secret_env.calls]
    assert any("push local HEAD:refs/heads/proposed/" in j for j in joined)          # staged, not main


def test_secret_apply_missing_required_422(client, secret_env):
    """A required field with no value/generator/existing is a clean 422 — never a half-written domain."""
    r = client.post("/api/secrets/snmp_observability", headers=_auth(), json={"values": {}, "apply": True})
    assert r.status_code == 422


def test_secret_apply_overwrite_gate_and_enact_parity(client, secret_env, tmp_path, monkeypatch):
    """GUI parity with the API (the two surfaces must stay isomorphic): overwriting an already-set field fails
    CLOSED with 409 + the NAMES (no write); the ack (overwrite:true) applies and returns the post-promote
    `enact` hand-off; the audit carries the NAMES-only `overwrite=` marker, never a value."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath))
    monkeypatch.setattr(gui, "_audit_logger", None)
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    r = client.post("/api/secrets/dashboards", headers=_auth(),
                    json={"values": {"grafana_admin_password": "GUI-ROT-PW"}, "apply": True})
    assert r.status_code == 409 and "grafana_admin_password" in r.get_json()["overwrite"]
    assert secret_env.written == {}                                          # fail-closed: nothing encrypted
    assert "GUI-ROT-PW" not in r.get_data(as_text=True)
    r2 = client.post("/api/secrets/dashboards", headers=_auth(),
                     json={"values": {"grafana_admin_password": "GUI-ROT-PW"}, "apply": True, "overwrite": True})
    assert r2.status_code == 200 and r2.get_json()["changed"]
    enact = {e["field"]: e for e in r2.get_json()["enact"]}
    assert enact["grafana_admin_password"]["cmd"].startswith("docker exec -i grafana")
    assert "GUI-ROT-PW" not in r2.get_data(as_text=True)
    text = logpath.read_text(encoding="utf-8")
    assert "overwrite=grafana_admin_password" in text and "GUI-ROT-PW" not in text


def test_secret_dryrun_reports_overwrite_names(client, secret_env, monkeypatch):
    """The GUI dry-run reports the `overwrite` NAMES (what the JS gate keys off, index.html) — names only, never
    a value. Parity with the API's test_dryrun_reports_overwrite_names; closes the unpinned GUI dry-run path (C-5)."""
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    r = client.post("/api/secrets/dashboards", headers=_auth(), json={"values": {"gui_admin_password": "x"}})
    assert r.status_code == 200
    ow = r.get_json()["overwrite"]
    assert "gui_admin_password" in ow and "x" not in ow


def test_capability_promote_stale_token_is_409(client, cap_class):
    """apply:true with a token that doesn't match the recomputed plan is a 409 (anti-drift) — no write."""
    before = (cap_class / "modules" / "demo_sw" / "module.yml").read_text(encoding="utf-8")
    r = client.post("/api/capability/telemetry", headers=_auth(),
                    json={"key": "demo_sw", "apply": True, "token": "stale", "selection": {"method": "host_node"}})
    assert r.status_code == 409
    assert (cap_class / "modules" / "demo_sw" / "module.yml").read_text(encoding="utf-8") == before


# --- keygen dialog (D): the GUI surface, in-process over service/keygen.py --------------------------------- #
from kontroll.service import keygen as keygen_service  # noqa: E402  (the keygen backend under test)

# A canned apply result (a FAKE keypair) so the route tests are deterministic and independent of the real
# /.sops.yaml structure (that edit is the unit test's job). The private key is shaped, never real.
_FAKE_KEYGEN = {"error": None, "changed": True, "role": "break-glass",
                "public_key": "age1pubdummyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxq",
                "private_key": "AGE-SECRET-KEY-1FAKETESTPRIVATEKEYZZZ",
                "placement": "offline", "private_key_path": None,
                "recipient_domains": ["network", "proxmox", "dashboards"], "high_blast": True,
                "sops_updatekeys": "sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml",
                "next_steps": ["Store the shown private key OFFLINE on trusted media — it is not saved here.",
                               "Promote the staged proposal: `kontroll promote <run_id>`."],
                "paths": [".sops.yaml"]}


@pytest.fixture
def keygen_env(monkeypatch):
    """Mock the keygen seams so the GUI keygen routes run offline: gitio._run recorded (commit_and_push goes
    through it) and apply_keygen_plan returns the canned FAKE result (no real age-keygen / no real .sops.yaml
    write). The route under test is the one doing the audit + commit + staging."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(keygen_service, "apply_keygen_plan", lambda role: dict(_FAKE_KEYGEN, role=role))
    return SimpleNamespace(calls=calls)


def test_keygen_list_returns_roles(client):
    """GET /api/keygen lists the registered key roles (label + scope) for the picker — the registry, not a
    hardcoded list. Names/labels only, no key material."""
    r = client.get("/api/keygen", headers=_auth())
    assert r.status_code == 200
    roles = {x["role"] for x in r.get_json()}
    assert {"break-glass", "semaphore", "control"} <= roles


def test_keygen_plan_unknown_role_404(client):
    """A role with no descriptor is a clean 404, never a 500 (and it generates nothing)."""
    assert client.get("/api/keygen/not_a_role", headers=_auth()).status_code == 404


def test_keygen_requires_auth(client):
    """The keygen surface is privileged — no creds ⇒ 401 (it can mint a recovery key + stage a recipient)."""
    assert client.get("/api/keygen").status_code == 401
    assert client.post("/api/keygen/break-glass", json={"apply": True}).status_code == 401


def test_keygen_apply_shows_private_once_and_audit_commit_carry_only_public(client, keygen_env, tmp_path,
                                                                            monkeypatch):
    """POST apply returns the minted PRIVATE key exactly once (the show-once surface) and STAGES the recipient
    add as proposed/<run_id>; the audit log AND the commit message carry ONLY the public key — the private key
    appears nowhere but the response. This is the crown-jewel no-leak property at the keygen GUI surface."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath))
    monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.post("/api/keygen/break-glass", headers=_auth(), json={"apply": True})
    assert r.status_code == 200
    b = r.get_json()
    assert b["private_key"] == _FAKE_KEYGEN["private_key"]            # shown once, in the response
    assert b["public_key"] == _FAKE_KEYGEN["public_key"]
    assert b["staged"] and b["target_ref"].startswith("proposed/")   # recipient add staged, not main
    # The audit recorded the action + the PUBLIC key — never the private key.
    text = logpath.read_text(encoding="utf-8")
    assert "keygen-apply" in text and _FAKE_KEYGEN["public_key"] in text
    assert "AGE-SECRET-KEY" not in text
    # The commit (through gitio._run) never carries the private key in any argv.
    joined = " ".join(" ".join(c) for c in keygen_env.calls)
    assert "AGE-SECRET-KEY" not in joined
    assert "push local HEAD:refs/heads/proposed/" in joined          # staged, not main


def test_keygen_apply_commit_failure_still_shows_key_but_never_logs_it(client, keygen_env, tmp_path,
                                                                       monkeypatch):
    """The most security-sensitive failure branch: if `git commit` fails AFTER the recipient was written and
    the key minted, the route returns 500 *with* the private key (the operator must still capture the minted
    key — it is not recoverable) — but the audit line and every git argv carry only the public key, never the
    private. Guards a leak on the error path the happy-path test can't reach."""
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath))
    monkeypatch.setattr(gui, "_audit_logger", None)

    def run(cmd, cwd=None, quiet_args=0):
        keygen_env.calls.append(list(cmd))
        return 1 if (len(cmd) > 1 and cmd[1] == "commit") else 0     # fail only the commit step
    monkeypatch.setattr(gitio, "_run", run)
    r = client.post("/api/keygen/break-glass", headers=_auth(), json={"apply": True})
    assert r.status_code == 500
    assert r.get_json()["private_key"] == _FAKE_KEYGEN["private_key"]    # operator still gets the minted key
    assert "AGE-SECRET-KEY" not in logpath.read_text(encoding="utf-8")   # never logged/audited
    assert "AGE-SECRET-KEY" not in " ".join(" ".join(c) for c in keygen_env.calls)   # never in any git argv


def test_homepage_get_returns_the_board(client, monkeypatch):
    """GET /api/homepage returns the normalized board (read-only) and is auth-gated. Guards the Homepage editor's
    read round-trip + the auth boundary on the new surface (#136)."""
    from kontroll.service import homepage
    monkeypatch.setattr(homepage, "read_board",
                        lambda: {"title": "t", "sections": [{"name": "A", "items": [], "generated": False}]})
    assert client.get("/api/homepage").status_code == 401               # auth gate (no creds)
    r = client.get("/api/homepage", headers=_auth())
    assert r.status_code == 200 and r.get_json()["sections"][0]["name"] == "A"


def test_homepage_propose_writes_nothing_and_returns_diff_token(client, monkeypatch):
    """POST /api/homepage apply:false returns the diff + a plan_token and NEVER calls promote_board (it writes
    nothing) — the propose-purity boundary the operator reviews before staging. Guards a propose that secretly
    writes/stages (#136, C10: the GUI stages only on an explicit apply)."""
    from kontroll.service import homepage
    monkeypatch.setattr(homepage, "build_homepage_plan",
                        lambda board: {"error": None, "paths": ["p"],
                                       "diff": [{"file": "services.yaml", "unified": "@@ reorder @@"}],
                                       "summary": {"changed": True, "removed_tiles": []}, "plan_token": "tok"})
    called = []
    monkeypatch.setattr(homepage, "promote_board",
                        lambda b, t: called.append(1) or {"error": None, "changed": True, "paths": ["p"]})
    r = client.post("/api/homepage", headers=_auth(), json={"board": {"sections": [{"name": "A", "items": []}]}})
    assert r.status_code == 200
    j = r.get_json()
    assert j["applied"] is False and j["token"] == "tok" and j["diff"]
    assert called == []                                                 # promote_board NOT called on propose


# --- the backup VIEWER routes (#131, gap 1 / C14): the GUI's read-only secret-bearing capture surface ----------- #
import shutil  # noqa: E402

_HAVE_GIT = shutil.which("git") is not None
_requires_git = pytest.mark.skipif(not _HAVE_GIT, reason="git binary not available")
_SECRET = "$9$NEWsynthHASHnotrealBB"                          # a SYNTHETIC (real-shaped) hash the viewer must mask


@pytest.fixture
def backup_store(tmp_path, monkeypatch):
    """A throwaway captures git store (one tracked Cisco capture, 2 revs, carrying a synthetic secret hash) pointed
    at by KONTROLL_CAPTURES_DIR — so the GUI backup routes run against a real git repo without touching a real
    store. Returns the capture filename."""
    cap = tmp_path / "captures"
    cap.mkdir()
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    run = lambda *a: __import__("subprocess").run(["git", "-C", str(cap), *a], check=True, capture_output=True, env=env)
    run("init", "-q", "-b", "main")
    fn = "cisco_ios_192-0-2-2.cfg"
    (cap / fn).write_text("interface Vlan100\nusername admin secret 9 $9$OLDsynthHASHnotrealAA\n", encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "backup r1")
    (cap / fn).write_text("interface Vlan100\n description MGMT\nusername admin secret 9 %s\n" % _SECRET, encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "backup r2")
    monkeypatch.setenv("KONTROLL_CAPTURES_DIR", str(cap))
    return fn


@_requires_git
def test_backups_index_lists_and_audits(client, backup_store, tmp_path, monkeypatch):
    """GET /api/backups returns the capture-bearing device (available + the tracked file) and writes a NAMES/counts
    `backup-index` audit line — the index round-trip + the audit posture (reading the secret-bearing store is the
    auditable event)."""
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath)); monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.get("/api/backups", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True and any(d["file"] == backup_store for d in body["devices"])
    assert "backup-index" in logpath.read_text(encoding="utf-8")


@_requires_git
def test_backup_view_redacts_and_audits_no_value(client, backup_store, tmp_path, monkeypatch):
    """GET /api/backups/<file>/view?rev=<sha> returns the REDACTED capture (the synthetic secret masked, NOT in the
    response) AND writes a `backup-view` audit line carrying device+rev+redacted but NO capture value (G4/M5). The
    crown-jewel no-leak property at the highest-risk read route — asserted in BOTH the response and the audit."""
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath)); monkeypatch.setattr(gui, "_audit_logger", None)
    rev = client.get("/api/backups/%s/revisions" % backup_store, headers=_auth()).get_json()["revisions"][0]["rev"]
    r = client.get("/api/backups/%s/view?rev=%s" % (backup_store, rev), headers=_auth())
    assert r.status_code == 200 and r.get_json()["redacted"] is True
    assert _SECRET not in r.get_data(as_text=True)           # the secret never leaves the server unmasked
    text = logpath.read_text(encoding="utf-8")
    assert "backup-view" in text and ("rev=%s" % rev) in text and _SECRET not in text   # NAMES only, never a value


@_requires_git
def test_backup_diff_audits_no_value(client, backup_store, tmp_path, monkeypatch):
    """GET /api/backups/<file>/diff redact-then-diffs and audits `backup-diff` with the revs only — no capture
    value reaches the response or the audit (G4). Guards the diff route's no-leak + the redact-then-diff property at
    the HTTP boundary."""
    logpath = tmp_path / "audit.log"
    monkeypatch.setattr(gui, "AUDIT_LOG", str(logpath)); monkeypatch.setattr(gui, "_audit_logger", None)
    revs = client.get("/api/backups/%s/revisions" % backup_store, headers=_auth()).get_json()["revisions"]
    r = client.get("/api/backups/%s/diff?base=%s&compare=%s" % (backup_store, revs[1]["rev"], revs[0]["rev"]),
                   headers=_auth())
    assert r.status_code == 200
    assert _SECRET not in r.get_data(as_text=True) and "$9$OLDsynthHASHnotrealAA" not in r.get_data(as_text=True)
    text = logpath.read_text(encoding="utf-8")
    assert "backup-diff" in text and _SECRET not in text


@_requires_git
def test_backup_view_bad_rev_is_400_no_git_show(client, backup_store):
    """An invalid rev (`HEAD; rm -rf /`) is a clean 400 (rejected by the 7–40-hex guard before any `git show`, M2)
    — not a 500/stack trace. Guards rev injection at the route boundary."""
    r = client.get("/api/backups/%s/view?rev=%s" % (backup_store, "HEAD;%20rm%20-rf%20/"), headers=_auth())
    assert r.status_code == 400


def test_backups_absent_store_is_200_not_500(client, monkeypatch):
    """With no captures mount, GET /api/backups returns HTTP 200 {available:false} (NOT a 500/stack trace) — the
    graceful-absent route path (M6). A fresh node without the mount shows 'not mounted', never an error. Also pins
    the auth gate on the new surface."""
    monkeypatch.delenv("KONTROLL_CAPTURES_DIR", raising=False)
    assert client.get("/api/backups").status_code == 401      # auth-gated like every /api/* route
    r = client.get("/api/backups", headers=_auth())
    assert r.status_code == 200 and r.get_json()["available"] is False


# --- app-store UNIT dialog (create-unit): the GUI surface, in-process over service/actuation.py ------------- #
from kontroll.service import actuation as actuation_service  # noqa: E402  (the create backend under test)
from kontroll.service import units as units_service          # noqa: E402  (the Pick-stage list under test)

_UNIT_BODY = {"collection": "community.docker", "kind": "role", "name": "swarm", "version": "3.10.4",
              "blast_radius": "LAN"}
_MODULE_LOW = {"key": "docker_host", "inventory_group": "docker_hosts"}     # low-blast — creatable
_MODULE_HIGH = {"key": "cisco_ios", "inventory_group": "core_switch"}       # high-blast — Tier-capped


@pytest.fixture
def unit_env(tmp_repo, monkeypatch):
    """The in-process create-unit seams mocked: gitio._run recorded (commit_and_push goes through it), an active
    instance/ overlay, and the collection→class resolver + enabled set injected (community.docker → docker_host, a
    low-blast creatable class). Tests override module_for_collection for the Tier-cap case. Mirrors
    test_api_create_unit.py's fixture so the GUI and API author the SAME way."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    (tmp_repo / "instance").mkdir()                                  # overlay active → instance/actuation/<key>/…
    monkeypatch.setattr(catalog, "module_for_collection", lambda c: _MODULE_LOW)
    monkeypatch.setattr(actuation_service, "_enabled_modules", lambda: ["docker_host"])
    monkeypatch.setattr(gui, "_CATALOG", None)
    return SimpleNamespace(calls=calls, repo=tmp_repo, monkeypatch=monkeypatch)


def _unit_file(env):
    return env.repo / "instance" / "actuation" / "community-docker-swarm" / "unit.yml"


def test_units_list_requires_auth(client):
    """GET /api/units/<collection> with no creds is 401 — the Pick-stage read is auth-gated like every /api/*."""
    assert client.get("/api/units/cisco.ios").status_code == 401


def test_units_list_returns_runnable_units(client, monkeypatch, tmp_path):
    """GET /api/units/<collection> returns the collection's runnable units (in-process over service_units) and
    writes a NAMES/counts `unit-list` audit line. Guards the Pick-stage read round-trip + the audit posture."""
    monkeypatch.setattr(units_service, "service_units", lambda c: {
        "collection": c, "units": [{"kind": "role", "name": "swarm", "id": c + "/role/swarm"}]})
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log")); monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.get("/api/units/community.docker", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["collection"] == "community.docker" and body["units"][0]["name"] == "swarm"
    assert "unit-list" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_units_list_not_installed_404(client, monkeypatch):
    """A collection not installed locally → 404 (service_units returns None) — a clean not-found, not a 500."""
    monkeypatch.setattr(units_service, "service_units", lambda c: None)
    assert client.get("/api/units/ns.absent", headers=_auth()).status_code == 404


def test_unit_create_requires_auth(client):
    """POST /api/actuation/create with no creds is 401 — the create route is privileged (it WRITES a descriptor)."""
    assert client.post("/api/actuation/create", json=_UNIT_BODY).status_code == 401


def test_unit_create_propose_is_pure(client, unit_env):
    """apply:false returns the derived key + the unit.yml path + a token + the resolved target, writing no file and
    touching no git — the review step. Guards the propose/create boundary at the GUI route."""
    r = client.post("/api/actuation/create", headers=_auth(), json=_UNIT_BODY)
    assert r.status_code == 200
    body = r.get_json()
    assert body["applied"] is False and body["token"] and body["key"] == "community-docker-swarm"
    assert body["device_class"] == "docker_host" and body["version"] == "==3.10.4"
    assert not _unit_file(unit_env).exists() and unit_env.calls == []


def test_unit_create_tier_cap_is_403(client, unit_env):
    """A collection whose device-class targets core_switch/edge_firewall is a 403 — an unsigned app-store install
    may not be authored onto the highest-blast tier (the create-time Tier-cap; never-brick + blast-radius)."""
    unit_env.monkeypatch.setattr(catalog, "module_for_collection", lambda c: _MODULE_HIGH)
    unit_env.monkeypatch.setattr(actuation_service, "_enabled_modules", lambda: ["cisco_ios"])
    r = client.post("/api/actuation/create", headers=_auth(),
                    json=dict(_UNIT_BODY, collection="cisco.ios", name="ios_config"))
    assert r.status_code == 403 and "highest-blast" in r.get_json()["detail"]


def test_unit_create_apply_stages_scoped_and_audits(client, unit_env, tmp_path, monkeypatch):
    """A propose→create round-trip: apply:true with the proposed token writes instance/actuation/<key>/unit.yml,
    commits ONLY that path (git mocked; scoped, not `git add -A`), and audits `unit-create`. Guards the audited
    write path + the scoped commit at the GUI surface."""
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log")); monkeypatch.setattr(gui, "_audit_logger", None)
    proposed = client.post("/api/actuation/create", headers=_auth(), json=_UNIT_BODY).get_json()
    r = client.post("/api/actuation/create", headers=_auth(),
                    json=dict(_UNIT_BODY, apply=True, token=proposed["token"]))
    assert r.status_code == 200 and r.get_json()["committed"] is True
    assert "==3.10.4" in _unit_file(unit_env).read_text(encoding="utf-8")
    add = next(c for c in unit_env.calls if "add" in c)
    assert any(p.endswith("actuation/community-docker-swarm/unit.yml") for p in add) and "-A" not in add   # scoped
    assert "unit-create" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_unit_create_collision_409(client, unit_env):
    """A create whose derived key already has a descriptor on disk is a 409 (exists) — never clobber a configured
    unit (the #124 reuse-don't-overwrite lesson). Guards an overwrite of an existing app-store install."""
    _unit_file(unit_env).parent.mkdir(parents=True)
    _unit_file(unit_env).write_text("schema: 1\n", encoding="utf-8")
    assert client.post("/api/actuation/create", headers=_auth(), json=_UNIT_BODY).status_code == 409


def test_unit_create_stale_token_409(client, unit_env):
    """apply:true with a token that doesn't match the recomputed plan is a 409 and writes nothing — the anti-drift
    gate at the GUI route. Guards a create landing a descriptor the operator never reviewed."""
    r = client.post("/api/actuation/create", headers=_auth(), json=dict(_UNIT_BODY, apply=True, token="stale"))
    assert r.status_code == 409
    assert not _unit_file(unit_env).exists()
    assert not any("commit" in " ".join(c) for c in unit_env.calls)


def test_unit_create_stages_proposed_ref_when_armed(client, unit_env, monkeypatch):
    """With KONTROLL_STAGE_PUSHES set (the deployed staging posture), the create stages proposed/<run_id> — never
    main (C10). Guards the load-bearing two-key property: an abused GUI can only park a rejectable create proposal,
    never land a unit on main."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    proposed = client.post("/api/actuation/create", headers=_auth(), json=_UNIT_BODY).get_json()
    r = client.post("/api/actuation/create", headers=_auth(),
                    json=dict(_UNIT_BODY, apply=True, token=proposed["token"]))
    body = r.get_json()
    assert body["staged"] is True and body["target_ref"].startswith("proposed/")
    assert body["target_ref"].endswith(body["run_id"])
    joined = [" ".join(c) for c in unit_env.calls]
    assert any("push local HEAD:refs/heads/proposed/" in j for j in joined)
    assert not any("HEAD:refs/heads/main" in j for j in joined)   # never main when staging


# --- app-store UNIT dialog (configure an EXISTING unit): the Automations index + preview/stage, in-process ----- #
_CFG_KEY = "community-docker-swarm"
_REGISTERED_UNIT = {
    "schema": 1, "key": _CFG_KEY,
    "unit": {"kind": "role", "collection": "community.docker", "name": "swarm"},
    "install": {"collections": [{"name": "community.docker", "version": "==3.10.4"}],
                "provenance": {"source": "galaxy", "signature": "adaptive"}},
    "target": {"device_class": "docker_host", "inventory_group": "docker_hosts", "blast_radius": "LAN"},
    "knobs": [{"key": "listen_port", "label": "Listen port", "type": "int",
               "range": {"min": 1, "max": 65535}, "default": 2377},
              {"key": "advertise_addr", "label": "Advertise address", "type": "text", "pattern": "^[0-9.]+$"}],
}


@pytest.fixture
def unit_config_env(tmp_repo, monkeypatch):
    """A REGISTERED knobbed actuation unit on disk in an active instance/ overlay + gitio._run recorded — the
    configure flow READS instance/actuation/<key>/unit.yml and STAGES instance/actuation/<key>/vars.yml. The same
    registered unit the API's test_api_actuation.py configures, so the GUI and API configure it the SAME way."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    udir = tmp_repo / "instance" / "actuation" / _CFG_KEY
    udir.mkdir(parents=True)
    (udir / "unit.yml").write_text(yaml.safe_dump(_REGISTERED_UNIT, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(gui, "_CATALOG", None)
    return SimpleNamespace(calls=calls, repo=tmp_repo, monkeypatch=monkeypatch, dir=udir)


def _vars_file(env):
    return env.dir / "vars.yml"


def test_actuation_registry_requires_auth(client):
    """GET /api/actuation (the Automations index) with no creds is 401 — the registry read is auth-gated."""
    assert client.get("/api/actuation").status_code == 401


def test_actuation_stage_requires_auth(client):
    """POST /api/actuation/<key> with no creds is 401 — the configure write is privileged."""
    assert client.post("/api/actuation/" + _CFG_KEY, json={}).status_code == 401


def test_actuation_registry_lists_registered_units(client, unit_config_env, tmp_path, monkeypatch):
    """GET /api/actuation projects the registered unit's NON-SECRET summary (key/derived-target/`==`-pin/knob-count/
    configured) + audits unit-registry — the Automations-index read the configure dialog opens from. Guards the
    registry round-trip + that no value is ever surfaced (SEC-2)."""
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log")); monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.get("/api/actuation", headers=_auth())
    assert r.status_code == 200
    u = next(x for x in r.get_json()["units"] if x["key"] == _CFG_KEY)
    assert u["device_class"] == "docker_host" and u["inventory_group"] == "docker_hosts"
    assert u["pin"] == "==3.10.4" and u["knobs"] == 2 and u["configured"] is False
    assert "unit-registry" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_actuation_stage_propose_is_pure(client, unit_config_env):
    """apply:false returns the would-run PLAY (the role wrapper, hosts = the derived inventory_group) + the
    resolved vars + a token, writing no vars.yml and touching no git — the Review step. Guards the preview/stage
    boundary at the GUI configure route."""
    r = client.post("/api/actuation/" + _CFG_KEY, headers=_auth(), json={"values": {"listen_port": 3000}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["applied"] is False and body["token"]
    assert "hosts: docker_hosts" in body["play"] and "listen_port: 3000" in body["play"]
    assert body["vars"] == {"listen_port": 3000}
    assert not _vars_file(unit_config_env).exists() and unit_config_env.calls == []


def test_actuation_stage_apply_writes_vars_scoped_and_audits(client, unit_config_env, tmp_path, monkeypatch):
    """A propose→stage round-trip: apply:true with the proposed token writes instance/actuation/<key>/vars.yml,
    commits ONLY that path (git mocked; scoped, not `git add -A`), and audits unit-config-stage + unit-config-result.
    Guards the audited write path + the scoped commit at the GUI configure surface."""
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log")); monkeypatch.setattr(gui, "_audit_logger", None)
    proposed = client.post("/api/actuation/" + _CFG_KEY, headers=_auth(),
                           json={"values": {"listen_port": 3000}}).get_json()
    r = client.post("/api/actuation/" + _CFG_KEY, headers=_auth(),
                    json={"values": {"listen_port": 3000}, "apply": True, "token": proposed["token"]})
    assert r.status_code == 200 and r.get_json()["committed"] is True
    assert "listen_port: 3000" in _vars_file(unit_config_env).read_text(encoding="utf-8")
    add = next(c for c in unit_config_env.calls if "add" in c)
    assert any(p.endswith("actuation/%s/vars.yml" % _CFG_KEY) for p in add) and "-A" not in add   # scoped
    log = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "unit-config-stage" in log and "unit-config-result" in log


def test_actuation_stage_invalid_values_422(client, unit_config_env):
    """A value outside its knob's range is a 422 carrying the per-knob errors, writing nothing — the server-side
    configure gate (P0a) at the GUI route. Guards an un-validated value reaching a staged proposal."""
    r = client.post("/api/actuation/" + _CFG_KEY, headers=_auth(), json={"values": {"listen_port": 99999}})
    assert r.status_code == 422 and "listen_port" in r.get_json()["detail"]
    assert not _vars_file(unit_config_env).exists()


def test_actuation_stage_absent_unit_404(client, unit_config_env):
    """POST to a key with no registered descriptor is a 404 (no_unit) — a clean not-found, not a 500; the unknown
    key resolves against the registry before any path is built (the SEC-3 analog for the configure write path)."""
    assert client.post("/api/actuation/ns-absent-unit", headers=_auth(), json={"values": {}}).status_code == 404


def test_actuation_stage_stale_token_409(client, unit_config_env):
    """apply:true with a token that doesn't match the recomputed plan is a 409 and writes nothing — the anti-drift
    gate at the GUI configure route. Guards staging values the operator never reviewed."""
    r = client.post("/api/actuation/" + _CFG_KEY, headers=_auth(),
                    json={"values": {"listen_port": 3000}, "apply": True, "token": "stale"})
    assert r.status_code == 409
    assert not _vars_file(unit_config_env).exists()
    assert not any("commit" in " ".join(c) for c in unit_config_env.calls)


def test_actuation_stage_stages_proposed_ref_when_armed(client, unit_config_env, monkeypatch):
    """With KONTROLL_STAGE_PUSHES set (the deployed staging posture), the configure stage targets proposed/<run_id>
    — never main (C10). Guards the load-bearing two-key property: an abused GUI can only park a rejectable configure
    proposal, never land values on main."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    proposed = client.post("/api/actuation/" + _CFG_KEY, headers=_auth(),
                           json={"values": {"listen_port": 3000}}).get_json()
    r = client.post("/api/actuation/" + _CFG_KEY, headers=_auth(),
                    json={"values": {"listen_port": 3000}, "apply": True, "token": proposed["token"]})
    body = r.get_json()
    assert body["staged"] is True and body["target_ref"].startswith("proposed/")
    assert body["target_ref"].endswith(body["run_id"])
    joined = [" ".join(c) for c in unit_config_env.calls]
    assert any("push local HEAD:refs/heads/proposed/" in j for j in joined)
    assert not any("HEAD:refs/heads/main" in j for j in joined)   # never main when staging


# --- supply-chain provenance (MF-5): the read-only Index "Supply chain" surface --------------------------------- #
from kontroll.service import provenance as provenance_service   # noqa: E402  (the provenance read under test)


def test_provenance_requires_auth(client):
    """GET /api/provenance with no creds is 401 — the read is auth-gated like every /api/*."""
    assert client.get("/api/provenance").status_code == 401


def test_provenance_lists_classes_and_audits(client, monkeypatch, tmp_path):
    """GET /api/provenance returns the per-collection provenance + summary and audits `provenance-index` (counts by
    class — names only). The MF-5 surface: an unsigned-pinned collection is reported AMBER with the honest
    pin+checksum note, so a green 'installed' never implies a signature was checked."""
    monkeypatch.setattr(provenance_service, "fleet_provenance", lambda: {
        "available": True, "default_policy": "adaptive", "keyring_present": False,
        "collections": [{"name": "community.docker", "pin": "==4.0.0", "pin_kind": "exact", "policy": "adaptive",
                         "class": "unsigned-pinned", "digest_recorded": True, "note": "verified by pin + checksum"}],
        "summary": {"total": 1, "unsigned_pinned": 1, "signed": 0, "digests_recorded": 1}})
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log")); monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.get("/api/provenance", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["collections"][0]["class"] == "unsigned-pinned" and body["summary"]["unsigned_pinned"] == 1
    assert "provenance-index" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_provenance_unavailable_is_200_not_500(client, monkeypatch):
    """A not-yet-generated sidecar → available:false at HTTP 200 (never a 500/stack trace) — the GUI degrades."""
    monkeypatch.setattr(provenance_service, "fleet_provenance", lambda: {
        "available": False, "default_policy": None, "keyring_present": False, "collections": [],
        "summary": {"total": 0, "unsigned_pinned": 0, "signed": 0, "digests_recorded": 0}})
    r = client.get("/api/provenance", headers=_auth())
    assert r.status_code == 200 and r.get_json()["available"] is False


def test_image_provenance_requires_auth(client):
    """GET /api/image-provenance with no creds is 401 — the C1 image-supply-chain read is auth-gated like every /api/*."""
    assert client.get("/api/image-provenance").status_code == 401


def test_image_provenance_lists_classes_and_audits(client, monkeypatch, tmp_path):
    """GET /api/image-provenance returns the per-image class + summary and audits `image-provenance-index` (counts by
    class — names only). The §7.3 surface: a `digest-pinned` image is AMBER with the honest 'not a signature' note, so
    a digest-pin is never mistaken for a verified signature (cosign deferred)."""
    monkeypatch.setattr(provenance_service, "image_provenance", lambda: {
        "available": True, "signing_configured": False,
        "images": [{"name": "kontroll-control", "ref": "ghcr.io/example/kontroll-control", "tag": "v1",
                    "digest": "sha256:" + "a" * 64, "digest_short": "a" * 12, "class": "digest-pinned",
                    "note": "Docker verifies this @sha256: on every pull — not a signature"}],
        "summary": {"total": 1, "digest_pinned": 1, "local_build": 0, "signed": 0}})
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log")); monkeypatch.setattr(gui, "_audit_logger", None)
    r = client.get("/api/image-provenance", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["images"][0]["class"] == "digest-pinned" and body["summary"]["digest_pinned"] == 1
    assert "image-provenance-index" in (tmp_path / "audit.log").read_text(encoding="utf-8")


# --- /api/discovery (the discovery-inbox Rung-1b read route) ---------------------------------------------------
def test_discovery_no_auth_is_401(client):
    """GET /api/discovery with no creds is 401 — the read is auth-gated like every /api/*, since the inbox lists
    hosts the network sees (the privileged surface is fail-closed)."""
    assert client.get("/api/discovery").status_code == 401


def test_discovery_returns_inbox(client, monkeypatch):
    """GET /api/discovery returns read_inbox()'s payload verbatim — the route is a thin pure passthrough (mirroring
    /api/pending), and the GUI panel depends on the {generated_at, sources, candidates} shape. The local import in
    the route resolves the patched module attribute, so this pins the wiring without a real sweep artifact."""
    inbox = {"generated_at": "2026-07-03T00:00:00+00:00",
             "sources": [{"key": "opnsense", "method": "dhcp_leases_opnsense", "host": "192.0.2.1",
                          "status": "ok", "count": 1}],
             "candidates": [{"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer",
                             "source_key": "opnsense", "source_host": "192.0.2.1"}]}
    monkeypatch.setattr("kontroll.service.discovery.read_inbox", lambda: inbox)
    r = client.get("/api/discovery", headers=_auth())
    assert r.status_code == 200 and r.get_json() == inbox


def test_discovery_read_failure_is_500_not_a_crash(client, monkeypatch):
    """A read_inbox() that raises becomes a clean 500 (never a worker crash / a bare 'request failed') — the
    degrade-never-crash posture fleet/pending carry, so a malformed artifact can't take down the GUI worker."""
    def _boom():
        raise RuntimeError("bad artifact")
    monkeypatch.setattr("kontroll.service.discovery.read_inbox", _boom)
    r = client.get("/api/discovery", headers=_auth())
    assert r.status_code == 500 and "discovery read failed" in r.get_json()["error"]


# --- FF-race Move 2: the discard route (POST /api/pending/<run_id>/discard) ------------------------------------- #

def test_discard_no_auth_is_401(client):
    """POST /api/pending/<id>/discard with no creds is 401 — a WRITE surface is auth-gated at least as hard as the
    reads (the privileged surface is fail-closed; an unauthenticated user can't delete a staged proposal)."""
    assert client.post("/api/pending/abc123def456/discard").status_code == 401


def test_discard_success_passes_the_run_id_and_maps_the_result(client, monkeypatch):
    """A successful discard: the route hands the URL run_id to service.discard_proposal and returns its {sha, ref}
    as 200 `discarded:true`. Pins the wiring (the local import resolves the patched attribute) + the C10 property
    that this route holds NO promote path — un-stage is the whole job."""
    seen = {}
    def _fake(run_id, canonical=None):
        seen["run_id"] = run_id
        return {"ok": True, "run_id": run_id, "ref": "proposed/%s" % run_id, "sha": "deadbeef" * 5}
    monkeypatch.setattr("kontroll.service.discard.discard_proposal", _fake)
    r = client.post("/api/pending/abc123def456/discard", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert seen["run_id"] == "abc123def456"                    # the URL id reached the service verb
    assert body["discarded"] is True and body["ref"] == "proposed/abc123def456" and body["sha"]


@pytest.mark.parametrize("code,err", [(400, "invalid run_id"), (404, "no such proposal"), (409, "moved or vanished")])
def test_discard_error_codes_pass_through(client, monkeypatch, code, err):
    """The service's structured refusal codes map straight to the HTTP status — 400 (bad run_id), 404 (no such
    proposal / no canonical), 409 (a concurrent move/TOCTOU) — never a 500. Guards the route's error contract the
    front-end renders as `pending-discard-error`."""
    monkeypatch.setattr("kontroll.service.discard.discard_proposal",
                        lambda run_id, canonical=None: {"ok": False, "error": err, "code": code, "run_id": run_id})
    r = client.post("/api/pending/abc123def456/discard", headers=_auth())
    assert r.status_code == code and err in r.get_json()["error"]


def test_discard_audits_action_and_result_without_a_secret(client, monkeypatch):
    """The route emits a `pending-discard` action line + a `pending-discard-result` line correlated by an action
    run_id — names/sha only, never a secret (C10 audit discipline). Guards the audit surface the operator relies on
    to see who un-staged what."""
    lines = []
    monkeypatch.setattr(gui, "_audit", lambda action, detail="": lines.append((action, detail)))
    monkeypatch.setattr("kontroll.service.discard.discard_proposal",
                        lambda run_id, canonical=None: {"ok": True, "run_id": run_id,
                                                        "ref": "proposed/%s" % run_id, "sha": "abc" * 14})
    client.post("/api/pending/abc123def456/discard", headers=_auth())
    actions = [a for a, _ in lines]
    assert "pending-discard" in actions and "pending-discard-result" in actions
    assert all("abc123def456" in d for _, d in lines)          # the target run_id is correlated in both lines
    assert not any("password" in d.lower() or "secret" in d.lower() for _, d in lines)


def test_audit_flattens_injected_newlines_and_tabs(monkeypatch):
    """INJ-2 (audit-log injection): the shared GUI `_audit` flattens `\\t`/`\\n`/`\\r` in every field, so a
    caller-influenced value — e.g. the discard route audits the RAW URL run_id BEFORE validation — can never forge an
    extra TSV line (`\\n`) or corrupt the tab-delimited columns (`\\t`). Captures the real audit logger output and
    asserts the emitted line has exactly the 3 field separators and no injected newline/CR. Systemic: this protects
    EVERY `_audit` caller, not just discard (it is the same function `api/audit.write_audit` uses — kontroll/auditfmt.py)."""
    import logging
    records = []

    class _Cap(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    lg = gui._get_audit_logger()
    h = _Cap()
    lg.addHandler(h)
    try:
        with gui.app.test_request_context("/"):
            gui._audit("pending-discard", "target_run_id=abc\tdef\nmain run_id=x")   # a tab + newline injection attempt
    finally:
        lg.removeHandler(h)
    assert len(records) == 1
    msg = records[0]
    assert msg.count("\t") == 3, "exactly 3 TSV separators — no injected tab column: %r" % msg
    assert "\n" not in msg and "\r" not in msg, "no injected audit line: %r" % msg


def test_gui_onboard_boundary_refusal_is_a_422_with_a_field_code(client):
    """A request the C22 boundary refuses answers 422 with the readable message AND a machine-readable `code`
    derived from the refused FIELD (`collection` → `invalid_collection`, the device-class key →
    `invalid_device_class_key`), so the browser can render per-refusal next-steps copy instead of a bare error.
    The value itself never reaches the code (it can be the payload). Guards the GUI relay dropping the code — the
    permissive edition's install-offer copy keys on `invalid_collection`."""
    r = client.post("/api/onboard", json={"collection": "Bad Name", "key": "k", "group": "g", "host": "192.0.2.9"},
                    headers=_auth())
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "invalid_collection" and "namespace.name" in body["error"]
    r = client.post("/api/onboard", json={"collection": "acme.edgeos", "key": "../etc", "group": "g",
                                          "host": "192.0.2.9"}, headers=_auth())
    assert r.status_code == 422 and r.get_json()["code"] == "invalid_device_class_key"
    assert "../etc" not in r.get_json()["code"]
    assert "../etc" not in r.get_json()["error"], "the message names the field and the rule, never the value"


def test_gui_classify_refuses_a_name_that_is_not_a_collection(client, monkeypatch):
    """GET /api/classify hands its `collection` query value to the deep probe. An option-shaped value is 422 with
    the field's code; `ansible-doc` is never started and the value is not echoed. Guards the route's broad `except`
    turning a boundary refusal into "classify failed: …" with status 500 — and a request value on a command line."""
    def must_not_run(args):
        raise AssertionError("ansible-doc must not be started: %r" % (args,))
    monkeypatch.setattr(probe, "_ad", must_not_run)
    monkeypatch.setattr(gui, "_CATALOG", None)
    r = client.get("/api/classify?collection=--playbook-dir%3D/x", headers=_auth())
    assert r.status_code == 422, r.get_data(as_text=True)
    body = r.get_json()
    assert body["code"] == "invalid_collection" and "playbook-dir" not in body["error"]


@pytest.mark.parametrize("method, url, body", [
    ("post", "/api/capability/telemetry", {"key": "../modules/demo_sw", "selection": {"method": "host_node"}}),
    ("post", "/api/capability/telemetry", {"key": "../modules/demo_sw", "selection": {"method": "host_node"},
                                           "apply": True, "token": "x"}),
    ("post", "/api/capability/logging", {"key": "Demo_SW", "selection": {"method": "syslog"}}),
    ("post", "/api/capability/backup", {"key": "demo_sw\n", "selection": {"params": {"schedule": "daily"}}}),
    ("post", "/api/identity/Demo_SW", {"selection": {"values": {}}}),
    ("post", "/api/host/Demo_SW/sw1", {"selection": {"values": {}}}),
    ("get", "/api/capability/telemetry/suggest?key=..%2Fmodules%2Fdemo_sw", None),
    ("get", "/api/configurable?kind=module-identity&id=Demo_SW", None),
], ids=["telemetry-propose", "telemetry-promote", "logging-propose", "backup-propose", "identity", "host-vars",
        "suggest", "configurable"])
def test_a_bad_key_is_422_with_a_field_code_on_every_reconfigure_route(client, cap_class, monkeypatch, method, url,
                                                                       body):
    """Every GUI route that takes a device-class key and hands it to a plan builder answers a malformed key with 422,
    the field's code and a message that does not quote the value — and leaves exactly one `request-refuse` audit
    line naming the route and the field. Guards a boundary refusal reaching the operator as a bare 500 or as an
    exception string: none of these routes catches the refusal itself — the suggest and configurable routes wrap
    their service call in a broad `except` that used to turn it into "… failed: <message>", and the others have no
    `try` at all."""
    lines = []
    monkeypatch.setattr(gui, "_audit", lambda action, detail="": lines.append((action, detail)))
    r = getattr(client, method)(url, headers=_auth(), **({"json": body} if body is not None else {}))
    assert r.status_code == 422, (url, r.status_code, r.get_data(as_text=True))
    out = r.get_json()
    assert out["code"] == "invalid_device_class_key" and "demo_sw" not in out["error"].lower()
    refusals = [detail for action, detail in lines if action == "request-refuse"]
    assert len(refusals) == 1, lines
    assert "reason=request-boundary field=device-class key route=api_" in refusals[0]
    assert "demo_sw" not in refusals[0].lower()


def test_gui_capability_refuses_a_hostile_dashboard_name(client, cap_class):
    """The GUI's capability route accepts whatever JSON arrives (the page itself never sends `dashboards`). A
    dashboards item whose name would rewrite the class's inventory group is 422 on propose and on promote, and the
    module file is byte-identical afterwards. Guards the second credential that reaches the same writer."""
    module = cap_class / "modules" / "demo_sw" / "module.yml"
    before = module.read_text(encoding="utf-8")
    selection = {"method": "host_node",
                 "dashboards": [{"gnet": 1860, "name": "x}\ninventory_group: edge_firewall\n_pad: {a: 1"}]}
    for extra in ({}, {"apply": True, "token": "x"}):
        r = client.post("/api/capability/telemetry", headers=_auth(),
                        json={"key": "demo_sw", "selection": selection, **extra})
        assert r.status_code == 422, r.get_data(as_text=True)
        # the GUI answers 422 for EVERY plan error, so the status alone does not name this validator
        assert (r.get_json().get("detail") or "").startswith("dashboards[0].name"), r.get_data(as_text=True)
        assert "edge_firewall" not in r.get_data(as_text=True)
    assert module.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("value", ["null", "true", "123", "no", "2026-10-12", "2026-02-30", "0000-00-00"])
def test_settings_domain_that_yaml_reads_as_another_type_is_a_422_on_propose_and_on_promote(
        client, settings_repo, monkeypatch, tmp_path, value):
    """POST /api/settings/domain with a name YAML reads as a keyword, a number or a date answers 422 `bad_value`
    — on propose and on promote — and instance.yml is unchanged. Guards the route's 500: the validator said
    "valid", the scalar writer then refused with a plain ValueError, the route has no `try`, and on the promote
    path the `settings-promote` audit line had already been written with nothing after it."""
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    before = (settings_repo / "config" / "instance.yml").read_text(encoding="utf-8")
    sel = {"selection": {"values": {"domain": value}}}
    for body in (sel, {"apply": True, "token": "x", **sel}):
        r = client.post("/api/settings/domain", headers=_auth(), json=body)
        assert r.status_code == 422 and r.get_json()["error"] == "bad_value", (body, r.get_json())
    assert (settings_repo / "config" / "instance.yml").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("apply", [False, True])
def test_gui_onboard_onto_a_damaged_class_is_a_409_before_any_write(client, onboard_env, monkeypatch, tmp_path,
                                                                    apply):
    """The GUI twin: a second device of a class whose module file holds a group Ansible reserves answers 409
    `repository_data` with the file named — on the preview as on the apply — nothing is written or committed, and
    the audit log holds `repository-refuse what=module file` and no refused REQUEST.

    Guards the two apps drifting (a handler registered in one only is an uncaught exception, a bare 500, in the
    other), a class on `all` taking a second host, and the audit log blaming the caller for a file's fault."""
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)        # rebuild against the temp path
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.edgeos": "1.0.0"})
    root = onboard_env.repo
    (root / "modules" / "edgeos").mkdir(parents=True)
    (root / "modules" / "edgeos" / "module.yml").write_text(yaml.safe_dump({
        "key": "edgeos", "collections": [{"name": "acme.edgeos", "version": ">=1.0.0"}],
        "role": "backend_netcommon_cli", "backend": "netcommon_cli", "inventory_group": "all",
        "secrets_domain": "network"}), encoding="utf-8")
    r = client.post("/api/onboard", headers=_auth(), json={
        "collection": "acme.edgeos", "key": "edgeos", "group": "edge_router", "host": "192.0.2.50", "apply": apply})
    assert r.status_code == 409, r.get_data(as_text=True)
    body = r.get_json()
    assert body["code"] == "repository_data" and "modules/edgeos/module.yml" in body["error"]
    assert not (root / "ansible" / "inventory" / "onboarded-edgeos.yml").exists()
    assert onboard_env.calls == []
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "repository-refuse" in audit and "what=module file" in audit
    assert "request-refuse" not in audit and "onboard-refuse" not in audit


def test_gui_commit_body_names_the_group_the_plan_adopted(client, onboard_env, monkeypatch):
    """The GUI twin: a reuse-onboard that names another group than the class's own commits with the ADOPTED group
    in its body. Guards a commit message that says a host is in a group it is not in."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.edgeos": "1.0.0"})
    root = onboard_env.repo
    (root / "modules" / "edgeos").mkdir(parents=True)
    (root / "modules" / "edgeos" / "module.yml").write_text(yaml.safe_dump({
        "key": "edgeos", "collections": [{"name": "acme.edgeos", "version": ">=1.0.0"}],
        "role": "backend_netcommon_cli", "backend": "netcommon_cli", "inventory_group": "edge_router",
        "secrets_domain": "network"}), encoding="utf-8")
    r = client.post("/api/onboard", headers=_auth(), json={
        "collection": "acme.edgeos", "key": "edgeos", "group": "some_other_group", "host": "192.0.2.50",
        "apply": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    commit = next(c for c in onboard_env.calls if c[:2] == ["git", "commit"])
    assert any("in group edge_router" in word for word in commit)
    assert not any("some_other_group" in word for word in commit)


def test_gui_identity_refuses_a_reserved_group_as_bad_value(client, cap_class):
    """`POST /api/identity/<key>` with `inventory_group: all` answers 422 `bad_value` and the module file is
    unchanged. Guards the one request that could re-home an existing class onto every host."""
    before = (cap_class / "modules" / "demo_sw" / "module.yml").read_text(encoding="utf-8")
    r = client.post("/api/identity/demo_sw", headers=_auth(), json={"selection": {"values": {"inventory_group": "all"}}})
    assert r.status_code == 422 and r.get_json()["error"] == "bad_value"
    assert (cap_class / "modules" / "demo_sw" / "module.yml").read_text(encoding="utf-8") == before


def test_gui_cred_fields_route_checks_the_domain_it_answers_with(client):
    """The GUI's cred-fields route refuses a secrets domain that is not a name (422, the field's code). Guards the
    same unread value as the API's route."""
    r = client.get("/api/onboard/cred-fields?backend=netcommon_cli&secrets=../x", headers=_auth())
    assert r.status_code == 422 and r.get_json()["code"] == "invalid_secrets_domain"


@pytest.mark.parametrize("route,body", [
    ("/api/capability/telemetry", {"key": "demo_sw", "selection": {"method": "host_node"}}),
    ("/api/identity/demo_id", {"selection": {"values": dict(_IDENTITY_CUR, inventory_group="edge_firewall")}}),
])
@pytest.mark.parametrize("apply", [False, True], ids=["propose", "promote"])
def test_gui_write_target_that_does_not_confine_is_a_409_on_every_reconfigure(client, cap_class, monkeypatch,
                                                                              tmp_path, route, body, apply):
    """The GUI twins: when the confinement refuses the module target, the capability and the identity routes
    answer 409 `repository_data` on the propose and on the promote, the module files are unchanged, nothing is
    committed, and the audit log holds `repository-refuse what=path` with the route.

    Guards the next route-local `except` around a plan builder in the app that has several broad ones: the type
    is mapped once, and none of them may turn it into a 500 carrying the sentence or into a refused field."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    _write_identity_module(cap_class)
    if apply:
        proposed = client.post(route, headers=_auth(), json=body)
        assert proposed.status_code == 200, proposed.get_data(as_text=True)
        body = dict(body, apply=True, token=proposed.get_json()["token"])
    before = {p: p.read_text(encoding="utf-8") for p in (cap_class / "modules").rglob("module.yml")}

    def refuse(rel_path):
        raise paths.RepositoryDataError("path", "a write target does not resolve inside the repository")
    monkeypatch.setattr(paths, "confined", refuse)
    r = client.post(route, headers=_auth(), json=body)
    assert r.status_code == 409 and r.get_json()["code"] == "repository_data", r.get_data(as_text=True)
    assert {p: p.read_text(encoding="utf-8") for p in (cap_class / "modules").rglob("module.yml")} == before
    assert not any("commit" in c for c in calls)
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "repository-refuse" in audit and "what=path" in audit and "route=api_" in audit


@pytest.mark.parametrize("word", ["localhost", "all", "127.0.0.1"])
def test_gui_onboard_refuses_a_reserved_host_name(client, onboard_env, word):
    """The GUI twin: `host_name` set to a name Ansible reserves — a loopback address is one — is a 422
    `invalid_host_name`, and nothing is written or committed. Guards the two apps drifting on the field next to
    the group."""
    r = client.post("/api/onboard", headers=_auth(), json={
        "collection": "acme.edgeos", "key": "edgeos", "group": "edge_router", "host": "192.0.2.50",
        "host_name": word, "apply": True})
    assert r.status_code == 422 and r.get_json()["code"] == "invalid_host_name", r.get_data(as_text=True)
    assert not (onboard_env.repo / "modules" / "edgeos").exists() and onboard_env.calls == []


@pytest.mark.parametrize("url,target", [
    ("/api/classify?collection=acme.edgeos", "app.service_classify"),
    ("/api/configurable?kind=module-identity&id=demo_sw", "kontroll.service.configurable.configurable_view"),
    ("/api/capability/telemetry/suggest?key=demo_sw", "kontroll.service.capability.suggest_view"),
    ("/api/units/acme.edgeos", "kontroll.service.units.service_units"),
])
def test_gui_read_route_answers_a_repository_refusal_as_one(client, monkeypatch, tmp_path, url, target):
    """Each GUI read route that wraps its service call in a broad `except` answers a `RepositoryDataError` raised
    below it as 409 `repository_data` — the app-wide handler's answer, with its audit line — and not as a
    "failed" 500 that carries the exception's text.

    Guards "mapped once per app" at the routes that could un-map it: no read path raises the type today, so only
    this test notices when a re-raise is dropped and the `except Exception` that follows relabels the refusal."""
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)

    def refuse(*args, **kwargs):
        raise paths.RepositoryDataError("path", "a write target does not resolve inside the repository")
    monkeypatch.setattr(target, refuse)          # the name each route calls, where the route finds it
    r = client.get(url, headers=_auth())
    assert r.status_code == 409 and r.get_json()["code"] == "repository_data", r.get_data(as_text=True)
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "repository-refuse" in audit and "what=path" in audit


def test_gui_identity_on_a_module_file_that_is_not_a_mapping_is_a_409(client, cap_class, monkeypatch, tmp_path):
    """`POST /api/identity/<key>` for a class whose module file parses to a list answers 409 `repository_data`
    with the file named and the audit line `repository-refuse what=module file`. Guards the 500 it was, and the
    identity route answering a damaged file differently from the onboard route."""
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    (cap_class / "modules" / "demo_sw" / "module.yml").write_text("- key: demo_sw\n", encoding="utf-8")
    r = client.post("/api/identity/demo_sw", headers=_auth(), json={"selection": {"values": {"role": "backend_x"}}})
    assert r.status_code == 409 and r.get_json()["code"] == "repository_data", r.get_data(as_text=True)
    assert "modules/demo_sw/module.yml" in r.get_json()["error"]
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "repository-refuse" in audit and "what=module file" in audit
