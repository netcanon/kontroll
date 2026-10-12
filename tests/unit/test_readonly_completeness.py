"""#134 / P0b — the fail-CLOSED read-only completeness gate.

`assert_read_only` (#132/#133) and `assert_no_git_write` (#131) both fail OPEN: each checks a function's calls
against `WRITE_VERBS`, so a mutator the registry *doesn't know about* is invisible — a read view could call an
unregistered writer and the pin would pass. That silently erodes the read-only / C10 two-key guarantee.

These tests pin the closure: `assert_write_verbs_complete` (every DIRECT mutator in the service+gitio layer is
registered) + `assert_write_verbs_resolve` (no dead/typo'd entry). They make "add every new write verb to
WRITE_VERBS in the same commit" an ENFORCED gate, not a human convention. The detection engine (`_is_direct_mutator`)
and the end-to-end gate are both proven against PLANTED violations so the gate can't silently no-op.
"""
import ast
import os
import textwrap

import pytest

import _readonly_pins
from _readonly_pins import assert_write_verbs_complete, assert_write_verbs_resolve

pytestmark = pytest.mark.unit


def _fn(src):
    """Parse a single-function source snippet → its FunctionDef node (for testing the detection engine directly)."""
    return next(n for n in ast.walk(ast.parse(textwrap.dedent(src))) if isinstance(n, ast.FunctionDef))


def test_detection_flags_every_write_primitive_and_no_read():
    """`_is_direct_mutator` (the engine under the gate) flags each DIRECT write primitive — a write-mode open, a
    `git commit`/`push`/`update-ref` argv (incl. the `["git","add"] + paths` BinOp form), a `sops --set`, a named
    sops-write call — and does NOT flag a pure read (`open(...,'r')`, `git log`, `sops --decrypt`, the dual-use
    `git remote get-url`, a pure return). This is the proof the gate FAILS CLOSED on a new unregistered writer
    while NOT false-positiving a read helper (the `git remote get-url` case is the real gitio.offsite_remote_exists
    shape that a naive git-write blacklist would mis-flag)."""
    mutators = {
        "open_w":            "def f():\n    with open('x', 'w') as fh: fh.write('a')",
        "open_a_kw":         "def f():\n    open('x', mode='a')",
        "git_commit":        "def f():\n    _run(['git', 'commit', '-m', 'x'])",
        "git_add_binop":     "def f(p):\n    _run(['git', 'add'] + list(p))",
        "git_update_ref":    "def f(ref):\n    subprocess.run(['git', 'update-ref', 'refs/heads/main', ref])",
        "git_reset":         "def f():\n    _run(['git', 'reset', '--hard', 'local/main'])",
        "git_clean_binop":   "def f(p):\n    _run(['git', '--literal-pathspecs', 'clean', '-f', '-x', '--'] + p)",
        "sops_set_argv":     "def f(path):\n    subprocess.run(['sops', '--set', '[\"k\"] 1', path])",
        "sops_encrypt_argv": "def f(path):\n    subprocess.run(['sops', '--encrypt', path])",
        "sops_rotate_argv":  "def f(path):\n    subprocess.run(['sops', '--rotate', '--in-place', path])",
        "sops_named_call":   "def f(d, m):\n    gitio.sops_write_domain(d, m)",
        "path_write_text":   "def f(p, s):\n    pathlib.Path(p).write_text(s)",
        "shutil_rmtree":     "def f(d):\n    shutil.rmtree(d)",
        "path_open_w":       "def f(p):\n    with pathlib.Path(p).open('w') as fh: fh.write('x')",  # attr-form open, mode-gated
        "os_replace":        "def f(a, b):\n    os.replace(a, b)",         # the atomic-write idiom, receiver-qualified
        "shutil_move":       "def f(a, b):\n    shutil.move(a, b)",
    }
    for key, src in mutators.items():
        assert _readonly_pins._is_direct_mutator(_fn(src)) is True, "engine MISSED a writer: %s" % key
    reads = {
        "open_r_default":    "def f():\n    return open('x').read()",
        "open_rb":           "def f():\n    open('x', 'rb')",
        "git_log":           "def f():\n    _run(['git', 'log', '--oneline'])",
        "git_remote_geturl": "def f():\n    subprocess.run(['git', 'remote', 'get-url', 'origin'])",
        "git_merge_base":    "def f(ref):\n    _run(['git', 'merge-base', '--is-ancestor', 'main', ref])",
        "sops_decrypt":      "def f(path):\n    subprocess.run(['sops', '--decrypt', path])",
        "pure":              "def f(a):\n    return a + 1",
        # the deliberate exclusions — these method NAMES are ubiquitous on reads, so they must NOT flag (else every
        # read view false-positives). The receiver-qualified os.*/shutil.* detection keeps them safe.
        "str_replace":       "def f(s):\n    return s.replace('a', 'b')",     # NOT os.replace — a str method
        "dict_copy":         "def f(d):\n    return d.copy()",                # NOT shutil.copy — a dict method
        "list_remove":       "def f(xs, x):\n    xs.remove(x); return xs",    # NOT os.remove — a list method
        "os_path_join":      "def f(a, b):\n    return os.path.join(a, b)",   # receiver is os.path, not os → not a write
        "path_open_read":    "def f(p):\n    return pathlib.Path(p).open().read()",  # attr-form open, NO write mode
    }
    for key, src in reads.items():
        assert _readonly_pins._is_direct_mutator(_fn(src)) is False, "engine FALSE-POSITIVE on a read: %s" % key


def test_detection_attributes_a_write_to_the_nested_scope_not_the_parent():
    """A write inside a NESTED function belongs to that nested scope, not the enclosing one — `_own_nodes` stops at
    the nested `def`. Guards against a parent being mis-flagged (false positive) because it merely DEFINES a writer
    it never calls; the nested writer is caught on its own (visited as its own FunctionDef by the gate's walk)."""
    src = "def outer():\n    def inner():\n        open('x', 'w')\n    return inner"
    tree = ast.parse(src)
    outer = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "outer")
    inner = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "inner")
    assert _readonly_pins._is_direct_mutator(outer) is False   # outer only DEFINES inner — not a direct mutator
    assert _readonly_pins._is_direct_mutator(inner) is True    # inner is the writer


def test_a_git_clean_in_a_read_only_git_module_is_a_write(monkeypatch, tmp_path):
    """`assert_no_git_write` refuses a module whose `subprocess.run(["git", "clean", …])` literal names `clean`.
    Guards the verb that DELETES untracked and ignored files being absent from the list a read-only git module is
    checked against — it was absent until the tree gained its first `git clean`, and nothing else would notice it
    going missing again."""
    (tmp_path / "view.py").write_text(
        "import subprocess\n\n\ndef _git(cap, argv):\n    return argv\n\n\n"
        "def view(cap):\n    subprocess.run(['git', 'clean', '-f'])\n    return _git(cap, ['log'])\n",
        encoding="utf-8")
    monkeypatch.setattr(_readonly_pins, "ROOT", str(tmp_path))
    with pytest.raises(AssertionError, match="clean"):
        _readonly_pins.assert_no_git_write("view.py")


def test_write_verbs_complete_on_the_real_tree():
    """Every DIRECT mutator in the real service+gitio layer is registered in WRITE_VERBS (or the documented
    WRITE_COMPLETENESS_ALLOWLIST). THE fail-CLOSED #134/P0b gate: a new file-writer / git-writer / sops-writer that
    isn't registered breaks CI here, so assert_read_only stays exhaustive and the read-only/C10 guarantee holds."""
    assert_write_verbs_complete()


def test_write_verbs_resolve_no_rot():
    """Every WRITE_VERBS entry resolves to a real `def` under scripts/ (or is a bare git verb guarding a
    git-library call) — no typo / dead-after-rename entry silently weakens the pin."""
    assert_write_verbs_resolve()


def test_gate_fires_on_an_unregistered_writer(monkeypatch, tmp_path):
    """End-to-end proof the GATE fails closed: point the scan at a throwaway service layer holding an UNREGISTERED
    write-open function → `assert_write_verbs_complete` RAISES, naming `file:func`. Guards a refactor that makes the
    scan silently no-op (the worst failure for a fail-closed gate — passing while blind)."""
    svc = tmp_path / "scripts" / "kontroll" / "service"
    svc.mkdir(parents=True)
    (svc.parent / "gitio.py").write_text("", encoding="utf-8")     # the gate also reads gitio.py; keep it empty
    (svc / "sneaky.py").write_text("def sneaky_write(p):\n    open(p, 'w').write('x')\n", encoding="utf-8")
    monkeypatch.setattr(_readonly_pins, "ROOT", str(tmp_path))
    with pytest.raises(AssertionError, match="sneaky_write"):
        assert_write_verbs_complete()


_PLANTED = """
import pathlib


def commit_and_push(paths, message):
    return 0


def _writes(x):
    return commit_and_push([x], "m")


def _stores(x):
    with open(x, "w") as fh:
        fh.write("a")


def _reads(x):
    with open(x) as fh:
        return fh.read()


def view_through_a_helper(x):
    return _writes(x)


def view_through_two_helpers(x):
    return _middle(x)


def _middle(x):
    return _stores(x)


def view_with_a_callback(xs):
    return list(map(_writes, xs))


def view_that_writes_without_a_verb(x):
    pathlib.Path(x).write_text("a")


def view_that_only_reads(x):
    return _reads(x) + _loops(x)


def _loops(x):
    return _loops(x) if False else ""
"""

_PLANTED_VIEWS = """
from . import store
from . import store as kept
from .store import save as keep, read_back


def view_through_a_module(x):
    return store.save(x)


def view_through_an_alias(x):
    return kept.save(x)


def view_through_an_imported_name(x):
    return keep(x)


def view_with_a_lazy_import(x):
    from .store import save
    return save(x)


def view_that_reads_through_a_module(x):
    return read_back(x) + store.read_back(x)
"""

_PLANTED_STORE = """
import os


def save(x):
    return _replace(x)


def _replace(x):
    os.replace(x + ".tmp", x)


def read_back(x):
    with open(x) as fh:
        return fh.read()
"""


def _plant(tmp_path):
    module = tmp_path / "planted.py"
    module.write_text(_PLANTED, encoding="utf-8")
    return str(module)


def _plant_package(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "views.py").write_text(_PLANTED_VIEWS, encoding="utf-8")
    (pkg / "store.py").write_text(_PLANTED_STORE, encoding="utf-8")
    return str(pkg / "views.py")


def _refused(module, func):
    with pytest.raises(AssertionError) as e:
        _readonly_pins.assert_read_only(module, func)
    return str(e.value)


def test_the_pin_follows_a_same_module_helper_to_a_write_verb(tmp_path):
    """`assert_read_only` fails for a read view whose write is one or two same-module helpers away — a registered
    write verb, or a file opened for writing — and the message names the path from the view to the write. Guards a
    refactor that splits a read view into helpers and thereby un-pins it: the pin used to look at the function's
    direct calls only, so `view() -> _helper() -> commit_and_push()` passed."""
    module = _plant(tmp_path)
    assert "view_through_a_helper -> _writes -> commit_and_push" in _refused(module, "view_through_a_helper")
    assert "view_through_two_helpers -> _middle -> _stores -> (a direct write)" in \
        _refused(module, "view_through_two_helpers")


def test_the_pin_follows_a_helper_that_is_passed_on_and_not_called(tmp_path):
    """A read view that hands a writing helper to `map()` is refused, with the path. Guards the callback form of
    the same hiding place: the view never CALLS the helper, so a closure over call names alone would pass it — and
    `map`, `sorted(key=…)` and a thread target are ordinary ways for a helper to run."""
    assert "view_with_a_callback -> _writes -> commit_and_push" in _refused(_plant(tmp_path), "view_with_a_callback")


def test_the_pin_sees_a_write_primitive_no_verb_names(tmp_path):
    """A pinned function that writes with `pathlib.Path(...).write_text` — no registered verb, no bare `open` — is
    refused. Guards the gap between the two gates: the completeness sweep would only have asked for such a function
    to be REGISTERED as a verb, after which both gates were green while a pinned read view wrote a file."""
    assert "view_that_writes_without_a_verb -> (a direct write)" in \
        _refused(_plant(tmp_path), "view_that_writes_without_a_verb")


def test_the_pin_follows_a_call_into_another_module(tmp_path):
    """A read view whose write lives in ANOTHER module of the same package is refused however the import is spelled
    — `from . import store`, an alias, `from .store import save as keep`, an import inside the function body — and
    the path names the module. Guards writing by delegation: the name check only ever saw a registered verb called
    directly, so moving the write behind an unregistered function of a sibling module un-pinned the view."""
    module = _plant_package(tmp_path)
    for view in ("view_through_a_module", "view_through_an_alias", "view_through_an_imported_name",
                 "view_with_a_lazy_import"):
        message = _refused(module, view)
        assert "%s -> store.save -> store._replace -> (a direct write)" % view in message, message


def test_a_helper_that_only_reads_does_not_trip_the_pin(tmp_path):
    """A read view that calls helpers which only read — a same-module one, a recursive one, and one in another
    module — passes, and the closure terminates. Guards the transitive pin against noise and against a loop on a
    self-calling helper; a pin that cries wolf gets its call sites removed."""
    module = _plant(tmp_path)
    _readonly_pins.assert_read_only(module, "view_that_only_reads")
    assert _readonly_pins.write_paths(module, "view_that_only_reads") == []
    package = _plant_package(tmp_path)
    _readonly_pins.assert_read_only(package, "view_that_reads_through_a_module")
    visited = [trail for _path, _name, trail in _readonly_pins.reachable(package, "view_that_reads_through_a_module")]
    assert ["view_that_reads_through_a_module", "store.read_back"] in visited      # it WAS followed, and is clean


def test_the_closure_resolves_the_real_trees_imports():
    """From a pinned GUI route the closure reaches functions of the service layer, and from a pinned service view
    it reaches other modules of that layer. Guards the cross-module step silently resolving nothing — after a
    package move, or a change in how the GUI imports the service layer — which would leave every pin green while
    the closure had quietly shrunk back to one file."""
    service = os.path.join("scripts", "kontroll", "service") + os.sep
    from_gui = {os.path.relpath(path, _readonly_pins.ROOT)
                for path, _name, _trail in _readonly_pins.reachable("gui/app.py", "api_provenance")}
    assert any(p.startswith(service) for p in from_gui), sorted(from_gui)
    from_view = {os.path.relpath(path, _readonly_pins.ROOT) for path, _name, _trail in
                 _readonly_pins.reachable("scripts/kontroll/service/configurable.py", "configurable_view")}
    assert len(from_view) >= 3, sorted(from_view)
