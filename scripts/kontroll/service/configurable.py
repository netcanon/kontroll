"""configurable — the read-only PROJECTION that turns each existing descriptor shape (a method's params, a
secret-form's fields, a capability's resource_stage) into ONE uniform KNOB / knob-GROUP model the GUI's single
knob renderer (`renderKnob`/`renderKnobGroup`, Phase 3) consumes. The no-bespoke-config tenet applied to the
GUI: "expose a knob" is a descriptor edit, never UI code, because capability params, secret fields, and the
Stage-3 resource all render through ONE data-driven renderer over this projection.

PURE + READ-ONLY (pinned by `tests/unit/_readonly_pins.assert_read_only`): it only reads descriptors + the
read-only suggest/offer views and reshapes them — it stages nothing, decrypts nothing, and serves secret NAMES
only (never a value; the secret VALUE entry stays the SOPS stage path). Degrade-to-empty, never `sys.exit`.

The knob descriptor (the unit of "one lever") is a strict SUPERSET of the two shipping shapes — the method-param
`{allowed, required, label_as}` and the secret-form field `{key,label,type,required,generate,default,help,
already_set}` — so EXISTING descriptors map with ZERO edits (design
`docs/reviews/2026-06-19-gui-paradigm/21-design-actionable-config-plane.md` §2/§4). The renderer reads
`key/label/type/allowed/range/pattern/default/required/help/secret/already_set/multiline`; the server
re-validates every submitted value against the same descriptor via `service/_validate` (the P0a guard). This is
the EXPOSURE half only — the diff/merge/overwrite write-back safety half is a later phase (design 21 §9 / 22).
"""
import logging
import os
import re

from kontroll.service import capability, secrets

log = logging.getLogger("kontroll.service.configurable")

# A knob group's testid names the container the page renders the group into, and the page finds that container
# with a selector built from it — and finds every other element of the dialog by testid too, first match first,
# after the container exists. A descriptor's value is held to its capability's own namespace, `cap-<cap>-`, then
# this closed charset: always a valid selector, never one of its own (T22 / F9: a space or a bracket made the lookup
# throw and left the dialog's buttons unwired), and never a testid the page gives anything else in the dialog — a
# control (`cap-promote`, which would stay disabled with its handler on the container), the method container
# (`cap-params`, which the Stage-3 lookup would wipe) or a knob row (`knob-<key>`). The one collision the namespace
# leaves — a capability named `cred` meeting the page's `cap-cred-*` controls — is held by test_capability_registry.py
# (the shipped descriptors, a synthetic `cred` one, and `cred` pinned as the only such name, so reserved); wiring the
# dialog by reference (T23) removes the class.
_TESTID = re.compile(r"[a-z][a-z0-9-]{0,62}")


def group_testid(value, owner):
    """`value` when it is a testid in capability `owner`'s namespace — `cap-<owner>-`, then [a-z0-9-], 63 characters
    at most — else None (the page renders no container for the group) and a warning naming `owner`, the descriptor,
    never the value."""
    prefix = "cap-%s-" % (owner,)
    if isinstance(value, str) and _TESTID.fullmatch(value) and value.startswith(prefix) and value != prefix:
        return value
    if value is not None:
        log.warning("knob group of %s: its testid is not cap-%s- then [a-z0-9-] (63 at most) — dropped, the group "
                    "renders no container", owner, owner)
    return None

# Secret-form field types that hold a CREDENTIAL VALUE (rendered write-only, never echoed). `text` is a
# NON-secret field (e.g. a username) that keeps its default; `textarea` is a MULTI-LINE secret (a PEM key).
_SECRET_FIELD_TYPES = frozenset({"password", "token", "textarea"})


def param_knob(name, spec):
    """Project a method-param spec (`{allowed, required, label_as}`) into a uniform knob. Type is inferred `enum`
    (an allow-list with no explicit type — design 21 §2.3), so a telemetry/backup/logging method param renders
    through the shared renderer with no descriptor edit. `label_as` is a GENERATOR concern, not a render one —
    not carried (the renderer ignores it)."""
    return {"key": name, "label": name, "type": "enum",
            "allowed": list(spec.get("allowed") or []), "required": bool(spec.get("required"))}


def secret_field_knob(field):
    """Project a secret-form field (`secrets.offerable_fields` shape) into a uniform knob, NAMES-only: it carries
    key/label/required/generate/help/already_set + the derived `secret`/`multiline` render flags, but NEVER a
    value — `already_set` is a boolean, not the value (C11). A password/token/textarea field is `secret: true`
    (rendered write-only, never pre-filled); a `text` field is non-secret and keeps its `default`."""
    ftype = field.get("type", "text")
    secret = ftype in _SECRET_FIELD_TYPES
    return {"key": field["key"], "label": field.get("label", field["key"]),
            "type": "secret" if secret else "text",
            "required": bool(field.get("required")), "generate": field.get("generate"),
            "default": None if secret else field.get("default"),   # never a default for a secret value (C11)
            "help": field.get("help", ""), "already_set": bool(field.get("already_set")),
            "secret": secret, "multiline": ftype == "textarea"}


def _stage_group(descriptor):
    """Project a capability descriptor's `resource_stage` (`{testid, source_kind, fields}`) into a knob GROUP.
    For `policy_fields` the descriptor's `fields` become knobs; `curated_list`/`none` carry NO knobs today
    (`suggested_dashboards` is a later phase — `telemetry/README.md`; backup/logging ride Stage-1), so the group
    renders its container honestly empty — the render-table CASE exists without authoring speculative knobs
    (design 21 §10)."""
    rs = descriptor.get("resource_stage") or {}
    sk = rs.get("source_kind", "none")
    knobs = []
    if sk == "policy_fields":
        knobs = [(param_knob(f["key"], f) if f.get("allowed") is not None else
                  {"key": f["key"], "label": f.get("label", f["key"]), "type": f.get("type", "text"),
                   "required": bool(f.get("required")), "pattern": f.get("pattern")})
                 for f in (rs.get("fields") or [])]
    return {"id": "%s-stage" % descriptor.get("name", "cap"), "source_kind": sk,
            "testid": group_testid(rs.get("testid"), descriptor.get("name", "cap")), "label": rs.get("label"),
            "knobs": knobs}


def _method_entry(m, current, declared):
    """Project one method (offerable=add, or reconfigurable=declared) into the dialog's method shape: a `params`
    knob group whose knobs are PRE-FILLED from the declared `current` values when `declared` (the reconfigure
    read-back — design 22 §3.1). A new (add) method carries no pre-fill (the descriptor default applies)."""
    cur = current.get(m["name"], {}) if declared else {}
    knobs = []
    for p, s in (m.get("params") or {}).items():
        k = param_knob(p, s)
        if cur.get(p) is not None:
            k["default"] = cur[p]                            # pre-select the current declared value
        knobs.append(k)
    return {"name": m["name"], "label": m.get("label", m["name"]), "declared": declared,
            "secret_domain": m.get("secret_domain"),
            "group": {"id": "params", "source_kind": "params", "testid": "cap-params", "knobs": knobs}}


def _capability_view(cap, key):
    """Project a capability dialog: WRAP the read-only `capability.suggest_view` and add (a) a `params` knob
    group per method — OFFERABLE (new → add) and RECONFIGURABLE (declared → pre-filled, Phase 4a) — and (b) the
    `resource_stage` group, so the cap dialog renders Stage-1 params AND Stage-3 through the one renderer. A thin
    wrapper (NOT a re-implementation of detection): suggest_view already does the read-only probe + reconciliation
    + the current read-back; this only adds the projection."""
    d = capability.get_descriptor(cap)
    if not d:
        return {"error": "no_capability", "kind": "capability:%s" % cap, "id": key}
    view = capability.suggest_view(cap, key)
    if view.get("error"):
        return {"error": view["error"], "kind": "capability:%s" % cap, "id": key}
    current = view.get("current") or {}
    methods = ([_method_entry(m, current, False) for m in view.get("offerable_methods", [])]
               + [_method_entry(m, current, True) for m in view.get("reconfigurable_methods", [])])
    return {"error": None, "kind": "capability:%s" % cap, "id": key,
            "object": {"kind": "capability:%s" % cap, "id": key, "label": d.get("label", cap)},
            "suggestion": view.get("suggestion"), "declared": view.get("declared", []),
            "current": current, "methods": methods, "resource_stage": _stage_group(d)}


def _secret_view(domain):
    """Project a secret-domain dialog into ONE `fields` knob group — NAMES-only (`secrets.offerable_fields` reads
    only key names + set/unset, never a value)."""
    info = secrets.offerable_fields(domain)
    if info.get("error"):
        return {"error": info["error"], "kind": "secret-domain", "id": domain}
    group = {"id": "fields", "source_kind": "fields", "testid": "secret-fields",
             "knobs": [secret_field_knob(f) for f in info.get("fields", [])]}
    return {"error": None, "kind": "secret-domain", "id": domain,
            "object": {"kind": "secret-domain", "id": domain, "label": info.get("label", domain)},
            "description": info.get("description", ""), "recipients": info.get("recipients", "base"),
            "groups": [group]}


def identity_knob(knob, current):
    """Project a module-identity descriptor knob (settings/module-identity.yml) into the uniform render knob,
    PRE-FILLED from the class's `current` value (a non-secret config key — never a value leak). Carries the
    `severity`/`blast_radius`/`confirm_text` the reconfigure GATE keys the right-weight confirm off (the
    type-to-confirm `reconfig-identity-confirm` for an `identity` knob) — the §10 descriptor metadata reaching
    the renderer. The render `type`/`allowed`/`pattern` drive the widget; the server re-validates via
    service/_validate regardless (the widget is convenience, the server is the guard)."""
    out = {"key": knob["key"], "label": knob["key"], "type": knob.get("type", "text"),
           "required": True, "help": knob.get("help", ""), "reconfigurable": knob.get("reconfigurable", True),
           "severity": knob.get("severity"), "blast_radius": knob.get("blast_radius"),
           "confirm_text": knob.get("confirm_text")}
    if knob.get("allowed") is not None:
        out["allowed"] = list(knob["allowed"])
    if knob.get("pattern"):
        out["pattern"] = knob["pattern"]
    cur = current.get(knob["key"])
    if cur is not None:
        out["default"] = cur                                 # pre-select the class's current identity value
    return out


def _identity_view(key):
    """Project a module-identity dialog into ONE `identity` knob GROUP — every declared identity knob pre-filled
    from the class's current `module.yml` value, carrying the severity/blast_radius/confirm_text. `not_onboarded`
    if the class has no module.yml. The reconfigure surface IS this projection + the diff pane + the severity
    confirm — one data-driven surface, no bespoke 'reconfigure identity' form (design 22 §11.4)."""
    from kontroll.service import identity                     # local import — the read-back + the knob descriptors
    if not os.path.exists(identity._mod_path(key)):
        return {"error": "not_onboarded", "kind": "module-identity", "id": key}
    current = identity.current_values(key)
    group = {"id": "identity", "source_kind": "identity", "testid": "identity-fields",
             "knobs": [identity_knob(k, current) for k in identity._knobs() if k.get("key")]}
    return {"error": None, "kind": "module-identity", "id": key,
            "object": {"kind": "module-identity", "id": key, "label": "%s · identity" % key},
            "current": current, "groups": [group]}


def _host_view(key, host):
    """Project an inventory host-var dialog into ONE `host` knob GROUP — every editable host knob pre-filled from
    the host's current drop-in value (a non-secret connection var; a templated cred-lookup is skipped — read-only;
    never a value leak). `not_onboarded` if the host/file is absent. Reuses `identity_knob` (it carries the
    default + severity/blast/confirm the gate reads). Phase 4b §5.3."""
    from kontroll.service import hostvars                     # local import — the read-back + the knob descriptors
    _, group, _ = hostvars._find_host(key, host)
    if group is None:
        return {"error": "not_onboarded", "kind": "inventory-host", "id": "%s:%s" % (key, host)}
    current = hostvars.current_values(key, host)
    grp = {"id": "host", "source_kind": "host", "testid": "host-fields",
           "knobs": [identity_knob(k, current) for k in hostvars._knobs() if k.get("key")]}
    return {"error": None, "kind": "inventory-host", "id": "%s:%s" % (key, host),
            "object": {"kind": "inventory-host", "id": "%s:%s" % (key, host),
                       "label": "%s · host %s" % (key, host)},
            "current": current, "groups": [grp]}


def _settings_knob_view(knob):
    """Project a single platform-settings knob (Phase 5) into a one-knob group pre-filled from instance.yml's
    current value — reuses `identity_knob` (default + severity/blast_radius/confirm_text the gate reads). A `list`
    knob (backup_remotes) pre-fills as its comma-joined text. `no_knob` if the key isn't an editable settings
    knob (the closed allow-list). Non-secret platform config — never a value leak (api_privileged/.env are FORBID,
    not here)."""
    from kontroll.service import settings
    k = settings._edit_knob(knob)
    if not k:
        return {"error": "no_knob", "kind": "settings-knob", "id": knob}
    cur = settings.current_value(knob)
    if k.get("writer") == "list" and isinstance(cur, list):
        cur = ",".join(cur)                                  # the comma-separated text widget pre-fill
    grp = {"id": "settings", "source_kind": "settings", "testid": "settings-fields",
           "knobs": [identity_knob(k, {knob: cur})]}
    return {"error": None, "kind": "settings-knob", "id": knob,
            "object": {"kind": "settings-knob", "id": knob, "label": "settings · %s" % knob},
            "current": {knob: cur}, "groups": [grp]}


def _settings_fleet_view(module):
    """Project a fleet-removal dialog (Phase 5): NO knobs — disabling a module is not a value edit, so Propose
    computes the removal and the remove-confirm gate fires. `not_enabled` if the module isn't active."""
    from kontroll.service import settings
    if module not in (settings.current_value("enabled_modules") or []):
        return {"error": "not_enabled", "kind": "settings-fleet", "id": module}
    return {"error": None, "kind": "settings-fleet", "id": module,
            "object": {"kind": "settings-fleet", "id": module, "label": "fleet · disable %s" % module},
            "current": {}, "groups": []}


def _unit_knob(knob):
    """Project a curated actuation-unit knob descriptor (actuation/<key>/unit.yml `knobs:`) into the uniform
    render knob the ONE renderer + service/_validate consume — key/label/type/required/help + the type options
    (allowed/range/pattern/default). The worked extraction (report 40) mandated CURATED knobs over argspec
    extraction for this fleet, so the configure surface is the operator-declared descriptor, validated by the
    SAME _validate guard the rest of the GUI uses. A secret-typed actuation var is forbidden at the descriptor
    layer (gen-actuation; SEC-2), so this projection has no credential-value surface — it carries config only."""
    out = {"key": knob["key"], "label": knob.get("label", knob["key"]),
           "type": knob.get("type", "text"), "required": bool(knob.get("required")),
           "help": knob.get("help", "")}
    for opt in ("allowed", "range", "pattern", "default"):
        if knob.get(opt) is not None:
            out[opt] = knob[opt]
    return out


def _unit_view(key):
    """Project an actuation unit's curated configure knobs into ONE `unit` knob GROUP for the GUI configure
    stage (R3). Read-only registry read via catalog.actuation_unit; `no_unit` when the key is absent (the route
    maps it to 404). The group renders through the EXISTING renderKnobGroup (the `unit` source_kind → renderKnob)
    — zero new renderer code (design 24 §6.1). A unit with no `knobs:` block projects an honestly empty group
    (nothing to configure), never an error."""
    from kontroll import catalog                              # local import — read-only registry read, no per-call cost elsewhere
    u = catalog.actuation_unit(key)
    if not u:
        return {"error": "no_unit", "kind": "actuation-unit", "id": key}
    grp = {"id": "unit", "source_kind": "unit", "testid": "unit-config-fields",
           "knobs": [_unit_knob(k) for k in (u.get("knobs") or []) if k.get("key")]}
    return {"error": None, "kind": "actuation-unit", "id": key,
            "object": {"kind": "actuation-unit", "id": key, "label": "%s · configure" % key},
            "current": {}, "groups": [grp]}


def configurable_view(kind, obj_id):
    """The single read-only projection behind the GUI's one knob renderer. CLOSED dispatch over the known object
    kinds (NOT a universal 'configure-anything' registry — design 21 §10 / synthesis M5): `secret-domain` → one
    `fields` group; `capability:<cap>` → per-method `params` groups + resource_stage; `module-identity` → the
    class's identity knob group; `inventory-host` (id `<key>:<host>`) → the host's editable-var group (Phase 4b);
    `settings-knob` → one platform knob; `settings-fleet` → a no-knob fleet-removal dialog (Phase 5);
    `actuation-unit` (id = the actuation `key`) → the unit's curated configure-knob group (R3 app-store). Returns
    `{error, object, groups|methods+resource_stage, ...}`; degrade-to-empty, never `sys.exit`. Pinned read-only."""
    if kind == "secret-domain":
        return _secret_view(obj_id)
    if kind == "actuation-unit":
        return _unit_view(obj_id)
    if kind == "module-identity":
        return _identity_view(obj_id)
    if kind == "inventory-host":
        key, _, host = (obj_id or "").partition(":")
        return _host_view(key, host)
    if kind == "settings-knob":
        return _settings_knob_view(obj_id)
    if kind == "settings-fleet":
        return _settings_fleet_view(obj_id)
    if isinstance(kind, str) and kind.startswith("capability:"):
        return _capability_view(kind.split(":", 1)[1], obj_id)
    return {"error": "no_kind", "kind": kind, "id": obj_id}
