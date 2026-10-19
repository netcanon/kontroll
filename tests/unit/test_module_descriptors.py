"""The shipped device-class descriptors (`modules/<key>/module.yml`) carry only fields that something reads.

WHY: a descriptor field that reads like a safety gate and is not one is worse than no field — an operator trusts it.
The module `status:` (`staged` / `active`) was such a field: nothing read it to decide anything, a class ran the
moment `instance/fleet.yml` listed it, and the onboard enabled it in the same apply that wrote `status: staged`. It
was removed (2026-10-19); these rows keep it from coming back by being copied from an old descriptor.
"""
import glob
import os

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _shipped_modules():
    found = sorted(glob.glob(os.path.join(ROOT, "modules", "*", "module.yml")))
    assert found, "no shipped modules/*/module.yml found — the glob, not the tree, is wrong"
    return found


def test_no_shipped_module_declares_a_lifecycle_status():
    """No shipped descriptor has a top-level `status` key. Which classes run is decided by `instance/fleet.yml`
    alone; a `status: staged` in a shipped file reads as "not live yet" while the class runs wherever it is enabled.
    Guards a new class written from an old one carrying the field (and its comment) back in."""
    offenders = []
    for path in _shipped_modules():
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        if "status" in doc:
            offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, "module descriptors that declare `status`: %s" % offenders
