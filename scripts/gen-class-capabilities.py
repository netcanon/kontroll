#!/usr/bin/env python3
"""Validate that every DERIVED device class's pinned capability FLOOR still matches what the F2 derivation
recomputes — the no-bespoke `--check` teeth for the metrics:/logs: floor (the analogue of gen-observability
--check, but the "lockfile" is the module.yml's own `derived: true`-marked entries, not a separate file).

A class marked `derived: true` in modules/<key>/module.yml carries an AUTO-DERIVED metrics:/logs: floor
(classify.derive_class_capabilities) — the agent-less snmp/blackbox metrics + syslog logs a blind onboard gets
with zero curation. To keep that floor from rotting when the world churns (a method's `derive_default` changes, a
backend's confer list changes, a new universal method ships), this generator RE-DERIVES the floor from the class's
PINNED offline facts (modules/<key>/facts.pinned.yml) + the static registries and asserts the module's
`derived: true`-marked entries still MATCH. A drift fails CI, naming the exact class + capability.

OFFLINE + hermetic (BRICK-1): reads only the committed facts.pinned.yml + the registries; NEVER re-probes (that
needs the collection installed + is non-deterministic). The onboard path is the WRITER (it derives + pins a new
class's floor at onboard time); this is the CHECKER. Bare (operator-curated) metrics/logs entries — the vendor
override tail — are NOT checked; only `derived: true`-marked entries are owned here.

Standalone except for kontroll.catalog/paths/predicate + the classify derivation (no ansible); mirrors
gen-observability.py so CI + deploy-stack can run it.

Usage:  python3 scripts/gen-class-capabilities.py [--check]   # --check validates; bare prints the result
"""
import os
import sys

import yaml

from kontroll import catalog, paths, predicate
from kontroll.service import classify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _derived_entries(block):
    """The `derived: true`-marked entries of a metrics:/logs: block, with the marker stripped — the floor this
    generator owns. Bare (operator-curated) entries are NOT checked (they are the vendor override tail, §3.6)."""
    return [{k: v for k, v in e.items() if k != "derived"}
            for e in (block or []) if isinstance(e, dict) and e.get("derived")]


def _backend_for(facts, module, backends):
    """The backend a derived class's floor is computed for: the module's declared `backend` (onboarded classes
    carry one), else RE-CLASSIFY from the pinned facts (a curated class like cisco_ios declares a role, not a
    backend). Returns None when no backend can be determined."""
    return module.get("backend") or (predicate.classify(facts, backends)[:1] or [None])[0]


def drift(fleet, backends=None, telemetry=None, logging=None, locks=None):
    """-> a list of human-readable drift messages (empty = in sync). For each ENABLED `derived: true` class: load
    its facts.pinned.yml, recompute the metrics/logs/dashboards floor, and compare to the module's derived-marked
    entries. `locks` = the dashboards/derived pins (Rung 4a); defaults to the loaded pins."""
    backends = backends if backends is not None else catalog.load_backends()
    telemetry = telemetry if telemetry is not None else catalog.load_telemetry()
    logging = logging if logging is not None else catalog.load_logging()
    locks = locks if locks is not None else catalog.load_dashboard_locks()
    msgs = []
    for key in paths.enabled_keys(fleet):
        mp = os.path.join(ROOT, "modules", key, "module.yml")
        if not os.path.exists(mp):
            continue
        module = _load(mp)
        if not module.get("derived"):
            continue
        fp = os.path.join(ROOT, "modules", key, "facts.pinned.yml")
        if not os.path.exists(fp):
            msgs.append("%s is `derived: true` but has no facts.pinned.yml (re-onboard, or pin its facts)" % key)
            continue
        facts = _load(fp)
        backend = _backend_for(facts, module, backends)
        if backend is None:
            msgs.append("%s: cannot classify a backend from facts.pinned.yml" % key)
            continue
        want = classify.derive_class_capabilities(facts, backend, telemetry=telemetry,
                                                  logging=logging, backends=backends, locks=locks)
        for cap in ("metrics", "logs", "dashboards"):
            have = _derived_entries(module.get(cap))
            if have != want.get(cap, []):
                msgs.append("%s %s: derived floor drift — re-derive (the module's `derived: true` entries no "
                            "longer match)\n      pinned:     %s\n      recomputed: %s"
                            % (key, cap, have, want.get(cap, [])))
    return msgs


def main(argv):
    # --check reads the PUBLIC example fleet (like gen-observability/gen-logging) so the committed derived floors
    # are validated hermetically; a bare/live run reads the instance overlay (an onboarded class's pinned floor).
    if "--check" in argv:
        fleet = _load(os.path.join(ROOT, "instance.example", "fleet.yml"))
    else:
        fleet = _load(paths.resolve("config/fleet.yml"))
    for problem in paths.fleet_problems(fleet):              # skipped, and said: the position, never the entry
        sys.stderr.write("gen-class-capabilities: ignoring: instance/fleet.yml: %s\n" % problem)
    msgs = drift(fleet)
    if "--check" in argv:
        if msgs:
            print("STALE derived class capabilities — a class's `derived: true` floor no longer matches the "
                  "derivation:\n  " + "\n  ".join(msgs))
            return 1
        print("derived class capabilities up to date")
        return 0
    print("derived class capabilities: %s" % ("DRIFT\n  " + "\n  ".join(msgs) if msgs else "in sync"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
