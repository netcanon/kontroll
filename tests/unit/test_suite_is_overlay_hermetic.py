"""The suite's verdict does not depend on an operator's private overlay (tests/_hermetic.py).

WHY: on a tree that carries a real `instance/` the resolver in `kontroll.paths` prefers it, so every test that
reads instance config at the real root — the fleet, the inventory, the .sops.yaml — was reading private data. The
suite was green there only while the private fleet enabled the same classes as the shipped example; the first
real fleet edit would have turned named tests red on that tree alone, and nobody could have reproduced it from a
public clone. tests/conftest.py installs a switch that hides the overlay AT THE REAL CHECKOUT ONLY. These tests pin
both halves: the switch works, and it is narrow.
"""
import os

import pytest

import _hermetic
from kontroll import paths

pytestmark = pytest.mark.unit


def _tree_with_both_fleets(tmp_path):
    (tmp_path / "instance").mkdir()
    (tmp_path / "instance" / "fleet.yml").write_text("enabled_modules: [a_private_class]\n", encoding="utf-8")
    (tmp_path / "instance.example").mkdir()
    (tmp_path / "instance.example" / "fleet.yml").write_text("enabled_modules: [the_example_class]\n",
                                                             encoding="utf-8")
    return str(tmp_path)


def test_the_switch_is_installed_in_this_test_process():
    """tests/conftest.py installed the wrappers before any test module was imported. Guards the switch being
    dropped from conftest in a refactor — every other test here would still pass, on a public clone, and the suite
    would quietly be overlay-coupled again."""
    assert paths._overlay_file_present is _hermetic._file_present
    assert paths._overlay_dir_present is _hermetic._dir_present
    assert _hermetic.at_real_root(), "outside a tmp-root fixture the resolver's root is the checkout itself"


def test_the_suite_resolves_the_example_even_when_an_overlay_exists(tmp_path, monkeypatch):
    """With a tree that holds BOTH `instance/fleet.yml` and `instance.example/fleet.yml` standing in for the real
    checkout, a read of the fleet resolves to the example, and the staged-path form does not name the overlay.
    Guards the suite's verdict depending on an operator's private data: a private fleet edit turning tests red that
    are green on every public clone."""
    root = _tree_with_both_fleets(tmp_path)
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    monkeypatch.setattr(_hermetic, "REAL_ROOT", os.path.realpath(root))       # this tree IS "the checkout"
    resolved = os.path.normpath(paths.resolve("config/fleet.yml"))
    assert resolved == os.path.normpath(os.path.join(root, "instance.example", "fleet.yml"))
    assert paths.overlay_rel("config/fleet.yml") == "config/fleet.yml"


def test_a_tmp_root_overlay_is_still_honoured(tmp_path, monkeypatch):
    """The switch is NARROW: with the resolver's root at a tmp tree (what `tmp_repo` and every overlay-layout test
    use), an `instance/` there shadows as before — for a read, for the staged path, and for a new file's write
    target. Guards the hermetic switch silently disabling the tests that exist to prove the overlay works."""
    root = _tree_with_both_fleets(tmp_path)
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    assert not _hermetic.at_real_root()
    assert os.path.normpath(paths.resolve("config/fleet.yml")) == \
        os.path.normpath(os.path.join(root, "instance", "fleet.yml"))
    assert paths.overlay_rel("config/fleet.yml") == "instance/fleet.yml"
    assert paths.overlay_target("ansible/inventory/onboarded-x.yml").startswith("instance/")
