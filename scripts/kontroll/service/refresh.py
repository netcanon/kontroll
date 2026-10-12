"""refresh domain — rebuild the capability-matrix cache for every installed collection.

service_refresh deep-probes every installed collection, writes the derived records to
the (gitignored) cache under the WRITE root, and returns {"count","path","records"}; the
CLI prints the one-line summary.
"""
import json
import logging

from kontroll import catalog, paths, probe, record

log = logging.getLogger("kontroll.refresh")


def service_refresh(vectors=None, overrides=None, backends=None):
    """Deep-probe every locally-installed collection, write the derived records to the
    capability-matrix cache (paths.matrix_cache() — under write_root(), so a baked deploy's
    read-only code tree is never the target), and return {"count","path","records"}."""
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
    cache = paths.matrix_cache()
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump({"records": records}, fh, indent=2)
    return {"count": len(records), "path": cache, "records": records}
