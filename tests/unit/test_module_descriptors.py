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

from kontroll import catalog, predicate

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


def test_every_shipped_class_resolves_a_backend_without_a_catch_all():
    """Every shipped class either declares a `backend:` the registry ships with an execution role, or has pinned
    facts that classify to one — so a reuse onboard of it (which adopts the declared backend, or classifies) and
    gen-class-capabilities both resolve a backend kontroll can run.

    Guards a class that can be enabled but not re-onboarded: raw_ssh caught every collection with no positive signal
    until it was made manual (S24), and openwrt, opnsense and proxmox resolved only through that catch-all. A class
    added later with neither a declaration nor classifying facts fails here, not at an operator's onboard."""
    registry = catalog.load_backends()
    runnable = set(catalog.runnable([b["name"] for b in registry], registry))
    unresolved = []
    for path in _shipped_modules():
        with open(path, encoding="utf-8") as fh:
            module = yaml.safe_load(fh) or {}
        pinned = os.path.join(os.path.dirname(path), "facts.pinned.yml")
        if module.get("backend"):
            ok = module["backend"] in runnable
        elif os.path.exists(pinned):
            with open(pinned, encoding="utf-8") as fh:
                ok = bool(catalog.runnable(predicate.classify(yaml.safe_load(fh) or {}, registry), registry))
        else:
            ok = False
        if not ok:
            unresolved.append(os.path.relpath(path, ROOT))
    assert unresolved == [], "declare `backend:` (one with a role) or pin facts that classify: %s" % unresolved
