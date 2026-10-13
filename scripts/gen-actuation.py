#!/usr/bin/env python3
"""Validate the actuation/<key>/unit.yml app-store unit registry — fail-closed (R2 of the GUI-actuation build).

An actuation unit is a self-describing, version-pinned runnable unit (a collection playbook / role / in-repo
standalone) the GUI stages + an operator promotes. Its `install.collections` DERIVES into
`ansible/collections/requirements.generated.yml` via scripts/gen-requirements.py — so a malformed descriptor must
be caught BEFORE it unions a pin or anything installs it. This generator's R2 job is that VALIDATION:
  * schema is a known version (forward-compat fail-closed),
  * `kind` ∈ {role, collection_playbook, standalone} (closed enum),
  * `key` matches [a-z][a-z0-9-]* AND equals its directory name (the template/wrapper-stem invariant),
  * an EXACT '==' pin for installable kinds (an app-store unit pins a REVIEWED version, not a floor),
  * `target.device_class` is an ENABLED module (instance/fleet.yml) — no orphaned install (report 21 §3),
  * closed blast-radius + provenance enums; `source: git` is rejected (MVP = galaxy only),
  * never-brick provenance (R5 PR-B): `signature` ∈ {required, adaptive, none} (absent ⇒ adaptive, the brick-proof
    default), `keyring`/`sha256` shape-checked when present, and the Tier-cap — a unit that does not declare
    `signature: required` may NOT target `edge_firewall`/`core_switch` (fail closed in --check). Design of record:
    docs/reviews/2026-06-25-never-brick-supply-chain/.

The PUSH-stage artifacts (the generated Semaphore template + the role-wrapper play) are NOT emitted yet — they
land with service/actuation.py at R4/R5; so `--check` is the same fail-closed validation pass that runs in
tests/validate (when those committed artifacts exist, --check will also diff them for staleness, gen-backup-style).

The descriptor SCHEMA + the per-unit validator (`validate_unit` / `validate_knobs` + the closed-enum constants)
live in `kontroll.service._actuation_schema` — the ONE contract this CLI and the app-store CREATE seam
(`service/actuation.build_create_unit_plan`, which renders a fresh descriptor from a searched collection and must
validate it BEFORE staging) both consume. This module re-exports them and adds the file-walking `validate_all` +
`_enabled_modules` + `main`. Standalone — its kontroll imports are `paths` and the schema module, which pulls
`paths`, `service/_validate` and through it `yamlscalar` (PyYAML, which this script needs anyway) — so CI +
tests/validate run it without ansible. Schema: actuation/README.md.

Usage:  python3 scripts/gen-actuation.py [--check]
"""
import os
import sys

import yaml

from kontroll import paths
# The descriptor contract — re-exported so existing callers (tests/validate, test_actuation_registry) keep using
# `gen-actuation.validate_unit` etc., while `service/actuation` imports the SAME validator (validate-before-stage).
from kontroll.service._actuation_schema import (  # noqa: F401  (re-exported for callers + tests)
    BLAST, FORBIDDEN_KNOB_TYPES, HIGH_BLAST_GROUPS, INSTALLABLE, KEY_RE, KINDS, KNOB_KEY_RE, KNOB_TYPES,
    SCHEMA, SHA256_RE, SIGNATURES, SOURCES, validate_knobs, validate_unit)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _enabled_modules():
    try:
        with open(paths.resolve("config/fleet.yml"), encoding="utf-8") as fh:
            return paths.enabled_keys(yaml.safe_load(fh))    # class keys only: a line that is not one is no class
    except (OSError, yaml.YAMLError):
        return []


def validate_all(adir, enabled_modules):
    """Validate every actuation/<key>/unit.yml under `adir`; return {dirkey: [errors]} for the failing units
    (empty dict = all valid / none present)."""
    failures = {}
    if not os.path.isdir(adir):
        return failures
    for dirkey in sorted(os.listdir(adir)):
        up = os.path.join(adir, dirkey, "unit.yml")
        if not os.path.isfile(up):
            continue
        try:
            with open(up, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as e:
            failures[dirkey] = ["unreadable / invalid YAML: %s" % e]
            continue
        errs = validate_unit(doc, dirkey, enabled_modules)
        if errs:
            failures[dirkey] = errs
    return failures


def main():
    check = "--check" in sys.argv[1:]
    adir = paths.resolve("actuation")
    failures = validate_all(adir, _enabled_modules())
    if failures:
        for dirkey in sorted(failures):
            for e in failures[dirkey]:
                sys.stderr.write("actuation/%s: %s\n" % (dirkey, e))
        sys.exit(1)
    n = sum(1 for d in (os.listdir(adir) if os.path.isdir(adir) else [])
            if os.path.isfile(os.path.join(adir, d, "unit.yml")))
    print("actuation: %d unit(s) valid%s" % (n, " (--check)" if check else ""))


if __name__ == "__main__":
    main()
