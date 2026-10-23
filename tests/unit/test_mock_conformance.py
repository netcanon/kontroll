"""Mock conformance (S33): every canned dictionary in tests/_canned.py has the SHAPE of what the real function returns
— the same key set and value types, compared on the smallest real fixture.

The canned dicts live in `tests/_canned.py`. A dict written from belief keeps a UI test green while the server
emits something else; two homepage mocks, the Pending panel's payload, the discard answers, the keygen answer and
the unit list had drifted (keys the producer emits that the mock lacked) before this file existed, and nothing
noticed, because no test compared them.
Shape, not value: a value is the test's own choice, a key or a type is the producer's contract.
"""
import ast
import base64
import functools
import os

import pytest
import yaml

import _canned
import _canon_repo as R
from kontroll import catalog, gitio, paths
from kontroll.service import discard, discovery, homepage, keygen, pending, provenance, units
from kontroll.service._actuation_schema import validate_unit

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def shape(x, optional=frozenset()):
    """The comparable shape of a JSON-like value: a dict is its sorted (key, shape) pairs, a list the SET of its
    elements' shapes, a scalar its type name with None kept distinct. A key in `optional` (at any depth) is kept but
    its value is not compared — for a value that may be None or a dict, a string or None, by design."""
    if isinstance(x, dict):
        return ("dict", tuple(sorted((k, "optional" if k in optional else shape(v, optional))
                                     for k, v in x.items())))
    if isinstance(x, (list, tuple)):
        return ("list", frozenset(shape(v, optional) for v in x))
    return "None" if x is None else type(x).__name__


# --- the real fixtures, one per seam: each returns what the real function answers ------------------------------ #
def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _real_fleet_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _write(tmp_path, "ansible/collections/trust.generated.yml",
           "---\nschema: 1\ndefault_signature_policy: adaptive\nkeyring: null\ncollections:\n"
           '  - name: community.docker\n    version: "==4.0.0"\n    signature_policy: adaptive\n    sha256: null\n')
    return provenance.fleet_provenance()


def _real_fleet_provenance_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    return provenance.fleet_provenance()


def _real_image_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _write(tmp_path, "docker/images.lock.yml",
           '---\nschema: 1\nimages:\n  kontroll-control:\n    ref: ghcr.io/example/kontroll-control\n'
           '    tag: "v1"\n    digest: "sha256:%s"\n' % ("a" * 64))
    return provenance.image_provenance()


def _real_read_inbox(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})
    art = tmp_path / "inbox.json"
    art.write_text(json.dumps({
        "schema": 1, "generated_at": "2026-07-03T00:00:00+00:00",
        "sources": [{"key": "opnsense", "method": "dhcp_leases_opnsense", "host": "192.0.2.1", "status": "ok",
                     "count": 2}],
        # the second lease's MAC is locally administered, so no OUI names a vendor: both of `vendor`'s types occur
        "leases": [{"ip": "192.0.2.42", "mac": "00:00:5e:00:53:01", "hostname": "sw-lab-3",
                    "source_key": "opnsense", "source_host": "192.0.2.1"},
                   {"ip": "192.0.2.43", "mac": "de:ad:be:ef:00:01", "hostname": "h2",
                    "source_key": "opnsense", "source_host": "192.0.2.1"}]}), encoding="utf-8")
    return discovery.read_inbox(str(art))


def _real_list_pending(tmp_path, monkeypatch):
    return pending.list_pending(canonical=R.build(tmp_path).bare)


def _post_discard(tmp_path, monkeypatch, run_id, status):
    """The GUI's discard route, answering for `run_id` against a real canonical (the route and the service run; only
    the canonical's path and the audit log are pointed at the test's own)."""
    import app as gui
    r = R.build(tmp_path)
    monkeypatch.setattr(discard, "discard_proposal", functools.partial(discard.discard_proposal, canonical=r.bare))
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    gui.app.config["TESTING"] = True
    auth = base64.b64encode(("%s:%s" % (gui.GUI_USER, gui.GUI_PASSWORD)).encode()).decode()
    resp = gui.app.test_client().post("/api/pending/%s/discard" % run_id, headers={"Authorization": "Basic " + auth})
    assert resp.status_code == status, resp.get_json()
    return resp.get_json()


def _real_discard_answer(tmp_path, monkeypatch):
    return _post_discard(tmp_path, monkeypatch, R.STALE, 200)


def _real_discard_refusal(tmp_path, monkeypatch):
    return _post_discard(tmp_path, monkeypatch, "0123456789ab", 404)     # never staged: the refusal's own shape


_SERVICES = ("- A:\n    - Grafana:\n        href: https://grafana.example.test\n"
             "- B:\n    - Portal:\n        href: https://portal.example.test\n")


def _board_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _write(tmp_path, "instance/dashboards/homepage/services.yaml", _SERVICES)
    _write(tmp_path, "instance/dashboards/homepage/settings.yaml", "title: t\n")


def _real_read_board(tmp_path, monkeypatch):
    _board_repo(tmp_path, monkeypatch)
    return homepage.read_board()


def _real_build_homepage_plan(tmp_path, monkeypatch):
    _board_repo(tmp_path, monkeypatch)
    board = homepage.read_board()
    board["sections"].reverse()                                   # a reorder: a diff, nothing removed
    return homepage.build_homepage_plan(board)


def _real_keygen_result(tmp_path, monkeypatch):
    _write(tmp_path, ".sops.yaml", open(os.path.join(ROOT, "instance.example", ".sops.yaml"), encoding="utf-8").read())
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(gitio, "age_keygen", lambda: {"public": _canned.KEYGEN_RESULT["public_key"],
                                                      "private": _canned.KEYGEN_RESULT["private_key"]})
    return keygen.apply_keygen_plan("break-glass")


def _post_keygen(tmp_path, monkeypatch, changed, staged):
    """The GUI's keygen route answering for the break-glass role. Only the service's apply is faked (to
    KEYGEN_RESULT, so nothing is minted), and git's commands are recorded, not run: a staging deploy pushes
    `proposed/<run_id>`, any other lands `main` (`land_main`, faked). The route and gitio.commit_and_push run."""
    import app as gui
    calls = []
    monkeypatch.setattr(gui.keygen_service, "apply_keygen_plan",
                        lambda role, run_id=None: dict(_canned.KEYGEN_RESULT, changed=changed))
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: 0)
    if staged:
        monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    else:
        monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    monkeypatch.setattr(gui, "AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setattr(gui, "_audit_logger", None)
    gui.app.config["TESTING"] = True
    auth = base64.b64encode(("%s:%s" % (gui.GUI_USER, gui.GUI_PASSWORD)).encode()).decode()
    resp = gui.app.test_client().post("/api/keygen/break-glass", headers={"Authorization": "Basic " + auth},
                                      json={"apply": True})
    assert resp.status_code == 200, resp.get_json()
    assert bool(calls) == changed                     # a commit was attempted exactly when the key was new
    return resp.get_json()


def _real_service_units(tmp_path, monkeypatch):
    (tmp_path / "ns" / "demo" / "roles" / "swarm").mkdir(parents=True)
    return units.service_units("ns.demo", installed={str(tmp_path): {"ns.demo": {"version": "1.0.0"}}})


def _real_local_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_installed_json", lambda: {str(tmp_path): {"cisco.ios": {"version": "5.0.0"}}})
    return catalog.local_installed()


# seam id → (the canned value, the real fixture, the keys whose value is not compared). The Pending rows keep
# `repropose_hint` optional: the shared canonical fixture stages proposals whose hint is None and one whose hint is
# a dict, and a list compares the SET of its elements' shapes, so the one-proposal canned payload holds only one.
ROWS = {
    "kontroll.service.provenance.fleet_provenance": (_canned.FLEET_PROVENANCE, _real_fleet_provenance, ()),
    "fleet_provenance, no sidecar": (_canned.FLEET_PROVENANCE_UNAVAILABLE, _real_fleet_provenance_unavailable, ()),
    "kontroll.service.provenance.image_provenance": (_canned.IMAGE_PROVENANCE, _real_image_provenance, ()),
    "kontroll.service.discovery.read_inbox": (_canned.DISCOVERY_INBOX, _real_read_inbox, ()),
    "GET /api/pending, the Discard e2e": (_canned.PENDING_STALE, _real_list_pending, ("repropose_hint",)),
    "GET /api/pending, the Re-propose e2e": (_canned.PENDING_STALE_ONBOARD, _real_list_pending, ("repropose_hint",)),
    "POST /api/pending/<run_id>/discard": (_canned.DISCARD_ANSWER, _real_discard_answer, ()),
    "POST /api/pending/<run_id>/discard, refused": (_canned.DISCARD_REFUSED, _real_discard_refusal, ()),
    "kontroll.service.homepage.read_board": (_canned.HOMEPAGE_BOARD, _real_read_board, ()),
    "kontroll.service.homepage.build_homepage_plan": (_canned.HOMEPAGE_PLAN, _real_build_homepage_plan, ()),
    "kontroll.service.keygen.apply_keygen_plan": (_canned.KEYGEN_RESULT, _real_keygen_result, ()),
    "POST /api/keygen/<role>, staged": (_canned.keygen_answer(True, True),
                                        functools.partial(_post_keygen, changed=True, staged=True), ()),
    "POST /api/keygen/<role>, committed to main": (_canned.keygen_answer(True, False),
                                                   functools.partial(_post_keygen, changed=True, staged=False), ()),
    "POST /api/keygen/<role>, nothing changed": (_canned.keygen_answer(False, False),
                                                 functools.partial(_post_keygen, changed=False, staged=False), ()),
    "kontroll.service.units.service_units": (_canned.service_units("ns.demo"), _real_service_units, ()),
    "kontroll.catalog.local_installed": (_canned.LOCAL_INSTALLED, _real_local_installed, ()),
}

# e2e seams whose fake needs no shape row, and why.
NO_ROW = {
    "kontroll.catalog.local_shallow": "the fake builds each record with probe._facts, the real constructor",
    "kontroll.probe.deep_probe": "the fake builds its record with probe._facts, the real constructor",
    "kontroll.catalog.galaxy_search": "the fake answers [] — an empty list has no element shape to drift",
    "kontroll.catalog.load_actuation_units": "test_the_e2e_unit_is_a_valid_unit runs the registry's own validator",
}


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("seam", sorted(ROWS))
def test_canned_shape_matches_the_real_function(seam, tmp_path, monkeypatch):
    """The canned value served in place of `seam` has the key set and value types the real function returns on a
    fixture. Guards a mock shaped by belief: the UI or route test stays green while the server answers otherwise —
    the class of fault L16 found most often after a missing check."""
    canned, real, optional = ROWS[seam]
    assert shape(canned, frozenset(optional)) == shape(real(tmp_path, monkeypatch), frozenset(optional))


def _seams():
    """The seams `_mock_service_seams` in tests/e2e/conftest.py replaces and the ones it hands back to be restored,
    read from its source (importing the conftest would boot its patches): `module.attr` for each `X.attr = …` it
    makes (a tuple target included) and each `setattr(X, "attr", …)` it calls; and for each `(X, "attr")` key of
    its `originals` dict, which live_server restores."""
    tree = ast.parse(open(os.path.join(ROOT, "tests", "e2e", "conftest.py"), encoding="utf-8").read())
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = "%s.%s" % (node.module, a.name)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_mock_service_seams")
    assigned, restored = set(), set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                for e in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name):
                        assigned.add("%s.%s" % (aliases[e.value.id], e.attr))
                if isinstance(t, ast.Name) and t.id == "originals" and isinstance(node.value, ast.Dict):
                    restored |= {"%s.%s" % (aliases[k.elts[0].id], k.elts[1].value) for k in node.value.keys}
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr"
              and isinstance(node.args[0], ast.Name) and isinstance(node.args[1], ast.Constant)):
            assigned.add("%s.%s" % (aliases[node.args[0].id], node.args[1].value))
    return assigned, restored


def test_every_replaced_seam_has_a_conformance_row():
    """Every attribute `_mock_service_seams` replaces — by a plain or tuple assignment, or a `setattr` — and every
    one it hands back to be restored has a shape row above or a reason it needs none, and the two sets are the
    same. Guards a new seam mocked in the browser suite with nothing comparing its fake to the real function, and
    a seam replaced and never restored, whose fake then leaks into the rest of a combined run."""
    assigned, restored = _seams()
    assert "kontroll.service.provenance.fleet_provenance" in assigned     # the parse reads the conftest it names
    assert assigned == restored, "a seam replaced and not restored, or restored and not replaced in a form read here"
    assert assigned - set(ROWS) - set(NO_ROW) == set()
    assert set(NO_ROW) <= assigned, "a NO_ROW entry for a seam the conftest no longer replaces"


def test_the_shape_helper_catches_a_planted_missing_key():
    """The planted violation: a canned dict missing one key the real one has, and one whose value changed type, do
    not compare equal; an optional key's value is ignored and its absence is not. Guards a comparison that cannot
    fail — every row above would then pass on any mock."""
    real = {"a": 1, "b": [{"c": "x", "d": None}]}
    assert shape({"a": 1, "b": [{"c": "x"}]}) != shape(real)                      # a key missing below a list
    assert shape({"a": "1", "b": [{"c": "x", "d": None}]}) != shape(real)          # a type changed
    assert shape({"a": 1, "b": [{"c": "x", "d": {"e": 1}}]}, frozenset({"d"})) == shape(real, frozenset({"d"}))
    assert shape({"a": 1, "b": [{"c": "x"}]}, frozenset({"d"})) != shape(real, frozenset({"d"}))


def test_the_e2e_unit_is_a_valid_unit():
    """The actuation unit the browser suite registers passes the registry's own validator, its target class held to
    the example fleet's enabled modules as the registry holds a unit to the fleet's. Guards the configure dialog
    e2e exercising a unit the real registry would refuse to load (with no fleet, the validator skips that check)."""
    with open(os.path.join(ROOT, "instance.example", "fleet.yml"), encoding="utf-8") as fh:
        enabled = paths.enabled_keys(yaml.safe_load(fh))
    assert _canned.E2E_UNIT["target"]["device_class"] in enabled
    assert validate_unit(_canned.E2E_UNIT, _canned.E2E_UNIT["key"], enabled) == []
