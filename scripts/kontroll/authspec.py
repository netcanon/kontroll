"""authspec — F1: derive the onboard form's credential/key fields from PUBLIC backend metadata.

The north-star vision (docs/reviews/2026-06-29-north-star-onboard/10-self-describing-auth.md): a blind user picks a
result from raw search and the onboard form shows the EXACT per-entry credential fields — not today's static 4-field
union (a `network_cli` switch wanting SSH login, a REST device wanting a token). This is the credential slice.

TIER A (this module — Rung 0a): the per-backend `auth:` block in `ansible/backends/<name>/backend.yml` declares the
auth-field SHAPE as DATA (config-as-data-validated, not bespoke branching) — so the right SHAPE is known the instant a
result is picked, install-free and with zero introspection. TIER B (the EXACT per-param fields from a module's
`argument_spec` via `ansible-doc -j` + the auth-param heuristic) is a later rung; `derive_auth` accepts a `deep` flag
for it but TIER B is not built here. When a backend declares no `auth:` block (a brand-new/unclassified device),
`derive_auth` falls through to the generic union with `source="fallback"` so onboarding is NEVER blocked (INVARIANT D*).

The output is an ordered `[CredField]` descriptor list the onboard planner's data-driven loop consumes and the GUI
renders (reusing the existing knob renderer). Credential VALUES never touch this module — it is PURE SCHEMA (names,
kinds, labels), the same no-leak posture as `secret-forms/<domain>.yml`. Each field carries its target `domain` so the
planner's domain-confinement guard (MF-S2) can refuse a field that would cross into another SOPS domain.

TIER B (Rung 3): `auth_fields_from_argspec` reads the connecting module's `argument_spec` via `ansible-doc -j` and
applies the auth-param heuristic (§3) to emit the EXACT credential fields — e.g. proxmox's coherent
`api_host`+`api_user`+`api_token_id`+`api_token_secret` set, not a generic single `api_token` box. Two hardening
gates from the design's adversary pass (docs/reviews/2026-06-29-north-star-onboard/99-synthesis.md):
  * MF-S1 (install-confinement, FAIL-CLOSED): the "TIER-B never introspects an un-installed collection" invariant
    lives INSIDE `auth_fields_from_argspec` (assert `coll in local_installed()` BEFORE any `ansible-doc` call), so
    a future "sharpen a Galaxy result on hover" caller can't bypass it into executing an un-installed collection's
    Python. `ansible-doc` runs collection import-time Python — the trust boundary is the INSTALL, already gated.
  * MF-S6 (mask-ambiguous, FAIL-SAFE): `ansible-doc`'s `no_log` is NOT a complete secret oracle (a module can set
    `no_log` only in code). A genuinely-ambiguous field (no `no_log`, no secret-class name) defaults to
    MASKED/secret — masking a non-secret is harmless; plaintexting a missed secret is a leak.
"""
import json
import logging
import re

from kontroll import catalog, paths, probe

log = logging.getLogger("kontroll.authspec")

# The closed `kind` enum — drives the GUI widget + the validator + the secret/SOPS routing. Keep in sync with the
# onboard planner's loop and the GUI renderer.
KINDS = frozenset({"ssh_key", "password", "token", "host", "identity", "port", "bool", "ca_cert"})

# Kinds that are secret by default (masked input → creds_to_set → SOPS, never printed/logged). A backend entry may
# override with an explicit `secret:` but these are the safe defaults.
_SECRET_KINDS = frozenset({"ssh_key", "password", "token"})

# The kinds an `inline: true` field may have (S28). An inline value is written INTO the inventory drop-in as a
# literal host var, and Ansible templates inventory variables, so each kind here has a validator that turns the
# request's string into a typed value — an int, a bool. The request never chooses the bytes that reach the drop-in
# (SECURITY.md C22). A kind with no validator cannot be inline; adding one means adding its branch to inline_value.
INLINE_KINDS = frozenset({"port", "bool"})
_PORT_RE = re.compile(r"[0-9]{1,5}")                 # fullmatch only: "22\n", " 22", "+22", "２２" are not ports
_INLINE_TARGET_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def port_number(value):
    """The TCP port `value` names, as an int in 1..65535, or None when it names none. An int (never a bool) in that
    range, or a string of one to five ASCII digits whose number is in it: no sign, no space, no line break, no digit
    of another script — `re.fullmatch` over `[0-9]`, because `re.match` with `$` accepts a trailing newline (the trap
    the C22 review found in three validators). Shared by an inline port the request types and the `probe_port` a
    backend descriptor declares; each caller raises its own error type."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and _PORT_RE.fullmatch(value):
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535:
        return value
    return None


def inline_target(cf):
    """The host var an inline field writes, or None when its descriptor may not write one: the kind must have a
    validator (INLINE_KINDS), `maps_to` must be a lower-case identifier, and a `port` lands only in a `*_port` var.
    A descriptor cannot aim a typed value at an arbitrary variable — a valid int written as `ansible_user` is still
    the wrong thing in the wrong place. A None is the declaring file's fault, and the planner refuses it as
    repository data, before any write."""
    target = cf.get("maps_to")
    if cf.get("kind") not in INLINE_KINDS or not isinstance(target, str) or not _INLINE_TARGET_RE.fullmatch(target):
        return None
    if cf["kind"] == "port" and not target.endswith("_port"):
        return None
    return target


def inline_value(cf, raw):
    """The TYPED literal an inline field writes into its host var: kind `port` → an int in 1..65535 (port_number);
    kind `bool` → True or False, from a bool or exactly "true" / "false". Anything else is refused as a request
    field (paths.RequestBoundaryError, a 422 naming the field and the rule — never the value). Only a kind
    inline_target accepts reaches here; any other kind is refused the same way, so a new inline kind cannot be
    written as text by mistake."""
    field, kind = cf["field"], cf.get("kind")
    if kind == "port":
        port = port_number(raw)
        if port is not None:
            return port
        raise paths.RequestBoundaryError(field, "%s is not a TCP port (a whole number from 1 to 65535)" % field)
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw in ("true", "false"):
            return raw == "true"
        raise paths.RequestBoundaryError(field, "%s is not true or false" % field)
    raise paths.RequestBoundaryError(field, "%s cannot be written into the inventory" % field)

# The generic 4-field union — TODAY's static onboard fields (onboard.py's former if-ladder), kept as the FALLBACK
# shape when a backend declares no `auth:` block, so a brand-new/unclassified device degrades to EXACTLY today's
# form (never a dead end). Also the back-compat safety net: a legacy scalar the caller passes that the backend's
# block doesn't declare is mapped from here, so no existing caller's field is ever silently dropped.
_GENERIC_UNION = (
    {"field": "username", "kind": "identity", "label": "username (SSH)", "maps_to": "ansible_user"},
    {"field": "password", "kind": "password", "label": "password (SSH)", "maps_to": "ansible_password",
     "group": "ssh_login"},
    {"field": "api_token", "kind": "token", "label": "API token (REST devices)"},   # maps_to is dynamic (token_var)
    {"field": "ssh_private_key", "kind": "ssh_key", "label": "SSH private key (PEM)",
     "maps_to": "ansible_ssh_private_key_file", "group": "ssh_login"},
)


def _normalize(entry):
    """Fill a backend `auth:` entry into a complete CredField (defaults: `sops_stem`=field, secret-by-kind). Pure;
    carries NO value. `inline:true` marks a non-secret literal hostvar (host/port/bool) the planner renders inline,
    not as a SOPS lookup."""
    field = entry["field"]
    kind = entry.get("kind", "password")
    cf = {"field": field,
          "kind": kind,
          "label": entry.get("label", field),
          "maps_to": entry.get("maps_to"),
          "sops_stem": entry.get("sops_stem", field),
          "secret": bool(entry.get("secret", kind in _SECRET_KINDS)),
          "required": bool(entry.get("required", False))}
    for opt in ("group", "auth_set", "help"):
        if isinstance(entry.get(opt), str) and entry[opt]:      # a list or a mapping here: the key is left out
            cf[opt] = entry[opt]
    if entry.get("inline"):
        cf["inline"] = True
    # `shared` (F1 seam S1): a credential that belongs to the SOPS DOMAIN, not one host — stored FLAT under its
    # sops_stem (NOT host-keyed), so a single domain-wide service credential (e.g. the one read-only proxmox token
    # the pve-exporter uses for every node) lands under the exact name its consumer reads. A non-shared field stays
    # host-keyed so two onboarded hosts never collide. Only meaningful with an explicit sops_stem (the flat name).
    if entry.get("shared"):
        cf["shared"] = True
    return cf


def auth_fields_for_backend(bdef):
    """TIER A: the `[CredField]` shape an `auth:` block declares, or None if it declares none (→ the caller falls
    back to the generic union). Pure: reads only `bdef`'s `auth:` block — works for a backend.yml OR a module.yml
    (the same CredField-template shape; a curated class's module declares its own exact set, consumed by derive_auth
    on the reuse path). Skips malformed entries (no `field`, a `field` or `kind` that is not a string, an unknown
    `kind`) defensively so one bad drop-in can't break the whole form."""
    block = (bdef or {}).get("auth")
    if not isinstance(block, list) or not block:
        return None
    out = []
    for e in block:
        if (isinstance(e, dict) and isinstance(e.get("field"), str) and e["field"]
                and isinstance(e.get("kind", "password"), str) and e.get("kind", "password") in KINDS):
            out.append(_normalize(e))
    return out or None


# ── TIER B (Rung 3): the argument_spec heuristic (10-self-describing-auth.md §3) ──────────────────────────────── #
# Ordered, first-match-wins over the LOWER-CASED param name. (kind, secret_class?, regex). A name match NEVER makes
# a field secret unless it is a SECRET class (password/token/ssh_key); host/identity/port/bool/ca_cert are
# non-secret locators. HARD `no_log` (checked before this table) always wins; a param matching NOTHING but included
# (required, in an auth-bearing module) is MASKED fail-safe (MF-S6). Resist growing this into a YAML DSL — it is a
# small public regex list validated by fixture tests, the same shape as the search spike's SECRET_HINT.
_AUTH_PATTERNS = (
    ("password", True,  re.compile(r"(^|_)(password|passwd|pass|secret|auth_pass|key_secret)(_|$)")),
    ("token",    True,  re.compile(r"(^|_)(token|api_key|apikey|access_key|bearer)(_|$)")),
    # `client_key`/`*_key_data`/`private_key` are the PRIVATE half of a keypair — SECRET (a `client_cert` is the
    # public half → ca_cert below). Keeping client_key out of the non-secret ca_cert class is load-bearing: a
    # no_log-less private-key param must never render as a plaintext box (the review-20 false-plaintext finding).
    ("ssh_key",  True,  re.compile(r"(^|_)(ssh_?key|private_?key|privatekey|key_?file|key_?data|client_key|pem)(_|$)")),
    ("host",     False, re.compile(r"(^|_)(api_host|hostname|fqdn|base_url|endpoint|server|host|url)(_|$)")),
    ("identity", False, re.compile(r"(^|_)(api_user|username|user|login|account|token_id|token_name|client_id)(_|$)")),
    ("port",     False, re.compile(r"(^|_)(api_port|port)(_|$)")),
    ("bool",     False, re.compile(r"(^|_)(validate_certs|verify_ssl|use_ssl|use_tls|insecure)(_|$)")),
    ("ca_cert",  False, re.compile(r"(^|_)(ca_cert|ca_path|cacert|ca_bundle|client_cert)(_|$)")),
)
_SECRET_CLASS_KINDS = frozenset({"password", "token", "ssh_key"})
# Short module-name signals worth introspecting for connection auth (bounded — `ansible-doc` runs collection Python,
# so we cap the candidate set). Kept to GENUINE connection/api-auth signals — deliberately NOT `command`/`facts`/
# `info`/`user`, which never carry the CONNECTION credential (a `*_user` module manages a DEVICE ACCOUNT, and letting
# it in is what let TIER-B replace a switch's SSH-login shape — review-30 F-1). The structural gate is the backend
# connection class (`_is_module_param_backend`); this regex is the secondary bound.
# An option NAME out of an installed module's documentation becomes a credential field's name, and the onboard
# planner formats that name into a lookup expression (its own `_STEM_RE`, the same rule, written twice so neither
# module imports the other). An option whose name is not an identifier is therefore not a field TIER-B offers:
# a third party's spelling must never be able to refuse an onboard ("never a blocked onboard", below). When the
# option it cannot name is REQUIRED or marked `no_log`, the module's set would no longer be exact, so the module
# is a miss as a whole and the TIER-A shape stands.
_OPTION_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_AUTH_SIGNAL_MODULE_RE = re.compile(r"(^|_)(api|kvm|node|cluster|login|auth|token|session)s?$")
_MAX_CANDIDATE_MODULES = 4


def _pattern_kind(name):
    """(kind, is_secret_class) for the first _AUTH_PATTERNS match on the lower-cased NAME, or (None, False)."""
    low = name.lower()
    for kind, secret_class, rx in _AUTH_PATTERNS:
        if rx.search(low):
            return kind, secret_class
    return None, False


def _classify_param(name, spec, module_has_secret):
    """Classify ONE argspec param into a CredField, or None to SKIP it (an operational knob, not a credential —
    owned by F2/the configure surface, not here). Heuristic order (§3, hardened by MF-S6):
      1. `no_log: true`      → HARD secret (the module author's own declaration; overrides the name).
      2. secret-class name   → secret (password/token/ssh_key).
      3. non-secret locator  → host/identity/port/bool/ca_cert; INCLUDED only when it is `required` OR co-occurs
                                with a `no_log` param (part of the auth SET, not a stray operational target).
      4. MF-S6 mask-ambiguous: no `no_log`, no name signal, but `required` in an auth-bearing module → genuinely
                                ambiguous → default MASKED/secret (fail-safe: masking a non-secret is harmless;
                                plaintexting a missed secret is a leak). Everything else → skip."""
    no_log = bool(spec.get("no_log"))
    required = bool(spec.get("required"))
    kind, secret_class = _pattern_kind(name)
    desc = spec.get("description")
    if isinstance(desc, list):
        desc = " ".join(str(d) for d in desc)
    help_ = ((desc or "").strip()[:200]) or None

    def cf(k, secret, derivation):
        d = {"field": name, "kind": k, "label": name.replace("_", " "), "sops_stem": name,
             "secret": secret, "required": required, "derivation": derivation}
        if help_:
            d["help"] = help_
        return d

    if no_log:                                              # (1) HARD — mask regardless of the name
        return cf(kind if kind in _SECRET_CLASS_KINDS else "password", True, "no_log")
    if kind in _SECRET_CLASS_KINDS:                         # (2) secret by name class
        return cf(kind, True, "secret_name")
    if kind is not None:                                    # (3) a non-secret locator
        if required:
            return cf(kind, False, "required_locator")
        if module_has_secret:
            return cf(kind, False, "identity_in_set")
        return None                                         # an optional, unrelated locator → operational, skip
    if required and module_has_secret:                      # (4) MF-S6 — ambiguous required member → MASK fail-safe
        return cf("password", True, "ambiguous_masked")
    return None                                             # an operational knob → not a credential


def _load_module_options(coll, module, ad):
    """The `argument_spec` options of `coll.module` from `ansible-doc -j` (the `ad` seam, injectable for offline
    tests). Returns {param: spec} or {} on any failure/garbage — degrade, never raise. ONLY called after MF-S1."""
    try:
        doc = json.loads(ad(["-j", "%s.%s" % (coll, module)]) or "{}")
    except (ValueError, TypeError):
        return {}
    for entry in (doc or {}).values():
        opts = ((entry or {}).get("doc") or {}).get("options")
        if isinstance(opts, dict):
            return opts
    return {}


def auth_candidate_modules(facts):
    """The bounded module set TIER-B introspects for connection auth — the (already deep-probed) modules whose short
    name matches a connection/api signal (api/kvm/node/…), capped at _MAX_CANDIDATE_MODULES. Pure (no I/O): reads
    only `facts["modules"]`. A non-string module entry is skipped (never raised — this runs OUTSIDE derive_auth's
    try/except at the onboard call site, so it must honour the never-raise contract itself; review-30 F-2)."""
    mods = (facts or {}).get("modules") or []
    return [m for m in mods if isinstance(m, str) and _AUTH_SIGNAL_MODULE_RE.search(m.lower())][:_MAX_CANDIDATE_MODULES]


def _is_module_param_backend(bdef):
    """True when the backend authenticates via MODULE PARAMETERS — a `local`/uri connection (the api/proxmox shape,
    design §2.1) — NOT via Ansible's magic connection vars. TIER-B argspec derivation only makes sense here: a
    conn-magic backend (network_cli/ssh/netconf) carries its credential as `ansible_user`/`ansible_password`/the SSH
    key (declared in the backend `auth:` block, TIER-A), which a module's argument_spec does NOT express — so
    introspecting a cliconf collection would REPLACE the real SSH-login shape with a module's DEVICE-ACCOUNT params
    (e.g. `cisco.ios.ios_user`), leaving the operator no way to enter the connection creds (review-30 F-1). Gating
    the REPLACE on the connection class is the structural fix; the module-name regex is only a secondary bound."""
    return ((bdef or {}).get("connection") or {}).get("ansible_connection") == "local"


def auth_fields_from_argspec(coll, modules, *, ad=probe._ad, installed=None):
    """TIER B: the EXACT credential fields from the connecting module's `argument_spec`. Returns an ordered
    [CredField] (one coherent `auth_set` from the first auth-bearing module — deterministic), or None on any miss
    (→ derive_auth keeps the TIER-A shape; NEVER blocks onboarding, INVARIANT D*). PURE SCHEMA — no value. Never
    raises.

    MF-S1 (FAIL-CLOSED install-confinement): the `coll in installed` check is HERE, before ANY `ansible-doc` call,
    so TIER-B can never introspect (execute the Python of) an un-installed collection — not even if a future caller
    forgets the search-flow gate. `installed` is injectable for offline tests; the default enumerates
    `catalog.local_installed()`."""
    inst = installed if installed is not None else set(catalog.local_installed())
    if coll not in inst:
        log.debug("TIER-B skipped: %s not installed (MF-S1 install-confinement)", coll)
        return None                                         # fail-closed: never introspect an un-installed collection
    for m in (modules or [])[:_MAX_CANDIDATE_MODULES]:
        opts = _load_module_options(coll, m, ad)
        if not opts:
            continue
        has_secret = any(isinstance(s, dict) and s.get("no_log") for s in opts.values())
        if any(not (isinstance(n, str) and _OPTION_NAME_RE.fullmatch(n))
               for n, s in opts.items() if isinstance(s, dict) and (s.get("required") or s.get("no_log"))):
            continue        # a required or `no_log` option this tool cannot name: not an EXACT set — TIER-A stands
        fields = [cf for cf in (_classify_param(n, s, has_secret) for n, s in opts.items()
                                if isinstance(n, str) and _OPTION_NAME_RE.fullmatch(n) and isinstance(s, dict))
                  if cf is not None]
        if not any(cf["secret"] for cf in fields):          # no secret → not an auth-bearing module (conn-magic gear)
            continue
        auth_set = "%s_api" % coll.split(".")[-1]
        for cf in fields:
            cf.setdefault("auth_set", auth_set)
        return fields                                       # first auth-bearing module wins (bounded, deterministic)
    return None


def derive_auth(bdef, domain, *, deep=False, module=None, coll=None, modules=None, ad=probe._ad, installed=None):
    """The one entrypoint the onboard planner calls. Returns
    `{"fields": [CredField], "source": "shallow"|"fallback"|"deep"}`.

    TIER A: a `module`'s own `auth:` block takes PRECEDENCE over the backend's generic shape when given (the reuse
    path — a curated class names its EXACT credential set, e.g. proxmox's coherent api-token `auth_set` with
    sops_stem names lined up to its exporter's secret_env_map, which the generic per-backend table can't express);
    else the backend's `auth:` block; else the generic union (`source="fallback"`). Either declared source is
    `source="shallow"`.

    TIER B (Rung 3): when `deep` and `coll`+`modules` are given AND the backend is a MODULE-PARAMETER backend
    (`_is_module_param_backend` — a `local`/uri connection, the api/proxmox shape), `auth_fields_from_argspec`
    derives the EXACT fields from the connecting module's `argument_spec`; a coherent secret set REPLACES the coarse
    TIER-A shape (`source="deep"`). TIER-B is **gated to module-param backends** because a conn-magic backend
    (network_cli/ssh/netconf) carries its credential as the magic connection vars — introspecting it would replace
    the real SSH-login shape with a module's device-account params (review-30 F-1). TIER-B is install-confined
    (MF-S1) + fail-safe (MF-S6); on any miss (wrong backend / un-installed / ambiguous / error) the TIER-A shape
    stands — never a blocked onboard (INVARIANT D*).

    Stamps each field's `domain` so the planner's domain-confinement guard (MF-S2) can refuse a secret field that
    would cross into another SOPS domain. NEVER raises."""
    fields = auth_fields_for_backend(module) if module is not None else None
    if fields is None:
        fields = auth_fields_for_backend(bdef)
    source = "shallow"
    if fields is None:
        fields = [_normalize(dict(cf)) for cf in _GENERIC_UNION]
        source = "fallback"
    if deep and coll and modules and _is_module_param_backend(bdef):
        try:
            tb = auth_fields_from_argspec(coll, modules, ad=ad, installed=installed)
        except Exception as e:                              # noqa: BLE001 — TIER-B must never break the plan (D*)
            log.warning("TIER-B argspec derivation failed (%s); keeping the TIER-A shape", e)
            tb = None
        if tb:
            fields = tb                                     # exact argspec fields supersede the coarse shape
            source = "deep"
    for cf in fields:
        cf["domain"] = domain
    return {"fields": fields, "source": source}


def union_field(field):
    """The generic-union CredField for a legacy scalar `field` (the back-compat safety net: a field the caller
    passed that the backend's block doesn't declare is mapped from here, so it's never silently dropped). None if
    `field` isn't a known legacy field."""
    for cf in _GENERIC_UNION:
        if cf["field"] == field:
            return _normalize(dict(cf))
    return None


# The CredField keys safe to expose on a CLIENT/WIRE surface — pure schema (names/kinds/labels/flags), NEVER a
# credential value. The descriptors this module builds are already value-free (test_derived_fields_carry_no_
# credential_values pins it); this projection is the belt-and-braces wire guard so even a future value-bearing key
# on a descriptor can't ride along through a plan view or the GET /onboard/cred-fields route.
_PUBLIC_KEYS = ("field", "kind", "label", "maps_to", "sops_stem", "secret", "required",
                "group", "auth_set", "help", "inline", "shared", "domain", "derivation")


def public_descriptor(cf):
    """A WIRE-SAFE projection of a CredField — only the schema keys (names/kinds/labels/flags), never a value. Used
    by the onboard plan views + the GET /onboard/cred-fields route so the GUI gets the field SHAPE with no risk of
    a value riding along (the same names-only posture as secret-forms)."""
    return {k: cf[k] for k in _PUBLIC_KEYS if k in cf}
