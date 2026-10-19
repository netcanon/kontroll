"""identity — the module-IDENTITY reconfigure instance (Phase 4b, #139).

A safe, diff-able change to a device-class's top-level IDENTITY keys (secrets_domain / inventory_group / backend /
role) in `modules/<key>/module.yml`. Before 4b these had NO surgical seam — changing one fell off into a
whole-file Overwrite (design `docs/reviews/2026-06-19-gui-paradigm/22-design-reconfigure-overwrite-rework.md`
§5.4, G7). This rides the SAME propose→token→promote→stage spine as the capability reconfigure (Phase 4a): a pure
`current_values` read-back → `_diff.compute_changes` (with the `module-identity` knob_meta that UPGRADES a value
change to the `identity` gate) → a comment-preserving `upsert_top_level_key` write → `verify_no_drop` + the human
type-to-confirm + the C10 two-key promote.

Heaviest-blast reconfigure short of platform settings: changing `inventory_group` RE-HOMES every host of the class
at the one dispatch seam; changing `secrets_domain` ORPHANS its credentials. So each identity change carries
`severity: identity` (the type-to-confirm `reconfig-identity-confirm`), and a proposal may change AT MOST ONE
identity-severity key at a time (so the type-to-confirm value is unambiguous). The staged proposal carries
`module.yml` only — the operator REGENERATES the scrape/log targets +
re-runs bootstrap/deploy-stack at enact (the identity ripple is named in the enact hint), keeping the blast of a
single GUI action bounded (MVP scope; M6 single-object). PURE propose; the write is the operator-promoted stage.
"""
import logging
import os

import yaml

from kontroll import catalog, paths
from kontroll.service import _diff, promote
from kontroll.service._blockwrite import upsert_top_level_key, write_module
from kontroll.service._validate import validate_value

log = logging.getLogger("kontroll.service.identity")

_AREA = "module-identity"


def _knobs():
    """The declared identity-knob descriptors (settings/module-identity.yml `knobs`), or [] if unregistered.
    The single source of which top-level keys are reconfigurable identity + each one's type/severity/blast."""
    doc = next((a for a in catalog.load_knob_descriptors() if a.get("area") == _AREA), None)
    return (doc.get("knobs") or []) if doc else []


def _mod_path(key):
    """Where the class is READ from: the clone's copy, or the baked one when the clone has none. Never a write
    target — a plan's `mod_path` is paths.module_write_path(key)."""
    return paths.module_file(key)


def current_values(key):
    """The DECLARED identity values of class `key` — a FLAT `{identity_key: value}` over the reconfigurable keys
    PRESENT in modules/<key>/module.yml (for pre-filling the renderer + the diff). PURE — reads through
    paths.module_file, writes nothing; {} if not onboarded / unreadable (degrade, never raise — service-never-sys.exit). The
    reconfigure read-back (design 22 §3.3); these are non-secret config keys, so the value (never a secret) is
    surfaced."""
    try:
        with open(_mod_path(key), encoding="utf-8") as fh:
            module = yaml.safe_load(fh.read()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    declared = [k["key"] for k in _knobs() if k.get("key")]
    return {k: module[k] for k in declared if k in module and not isinstance(module[k], (dict, list))}


def build_plan(key, selection):
    """Compute the identity reconfigure PLAN for class `key`. `selection` = `{"values": {identity_key: value}}`
    (the renderer submits the full identity knob set, pre-filled from `current_values`). PURE — reads
    modules/<key>/module.yml, writes nothing. Returns
    {"error": "not_onboarded"|"unknown_field"|"bad_value"|"multi_identity"|None, ...}; on success the plan
    carries the comment-preserving `text_after`, the field-level `changes`/`will_overwrite`/`severity` (each
    identity change UPGRADED via the descriptor knob_meta), the `current`/`proposed` structs `verify_no_drop`
    checks, the enact hand-off, and `token_parts`/`plan_token` (text_after + the severity decision — anti-drift +
    no-quiet-upgrade, M1). At most ONE identity-severity change per proposal (so the type-to-confirm is
    unambiguous)."""
    # Where the class is READ from (the clone's copy, or the baked one). The plan's "mod_path", below, is the
    # WRITE target, and is not this path for a shipped class the clone does not hold yet.
    mod_path = _mod_path(key)
    if not os.path.exists(mod_path):
        return {"error": "not_onboarded", "key": key}
    with open(mod_path, encoding="utf-8") as fh:             # read ONCE — the diff, text_before, and the no-drop
        text_before = fh.read()                              # full-struct all derive from these bytes (review NIT 3)
    module = yaml.safe_load(text_before)
    if module is None:
        module = {}                                          # an empty file is a class with no keys yet
    if not isinstance(module, dict):
        # a file that parses to a list or a bare word is neither absent nor a class: answered as the file it is
        # (the onboard's reader does the same), not as the AttributeError its first `.get` would be
        raise paths.RepositoryDataError(
            "module file", "the %s device class's module file is not a mapping — correct modules/%s/module.yml"
            % (key, key))
    knobs = {k["key"]: k for k in _knobs() if k.get("key")}
    declared = [k["key"] for k in _knobs() if k.get("key")]
    submitted = (selection or {}).get("values") or {}
    proposed = {}
    for field, value in submitted.items():
        knob = knobs.get(field)
        if knob is None:
            return {"error": "unknown_field", "key": key, "field": field}
        if isinstance(value, str) and module.get(field) == value:
            # READ BACK, not chosen: the dialog submits every knob pre-filled with what the file holds, nothing is
            # written for a value that did not change, and so it is not judged. Without this a class whose file
            # holds a name the knob would not accept today (a hyphen from the onboard field, a name longer than
            # the bound, a reserved group) could have none of its OTHER keys edited.
            proposed[field] = value
            continue
        bad = validate_value(dict(knob, key=field), value)   # the P0a config-injection guard, server-side
        if bad:
            return {"error": "bad_value", "key": key, "field": field, "detail": bad}
        if field == "inventory_group" and value in paths.RESERVED_NAMES:
            # the same deny-list the onboard field gets (paths.inventory_group): this knob re-homes a class, and
            # a class on `all` addresses every host wherever its group is a play's pattern
            return {"error": "bad_value", "key": key, "field": field,
                    "detail": "inventory_group is a name Ansible reserves (all, ungrouped, localhost)"}
        proposed[field] = value

    # The renderer submits the full declared set; restrict the editable `current` to the submitted keys so an
    # UNSUBMITTED key is not seen as a `remove` (we only diff what the dialog offered).
    current_edit = {k: module[k] for k in declared
                    if k in module and k in proposed and not isinstance(module[k], (dict, list))}
    diff = _diff.compute_changes(current_edit, proposed, catalog.knob_meta(_AREA))
    identity_changes = [c for c in diff["changes"] if _diff.effective_severity(c) == "identity"]
    if len(identity_changes) > 1:
        return {"error": "multi_identity", "key": key,
                "detail": "change identity keys one at a time (each re-homes/re-classifies the class); "
                          "fields: %s" % ", ".join(sorted(c["path"] for c in identity_changes))}

    text_after = text_before
    try:
        for c in diff["changes"]:                            # fold each changed key (≤1 identity)
            if c["kind"] in ("add", "modify"):
                text_after = upsert_top_level_key(text_after, c["path"], c["after"])
    except ValueError:
        # the writer's own refusal (a value that would not read back from YAML as itself) — the knob validators
        # above refuse such a value first; this keeps a looser future knob from surfacing as an uncaught error
        return {"error": "bad_value", "key": key, "detail": "an identity value cannot be written as plain YAML"}

    mod_rel = "modules/%s/module.yml" % key
    sev_dec = _diff.severity_decision(diff)
    # verify_no_drop runs over the FULL module top-level keys (not just the edited identity keys) — so a future
    # writer bug dropping a co-owner key (collections / metrics / a sibling identity key) fails closed at promote.
    # upsert_top_level_key only replaces/appends (never removes), so a legit edit keeps every key → passes; the
    # operator-facing diff stays the editable change-set (review SHOULD-FIX 1).
    after_module = yaml.safe_load(text_after) or {}
    # the WRITE target is always under write_root(): the read path above is the BAKED copy for a shipped class the
    # clone does not hold yet, and that tree is read-only (and is not where a proposal is staged from)
    return {"error": None, "key": key, "mod_rel": mod_rel, "mod_path": paths.module_write_path(key),
            "text_before": text_before, "text_after": text_after,
            "changes": diff["changes"], "will_overwrite": diff["will_overwrite"], "severity": diff["severity"],
            "current": module, "proposed": after_module,     # FULL top-level keys — what verify_no_drop checks
            "paths": [mod_rel],                              # MVP: module.yml only; operator regens targets at enact
            "enact": identity_enact_commands(key, diff["changes"]),
            "token_parts": [text_after, sev_dec],
            "plan_token": promote.plan_token(text_after, sev_dec)}


def apply_plan(plan):
    """Write the reconfigured identity keys into modules/<key>/module.yml (comment-preserving line-surgery).
    Idempotent: a plan that changes nothing, or a target that already holds the text, is no write
    (_blockwrite.write_module). Returns {"changed", "paths"} — the repo-rel paths the route's scoped commit stages
    (module.yml only; the operator regenerates targets at enact). The API runs no play."""
    changed = write_module(plan)
    if changed:
        log.info("identity: wrote identity keys for %s (paths %s)",
                 plan["key"], ",".join(c["path"] for c in plan["changes"]))
    return {"changed": changed, "paths": [plan["mod_rel"]]}


# The directories gen-observability.py and gen-logging.py write (tests/unit/test_identity.py reads the generators'
# own constants and fails if these drift).
_REGEN_TREES = ("prometheus/targets", "docker/vector/generated", "docker/loki/overrides")


def identity_enact_commands(key, changes):
    """The hand-off steps to make a promoted identity change LIVE — the API runs none. An identity change ripples
    to the dispatch seam + the generators, so the enact NAMES the re-gen + (when the dispatch identity moved) the
    bootstrap dry-run. Each step is `{kind, cmd, why}` (kind 'operator' — needs the control node)."""
    cmds = []
    fields = {c["path"] for c in changes if c["kind"] in ("add", "modify")}
    if fields & {"inventory_group", "role", "backend"}:
        cmds.append({"kind": "operator",
                     "cmd": "cd ansible && ansible-playbook playbooks/bootstrap.yml -e target=%s --check --diff" % key,
                     "why": "the new identity re-homes/re-classifies %s — dry-run bootstrap (the dispatch seam now "
                            "selects a different role/collection) before applying" % key})
    # A SCOPED add — the three trees the two generators write, nothing else. The hint used to say `git add -A`:
    # a blanket add of the operator's whole worktree, typed by hand, with no gate in front of it.
    cmds.append({"kind": "operator",
                 "cmd": "python3 scripts/gen-observability.py && python3 scripts/gen-logging.py "
                        "&& git add -- %s && git commit -m 'regen targets after identity change'"
                        % " ".join(_REGEN_TREES),
                 "why": "regenerate the scrape/log targets from the updated module identity, then deploy-stack so "
                        "the change takes effect"})
    return cmds
