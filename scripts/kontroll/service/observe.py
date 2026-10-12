"""observe — the TELEMETRY instance (#1) of the generalized secondary-capability seam.

Telemetry is added to an ALREADY-onboarded device class via the standalone capability dialog: choose a method
(snmp / blackbox / host_node / pve) + optional curated dashboards, PROPOSE (a pure diff), then PROMOTE (the
audited data write). This module is the telemetry-SPECIFIC half — the `metrics:`/`dashboards:` write into
`modules/<key>/module.yml` + the regen of the generated Prometheus targets + the enact-command builder. The
capability-NEUTRAL spine (the dialog/route/promote.plan_token) lives elsewhere and dispatches here through the
`capabilities/telemetry.yml` descriptor; registering telemetry touched zero spine file (the seam's whole point).

Three boundaries the design pins (docs/observability/secondary-capability-dialog.md, master §6):
  * PROPOSE writes nothing — `build_telemetry_plan` is PURE (reads via paths.ROOT; returns the plan + token).
  * PROMOTE writes the DECLARED block (the source of truth) and REGENERATES the targets FROM it — the targets
    can't drift independently, so `plan_token` gates the declared block alone, and the scoped commit carries
    the block + its regenerated artifacts (so `gen-observability --check` stays green in CI).
  * ENACT is hand-off only — `telemetry_enact_commands` returns the strings the operator runs to make metrics
    flow ("not live until you run these"); the API runs NO play and touches NO exporter.

The module write is comment-PRESERVING line-surgery (never a `yaml.safe_dump` round-trip) so a hand-written
`modules/<key>/module.yml` (proxmox/cisco carry rich comments) is never flattened. The param values that reach
the rendered entry are validated against the method's CLOSED allow-list first — the same config-injection
guard the generator enforces (defense-in-depth; a value with YAML metacharacters is rejected, never written).
"""
import glob
import importlib.util
import json
import logging
import os

import yaml

from kontroll import catalog, paths
from kontroll.service import promote
# Shared with logsvc (logging) + any future capability instance — one comment-preserving block writer + one
# config-injection guard, so the copies can never re-diverge (test_blockwrite.py pins them). Lifted from here.
from kontroll.service import _diff
from kontroll.service._blockwrite import insert_into_block, upsert_into_block, validate_params
from kontroll.service._validate import plain_scalar

log = logging.getLogger("kontroll.service.observe")

# observe.py is scripts/kontroll/service/observe.py — four dirnames up is the REAL repo root, where the
# generators live. The generators key off their OWN __file__ root (always the real tree), so we both load
# them from here AND use it to detect when paths.ROOT has been repointed (a tmp_repo test) — see regenerate.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _gen_observability():
    """The gen-observability.py module, loaded by path (hyphen in the filename → importlib). Only its PURE
    helpers (_hosts_in_group/_entries/_render) and main() are used — single source of truth for target bytes,
    so observe's render can never drift from the generator's."""
    spec = importlib.util.spec_from_file_location(
        "gen_observability", os.path.join(_REPO, "scripts", "gen-observability.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- entry rendering (validated values only) ----------------------------------------------------------- #
def _render_metrics_entry(method_name, params):
    """A single `metrics:` list item, flow-mapping style matching the existing module.yml entries
    (`- {method: snmp, params: {module: if_mib}}`). Only allow-listed param values reach here."""
    if params:
        inner = ", ".join("%s: %s" % (k, params[k]) for k in params)
        return "  - {method: %s, params: {%s}}" % (method_name, inner)
    return "  - {method: %s}" % method_name


def _render_dashboard_entry(d):
    """A single `dashboards:` list item (`- {gnet: 10347, name: proxmox}`). A hand-formatted line, so it renders
    only what _clean_dashboards() let through: an integer and one path component."""
    return "  - {gnet: %d, name: %s}" % (int(d["gnet"]), d["name"])


_MAX_DASHBOARDS = 32                    # per request; a class declares a handful
_MAX_GNET = 2 ** 31 - 1                 # a grafana.com board id (scripts/fetch-dashboards.py holds the same bound;
                                        # that script does not import the service layer)


def _declared_dashboards():
    """{name: {board ids}} over every class's `dashboards:` block, shipped and onboarded. One name is ONE file
    under dashboards/grafana/dashboards/, whichever class declares it — so a selection may not give a declared name
    another board id (fetch-dashboards.py would write the later download over the curated one), nor a declared
    board another name (the fetch keeps one name per board; the second file would never be written). A module file
    that cannot be read, or whose block is not a list of {gnet, name} items, contributes what it validly holds and
    nothing else: one damaged class must not turn every class's propose into an error."""
    out = {}
    for key in paths.module_keys():
        try:
            with open(paths.module_file(key), encoding="utf-8") as fh:
                module = yaml.safe_load(fh)
        except (OSError, ValueError, yaml.YAMLError):          # ValueError: bytes that are not UTF-8
            continue
        block = module.get("dashboards") if isinstance(module, dict) else None
        for d in block if isinstance(block, list) else []:
            if not isinstance(d, dict):
                continue
            name, gnet = d.get("name"), d.get("gnet")
            if isinstance(name, str) and isinstance(gnet, int) and not isinstance(gnet, bool):
                out.setdefault(name, set()).add(gnet)
    return out


def _clean_dashboards(dashboards, declared=None):
    """(list of {gnet, name}, None), or (None, message) when the selection is outside the closed shape. An item is
    exactly `{gnet: <positive integer>, name: <one path component>}`. The name is written into a hand-formatted
    YAML line (with a newline inside it, it rewrote the class's role and inventory group; with none, the item
    itself — its board id and its `derived` mark), is a FILE name when the plan reads the board's uid, and is the
    file fetch-dashboards.py later writes an internet download to. So it gets the request-boundary charset, which a
    serializer would not have given it (`../x` is valid YAML); it must also be a word YAML reads as a string
    (`null`, `true` and `123` are inside the charset) where the line puts it — inside a flow mapping; and a name is
    one file and a board is one name: no name repeated within the selection or tied by `declared`
    ({name: {board ids}} over every class) to another board id, and no board id given a second name. The message
    names the item and the rule, never the value."""
    if not dashboards:
        return [], None
    if not isinstance(dashboards, list):
        return None, "dashboards must be a list"
    if len(dashboards) > _MAX_DASHBOARDS:
        return None, "dashboards holds more items than one request may declare"
    declared = declared or {}
    names_of = {}                                            # board id -> the names it already has
    for known, boards in declared.items():
        for board in boards:
            names_of.setdefault(board, set()).add(known)
    out, seen = [], set()
    for i, d in enumerate(dashboards):
        if not isinstance(d, dict) or set(d) != {"gnet", "name"}:
            return None, "dashboards[%d] must be exactly {gnet, name}" % i
        gnet, name = d["gnet"], d["name"]
        if isinstance(gnet, bool) or not isinstance(gnet, int) or not 0 < gnet <= _MAX_GNET:
            return None, "dashboards[%d].gnet must be a positive integer (a grafana.com board id)" % i
        try:
            paths.component(name, "dashboard name")
        except paths.RequestBoundaryError:
            return None, "dashboards[%d].name must be 1-64 chars of [a-z0-9_-]" % i
        if not plain_scalar(name, flow=True):
            return None, ("dashboards[%d].name is a word YAML reads as another type (null, a boolean, a number, a "
                          "date)" % i)
        if name in seen:
            return None, "dashboards[%d].name repeats an earlier item — a name is one file" % i
        if declared.get(name, {gnet}) != {gnet}:
            return None, "dashboards[%d].name is already declared with another board id — a name is one file" % i
        if names_of.get(gnet) and name not in names_of[gnet]:
            return None, "dashboards[%d].gnet is already declared under another name — a board is one file" % i
        seen.add(name)
        names_of.setdefault(gnet, set()).add(name)
        out.append({"gnet": gnet, "name": name})
    return out, None


def _dashboard_path(name):
    """dashboards/grafana/dashboards/<name>.json in the clone — dashboards/ is config-as-data in the canonical, NOT
    baked into the control image, so write_root() and not the read-only ROOT. `name` is one path component."""
    return os.path.join(paths.write_root(), "dashboards", "grafana", "dashboards", "%s.json" % name)


def _foreign_dashboard_file(dashboards):
    """A message when an item of a CLEANED selection names a file that already exists under
    dashboards/grafana/dashboards/ and does not say it is that board (its `gnetId`), else None. fetch-dashboards.py
    writes a download to `<name>.json`, and a name no class declares is not thereby a free name: the hand-authored
    logs dashboard is such a file. (The fetch refuses the overwrite as well — this is the same rule at the request,
    so the operator hears it before the proposal is promoted.)"""
    for i, d in enumerate(dashboards):
        path = _dashboard_path(d["name"])
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                have = json.load(fh).get("gnetId")
        except (OSError, ValueError, AttributeError):
            have = None
        if isinstance(have, bool) or have != d["gnet"]:
            return "dashboards[%d].name is an existing dashboard file that is not this board — a name is one file" % i
    return None


# --- Grafana deep-links (surfaced in the capability dialog after a telemetry promote — #122) ------------ #
def _grafana_base():
    """The Grafana base URL for the deploy, or None when it can't be resolved (no fabricated host). Prefers the
    by-IP form the operator reaches from the mgmt network (`http://<KONTROLL_MGMT_IP>:3002` — Grafana publishes
    HTTP on :3002; the Index link in config/services.yml carries the same URL and tests/unit/test_services.py
    holds those two together; the shipped Homepage tile carries it too, pinned by
    tests/unit/test_kontroll_init.py), falling back to the reverse-proxied domain form
    (`https://grafana.<KONTROLL_DOMAIN>`, GF_SERVER_ROOT_URL). Both come from the deploy `.env`; the onboard-gui
    container is passed KONTROLL_MGMT_IP/KONTROLL_DOMAIN so the link resolves there."""
    ip = os.environ.get("KONTROLL_MGMT_IP")
    if ip:
        return "http://%s:3002" % ip
    dom = os.environ.get("KONTROLL_DOMAIN")
    if dom:
        return "https://grafana.%s" % dom
    return None


def _dashboard_uid(name):
    """The committed Grafana dashboard's STABLE uid (dashboards/grafana/dashboards/<name>.json), or None if it
    hasn't been fetched yet. fetch-dashboards.py preserves the uid pinned in the downloaded JSON, so a
    `/d/<uid>` deep-link is reproducible (offline, committed). Read-only against the clone (dashboards/ is
    config-as-data in the canonical, NOT baked into the control image — so write_root(), not the read-only ROOT)."""
    if not name:
        return None
    try:
        paths.component(name, "dashboard name")         # C22, at the join: here the name is a file name
    except paths.RequestBoundaryError:
        return None
    p = _dashboard_path(name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("uid")
    except (OSError, ValueError):
        return None


def telemetry_grafana_links(base, dashboards):
    """The Grafana links to surface in the capability dialog after a telemetry promote (#122): an `Open Grafana`
    entry plus a per-dashboard `/d/<uid>` deep-link for each curated dashboard whose committed JSON pins a uid.
    Returns [] when `base` is None (KONTROLL_MGMT_IP/KONTROLL_DOMAIN unset — e.g. a sandbox/test), so the plan
    never fabricates a host. Each link is {label, url}; the route passes them through GENERICALLY (any capability
    may emit `links`), so the dialog shell stays capability-neutral."""
    if not base:
        return []
    links = [{"label": "Open Grafana", "url": base}]
    for d in dashboards or []:
        uid = _dashboard_uid(d.get("name"))
        if uid:
            links.append({"label": "%s dashboard" % d["name"], "url": "%s/d/%s" % (base, uid)})
    return links


# --- PROPOSE (pure) ------------------------------------------------------------------------------------ #
def build_telemetry_plan(key, method_name, params=None, dashboards=None, methods=None):
    """Compute the telemetry PLAN for adding `method_name` (+ optional curated `dashboards`) to onboarded
    class `key`. PURE — reads modules/<key>/module.yml via paths.ROOT, writes nothing. Returns
    {"error": "not_onboarded"|"unknown_method"|"bad_param"|"already_declared"|None, ...}; on success the plan
    carries the comment-preserving module text AFTER the add, the repo-rel paths the promote will commit
    (the module + the method's generated target), the enact commands, the Grafana `links` to surface after
    promote (#122 — read-only: the dashboard uids from the committed JSON + the deploy's KONTROLL_MGMT_IP/
    KONTROLL_DOMAIN env for the base; [] when neither is set, never a fabricated host), and the propose→promote
    `plan_token` (a hash of the declared block being written — the targets are generated FROM it, so they cannot
    drift independently). ADD-ONLY: a method already declared on the class is `already_declared` (idempotent).
    `key` and `dashboards` are REQUEST values: the key is refused first (paths.RequestBoundaryError — before the
    join, and before any answer that would say whether some other directory holds a module.yml), and a dashboards
    selection outside the closed `{gnet, name}` shape is `bad_param` before anything is rendered or read."""
    paths.component(key, "device-class key")            # C22: the request boundary, before the join
    methods = methods if methods is not None else catalog.load_telemetry()
    by_name = {m["name"]: m for m in methods}
    mod_rel = "modules/%s/module.yml" % key   # repo-rel paths are POSIX (git + the scoped commit use them)
    mod_path = os.path.join(paths.ROOT, mod_rel)
    if not os.path.exists(mod_path):
        return {"error": "not_onboarded", "key": key}
    method = by_name.get(method_name)
    if method is None:
        return {"error": "unknown_method", "key": key, "method": method_name}
    bad = validate_params(method, params)
    if bad:
        return {"error": "bad_param", "key": key, "method": method_name, "detail": bad}
    dashboards, bad = _clean_dashboards(dashboards, _declared_dashboards() if dashboards else None)
    bad = bad or _foreign_dashboard_file(dashboards)
    if bad:
        return {"error": "bad_param", "key": key, "method": method_name, "detail": bad}

    with open(mod_path, encoding="utf-8") as fh:
        text_before = fh.read()
    module = yaml.safe_load(text_before) or {}
    # RECONFIGURE (Phase 4a): a declared method is no longer a wall — read the current values + DIFF the
    # proposal, then ADD (new), no-op (same value), or UPSERT in place (changed value). The diff/severity drive
    # the operator's overwrite-confirm + the token's no-quiet-upgrade fold; verify_no_drop is the spine's
    # machine anti-clobber gate at promote (design 22 §4/§5/§7).
    current = current_values(key)
    proposed = dict(current)
    proposed[method_name] = params or {}
    diff = _diff.compute_changes(current, proposed)
    new_lines = [_render_metrics_entry(method_name, params)]
    if not diff["changes"]:
        text_after = text_before                               # idempotent no-op (same value) — writes nothing
    elif method_name in current:
        text_after = upsert_into_block(text_before, "metrics", method_name, new_lines)   # reconfigure (modify)
    else:
        text_after = insert_into_block(text_before, "metrics", new_lines)                # first declare (add)
    if dashboards:
        existing_dash = {d.get("name") for d in (module.get("dashboards") or [])}
        dash_lines = [_render_dashboard_entry(d) for d in dashboards if d.get("name") not in existing_dash]
        if dash_lines:
            text_after = insert_into_block(text_after, "dashboards", dash_lines)

    # Grafana links cover the WHOLE class's curated dashboards (already-declared ∪ newly-selected), deduped by
    # name, so the operator gets a deep-link even when this promote added no new dashboard (#122).
    all_dash = list({d.get("name"): d for d in ((module.get("dashboards") or []) + (dashboards or []))
                     if d.get("name")}.values())
    target_rel = "prometheus/targets/%s/%s.generated.yml" % (method["job"], key)
    sev_dec = _diff.severity_decision(diff)
    return {"error": None, "key": key, "method": method_name, "params": params or {},
            "dashboards": dashboards or [], "mod_rel": mod_rel, "mod_path": mod_path,
            "text_before": text_before, "text_after": text_after,
            "changes": diff["changes"], "will_overwrite": diff["will_overwrite"], "severity": diff["severity"],
            "current": current, "proposed": proposed,   # the structs verify_no_drop checks at promote
            "regen_paths": [target_rel], "paths": [mod_rel, target_rel],
            "enact": telemetry_enact_commands(key, method, bool(dashboards)),
            "links": telemetry_grafana_links(_grafana_base(), all_dash),   # Grafana deep-links for the dialog (#122)
            "token_parts": [text_after, sev_dec],   # text_after (anti-drift) + the severity decision (no-quiet-upgrade, M1)
            "plan_token": promote.plan_token(text_after, sev_dec)}


# --- the seam's uniform service interface (the capability spine dispatches to these by convention) ----- #
def offerable_methods(facts):
    """The Stage-1 picker PAYLOAD for these facts — the `applies_when`-filtered methods, each as
    {name, label, params}, where `params` is the method's CLOSED allow-list schema ({param: {allowed,
    required}}) so the dialog can render a param picker (and the operator never types a free value). The
    seam's optional `offerable_methods(facts)` convention: the spine calls it ONLY when render_method_stage is
    true; an instance with a skipped picker (backup) simply doesn't define it. Telemetry-specific, so it lives
    here (the instance) — the neutral spine never imports telemetry filtering."""
    from kontroll.service.classify import applicable_methods   # local import — telemetry-specific, instance-owned
    out = []
    for m in applicable_methods(facts):
        params = {p: {"allowed": list(spec.get("allowed") or []), "required": bool(spec.get("required"))}
                  for p, spec in (m.get("params") or {}).items()}
        out.append({"name": m["name"], "label": m.get("label", m["name"]), "params": params,
                    "secret_domain": m.get("secret_domain")})   # advisory cred-prereq surfaced in the cap dialog (G3)
    return out


def current_values(key):
    """The DECLARED telemetry values of class `key`, for pre-filling the reconfigure renderer + the diff (Phase
    4a). PURE — reads modules/<key>/module.yml via paths.ROOT, writes nothing; returns {method_name: {param:
    value}} per declared `metrics:` entry. {} if not onboarded / no metrics block (degrade, never raise —
    service-never-sys.exit). The reconfigure read-back (design 22 §3.1)."""
    mod_path = paths.module_file(key)
    try:
        with open(mod_path, encoding="utf-8") as fh:
            module = yaml.safe_load(fh.read()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {e["method"]: (e.get("params") or {}) for e in (module.get("metrics") or []) if e.get("method")}


def build_plan(key, selection):
    """The capability-NEUTRAL propose entrypoint the spine (capability.py) calls — unpacks the dialog's
    `selection` ({method, params, dashboards}) into build_telemetry_plan. Every instance module exposes this
    exact signature, so the spine never branches on the capability."""
    return build_telemetry_plan(key, selection.get("method"),
                                params=selection.get("params"), dashboards=selection.get("dashboards"))


def apply_plan(plan):
    """The capability-NEUTRAL promote entrypoint the spine calls — the telemetry write + regen."""
    return apply_telemetry_plan(plan)


# --- PROMOTE (the data write + regen) ------------------------------------------------------------------ #
def apply_telemetry_plan(plan):
    """Write the telemetry block into modules/<key>/module.yml (comment-preserving) and REGENERATE the
    Prometheus targets from it. Idempotent: identical content → no write. Returns {"changed", "paths"} — the
    repo-rel paths the route's scoped commit stages (the module + the class's regenerated target files). The
    API runs NO play here; making metrics flow is the operator's enact step (telemetry_enact_commands)."""
    changed = False
    with open(plan["mod_path"], encoding="utf-8") as fh:
        if fh.read() != plan["text_after"]:
            with open(plan["mod_path"], "w", encoding="utf-8", newline="\n") as out:
                out.write(plan["text_after"])
            changed = True
            log.info("observe: wrote %s telemetry block for %s", plan["method"], plan["key"])
    regen = regenerate_observability(plan["key"])
    paths_changed = [plan["mod_rel"]] + regen
    return {"changed": changed or bool(regen), "paths": sorted(set(paths_changed))}


def regenerate_observability(key):
    """Re-run gen-observability against the real tree so the committed targets match the new metrics block,
    and return the class's regenerated target paths (repo-rel) for the scoped commit. SELF-PROTECTING: if
    paths.ROOT has been repointed away from the generators' real-tree root (a tmp_repo test), this is a no-op
    — a test asserts the block write without invoking the real generators against the real repo. In
    production paths.ROOT IS the real tree, so the regen runs and stays consistent with the --check guard."""
    gen = _gen_observability()
    if os.path.normpath(paths.ROOT) != os.path.normpath(gen.ROOT):
        log.warning("observe: paths.ROOT (%s) != generator root (%s) — skipping regen (test/sandbox)",
                    paths.ROOT, gen.ROOT)
        return []
    # Phase B (bake-code): point the generator at the WRITE clone (paths.write_root()) so a baked deploy regenerates
    # into /propose — NOT the read-only baked /opt/kontroll (chmod -R a-w would make gen.main() fail). Identity when
    # write_root()==ROOT (the legacy /repo deploy + deploy-stack, which runs the generators directly with ROOT already
    # the writable clone). gen reads modules/instance + writes prometheus/ all relative to gen.ROOT, so this one
    # reassignment redirects the whole regen to the clone (the operator's proposed state + the writable target).
    gen.ROOT = paths.write_root()
    gen.main([])   # writes prometheus/targets/*/*.generated.yml under write_root() (idempotent; prunes orphans)
    return sorted(os.path.relpath(p, gen.ROOT).replace(os.sep, "/")
                  for p in glob.glob(os.path.join(gen.ROOT, "prometheus", "targets", "*", "%s.generated.yml" % key)))


# --- ENACT (hand-off strings; the API runs none) ------------------------------------------------------- #
def telemetry_enact_commands(key, method, has_dashboards):
    """The steps to make the just-promoted telemetry LIVE — the API executes none (the data write is committed;
    nothing actuates until these run). Each step is `{kind, why, …}` (item F):
      * kind 'semaphore' — a one-click Semaphore task (Tier-1, no host root): `task` (+ optional `args`) names it;
        `cmd` is the equivalent CLI fallback. The host-agent install play + the Prometheus config reload.
      * kind 'operator' — a shell command the operator runs on the control node (Tier-2, needs host Docker):
        bringing up a proxy-exporter container / provisioning dashboards via deploy-stack.
    Tailored to the method's kind: a host-agent needs its install play; a proxy-exporter needs deploy-stack;
    dashboards need fetch-dashboards; every telemetry add ends with a Prometheus config reload."""
    cmds = []
    if method.get("kind") == "host-agent" and method.get("agent_role"):
        role = method["agent_role"].replace("_", "-")
        cmds.append({"kind": "semaphore", "task": "install-%s" % role, "args": {"target": key},
                     "cmd": "ansible-playbook ansible/playbooks/install-%s.yml -e target=%s --check --diff"
                            % (role, key),
                     "why": "install the %s agent (Semaphore task — tick Dry Run for the --check pass, then "
                            "apply)" % method["agent_role"]})
    if method.get("kind") == "proxy-exporter" and method.get("exporter"):
        cmds.append({"kind": "operator",
                     "cmd": "cd ansible && ansible-playbook playbooks/deploy-stack.yml -e @/tmp/sv.yml",
                     "why": "bring up the %s exporter container (operator-run — needs host Docker)"
                            % method["exporter"].get("container_name", method["name"])})
    if has_dashboards:
        cmds.append({"kind": "operator",
                     "cmd": "python3 scripts/fetch-dashboards.py && cd ansible && "
                            "ansible-playbook playbooks/deploy-stack.yml -e @/tmp/sv.yml",
                     "why": "fetch + provision the curated Grafana dashboards (operator-run — needs host Docker)"})
    cmds.append({"kind": "semaphore", "task": "reload-observability",
                 "cmd": "ansible-playbook ansible/playbooks/reload-observability.yml",
                 "why": "reload Prometheus so it picks up the new scrape target (Semaphore task — HTTP reload, "
                        "no restart)"})
    return cmds
