"""Shared pytest fixtures for the kontroll code-half test suite.

Two testable surfaces (docs/qa-and-release-pipeline.md §1):
  * scripts/galaxy.py — the onboarding engine. Its core (eval_pred/eval_vector/
    classify/build_record/derive_*/classify_endpoints) is pure dict->dict and needs
    no fixtures beyond the real vectors/backends. Its three I/O seams — `_ad`
    (ansible-doc), `local_installed` (ansible-galaxy), `galaxy_search` (urllib) — are
    single-purpose module functions the tests MONKEYPATCH to inject canned facts, so
    the suite runs with neither ansible nor a network (the "injectable seam" §7.1 asks
    for already exists; this is its use).
  * gui/app.py — a thin Flask shell; it calls the SAME service-layer seams in-process
    (search/classify/onboard — `run_galaxy` retired), mocked in their home modules like galaxy.py's.

The live lab is NEVER reached here (CI proves OFFLINE correctness).
"""
import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # import galaxy
sys.path.insert(0, os.path.join(ROOT, "gui"))        # import app (the GUI)
sys.path.insert(0, ROOT)                             # import api (the FastAPI app)

# The GUI is fail-closed: it reads GUI_PASSWORD at import. Provide a test password
# (and a throwaway audit log under the OS temp dir, so tests never write the repo).
os.environ.setdefault("GUI_USER", "admin")
os.environ.setdefault("GUI_PASSWORD", "test-password")
os.environ.setdefault("GUI_AUDIT_LOG",
                      os.path.join(tempfile.gettempdir(), "kontroll-test-gui-audit.log"))

sys.path.insert(0, os.path.join(ROOT, "tests"))      # import _hermetic (a helper beside this file)
import _hermetic  # noqa: E402

# HERMETIC OVERLAY — installed at IMPORT time, before any test module is collected (several read the fleet at module
# import, before a fixture could run). From here on a reader that goes through kontroll.paths at the REAL checkout
# sees no `instance/` overlay and lands on the shipped instance.example/, so the suite gives the same verdict on a
# public clone, a CI runner and a tree that carries a private overlay. A tmp root is untouched. tests/_hermetic.py.
_hermetic.install()

import galaxy  # noqa: E402  (after sys.path is set up)
from kontroll import paths  # noqa: E402  (the repo-root ROOT seam moved here in the service refactor)


@pytest.fixture
def vectors():
    """The real capability vectors (vectors/*.yml) — the contract under test."""
    return galaxy.load_vectors()


@pytest.fixture
def backends():
    """The real execution backends (ansible/backends/*/backend.yml)."""
    return galaxy.load_backends()


@pytest.fixture
def make_facts():
    """Factory for synthetic probe facts — the input every pure function consumes.

    make_facts(plugins={"cliconf": ["ios"]}, modules=["ios_command"]) etc.
    Omitted signal channels default empty (module_options={} means "unknown at
    depth" for module_option predicates — the three-valued None case)."""
    def _make(collection="ns.coll", version="1.0.0", origin="local", depth="deep",
              plugins=None, modules=None, module_options=None,
              description="", tags=None, certified=False):
        f = galaxy._facts(collection, version, origin, depth)
        f["plugins"] = plugins or {}
        f["modules"] = modules or []
        f["module_options"] = module_options or {}
        f["description"] = description
        f["tags"] = tags or []
        f["certified"] = certified
        return f
    return _make


def pytest_collection_modifyitems(items):
    """Run e2e-marked tests LAST in a combined run. The e2e GUI harness boots a SESSION-scoped live server in
    this process and sets the service I/O seams (`catalog.local_shallow`, `probe.deep_probe`, …) by PLAIN
    module-attribute assignment with no teardown (tests/e2e/conftest.py — the e2e process was designed
    throwaway). Under a full `py -m pytest` (e2e + unit together) those global assignments would otherwise
    LEAK into later non-e2e tests — e.g. `test_local_shallow_caps_fanout_at_limit`, which exercises the REAL
    `catalog.local_shallow`, fails because it's been replaced by the e2e fake. Ordering e2e last means every
    non-e2e test runs against the real seams first; the e2e `live_server` fixture also restores them on
    teardown (belt-and-suspenders). `-m "not e2e"` (the standard local + CI invocation) still excludes them
    entirely — this only makes the combined run clean. Stable sort: non-e2e order is preserved."""
    items.sort(key=lambda it: 1 if it.get_closest_marker("e2e") else 0)


@pytest.fixture
def isolated_git(tmp_path_factory, monkeypatch):
    """Every git a test starts sees the test, and nothing of whoever runs the suite. Its CONFIGURATION is empty:
    `GIT_CONFIG_GLOBAL` names an empty file, the system file is switched off, `XDG_CONFIG_HOME` is a directory with
    nothing in it — a global hooks path, excludes file, `core.safecrlf` or signing setting must not decide a test.
    And no variable points it at a REPOSITORY: `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`
    and `GIT_COMMON_DIR` are removed — nor does a key or a switch handed down by a parent git reach it
    (`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_NO_REPLACE_OBJECTS`, `GIT_DEFAULT_HASH`). A suite started from a git hook inherits them, and a test's
    `git -C <scratch> init`, `config` and `add -A` would then act on the DEVELOPER's repository instead of the
    scratch one. For tests that run the real git (name it in `pytest.mark.usefixtures`, and as a parameter of any
    fixture that copies `os.environ`)."""
    home = tmp_path_factory.mktemp("no-git-config")
    (home / "gitconfig").write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_NO_REPLACE_OBJECTS", "GIT_DEFAULT_HASH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """A throwaway repo root for the file-mutating paths (enable_in_fleet, onboard
    --apply). Copies the shipped EXAMPLE fleet (the suite never reads a private overlay —
    tests/_hermetic.py) and creates the dirs the writers target, then repoints
    kontroll.paths.ROOT at it — so --apply/enable_in_fleet never touch the real tree.
    Backends/vectors still load from the real repo (read-only input)."""
    (tmp_path / "config").mkdir()
    shutil.copy(paths.resolve("config/fleet.yml"), tmp_path / "config" / "fleet.yml")
    shutil.copy(os.path.join(ROOT, "config", "capture-exceptions.yml"),
                tmp_path / "config" / "capture-exceptions.yml")
    (tmp_path / "ansible" / "inventory").mkdir(parents=True)
    (tmp_path / "modules").mkdir()
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    return tmp_path
