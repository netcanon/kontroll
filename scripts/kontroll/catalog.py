"""Content registries + the two "what's available" sources.

Drop-in loaders — load_vectors (vectors/*.yml), load_overrides (overrides/*.yml),
load_backends (ansible/backends/*/backend.yml) — and the sources: local_installed
(`ansible-galaxy collection list`) and galaxy_search (galaxy.ansible.com). The loaders
read the REAL tree via paths.* (import-bound, so a repointed paths.ROOT never diverts
them). local_installed + galaxy_search are the I/O SEAM the tests monkeypatch
(kontroll.catalog.local_installed / kontroll.catalog.galaxy_search). Lifted from galaxy.py.
"""
import json
import logging
import os
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import yaml

from kontroll import paths, probe

log = logging.getLogger("kontroll.catalog")
_GALAXY_TIMEOUT = 8   # s; a slow/unreachable galaxy.ansible.com must degrade to local-only, not hang /search


def load_vectors():
    vecs = []
    for fn in sorted(os.listdir(paths.VECTORS_DIR)):
        if fn.endswith((".yml", ".yaml")):
            with open(os.path.join(paths.VECTORS_DIR, fn), encoding="utf-8") as fh:
                vecs.append(yaml.safe_load(fh))
    return sorted(vecs, key=lambda v: v.get("order", 99))


def load_telemetry():
    """telemetry/<method>.yml — the drop-in telemetry-method registry gen-observability.py dispatches over.
    Each descriptor declares HOW a class is scraped: kind host-agent (the host runs an exporter -> target
    host:port) or proxy-exporter (a control-node exporter queries the device -> target the device address).
    node_exporter is ONE descriptor (host_node), not a hardcoded branch. Sorted by `order` (host-agents low,
    proxy-exporters high), mirroring load_vectors()/load_backends(). README.md and other non-YAML are ignored."""
    methods = []
    if os.path.isdir(paths.TELEMETRY_DIR):
        for fn in sorted(os.listdir(paths.TELEMETRY_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(paths.TELEMETRY_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                    if doc:
                        methods.append(doc)
    return sorted(methods, key=lambda m: m.get("order", 99))


def load_dashboard_locks():
    """dashboards/derived/<method>.lock.yml -> {method: lock_dict} — the DERIVED dashboard-floor pins (north-star
    Rung 4a, written by gen-dashboard-floor.py --resolve). The READ side of the dashboard floor: a `derived: true`
    class's `dashboards:` block is emitted from these locks (classify._derive_dashboards — the id/name a derived
    telemetry method's board resolved to). PURE read (paths.DASHBOARD_LOCKS_DIR is import-bound like the other `*_DIR`
    registries — it reads the real shipped locks even under a repointed ROOT); mirrors load_telemetry's sorted-glob.
    An absent/empty dir -> {} (a class with no resolved lock derives NO
    board — the honest empty outcome, never a faked id). README.md and other non-`.lock.yml` files are ignored."""
    out = {}
    if os.path.isdir(paths.DASHBOARD_LOCKS_DIR):
        for fn in sorted(os.listdir(paths.DASHBOARD_LOCKS_DIR)):
            if fn.endswith(".lock.yml"):
                with open(os.path.join(paths.DASHBOARD_LOCKS_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                if doc and doc.get("method"):
                    out[doc["method"]] = doc
    return out


def load_logging():
    """logging/<method>.yml — the drop-in log-method registry gen-logging.py dispatches over. Each descriptor
    declares HOW a class's logs reach Loki: a Vector SOURCE (journald/file/docker_logs/syslog/http_*/exec) +
    the device-side WIRING role that configures the device to export (push methods) or null (pull methods).
    Sorted by `order`, mirroring load_telemetry()/load_capabilities() exactly — adding a log method is one
    drop-in file, never a loader edit. README.md and other non-YAML are ignored."""
    methods = []
    if os.path.isdir(paths.LOGGING_DIR):
        for fn in sorted(os.listdir(paths.LOGGING_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(paths.LOGGING_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                    if doc:
                        methods.append(doc)
    return sorted(methods, key=lambda m: m.get("order", 99))


def load_discovery():
    """discovery/<method>.yml — the drop-in PASSIVE-DISCOVERY method registry the top-level sweep actor
    (scripts/kontroll-discover.py) dispatches over (the 4th sorted-glob loader, mirroring load_telemetry/
    load_logging exactly). Each descriptor declares HOW an already-onboarded class's OWN lease/neighbour view is
    read read-only (a GET endpoint + auth + a `parse.shape` NAME the sweep's `_PARSERS` table keys on) so the
    un-onboarded delta can be surfaced into the onboard inbox — NEVER an active scan. A class OPTS IN by declaring
    `discovery: [{method: <name>}]` in its module.yml (declared, not facts-derived; opnsense ships no httpapi
    plugin). Sorted by `order`; README.md and other non-YAML are ignored. Schema: discovery/README.md; SECURITY C18."""
    methods = []
    if os.path.isdir(paths.DISCOVERY_DIR):
        for fn in sorted(os.listdir(paths.DISCOVERY_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(paths.DISCOVERY_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                    if doc:
                        methods.append(doc)
    return sorted(methods, key=lambda m: m.get("order", 99))


def discovery_method(name):
    """The one discovery descriptor whose `name` == `name` (or None) — the sweep's get-descriptor analogue."""
    return next((m for m in load_discovery() if m.get("name") == name), None)


def registered_discovery_methods():
    """The registry KEYS — the sorted `name` of every discovery/<method>.yml. The neutral list a sweep/test loop
    iterates, so adding a method grows a list a test reads rather than editing a test body."""
    return [m["name"] for m in load_discovery()]


def load_capabilities():
    """capabilities/<cap>.yml — the drop-in SECONDARY-CAPABILITY registry the generalized dialog seam
    dispatches over (the 5th sorted-glob loader, mirroring load_vectors/load_telemetry/load_overrides/
    load_backends). Each descriptor parameterizes the ONE shared dialog/route/promote spine for a
    capability (telemetry, backup, …) with ZERO `if cap==…` branching: it names the capability's vector,
    its suggester (resolved by import-by-convention, NOT a god-map), the module.yml block it declares, its
    generator + enact builder, and its trust note. Adding a capability is THIS file + vectors/<cap>.yml +
    gen-<cap>.py + a suggest_<cap>/declared_<cap> pair — never an edit to the shell. README.md and other
    non-YAML are ignored; sorted by `order`. Schema: docs/observability/secondary-capability-dialog.md §2."""
    caps = []
    if os.path.isdir(paths.CAPABILITIES_DIR):
        for fn in sorted(os.listdir(paths.CAPABILITIES_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(paths.CAPABILITIES_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                    if doc:
                        caps.append(doc)
    return sorted(caps, key=lambda c: c.get("order", 99))


def registered_capabilities():
    """The registry KEYS — the sorted `name` of every dropped-in capabilities/<cap>.yml. This is the
    capability-neutral list the route loop, the privileged tripwire, and the INVARIANT D* parametrized
    pins iterate (so adding a capability grows the list a test reads, never edits a test body)."""
    return [c["name"] for c in load_capabilities()]


def load_actuation_units():
    """actuation/<key>/unit.yml — the drop-in APP-STORE UNIT registry (R2), the 3rd instance of the registry idiom
    (modules = device classes; capabilities/<cap>.yml = post-onboard capabilities; actuation = runnable units). A
    directory-per-unit descriptor names one chosen, version-pinned runnable unit (collection playbook / role /
    in-repo standalone) + its install pin (which DERIVES into requirements.generated.yml via gen-requirements.py) +
    its target device class. Resolved through the INSTANCE OVERLAY (`paths.resolve`) so the operator's chosen units
    live in the private `instance/actuation/` overlay (stripped from the public tree), never the shipped tree —
    `gen-actuation.py` is the fail-closed validator, so a malformed descriptor is skipped HERE (a typo in one unit
    can't blank another). README.md and other non-`<key>/unit.yml` paths are ignored; sorted by key. Schema:
    actuation/README.md."""
    units = []
    adir = paths.resolve("actuation")
    if os.path.isdir(adir):
        for key in sorted(os.listdir(adir)):
            up = os.path.join(adir, key, "unit.yml")
            if not os.path.isfile(up):
                continue
            try:
                with open(up, encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh) or {}
            except (OSError, yaml.YAMLError):
                continue
            if doc.get("key"):
                units.append(doc)
    return units


def actuation_unit(key):
    """The one actuation descriptor whose `key` == `key` (or None) — the dialog's get-descriptor analogue."""
    return next((u for u in load_actuation_units() if u.get("key") == key), None)


def registered_actuation_units():
    """The registry KEYS — the sorted `key` of every actuation/<key>/unit.yml. The neutral list a route loop +
    tests iterate, so adding a unit grows a list a test reads rather than editing a test body."""
    return [u["key"] for u in load_actuation_units()]


def load_secret_forms():
    """secret-forms/<domain>.yml — the drop-in SECRET-ONBOARDING form registry the shared secret dialog
    dispatches over (the secret analogue of load_capabilities). Each descriptor declares a SOPS domain's form
    FIELDS (key/label/type/required/generate) — NEVER values; recipiency stays in /.sops.yaml. Sorted by
    `order`; README.md and other non-YAML ignored. Schema: secret-forms/README.md."""
    forms = []
    if os.path.isdir(paths.SECRET_FORMS_DIR):
        for fn in sorted(os.listdir(paths.SECRET_FORMS_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(paths.SECRET_FORMS_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                    if doc:
                        forms.append(doc)
    return sorted(forms, key=lambda f: f.get("order", 99))


def secret_form(domain):
    """The one secret-onboarding descriptor for `domain` (or None) — the dialog's get-descriptor analogue."""
    return next((f for f in load_secret_forms() if f.get("domain") == domain), None)


def registered_secret_forms():
    """The registry KEYS — the sorted `domain` of every secret-forms/<domain>.yml. The neutral list the route
    loop + tests iterate, so adding a domain grows a list a test reads rather than editing a test body."""
    return [f["domain"] for f in load_secret_forms()]


def load_key_roles():
    """key-roles/<role>.yml — the drop-in KEYGEN role registry the shared keygen dialog dispatches over (the
    key analogue of load_secret_forms). Each descriptor declares one age-key ROLE (break-glass / scoped
    Semaphore / control rotation) — placement of the show-once private key + which /.sops.yaml recipient
    group(s) the public key joins — NEVER any key material. Sorted by `order`; README.md and other non-YAML
    ignored. Schema: key-roles/README.md."""
    roles = []
    if os.path.isdir(paths.KEY_ROLES_DIR):
        for fn in sorted(os.listdir(paths.KEY_ROLES_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(paths.KEY_ROLES_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                    if doc:
                        roles.append(doc)
    return sorted(roles, key=lambda r: r.get("order", 99))


def key_role(role):
    """The one keygen descriptor for `role` (or None) — the dialog's get-descriptor analogue."""
    return next((r for r in load_key_roles() if r.get("role") == role), None)


def registered_key_roles():
    """The registry KEYS — the sorted `role` of every key-roles/<role>.yml. The neutral list the route loop +
    tests iterate, so adding a role grows a list a test reads rather than editing a test body."""
    return [r["role"] for r in load_key_roles()]


def load_knob_descriptors():
    """settings/<area>.yml — the drop-in RECONFIGURE-KNOB METADATA registry (design 22 §10). Each area file is a
    mapping `{area, file, knobs: [{key, type, severity, blast_radius, reconfigurable, confirm_text, help, ...}]}`
    describing the per-knob reconfigure metadata for a config surface (module identity, platform settings, …). It
    carries NO values — only the severity/blast-radius classification the differ + the renderer key off, so the
    severity gate is DATA not code (the no-bespoke-config tenet). Sorted by filename; README.md and non-YAML are
    ignored. Mirrors load_capabilities/load_secret_forms exactly — adding a surface is one drop-in file."""
    areas = []
    if os.path.isdir(paths.SETTINGS_DIR):
        for fn in sorted(os.listdir(paths.SETTINGS_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(paths.SETTINGS_DIR, fn), encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
                    if doc and doc.get("area"):
                        areas.append(doc)
    return areas


def knob_meta(area):
    """`{knob_key: {"severity", "blast_radius"}}` for one settings AREA — the lookup table
    service/_diff.compute_changes consults to UPGRADE a `modify` of that knob to its declared severity
    (identity/redeploy). {} if the area is unregistered. The capability-neutral feed that lets a flat scalar
    surface (module identity, platform settings) drive the same severity-confirm the capability params do."""
    doc = next((a for a in load_knob_descriptors() if a.get("area") == area), None)
    if not doc:
        return {}
    return {k["key"]: {"severity": k.get("severity"), "blast_radius": k.get("blast_radius")}
            for k in (doc.get("knobs") or []) if k.get("key")}


def module_for_collection(collection):
    """The SHIPPED device-class module (`modules/<key>/module.yml`, parsed) that declares `collection` in its
    `collections[].name`, or None. The collection→class resolver behind the app-store CREATE seam
    (`service/actuation.build_create_unit_plan`): a searched collection (e.g. `cisco.ios`) must map to the
    device-class (`cisco_ios`) + its `inventory_group` (`core_switch`) the new unit will target — the ONE
    dispatch seam, read from shipped data, never re-derived. First match wins (one class per collection).

    Resolved by SCANNING modules/*/module.yml + matching the collection NAME (not a path-join by key — the module key
    `proxmox` ≠ its collection `community.proxmox`). README.md and other non-YAML are ignored; a malformed module.yml is
    skipped (a typo in one class can't blank another) — whatever its parse raises: a file that does not parse, is not
    UTF-8, holds a date no calendar has, nests past the recursion limit, or tags a scalar YAML cannot build
    (`!!bool maybe`). The shared scan `module_provisioning` reuses. Scans BOTH module roots via
    paths.module_keys()/module_file() (Phase-B Fork B): pristine SHIPPED classes baked at ROOT + operator-onboarded
    ones in the write_root clone, so a newly-onboarded class resolves post-promote."""
    for key in paths.module_keys():
        mp = paths.module_file(key)
        if not os.path.exists(mp):
            continue
        try:
            with open(mp, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except Exception:                          # noqa: BLE001 — a module file is data: a bad one declares nothing
            continue
        # a file that parses to a list or a scalar, or whose `collections` is not a list, declares nothing here —
        # skipped like a file that does not parse (a no_backend answer reads this scan for EVERY refusal, S24)
        colls = doc.get("collections") if isinstance(doc, dict) else None
        names = [c.get("name") for c in colls if isinstance(c, dict)] if isinstance(colls, list) else []
        if collection in names:
            return doc
    return None


def module_provisioning(collection):
    """The human credential-provisioning prerequisites the SHIPPED device-class module for `collection`
    declares — a list of {grant, note, docs_url?} records (or []). The READ side of the no-bespoke onboarding
    UX (the blind-joe constraint): a class names the token scope/role a blind operator must grant ONCE, as
    shipped data, so the onboard plan can SURFACE it instead of burying it in a doc/source comment. The SAME
    getter the validate seam's (deferred) A' role-map check reads `grant` from, so "one declaration, two
    consumers" (surface=prevent, validate=detect) is DRY in CODE, not by convention — hence homed here in
    catalog (next to the other registry readers), not onboard-private.

    Resolved via `module_for_collection` (the shared collections[].name scan — NOT a path-join by key). First
    match wins (one class per collection); a class with no `provisioning:` block (the universal case) yields []
    — purely additive, no per-class registration."""
    doc = module_for_collection(collection)
    return (doc.get("provisioning") or []) if doc else []


def load_overrides():
    """overrides/<*>.yml -> {collection: {capabilities:{...}, meta_note:str}}."""
    out = {}
    if not os.path.isdir(paths.OVERRIDES_DIR):
        return out
    for fn in sorted(os.listdir(paths.OVERRIDES_DIR)):
        if fn.endswith((".yml", ".yaml")):
            with open(os.path.join(paths.OVERRIDES_DIR, fn), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
                if doc.get("collection"):
                    out[doc["collection"]] = doc
    return out


def load_backends():
    """ansible/backends/<name>/backend.yml — drop-in execution backends."""
    bes = []
    if os.path.isdir(paths.BACKENDS_DIR):
        for d in sorted(os.listdir(paths.BACKENDS_DIR)):
            bp = os.path.join(paths.BACKENDS_DIR, d, "backend.yml")
            if os.path.exists(bp):
                with open(bp, encoding="utf-8") as fh:
                    bes.append(yaml.safe_load(fh))
    return sorted(bes, key=lambda b: b.get("order", 99))


def backend_role(bdef):
    """The execution role a backend registry entry DECLARES (its `role:`), or None where it declares none (absent,
    null or empty). A backend with none is classifier metadata only: no play can run a check or a backup through it,
    so the onboard planner never chooses one and refuses a request that names one — it never makes a role name up
    (S26). A declared role that is not a name is returned as it is: the planner answers that as repository data."""
    role = (bdef or {}).get("role")
    return None if role is None or role == "" else role


def made_up_roles(backends=None):
    """The role names the onboard planner and the scaffold made up, before S26, for a shipped backend whose registry
    entry declares no role — `backend_<name>` for each (`backend_vendor_config`, `backend_napalm`). No play can
    include one: a reused class that names one is refused (onboard._no_made_up_role), and the identity knob does not
    store one. A list (the registry is data, its names need not hash); empty the day every backend declares a role.
    PURE."""
    backends = backends if backends is not None else load_backends()
    return ["backend_%s" % b.get("name") for b in backends if backend_role(b) is None]


# A backend descriptor's TEMPLATES (S27): a `derive_params` value and a templated `maps_to`. The grammar is closed and
# is not Jinja: `{name}` placeholders filled from a fixed context, and nothing else — no attribute access (what
# `str.format` would give `{collection.__class__}`), no expression, no filter.
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
# What a rendered value may hold. A derived param is written into the module file and, through `connection:`, into a
# host var Ansible templates (`ansible_network_os`): the collection's own charset and the dot that joins its names. A
# templated `maps_to` names a host var after the host's credential key — the inventory host name with `-` as `_` —
# so the charset of every host name the request boundary accepts: letters of either case, digits, `_`, and the `.`
# and `:` of a dotted name or an IP literal. Never a brace, a space or a quote; the var is a key of the drop-in.
# On a clock: ansible-core 2.19 warns on every inventory load that a var name with a `.` or a `:` is accepted until
# 2.23 (measured, T18's read F2). The former code wrote the same names, so this charset keeps them; before the stack
# reaches 2.23 the var name must swap them for `_`, here and in backend_api's lookup, with a migration for the
# api drop-ins that hold one.
PARAM_RE = re.compile(r"[a-z0-9_.]{1,256}\Z")
MAPS_TO_RE = re.compile(r"[A-Za-z0-9_.:]{1,320}\Z")


def render_template(tmpl, ctx, out_re):
    """`tmpl` with each `{name}` placeholder replaced from `ctx` (a {name: text} map), held to `out_re`. A template
    that is not text, a placeholder `ctx` does not fill, a brace left once the placeholders are gone, or a result
    `out_re` does not match raises ValueError: a descriptor typo is a refused plan, never a written var. PURE."""
    if not isinstance(tmpl, str):
        raise ValueError("a template is not text")
    rest = _PLACEHOLDER.sub("", tmpl)
    if "{" in rest or "}" in rest:
        raise ValueError("a template holds a brace that is not a placeholder")
    if any(not isinstance(ctx.get(name), str) for name in _PLACEHOLDER.findall(tmpl)):
        raise ValueError("a template names a placeholder it cannot fill")
    out = _PLACEHOLDER.sub(lambda m: ctx[m.group(1)], tmpl)
    if not out_re.fullmatch(out):
        raise ValueError("a template renders to a value outside its charset")
    return out


def runnable(names, backends=None):
    """The backends among `names` (a classifier's ordered matches), in their order, whose registry entry declares an
    execution role — what the planner, the scaffold and a search record's suggestion choose from. PURE."""
    backends = backends if backends is not None else load_backends()
    by_name = {b.get("name"): b for b in backends}
    return [n for n in names if backend_role(by_name.get(n)) is not None]


def _card_ok(card):
    """A search card's shape: a collection ansible-core ships (paths.ANSIBLE_CORE_BUNDLED — the planner neither probes
    nor pins a card's collection, so one naming a Galaxy collection would onboard it unpinned), a non-empty title, a
    device-class key, a list of keywords."""
    return (isinstance(card, dict) and card.get("collection") in paths.ANSIBLE_CORE_BUNDLED
            and isinstance(card.get("title"), str) and card["title"].strip() != ""
            and paths.is_component(card.get("key"))
            and isinstance(card.get("keywords"), list) and card["keywords"] != []
            and all(isinstance(k, str) and k != "" for k in card["keywords"]))


def search_cards(backends=None):
    """The SYNTHETIC search results the backend registry declares, as `(backend entry, card)` — for a target that has
    NO collection to search for, a plain SSH host above all (S24). A card lives on the backend it onboards with, so
    the backend names itself and nothing overrides the classifier: `collection` names the collection-less case the
    way Ansible does (`ansible.builtin`, which ships inside ansible-core), `title` is what the result shows, `key` the
    device-class key the onboard form proposes, `keywords` what a query matches. Only a backend that declares a role
    offers one (a card is something to onboard with); a card that is not well formed is skipped with a warning —
    shipped data, held to the shape by tests/unit/test_search_card.py. PURE given `backends`."""
    backends = backends if backends is not None else load_backends()
    out = []
    for b in backends:
        card = b.get("search_card")
        if card is None:
            continue
        if not _card_ok(card) or backend_role(b) is None:
            log.warning("backend %r: its search_card is not well formed, or the backend declares no role; skipped",
                        b.get("name"))
            continue
        out.append((b, card))
    return out


def bundled_collections(backends=None):
    """The collections the search cards name: ones that ship INSIDE ansible-core and are never installed from
    Galaxy. The onboard planner probes nothing for one (it is always there, and there is nothing to read) and pins
    nothing for it — a class onboarded from a card carries `collections: []` (S24)."""
    return {card["collection"] for _, card in search_cards(backends)}


def backend_confers(name, capability, backends=None):
    """What a backend `confers` for `capability` — the F2 host->capability accessor (the ONE dispatch seam, like
    `device_role`). `capability` is 'actuate'/'backup' (a scalar: true/false/'maybe') or 'telemetry'/'logs' (a list
    of method NAMES this execution paradigm can AUTO-DERIVE). Returns the conferred value, or None when the backend
    (or the key) is absent — `[]`/None means "derives nothing", so a backend that hasn't opted in stays back-compatible.
    PURE: reads only the loaded backend registry (no probe, no I/O beyond the loader)."""
    backends = backends if backends is not None else load_backends()
    bdef = next((b for b in backends if b.get("name") == name), {})
    return (bdef.get("confers") or {}).get(capability)


def _installed_json():
    """`ansible-galaxy collection list --format json` parsed to {search_path: {name: info}}, or {} when
    ansible-galaxy is slow/absent. The single subprocess+timeout seam that local_installed (name->version)
    and local_shallow (which also needs each collection's on-disk PATH) share — one timeout, one place."""
    try:
        p = subprocess.run(["ansible-galaxy", "collection", "list", "--format", "json"],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("ansible-galaxy collection list unavailable (%s) -- treating as none installed", e)
        return {}
    return json.loads(p.stdout or "{}") if p.returncode == 0 else {}


def local_installed():
    """Installed collections -> {name: version} (first search-path wins). Degrades to {} when
    ansible-galaxy is unavailable, so a hung/slow subprocess never blocks a search (reports none installed)."""
    found = {}
    for _path, colls in _installed_json().items():
        for name, info in colls.items():
            found.setdefault(name, info.get("version"))
    return found


def local_shallow(keywords, limit):
    """Keyword-matched INSTALLED collections as SHALLOW facts (probe.shallow_from_local) — the fast local
    mirror of galaxy_search: no ansible-doc, no network. Substring-matches the collection name, sorts, CAPS
    the fan-out at `limit`, then reads each match's files. This is the local arm of the redesigned /search;
    deep capability classification (module_options) is deferred to deep_probe / GET /probe. First path wins."""
    kw = [k.lower() for k in keywords]
    seen, hits = set(), []
    for base, colls in _installed_json().items():
        for name, info in colls.items():
            if name not in seen and any(k in name.lower() for k in kw):
                hits.append((name, info.get("version"), base))
                seen.add(name)
    hits.sort()
    if len(hits) > limit:
        log.warning("search %r matched %d installed collections; shallow-probing the first %d "
                    "(raise the limit or narrow the keywords for the rest)", keywords, len(hits), limit)
        hits = hits[:limit]
    out = []
    for name, version, base in hits:
        try:
            out.append(probe.shallow_from_local(name, version, base))
        except OSError as e:
            log.warning("shallow_from_local(%s) failed (%s); skipping", name, e)
    return out


def galaxy_search(keywords, limit):
    """Search galaxy.ansible.com for collections matching `keywords` -> shallow records, or [] if Galaxy
    is unreachable/slow. A short timeout + caught network errors keep this external dependency from hanging
    /search: a caller degrades to local-only results instead of blocking (galaxy is best-effort)."""
    q = urllib.parse.urlencode({"keywords": " ".join(keywords), "limit": limit, "is_highest": "true"})
    url = "%s/search/collection-versions/?%s" % (paths.GALAXY, q)
    try:
        with urllib.request.urlopen(url, timeout=_GALAXY_TIMEOUT) as resp:
            data = json.load(resp).get("data", [])
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        log.warning("galaxy search unavailable (%s) -- returning local-only results", e)
        return []
    return [probe.shallow_from_galaxy(r["collection_version"],
                                      (r.get("repository") or {}).get("name") == "certified")
            for r in data]
