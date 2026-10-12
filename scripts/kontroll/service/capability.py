"""capability — the capability-NEUTRAL orchestration spine of the secondary-capability dialog seam.

Drives the suggest → propose → promote lifecycle over a `capabilities/<cap>.yml` descriptor with ZERO
`if cap==…` branching anywhere. Two import-by-convention seams, both DERIVED from the drop-in descriptor (the
`requirements.generated.yml` "never hand-maintain the list" rule applied to dispatch — there is no god-map):

  * the SUGGESTER (read-only): `descriptor.suggester.{module, suggest, declared, hint}` resolves the detection
    pair (`suggest_<cap>` / `declared_<cap>`). PURE — a suggester writes nothing and could never gate
    onboarding (INVARIANT D*).
  * the SERVICE (write): `descriptor.service.module` resolves the instance's `build_plan(key, selection)` /
    `apply_plan(plan)`. The instance carries the capability-specific bodies (observe.py for telemetry,
    backup.py at Phase 8); registering a capability adds an instance module, never edits THIS file.

`promote.verify_token` is the verbatim propose→promote anti-drift gate. The route layer
(api/routes/capability.py) owns auth + the fail-closed audit + the scoped commit; this owns dispatch + the
token gate + the write. The "promote.py verbatim, zero spine edit per capability" guarantee IS this module.
"""
import importlib
import logging
import os

import yaml

from kontroll import catalog, paths, probe
from kontroll.service._reconfig import verify_no_drop
from kontroll.service.promote import verify_token

log = logging.getLogger("kontroll.service.capability")


def get_descriptor(cap, caps=None):
    """The capabilities/<cap>.yml descriptor for `cap`, or None if no such capability is registered."""
    caps = caps if caps is not None else catalog.load_capabilities()
    return next((c for c in caps if c.get("name") == cap), None)


def _resolve(module_name, fn_name):
    """Import scripts/kontroll/service/<module_name> and return its <fn_name> callable — import-by-convention.
    Raises (Import/Attribute)Error if the descriptor points at a missing module/function; the route turns that
    into a clean error, and test_capability_registry's wiring pin catches a misreferenced descriptor before
    ship (so this never raises for a registered capability in practice)."""
    return getattr(importlib.import_module("kontroll.service." + module_name), fn_name)


def suggest(cap, facts, vectors=None, caps=None):
    """The read-only detection for `cap` on a device's facts — the Stage-0 nudge cell + candidates. Resolves
    descriptor.suggester and calls `suggest_<cap>(facts, vectors, hint)` (the descriptor carries the canonical
    picker hint). PURE; returns the suggester's dict, or None if `cap` is unregistered."""
    d = get_descriptor(cap, caps)
    if not d:
        return None
    vectors = vectors if vectors is not None else catalog.load_vectors()
    fn = _resolve(d["suggester"]["module"], d["suggester"]["suggest"])
    return fn(facts, vectors, d["suggester"].get("hint"))


def declared(cap, collection, fleet=None, caps=None):
    """The DECLARED-side reconciliation for `cap` + a collection — what an enabled module already wired.
    Resolves descriptor.suggester.declared and calls `declared_<cap>(collection, fleet)`. [] if unregistered."""
    d = get_descriptor(cap, caps)
    if not d:
        return []
    return _resolve(d["suggester"]["module"], d["suggester"]["declared"])(collection, fleet)


def suggest_view(cap, key, vectors=None, fleet=None, caps=None):
    """The Stage-0 detection VIEW for opening `cap`'s dialog on onboarded class `key`: the suggester cell +
    candidates, the methods the picker may offer (the instance's `offerable_methods` ∪ what's already
    declared), and the declared reconciliation. Reads modules/<key>/module.yml for the collection (paths.ROOT)
    + deep-probes it READ-ONLY, then folds the three. Returns {"error": "no_capability"|"not_onboarded"|None,
    ...}; writes nothing. The offerable-methods step is descriptor-gated (only when render_method_stage) and
    delegated to the INSTANCE module's optional `offerable_methods` — the spine stays capability-neutral."""
    d = get_descriptor(cap, caps)
    if not d:
        return {"error": "no_capability", "cap": cap}
    mp = paths.module_file(key)
    if not os.path.exists(mp):
        return {"error": "not_onboarded", "cap": cap, "key": key}
    with open(mp, encoding="utf-8") as fh:
        module = yaml.safe_load(fh) or {}
    collection = ((module.get("collections") or [{}])[0] or {}).get("name")
    try:
        facts = probe.deep_probe(collection) if collection else None
    except paths.RequestBoundaryError:
        facts = None      # the class's module file names something that is not `namespace.name`: nothing to probe
    suggestion = suggest(cap, facts, vectors=vectors, caps=caps) if facts else None
    declared_here = declared(cap, collection, fleet, caps=caps) if collection else []
    offerable, reconfigurable, current = [], [], {}
    if facts and d.get("render_method_stage"):
        svc = importlib.import_module("kontroll.service." + d["service"]["module"])
        picker = getattr(svc, "offerable_methods", None)
        current = getattr(svc, "current_values", lambda k: {})(key)   # the declared values (reconfigure pre-fill, Phase 4a)
        if picker:
            declared_names = {n for entry in declared_here if entry["key"] == key for n in entry["methods"]}
            all_methods = picker(facts)
            offerable = [m for m in all_methods if m["name"] not in declared_names]      # NEW methods (add path)
            reconfigurable = [m for m in all_methods if m["name"] in declared_names]      # DECLARED methods (reconfigure path)
    return {"error": None, "cap": cap, "key": key, "collection": collection,
            "suggestion": suggestion, "offerable_methods": offerable,
            "reconfigurable_methods": reconfigurable, "current": current, "declared": declared_here}


def propose(cap, key, selection, caps=None):
    """PROPOSE (apply:false): dispatch to the instance's `build_plan(key, selection)` and return the PURE plan
    (+ token_parts + plan_token + enact). {"error": "no_capability"} if `cap` is unregistered. Writes nothing
    — this is the dialog's review step."""
    d = get_descriptor(cap, caps)
    if not d:
        return {"error": "no_capability", "cap": cap}
    return _resolve(d["service"]["module"], "build_plan")(key, selection)


def promote(cap, key, selection, token, caps=None):
    """PROMOTE (apply:true): RECOMPUTE the plan, refuse (error 'drift') unless `token` still matches it — the
    propose→promote anti-drift gate (the data may have moved under the operator) — then apply the write.
    Returns {"error": None, "plan", "changed", "paths"} on success, else the plan's own error / 'drift' /
    'no_capability'. Capability-neutral: the token is verified over the plan's generic `token_parts`, so
    telemetry's and backup's promote flow through this IDENTICAL function."""
    d = get_descriptor(cap, caps)
    if not d:
        return {"error": "no_capability", "cap": cap}
    mod_name = d["service"]["module"]
    plan = _resolve(mod_name, "build_plan")(key, selection)
    if plan.get("error"):
        return plan
    if not verify_token(token, *plan.get("token_parts", [])):       # anti-DRIFT (also no-quiet-upgrade: the
        log.info("capability %s promote refused for %s: plan drifted since propose", cap, key)  # severity is in token_parts, M1)
        return {"error": "drift", "cap": cap, "key": key, "changes": plan.get("changes")}   # diff-on-409 so the UI shows what moved (G6)
    # anti-CLOBBER machine gate. In the single-object flow `proposed` is additive (current ∪ one method), so this
    # is defense-in-depth (inert today — verify_token already catches the cross-seam race); it becomes load-bearing
    # when a remove-producing or aggregate reconfigure surface ships (Phase 4b). Fail-closed by construction.
    if not verify_no_drop(plan.get("current") or {}, plan.get("proposed") or {}, plan.get("changes") or []):
        log.warning("capability %s: reconfigure would drop co-owner data for %s — refusing", cap, key)
        return {"error": "would_drop", "cap": cap, "key": key, "changes": plan.get("changes")}
    result = _resolve(mod_name, "apply_plan")(plan)
    return {"error": None, "plan": plan, **result}
