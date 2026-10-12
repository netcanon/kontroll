"""service/_actuation_schema — the actuation-unit descriptor SCHEMA + its fail-closed validator, the ONE contract.

`validate_unit` / `validate_knobs` (and the closed-enum constants) are the single source of truth for what a
well-formed `instance/actuation/<key>/unit.yml` is. They were lifted out of `scripts/gen-actuation.py` so the
descriptor contract has TWO consumers without duplicating the rules (the no-bespoke-config / DRY-in-code tenet):

  * `gen-actuation.py` — the fail-closed CLI validator wired into `tests/validate` (a malformed committed/staged
    descriptor never unions a pin / never reaches `ansible-galaxy`); it re-exports these and adds the file-walking
    `validate_all` + `_enabled_modules` + `main`.
  * `service/actuation.build_create_unit_plan` — the app-store CREATE seam: it RENDERS a fresh descriptor from a
    searched collection and MUST validate it through the SAME rules BEFORE it stages (validate-before-stage), so a
    GUI-authored unit is held to exactly the bar a hand-authored one is.

PURE + light-deps (re + service/_validate only — no yaml/paths/network), so it loads in the hermetic
`tests/validate` and CI without ansible, exactly as gen-actuation did. Read-only: it inspects already-parsed docs;
it opens nothing, stages nothing. Schema reference + field semantics: actuation/README.md.
"""
import re

from kontroll.service import _validate

SCHEMA = 1
KINDS = ("role", "collection_playbook", "standalone")
INSTALLABLE = ("role", "collection_playbook")
SOURCES = ("galaxy", "git")
SIGNATURES = ("required", "adaptive", "none")   # adaptive = verify-if-served/pass-if-absent (the brick-proof default)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
HIGH_BLAST_GROUPS = ("edge_firewall", "core_switch")   # the Tier-cap: unverified provenance may not reach these
BLAST = ("this-device", "VLAN", "LAN", "WAN", "all")
KEY_RE = re.compile(r"^[a-z][a-z0-9-]*\Z")
# The version of an exact `==` pin: one plain version word. It ends up inside double quotes in the generated
# collection lockfile (scripts/gen-requirements.py renders that file by hand), so the charset is closed here.
PIN_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}\Z")

# The curated configure-form knob vocabulary (R3): EXACTLY the server-validated type set (service/_validate's
# registry) — so a unit's `knobs:` render through the ONE knob renderer and re-validate through the SAME P0a
# guard the rest of the GUI uses (the widget is convenience, _validate is the contract). Derived from the
# registry (never a parallel list) so the two stay in lockstep. A SECRET-typed actuation var is FORBIDDEN in the
# MVP — there is no per-unit SOPS domain, so a unit may not carry a credential (SEC-2); secrets route through the
# Secrets dialog. An ansible var name is lowercase-with-underscores.
KNOB_TYPES = tuple(sorted(_validate._VALIDATORS))
FORBIDDEN_KNOB_TYPES = ("secret", "password", "token", "textarea")
KNOB_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\Z")


def validate_knobs(knobs):
    """Validate a unit's OPTIONAL curated `knobs:` block (R3 configure form) — the operator-declared levers the
    GUI renders + the server re-validates. Returns a list of error strings (empty = valid / absent). FAIL-CLOSED:
    each knob needs a `key` (^[a-z][a-z0-9_]*$, unique) + a `type` in the server-validated set (KNOB_TYPES); a
    secret-typed var is rejected with a pointed message (no secret actuation vars — SEC-2); a `text` knob needs a
    `pattern` and an `enum` a non-empty `allowed` (else _validate rejects every value at runtime — catch it at
    author time); an `int` `range` must be min<=max; and a curated `default`, if present, must itself VALIDATE
    through service/_validate (a default can't smuggle a value the live validator would reject). The
    worked-extraction MVP (report 40): curated knobs, not argspec extraction."""
    errs = []
    if knobs is None:
        return errs
    if not isinstance(knobs, list):
        return ["knobs must be a list"]
    seen = set()
    for i, k in enumerate(knobs):
        if not isinstance(k, dict):
            errs.append("knobs[%d] must be a mapping" % i)
            continue
        key = k.get("key")
        where = key if key else "[%d]" % i
        if not key or not KNOB_KEY_RE.fullmatch(str(key)):
            errs.append("knob %s key must match %s" % (where, KNOB_KEY_RE.pattern))
        elif key in seen:
            errs.append("knob %s key is duplicated" % key)
        else:
            seen.add(key)
        t = k.get("type")
        if t in FORBIDDEN_KNOB_TYPES:
            errs.append("knob %s type %r is forbidden — a secret-typed actuation var is not allowed in the MVP "
                        "(route secrets via the Secrets dialog; SEC-2)" % (where, t))
            continue
        if t not in KNOB_TYPES:
            errs.append("knob %s type %r must be one of %s" % (where, t, list(KNOB_TYPES)))
            continue
        if t == "text" and not k.get("pattern"):
            errs.append("knob %s is type 'text' but carries no validation pattern — rejected (no unvalidated "
                        "free field; design 21 §10)" % where)
        if t == "enum" and not (k.get("allowed") or []):
            errs.append("knob %s is type 'enum' but carries no non-empty 'allowed' list" % where)
        if t == "int":
            rng = k.get("range") or {}
            lo, hi = rng.get("min"), rng.get("max")
            if lo is not None and hi is not None and lo > hi:
                errs.append("knob %s range.min %r is above range.max %r" % (where, lo, hi))
        if "default" in k:
            verr = _validate.validate_value(k, k["default"])
            if verr:
                errs.append("knob %s default is invalid: %s" % (where, verr))
    return errs


def validate_unit(doc, dirkey, enabled_modules):
    """Return the list of human-readable error strings for ONE descriptor (empty list = valid). Pure — tests call
    it directly with crafted docs. `enabled_modules` is the fleet's enabled list (None skips the target-enabled
    check, for a unit-shape-only test)."""
    errs = []
    if doc.get("schema") != SCHEMA:
        errs.append("schema must be %r (got %r)" % (SCHEMA, doc.get("schema")))

    key = doc.get("key")
    if not key or not KEY_RE.fullmatch(str(key)):
        errs.append("key %r must match %s" % (key, KEY_RE.pattern))
    elif key != dirkey:
        errs.append("key %r must equal its directory name %r" % (key, dirkey))

    status = doc.get("status", "active")
    if status not in ("active", "disabled"):
        errs.append("status %r must be active|disabled" % (status,))

    unit = doc.get("unit") or {}
    kind = unit.get("kind")
    if kind not in KINDS:
        errs.append("unit.kind %r must be one of %s" % (kind, list(KINDS)))

    if kind in INSTALLABLE:
        if not unit.get("collection") or not unit.get("name"):
            errs.append("unit.collection + unit.name are required for kind %r" % (kind,))
        install = doc.get("install") or {}
        colls = install.get("collections") or []
        if not colls:
            errs.append("install.collections is required for kind %r" % (kind,))
        for c in colls:
            v = (c or {}).get("version")
            if not (isinstance(v, str) and v.strip().startswith("==")):
                errs.append("install.collections[%s].version must be an EXACT '==' pin (got %r)"
                            % ((c or {}).get("name"), v))
            elif not PIN_VERSION_RE.fullmatch(v.strip()[2:].strip()):
                errs.append("install.collections[%s].version must be '==' plus a plain version (letters, digits, "
                            "`.`, `+`, `-`)" % ((c or {}).get("name"),))
        prov = install.get("provenance") or {}
        if prov.get("source") is not None and prov["source"] not in SOURCES:
            errs.append("install.provenance.source %r must be one of %s" % (prov["source"], list(SOURCES)))
        if prov.get("source") == "git":
            errs.append("install.provenance.source 'git' is not supported in the MVP (galaxy only) — fail closed")
        if prov.get("signature") is not None and prov["signature"] not in SIGNATURES:
            errs.append("install.provenance.signature %r must be one of %s" % (prov["signature"], list(SIGNATURES)))
        # keyring + sha256 are OPTIONAL; ABSENCE is never an error (NB-1). Validate SHAPE only when present.
        kr = prov.get("keyring")
        if kr is not None and not isinstance(kr, str):
            errs.append("install.provenance.keyring must be a host path string (or absent)")
        sha = prov.get("sha256")
        if sha is not None and not (isinstance(sha, str) and SHA256_RE.fullmatch(sha)):
            errs.append("install.provenance.sha256 must be a 64-hex digest (or null/absent)")
        # Tier-cap (NB-4): a DOWNLOADED unit with unverified provenance may not reach the highest-blast tier. Fails
        # on the COMBINATION (high-blast target + signature != required), NEVER on plain absence — and only for
        # installable (downloaded) kinds; an in-repo `standalone` play carries no external provenance to verify, and
        # the module-derived FLEET collections never reach this validator (they are not actuation units).
        sig_eff = prov.get("signature") or "adaptive"
        ig = (doc.get("target") or {}).get("inventory_group")
        if ig in HIGH_BLAST_GROUPS and sig_eff != "required":
            errs.append("a unit targeting inventory_group %r must set install.provenance.signature: required — "
                        "unverified provenance (%s) may not reach the highest-blast tier (fail closed)" % (ig, sig_eff))
    elif kind == "standalone":
        if not unit.get("repo_playbook"):
            errs.append("unit.repo_playbook is required for kind standalone (the in-repo play to run)")
        if doc.get("install"):
            errs.append("kind standalone must carry NO install: block (nothing to install)")

    target = doc.get("target") or {}
    dc = target.get("device_class")
    if not dc:
        errs.append("target.device_class is required")
    elif enabled_modules is not None and dc not in enabled_modules:
        errs.append("target.device_class %r is not an enabled module (instance/fleet.yml) — fail closed" % (dc,))
    br = target.get("blast_radius")
    if br is not None and br not in BLAST:
        errs.append("target.blast_radius %r must be one of %s" % (br, list(BLAST)))

    errs += validate_knobs(doc.get("knobs"))   # R3: the optional curated configure-form knob block
    return errs
