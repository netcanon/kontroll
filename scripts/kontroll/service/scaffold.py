"""scaffold domain — emit a (staged) device-class module declaration for a collection.

build_scaffold computes the module dict + its rendered YAML text + the target rel path,
or an error code ("not_installed" / "no_backend"); the CLI owns the dry-run/write gate
and the not-installed/no-backend exit messages.
"""
import os

import yaml

from kontroll import catalog, paths, predicate, probe
from kontroll.service.onboard import _plain_name, _role_of


def build_scaffold(collection, key, group, secrets, backend=None, backends=None):
    """Compute the scaffolded device-class module for `collection`. Returns a dict with
    "error": "not_installed" (probe found nothing) | "no_backend" (none auto-matched and
    none given) | None; on success {"error":None,"backend","module","text","path"} where
    `text` is the YAML to write and `path` is repo-relative.

    The names are held to the onboard planner's own rules, in its order, before anything is probed — and the
    role it takes from the registry or derives to the role's, like the planner's: this is another writer of a
    class's identity keys (with the onboard and the identity reconfigure), and the file it writes is what a later
    onboard ADOPTS."""
    paths.collection_fqcn(collection)
    paths.component(key, "device-class key")
    _plain_name(key, "device-class key")
    paths.inventory_group(group)
    _plain_name(group, "inventory group")
    paths.component(secrets, "secrets domain")
    _plain_name(secrets, "secrets domain")
    backend_from_request = backend is not None
    if backend_from_request:
        paths.component(backend, "backend")
        _plain_name(backend, "backend")
    backends = backends if backends is not None else catalog.load_backends()
    f = probe.deep_probe(collection)
    if not f["modules"] and not f["plugins"]:
        return {"error": "not_installed"}
    backend = backend or (predicate.classify(f, backends)[:1] or [None])[0]
    if not backend:
        return {"error": "no_backend"}
    bdef = next((b for b in backends if b["name"] == backend), {})
    requires = bdef.get("requires") or []
    parts = collection.split(".")
    version = catalog.local_installed().get(collection) or f["version"] or "0.0.0"
    params = {}
    if "network_os" in requires:                 # ns.name -> ns.name.name (verify)
        params["network_os"] = "%s.%s" % (collection, parts[-1])
    module = {
        "key": key,
        "description": "%s via %s backend (auto-scaffolded - VERIFY before enabling)" % (collection, backend),
        "status": "staged",
        "collections": [{"name": collection, "version": ">=%s" % version}],
        "role": _role_of(bdef, backend, key, backend_from_request),
        "backend": backend,
    }
    if params:
        module["backend_params"] = params
    module["inventory_group"] = group
    module["secrets_domain"] = secrets
    text = "# Auto-scaffolded by scripts/galaxy.py. Verify network_os + commands\n" \
           "# against a real device (live-smoke) before flipping status to active.\n" \
           + yaml.safe_dump(module, sort_keys=False, default_flow_style=False,
                            allow_unicode=True, width=4096)
    return {"error": None, "backend": backend, "module": module, "text": text,
            "path": os.path.join("modules", key, "module.yml")}
