"""backup — the BACKUP instance (#2) of the generalized secondary-capability seam.

The empirical proof the seam generalizes: backup registers as a pure drop-in (this module + capabilities/
backup.yml + vectors/backup.yml + gen-backup.py + classify.suggest_backup/declared_backup) touching ZERO spine
file — `promote.py`, `capability.py`, `api/routes/capability.py`, the GUI shell, and the generic INVARIANT D*
pins are all UNCHANGED. The capability-neutral spine dispatches here through the descriptor exactly as it does
to observe.py for telemetry.

Backup reuses the generic shell with no edit by modeling its single role-entrypoint capture as the one
"method" and its knobs as closed allow-list `params` (a preset cron `schedule`, a `retention` window, and a
`destination`) — so the shell's method+param picker renders all three as-is, no template change (#123). The
dialog writes a `backup:` MAPPING block (`{capable, schedule, retention, destination}`) into the class's
module.yml (block_shape: mapping, vs telemetry's list) and regenerates the schedule spec; ENACT is hand-off
strings (gen-backup + configure-semaphore — the API runs neither). `retention` is RECORDED + carried to the
schedule as `kontroll_backup_retention` AND ENFORCED by the retention-prune play in `backup-configs.yml` (#125 — a
destructive git rewrite of the local capture history). `destination: offsite` is OFFERED but REFUSED at propose
(no encryption-at-rest story — captures aren't SOPS; SECURITY.md C8), so `local` is the only enactable target.

DISTINCT from service/backups.py (plural) — that is the read-only VIEWER of the capture git history (the GUI backup
index/view/diff, C14). THIS module is the capability BUILDER that writes the `backup:` block. They share a name root
and the C8/C14 captures concern, nothing else; do not conflate them.
"""
import importlib.util
import logging
import os

import yaml

from kontroll import paths
from kontroll.service import _diff, promote
from kontroll.service._blockwrite import upsert_into_block

log = logging.getLogger("kontroll.service.backup")

# scripts/kontroll/service/backup.py — four dirnames up is the real repo root (where gen-backup + the roles
# live). Used to load the generator and to check the role backup entrypoint (roles are real repo content, not
# tmp_repo-mutable), and to detect a repointed paths.ROOT (a test) so regen no-ops off the real tree.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# The CLOSED allow-lists of knobs the dialog offers — modeling them as `params` lets backup reuse the generic
# shell's allow-list param picker with NO shell edit (capParamsUI renders one <select> per param; capSelection
# collects them — so adding a knob is a list entry here, never a template change). Same pattern the logging
# capability uses for its retention (a Stage-1 method param, not a Stage-3 resource). Extend by adding a preset.
_SCHEDULES = ["0 2 * * *", "0 */6 * * *", "0 3 * * 0"]   # nightly 02:00 / every 6h / weekly Sun 03:00
# RETENTION — how much capture HISTORY to keep (the captures dir is a LOCAL-ONLY git history; keep-all = full).
# `keep-all` is first = the safe default (no pruning). A finite window is RECORDED in the block + carried to the
# schedule as `kontroll_backup_retention` and ENFORCED by the retention-prune play in `backup-configs.yml`
# (#125 — a guarded, default-safe, `--check`-safe git-history rewrite of the local-only capture history), so a
# finite window declares intent that the prune enforces; `keep-all` keeps the full history. The value is NON-secret.
_RETENTIONS = ["keep-all", "30d", "90d", "365d"]
# DESTINATION — where captures live. `offsite` is OFFERED (the lever is visible) but REFUSED at propose: config
# captures aren't encrypted at rest, so an offsite copy needs an encryption-at-rest story first (SECURITY.md C8).
_DESTINATIONS = ["local", "offsite"]
_METHOD = "backup"   # backup's single "method" == the class's role backup entrypoint (method_source role_entrypoint)


def _gen_backup():
    spec = importlib.util.spec_from_file_location("gen_backup", os.path.join(_REPO, "scripts", "gen-backup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _role_has_backup(role):
    return os.path.exists(os.path.join(_REPO, "ansible", "roles", role, "tasks", "backup.yml"))


def offerable_methods(facts):
    """The Stage-1 picker payload for backup — ONE method (the class's role-entrypoint capture) whose params are
    the closed allow-lists of knobs: `schedule` (preset cron), `retention` (how much history to keep), and
    `destination` (local; offsite is offered-but-deferred). This reuses the GENERIC shell's method+param picker
    with zero shell edit (the seam's whole point): a class backs up exactly one way, so a single method."""
    return [{"name": _METHOD, "label": "config backup (per-class capture)",
             "params": {"schedule": {"allowed": list(_SCHEDULES), "required": True},
                        "retention": {"allowed": list(_RETENTIONS), "required": False},
                        "destination": {"allowed": list(_DESTINATIONS), "required": False}}}]


def current_values(key):
    """The DECLARED backup knobs of class `key` (the single `backup:` mapping as the pseudo-method 'backup'),
    for pre-filling the reconfigure renderer + the diff (Phase 4a). PURE — reads modules/<key>/module.yml via
    paths.ROOT; {} if not onboarded / no backup block (degrade, never raise). The reconfigure read-back
    (design 22 §3.1)."""
    mod_path = paths.module_file(key)
    try:
        with open(mod_path, encoding="utf-8") as fh:
            module = yaml.safe_load(fh.read()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    b = module.get("backup") or {}
    if not b:
        return {}
    return {_METHOD: {"schedule": b.get("schedule"), "retention": b.get("retention"),
                      "destination": b.get("destination")}}


def build_plan(key, selection):
    """PROPOSE (pure) — the plan to add a `backup:` block to onboarded class `key`. selection =
    {method:'backup', params:{schedule:<preset cron>}}. Returns
    {error: 'not_onboarded'|'not_backup_capable'|'bad_param'|'already_declared'|None, ...}; writes nothing.
    Mirrors observe.build_telemetry_plan but for the `backup:` MAPPING block; the plan_token gates it. `key` is
    a REQUEST value and is refused first (paths.RequestBoundaryError), before the join and before any other answer."""
    paths.component(key, "device-class key")            # C22: the request boundary, before the join
    params = (selection or {}).get("params") or {}
    schedule = params.get("schedule")
    retention = params.get("retention") or "keep-all"     # default-safe: keep the full history (no prune)
    destination = params.get("destination") or "local"
    mod_rel = "modules/%s/module.yml" % key
    mod_path = os.path.join(paths.ROOT, mod_rel)
    if not os.path.exists(mod_path):
        return {"error": "not_onboarded", "key": key}
    if schedule not in _SCHEDULES:
        return {"error": "bad_param", "key": key, "detail": "schedule %r is not a preset" % schedule}
    if retention not in _RETENTIONS:
        return {"error": "bad_param", "key": key, "detail": "retention %r is not a preset" % retention}
    if destination not in _DESTINATIONS:
        return {"error": "bad_param", "key": key, "detail": "destination %r is not a preset" % destination}
    if destination == "offsite":
        # OFFERED but gated: config captures aren't encrypted at rest, so an offsite copy needs the
        # encryption-at-rest story first (SECURITY.md C8). Fail-closed with the reason, not a silent local fallback.
        return {"error": "bad_param", "key": key,
                "detail": "destination 'offsite' is deferred — config captures aren't encrypted at rest; an "
                          "offsite copy needs the encryption-at-rest story first (SECURITY.md C8). Use 'local'."}
    with open(mod_path, encoding="utf-8") as fh:
        text_before = fh.read()
    module = yaml.safe_load(text_before) or {}
    role = module.get("role") or key
    if not _role_has_backup(role):
        return {"error": "not_backup_capable", "key": key,
                "detail": "role %r has no tasks/backup.yml entrypoint" % role}
    # RECONFIGURE (Phase 4a): a declared `backup:` block is no longer a wall — read the current knobs + DIFF the
    # proposal, then ADD (first declare), no-op (same), or UPSERT the mapping in place (changed schedule/
    # retention/destination). The diff/severity drive the overwrite-confirm + the token's no-quiet-upgrade fold.
    current = current_values(key)
    proposed = {_METHOD: {"schedule": schedule, "retention": retention, "destination": destination}}
    diff = _diff.compute_changes(current, proposed)
    block_lines = ["backup:", "  capable: true", '  schedule: "%s"' % schedule,
                   "  retention: %s" % retention, "  destination: %s" % destination]
    if not diff["changes"]:
        text_after = text_before                               # idempotent no-op (same value) — writes nothing
    elif current:                                              # declared → replace the `backup:` mapping in place
        text_after = upsert_into_block(text_before, "backup", _METHOD, block_lines, list_block=False)
    else:                                                      # first declare → append at EOF
        text_after = text_before.rstrip("\n") + "\n" + "\n".join(block_lines) + "\n"
    sched_rel = "config/semaphore/schedules.generated.yml"
    sev_dec = _diff.severity_decision(diff)
    return {"error": None, "key": key, "method": _METHOD,
            "params": {"schedule": schedule, "retention": retention, "destination": destination},
            "changes": diff["changes"], "will_overwrite": diff["will_overwrite"], "severity": diff["severity"],
            "current": current, "proposed": proposed,   # the structs verify_no_drop checks at promote
            "dashboards": [], "mod_rel": mod_rel, "mod_path": mod_path,
            "text_before": text_before, "text_after": text_after,
            "paths": [mod_rel, sched_rel], "regen_paths": [sched_rel],
            "enact": backup_enact_commands(key),
            "token_parts": [text_after, sev_dec],   # text_after (anti-drift) + the severity decision (no-quiet-upgrade, M1)
            "plan_token": promote.plan_token(text_after, sev_dec)}


def apply_plan(plan):
    """PROMOTE — write the `backup:` block into module.yml (idempotent) + regenerate the schedule spec.
    Returns {changed, paths} for the route's scoped commit. The API runs NO play — registering the schedule in
    Semaphore is the operator's enact step."""
    changed = False
    with open(plan["mod_path"], encoding="utf-8") as fh:
        if fh.read() != plan["text_after"]:
            with open(plan["mod_path"], "w", encoding="utf-8", newline="\n") as out:
                out.write(plan["text_after"])
            changed = True
            log.info("backup: wrote backup block for %s", plan["key"])
    regen = regenerate_schedules()
    return {"changed": changed or bool(regen), "paths": sorted(set([plan["mod_rel"]] + regen))}


def regenerate_schedules():
    """Re-run gen-backup against the real tree so the committed schedule spec matches the new block. SELF-
    PROTECTING (mirrors observe.regenerate_observability): a no-op when paths.ROOT is repointed off the
    generator's real-tree root (a tmp_repo test), so a test exercises the block write without the real
    generator touching the real repo."""
    gen = _gen_backup()
    if os.path.normpath(paths.ROOT) != os.path.normpath(gen.ROOT):
        log.warning("backup: paths.ROOT (%s) != generator root (%s) — skipping regen (test/sandbox)",
                    paths.ROOT, gen.ROOT)
        return []
    # Phase B (bake-code): regenerate into the WRITE clone (paths.write_root()), never the read-only baked
    # /opt/kontroll. Identity when write_root()==ROOT (legacy /repo + deploy-stack). Mirrors observe.regenerate_observability.
    gen.ROOT = paths.write_root()
    gen.main([])
    return ["config/semaphore/schedules.generated.yml"]


def backup_enact_commands(key):
    """The steps to make the just-promoted backup schedule LIVE — the API runs none. enact_kind:
    semaphore_schedule → regenerate the spec, then register it in Semaphore. All `kind: operator` (item F):
    `configure-semaphore.py` needs the Semaphore admin creds + key files, which live on the control node (not the
    runner), so registration stays an operator command; once registered the schedule runs autonomously on cron."""
    return [
        {"kind": "operator", "cmd": "python3 scripts/gen-backup.py",
         "why": "regenerate the per-class Semaphore backup-schedule spec from the backup: block"},
        {"kind": "operator", "cmd": "python3 scripts/configure-semaphore.py",
         "why": "register the generated schedule(s) in Semaphore (idempotent; needs the Semaphore admin creds)"},
        {"kind": "operator", "cmd": "# verify: the 'backup-%s' schedule appears + is active in the Semaphore UI" % key,
         "why": "confirm the schedule landed (it then runs backup-configs.yml --limit on its cron)"},
    ]
