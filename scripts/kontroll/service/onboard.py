"""onboard domain — the operator's one-shot: collection + host -> a managed device.

Split into the API-ready shape the workstream calls for:
  * build_onboard_plan — PURE (reads only: deep_probe + loaders). Computes the whole
    plan (module + drop-in host blocks, rendered YAML, rel paths, creds-to-set) and
    returns it; no writes, no prints. An error code ("not_installed"/"no_backend") if a
    precondition fails (CLI owns the message).
  * apply_onboard_plan — performs the repo mutations for a plan (idempotent no-clobber
    writes + fleet-enable + SOPS encrypt), prints progress (the exemplar's pattern), and
    returns {"changed","paths"}.
Onboarding a device whose CLASS already exists REUSES the curated module (adds only the host
+ fleet-enable) rather than regenerating a minimal one that would strip its metrics:/backup:/
logs: blocks (#124); the inventory drop-in MERGES so a 2nd device of a class never drops the
1st. A genuinely DIFFERENT class colliding on a key still hard-stops (WriteConflict) unless the
operator deliberately Overwrites.
The commit/push-canonical GATE stays in the CLI wrapper (galaxy.py cmd_onboard), exactly
as the capture-exception exemplar keeps it. enable_in_fleet lives here as an
onboard-domain helper. Credential VALUES flow through creds_to_set in memory only — the
plan display shows var NAMES, never values, and they are never logged.
"""
import logging
import os
import re

import yaml

from kontroll import authspec, catalog, gitio, paths, predicate, probe
from kontroll.service._validate import plain_scalar
from kontroll.service.classify import derive_class_capabilities, suggest_telemetry

log = logging.getLogger("kontroll.service.onboard")

# Drop-in banners — also the OWNERSHIP markers gitio.write_inventory_host keys its merge-vs-conflict decision on
# (a file that opens with our inventory banner is one we wrote, so a 2nd host MERGES; a foreign file hard-stops).
_MODULE_BANNER = ("# Onboarded by scripts/galaxy.py. Nothing verifies it for you: run ping.yml --limit <host> "
                  "after promote.\n")
_INV_BANNER = "# Drop-in host, onboarded by scripts/galaxy.py onboard. Additive — safe to delete to remove the host.\n"


def _existing_module(key):
    """Parse modules/<key>/module.yml via the modules overlay (paths.module_file — the write_root clone shadows the
    baked ROOT, Phase-B Fork B) if it exists, else None. This is the SHIPPED/already-onboarded device-class we must
    REUSE rather than clobber when onboarding another device of the same class (#124): regenerating a minimal module
    here would strip the curated metrics:/backup:/logs: blocks. Resolved so a baked deploy sees an operator class in
    the propose clone AND a pristine shipped class in the image; a file that cannot be read or parsed degrades to
    None (treated as absent → the normal generate path, which then no-clobbers). A file that parses to something
    other than a mapping — a list, a bare word — is neither absent nor a class: it is answered as the file it is
    (RepositoryDataError), not as the AttributeError its first `.get` would be."""
    mp = paths.module_file(key)
    if not os.path.exists(mp):
        return None
    try:
        with open(mp, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise paths.RepositoryDataError(
            "module file", "the %s device class's module file is not a mapping — correct modules/%s/module.yml"
            % (key, key))
    return doc


def _module_collections(mod):
    """The collection NAMES a module declares (modules/<key>/module.yml collections[].name), as a set; a block
    that is not a list, and an entry whose name is not a string, declare none."""
    block = mod.get("collections")
    return {c["name"] for c in (block if isinstance(block, list) else [])
            if isinstance(c, dict) and isinstance(c.get("name"), str)}


_STEM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# The first name a backend's `connection:` value reads (`{{ network_os | lower }}` -> network_os).
_HOLE_NAME_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)")
_NOT_A_NAME = "%s is a word YAML reads as another type (null, a boolean, a number, a date)"


def _plain_name(value, field):
    """A request field that becomes a NAME somebody later writes unquoted — the class key into the fleet list,
    the inventory group into the generated Prometheus targets and backup schedules, the secrets domain and the
    backend into the module file's readers — is one path component (the caller checked that) AND a word YAML reads
    as a string. `null`, `true`, `123` and a date are inside the charset; a group named `null` was stored quoted
    in the module file and then written bare by the generators that read it back."""
    if not plain_scalar(value):
        raise paths.RequestBoundaryError(field, _NOT_A_NAME % field)


_ADOPTED = ("backend", "secrets_domain", "inventory_group", "role")


def _adopted(module, field, key):
    """A name the reuse path takes from an EXISTING module file instead of from the request (one of _ADOPTED).
    It is held to the rule the request field of the same meaning is held to — one path component that YAML reads
    as a string, and for the group not a name Ansible reserves — because it goes where that field would have gone:
    a path, an inventory key, a host var Ansible templates, a lookup expression. Absent (or null) means "use the
    request's own, or the registry's". Anything else that fails is the FILE's fault, never the caller's: a
    RepositoryDataError that names the class, the key of the file and the RULE it breaks — the inner refusal's own
    sentence, which names the key and the rule and never the value."""
    value = module.get(field)
    if value is None:
        return None
    try:
        if field == "inventory_group":
            paths.inventory_group(value, field)
        else:
            paths.component(value, field)
        _plain_name(value, field)
    except paths.RequestBoundaryError as refused:
        raise paths.RepositoryDataError(
            "module file", "the %s device class's module file: %s — correct modules/%s/module.yml"
            % (key, refused, key)) from None
    return value


def _registered(backend, backends):
    """A backend the REQUEST names is one the registry ships (a directory under ansible/backends/), checked before
    anything is probed. The planner used to make a role name up for any other (`backend_<name>`) and write it into
    the module file and every host's `device_role`, where no play could include it (S26). Refused as the `backend`
    field, never naming the value."""
    if backend not in {b.get("name") for b in backends}:
        raise paths.RequestBoundaryError("backend", "backend is not one this install ships (a directory under "
                                         "ansible/backends/)")


def _no_made_up_role(role, key, backends):
    """Stop a reuse whose module file names the role the planner made up, before S26, for a shipped backend that
    declares none (`backend_vendor_config`, `backend_napalm`): no play can include it, and the reuse would give every
    new host that `device_role`. Answered as repository data, the role itself not quoted. Every other role passes,
    `backend_` or not — an operator's own role may carry the prefix, and whether a role exists under ansible/roles/
    is the linker's question (S36), not the planner's; a class written for a backend string the registry never
    shipped (`backend_<string>`) is what the CHANGELOG's upgrade grep finds."""
    if role in catalog.made_up_roles(backends):
        raise paths.RepositoryDataError(
            "module file", "the %s class's module file names the role kontroll used to make up for a backend that "
            "declares none (written before S26) — give the class a real `role:` in modules/%s/module.yml"
            % (key, key))


def _role_of(bdef, key, from_request):
    """The role of a class no module file gives one: the one its backend's registry entry DECLARES — never one made up
    from the backend's name (S26: `vendor_config` and `napalm` declare none, and a class written with
    `backend_vendor_config` reached a play that could not include it, its failure rescued into a bare UNREACHABLE).
    A backend that declares none cannot run a check or a backup, so a plan for it stops: the request's own `backend`
    is refused as that field; a backend the request did not send — adopted from a module file that names no role
    either — is that file's fault, answered as repository data naming the class and never the backend. (The
    classifier's choice never lands here: the planner chooses only a backend that declares a role.) A declared role
    is written into the module file and adopted by the next onboard, so it is held to the adopted role's rule here;
    one that fails is repository data too, naming the class only."""
    role = catalog.backend_role(bdef)
    if role is None:
        if from_request:
            raise paths.RequestBoundaryError(
                "backend", "backend is one kontroll can classify a collection to but cannot run: its registry entry "
                "declares no execution role (`role:`) — choose one that does")
        raise paths.RepositoryDataError(
            "backend", "the %s device class's module file names no role, and its backend declares none either (or "
            "is not one this install ships) — give the class a `role:` (modules/%s/module.yml)" % (key, key))
    if not (paths.is_component(role) and plain_scalar(role)):
        raise paths.RepositoryDataError(
            "backend", "the backend registry entry chosen for the %s device class holds a `role` that is not a "
            "valid name — correct that entry (its backend.yml under ansible/backends/)" % key)
    return role


def default_secrets_domain(bdef, backend):
    """The SOPS domain a class of `backend` stores its credentials in when neither the request nor a reused class
    names one: what its registry entry declares (`default_secrets_domain`, S27 — `network` for every shipped
    backend), or None where it declares none. It becomes a path, the module file's `secrets_domain` and part of a
    lookup expression, so a declared one is held to the request field's rule; one that fails is the registry's
    fault, answered as repository data naming the backend (a registry name), never as the request's."""
    domain = (bdef or {}).get("default_secrets_domain")
    if domain is None:
        return None
    if not (paths.is_component(domain) and plain_scalar(domain)):
        raise paths.RepositoryDataError(
            "backend registry", "the %s backend's default_secrets_domain is not a valid name — correct its "
            "backend.yml under ansible/backends/" % backend)
    return domain


def _secrets_domain(secrets, bdef, backend):
    """The domain a plan or a scaffold stores credentials in: the one already chosen (the request's, or a reused
    class's), else the backend's default. With neither, the request must name one: refused as the `secrets domain`
    field (a 422), since naming one is the remedy."""
    secrets = secrets or default_secrets_domain(bdef, backend)
    if secrets is None:
        raise paths.RequestBoundaryError(
            "secrets domain", "secrets domain is required: the backend's registry entry declares no default "
            "(default_secrets_domain)" if bdef else "secrets domain is required: the backend is not one this "
            "install ships, so it declares no default")
    return secrets


def backend_params(bdef, collection, backend):
    """The params a NEW class of `backend` is written with (its module file's `backend_params`): each param the
    registry entry DERIVES (`derive_params`, S27 — netcommon_cli's `network_os: "{collection}.{name}"` turns
    cisco.ios into cisco.ios.ios) that it also requires, rendered from the collection's names by
    catalog.render_template. A required param it does not derive is the operator's to set at run time (raw_ssh's
    backup command, a group var its role reads); the planner refuses one the backend's `connection:` block reads.
    A block or a template that does not render is the registry's fault, refused as repository data before any
    write. Shared by the planner and the scaffold."""
    derive = bdef.get("derive_params")
    if derive is None:
        return {}
    where = ("backend registry", "the %s backend's derive_params is not a map of params to templates that render "
             "to a param value — correct its backend.yml under ansible/backends/" % backend)
    if not isinstance(derive, dict):
        raise paths.RepositoryDataError(*where)
    namespace, name = collection.split(".", 1)
    ctx = {"collection": collection, "namespace": namespace, "name": name}
    requires = bdef.get("requires") or []
    params = {}
    for param, tmpl in derive.items():
        if not (isinstance(param, str) and _STEM_RE.fullmatch(param)):
            raise paths.RepositoryDataError(*where)    # a key of `backend_params`, matched to `connection:` holes
        if param in requires:
            try:
                params[param] = catalog.render_template(tmpl, ctx, catalog.PARAM_RE)
            except ValueError:
                raise paths.RepositoryDataError(*where) from None
    return params


def _facts_for(collection, bundled):
    """The facts a plan or a scaffold is built from, or None when the collection is not installed. A collection
    that ships inside ansible-core (`bundled` — a search card's, the plain SSH host) gets its empty DECLARED facts,
    what openwrt pins: it is always installed and there is nothing to read, so it costs no ansible-doc call and
    `not_installed` cannot misfire for it (S24)."""
    if collection in bundled:
        return probe._facts(collection, None, "local", "declared")
    f = probe.deep_probe(collection)
    return f if (f["modules"] or f["plugins"]) else None


def _pins(collection, f, bundled):
    """What a NEW class's module file pins: nothing for a bundled collection — ansible-core ships it, and a pin would
    send the next bootstrap's `ansible-galaxy` after a collection Galaxy does not have (S24) — else the collection
    at its installed version or newer."""
    if collection in bundled:
        return []
    version = catalog.local_installed().get(collection) or f["version"] or "0.0.0"
    return [{"name": collection, "version": ">=%s" % version}]


def no_backend_signals(facts, matches):
    """What a `no_backend` answer carries, so its sentence can name the gap and not only the code: the plugin TYPES
    the collection ships (never their names — those are third-party bytes), how many modules it has, and the
    backends it does classify to, each one kontroll cannot run (registry names; none declares a role)."""
    return {"plugin_types": sorted(t for t, names in (facts.get("plugins") or {}).items() if names),
            "modules": len(facts.get("modules") or []),
            "unrunnable": list(matches)}


def _declaring_class(collection, backends):
    """The key of the class whose module file declares `collection` (catalog.module_for_collection — shipped or
    onboarded, the first that declares it) when an onboard of the collection under that key would plan, or None. A
    reuse onboard of that class adopts its backend and its role, so it is where a `no_backend` answer for the
    collection under another key points: `community.proxmox` classifies to nothing, but the proxmox class declares
    `backend: raw_ssh`. Named by the rules the reuse path holds the class to, so the hint never sends a request
    without credentials to a refusal of its own (a fault in the class's own `auth:` block is met only when a value
    is): the key is one a request may send, and the file under it is the one that declares the collection; each name
    the reuse adopts passes `_adopted`; the backend is one this install ships; and the class brings a role that is
    not a made-up one, or its backend declares one (`_role_of`). Classes whose reuse would plan but that are not
    named, so the hint fails closed, include one that names a backend this install does not ship (its reuse plans
    with its own role and no connection shape — not a fault the planner checks yet); one that names no backend but
    brings its own role, whose reuse takes the collection's first match (S26) — a backend that declares no role,
    which is why the answer is `no_backend` under another key; a later class the first declaring one shadows; and
    one whose directory and `key` disagree. A malformed module file declares nothing, the class's own included."""
    key = (catalog.module_for_collection(collection) or {}).get("key")
    if not (paths.is_component(key) and plain_scalar(key)):
        return None
    try:
        existing = _existing_module(key)
    except Exception:                                    # noqa: BLE001 — the class's own file is data, as in the scan
        return None
    try:
        if existing is None or collection not in _module_collections(existing):
            return None
        backend = existing.get("backend")
        if not (isinstance(backend, str) and backend in {b.get("name") for b in backends}):
            return None
        for field in _ADOPTED:
            _adopted(existing, field, key)
        _no_made_up_role(existing.get("role"), key, backends)
        if existing.get("role") is None:
            _role_of(next(b for b in backends if b.get("name") == backend), key, False)
    except paths.RepositoryDataError:
        return None
    return key


def no_backend_message(plan, collection, remedy):
    """The one sentence a `no_backend` plan or scaffold is answered with, on every surface — what the collection
    showed, then the surface's `remedy`. Since raw_ssh stopped catching every collection nothing else claimed
    (S24), this is what a Kubernetes, cloud or WinRM collection meets: it names the gap instead of onboarding the
    target as SSH-as-root. Every value in it is the tool's own (plugin types, a count, registry names, a class key)
    or the collection, which the request boundary has held to its charset; the audit line carries none of them."""
    sig = plan.get("signals") or {}
    if sig.get("unrunnable"):
        found = ("it classifies only to %s, which kontroll recognises but cannot run (no execution role)"
                 % ", ".join(sig["unrunnable"]))
    else:
        found = ("nothing in it is a signal a backend classifies on (plugin types: %s; %d modules)"
                 % (", ".join(sig.get("plugin_types") or []) or "none", sig.get("modules") or 0))
    if plan.get("reused_class"):
        # the class's own file is the remedy — the surface's (the Generic SSH host card under this key) would meet
        # the class's declared collection and be refused as a conflict
        return ("no backend kontroll can run fits %s: %s; the %s class's module file declares no backend — add one "
                "(`backend:` in modules/%s/module.yml; `raw_ssh` if it is an SSH host)"
                % (collection, found, plan["reused_class"], plan["reused_class"]))
    if plan.get("declaring_class"):
        return ("no backend kontroll can run fits %s on its own, but the %s class declares it with a backend — "
                "onboard it as device-class key %s" % (collection, plan["declaring_class"], plan["declaring_class"]))
    return "no backend kontroll can run fits %s: %s — %s" % (collection, found, remedy)


def _sops_ref(sops_expr, sops_key):
    """The host var that resolves ONE key of a decrypted SOPS domain: `(…).key` when the key is an identifier — what
    every existing drop-in holds, so a second onboard of the same host still reports no change — else
    `(…)['key']`. A host named `sw.lab.example` (or `192.0.2.9`) gets the key `sw.lab.example_password`, and the
    dotted form of that is an attribute CHAIN that can never resolve. The expression is built by formatting, so it
    is safe only because the key's charset is closed first (paths.hostname on the host name, an identifier for
    the field's stem — see the planner's loop): no quote, brace or bracket can reach it."""
    return ("{{ (%s).%s }}" if sops_key.isidentifier() else "{{ (%s)['%s'] }}") % (sops_expr, sops_key)


def build_onboard_plan(collection, key, group, host, host_name=None, secrets=None,
                       backend=None, username=None, password=None, api_token=None,
                       ssh_private_key=None, backends=None, vectors=None, creds=None, deep=False):
    """Compute the onboard PLAN for `collection` as device-class `key` on `host`. Returns
    {"error": "not_installed"|"no_backend"|None, ...}; on success a plan with backend/role/
    host_name, the module + drop-in host blocks (dict + rendered YAML text), the repo-rel
    paths, creds_to_set (cred-var -> SECRET value, never printed), and a read-only `telemetry`
    suggestion (the capability vector's hint — writes nothing; the observe path is what actuates it).

    Credentials: pass `creds` (a {field: value} map for the DERIVED per-backend fields — F1) OR the legacy scalar
    kwargs (`username`/`password`/`api_token`/`ssh_private_key`, folded into `creds` when `creds` is None). The plan
    returns `cred_fields` (the derived CredField DESCRIPTORS — names/kinds only, never values) so the dry-run/GUI can
    show exactly which fields a device needs; `cred_source` is "shallow" (the backend's auth: block), "fallback"
    (the generic union — a backend that declares no shape), or "deep" (F1 TIER-B — the EXACT fields from the
    connecting module's argument_spec, when `deep` and the collection is installed). Values flow through
    creds_to_set in memory only."""
    # REQUEST BOUNDARY (2026-10-08 review, finding 1). Every field that becomes a path component, an inventory key
    # or an argv word is a closed charset BEFORE anything is read, probed or written — the API and the GUI both map
    # the ValueError to 422, pre-write. The planner used to validate only `collection`'s installed-ness;
    # `key`, `group`, `host`, `host_name` and `secrets` were bare strings joined into modules/<key>/,
    # onboarded-<key>.yml and ansible/secrets/<domain>.sops.yml.
    paths.collection_fqcn(collection)
    paths.component(key, "device-class key")
    # `null`, `true`, `123`, `2026-10-12`: inside the charset, and not a string once it stands unquoted in the
    # fleet list — every reader of that list would then hold None, a bool, a number or a date for a class key.
    # The same for every other field that becomes a name (_plain_name). NOT for `host` / `host_name`: `123` and
    # `true` are legitimate host names, the inventory drop-in is serialized (quoted), and the generators that
    # render a host name by hand quote one that is not plain (kontroll.yamlscalar.word).
    _plain_name(key, "device-class key")
    paths.inventory_group(group)                 # a component that is not `all`, `ungrouped` or `localhost`
    _plain_name(group, "inventory group")
    paths.hostname(host, "host")
    if host_name is not None:
        paths.inventory_host(host_name)          # none of RESERVED_HOST_NAMES (the group's, and the two loopbacks)
    if secrets is not None:                      # None: the reused class's, else the backend's default (S27)
        paths.component(secrets, "secrets domain")
        _plain_name(secrets, "secrets domain")
    backend_from_request = backend is not None
    if backend_from_request:
        paths.component(backend, "backend")
        _plain_name(backend, "backend")
    backends = backends if backends is not None else catalog.load_backends()
    if backend_from_request:
        _registered(backend, backends)
    vectors = vectors if vectors is not None else catalog.load_vectors()
    bundled = catalog.bundled_collections(backends)

    # REUSE an existing device-class instead of regenerating a minimal module that would clobber it (#124). The
    # same class — one whose modules/<key>/module.yml already declares THIS collection — is reused as-is: we add
    # only the inventory host + fleet-enable, so its curated metrics:/backup:/logs: blocks survive (the live defect
    # was an Overwrite stripping cisco_ios's blocks). A DIFFERENT collection colliding on the key is NOT reuse — it
    # stays a WriteConflict at apply, so the operator picks a fresh key or consciously Overwrites. A collection-less
    # class (exactly `collections: []` — openwrt, a class onboarded from a search card; not an empty file, nor one
    # whose `collections` is not a list or lists entries that name no collection, nor one that declares another
    # collection, which all stay a WriteConflict) is the class a bundled collection's onboard adds a host to: it
    # declares no collection to match, so the second plain SSH host used to
    # regenerate the module and hit a WriteConflict as soon as the operator had curated it (S24).
    existing = _existing_module(key)
    declared = _module_collections(existing) if existing is not None else set()
    collection_less = (existing or {}).get("collections") == []
    module_reuse = existing is not None and (collection in declared or (collection in bundled and collection_less))
    if module_reuse:
        # Adopt the curated class's identity so the host attaches FAITHFULLY (its declared role/backend/domain/
        # group), not a freshly re-classified one that could drift from the shipped module. Each adopted name
        # replaces a request field that was checked above with a file value nothing had checked — and the domain
        # is formatted into a lookup EXPRESSION below, the role is a host var Ansible templates, the group is the
        # drop-in's top-level key. So each one the plan TAKES is held to its field's rule here (the role just
        # below, once the backend is known), long before any write (_adopted); a file that fails is not answered
        # as a refused request.
        backend = backend or _adopted(existing, "backend", key)
        secrets = _adopted(existing, "secrets_domain", key) or secrets
        group = _adopted(existing, "inventory_group", key) or group

    adopted_role = _adopted(existing, "role", key) if module_reuse else None
    _no_made_up_role(adopted_role, key, backends)
    if backend and not adopted_role:
        # A backend already known — the request's, or the one a reused class's own file names — gives the role when
        # the class brings none, so one that declares none stops the plan here, before the collection is probed: as
        # the request's `backend` field when the request named it, as the module file's fault when the file did. A
        # reused class with its own role keeps it, and plans with that backend's connection shape (brief 14 §3.2).
        _role_of(next((b for b in backends if b.get("name") == backend), {}), key, backend_from_request)
    f = _facts_for(collection, bundled)
    if f is None:
        return {"error": "not_installed"}
    if not backend:
        # The classifier's matches, in its order; the first that declares a role — the one a search record
        # suggests, so the GUI's form and the auto path agree. A match that declares none (classifier metadata
        # only) is passed over, except by a reused class with its own role when nothing else matched: that role
        # runs whatever its collection classifies to.
        matches = predicate.classify(f, backends)
        backend = next(iter(catalog.runnable(matches, backends) or (matches if adopted_role else [])), None)
        if not backend:
            return {"error": "no_backend", "signals": no_backend_signals(f, matches),
                    "reused_class": key if module_reuse else None,
                    "declaring_class": None if module_reuse else _declaring_class(collection, backends)}
    bdef = next((b for b in backends if b.get("name") == backend), {})
    role = adopted_role or _role_of(bdef, key, backend_from_request)
    secrets = _secrets_domain(secrets, bdef, backend)
    params = backend_params(bdef, collection, backend)
    host_name = host_name or (key.replace("_", "-") + "-1")

    capability_gap = []   # the honest residual surfaced for a blind onboard (non-gating); a reused class shows none
    if module_reuse:
        # The plan SHOWS the curated module (so the operator sees what they're attaching to) but apply WON'T
        # rewrite it — module_text is the file verbatim, display-only.
        module = existing
        with open(paths.module_file(key), encoding="utf-8") as fh:
            module_text = fh.read()
    else:
        module = {"key": key,
                  "description": "%s via %s backend (onboarded)" % (collection, backend),
                  "collections": _pins(collection, f, bundled),
                  "role": role, "backend": backend}
        if params:
            module["backend_params"] = params
        module["inventory_group"] = group
        module["secrets_domain"] = secrets
        # F2 (de-bespoke): AUTO-DERIVE the universal capability floor (agent-less metrics + logs) from the
        # conferred + universal methods, so a blind-onboarded class is monitored + log-shippable on day one with
        # ZERO curation — closing the empty-Grafana / empty-Loki defect. Best-effort + NON-GATING (INVARIANT D*):
        # a derivation bug degrades to no floor + a logged warning, never a failed onboard (the SAME try/except
        # posture as the telemetry nudge below). The blocks are marked `derived: true` (class + per-entry) so
        # gen-class-capabilities can keep them honest (--check); `capability_gap` names the curated richness this
        # class can't derive (e.g. a vendor exporter), surfaced like provisioning (advisory, names only).
        try:
            derived = derive_class_capabilities(f, backend, backends=backends)
        except Exception as e:                       # noqa: BLE001 — a derivation bug must never break the plan
            log.warning("capability derivation failed (%s); onboarding proceeds with no derived floor "
                        "(INVARIANT D*)", e)
            derived = {"metrics": [], "logs": [], "dashboards": [], "gap": []}
        if derived["metrics"]:
            module["metrics"] = [dict(m, derived=True) for m in derived["metrics"]]
        if derived["logs"]:
            module["logs"] = [dict(m, derived=True) for m in derived["logs"]]
        if derived["metrics"] or derived["logs"]:
            module["derived"] = True                 # class-level: gen-class-capabilities owns this class's floor
        # INVARIANT-D (Rung 4a): onboard writes the offline creds-free floor (metrics/logs) ONLY — NOT a `dashboards:`
        # block. A derived board rides the dashboards/derived locks, which are resolved OUT-OF-BAND (a network
        # gen-dashboard-floor.py --resolve, never the onboard path), so onboard must not bake in whatever lock-state
        # happens to exist. A blind-onboarded class therefore has no board until the operator re-derives its floor
        # (gen-class-capabilities recomputes the `dashboards:` block from the locks); the honest gap names it.
        capability_gap = derived.get("gap") or []
        module_text = yaml.safe_dump(module, sort_keys=False, allow_unicode=True, width=4096)

    hostvars = {"device_role": role}
    required = bdef.get("requires") or []
    for k, v in (bdef.get("connection") or {}).items():
        if isinstance(v, str) and v.strip().startswith("{{"):
            hole = v.strip().strip("{} ")
            first = _HOLE_NAME_RE.match(v.strip())
            name = first.group(1) if first else hole
            if name in required and name != hole:
                # Only a bare `{{ param }}` is filled from `params`: a required param read through a filter or an
                # expression would reach the drop-in unrendered, derived or not (T18's fold read, N5).
                raise paths.RepositoryDataError(
                    "backend registry", "the %s backend's connection block reads %s, which it requires, through an "
                    "expression the planner does not render — write it as `{{ %s }}` in its backend.yml under "
                    "ansible/backends/" % (backend, name, name))
            if name in required and hole not in params:
                # A required param the backend does not derive would reach the drop-in unrendered, and the first play
                # fails far from the cause unless a group var happens to define it. An operator's backend written
                # before S27 (a copy of netcommon_cli without `derive_params`) meets this, and so does one whose
                # `connection:` block reads a required value set at run time: that var belongs in the class's group
                # vars. The planner reads no class's own `backend_params`, a reused class's included (T18's read,
                # F12; its fold read, N5).
                raise paths.RepositoryDataError(
                    "backend registry", "the %s backend requires %s in its connection block and does not derive "
                    "it — add it to `derive_params` in its backend.yml under ansible/backends/, or, for a value set "
                    "at run time, set that connection var in the class's group vars instead" % (backend, hole))
            v = params.get(hole, v)
        hostvars[k] = v
    # S28: the TCP port the reachability probe (ping.yml) uses for a host of this backend when the host sets no
    # port of its own — the API port for `api`, where the SSH port the probe defaults to says nothing. A top-level
    # key, not a `connection:` one: it is kontroll's own var, read only by a control-node TCP probe. It is a YAML
    # integer in range or the plan is refused; a quoted or templated value in the descriptor never reaches the drop-in.
    if bdef.get("probe_port") is not None:
        probe_port = authspec.port_number(bdef["probe_port"]) if isinstance(bdef["probe_port"], int) else None
        if probe_port is None:
            raise paths.RepositoryDataError(
                "backend registry", "the %s backend's probe_port is not a TCP port (a whole number from 1 to 65535)"
                % backend)
        hostvars["kontroll_probe_port"] = probe_port
    # Self-contained creds: the host carries its OWN inline SOPS lookups (no shared group_vars needed), keyed per
    # host so onboarded hosts never collide. Values are encrypted into the domain at apply time.
    #
    # F1 (self-describing auth): the CREDENTIAL FIELDS are DERIVED from the classified backend's `auth:` block
    # (authspec.derive_auth — a network_cli switch wants SSH login; a REST device wants a token), replacing the
    # former static four-branch if-ladder with one data-driven loop. `creds` (a {field: value} map) supplies the
    # values; the legacy scalar kwargs fold into it for back-compat. Each field maps to its hostvar exactly as the
    # if-ladder did, so a password/key/token onboard is byte-identical — only the SET OF OFFERED fields is now
    # per-backend instead of a one-size-fits-all union.
    if creds is None:
        creds = {fld: val for fld, val in (("username", username), ("password", password),
                                           ("api_token", api_token), ("ssh_private_key", ssh_private_key))
                 if val is not None}
    if not isinstance(creds, dict):   # the GUI's JSON body can carry a list or a string here; the API's model cannot
        raise paths.RequestBoundaryError("creds", "creds is not a map of credential fields to values")
    # A blank value is no value (S28, MF-02): the GUI never sends one, and an API caller's "" means the same thing —
    # so "blank = 22" holds for both, and a blank never overwrites a stored secret.
    creds = {fld: val for fld, val in creds.items() if val != ""}
    # F1 seam S1: on the REUSE path a curated class's OWN `auth:` block wins over the backend's generic shape — so a
    # proxmox reuse derives its coherent api-token auth_set (field names lined up with the pve-exporter's
    # secret_env_map), not the generic single api_token box. A freshly-classified (non-reuse) class has no module
    # auth block yet → the backend shape, exactly as Rung 0a.
    # F1 TIER-B (Rung 3): when `deep`, SHARPEN the coarse per-backend shape to the EXACT credential fields read from
    # the connecting module's argument_spec (proxmox's coherent api_user+api_token_id+api_token_secret set, not a
    # generic single api_token box). Install-confined (MF-S1 — never introspects an un-installed collection) +
    # mask-ambiguous (MF-S6), and on any miss the TIER-A shape stands, so onboarding is never blocked (INVARIANT D*).
    auth = authspec.derive_auth(bdef, secrets, module=existing if module_reuse else None,
                                deep=deep, coll=collection, modules=authspec.auth_candidate_modules(f))
    # Back-compat safety net: a legacy field the caller passed that this backend's `auth:` block does NOT declare is
    # appended from the generic union, so an existing caller's field is mapped EXACTLY as before, never silently
    # dropped by the new per-backend derivation.
    declared = {cf["field"] for cf in auth["fields"]}
    for fld in creds:
        if fld not in declared:
            extra = authspec.union_field(fld)
            if extra is not None:
                extra["domain"] = secrets
                auth["fields"].append(extra)
    # The backend's TEMPLATED `maps_to` entries are the default, by field name, for a field that brings none (S27): a
    # TIER-B field read from a module's argument spec never sets one, and an `api_token` among them got its host var
    # from the branch on the field's name this replaces. The same rule, stated as the registry's data.
    templated = {e["field"]: e["maps_to"] for e in (bdef.get("auth") if isinstance(bdef.get("auth"), list) else [])
                 if isinstance(e, dict) and isinstance(e.get("field"), str)
                 and isinstance(e.get("maps_to"), str) and "{" in e["maps_to"]}
    for cf in auth["fields"]:
        if cf.get("maps_to") is None and cf["field"] in templated:
            cf["maps_to"] = templated[cf["field"]]

    # MF-S2 domain confinement (the one NEW security guard) — a PRE-PASS before any creds_to_set write: every SECRET
    # field that has a value must target the onboard's own SOPS domain. Refuse (loud, catchable) BEFORE the mapping
    # loop, so a malformed/adversarial descriptor can't smuggle a secret into a domain the operator didn't choose and
    # no half-built creds dict can persist. Today's hardcoded if-ladder couldn't cross domains; a data-driven loop
    # could, so this guard makes the no-leak constraint enforceable in code, not by convention.
    for cf in auth["fields"]:
        if cf.get("secret") and creds.get(cf["field"]) is not None and cf.get("domain", secrets) != secrets:
            raise ValueError("cred field %s derived for domain %s but onboard targets %s — refusing to cross domains"
                             % (cf["field"], cf.get("domain"), secrets))

    # auth_set COHERENCE (F1 seam S1): a multi-field credential is ALL-or-NONE — you can't half-authenticate (a
    # proxmox API token needs user@realm + token_id + token_secret TOGETHER, else the exporter has a broken cred and
    # Grafana silently stays empty). Refuse a PARTIALLY-filled required set (loud + catchable, like the domain guard
    # — the operator completes it), but an entirely-empty set is fine: a values-free dry-run plan, or an optional
    # set, never gates (INVARIANT D*). Keyed on the REQUIRED members so an optional extra doesn't force the set.
    sets = {}
    for cf in auth["fields"]:
        if cf.get("auth_set") and cf.get("required"):
            sets.setdefault(cf["auth_set"], []).append(cf["field"])
    for set_name, members in sets.items():
        filled = [m for m in members if creds.get(m) not in (None, "")]
        if filled and len(filled) != len(members):
            missing = [m for m in members if creds.get(m) in (None, "")]
            raise ValueError("incomplete credential set %r — provide all of %s together (missing %s)"
                             % (set_name, members, missing))

    cred_key = host_name.replace("-", "_")
    _sops = "lookup('community.sops.sops', inventory_dir ~ '/../secrets/%s.sops.yml') | from_yaml" % secrets
    creds_to_set = {}
    for cf in auth["fields"]:
        val = creds.get(cf["field"])
        if val is None:
            continue
        if not cf.get("inline") and not isinstance(val, str):
            # Only an inline field takes a typed value (authspec.inline_value refuses the rest of it). Anywhere else a
            # list, a number or an object from the GUI's untyped JSON went into SOPS as itself, or a 500 (an SSH key's
            # `.replace`). Refused as `creds`, never as the request's own key, which would reach the audit line.
            raise paths.RequestBoundaryError("creds", "a credential value is not text")
        # A `shared` cred is a DOMAIN-level secret (one per SOPS domain, e.g. the read-only proxmox token the
        # pve-exporter uses for every node) → stored FLAT under its sops_stem so its consumer reads the exact name
        # (telemetry/pve.yml's secret_env_map {proxmox_api_user}…). A normal cred stays HOST-keyed so two onboarded
        # hosts never collide. Re-onboarding another node of the same domain re-sets the same shared key (idempotent).
        # The stem is the name the backend registry or the class's module file declares (a TIER-B-derived field's
        # stem is an option name `authspec` has already held to this rule). It is formatted into a lookup
        # EXPRESSION below, so it is an identifier or the onboard is refused (SECURITY.md C22); the name is not
        # echoed.
        # `maps_to` is the NAME of the host var the field is written under. It is a key of the inventory drop-in,
        # so it is an identifier too: no brace, no space, no line break, no value that is not a string — or a
        # template the backend registry or the generic union declares for that field, whose rendering with this
        # host's credential key stays in catalog.MAPS_TO_RE (below). WHICH
        # variable a SOPS-stored field names is the declaring file's to choose — a module's own `auth:` entry may
        # name `ansible_connection` as the registry names `ansible_user`; what lands there is a lookup, never the
        # request's text — and is not restricted here (SECURITY.md C22, limits). An inline field's variable is
        # narrower: authspec.inline_target, below. Neither name is anything the caller sent: the backend registry
        # or the class's module file declared it (an option name out of an installed module that is not an
        # identifier is not offered at all — authspec), so the refusal is the repository-data one and says where
        # to look.
        _where = ("declared for the %s device class (its module file's `auth:` block, or the %s backend's "
                  "registry entry)" % (key, backend))
        stem = cf["sops_stem"]
        if not isinstance(stem, str) or not _STEM_RE.fullmatch(stem):
            raise paths.RepositoryDataError(
                "credential field", "a credential field %s has a name that is not an identifier" % _where)
        # A `maps_to` holding a placeholder is a TEMPLATE (S27 — the api backend's `{cred_key}_api_token`): rendered
        # with this host's credential key and held to catalog.MAPS_TO_RE, the charset of every host name the request
        # boundary accepts, so an upper-case, dotted or IP-literal host keeps the var it had. The host name cannot
        # make the render fail; the template's own text can, so a failure is the declaring file's. Only the backend
        # registry and the generic union declare one: a template lets the request's host name choose WHICH var the
        # lookup lands in, so a module file's `auth:` entry — curated, but not the registry — names its var as
        # written. It may repeat, for the same field, the template its backend's registry entry or the generic union
        # declares (today only `api_token`'s `{cred_key}_api_token`); any other template is refused once a value is
        # given (T18's read, F1; its fold read, N2).
        maps_to = cf.get("maps_to")
        if isinstance(maps_to, str) and "{" in maps_to:
            if maps_to not in (templated.get(cf["field"]), (authspec.union_field(cf["field"]) or {}).get("maps_to")):
                raise paths.RepositoryDataError(
                    "credential field", "a credential field %s maps to a template its backend does not declare "
                    "for that field — name the variable" % _where)
            try:
                maps_to = catalog.render_template(maps_to, {"cred_key": cred_key}, catalog.MAPS_TO_RE)
            except ValueError:
                raise paths.RepositoryDataError(
                    "credential field", "a credential field %s maps to a template that does not render to a "
                    "variable name" % _where) from None
        elif maps_to is not None and not (isinstance(maps_to, str) and _STEM_RE.fullmatch(maps_to)):
            raise paths.RepositoryDataError(
                "credential field", "a credential field %s maps to something that is not a variable name" % _where)
        sops_key = stem if cf.get("shared") else "%s_%s" % (cred_key, stem)
        if cf["kind"] == "ssh_key":
            # SSH KEY AUTH — the unified root seam. The PEM is SOPS-encrypted per host; the hostvar points at a 0600
            # file `_render-ssh-keys.yml` materializes from SOPS into the run-keyed kontroll_device_key_dir.
            # kontroll_ssh_key_domain (NON-secret) tells that render play which domain to decrypt — emitted ONLY on
            # key hosts, so password/token drop-ins stay byte-identical. `\r\n`->`\n` so a Windows paste round-trips
            # to paramiko (MF-7); ensure a trailing newline (a paste may strip it -> OpenSSH rejects a short key).
            val = val.replace("\r\n", "\n")
            if not val.endswith("\n"):
                val += "\n"
            creds_to_set[sops_key] = val
            # A backend/curated ssh_key field maps to a connection hostvar (ansible_ssh_private_key_file); a
            # TIER-B-DERIVED ssh_key param (e.g. a module's `client_key`) has NO maps_to → SOPS-store it, but skip
            # the hostvar wiring (a None-keyed hostvar would be bogus — review-30 F-3). Its consumer reads the SOPS
            # value by name; only a maps_to field materializes the 0600 key file + declares the decrypt domain.
            if maps_to:
                hostvars[maps_to] = "{{ kontroll_device_key_dir }}/%s.key" % cred_key
                hostvars["kontroll_ssh_key_domain"] = secrets
        elif cf.get("inline"):
            # A non-secret literal host var (a port, a flag) — not SOPS, so the VALUE itself lands in the inventory,
            # where Ansible templates it (S28, SECURITY.md C22). authspec types it first, so what is written is an int
            # or a bool and never the request's text. The descriptor's half — which kinds may be inline, and that a
            # port lands in a `*_port` var — is repository data, refused as such before any write.
            target = authspec.inline_target(cf)
            if target is None:
                raise paths.RepositoryDataError(
                    "credential field", "an inline credential field %s is not a port or a flag written to a var "
                    "of its kind" % _where)
            hostvars[target] = authspec.inline_value(cf, val)
        else:
            # SOPS-store the value, and wire a hostvar inline-lookup ONLY if the field maps to one. A shared
            # domain cred (proxmox exporter token) has NO maps_to — it is read from the SOPS domain by its consumer
            # (deploy-stack renders it into the exporter .env via the class's secret_env_map), not via a hostvar.
            # An API token is one of these since S27: its var is the api backend's `{cred_key}_api_token` template,
            # rendered above. roles/backend_api reads that name, or the recipe's own `token_var` when one is in scope.
            creds_to_set[sops_key] = val
            if maps_to:
                hostvars[maps_to] = _sops_ref(_sops, sops_key)
    host_block = {group: {"hosts": {host_name: {"ansible_host": host, **hostvars}}}}
    host_text = yaml.safe_dump(host_block, sort_keys=False, allow_unicode=True, width=4096)

    # The telemetry suggestion is a NON-binding nudge — and INVARIANT D* requires that no secondary
    # capability (its suggester included) can gate onboarding. So it is BEST-EFFORT: a suggester bug
    # degrades to "no hint", never a failed onboard. The plan above is already complete without it.
    try:
        telemetry = suggest_telemetry(f, vectors)
    except Exception as e:                                # noqa: BLE001 — a hint must never break the plan
        log.warning("telemetry suggestion failed (%s); onboarding proceeds without the hint (INVARIANT D*)", e)
        telemetry = None

    # The provisioning SURFACE — the credential prerequisites THIS device class declares as shipped data (e.g.
    # "the API token must carry the PVEAuditor role"). The blind-joe constraint: any manual step beyond IP+creds
    # is surfaced AT onboarding, never buried in a doc. Like the telemetry nudge it is BEST-EFFORT + NON-GATING
    # (INVARIANT D*): a reader bug degrades to [] (the plan is already complete), and the block is NEVER written
    # into the onboarded module (it's not in module{} above → the _ONBOARD_KEYS pin stays green). Public role
    # NAMES only — no credential. Resolved by COLLECTION (the form key is a slug; module_provisioning scans).
    try:
        provisioning = catalog.module_provisioning(collection)
    except Exception as e:                                # noqa: BLE001 — surfacing must never break the plan
        log.warning("provisioning surface read failed (%s); onboarding proceeds without it (INVARIANT D*)", e)
        provisioning = []

    return {"error": None, "collection": collection, "key": key, "group": group,
            "secrets": secrets, "backend": backend, "role": role, "host_name": host_name,
            "module_reuse": module_reuse,   # True => apply reuses the curated module (#124): add only the host
            "module": module, "module_text": module_text,
            "host_block": host_block, "host_text": host_text, "creds_to_set": creds_to_set,
            # F1: the DERIVED credential-field descriptors (names/kinds/labels only — NEVER values), so the
            # dry-run/GUI can show exactly which fields THIS device needs; cred_source = shallow (backend auth:
            # block) | fallback (generic union). Pure schema, like secret-forms — no value-bearing field added.
            "cred_fields": auth["fields"], "cred_source": auth["source"],
            "mod_path": os.path.join("modules", key, "module.yml"),
            # New drop-in host file: overlay_target so it lands in the ACTIVE instance/ inventory (the dir
            # Semaphore reads) and the same string is what gets git-added — write target == staged path.
            "inv_path": paths.overlay_target("ansible/inventory/onboarded-%s.yml" % key),
            "telemetry": telemetry,   # read-only suggestion (or None on failure); the GUI/operator decides
            # F2: the HONEST capability gap — curated rungs (a vendor exporter, a credentialed log pull) this blind
            # class could NOT auto-derive but a peer has; surfaced advisory + NON-GATING (INVARIANT D*), names only.
            # Empty for a full-parity class (cisco_ios) and for a reused curated class.
            "capability_gap": capability_gap,
            "provisioning": provisioning}   # advisory credential-prerequisite surface (or []); names only, non-gating


def apply_onboard_plan(plan, overwrite=False):
    """Perform the onboard repo mutations for a computed plan: write the device-class module (UNLESS the class
    already exists for this collection — then REUSE it, adding only the host, so its curated metrics:/backup:/
    logs: blocks survive, #124), MERGE the drop-in inventory host alongside any siblings (a 2nd device of a class
    never drops the 1st), enable the class in instance/fleet.yml, and encrypt any creds into the SOPS domain.
    Prints progress and returns {"changed": bool, "paths": [repo-rel paths to git-add]}. A divergent existing
    module / a foreign inventory file raises gitio.WriteConflict unless `overwrite=True` (the GUI 'Overwrite'
    button / API `overwrite` flag — the deliberate, loudly-warned escape hatch)."""
    # The drop-in targets this apply will WRITE, before the first write: a layout the confinement refuses must not
    # leave half an onboard behind. A reuse writes no module file, so it does not ask about one.
    for rel in ((plan["inv_path"],) if plan.get("module_reuse") else (plan["mod_path"], plan["inv_path"])):
        paths.confined(rel)
    if plan.get("module_reuse"):
        print("  = reusing existing device-class %s (curated module kept; adding host only)" % plan["mod_path"])
        changed = False
    else:
        # The READER's view (paths.module_file): on an install whose write root is not its code root a shipped
        # class is not at the write target, and a new module written there would shadow it for every reader
        # (#124). A one-root tree answers this through _write_new's own comparison; this makes the split layout
        # give the same answer, before the first write. `overwrite` stays the deliberate way to shadow.
        shipped = paths.module_file(plan["key"])
        if (not overwrite and os.path.exists(shipped)
                and os.path.realpath(shipped) != os.path.realpath(paths.confined(plan["mod_path"]))):
            raise gitio.WriteConflict(
                "%s already exists with different content — pick a new device-class key, "
                "or use Overwrite to replace it" % plan["mod_path"])
        changed = gitio._write_new(plan["mod_path"], plan["module_text"], _MODULE_BANNER, overwrite=overwrite)
    group, gbody = next(iter(plan["host_block"].items()))
    changed |= gitio.write_inventory_host(plan["inv_path"], group, gbody.get("hosts") or {},
                                          _INV_BANNER, overwrite=overwrite)
    changed |= enable_in_fleet(plan["key"])
    paths_changed = [plan["mod_path"], plan["inv_path"], paths.overlay_rel("config/fleet.yml")]
    creds_to_set = plan["creds_to_set"]
    if creds_to_set:
        # Whole-domain decrypt -> merge -> stdin re-encrypt (gitio.sops_write_domain). The plaintext (incl. a
        # multi-line SSH PRIVATE key) transits sops' STDIN only, NEVER argv — `sops --set` would put it on a
        # ps-/proc-visible command line (MF-3). Merge {existing, new} so a second host's onboard never drops the
        # first host's fields (sops_write_domain re-encrypts the WHOLE domain).
        print("--- encrypting credentials into the %s SOPS domain (stdin; never argv) ---" % plan["secrets"])
        existing = gitio.sops_decrypt_domain(plan["secrets"])      # {} for a new domain; None on a decrypt failure
        if existing is None:
            raise RuntimeError("cannot decrypt the '%s' SOPS domain to merge creds (no/invalid age key here)"
                               % plan["secrets"])
        if gitio.sops_write_domain(plan["secrets"], {**existing, **creds_to_set}):
            changed = True
        paths_changed.append(paths.overlay_rel("ansible/secrets/%s.sops.yml" % plan["secrets"]))
    return {"changed": changed, "paths": paths_changed}


def enable_in_fleet(key):
    """Insert `  - <key>` into instance/fleet.yml's enabled_modules, preserving the
    file's comments. Idempotent: a no-op if the key is already active."""
    paths.component(key, "device-class key")              # the join: this line is hand-formatted into the list
    _plain_name(key, "device-class key")
    path = paths.resolve("config/fleet.yml", write=True)   # a WRITER: never the shipped instance.example/ stub
    lines = open(path, encoding="utf-8").read().splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "enabled_modules:"), None)
    if start is None:
        # RAISE (a catchable ValueError), never sys.exit — a SystemExit (BaseException) is NOT caught by
        # Flask/Werkzeug, so it would kill the in-process GUI/API request worker mid-request (the
        # service-degrades principle; see gitio.WriteConflict). The route's try/except returns a clean 5xx; the
        # CLI surfaces it as an error trace. (S-3/G9 fix, Phase 5.)
        raise ValueError("instance/fleet.yml: no enabled_modules: key")
    last_active = None
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith((" ", "\t")):  # dedented -> block ended
            break
        item = ln.strip()
        if item.startswith("- ") and item[2:].split("#")[0].strip() == key:
            print("  = %s already enabled in instance/fleet.yml" % key)
            return False
        if item.startswith("- "):                          # active (non-comment) entry
            last_active = i
    insert_at = (last_active + 1) if last_active is not None else (start + 1)
    lines.insert(insert_at, "  - %-13s # onboarded via scripts/galaxy.py" % key)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("  + enabled %s in instance/fleet.yml" % key)
    return True

