"""refresh domain — rebuild the capability-matrix cache for every installed collection.

service_refresh deep-probes every installed collection, writes the derived records to
the (gitignored) cache, and returns {"count","path","records"}; the CLI prints the
one-line summary.
"""
import json
import logging

from kontroll import catalog, paths, probe, record

log = logging.getLogger("kontroll.refresh")


def service_refresh(vectors=None, overrides=None, backends=None):
    """Deep-probe every locally-installed collection, write the derived records to the
    capability-matrix cache, and return {"count","path","records"}."""
    vectors = vectors if vectors is not None else catalog.load_vectors()
    overrides = overrides if overrides is not None else catalog.load_overrides()
    backends = backends if backends is not None else catalog.load_backends()
    records = []
    for coll, ver in sorted(catalog.local_installed().items()):
        try:
            facts = probe.deep_probe(coll, ver)
        except paths.RequestBoundaryError:
            # a directory under ansible_collections/ whose name is not `namespace.name`: never an argv word
            # (SECURITY.md C22), and one stray directory must not fail the whole refresh
            log.warning("refresh: skipping an installed entry whose name is not a collection name")
            continue
        records.append(record.build_record(facts, vectors, overrides, backends))
    with open(paths.MATRIX_CACHE, "w", encoding="utf-8") as fh:
        json.dump({"records": records}, fh, indent=2)
    return {"count": len(records), "path": paths.MATRIX_CACHE, "records": records}
