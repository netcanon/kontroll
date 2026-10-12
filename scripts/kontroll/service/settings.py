"""settings domain — the read-only "what is my instance configured as?" view (#135, GUI-paradigm Phase 1).

`read_view()` assembles a value-free, four-group snapshot of the platform configuration for the GUI Settings
panel: **identity** (mgmt_ip / domain / tls_mode / source_of_truth from instance.yml), **backup** (the offsite
`backup_remotes` list), **fleet** (the `enabled_modules` from fleet.yml), and **status** (whether privileged
mutation is ARMED — the GUI's own `KONTROLL_STAGE_PUSHES` env; the running control-plane services; and the
secret-domain roster by NAME with a set/unset badge). It is PURE reads — instance/instance.yml + instance/fleet.yml
+ the GUI env + config/services.yml + the secret-form descriptors — and writes NOTHING, so it never gates
onboarding (the read-only posture of fleet.list_fleet; INVARIANT D*).

This is the SURFACE half (Phase 1). It carries NO secret value (C11 — the secret roster is NAMES + set/unset
only, derived from key presence, never a decrypt) and never reads `docker/.env` (the rendered output holding
decrypted secrets — C11/C12); it reads the INPUTS only. The EDIT/GUARD write controls (mgmt_ip re-IP, tls flip,
fleet removal) are a later, C10-staged phase; this surface stages nothing. Each group degrades INDEPENDENTLY: a
missing/malformed source yields that group's `{"error": ...}` and never blanks the others, and the function never
raises out of the route (service-never-sys.exit — a crash would kill the in-process Flask worker).
"""
import logging
import os
import re

import yaml

from kontroll import catalog, paths
from kontroll.service import _diff, promote
from kontroll.service._blockwrite import disable_in_fleet, set_list_block, set_nested_scalar, upsert_top_level_key
from kontroll.service._validate import plain_scalar, validate_value
from kontroll.service.fleet import list_services
from kontroll.service.secrets import offerable_fields

log = logging.getLogger("kontroll.service.settings")

_REMOTE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*\Z")   # a single offsite-backup git-remote name (per-item validate)


def _load_yaml(rel):
    """A mapping parsed from the overlay-resolved ROOT-relative path, or {} on any failure (degrade, never raise)."""
    try:
        return yaml.safe_load(open(paths.resolve(rel), encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _identity():
    """Instance identity from instance/instance.yml — mgmt_ip / domain / tls_mode / trust_mode / source_of_truth.
    Non-secret config values, surfaced read-only. `trust_mode` (F3) is the chosen promote posture (separated|solo|
    None-if-unset); surfaced READ-ONLY here (no edit knob; this edition has no auto-promoter).
    {"error": ...} if instance.yml is unreadable."""
    inst = _load_yaml("config/instance.yml")
    if not inst:
        return {"error": "instance.yml not present or unreadable"}
    frontend = inst.get("frontend") or {}
    return {"mgmt_ip": inst.get("mgmt_ip"), "domain": inst.get("domain"),
            "tls_mode": (frontend.get("tls_mode") if isinstance(frontend, dict) else None),
            "trust_mode": inst.get("trust_mode"),
            "source_of_truth": inst.get("source_of_truth")}


def _backup():
    """The offsite backup remotes (NAMES only — each is a git-remote name, never a credential). [] if none."""
    inst = _load_yaml("config/instance.yml")
    remotes = inst.get("backup_remotes")
    return {"backup_remotes": [r for r in remotes if r] if isinstance(remotes, list) else []}


def _fleet():
    """The enabled device-class modules from instance/fleet.yml — the pick-and-choose list. [] if none."""
    fl = _load_yaml("config/fleet.yml")
    mods = fl.get("enabled_modules")
    return {"enabled_modules": [m for m in mods if m] if isinstance(mods, list) else []}


def _secret_roster():
    """The secret-domain roster: one entry per registered secret-form domain with a NAMES-only set/unset badge
    (`set` = the domain has at least one key present — derived from key NAMES, NEVER a decrypted value; C11).
    Powers the 'configure in Secrets' deep-link. [] if the registry is unreadable."""
    roster = []
    for domain in catalog.registered_secret_forms():
        view = offerable_fields(domain)
        if view.get("error"):
            continue
        roster.append({"domain": domain, "label": view.get("label", domain),
                       "set": any(f.get("already_set") for f in view.get("fields") or [])})
    return roster


def _status():
    """The platform-status group: arming (the GUI's own KONTROLL_STAGE_PUSHES posture — a read of OUR env, never a
    toggle), the running control-plane services (reuse list_services), and the secret-domain NAMES roster. All
    read-only/SURFACE — arming is FORBID-as-a-write (a settings page that could disarm itself is a C10 footgun)."""
    return {"armed": bool(os.environ.get("KONTROLL_STAGE_PUSHES")),
            "services": [{"name": s.get("name"), "enabled": s.get("enabled", True)} for s in list_services()],
            "secret_domains": _secret_roster()}


def read_view():
    """The value-free, four-group platform-settings snapshot for the GUI Settings panel (#135). Each group is
    computed independently and degrades to `{"error": ...}` on a bad source (never blanks its peers); the function
    never raises (service-never-sys.exit). NO secret value, NO `docker/.env` line — INPUTS + NAMES only (C11)."""
    groups = {"identity": _identity, "backup": _backup, "fleet": _fleet, "status": _status}
    out = {}
    for name, fn in groups.items():
        try:
            out[name] = fn()
        except Exception as e:                          # one bad group must not blank the panel; never sys.exit
            log.warning("settings group %s read failed: %s", name, e)
            out[name] = {"error": "%s read failed" % name}
    return out


# --- the EDIT half (Phase 5, #140): STAGE a change to a tracked instance.yml/fleet.yml knob via the SAME
# reconfigure spine as goal 2 (read-back → diff → severity confirm → C10 stage). The GUI never promotes; the
# operator promotes out-of-band and re-runs deploy-stack (the enact hint). api_privileged + docker/.env are NOT
# editable here — categorically FORBID (the read-only status group surfaces arming; secrets stay in the Secrets
# dialog). The knob set is DATA (settings/<area>.yml), not code. -------------------------------------------- #
def _edit_knobs():
    """The editable platform knobs (settings/instance.yml + settings/fleet.yml `knobs`), each stamped with its
    `area`/`file`. The closed allow-list of what the Settings page may STAGE — a knob NOT here has no write path."""
    return [dict(k, area=a["area"], file=a["file"]) for a in catalog.load_knob_descriptors()
            for k in (a.get("knobs") or []) if a.get("area") in ("instance", "fleet") and k.get("key")]


def _edit_knob(knob):
    """The descriptor for an instance.yml editable knob `knob` (writer scalar/nested/list), or None — fleet's
    `enabled_modules` (writer fleet_disable) is excluded here (it has its own removal plan, not a value edit)."""
    return next((k for k in _edit_knobs()
                 if k["key"] == knob and k.get("writer") in ("scalar", "nested", "list")), None)


def _fleet_knob():
    """The fleet `enabled_modules` removal descriptor (writer fleet_disable), or None."""
    return next((k for k in _edit_knobs() if k.get("writer") == "fleet_disable"), None)


def current_value(knob):
    """The CURRENT value of a settings knob, read from its target file (scalar / nested-under-parent / list).
    PURE — degrade to None/[] on a bad source, never raise. The reconfigure read-back for the Settings EDIT."""
    k = next((x for x in _edit_knobs() if x["key"] == knob), None)
    if not k:
        return None
    doc = _load_yaml(k["file"])
    if k.get("writer") == "nested":
        parent = doc.get(k.get("parent"))
        return parent.get(knob) if isinstance(parent, dict) else None
    if k.get("writer") == "fleet_disable":
        return [m for m in (doc.get("enabled_modules") or []) if m]
    val = doc.get(knob)
    return [r for r in val if r] if (k.get("writer") == "list" and isinstance(val, list)) else val


def build_plan(knob, value):
    """Compute the Settings EDIT PLAN for instance.yml knob `knob` set to `value`. PURE — reads instance.yml,
    writes nothing. Returns {"error": "unknown_knob"|"no_instance"|"bad_value"|None, ...}; on success the plan
    carries the comment-preserving `text_after` (the right writer: upsert_top_level_key / set_nested_scalar /
    set_list_block), the field diff (severity UPGRADED from the descriptor — mgmt_ip→identity, domain/tls_mode→
    redeploy), the FULL-file `current`/`proposed` structs verify_no_drop checks, the enact hand-off, and
    `token_parts`/`plan_token` (anti-drift + no-quiet-upgrade, M1). The write STAGES proposed/<run_id> (C10)."""
    k = _edit_knob(knob)
    if not k:
        return {"error": "unknown_knob", "knob": knob}
    path = paths.resolve(k["file"])
    if not os.path.exists(path):
        return {"error": "no_instance", "knob": knob}
    with open(path, encoding="utf-8") as fh:
        text_before = fh.read()
    cur = current_value(knob)
    if k["writer"] == "list":                                # backup_remotes — comma-separated → a validated list
        items = [s.strip() for s in str(value or "").split(",") if s.strip()]
        for it in items:
            if _REMOTE_NAME.fullmatch(it) is None or not plain_scalar(it):
                return {"error": "bad_value", "knob": knob, "detail": "%r is not a valid remote name" % it}
        proposed = items
    else:
        bad = validate_value(dict(k, key=knob), value)       # the P0a config-injection guard (ipv4/hostname/enum)
        if bad:
            return {"error": "bad_value", "knob": knob, "detail": bad}
        proposed = value
    try:
        text_after = (set_list_block(text_before, knob, proposed) if k["writer"] == "list"
                      else set_nested_scalar(text_before, k["parent"], knob, value) if k["writer"] == "nested"
                      else upsert_top_level_key(text_before, knob, value))
    except ValueError:
        # The writers fail closed on a value that would not read back from YAML as itself. A validator in front
        # should have refused it with its own message; this keeps a knob type whose validator is looser than its
        # writer from turning the refusal into an uncaught error (a 500, after the promote was already audited).
        return {"error": "bad_value", "knob": knob,
                "detail": "the value for %s cannot be written as plain YAML" % knob}
    meta = {knob: {"severity": k.get("severity"), "blast_radius": k.get("blast_radius")}}
    diff = _diff.compute_changes({knob: cur}, {knob: proposed}, meta)
    if not diff["changes"]:
        text_after = text_before                             # idempotent no-op — writes nothing
    return _stage_plan(knob, k["file"], text_before, text_after, diff, _instance_enact(k),
                       consequence=k.get("confirm_text") or "")


def build_fleet_disable_plan(module):
    """Compute the fleet-REMOVAL plan: disable `module` in fleet.yml (`disable_in_fleet`). Returns
    {"error": "unknown_knob"|"no_fleet"|"not_enabled"|None, ...}; severity `remove` (drops monitoring/log
    pipelines for live hosts — the consequence-naming confirm). PURE — reads fleet.yml, writes nothing."""
    k = _fleet_knob()
    if not k:
        return {"error": "unknown_knob", "module": module}
    path = paths.resolve(k["file"])
    if not os.path.exists(path):
        return {"error": "no_fleet", "module": module}
    with open(path, encoding="utf-8") as fh:
        text_before = fh.read()
    cur = [m for m in (_load_yaml(k["file"]).get("enabled_modules") or []) if m]
    if module not in cur:
        return {"error": "not_enabled", "module": module}
    text_after = disable_in_fleet(text_before, module)       # raises only if the key is absent — guarded by `cur` above
    proposed = [m for m in cur if m != module]
    diff = _diff.compute_changes({m: {} for m in cur}, {m: {} for m in proposed})   # a `remove` of `module`
    return _stage_plan("enabled_modules", k["file"], text_before, text_after, diff,
                       _fleet_enact(module), module=module, consequence=k.get("confirm_text") or "")


def _stage_plan(knob, rel, text_before, text_after, diff, enact, module=None, consequence=""):
    """Assemble the common reconfigure plan (the staged shape every settings write returns). The
    `current`/`proposed` structs verify_no_drop checks are the FULL file (so a writer that dropped a co-owner key
    fails closed — the load-bearing no-drop, review SHOULD-FIX 1); the operator-facing diff stays the edited
    knob. The write STAGES `proposed/<run_id>` to the overlay-resolved path (read == write == staged)."""
    target_rel = paths.overlay_rel(rel)
    before_full = yaml.safe_load(text_before) or {}
    after_full = yaml.safe_load(text_after) or {}
    sev_dec = _diff.severity_decision(diff)
    plan = {"error": None, "knob": knob, "target_rel": target_rel,
            "text_before": text_before, "text_after": text_after,
            "changes": diff["changes"], "will_overwrite": diff["will_overwrite"], "severity": diff["severity"],
            "current": before_full, "proposed": after_full,   # FULL file — what verify_no_drop checks
            "paths": [target_rel], "enact": enact, "consequence": consequence,
            "token_parts": [text_after, sev_dec], "plan_token": promote.plan_token(text_after, sev_dec)}
    if module is not None:
        plan["module"] = module
    return plan


def apply_plan(plan):
    """Write the staged instance.yml/fleet.yml change (comment-preserving text_after) to the overlay-resolved
    target. Idempotent: identical content → no write. Returns {"changed", "paths"} for the route's scoped commit.
    The API runs no play — making it live is the operator's promote + deploy-stack (the enact hand-off)."""
    path = os.path.join(paths.write_root(), plan["target_rel"])
    changed = False
    before = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
    if before != plan["text_after"]:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as out:
            out.write(plan["text_after"])
        changed = True
        log.info("settings: wrote %s (knob %s)", plan["target_rel"], plan.get("knob"))
    return {"changed": changed, "paths": [plan["target_rel"]]}


def _instance_enact(knob):
    """The hand-off to make a promoted instance.yml change LIVE — the API runs none. Every instance.yml knob
    needs a deploy-stack re-run; mgmt_ip additionally severs the current session (reconnect at the new IP)."""
    why = ("re-render docker/.env + recreate services on the new mgmt bind — RECONNECT at the new IP after"
           if knob["key"] == "mgmt_ip" else "re-render docker/.env + apply the change")
    return [{"kind": "operator",
             "cmd": "cd ansible && ansible-playbook playbooks/deploy-stack.yml -e @/tmp/sv.yml",
             "why": "after an admin promotes the proposal, %s (the GUI cannot self-actuate a platform change)" % why}]


def _fleet_enact(module):
    """The hand-off for a promoted fleet removal — re-gen collections at bootstrap, then deploy-stack."""
    return [{"kind": "operator",
             "cmd": "cd ansible && ansible-playbook playbooks/bootstrap.yml && "
                    "ansible-playbook playbooks/deploy-stack.yml -e @/tmp/sv.yml",
             "why": "after promote: bootstrap re-gens the collection set without %s, then deploy-stack drops its "
                    "scrape/log pipelines" % module}]
