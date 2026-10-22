"""search domain — the unified Galaxy + local capability search.

service_search runs the source -> record -> filter -> sort pipeline and RETURNS the
records (no printing); the CLI (galaxy.py cmd_search) renders or JSON-dumps them.

By DEFAULT local hits are SHALLOW (files only, no ansible-doc — sub-second): the cells that
need the deep per-module doc parse read as 'maybe', resolved on demand. `deep=True` (or any
`vector` filter, which needs exact cells) re-probes the keyword-matched candidates deeply. The
I/O calls (local_shallow / deep_probe / galaxy_search) go through their home modules, so tests
patch kontroll.catalog.* / kontroll.probe.deep_probe.

service_search_parity verifies the shallow result against the full deep probe (the accuracy
guarantee: shallow ⊑ deep — a cell only ever softens yes/no to 'maybe', never flips yes↔no).
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from kontroll import catalog, probe, record

log = logging.getLogger("kontroll.service.search")
# deep_probe (ansible-doc) per candidate is ~seconds + CPU-bound; the candidate set is already capped by
# local_shallow's `limit`, so deepen them concurrently (a serial fan-out hung the deployed /search).
_DEEP_PROBE_WORKERS = 8


def _deepen(shallow_facts):
    """Re-probe each shallow local candidate with deep_probe (ansible-doc) CONCURRENTLY, carrying the
    shallow MANIFEST metadata (description/tags) across since deep_probe leaves it blank, and tolerating a
    single collection's failure (fall back to its shallow facts rather than 500 the whole search). The
    candidate set is already capped by local_shallow's `limit`, so the fan-out is bounded."""
    def _probe_one(f):
        try:
            d = probe.deep_probe(f["collection"], f["version"])
            d["description"] = d["description"] or f.get("description", "")
            d["tags"] = d["tags"] or f.get("tags", [])
            d["certified"] = d["certified"] or f.get("certified", False)
            return d
        except Exception:
            log.warning("deep_probe(%s) failed during deep search; keeping shallow facts", f["collection"])
            return f
    if not shallow_facts:
        return []
    with ThreadPoolExecutor(max_workers=_DEEP_PROBE_WORKERS) as ex:
        return list(ex.map(_probe_one, shallow_facts))


def _cards(keywords, vectors, overrides, backends):
    """The records of the backend registry's search cards a query matches (catalog.search_cards) — the commonest
    Ansible target, a plain SSH host, has no collection to find. A query keyword matches a card the way local_shallow
    matches a collection name (a substring, case-folded) — of the card's collection or one of its keywords, not of
    its title, whose prose ("backup", "metrics") would offer the card to unrelated queries. Built from declared data
    after any deepening, so a card is never probed."""
    kw = [k.lower() for k in keywords]
    return [record.build_card_record(bdef, card, vectors, overrides, backends)
            for bdef, card in catalog.search_cards(backends)
            if any(k in h.lower() for k in kw for h in [card["collection"], *card["keywords"]])]


def service_search(keywords, origin="both", limit=8, vector=None, deep=False,
                   vectors=None, overrides=None, backends=None):
    """Return the capability RECORDS matching `keywords`: installed collections (SHALLOW by default — fast,
    files only) with the backend registry's search cards the query matches (the local arm; `_cards`) and/or
    Galaxy (shallow), filtered to those that HAVE every capability in `vector`, sorted
    local-first then by name. `deep=True` (or a non-empty `vector`, since a capability filter needs exact
    cells) re-probes the keyword-matched local candidates deeply, resolving the deferred 'maybe' cells.
    Loaders default to the real tree; callers may inject vectors/overrides/backends to avoid reloading."""
    vectors = vectors if vectors is not None else catalog.load_vectors()
    overrides = overrides if overrides is not None else catalog.load_overrides()
    backends = backends if backends is not None else catalog.load_backends()
    records, seen = [], set()
    if origin in ("both", "local"):
        local_facts = catalog.local_shallow(keywords, limit)
        if deep or vector:                  # a capability filter needs exact (deep) cells; shallow defers them
            local_facts = _deepen(local_facts)
        for f in local_facts:
            records.append(record.build_record(f, vectors, overrides, backends))
            seen.add(f["collection"])
        for rec in _cards(keywords, vectors, overrides, backends):
            if rec["collection"] not in seen:
                records.append(rec)
                seen.add(rec["collection"])
    if origin in ("both", "galaxy"):
        try:
            galaxy = catalog.galaxy_search(keywords, limit)
        except Exception:                  # galaxy is a best-effort external dep; never sink the whole search
            galaxy = []
        for f in galaxy:
            if f["collection"] not in seen:
                records.append(record.build_record(f, vectors, overrides, backends))
                seen.add(f["collection"])
    if vector:
        records = [r for r in records
                   if all(r["capabilities"].get(v, {}).get("state") == "yes" for v in vector)]
    records.sort(key=lambda r: (r["origin"] != "local", r["collection"]))
    return records


def service_search_parity(keywords, limit=8, vectors=None, overrides=None, backends=None):
    """Verify the SHALLOW local search against the full DEEP probe over the SAME matched candidates — the
    repeatable form of 'is the fast search accurate vs the lengthy one'. For every capability cell, compare
    the shallow vs deep state and classify the disagreement:
      * 'agree'    — shallow == deep (signal visible at both depths; the common case)
      * 'deferred' — shallow == 'maybe', deep is definite (the HONEST degradation: a module_option-only
                     signal the shallow probe can't see; deep_probe / GET /probe resolves it)
      * 'unsound'  — shallow and deep disagree on yes vs no (a FLIP — shallow would be lying; MUST be 0)
    Returns {collections:[{collection,vector,shallow,deep,verdict}...], summary:{checked,cells,agree,
    deferred,unsound}}. unsound>0 means the soundness invariant (shallow ⊑ deep) broke — run live over the
    real fleet; it must always read 0."""
    vectors = vectors if vectors is not None else catalog.load_vectors()
    overrides = overrides if overrides is not None else catalog.load_overrides()
    backends = backends if backends is not None else catalog.load_backends()
    shallow_facts = catalog.local_shallow(keywords, limit)
    rows, agree, deferred, unsound = [], 0, 0, 0
    for sf in shallow_facts:
        coll = sf["collection"]
        try:
            df = probe.deep_probe(coll, sf["version"])
        except Exception:
            log.warning("deep_probe(%s) failed during parity check; skipping", coll)
            continue
        srec = record.build_record(sf, vectors, overrides, backends)
        drec = record.build_record(df, vectors, overrides, backends)
        for v in vectors:
            name = v["name"]
            ss = srec["capabilities"][name]["state"]
            ds = drec["capabilities"][name]["state"]
            if ss == ds:
                verdict, agree = "agree", agree + 1
            elif ss == "maybe":
                verdict, deferred = "deferred", deferred + 1
            else:
                verdict, unsound = "unsound", unsound + 1
            rows.append({"collection": coll, "vector": name, "shallow": ss, "deep": ds, "verdict": verdict})
    return {"collections": rows,
            "summary": {"checked": len(shallow_facts), "cells": len(rows),
                        "agree": agree, "deferred": deferred, "unsound": unsound}}
