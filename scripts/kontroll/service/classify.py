"""classify domain — which execution backend(s) fit a collection, and (read-only) whether it looks scrapable.

service_classify returns the ordered backend matches plus the best backend's name + requirements (or None
when not installed locally), AND the telemetry side: declared_metrics_methods reconciles what module.yml
ALREADY declares; suggest_telemetry derives a NON-binding scrape suggestion from the telemetry capability
vector. Both telemetry helpers are PURE (read the repo via paths.ROOT) and write NOTHING — the DECLARED
method in module.yml is the source of truth, the vector is only a hint. No actuation, no new trust boundary.
"""
import logging
import os

import yaml

from kontroll import catalog, paths, predicate, probe

log = logging.getLogger("kontroll.service.classify")

# Suggestion map: a DETECTED collection plugin -> the telemetry method(s) an operator would pick. DATA, not
# logic — extend by editing this dict. It is the suggestion layer, distinct from the telemetry/<method>.yml
# GENERATION registry. host_node is always a *possible* manual pick (a host MIGHT run node_exporter) but is
# NEVER suggested first — no collection fact proves the host runs an agent.
_TELEMETRY_HINT = {
    "httpapi": ["proxy-exporter (api)", "host_node"],   # REST device/service: an API exporter; host if a box
    "cliconf": ["snmp", "blackbox", "host_node"],       # network_cli device: agent-less snmp/blackbox
    "netconf": ["snmp"],
}

# Logging suggestion map: a DETECTED collection plugin -> the logging method(s) an operator would pick. DATA,
# not logic. THIN by design: only a CLI/NETCONF management plane is a real "can ship syslog" signal;
# journald_remote / file_tail_ssh / rest_pull are operator-DECLARED, never suggested (the dominant log sources
# are host/runtime facts no probe sees, like host_node). The canonical copy lives in capabilities/logging.yml
# suggester.hint; a test pins _LOGGING_HINT == that descriptor field so they cannot drift.
_LOGGING_HINT = {
    "cliconf": ["syslog_push"],   # network_cli gear: point its syslog at the collector
    "netconf": ["syslog_push"],
}


def service_classify(collection, backends=None):
    """Deep-probe `collection` and return {"collection","matches","best","requires","telemetry_declared"}
    (matches ordered best-first; best/requires describe the top backend; telemetry_declared is the
    DECLARED-side reconciliation), or None if the collection isn't installed locally (CLI owns the message)."""
    backends = backends if backends is not None else catalog.load_backends()
    f = probe.deep_probe(collection)
    if not f["modules"] and not f["plugins"]:
        return None
    matches = predicate.classify(f, backends)
    best = next((b for b in backends if b["name"] == matches[0]), None) if matches else None
    return {"collection": collection, "matches": matches,
            "best": best["name"] if best else None,
            "requires": (best.get("requires") or []) if best else [],
            "telemetry_declared": declared_metrics_methods(collection)}


def declared_metrics_methods(collection, fleet=None):
    """Which enabled device-class module(s) declare a `metrics:` block whose collection is `collection` — the
    DECLARED side of telemetry, read from modules/<key>/module.yml. Returns a sorted list of
    {key, methods:[<method-or-job names>]} (possibly empty). PURE read of the repo (paths.ROOT-bound, so
    tmp_repo can repoint it); no probe, no I/O beyond reading module.yml + fleet.yml. This is the bridge
    between a DETECTED telemetry capability (the vector) and what the operator has ALREADY wired."""
    out = []
    if fleet is None:
        fp = paths.resolve("config/fleet.yml")
        fleet = (yaml.safe_load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}) or {}
    for key in paths.enabled_keys(fleet):
        mp = paths.module_file(key)
        if not os.path.exists(mp):
            continue
        mod = yaml.safe_load(open(mp, encoding="utf-8")) or {}
        colls = {c.get("name") for c in (mod.get("collections") or [])}
        if collection in colls and mod.get("metrics"):
            # tolerate BOTH schemas: the registry {method, params} and a legacy {job, via, port}.
            methods = sorted({m.get("method") or m.get("job") or "?" for m in (mod.get("metrics") or [])})
            out.append({"key": key, "methods": methods})
    log.debug("telemetry declared for %s: %s", collection, [d["key"] for d in out])
    return sorted(out, key=lambda d: d["key"])


def applicable_methods(facts, methods=None):
    """The telemetry methods OFFERABLE for a device's facts — the Stage-1 picker filter the capability
    dialog renders. A method is offerable unless its `applies_when` predicate is definitively False; a method
    with NO applies_when is UNIVERSAL (host_node, blackbox — any box can run an agent / be pinged). PURE, and
    reuses predicate.eval_pred — the SAME three-valued engine the vectors fold (a true drop-in, no new
    grammar). Deliberately PERMISSIVE: unknown (None, at a shallow probe) keeps a method visible, and it
    never promises the method will succeed (a creds/reach failure blanks a panel, not the lab). The picker
    UNIONs this with what's already DECLARED, so an operator-wired method (snmp on the httpapi FortiGate)
    still shows even though applies_when alone wouldn't pick it."""
    methods = methods if methods is not None else catalog.load_telemetry()
    out = []
    for m in methods:
        pred = m.get("applies_when")
        if pred is None or predicate.eval_pred(pred, facts) is not False:
            out.append(m)
    log.debug("applicable methods for %s: %s", facts.get("collection"), [m["name"] for m in out])
    return out


# --- F2 de-bespoke: AUTO-DERIVE a class's capability FLOOR from the conferred + universal methods --------------- #
# MF-S5 — the auto-derivability SECRET posture. A method is auto-derivable ONLY if it needs NO PER-CLASS secret: its
# secret_domain is null/absent, OR it reuses one of these SHARED, already-provisioned, READ-ONLY domains. snmp's
# SNMPv3 USM creds are provisioned ONCE for the whole fleet (control + break-glass) and only ever GET, never actuate —
# so reusing them on a blind device adds no per-class secret. A method with a PER-CLASS secret (pve->proxmox,
# rest_pull->its own token) is NEVER in this set, so it can never be silently auto-attached to a blind device with an
# unfilled credential — the never-fake invariant enforced in CODE, not just by a test. (test_derive_creds_free_invariant.)
SHARED_DERIVABLE_SECRET_DOMAINS = frozenset({"snmp_observability"})


def _is_creds_free_to_derive(method):
    """MF-S5: True iff `method` is safe to AUTO-attach — it carries no PER-CLASS secret (secret_domain null/absent,
    or a SHARED already-provisioned read-only domain). The auto-derivability criterion in disguise (the universal
    floor is creds-free by construction; a per-class-secret method is exactly the non-derivable one)."""
    dom = method.get("secret_domain")
    return dom is None or dom in SHARED_DERIVABLE_SECRET_DOMAINS


def _derive_methods(facts, conferred, registry):
    """The auto-derived `{method, params?}` entries for ONE capability: for each method NAME the backend confers,
    emit it iff the method (a) OPTS IN to derivation (`derive_default` present — the §4.2 switch), (b) is creds-free
    to derive (MF-S5), (c) is not DEFINITIVELY ruled out by its applies_when (3-valued: True/None keep it, the SAME
    permissive posture as applicable_methods), and (d) has a derive_default value for every REQUIRED param
    (fail-honest: a method missing a universal default can only be operator-DECLARED, never faked). PURE — reuses
    predicate.eval_pred, no I/O. Order follows the conferred list (stable, pinnable)."""
    by_name = {m.get("name"): m for m in registry}
    out = []
    for name in (conferred or []):
        m = by_name.get(name)
        if m is None:
            continue
        dd = m.get("derive_default")
        if dd is None or not _is_creds_free_to_derive(m):
            continue
        pred = m.get("applies_when")
        if pred is not None and predicate.eval_pred(pred, facts) is False:
            continue
        params = dict(dd.get("params") or {})
        required = [p for p, spec in (m.get("params") or {}).items()
                    if isinstance(spec, dict) and spec.get("required")]
        if any(r not in params for r in required):
            continue                                   # no universal default for a required param — stays declared
        entry = {"method": name}
        if params:
            entry["params"] = params
        out.append(entry)
    return out


def _derive_dashboards(derived_metrics, telemetry, locks):
    """The AUTO-DERIVED `dashboards:` FLOOR (north-star Rung 4a): for each method ACTUALLY derived into `metrics`
    that carries a `derive_dashboard:` selector AND has a resolved lock (dashboards/derived/<method>.lock.yml), emit
    ONE {gnet, name} from the lock. Deduped by gnet. The board RIDES the metrics floor — a class that didn't derive a
    method (a switch derives no host_node) never gets that method's board, so a node board is never hung off a class
    with no node_exporter series (the honesty guard, §8). NEVER fabricates: no selector OR no lock ⇒ no board (the
    honest gap names it). Order follows `derived_metrics` (stable, pinnable, order-sensitive like metrics/logs).
    PURE — reads only the passed derived_metrics + telemetry registry + locks; no network, no I/O."""
    by_name = {m.get("name"): m for m in telemetry}
    out, seen = [], set()
    for e in derived_metrics:
        m = by_name.get(e["method"])
        if not m or not m.get("derive_dashboard"):
            continue
        lock = (locks or {}).get(e["method"])
        if not lock or not lock.get("id"):            # unresolved selector ⇒ no board (fail-honest, never faked)
            continue
        gnet = int(lock["id"])
        if gnet in seen:
            continue
        seen.add(gnet)
        out.append({"gnet": gnet, "name": lock.get("name")})
    return out


def derive_class_capabilities(facts, backend, *, telemetry=None, logging=None, backends=None, locks=None):
    """The AUTO-DERIVED metrics:/logs: FLOOR for a freshly-classified device — the conferred-capability analogue of
    applicable_methods, RESOLVED to a concrete, pinnable block (not just an offered list). The de-bespoke core (F2):
    a blind-onboarded class reaches the SAME universal floor (agent-less snmp/blackbox metrics, syslog logs) the
    curated classes hand-type, with ZERO curation — closing the empty-Grafana / empty-Loki defect — plus an HONEST
    `gap` naming the vendor-specific richness it cannot derive (never a faked exporter). PURE: reads only `facts`
    (deep_probe output) + the static registries; no probe, no network, no I/O beyond the loaders. Returns
    {"metrics":[{method,params?}...], "logs":[...], "gap":[{method,capability,why}...]}.

    (Backup is intentionally NOT derived here: it is ALREADY conferred via the role gate that gen-backup checks
    [confers.backup + roles/<role>/tasks/backup.yml], a filesystem signal outside this pure function. So F2's floor
    is exactly the two capabilities that were EMPTY for a blind class — metrics + logs.)

    `dashboards` (Rung 4a): the DERIVED Grafana-board floor — for each derived metric method carrying a
    `derive_dashboard:` selector with a resolved lock, the {gnet, name} its board pinned to. OPTIONAL key (every
    reader uses `.get("dashboards", [])`); the onboard path does NOT write it (INVARIANT-D — a board rides the
    out-of-band-resolved locks, not the offline creds-free onboard floor). `locks` defaults to the loaded pins."""
    telemetry = telemetry if telemetry is not None else catalog.load_telemetry()
    logging = logging if logging is not None else catalog.load_logging()
    backends = backends if backends is not None else catalog.load_backends()
    locks = locks if locks is not None else catalog.load_dashboard_locks()
    metrics = _derive_methods(facts, catalog.backend_confers(backend, "telemetry", backends) or [], telemetry)
    logs = _derive_methods(facts, catalog.backend_confers(backend, "logs", backends) or [], logging)
    dashboards = _derive_dashboards(metrics, telemetry, locks)
    return {"metrics": metrics, "logs": logs, "dashboards": dashboards,
            "gap": derive_gap(facts, metrics, logs, telemetry, logging, locks)}


def derive_gap(facts, derived_metrics, derived_logs, telemetry=None, logging=None, locks=None):
    """The HONEST residual — methods whose applies_when is DEFINITIVELY True for these facts (a POSITIVE probe
    signal, like pve's `module_suffix: proxmox_kvm`) but which were NOT auto-derived (vendor-narrow / creds-bearing /
    no derive_default). These are the curated rungs a blind class is MISSING and must DECLARE to reach full parity;
    surfaced (non-gating, INVARIANT D*) so the operator is told what richness to add, never a silent blank. A method
    with NO applies_when (universal, like host_node) is never nagged — only a positive signal flags a gap. For a
    full-parity class (cisco_ios: snmp/blackbox/syslog all derive) the gap is empty."""
    telemetry = telemetry if telemetry is not None else catalog.load_telemetry()
    logging = logging if logging is not None else catalog.load_logging()
    got_m = {e["method"] for e in derived_metrics}
    got_l = {e["method"] for e in derived_logs}
    gap = []
    for cap, registry, got in (("telemetry", telemetry, got_m), ("logs", logging, got_l)):
        for m in registry:
            pred = m.get("applies_when")
            if pred is not None and predicate.eval_pred(pred, facts) is True and m.get("name") not in got:
                gap.append({"method": m.get("name"), "capability": cap,
                            "why": "applies to this device but is not auto-derivable (vendor-specific or "
                                   "credentialed) — declare it in the module's %s: block to enable it" % cap})
    # dashboard gap (Rung 4a): a method WE DERIVED into metrics that declares a `derive_dashboard:` selector but has
    # NO resolved lock — the class scrapes the series yet carries no board (honest, never a faked id). All floor
    # selectors ship resolved, so this is empty in practice; it names a NEW selector added without a --resolve run.
    if locks is not None:
        tel_by_name = {m.get("name"): m for m in telemetry}
        for e in derived_metrics:
            m = tel_by_name.get(e["method"])
            if m and m.get("derive_dashboard") and not ((locks.get(e["method"]) or {}).get("id")):
                gap.append({"method": e["method"], "capability": "dashboard",
                            "why": "this method's derive_dashboard: selector has no resolved board lock — run "
                                   "gen-dashboard-floor.py --resolve; the series scrapes but has no dashboard"})
    return gap


def suggest_telemetry(facts, vectors, hint=None):
    """The DETECTED telemetry suggestion for a collection's facts: the telemetry vector cell + a NON-binding
    list of candidate method names an operator could declare. PURE — writes nothing. Returns
    {cell:{state,confidence,evidence}, candidates:[str], note:str}; candidates is empty when the cell is 'no'.
    host_node is only ever a trailing/manual candidate (no collection fact proves the host runs an agent);
    the chosen method's GENERATION lives in the telemetry-method registry, not here. `hint` is the
    plugin->candidate-method map: the capability seam passes capabilities/telemetry.yml's `suggester.hint`
    (so the descriptor is the canonical copy), and the lower-level onboard-nudge caller omits it to use the
    built-in _TELEMETRY_HINT default — a test pins the two equal so they cannot drift."""
    hint = hint if hint is not None else _TELEMETRY_HINT
    vec = next((v for v in vectors if v["name"] == "telemetry"), None)
    cell = predicate.eval_vector(vec, facts) if vec else {"state": "no", "confidence": None, "evidence": None}
    candidates = []
    if cell["state"] in ("yes", "maybe"):
        for plug in ("httpapi", "cliconf", "netconf"):
            if facts["plugins"].get(plug):
                for m in hint.get(plug, []):
                    if m not in candidates:
                        candidates.append(m)
    note = ("scrapable — pick a telemetry method to monitor it" if candidates
            else "no scrape-shaped signal; host-agent (host_node) is the only option if it is a host")
    log.debug("telemetry suggest %s -> state=%s candidates=%s", facts["collection"], cell["state"], candidates)
    return {"cell": cell, "candidates": candidates, "note": note}


def declared_backup(collection, fleet=None):
    """Which enabled device-class module(s) declare a backup-CAPABLE `backup:` block whose collection is
    `collection` — the DECLARED side of backup (the sibling of declared_metrics_methods, for instance #2).
    Returns sorted [{key, methods:["backup"]}] (a class backs up exactly ONE way — its role entrypoint — so
    the method is always the literal 'backup'). PURE (paths.ROOT-bound); reads module.yml + fleet.yml only."""
    out = []
    if fleet is None:
        fp = paths.resolve("config/fleet.yml")
        fleet = (yaml.safe_load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}) or {}
    for key in paths.enabled_keys(fleet):
        mp = paths.module_file(key)
        if not os.path.exists(mp):
            continue
        mod = yaml.safe_load(open(mp, encoding="utf-8")) or {}
        colls = {c.get("name") for c in (mod.get("collections") or [])}
        bk = mod.get("backup") or {}
        if collection in colls and bk.get("capable"):
            out.append({"key": key, "methods": ["backup"]})
    log.debug("backup declared for %s: %s", collection, [d["key"] for d in out])
    return sorted(out, key=lambda d: d["key"])


def suggest_backup(facts, vectors, hint=None):
    """The DETECTED backup suggestion for a collection's facts: the backup vector cell + a NON-binding
    candidate (the single per-class role-entrypoint capture). PURE — writes nothing. Mirrors suggest_telemetry
    so the capability seam dispatches both identically; `hint` is unused (backup has ONE capture path, not a
    plugin->method map) but kept for the uniform suggester signature. Returns {cell, candidates, note}."""
    vec = next((v for v in vectors if v["name"] == "backup"), None)
    cell = predicate.eval_vector(vec, facts) if vec else {"state": "no", "confidence": None, "evidence": None}
    capable = cell["state"] in ("yes", "maybe")
    candidates = ["per-class config backup"] if capable else []
    note = ("config-capturable — schedule a per-class backup" if capable
            else "no backup signal; only a class with a role backup entrypoint can be scheduled")
    log.debug("backup suggest %s -> state=%s", facts.get("collection"), cell["state"])
    return {"cell": cell, "candidates": candidates, "note": note}


# --- logging (instance #3) — the DECLARED + DETECTED halves, mirroring the telemetry twins ----------------- #
def declared_logs_methods(collection, fleet=None):
    """Which enabled device-class module(s) declare a `logs:` block whose collection is `collection` — the
    DECLARED side of logging, read from modules/<key>/module.yml. Returns a sorted list of
    {key, methods:[<method names>]} (possibly empty). PURE read (paths.ROOT-bound, so tmp_repo can repoint it);
    no probe. The bridge between a DETECTED logging capability (the vector) and what the operator has ALREADY
    wired. Mirror of declared_metrics_methods; the `logs:` block is a LIST like `metrics:` (block_shape: list),
    so this iterates and collects (UNLIKE declared_backup's single-mapping read)."""
    out = []
    if fleet is None:
        fp = paths.resolve("config/fleet.yml")
        fleet = (yaml.safe_load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}) or {}
    for key in paths.enabled_keys(fleet):
        mp = paths.module_file(key)
        if not os.path.exists(mp):
            continue
        mod = yaml.safe_load(open(mp, encoding="utf-8")) or {}
        colls = {c.get("name") for c in (mod.get("collections") or [])}
        if collection in colls and mod.get("logs"):
            methods = sorted({m.get("method") or "?" for m in (mod.get("logs") or [])})
            out.append({"key": key, "methods": methods})
    log.debug("logging declared for %s: %s", collection, [d["key"] for d in out])
    return sorted(out, key=lambda d: d["key"])


def suggest_logging(facts, vectors, hint=None, evidence=None):
    """The DETECTED logging suggestion for a collection's facts: the logging vector cell + a NON-binding list
    of candidate method names. PURE — writes nothing. Returns {cell, candidates, note} (+ an optional
    `evidence` key ONLY when a probe sharpened the note — see below). DELIBERATELY WEAKER than
    suggest_telemetry: only a CLI/NETCONF management plane is a collection-visible log-export signal, so only
    `syslog_push` is ever auto-suggested; the dominant methods (journald_remote / file_tail_ssh, and
    rest_pull) are DECLARED in module.yml, never detected — the UNIVERSAL bucket, mirroring host_node. The
    picker UNIONs candidates with what's already DECLARED, so a declared method shows even when the vector is
    silent. `hint` is the plug->candidate map (the capability seam passes capabilities/logging.yml's
    suggester.hint; the onboard-nudge caller omits it to use _LOGGING_HINT — a test pins the two equal).

    `evidence` (S6, graceful-export probe — OPTIONAL, read-only, NON-GATING): an external read-only probe
    result `{state: yes|maybe|no, note}` or None. INVARIANT-D: it can ONLY re-word the note of an
    ALREADY-derived candidate — it never adds/removes a candidate, never gates onboarding, and is NEVER passed
    on the onboard/spine path (capability.suggest calls this with three positional args, so evidence is None
    there). With `evidence=None` (the default) the output is BYTE-IDENTICAL to before — the regression pin
    test_suggest_logging_probe.py guards exactly that. The probe therefore can never become a de-facto gate."""
    hint = hint if hint is not None else _LOGGING_HINT
    vec = next((v for v in vectors if v["name"] == "logging"), None)
    cell = predicate.eval_vector(vec, facts) if vec else {"state": "no", "confidence": None, "evidence": None}
    candidates = []
    if cell["state"] in ("yes", "maybe"):
        for plug in ("cliconf", "netconf"):
            if facts["plugins"].get(plug):
                for m in hint.get(plug, []):
                    if m not in candidates:
                        candidates.append(m)
    note = ("log-exportable — point the device's syslog at the collector (pick a logging method)" if candidates
            else "no log-export-shaped signal; journald/file/docker sources are operator-declared in logs:")
    out = {"cell": cell, "candidates": candidates, "note": note}
    # SHARPEN-ONLY (S6): an external probe result re-words the note of a candidate the vector+hint ALREADY
    # produced. It never manufactures or removes a candidate (so it cannot become a gate), and with no candidate
    # there is nothing to sharpen — an evidence-with-no-candidate is silently ignored (the class stays offerable).
    # The `evidence` key surfaces ONLY here, so the default (evidence=None) path keeps the exact 3-key contract.
    if evidence and candidates:
        state = evidence.get("state")
        if state == "yes":
            out["note"] = "log-exportable (probe: device already accepts a syslog host) — pick a logging method"
        elif state == "no":
            out["note"] = note + "  (probe: no syslog-host config detected; still offerable)"
        out["evidence"] = evidence
    log.debug("logging suggest %s -> state=%s candidates=%s evidence=%s",
              facts.get("collection"), cell["state"], candidates, bool(evidence))
    return out
