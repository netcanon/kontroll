"""logsvc — the LOGGING instance (#3) of the generalized secondary-capability seam.

Logging is added to an ALREADY-onboarded device class via the standalone capability dialog: choose a method
(syslog_push / journald_remote / rest_pull / file_tail_ssh) + optional params (incl. a retention preset), PROPOSE
(a pure diff), then PROMOTE (the audited data write). This module is the logging-SPECIFIC half — the `logs:` LIST
write into modules/<key>/module.yml + the regen of the generated Vector config drop-ins + the enact-command
builder. The capability-NEUTRAL spine (the dialog/route/promote.plan_token) dispatches here through
capabilities/logging.yml; registering logging touches zero spine file (the seam's whole point — proven by
backup.py/observe.py).

Named `logsvc` NOT `logging` to avoid shadowing the stdlib `logging` module every service file imports. Three
boundaries, identical to observe.py: PROPOSE writes nothing (build_logging_plan is PURE); PROMOTE writes the
DECLARED `logs:` block and REGENERATES the Vector drop-ins FROM it (they can't drift independently — plan_token
gates the declared block, the scoped commit carries block + regenerated config); ENACT is hand-off only
(logging_enact_commands returns the strings the operator runs to make logs flow — a Vector reload + the
device-side wiring play; the API runs NO play).

The module write is comment-PRESERVING line-surgery (shared service/_blockwrite.insert_into_block) so a
hand-written module.yml is never flattened. Param values are validated against the method's CLOSED allow-list
first (shared validate_params — the config-injection guard, defense-in-depth with gen-logging).
"""
import glob
import importlib.util
import logging
import os

import yaml

from kontroll import catalog, paths
from kontroll.service import promote
from kontroll.service import _diff
from kontroll.service._blockwrite import (insert_into_block, require_fleet_list, require_reads_back, staged_paths,
                                          upsert_into_block, validate_params,
                                          write_module)

log = logging.getLogger("kontroll.service.logsvc")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _gen_logging():
    """The gen-logging.py module, loaded by path (hyphen -> importlib). Single source of truth for the Vector
    drop-in bytes, so logsvc's regen can never drift from the generator's (mirrors observe._gen_observability)."""
    spec = importlib.util.spec_from_file_location(
        "gen_logging", os.path.join(_REPO, "scripts", "gen-logging.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _generated_paths(key, method_name):
    """The repo-rel Vector drop-ins a promote of (method_name, key) (re)generates — single-sourced from
    gen-logging's GEN_DIR + filename convention so logsvc's PLANNED paths can NEVER drift from where the
    generator actually writes. (This guards the bug a hard-coded path caused: logsvc named
    docker/vector/config.d/sources|transforms/generated/, but gen-logging writes ONE fragment per (method, key)
    under docker/vector/generated/ + the aggregate capability sink — so a promote committed phantom paths and
    never staged the real config.) TWO files change on every add: the per-(method, key) fragment AND the
    aggregate `_capability_sink` (its `inputs` list grows by the new transform). PURE — _gen_logging only
    imports the module (defines constants/functions; main() runs only under __main__)."""
    gen = _gen_logging()
    return ["%s/%s_%s.generated.yaml" % (gen.GEN_DIR, method_name, key),
            "%s/_capability_sink.generated.yaml" % gen.GEN_DIR]


def _render_logs_entry(method_name, params):
    """A single `logs:` list item, flow-mapping style matching the existing module.yml entries
    (`- {method: syslog_push, params: {transport: tcp}}`). Only allow-listed param values reach here
    (validate_params has already run). Peer of observe._render_metrics_entry."""
    if params:
        inner = ", ".join("%s: %s" % (k, params[k]) for k in params)
        return "  - {method: %s, params: {%s}}" % (method_name, inner)
    return "  - {method: %s}" % method_name


# --- PROPOSE (pure) ------------------------------------------------------------------------------------ #
def build_logging_plan(key, method_name, params=None, methods=None):
    """Compute the logging PLAN for adding `method_name` (+ optional params incl. a `retention` preset) to
    onboarded class `key`. PURE — reads modules/<key>/module.yml through paths.module_file (the clone's copy
    shadows the baked one), writes nothing; `mod_path` is the WRITE target (paths.module_write_path). Returns
    {"error": "not_onboarded"|"unknown_method"|"bad_param"|"already_declared"|None, ...}; on success the plan
    carries the comment-preserving module text AFTER the add, the repo-rel paths promote will commit (the
    module + the method's generated Vector source/transform drop-ins), the enact commands, and the
    propose->promote plan_token (a hash of the declared block — the config is generated FROM it, so it can't
    drift independently). ADD-ONLY: a method already declared on the class is `already_declared` (idempotent).
    Mirrors observe.build_telemetry_plan. `key` is a REQUEST value and is refused first
    (paths.RequestBoundaryError), before the join and before any other answer."""
    paths.component(key, "device-class key")            # C22: the request boundary, before the join
    methods = methods if methods is not None else catalog.load_logging()
    by_name = {m["name"]: m for m in methods}
    mod_rel = "modules/%s/module.yml" % key
    read_path = paths.module_file(key)        # the clone shadows the baked root — what current_values() reads too
    if not os.path.exists(read_path):
        return {"error": "not_onboarded", "key": key}
    method = by_name.get(method_name)
    if method is None:
        return {"error": "unknown_method", "key": key, "method": method_name}
    bad = validate_params(method, params)
    if bad:
        return {"error": "bad_param", "key": key, "method": method_name, "detail": bad}

    with open(read_path, encoding="utf-8") as fh:
        text_before = fh.read()
    module = yaml.safe_load(text_before) or {}
    # RECONFIGURE (Phase 4a) — mirror observe.build_telemetry_plan: read current, DIFF, then add/no-op/upsert.
    current = current_values(key)
    proposed = dict(current)
    proposed[method_name] = params or {}
    diff = _diff.compute_changes(current, proposed)
    new_lines = [_render_logs_entry(method_name, params)]
    if not diff["changes"]:
        text_after = text_before                               # idempotent no-op (same value) — writes nothing
    elif method_name in current:
        text_after = upsert_into_block(text_before, "logs", method_name, new_lines)      # reconfigure (modify)
    else:
        text_after = insert_into_block(text_before, "logs", new_lines)                   # first declare (add)
    # the writers edit by line: what they would write must READ as this change and nothing else (409 if not)
    require_reads_back(text_before, text_after, "logs", new_lines, entry_id=method_name)

    # The Vector drop-ins this add (re)generates — single-sourced from gen-logging (see _generated_paths) so the
    # planned paths can never drift from where the generator actually writes (the per-(method, key) fragment +
    # the aggregate capability sink).
    regen = _generated_paths(key, method_name)
    sev_dec = _diff.severity_decision(diff)
    return {"error": None, "key": key, "method": method_name, "params": params or {},
            "mod_rel": mod_rel, "mod_path": paths.module_write_path(key),   # the WRITE target: write_root()
            "text_before": text_before, "text_after": text_after,
            "changes": diff["changes"], "will_overwrite": diff["will_overwrite"], "severity": diff["severity"],
            "current": current, "proposed": proposed,   # the structs verify_no_drop checks at promote
            "regen_paths": regen, "paths": [mod_rel] + regen,
            "enact": logging_enact_commands(key, method),
            "token_parts": [text_after, sev_dec],   # text_after (anti-drift) + the severity decision (no-quiet-upgrade, M1)
            "plan_token": promote.plan_token(text_after, sev_dec)}


# --- the seam's uniform service interface (the spine dispatches to these by convention) ---------------- #
def offerable_methods(facts):
    """The Stage-1 picker PAYLOAD for these facts — the `applies_when`-filtered logging methods, each as
    {name, label, params}. REUSES classify.applicable_methods with the logging registry injected (the
    `methods=` kwarg already exists), so NO change to classify is needed. Most logging methods have NO
    applies_when -> the UNIVERSAL bucket (journald/file on any box), mirroring host_node; syslog_push carries
    applies_when {plugin: cliconf|netconf} to scope it to network gear. The spine calls this ONLY when
    render_method_stage is true (it is, for logging)."""
    from kontroll.service.classify import applicable_methods   # local import — capability-specific
    out = []
    for m in applicable_methods(facts, methods=catalog.load_logging()):
        params = {p: {"allowed": list(spec.get("allowed") or []), "required": bool(spec.get("required"))}
                  for p, spec in (m.get("params") or {}).items()}
        out.append({"name": m["name"], "label": m.get("label", m["name"]), "params": params,
                    "secret_domain": m.get("secret_domain")})   # advisory cred-prereq surfaced in the cap dialog (G3)
    return out


def current_values(key):
    """The DECLARED logging values of class `key`, for pre-filling the reconfigure renderer + the diff (Phase
    4a). PURE — reads modules/<key>/module.yml through paths.module_file; returns {method_name: {param: value}} per
    declared `logs:` entry. {} if not onboarded / no logs block (degrade, never raise). Mirrors
    observe.current_values (design 22 §3.1)."""
    mod_path = paths.module_file(key)
    try:
        with open(mod_path, encoding="utf-8") as fh:
            module = yaml.safe_load(fh.read()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {e["method"]: (e.get("params") or {}) for e in (module.get("logs") or []) if e.get("method")}


def build_plan(key, selection):
    """The capability-NEUTRAL propose entrypoint the spine (capability.py) calls — unpacks the dialog's
    `selection` ({method, params}) into build_logging_plan. Identical signature to observe.build_plan, so the
    spine never branches on the capability."""
    return build_logging_plan(key, selection.get("method"), params=selection.get("params"))


def apply_plan(plan):
    """The capability-NEUTRAL promote entrypoint the spine calls — the logging write + regen."""
    return apply_logging_plan(plan)


# --- PROMOTE (the data write + regen) ------------------------------------------------------------------ #
def apply_logging_plan(plan):
    """Write the `logs:` block into modules/<key>/module.yml (comment-preserving) and REGENERATE the Vector
    config drop-ins from it. Idempotent: a plan that changes nothing, or a target that already holds the text, is
    no write (_blockwrite.write_module). Returns {"changed", "paths"} — the repo-rel paths the route's scoped
    commit stages (the module + the class's regenerated drop-ins). The API runs NO play; making logs flow is the
    operator's enact step. Mirror of observe.apply_telemetry_plan."""
    require_fleet_list()                 # before the first write: no regeneration can follow a fleet with no list
    changed = write_module(plan)
    if changed:
        log.info("logsvc: wrote %s logs block for %s", plan["method"], plan["key"])
    regen = regenerate_logging(plan["key"])
    return {"changed": changed or bool(regen), "paths": staged_paths(plan, regen)}


def regenerate_logging(key):
    """Re-run gen-logging against the real tree so the committed Vector drop-ins match the new logs block, and
    return the class's regenerated drop-in paths (repo-rel) for the scoped commit. SELF-PROTECTING: a no-op
    when paths.ROOT is repointed off the generator's real-tree root (a tmp_repo test) — copied verbatim from
    observe.regenerate_observability. Returns this class's per-(method, key) fragment(s) under gen-logging's
    GEN_DIR PLUS the aggregate capability sink (whose `inputs` list grew by the new transform), so the route's
    scoped commit stages the regenerated config alongside the module change — never leaves it uncommitted."""
    gen = _gen_logging()
    if os.path.normpath(paths.ROOT) != os.path.normpath(gen.ROOT):
        log.warning("logsvc: paths.ROOT (%s) != generator root (%s) — skipping regen (test/sandbox)",
                    paths.ROOT, gen.ROOT)
        return []
    # Phase B (bake-code): regenerate into the WRITE clone (paths.write_root()), never the read-only baked
    # /opt/kontroll. Identity when write_root()==ROOT (legacy /repo + deploy-stack). Mirrors observe.regenerate_observability.
    gen.ROOT = paths.write_root()
    gen.main([])   # writes docker/vector/generated/*.generated.yaml under write_root() (idempotent; prunes orphans)
    gen_dir = os.path.join(gen.ROOT, gen.GEN_DIR)
    files = glob.glob(os.path.join(gen_dir, "*_%s.generated.yaml" % key))
    sink = os.path.join(gen_dir, "_capability_sink.generated.yaml")
    if os.path.exists(sink):
        files.append(sink)
    return sorted(os.path.relpath(p, gen.ROOT).replace(os.sep, "/") for p in files)


# --- ENACT (hand-off strings; the API runs none) ------------------------------------------------------- #
def logging_enact_commands(key, method):
    """The steps to make the just-promoted logging LIVE — the API executes none (the data write is committed;
    nothing flows until these run). Each step is `{kind, why, ...}` (item F). Tailored to the method:
      * a PUSH method (syslog_push / journald_remote) needs its device-side WIRING play to configure the device
        to ship logs to the collector (kind 'semaphore' — Tier-1, --check then apply) + a Vector reload.
      * a PULL method (rest_pull / file_tail_ssh) needs ONLY the Vector reload — the source reaches OUT.
    NEVER a Prometheus reload (that's telemetry). The Vector reload is the ONE step every logging add ends on."""
    cmds = []
    wiring = method.get("wiring_role")
    if wiring and method.get("direction") == "push":
        cmds.append({"kind": "semaphore", "task": "wire-logging", "args": {"role": wiring, "target": key},
                     "cmd": "ansible-playbook ansible/playbooks/wire-logging.yml -e role=%s -e target=%s "
                            "--check --diff" % (wiring, key),
                     "why": "configure %s to ship its logs to the collector (Semaphore task — tick Dry Run for "
                            "the --check pass, then apply)" % key})
    cmds.append({"kind": "operator",
                 "cmd": "python3 scripts/gen-logging.py && cd ansible && "
                        "ansible-playbook playbooks/deploy-stack.yml -e @/tmp/sv.yml",
                 "why": "regenerate the Vector drop-ins and reload Vector so it picks up the new %s source "
                        "(operator-run — needs host Docker; Vector reloads config on bring-up/SIGHUP)"
                        % method["name"]})
    return cmds
