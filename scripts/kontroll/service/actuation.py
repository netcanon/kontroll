"""actuation — the app-store unit PREVIEW (R4): render what a configured unit WOULD run, WRITE-FREE.

`build_actuation_plan` is the PURE read behind the Review stage (design 22 §3.1 `build_plan` half / report 24 §2
Stage ⑤): it loads the actuation unit, re-validates the operator's submitted configure values (the P0a guard,
reusing `units.validate_unit_config` — fail-closed), and renders the WOULD-RUN play — the role→play WRAPPER (with
the derived access-chain header, design 22 §2.3/§2.4) for a `role` unit, or the FQCN / standalone run-spec for a
directly-runnable unit — plus the resolved (non-secret) vars and the `--check`-first hand-off + the post-promote
enact steps.

It WRITES NOTHING and stages nothing (pinned `assert_read_only`; the #134 completeness gate sees no write verb).
The staged write — `apply_actuation_plan`, the generated-artifact change-set, the first write verb staging
`proposed/<run_id>` — is R5 (the security-critical rung). Mirrors `observe.py`/`identity.py`'s `build_plan` shape.
Secret-typed actuation vars are forbidden at the descriptor layer (`gen-actuation`; SEC-2), so the preview never
carries a credential — the resolved vars are non-secret config only.
"""
import logging
import os
import re

import yaml

from kontroll import catalog, paths
from kontroll.yamlscalar import word as _yw       # one YAML scalar on one line: bare when plain, else quoted
from kontroll.service import _actuation_schema as _schema
from kontroll.service import promote, units

log = logging.getLogger("kontroll.service.actuation")

# A valid actuation registry key — the SAME contract gen-actuation enforces (key matches this AND == its dir name).
# build_actuation_plan rejects a key that doesn't match BEFORE it builds any path, so a crafted route {key}
# (`../`, absolute, slashes, NUL) can never reach the path-builder (_vars_rel) — the SEC-3 analog for the WRITE
# path (R5 review MF-1: the escape is closed by CONSTRUCTION here, not only by the catalog-lookup ordering).
_KEY_RE = re.compile(r"^[a-z][a-z0-9-]*\Z")


def _yv(value):
    """Render one configure value as a YAML scalar for the PREVIEW (display only — nothing runs): a bool as
    lowercase true/false, a string quoted, a number bare. Keeps the rendered play readable + copy-pasteable."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value) if isinstance(value, str) else str(value)


def _access_chain_header(unit, target):
    """Derive the access-chain comment header for the would-run play from the unit's blast_radius + device class
    (design 22 §2.4 — a GENERATED header, never hand-authored; CLAUDE.md access-chain discipline). MVP derives the
    blast + a class-named fallback placeholder; the full per-class access-chain is report 23's territory."""
    u = unit.get("unit") or {}
    grp = target.get("inventory_group") or "all"
    dc = target.get("device_class") or "?"
    blast = target.get("blast_radius") or "this-device"
    runs = ("%s.%s" % (u.get("collection"), u.get("name"))) if u.get("collection") else (u.get("repo_playbook") or "?")
    # every value is read back from a unit descriptor or a module file — rendered as ONE word each (_yw), so none
    # can end a comment line and continue the preview with a line of its own
    grp, dc, runs, blast = _yw(grp), _yw(dc), _yw(runs), _yw(blast)
    return ("# Access chain used:   Semaphore runner -> %s (group %s) via the %s device-class backend.\n"
            "# May break:           runs %s against %s.\n"
            "# Fallback required:   per the %s device-class access-chain.\n"
            "# Blast radius:        %s." % (grp, grp, dc, runs, grp, dc, blast))


def _wrapper_play(unit, target, values):
    """Render the role→play WRAPPER preview (kind: role) — a role can't run without hosts:+roles: (design 22
    §2.3). Pure-data, one dispatch seam (hosts = the inventory_group), `ignore_unreachable` so a down host never
    fails the fleet run (CLAUDE.md). The curated non-secret values render as a vars: block (no secret literal —
    SEC-2)."""
    u = unit.get("unit") or {}
    grp = target.get("inventory_group") or "all"
    fqcn = "%s.%s" % (u.get("collection"), u.get("name"))
    lines = ["---",
             "- name: %s" % _yw("App-store unit %s (%s on %s)" % (unit.get("key"), fqcn, grp)),
             "  hosts: %s" % _yw(grp),
             "  gather_facts: false",
             "  ignore_unreachable: true"]
    if values:
        lines.append("  vars:")
        lines += ["    %s: %s" % (k, _yv(values[k])) for k in sorted(values)]
    lines += ["  roles:", "    - role: %s" % _yw(fqcn)]
    return "\n".join(lines)


def _run_spec(unit, target, values):
    """Render the run-spec preview for a directly-runnable unit (kind: collection_playbook → the FQCN; standalone
    → the in-repo playbook path) — no wrapper needed (design 22 §2.2). Shows the playbook ref + the limit group +
    the resolved extra-vars Semaphore would pass."""
    u = unit.get("unit") or {}
    ref = ("%s.%s" % (u.get("collection"), u.get("name"))) if u.get("kind") == "collection_playbook" \
        else (u.get("repo_playbook") or "?")
    grp = target.get("inventory_group") or "all"
    lines = ["playbook: %s" % _yw(ref), "limit: %s" % _yw(grp)]
    if values:
        lines.append("extra_vars:")
        lines += ["  %s: %s" % (k, _yv(values[k])) for k in sorted(values)]
    return "\n".join(lines)


def render_play(unit, values):
    """The would-run play preview for any unit kind = the derived access-chain header + (the role wrapper | the
    run-spec). PURE string render — this is what the operator reviews before authorizing a stage (design 24 §5)."""
    target = unit.get("target") or {}
    body = _wrapper_play(unit, target, values) if (unit.get("unit") or {}).get("kind") == "role" \
        else _run_spec(unit, target, values)
    return _access_chain_header(unit, target) + "\n" + body


def actuation_enact_commands(key):
    """The post-promote hand-off the operator runs to make a staged unit live — the API runs NONE (design 22
    §3.5; read-only-by-construction). promote (FF, the trusted second key) → configure-semaphore re-run → the
    Dry Run (--check --diff) FIRST → the real apply. Each step is `{kind, ...}`; preview-generic (R5 mints the
    run_id when it actually stages)."""
    return [
        {"kind": "semaphore", "task": "promote-proposal",
         "why": "FF-promote proposed/<run_id> to main (the trusted second key)"},
        {"kind": "operator", "cmd": "python3 scripts/configure-semaphore.py",
         "why": "register the generated template (operator-run post-promote — never an API action)"},
        {"kind": "semaphore", "task": "act-%s (Dry Run)" % key,
         "why": "the --check --diff dry-run FIRST — review the diff before applying"},
        {"kind": "semaphore", "task": "act-%s" % key,
         "why": "the real apply, after the check-first diff is reviewed"},
    ]


def build_actuation_plan(key, values, unit=None):
    """Compute the PREVIEW plan for actuation unit `key` with the submitted `values` — the write-free Review-stage
    read (design 24 §5). PURE: loads the unit + re-validates the values (the P0a guard, `units.validate_unit_config`),
    then renders the would-run play. `unit` is the I/O seam tests inject; it defaults to `catalog.actuation_unit(key)`.

    Returns `{"error": None, key, unit, target, play, vars, check_first, enact}` on success, `{"error": "no_unit"}`
    when the key is absent (→ 404), or `{"error": "invalid", errors}` when the values don't validate (→ 422).
    WRITES NOTHING (pinned `assert_read_only`): the staged write + the generated change-set are R5. No `vars`
    value is ever a secret (secret-typed actuation vars are forbidden at the descriptor layer; SEC-2)."""
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):   # fullmatch: `$` also matches before a final \n
        return {"error": "no_unit", "key": key}     # SEC-3 analog (write path): a malformed key matches no unit
    unit = unit if unit is not None else catalog.actuation_unit(key)   # AND never reaches _vars_rel (no traversal)
    if not unit:
        return {"error": "no_unit", "key": key}
    verdict = units.validate_unit_config(key, values, unit=unit)
    if verdict.get("error") == "no_unit":
        return {"error": "no_unit", "key": key}
    if not verdict["ok"]:
        return {"error": "invalid", "key": key, "errors": verdict["errors"]}
    u = unit.get("unit") or {}
    target = unit.get("target") or {}
    vars_rel = _vars_rel(key)
    vars_content = _render_vars_file(key, values or {})
    return {"error": None, "key": key,
            "unit": {"name": u.get("name") or u.get("repo_playbook"), "kind": u.get("kind"),
                     "collection": u.get("collection")},
            "target": {"device_class": target.get("device_class"),
                       "inventory_group": target.get("inventory_group"),
                       "blast_radius": target.get("blast_radius")},
            "play": render_play(unit, values or {}),
            "vars": values or {},
            "check_first": "the first run is a --check --diff dry-run — review the diff in Semaphore before the "
                           "apply run (CLAUDE.md; especially edge_firewall/core_switch)",
            "enact": actuation_enact_commands(key),
            # --- the staging metadata (R5): the ONE artifact this proposal stages + the anti-drift token. PURE —
            # build RENDERS the content (a string), it never writes; apply_actuation_plan is the write verb. ---
            "vars_rel": vars_rel, "vars_content": vars_content, "paths": [vars_rel],
            "token_parts": [vars_content], "plan_token": promote.plan_token(vars_content)}


# ── R5: the FIRST app-store write verb — stage a configured unit's values as a proposal ──────────────────────────
def _vars_rel(key):
    """The repo-RELATIVE path of unit `key`'s committed configure-values file — the instance-overlay write target
    (`instance/actuation/<key>/vars.yml` when the overlay is active). `overlay_target` so the write path and the
    git-add/staged path AGREE (the C10 staging invariant, paths.py). PURE."""
    return paths.overlay_target("actuation/%s/vars.yml" % key)


def _render_vars_file(key, values):
    """Render unit `key`'s committed configure-values file (PURE) — a GENERATED do-not-hand-edit header + the
    validated values as YAML. This is the `-e @instance/actuation/<key>/vars.yml` file the unit's run consumes
    (report 22 §6 MVP). NO secret rides here (secret actuation vars are forbidden — SEC-2), so the staged,
    human-reviewed proposal never carries a credential. Deterministic (sorted keys) so a re-stage of identical
    values is a no-op (apply is idempotent)."""
    body = yaml.safe_dump(values or {}, default_flow_style=False, sort_keys=True)
    return ("# GENERATED by the kontroll app-store configure flow for actuation unit %s.\n"
            "# Do NOT hand-edit — re-configure the unit and re-stage. Non-secret values only (SEC-2).\n"
            "---\n%s" % (key, body))


def apply_actuation_plan(plan):
    """WRITE unit `key`'s configure-values file (the ONE staged artifact, R5 MVP) — the FIRST app-store write
    verb. Writes `plan["vars_rel"]` (the overlay target) from the plan's rendered `vars_content`. IDEMPOTENT:
    identical on-disk content → no write (`changed: False`). Returns `{"changed", "paths"}` — the repo-rel paths
    the ROUTE's scoped `commit_and_push(run_id=…)` stages to `proposed/<run_id>` (this writes ONLY the working
    tree; the route owns run_id + the push). REGISTERED in `WRITE_VERBS` (tests/unit/_readonly_pins.py) so
    `assert_read_only` + the #134 completeness gate stay exhaustive — a read view that calls this trips the pin."""
    rel = plan["vars_rel"]
    abspath = paths.confined(rel)                          # never outside write_root() (finding 1)
    content = plan["vars_content"]
    changed = True
    if os.path.exists(abspath):
        with open(abspath, encoding="utf-8") as fh:
            changed = fh.read() != content
    if changed:
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "w", encoding="utf-8", newline="\n") as out:
            out.write(content)
        log.info("actuation: wrote configure values for %s (path %s)", plan["key"], rel)
    return {"changed": changed, "paths": [rel]}


def stage_plan(key, values, token):
    """RECOMPUTE the plan, refuse ('drift') unless `token` still matches it (the propose→promote anti-drift gate
    — the values may have moved under the operator), then WRITE via `apply_actuation_plan`. Returns
    `{"error": 'no_unit'|'invalid'|'drift'|None, plan?, changed?, paths?}`. The ROUTE mints run_id, audits, and
    stages the returned `paths` via `commit_and_push(run_id=…)` — this does the recompute + gate + write, NEVER
    the push (mirrors `capability.promote`). Not read-only (it calls the write verb); not itself a direct mutator
    (it delegates), so it is an orchestrator, not a `WRITE_VERBS` member."""
    plan = build_actuation_plan(key, values)
    if plan.get("error"):
        return plan
    if not promote.verify_token(token, *plan.get("token_parts", [])):
        log.info("actuation: stage refused for %s — plan drifted since propose", key)
        return {"error": "drift", "key": key}
    result = apply_actuation_plan(plan)
    return {"error": None, "plan": plan, **result}


# ── CREATE-UNIT: author an actuation unit descriptor from a searched collection (the app-store blocker) ───────────
# The R3/R4/R5 stages all read an EXISTING instance/actuation/<key>/unit.yml; nothing AUTHORED that descriptor —
# the registry was empty. build_create_unit_plan RENDERS one from {collection, kind, name, version, blast_radius}
# (the search + R1 unit-enumeration outputs), validates it through the SAME fail-closed schema gen-actuation
# enforces (validate-before-stage), and stages it on the SAME C10 two-key spine R5 uses. Zero-knob MVP: the
# rendered descriptor carries no `knobs:` block (the worked-extraction mandate blesses "a unit may ship with zero
# knobs"); knob curation is a later additive surface. Design of record: docs/reviews/2026-06-25-create-unit-stage/.

# A bare collection name (`namespace.name`, lowercase — paths.collection_fqcn) and a runnable-unit name (an ansible
# playbook/role stem — _schema.UNIT_NAME_RE, the descriptor's own rule: what this route accepts is exactly what the
# schema accepts). Shape-checked so a crafted value can never corrupt the rendered YAML / the derived key.
# "create" is reserved: the WRITE route is POST /actuation/create (a static path declared before /{key}), so a
# unit may not be keyed "create" or the stage route's {key} could never address it. create-derivation never mints
# it (a key is collection+name slug, which always carries a dash), but the guard closes the loop.
_RESERVED_KEYS = frozenset({"create"})


def _enabled_modules():
    """The fleet's enabled module list (instance/fleet.yml `enabled_modules`), or [] — the target-enabled check
    `_schema.validate_unit` needs. The injectable default for build_create_unit_plan; a degraded read yields []
    (so a unit whose class isn't resolvable as enabled fails closed, never silently passes). READ-only."""
    try:
        with open(paths.resolve("config/fleet.yml"), encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("enabled_modules") or []
    except (OSError, yaml.YAMLError):
        return []


def _derive_key(collection, name):
    """The actuation registry key for a created unit: a deterministic `<collection>-<name>` slug, lowercased, every
    run of non-`[a-z0-9]` collapsed to a single `-`, stripped — so `cisco.ios` + `ios_config` → `cisco-ios-ios-
    config`, a valid `^[a-z][a-z0-9-]*$` key that == its directory name. PURE (the key IS the dir; same inputs →
    same key → an idempotent re-create lands on the same path)."""
    slug = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (collection, name)).lower()).strip("-")
    return slug


def _normalize_pin(version):
    """Turn an operator-supplied version into an EXACT `==` pin, or (None, error). Accepts a bare version
    (`8.0.4`) or an already-`==`-prefixed one; REJECTS a range/operator (`>=`, `~`, `*`, …) — an app-store install
    pins ONE reviewed version, never a floor (the supply-chain invariant gen-actuation also enforces). PURE."""
    v = str(version or "").strip()
    if v.startswith("=="):
        v = v[2:].strip()
    if not v:
        return None, "version is required (the exact reviewed version to pin)"
    if any(c in v for c in "<>=!~*^ "):
        return None, "version must be an EXACT version, not a range/operator (an app-store install pins one " \
                     "reviewed version)"
    if not _schema.PIN_VERSION_RE.fullmatch(v):
        # A CLOSED charset, not a list of forbidden operators: the pin is written into the unit descriptor and
        # later hand-formatted, inside double quotes, into the collection lockfile — a quote or a newline there
        # made that file unparseable, or parse to something else.
        return None, "version must be a plain version string (letters, digits, `.`, `+`, `-`)"
    return "==" + v, None


def _unit_rel(key):
    """The repo-RELATIVE path of unit `key`'s DESCRIPTOR file — the instance-overlay write target
    (`instance/actuation/<key>/unit.yml` when the overlay is active). `overlay_target` so the write path and the
    git-add/staged path AGREE (the C10 staging invariant, paths.py). PURE."""
    return paths.overlay_target("actuation/%s/unit.yml" % paths.component(key, "unit key"))


def _render_unit_file(doc):
    """Render a created unit's `unit.yml` (PURE) — a GENERATED do-not-hand-edit header + the descriptor as YAML in
    a fixed field order (sort_keys=False) so a re-create of identical inputs is byte-identical (the anti-drift token
    + the idempotent apply rely on it). The run_id that stages this rides the staging COMMIT (audit correlation),
    not the descriptor — keeping the content a pure function of the operator's inputs so propose's token verifies at
    stage. No secret rides here (the descriptor carries no value; SEC-2)."""
    body = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)
    return ("# GENERATED by the kontroll app-store create-unit flow.\n"
            "# Do NOT hand-edit the install pin / target — re-create via the app-store to change them (curated\n"
            "# `knobs:` may be added by hand). The staging commit carries the kontroll_run_id (audit correlation).\n"
            "---\n%s" % body)


def build_create_unit_plan(inputs, module=None, enabled_modules=None):
    """Compute the PREVIEW plan for AUTHORING an actuation unit from a searched collection — the write-free propose
    behind the app-store create stage. PURE: derives the key, resolves the target device-class from the declaring
    module, enforces the create-time Tier-cap + collision guards, RENDERS the `unit.yml`, and validates it through
    `_schema.validate_unit` (validate-before-stage — a malformed descriptor never stages / never unions a pin).
    `inputs` is `{collection, kind, name, version, blast_radius}`; `module`/`enabled_modules` are the I/O seams
    tests inject (default: `catalog.module_for_collection` + `_enabled_modules`).

    Returns `{"error": None, key, device_class, inventory_group, …, unit_rel, unit_content, paths, plan_token}` on
    success, or `{"error": <code>, …}` — `invalid` (bad inputs / the rendered descriptor fails schema; → 422),
    `no_module` (no enabled-or-shipped class declares the collection; → 422), `not_enabled` (the class exists but
    isn't in the fleet; → 409), `tier_cap` (the class targets edge_firewall/core_switch, which an unsigned
    app-store install may not reach — fail closed; → 403), or `exists` (a unit with that key already exists; →
    409, never clobber). WRITES NOTHING (pinned `assert_read_only`): the write is `apply_create_unit` (R5 spine)."""
    inputs = inputs or {}
    collection = inputs.get("collection")
    kind = inputs.get("kind")
    name = inputs.get("name")
    blast = inputs.get("blast_radius")

    # 1. shape-check the operator inputs (fail closed with a per-field message; nothing reaches the path-builder yet).
    errs = {}
    try:
        paths.collection_fqcn(collection)                # exactly namespace.name — the request-boundary validator
    except paths.RequestBoundaryError:
        errs["collection"] = "collection must be a 'namespace.name' galaxy collection"
    if kind not in _schema.INSTALLABLE:
        errs["kind"] = "kind must be one of %s (a runnable collection unit)" % list(_schema.INSTALLABLE)
    if not isinstance(name, str) or not _schema.UNIT_NAME_RE.fullmatch(name):
        errs["name"] = "name must be a runnable-unit name (lowercase letters, digits and `_`; at most 64 characters)"
    if blast not in _schema.BLAST:
        errs["blast_radius"] = "blast_radius must be one of %s" % list(_schema.BLAST)
    pin, perr = _normalize_pin(inputs.get("version"))
    if perr:
        errs["version"] = perr
    if errs:
        return {"error": "invalid", "errors": errs}

    # 2. derive + guard the key BEFORE any path is built (SEC-3 analog; the key IS the on-disk dir name).
    # The key is also ONE PATH COMPONENT (paths.component: at most 64 characters) — a collection and a name that
    # are each valid can be too long together, and the path builder below would raise for it as a field nobody sent.
    key = _derive_key(collection, name)
    if not _schema.KEY_RE.fullmatch(key) or not paths.is_component(key) or key in _RESERVED_KEYS:
        return {"error": "invalid", "errors": {"key": "could not derive a valid registry key from the inputs (the "
                                               "key built from the collection and the name is one path "
                                               "component: at most 64 characters — a unit with a longer pair "
                                               "is written by hand, under a key of its own)"}}

    # 3. resolve the target device-class from the module that DECLARES the collection (the one dispatch seam).
    module = module if module is not None else catalog.module_for_collection(collection)
    if not module:
        return {"error": "no_module", "key": key, "collection": collection}
    device_class = module.get("key")
    inventory_group = module.get("inventory_group")
    enabled = enabled_modules if enabled_modules is not None else _enabled_modules()
    if device_class not in enabled:
        return {"error": "not_enabled", "key": key, "device_class": device_class}

    # 4. the Tier-cap, AT CREATE (NB-4 + blast-radius): an app-store install pins an unsigned public-Galaxy
    # collection (signature: adaptive), which may NOT reach the highest-blast tier. Refuse here with a pointed
    # message; `_schema.validate_unit` is the construction-level backstop (it rejects adaptive-on-high-blast too).
    if inventory_group in _schema.HIGH_BLAST_GROUPS:
        return {"error": "tier_cap", "key": key, "device_class": device_class, "inventory_group": inventory_group}

    # 5. collision: never overwrite an existing unit (the #124 reuse-don't-clobber lesson) — a re-create of the
    # same collection/name lands on the same key, so an operator must disable/remove the old one deliberately.
    unit_rel = _unit_rel(key)
    if os.path.exists(os.path.join(paths.write_root(), unit_rel)):
        return {"error": "exists", "key": key, "unit_rel": unit_rel}

    # 6. render the descriptor + validate it through the SAME fail-closed schema (validate-before-stage).
    doc = {
        "schema": _schema.SCHEMA, "key": key, "status": "active",
        "unit": {"kind": kind, "collection": collection, "name": name},
        "install": {"collections": [{"name": collection, "version": pin}],
                    "provenance": {"source": "galaxy", "signature": "adaptive"}},
        "target": {"device_class": device_class, "inventory_group": inventory_group, "blast_radius": blast},
    }
    serrs = _schema.validate_unit(doc, key, enabled)
    if serrs:
        return {"error": "invalid", "key": key, "errors": {"descriptor": serrs}}

    content = _render_unit_file(doc)
    return {"error": None, "key": key, "device_class": device_class, "inventory_group": inventory_group,
            "blast_radius": blast, "collection": collection, "kind": kind, "name": name, "version": pin,
            "unit_rel": unit_rel, "unit_content": content, "paths": [unit_rel],
            "token_parts": [content], "plan_token": promote.plan_token(content)}


def apply_create_unit(plan):
    """WRITE a created unit's descriptor (`instance/actuation/<key>/unit.yml`) from the plan's rendered
    `unit_content` — the app-store CREATE write verb. IDEMPOTENT: identical on-disk content → no write
    (`changed: False`). Returns `{"changed", "paths"}` — the repo-rel paths the ROUTE's scoped
    `commit_and_push(run_id=…)` stages to `proposed/<run_id>` (this writes ONLY the working tree; the route owns
    run_id + the push). REGISTERED in `WRITE_VERBS` (tests/unit/_readonly_pins.py) so `assert_read_only` + the #134
    completeness gate stay exhaustive — a read view that calls this trips the pin. The build's collision guard
    refuses an EXISTING key, so this only ever creates a new descriptor (never silently overwrites a configured
    unit)."""
    rel = plan["unit_rel"]
    abspath = paths.confined(rel)                          # never outside write_root() (finding 1)
    content = plan["unit_content"]
    changed = True
    if os.path.exists(abspath):
        with open(abspath, encoding="utf-8") as fh:
            changed = fh.read() != content
    if changed:
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "w", encoding="utf-8", newline="\n") as out:
            out.write(content)
        log.info("actuation: created unit descriptor for %s (path %s)", plan["key"], rel)
    return {"changed": changed, "paths": [rel]}


def stage_create_unit(inputs, token):
    """RECOMPUTE the create plan, refuse ('drift') unless `token` still matches it (the propose→stage anti-drift
    gate), then WRITE via `apply_create_unit`. Returns `{"error": <code>|None, plan?, changed?, paths?}`. The ROUTE
    mints run_id, audits, and stages the returned `paths` via `commit_and_push(run_id=…)` — this does the recompute
    + gate + write, NEVER the push (mirrors `stage_plan`). An orchestrator (it delegates the write), registered in
    `WRITE_VERBS` for the read-only-by-delegation defense (R5 review GAP-3)."""
    plan = build_create_unit_plan(inputs)
    if plan.get("error"):
        return plan
    if not promote.verify_token(token, *plan.get("token_parts", [])):
        log.info("actuation: create-unit stage refused for %s — plan drifted since propose", plan.get("key"))
        return {"error": "drift", "key": plan.get("key")}
    result = apply_create_unit(plan)
    return {"error": None, "plan": plan, **result}
