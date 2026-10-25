"""scaffold domain — emit a (staged) device-class module declaration for a collection.

build_scaffold computes the module dict + its rendered YAML text + the target rel path,
or an error code ("not_installed" / "no_backend"); the CLI owns the dry-run/write gate
and the not-installed/no-backend exit messages.
"""
import os

import yaml

from kontroll import catalog, paths, predicate
from kontroll.service.onboard import (_facts_for, _pins, _plain_name, _registered, _role_of, _secrets_domain,
                                      backend_params, no_backend_signals)


def build_scaffold(collection, key, group, secrets=None, backend=None, backends=None):
    """Compute the scaffolded device-class module for `collection`. Returns a dict with
    "error": "not_installed" (probe found nothing) | "no_backend" (no backend that declares a role
    auto-matched, and none given — with the planner's `signals`, for its sentence) | None; on success
    {"error":None,"backend","module","text","path"} where
    `text` is the YAML to write and `path` is repo-relative.

    The names are held to the onboard planner's own rules, in its order, before anything is probed — a `backend`
    must be one the registry ships — and the role is the one the backend's registry entry declares, held to the
    role's rule, like the planner's: this is another writer of a class's identity keys (with the onboard and the
    identity reconfigure), and the file it writes is what a later onboard ADOPTS. With no `backend`, it chooses as
    the planner does: the first match that declares a role. With no `secrets`, the class stores its credentials in
    the backend's `default_secrets_domain`, and its `backend_params` are what the backend derives — the planner's
    own helpers (S27)."""
    paths.collection_fqcn(collection)
    paths.component(key, "device-class key")
    _plain_name(key, "device-class key")
    paths.inventory_group(group)
    _plain_name(group, "inventory group")
    if secrets is not None:
        paths.component(secrets, "secrets domain")
        _plain_name(secrets, "secrets domain")
    backend_from_request = backend is not None
    if backend_from_request:
        paths.component(backend, "backend")
        _plain_name(backend, "backend")
    backends = backends if backends is not None else catalog.load_backends()
    if backend_from_request:
        _registered(backend, backends)
        _role_of(next((b for b in backends if b.get("name") == backend), {}), key, True)   # before the probe
    bundled = catalog.bundled_collections(backends)
    f = _facts_for(collection, bundled)
    if f is None:
        return {"error": "not_installed"}
    if not backend:
        matches = predicate.classify(f, backends)
        backend = next(iter(catalog.runnable(matches, backends)), None)
        if not backend:
            return {"error": "no_backend", "signals": no_backend_signals(f, matches)}
    bdef = next((b for b in backends if b.get("name") == backend), {})
    secrets = _secrets_domain(secrets, bdef, backend)
    params = backend_params(bdef, collection, backend)
    module = {
        "key": key,
        "description": "%s via %s backend (auto-scaffolded)" % (collection, backend),
        "collections": _pins(collection, f, bundled),
        "role": _role_of(bdef, key, backend_from_request),
        "backend": backend,
    }
    if params:
        module["backend_params"] = params
    module["inventory_group"] = group
    module["secrets_domain"] = secrets
    text = "# Auto-scaffolded by scripts/galaxy.py. Verify network_os + commands against a real device\n" \
           "# before you enable it.\n" \
           + yaml.safe_dump(module, sort_keys=False, default_flow_style=False,
                            allow_unicode=True, width=4096)
    return {"error": None, "backend": backend, "module": module, "text": text,
            "path": os.path.join("modules", key, "module.yml")}
