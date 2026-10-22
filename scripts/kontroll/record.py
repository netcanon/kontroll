"""facts -> RECORD (the result schema).

A record is top-level metadata + a `capabilities` block (one {state,confidence,evidence}
cell per vector) + the suggested/candidate execution backends, with the overrides/ layer
merged at read time (an override patches a capability cell and stamps provenance). Pure;
the contract under test in tests/unit/test_build_record.py. Lifted verbatim from galaxy.py.
"""
from kontroll import catalog, predicate, probe


def build_record(facts, vectors, overrides=None, backends=None):
    """The record for one collection. `backend_candidates` is every backend the classifier matches, in its order;
    `suggested_backend` is the first of them that declares an execution role — the one the onboard planner would
    choose, and the one the GUI's onboard form sends and loads its credential fields for. A match that declares none
    (`vendor_config`) is classifier metadata: suggested, the form would send a backend the planner refuses (S26)."""
    matches = predicate.classify(facts, backends or [])
    rec = {
        "collection": facts["collection"],
        "version": facts["version"],
        "origin": facts["origin"],
        "depth": facts["depth"],
        "meta": {"description": facts["description"], "tags": facts["tags"],
                 "certified": facts["certified"]},
        "capabilities": {v["name"]: predicate.eval_vector(v, facts) for v in vectors},
        "suggested_backend": next(iter(catalog.runnable(matches, backends or [])), None),
        "backend_candidates": matches,
        "provenance": "derived",
    }
    ov = (overrides or {}).get(facts["collection"])
    if ov:
        for name, patch in (ov.get("capabilities") or {}).items():
            patch = dict(patch)
            # YAML parses `yes`/`no` as booleans; normalize back to our state strings.
            if patch.get("state") is True:
                patch["state"] = "yes"
            elif patch.get("state") is False:
                patch["state"] = "no"
            rec["capabilities"][name] = {**patch, "provenance": "override"}
        if ov.get("note"):
            rec["meta"]["note"] = ov["note"]
        rec["provenance"] = "derived+override"
    return rec


def build_card_record(bdef, card, vectors, overrides=None, backends=None):
    """The record for a backend's SYNTHETIC search card (catalog.search_cards): the record of the collection's
    empty, declared facts — nothing is probed, there is nothing to probe — in which the declaring backend names
    itself: `suggested_backend` and `backend_candidates` are that backend, `provenance` is "declared",
    `meta.description` is the card's title and `suggested_key` the class key the onboard form proposes. The GUI's
    form sends the suggestion as the request's `backend`, so a card onboards with the backend that declared it."""
    rec = build_record(probe._facts(card["collection"], None, "local", "declared"), vectors, overrides, backends)
    rec.update(suggested_backend=bdef["name"], backend_candidates=[bdef["name"]], provenance="declared",
               suggested_key=card["key"])
    rec["meta"]["description"] = card["title"]
    return rec
