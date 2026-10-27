"""The API onboard route — the highest-blast-radius privileged mutation over HTTP (ws3 deferred,
now built). Dry-run returns the plan; apply writes the module + drop-in host + fleet-enable and
commits + pushes the local canonical (git mocked, tmp_repo). The security-critical properties:
token-gated, dry-run by default, credentials NEVER returned or logged (only var names), a re-onboard
with no change commits nothing, and the action is audited. docs/api-architecture.md §3/§8.
"""
import dataclasses
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.audit import read_audit
from api.main import create_app
from api.settings import Settings
from kontroll import catalog, gitio, paths, probe

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
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: calls.append(["kontroll-land", "main"]) or 0)
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
    """apply writes the three artifacts (module + drop-in host + fleet-enable), commits, and LANDS on the LOCAL
    canonical through `gitio.land_main` (both seams mocked: `_run` and `land_main`) — never a `git push` of `main`,
    which the canonical's hook refuses from everyone — the full self-completing onboard, audited."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(apply=True))
    assert r.status_code == 200
    b = r.json()
    assert b["applied"] and b["changed"] and b["committed"] and b["pushed_canonical"]
    assert (onb.repo / "modules" / "edgeos" / "module.yml").exists()
    assert (onb.repo / "ansible" / "inventory" / "onboarded-edgeos.yml").exists()
    assert "- edgeos" in (onb.repo / "config" / "fleet.yml").read_text(encoding="utf-8")
    joined = [" ".join(c) for c in onb.calls]
    assert any("git add" in j for j in joined) and any("git commit" in j for j in joined)
    assert "kontroll-land main" in joined and not any("push local" in j for j in joined)
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
    # S28: the SSH port box — inline (written into the drop-in as an int, never stored in SOPS), never secret
    port = next(cf for cf in b["cred_fields"] if cf["field"] == "port")
    assert port["kind"] == "port" and port["inline"] is True and port["secret"] is False
    assert port["maps_to"] == "ansible_port"


def test_cred_fields_for_a_rest_backend_offers_a_token_not_an_ssh_box(onb):
    """THE HEADLINE WIN at the wire: GET /onboard/cred-fields?backend=api derives an api_token and NO
    ssh_private_key — so a REST device's form stops showing a dead SSH box (the operator's exact gap)."""
    b = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": "api"}).json()
    names = {cf["field"] for cf in b["cred_fields"]}
    assert "api_token" in names and "ssh_private_key" not in names
    assert "port" not in names            # S28: an api host's probe port is the backend's, never an operator's box


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


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
@pytest.mark.parametrize("backend", ["napalm", "vendor_config", "zzz"])
def test_onboard_a_backend_nothing_can_run_is_a_422_and_audited(onb, backend, apply):
    """A `backend` that declares no execution role (`napalm`, `vendor_config`) or that the registry does not
    ship (`zzz`) is answered 422 with the code `invalid_backend`, a message that does not quote it, nothing written and
    git untouched — on a dry-run and on an apply — and leaves one `onboard-refuse` audit line naming the field, never
    the value. Guards API parity with the planner's refusal (S26): the route answered 200 with a plan whose
    `device_role` (`backend_napalm`) no play can include, and an apply wrote it."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(backend=backend, apply=apply))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_backend" and backend not in r.json()["detail"]
    assert onb.calls == [] and not (onb.repo / "modules" / "edgeos").exists()
    audit = open(onb.audit_log, encoding="utf-8").read()
    assert "onboard-refuse" in audit and "reason=request-boundary field=backend" in audit
    assert backend not in audit.replace("onboard-refuse", "")


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
@pytest.mark.parametrize("port", ["{{ lookup('pipe', 'id') }}", "65536", "22x"])
def test_onboard_bad_port_is_a_422_and_writes_nothing(onb, port, apply):
    """A typed SSH port that is not a TCP port is answered 422 with the code `invalid_port` and a message that does
    not quote it, nothing written and git untouched — on a dry-run and on an apply — and one `onboard-refuse` audit
    line naming the field, never the value. Guards S28's new path, the first on which a request value lands in an
    inventory host var: a Jinja value there would be templated on the control node by the next play (MF-02 asked
    for this exact audit shape; a bare ValueError would have read `field=input`)."""
    r = onb.client.post("/onboard", headers=AUTH,
                        json=_body(creds={"username": "admin", "port": port}, apply=apply))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_port" and port not in r.json()["detail"]
    assert onb.calls == [] and not (onb.repo / "modules" / "edgeos").exists()
    audit = open(onb.audit_log, encoding="utf-8").read()
    assert "onboard-refuse" in audit and "reason=request-boundary field=port" in audit
    assert port not in audit


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_onboard_of_an_unrecognised_shape_is_a_422_naming_the_gap_and_audited(onb, monkeypatch, make_facts, apply):
    """An onboard of a collection that carries no signal a backend classifies on (a WinRM shape: modules only) is
    answered 422 with the sentence that names what it showed and the remedy, nothing written and git untouched, and
    one `onboard-refuse reason=no-backend` audit line with nothing from the collection in it. Guards the S24 misroute —
    such a collection was planned as raw_ssh and an apply wrote it — and a refusal that leaves no trace."""
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, modules=["win_ping", "win_shell"]))
    r = onb.client.post("/onboard", headers=AUTH, json=_body(collection="ansible.windows", key="win", apply=apply))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "no_backend"            # key on the code, not the status (api/README)
    assert r.json()["detail"] == (
        "no backend kontroll can run fits ansible.windows: nothing in it is a signal a backend classifies on "
        "(plugin types: none; 2 modules) — if it is a plain SSH host, onboard it from the Generic SSH host search "
        "result (search: ssh), or pass an explicit backend")
    assert onb.calls == [] and not (onb.repo / "modules" / "win").exists()
    audit = [ln for ln in open(onb.audit_log, encoding="utf-8").read().splitlines() if "onboard-refuse" in ln]
    assert len(audit) == 1 and audit[0].endswith("reason=no-backend") and "windows" not in audit[0]


def _damage_a_class(repo, **identity_keys):
    """Seed an onboarded class (`edgeos`, acme.edgeos) whose module file holds the given identity keys."""
    import yaml
    module = {"key": "edgeos", "collections": [{"name": "acme.edgeos", "version": ">=1.0.0"}],
              "role": "backend_netcommon_cli", "backend": "netcommon_cli",
              "inventory_group": "edge_router", "secrets_domain": "network"}
    module.update(identity_keys)
    (repo / "modules" / "edgeos").mkdir(parents=True)
    (repo / "modules" / "edgeos" / "module.yml").write_text(yaml.safe_dump(module), encoding="utf-8")


@pytest.mark.parametrize("apply", [False, True])
def test_onboard_onto_a_damaged_class_is_a_409_before_any_write(onb, monkeypatch, apply):
    """A second device of a class whose module file holds a secrets domain that is not a name answers **409**
    `repository_data` — on the dry-run as on the apply — with the file named and the value not; nothing is
    written or committed, and the audit log holds `repository-refuse what=module file`, no refused REQUEST.

    Guards the answer this used to get: 422 `invalid_secret_domain`, a field the caller never sent, raised at
    apply after the inventory drop-in had been written (and the fleet line, where the class was not enabled yet)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.edgeos": "1.0.0"})
    _damage_a_class(onb.repo, secrets_domain="a" * 80)
    def files():                                          # the tree, without this test's own audit log
        return sorted(str(p.relative_to(onb.repo)) for p in onb.repo.rglob("*")
                      if p.is_file() and p.relative_to(onb.repo).parts[0] != "audit")
    before = files()
    r = onb.client.post("/onboard", headers=AUTH, json=_body(apply=apply, password="pw"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "repository_data" and "modules/edgeos/module.yml" in r.json()["detail"]
    assert "a" * 80 not in r.text
    assert files() == before
    assert onb.calls == []
    audit = open(onb.audit_log, encoding="utf-8").read()
    assert "repository-refuse" in audit and "what=module file" in audit
    assert "request-refuse" not in audit and "reason=request-boundary" not in audit


def test_a_write_target_that_does_not_confine_is_a_409_and_is_audited(onb, monkeypatch):
    """When the confinement refuses a target the server built — here every target, as a symlinked overlay that
    resolves outside the write root would — an onboard apply answers 409 `repository_data`, writes nothing, and
    leaves `repository-refuse what=path` in the audit log.

    Guards the answer of the app-wide handler for the type, and its audit line. (That the question comes before
    the first write is held by the unit test of `apply_onboard_plan`: here every target is refused, so the first
    writer would refuse as well.)"""
    def refuse(rel_path):
        raise paths.RepositoryDataError("path", "a write target does not resolve inside the repository")
    monkeypatch.setattr(paths, "confined", refuse)
    r = onb.client.post("/onboard", headers=AUTH, json=_body(apply=True))
    assert r.status_code == 409 and r.json()["code"] == "repository_data"
    assert not (onb.repo / "modules" / "edgeos").exists() and onb.calls == []
    audit = open(onb.audit_log, encoding="utf-8").read()
    assert "repository-refuse" in audit and "what=path" in audit and "route=/onboard" in audit


def test_the_cred_fields_route_checks_the_domain_it_answers_with(onb):
    """`GET /onboard/cred-fields` refuses a secrets domain that is not a name (422, the field's code) and answers
    a plain one. Guards a value that is written into every descriptor of a 200 answer being taken unread."""
    r = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": "netcommon_cli", "secrets": "../x"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_secrets_domain"
    ok = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": "netcommon_cli", "secrets": "lab"})
    assert ok.status_code == 200 and ok.json()["secrets"] == "lab"


def test_the_commit_body_names_the_group_the_plan_adopted(onb, monkeypatch):
    """A reuse-onboard that names another group than the class's own commits with the ADOPTED group in its body.
    Guards a commit message that says a host is in a group it is not in."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.edgeos": "1.0.0"})
    _damage_a_class(onb.repo)                                  # a healthy class: nothing is damaged here
    r = onb.client.post("/onboard", headers=AUTH, json=_body(apply=True, group="some_other_group"))
    assert r.status_code == 200, r.text
    commit = next(c for c in onb.calls if c[:2] == ["git", "commit"])
    assert any("in group edge_router" in word for word in commit)
    assert not any("some_other_group" in word for word in commit)


@pytest.mark.parametrize("word", ["localhost", "all", "ungrouped", "127.0.0.1", "::1"])
def test_onboard_refuses_a_reserved_host_name_before_any_write(onb, word):
    """`POST /onboard` with `host_name` set to a name Ansible reserves — `localhost`, either loopback address,
    `all`, `ungrouped` — answers 422 `invalid_host_name` on the dry-run, writes nothing and commits nothing.
    Guards one request naming a device `localhost` (or `127.0.0.1`): an inventory host of that name replaces the
    implicit control node for every play written for it."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(host_name=word))
    assert r.status_code == 422 and r.json()["code"] == "invalid_host_name", r.text
    assert not (onb.repo / "modules" / "edgeos").exists() and onb.calls == []


def _registry_default(backend):
    """The registry's own `default_secrets_domain` for `backend` — what the answers below must equal, read from the
    data rather than written here as a literal."""
    return next(b for b in catalog.load_backends() if b["name"] == backend)["default_secrets_domain"]


def test_cred_fields_reports_the_resolved_default_domain(onb):
    """With no `secrets`, `GET /onboard/cred-fields` answers with the backend's `default_secrets_domain` and stamps it
    on every descriptor; for a backend this install does not ship it names none (null). Guards the form's
    placeholder, which shows this answer, going blank or naming a domain the plan would not use (S27)."""
    for backend in ("api", "netcommon_cli"):
        b = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": backend}).json()
        assert b["secrets"] == _registry_default(backend)
        assert {cf["domain"] for cf in b["cred_fields"]} == {_registry_default(backend)}
    unknown = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": "no_such_backend"})
    assert unknown.status_code == 200 and unknown.json()["secrets"] is None


def _two_defaults():
    """The shipped registry with the api backend's default changed to `lab_api`, plus a copy of netcommon_cli that
    declares none. Every shipped default is `network`, so only a registry like this one tells a route that resolves
    the asked backend's default from one that resolves any other's."""
    reg = [dict(b) for b in catalog.load_backends()]
    for b in reg:
        if b["name"] == "api":
            b["default_secrets_domain"] = "lab_api"
    none = dict(next(b for b in reg if b["name"] == "netcommon_cli"), name="no_default")
    none.pop("default_secrets_domain")
    return reg + [none]


def test_cred_fields_answers_the_asked_backends_own_default(onb):
    """`GET /onboard/cred-fields` with no `secrets` answers the default of the backend it was asked about, over a
    registry whose backends declare different ones, and null for one that declares none.

    Guards the route resolving another backend's default (mutant C of T18's read, D6) — invisible while every
    shipped backend defaults to `network`, and exactly what S27 exists to let an operator change."""
    onb.client.app.state.catalog = dataclasses.replace(onb.client.app.state.catalog, backends=_two_defaults())
    for backend, want in (("api", "lab_api"), ("netcommon_cli", _registry_default("netcommon_cli")),
                          ("no_default", None)):
        b = onb.client.get("/onboard/cred-fields", headers=AUTH, params={"backend": backend}).json()
        assert b["secrets"] == want, backend


@pytest.mark.parametrize("over", [{}, {"secrets": ""}, {"secrets": None}], ids=["omitted", "blank", "null"])
def test_onboard_with_no_domain_plans_into_the_backend_default(onb, over):
    """An onboard body that omits `secrets`, sends it blank or sends null plans into the backend's default domain.
    Guards the request model's former literal coming back, and a blank meaning anything but "omitted" (the form
    sends one when its box is empty)."""
    r = onb.client.post("/onboard", headers=AUTH, json=_body(creds={"password": "p"}, **over))
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["module"]["secrets_domain"] == _registry_default("netcommon_cli")
    named = onb.client.post("/onboard", headers=AUTH, json=_body(creds={"password": "p"}, secrets="lab")).json()
    assert named["plan"]["module"]["secrets_domain"] == "lab"
