"""Mock conformance (S33): every canned dictionary in tests/_canned.py (bar `E2E_UNIT`, which the registry's own
validator holds) has the SHAPE of what the real function returns — the same key set and value types, compared on the
smallest real fixture.

The canned dicts live in `tests/_canned.py`. A dict written from belief keeps a UI test green while the server
emits something else; two homepage mocks, the Pending panel's payload, the discard answers, the keygen answer and
the unit list had drifted (keys the producer emits that the mock lacked) before this file existed, and nothing
noticed, because no test compared them.
Shape, not value: a value is the test's own choice, a key or a type is the producer's contract.
"""
import ast
import base64
import copy
import functools
import inspect
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
                        lambda role, run_id=None: dict(copy.deepcopy(_canned.KEYGEN_RESULT), changed=changed))
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: 0)
    if staged:
        monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    else:
        monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))     # a seam missed above meets no checkout
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
# `repropose_hint` optional: every proposal the shared canonical fixture stages has a None hint (none of its paths
# is an onboard drop-in), while the canned payload's one proposal carries the dict hint the Re-propose e2e needs.
# The canned hint's own shape has its row below (`list_pending repropose_hint, an onboard drop-in`), read from the
# Re-propose payload, the one its e2e consumes (both payloads share `_canned._pending()`'s hint).
ROWS = {
    "kontroll.service.provenance.fleet_provenance": (_canned.FLEET_PROVENANCE, _real_fleet_provenance, ()),
    "fleet_provenance, no sidecar": (_canned.FLEET_PROVENANCE_UNAVAILABLE, _real_fleet_provenance_unavailable, ()),
    "kontroll.service.provenance.image_provenance": (_canned.IMAGE_PROVENANCE, _real_image_provenance, ()),
    "kontroll.service.discovery.read_inbox": (_canned.DISCOVERY_INBOX, _real_read_inbox, ()),
    "GET /api/pending, the Discard e2e": (_canned.PENDING_STALE, _real_list_pending, ("repropose_hint",)),
    "GET /api/pending, the Re-propose e2e": (_canned.PENDING_STALE_ONBOARD, _real_list_pending, ("repropose_hint",)),
    "list_pending repropose_hint, an onboard drop-in": (
        _canned.PENDING_STALE_ONBOARD["proposals"][0]["repropose_hint"],
        lambda tmp_path, monkeypatch: pending._repropose_hint(["instance/inventory/onboarded-x.yml"]), ()),
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


_CANNOT_NAME = "a replacement the seam parse cannot name, or one outside _mock_service_seams: "


def _flat(target):
    """The targets an assignment, `for` or `with` target names: a tuple's or a list's elements (nested too), a
    starred one's value, else the target itself."""
    if isinstance(target, (ast.Tuple, ast.List)):
        for t in target.elts:
            yield from _flat(t)
    elif isinstance(target, ast.Starred):
        yield from _flat(target.value)
    else:
        yield target


def _seams(source=None):
    """What tests/e2e/conftest.py replaces and puts back, read from its source (importing the conftest would boot its
    patches): (assigned, restored, loops, hands_back).

    Read: `module.attr` for each `X.attr` target `_mock_service_seams` assigns — a plain, tuple, list (nested too),
    starred, annotated, augmented, `for`, comprehension or `with` target — and each `setattr(X, "attr", …)` it calls,
    X an imported name (`import a.b` binds `a`); for each `(X, "attr")` key of its `originals` dict; the number of
    bare `setattr` calls outside it on a name that is not an import (live_server's restore loop: the seam test wants
    exactly one); and whether a `return originals` appears in it.
    Refused, failing here rather than passing unseen: an attribute target inside `_mock_service_seams` that is not
    `X.attr` (a nested or a local alias's), an assignment outside it to an attribute rooted at an imported name, a
    `setattr` by a variable name or on a nested object, a `setattr` handed on rather than called (`sa = setattr`,
    `functools.partial(setattr, …)`, `map(setattr, …)`), a monkeypatch object's `.setattr`, a `unittest.mock`
    import, and anywhere in the conftest `__dict__` or `__setattr__` as an attribute or a string, or a call of
    anything named `vars`, `exec` or `eval` (`builtins.vars` too).
    Anything else is not read, so only review holds it. Among the forms not read: a replacement outside
    `_mock_service_seams` through a name that is not an import (a local alias, `sys.modules[…]`, `globals()`,
    `getattr(…)`); `unittest.mock` reached without an import statement (`importlib.import_module`, `__import__`); a
    fixture that patches (pytest-mock's `mocker`); a replacement made in another file, an e2e test module's own
    `monkeypatch` included; and whether the restore loop puts back what `_mock_service_seams` handed over (the loop
    is counted, not traced)."""
    tree = ast.parse(source if source is not None else
                     open(os.path.join(ROOT, "tests", "e2e", "conftest.py"), encoding="utf-8").read())
    aliases, imported = {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = "%s.%s" % (node.module, a.name)
                imported.append("%s.%s" % (node.module, a.name))
        elif isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                aliases[a.asname or top] = a.name if a.asname else top
                imported.append(a.name)
    assert not any("mock" in m.split(".") for m in imported), _CANNOT_NAME + "a unittest.mock import"
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_mock_service_seams")
    inside = {id(n) for n in ast.walk(fn)}
    hands_back = any(isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == "originals"
                     for n in ast.walk(fn))
    assigned, restored, loops = set(), set(), 0

    def seam(obj, attr, node):
        ok = id(node) in inside and isinstance(obj, ast.Name) and obj.id in aliases and isinstance(attr, str)
        assert ok, _CANNOT_NAME + ast.unparse(node)
        assigned.add("%s.%s" % (aliases[obj.id], attr))

    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        assert not (isinstance(node, ast.Attribute) and node.attr in ("__dict__", "__setattr__")), \
            _CANNOT_NAME + ast.unparse(node)
        assert not (isinstance(node, ast.Constant) and node.value in ("__dict__", "__setattr__")), \
            _CANNOT_NAME + ast.unparse(node)
        assert not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None)) in ("vars", "exec", "eval")), \
            _CANNOT_NAME + ast.unparse(node)
        assert not (isinstance(node, ast.Name) and node.id == "setattr" and id(node) not in called), \
            _CANNOT_NAME + "setattr handed on, not called"
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        else:
            targets = []
        for t in targets:
            for e in _flat(t):
                if isinstance(e, ast.Attribute):
                    root = e
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if id(node) in inside or (isinstance(root, ast.Name) and root.id in aliases):
                        seam(e.value, e.attr, node)
            if (isinstance(node, ast.Assign) and id(node) in inside and isinstance(t, ast.Name)
                    and t.id == "originals" and isinstance(node.value, ast.Dict)):
                restored |= {"%s.%s" % (aliases[k.elts[0].id], k.elts[1].value) for k in node.value.keys}
        if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == "setattr":
            restore_loop = (id(node) not in inside and isinstance(node.func, ast.Name)
                            and isinstance(node.args[0], ast.Name) and node.args[0].id not in aliases)
            if restore_loop:
                loops += 1
            else:
                assert isinstance(node.func, ast.Name), _CANNOT_NAME + ast.unparse(node)
                seam(node.args[0], node.args[1].value if isinstance(node.args[1], ast.Constant) else None, node)
    return assigned, restored, loops, hands_back


def _restored_seams(source=None):
    """The seams the conftest replaces, once the parse has found the replaced and the handed-back sets equal, one
    restore loop and a `return originals` (the seam test's restore check, shared with the counts test below)."""
    assigned, restored, loops, hands_back = _seams(source)
    assert assigned == restored, "a seam replaced and not restored, or restored and not replaced in a form read here"
    assert loops == 1 and hands_back, ("exactly one bare `setattr` on a local outside _mock_service_seams "
                                       "(live_server's restore loop), and a `return originals` in _mock_service_seams")
    return assigned


def test_every_replaced_seam_has_a_conformance_row():
    """Every attribute `_mock_service_seams` replaces in a form `_seams` reads, and every one it hands back to be
    restored, has a shape row above or a reason it needs none, and the two sets are the same; the conftest has
    exactly one restore loop and a `return originals` in `_mock_service_seams`. A replacement in a form `_seams`
    refuses, inside the function or out, fails the parse (the planted test below); a form `_seams` does not read
    passes unseen (its docstring names some), and review holds it. Guards a new seam mocked in the browser suite
    with nothing comparing its fake to the real function, and a seam replaced and never restored, whose fake then
    leaks into the rest of a combined run."""
    assigned = _restored_seams()
    assert "kontroll.service.provenance.fleet_provenance" in assigned     # the parse reads the conftest it names
    assert assigned - set(ROWS) - set(NO_ROW) == set()
    assert set(NO_ROW) <= assigned, "a NO_ROW entry for a seam the conftest no longer replaces"


_PLANT_HEAD = "from kontroll import catalog as _catalog\nfrom kontroll.service import discovery as _disc\n"


@pytest.mark.parametrize("body", [
    "def _mock_service_seams():\n    _disc.fleet._inventory_groups = lambda: {}\n",              # a nested target
    "def _mock_service_seams():\n    c = _catalog\n    c.load = 1\n",                         # a local alias
    "def _mock_service_seams():\n    n = 'load'\n    setattr(_catalog, n, 1)\n",             # setattr by a variable
    "def _mock_service_seams():\n    setattr(_disc.fleet, 'load', 1)\n",                      # setattr on a nested object
    "def _mock_service_seams():\n    mp = object()\n    mp.setattr(_catalog, 'load', 1)\n",  # a monkeypatch object
    "def _mock_service_seams():\n    pass\n\n\ndef live_server():\n    _catalog.load = 1\n",  # outside the function
    "def _mock_service_seams():\n    pass\n\n\n_catalog.load: object = 1\n",                 # annotated, at module level
    "def _mock_service_seams():\n    pass\n\n\nsetattr(_catalog, 'load', 1)\n",             # setattr, at module level
    "import kontroll.catalog as kc\ndef _mock_service_seams():\n    pass\n\n\ndef live_server():\n    kc.load = 1\n",
    "def _mock_service_seams():\n    pass\n\n\ndef live_server():\n    _disc.fleet.load = 1\n",      # nested, outside
    "import kontroll.catalog\ndef _mock_service_seams():\n    pass\n\n\ndef live_server():\n"
    "    kontroll.catalog.load = 1\n",                                                         # a dotted import
    "def _mock_service_seams():\n    pass\n\n\ndef live_server():\n    [_catalog.load] = [1]\n",    # a list target
    "def _mock_service_seams():\n    pass\n\n\ndef live_server():\n    for _catalog.load in (1,):\n        pass\n",
    "def _mock_service_seams():\n    pass\n\n\ndef live_server(cm):\n    with cm as _catalog.load:\n        pass\n",
    "from unittest import mock\ndef _mock_service_seams():\n    mock.patch.object(_catalog, 'load', 1).start()\n",
    "import unittest.mock\ndef _mock_service_seams():\n    unittest.mock.patch('kontroll.catalog.load', 1).start()\n",
    "def _mock_service_seams():\n    _catalog.__dict__['load'] = 1\n",
    "def _mock_service_seams():\n    _catalog.__setattr__('load', 1)\n",
    "def _mock_service_seams():\n    vars(_catalog)['load'] = 1\n",
    "def _mock_service_seams():\n    exec('_catalog.load = 1')\n",
    "def _mock_service_seams():\n    eval(\"setattr(_catalog, 'load', 1)\")\n",
    "def _mock_service_seams():\n    pass\n\n\ndef live_server():\n    vars(_catalog)['load'] = 1\n",
    "def _mock_service_seams():\n    pass\n\n\ndef live_server():\n    [0 for _catalog.load in (1,)]\n",
    "import builtins\ndef _mock_service_seams():\n    builtins.vars(_catalog)['load'] = 1\n",
    "def _mock_service_seams():\n    getattr(_catalog, '__dict__')['load'] = 1\n",
    "def _mock_service_seams():\n    sa = setattr\n    sa(_catalog, 'load', 1)\n",
    "def _mock_service_seams():\n    pass\n\n\n_catalog.__dict__['load'] = 1\n",
], ids=["nested", "local-alias", "setattr-variable", "setattr-nested", "monkeypatch-object", "outside", "module-level",
        "setattr-module-level", "import-as-outside", "nested-outside", "dotted-import-outside", "list-target-outside",
        "for-target-outside", "with-target-outside", "mock-patch-object", "mock-patch-dotted", "dunder-dict",
        "dunder-setattr", "vars", "exec", "eval", "vars-outside", "comprehension-outside", "builtins-vars",
        "getattr-dunder", "setattr-handed-on", "dunder-outside"])
def test_the_seam_parse_refuses_a_replacement_it_cannot_name(body):
    """The planted violations: a seam replaced in a form the parse cannot name as `module.attr`, or outside
    `_mock_service_seams`, or patched in a form the parse refuses outright (`unittest.mock`, `__dict__`,
    `__setattr__` as an attribute or a string, a call named `vars`, `exec` or `eval`, a `setattr` handed on), fails
    the parse. Guards the seam test passing over a replacement it never read, so that the seam got no shape row and,
    unrestored, leaked: of the first eight, each but the local alias (which raised a KeyError) passed unseen before
    T21's fold read (N3); the import-as and nested-outside rows hold branches no plant reached (its fold-2 read, N3);
    the next twelve passed unseen through the fold's own parse (that read, S1); and the last six through fold 3's
    (its read, S1 and N2: `vars` and `__dict__` outside the seam function hold "anywhere")."""
    with pytest.raises(AssertionError, match="cannot name"):
        _seams(_PLANT_HEAD + body)


def test_the_seam_parse_reads_the_forms_it_names():
    """A plain, tuple, annotated, augmented, list, starred, `for`, `with`, comprehension, nested-tuple and
    literal-`setattr` replacement inside `_mock_service_seams` are each read as `module.attr`, the `originals` keys as
    restored, and live_server's restore loop is let through and counted. Guards the planted refusals above being
    bought by a parse that refuses everything, the real conftest's forms included, and `_flat` reading only one level
    of a target (T21's fold-3 read, N2)."""
    src = _PLANT_HEAD + (
        "def _mock_service_seams():\n"
        "    originals = {(_catalog, 'a'): 0, (_disc, 'b'): 0}\n"
        "    _catalog.a, _disc.b = 1, 2\n"
        "    _catalog.c: object = 3\n"
        "    setattr(_disc, 'd', 4)\n"
        "    _catalog.e += ()\n"
        "    [_catalog.f, *_disc.g] = [5, 6]\n"
        "    for _disc.h in ():\n"
        "        pass\n"
        "    with open(__file__) as _catalog.i:\n"
        "        pass\n"
        "    [0 for _catalog.j in ()]\n"
        "    _x, (_catalog.k, _y) = 0, (1, 2)\n"
        "    return originals\n\n\n"
        "def live_server(orig):\n"
        "    for (mod, name), fn in orig.items():\n"
        "        setattr(mod, name, fn)\n")
    assert _seams(src) == ({"kontroll.catalog.a", "kontroll.service.discovery.b", "kontroll.catalog.c",
                            "kontroll.service.discovery.d", "kontroll.catalog.e", "kontroll.catalog.f",
                            "kontroll.service.discovery.g", "kontroll.service.discovery.h", "kontroll.catalog.i",
                            "kontroll.catalog.j", "kontroll.catalog.k"},
                           {"kontroll.catalog.a", "kontroll.service.discovery.b"}, 1, True)


def test_the_seam_parse_counts_the_restore_loops_and_reads_the_hand_back():
    """The parse counts the bare `setattr` calls it lets through outside `_mock_service_seams` — two loops count two,
    none counts none — and reads whether that function returns its `originals` (`return {}` does not); the seam
    test's own check (`_restored_seams`) passes one loop with a hand-back and fails no loop, two loops, `return {}`
    and a seam replaced and not handed back; and the seam test runs that check on the real conftest.

    Guards the seam test's restore check passing a conftest whose seams stay replaced: deleting live_server's restore
    loop, or handing back `{}`, passed it (T21's fold-2 read, N6), and so did a second, table-driven `setattr` loop
    that patches, which the restore loop's exemption let by (its S1); and that check's own assertions weakened or
    dropped, which only the real conftest reached (the fold-3 read, N1)."""
    base = _PLANT_HEAD + ("def _mock_service_seams():\n    originals = {(_catalog, 'a'): 0}\n    _catalog.a = 1\n"
                          "    return originals\n\n\n")
    loop = "def live_server(orig):\n    for (mod, name), fn in orig.items():\n        setattr(mod, name, fn)\n"
    again = "\n\ndef other():\n    for m in (_catalog,):\n        setattr(m, 'load', 1)\n"
    assert _seams(base + loop)[2:] == (1, True)
    assert _seams(base + loop + again)[2:] == (2, True)
    assert _seams(base)[2:] == (0, True)
    assert _seams(base.replace("return originals", "return {}") + loop)[2:] == (1, False)
    assert _restored_seams(base + loop) == {"kontroll.catalog.a"}
    for planted in (base, base + loop + again, base.replace("return originals", "return {}") + loop,
                    base.replace("    return originals", "    _catalog.z = 2\n    return originals") + loop):
        with pytest.raises(AssertionError):
            _restored_seams(planted)
    assert "_restored_seams()" in inspect.getsource(test_every_replaced_seam_has_a_conformance_row)


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
