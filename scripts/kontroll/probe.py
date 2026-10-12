"""Probers: turn a collection into capability FACTS at one of two depths.

SHALLOW — from the Galaxy `contents` list (no install needed); DEEP — from
`ansible-doc -j` against a locally-installed collection (full module/plugin/option
signals). `_ad` (the ansible-doc shell-out) and `deep_probe` are the I/O SEAM the
tests monkeypatch (kontroll.probe._ad / kontroll.probe.deep_probe) to run offline.
Lifted verbatim from galaxy.py.
"""
import json
import logging
import os
import subprocess

from kontroll import paths

from kontroll.paths import PLUGIN_TYPES

log = logging.getLogger("kontroll.probe")


def _facts(coll, version, origin, depth):
    return {"collection": coll, "version": version, "origin": origin, "depth": depth,
            "description": "", "tags": [], "modules": [], "plugins": {},
            "module_options": {}, "certified": False}


def shallow_from_galaxy(cv, certified):
    f = _facts("%s.%s" % (cv["namespace"], cv["name"]), cv.get("version"), "galaxy", "shallow")
    f["description"] = cv.get("description") or ""
    # Galaxy returns tags either as plain strings or as objects ({"name": "..."}); normalize to strings so
    # the typed Record (meta.tags: list[str]) serializes — object-tags otherwise 500 the API's /search.
    f["tags"] = [t.get("name", "") if isinstance(t, dict) else str(t) for t in (cv.get("tags") or [])]
    f["certified"] = certified
    for c in cv.get("contents") or []:
        ct, nm = c.get("content_type"), c.get("name")
        if ct == "module":
            f["modules"].append(nm)
        elif ct in PLUGIN_TYPES:
            f["plugins"].setdefault(ct, []).append(nm)
    return f


def _names_in(plugin_dir):
    """Short names of the .py/.ps1 plugin/module files under `plugin_dir` (walked recursively), minus
    __init__ — the same short-name set `ansible-doc -l/-t -l` reports, but read straight from the
    filesystem so no ansible-doc subprocess is spawned. Verified zero-divergence vs ansible-doc across
    the real network fleet (arista.eos/cisco.ios/fortinet.fortios/ansible.netcommon)."""
    out = set()
    if not os.path.isdir(plugin_dir):
        return []
    for _root, _dirs, files in os.walk(plugin_dir):
        for fn in files:
            base, ext = os.path.splitext(fn)
            if ext in (".py", ".ps1") and base != "__init__":
                out.add(base)
    return sorted(out)


def shallow_from_local(coll, version, base_path):
    """SHALLOW-probe an INSTALLED collection from its files alone (no ansible-doc): modules + plugins from
    walking plugins/, metadata from MANIFEST.json. `module_options` stays {} — that is the ONE signal that
    needs the deep per-module doc parse — so a `module_option` rule reads as unknown-at-depth (-> 'maybe'),
    never a wrong yes/no. This is the fast path the redesigned /search uses by default; deep_probe resolves
    the deferred cells on demand. `base_path` is the ansible_collections dir that holds the collection."""
    ns, name = paths.collection_fqcn(coll).split(".", 1)      # SEC-3: never an unvalidated name into a join
    cdir = os.path.join(base_path, ns, name)
    f = _facts(coll, version, "local", "shallow")
    try:
        with open(os.path.join(cdir, "MANIFEST.json"), encoding="utf-8") as fh:
            ci = json.load(fh).get("collection_info") or {}
        f["description"] = ci.get("description") or ""
        f["tags"] = [str(t) for t in (ci.get("tags") or [])]
        f["version"] = version or ci.get("version")
    except (OSError, ValueError):           # no/garbled MANIFEST -> metadata stays empty, signals still read
        pass
    f["modules"] = _names_in(os.path.join(cdir, "plugins", "modules"))
    for t in PLUGIN_TYPES:
        plug = _names_in(os.path.join(cdir, "plugins", t))
        if plug:
            f["plugins"][t] = plug
    return f


# ── RUNNABLE-UNIT enumeration (the app-store "search → unit" seam, R1) ───────────────────────────────────────────
# A collection's RUNNABLE units (directly launchable as a play) are its collection-shipped playbooks and its roles —
# NOT its modules (a module is a task, not runnable on its own). The worked extraction
# (docs/reviews/2026-06-24-gui-actuation-design/40-...) measured that kontroll's device-management fleet ships modules
# and ~zero roles/playbooks, so for those collections this correctly returns empty. Read-only, files-only (no
# ansible-doc / no subprocess) — the same shallow, monkeypatch-free filesystem grain as shallow_from_local.

# the CLOSED kind set R1 enumerates (the design's third kind, `standalone` in-repo playbooks, is deferred — it is a
# repo-global listing, not per-collection, so it does not ride GET /units/{collection}).
UNIT_KINDS = ("collection_playbook", "role")


def _playbook_names(pdir):
    """Sorted base names (sans extension) of the collection-shipped playbooks directly under `pdir` — the
    top-level `playbooks/*.yml|*.yaml` files, which are the ones addressable as the `namespace.name.<playbook>`
    runnable FQCN. Non-recursive on purpose; a missing dir yields []."""
    if not os.path.isdir(pdir):
        return []
    out = set()
    for fn in os.listdir(pdir):
        base, ext = os.path.splitext(fn)
        if ext in (".yml", ".yaml") and os.path.isfile(os.path.join(pdir, fn)):
            out.add(base)
    return sorted(out)


def _role_names(rdir):
    """Sorted names of the role directories directly under `rdir` (each `roles/<name>/` that is itself a dir);
    a missing dir yields []."""
    if not os.path.isdir(rdir):
        return []
    return sorted(d for d in os.listdir(rdir) if os.path.isdir(os.path.join(rdir, d)))


def units_in_collection(coll, base_path):
    """The runnable units of the INSTALLED collection `coll` (e.g. `cisco.ios`), read from its files alone:
    `{"collection_playbook": [...names...], "role": [...names...]}`. `base_path` is the `ansible_collections`
    search dir that holds the collection — the SAME `cdir = base_path/ns/name` shallow_from_local computes.

    CALLER CONTRACT (SEC-3): `coll` MUST already be a validated installed-collection name (a key the caller
    resolved against the enumerated installed set) — this walks `base_path/ns/name`, so handing it an
    unvalidated/`..` value would read an attacker-chosen dir. service.units.service_units is that validating
    caller; do not call this with a raw request param."""
    ns, name = paths.collection_fqcn(coll).split(".", 1)      # SEC-3: never an unvalidated name into a join
    cdir = os.path.join(base_path, ns, name)
    return {"collection_playbook": _playbook_names(os.path.join(cdir, "playbooks")),
            "role": _role_names(os.path.join(cdir, "roles"))}


def _ad(args):
    p = subprocess.run(["ansible-doc"] + args, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def deep_probe(coll, version=None):
    """DEEP-probe an installed collection through `ansible-doc`: its modules, its plugins by type, and the options
    of each `*_config` module. `coll` becomes an argv word of every one of those calls, so it is validated HERE, at
    the join (SEC-3 / SECURITY.md C22) — a caller cannot forget it, and some callers are routes that need no token
    (GET /probe/{collection}, GET /classify/{collection}). Raises paths.RequestBoundaryError before any process
    starts."""
    paths.collection_fqcn(coll)
    f = _facts(coll, version, "local", "deep")
    f["modules"] = [k.split(".")[-1] for k in json.loads(_ad(["-l", coll, "-j"]) or "{}")]
    for t in PLUGIN_TYPES:
        plugs = json.loads(_ad(["-t", t, "-l", coll, "-j"]) or "{}")
        if plugs:
            f["plugins"][t] = [k.split(".")[-1] for k in plugs]
    for m in [m for m in f["modules"] if m.endswith("_config")]:
        doc = json.loads(_ad(["-j", coll + "." + m]) or "{}")
        for entry in doc.values():
            f["module_options"][m] = list((entry.get("doc") or {}).get("options") or {})
    return f


# Substrings that evidence a configurable/active syslog host in a device's `show logging` / running-config
# output. Lower-cased match (Cisco/Arista emit "logging host", JunOS "syslog", others "logging server").
_SYSLOG_HOST_MARKERS = ("logging host", "logging server", "syslog")


def logging_export_probe(addr, conn, run_show):
    """READ-ONLY, FAIL-SOFT graceful-export detection for a cliconf/netconf device (the S6 probe).

    Runs a `show logging` / `show running-config | include logging host` style read via the caller-supplied
    `run_show(addr, conn)` callable — the SAME backend dispatch seam the rest of kontroll uses, never a new
    path — and classifies whether the device already advertises a syslog host. Returns
    {state: yes|maybe|no, note}.

    CONTRACT (load-bearing, mirrors rest_pull's "an HTTP GET reads ... actuates NOTHING" posture):
      * read-only — it issues a SHOW/read only; it actuates nothing.
      * fail-soft — ANY unreachable / timeout / unknown-command degrades to {state: 'maybe'} (offerable,
        evidence-less); it NEVER raises, so a probe failure can never abort an operator action.
      * NON-GATING — onboarding/promote never wait on it (INVARIANT-D). The result is only ever fed to
        suggest_logging(evidence=...), which can merely SHARPEN a note, never add/remove a candidate."""
    try:
        out = run_show(addr, conn) or ""
    except Exception as e:                       # fail-soft: a probe failure is NEVER fatal (INVARIANT-D)
        log.debug("logging_export_probe(%s) soft-failed: %s", addr, e)
        return {"state": "maybe", "note": "probe unavailable; offerable on the static signal alone"}
    low = out.lower()
    if any(marker in low for marker in _SYSLOG_HOST_MARKERS):
        return {"state": "yes", "note": "device shows a configurable/active syslog host"}
    return {"state": "no", "note": "no syslog-host line detected; still offerable"}
