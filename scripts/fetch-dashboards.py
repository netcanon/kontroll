#!/usr/bin/env python3
"""Fetch the curated Grafana.com community dashboards declared by the enabled device modules and provision
them — the dashboard analogue of gen-requirements.py (collections) + gen-observability.py (scrape targets),
sourcing from the PUBLIC Grafana.com registry (grafana.com/grafana/dashboards) instead of hand-authored JSON.

Each device class declares its data-relevant dashboards in modules/<key>/module.yml:

    dashboards:
      - {gnet: 10347, name: proxmox}   # grafana.com/grafana/dashboards/10347 (Proxmox via Prometheus)
      - {gnet: 1860, name: node}       # Node Exporter Full

This fetches each UNIQUE gnet id from grafana.com (latest revision), pins the Prometheus datasource (uid
`prometheus`, the fixed uid the provisioning sets), strips the import `__inputs`, and writes
dashboards/grafana/dashboards/<name>.json — committed + pinned (reproducible, offline at deploy). The Grafana
file provider auto-loads them. Two datasource styles are handled: the `${DS_PROMETHEUS}` import input (older
dashboards, e.g. 10347) and the `type: datasource` template variable (newer, e.g. 1860).

ONLINE (needs grafana.com). Run deliberately when adding/refreshing a dashboard (NOT a hermetic --check).
This is the curated/pinned tier; the onboarding live-search pick-list rides the same fetch path next.

Usage:  python3 scripts/fetch-dashboards.py [--list]
"""
import json
import os
import sys
import urllib.request

import yaml

from kontroll import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GNET = "https://grafana.com/api/dashboards"
DS_UID = "prometheus"   # must match the uid pinned in dashboards/grafana/provisioning/datasources/prometheus.yml
OUT_DIR = os.path.join(ROOT, "dashboards", "grafana", "dashboards")
_MAX_GNET = 2 ** 31 - 1   # a grafana.com board id — the bound service/observe.py applies to a request (_MAX_GNET);
                          # this script does not import the service layer, so the number stands in both


def _load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kontroll-fetch-dashboards"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _dashboard_entry(d):
    """(gnet, name) of one declared `dashboards:` item, or None when it is not a positive board id plus a name that
    is one path component. The name is the FILE this script writes and the id chooses what an internet download
    puts in it, so a module file is not trusted to carry either: a promoted line is still a line somebody proposed
    (SECURITY.md C22 — the check at the join)."""
    if not isinstance(d, dict):
        return None
    gnet, name = d.get("gnet"), d.get("name")
    if isinstance(gnet, bool) or not isinstance(gnet, int) or not 0 < gnet <= _MAX_GNET:
        return None
    try:
        paths.component(name, "dashboard name")
    except ValueError:
        return None
    return gnet, name


def declared_dashboards(fleet):
    """-> {gnet: name} unique across every enabled module's `dashboards:` block (first name wins on dup). FAILS
    CLOSED (SystemExit naming the module files, before anything is fetched or written) when an item's `gnet` is
    not a positive integer or its `name` is not one path component (other keys, such as `derived`, are allowed
    here), and when one NAME is declared with two different board ids: a name is one file, and the later download
    would replace the earlier — a curated board overwritten by whichever id sorts last."""
    out, bad, owner, clash = {}, set(), {}, set()
    for key in paths.enabled_keys(fleet):
        mp = "modules/%s/module.yml" % key
        if not os.path.exists(os.path.join(ROOT, mp)):
            continue
        module = _load(mp)
        block = module.get("dashboards") if isinstance(module, dict) else None
        if block is not None and not isinstance(block, list):
            bad.add(mp)                                   # `dashboards: 5` — not a list of items at all
            continue
        for d in block or []:
            entry = _dashboard_entry(d)
            if entry is None:
                bad.add(mp)
                continue
            gnet, name = entry
            first_gnet, first_mp = owner.setdefault(name, (gnet, mp))
            if first_gnet != gnet:
                clash.add("%s (%s and %s)" % (name, first_mp, mp))
            out.setdefault(gnet, name)
    if bad:
        raise SystemExit("fetch-dashboards: refusing to run — a `dashboards:` item in %s has a gnet that is not a "
                         "positive integer or a name that is not 1-64 chars of [a-z0-9_-]" % ", ".join(sorted(bad)))
    if clash:
        raise SystemExit("fetch-dashboards: refusing to run — one dashboard name is declared with two board ids: "
                         "%s. A name is one file under dashboards/grafana/dashboards/." % "; ".join(sorted(clash)))
    return out


def pin_datasource(dash):
    """Make a downloaded community dashboard self-provisioning against our fixed Prometheus datasource:
    replace any `${DS_*}` datasource import with the uid, drop the import metadata, and default every
    datasource template variable to it — so it renders without an import prompt or a stale datasource ref."""
    text = json.dumps(dash)
    for inp in dash.get("__inputs", []):
        if inp.get("type") == "datasource":
            text = text.replace("${%s}" % inp["name"], DS_UID)
    dash = json.loads(text)
    dash.pop("__inputs", None)
    dash.pop("__requires", None)
    dash["id"] = None                                   # null id => Grafana treats it as a fresh import
    for var in (dash.get("templating", {}).get("list") or []):
        if var.get("type") == "datasource":
            var["current"] = {"selected": True, "text": "Prometheus", "value": DS_UID}
    return dash


def fetch(gnet):
    """-> (revision, name, pinned_dashboard_dict) for a Grafana.com dashboard id. The dict says which board it is
    (`gnetId`): a download carries that itself — every board fetched so far does, so the bytes are unchanged — and
    it is set here for one that does not, because `foreign_targets()` decides by it whether a file may be
    replaced. gen-dashboard-floor.py takes its boards through this function too, so both write the same bytes."""
    meta = json.loads(_get("%s/%d" % (GNET, gnet)))
    rev = meta["revision"]
    raw = json.loads(_get("%s/%d/revisions/%d/download" % (GNET, gnet, rev)))
    dash = pin_datasource(raw)
    dash["gnetId"] = gnet
    return rev, meta.get("name"), dash


def foreign_targets(want, out_dir=None):
    """The `<name>.json` targets of `want` ({gnet: name}) that already exist and do NOT say they are that board
    (`gnetId`) — a hand-authored dashboard such as the shipped logs one, or another board. Offline. A download is
    never written over such a file: a module line gives a name to a board, it does not hand over whatever file
    already has that name."""
    foreign = []
    for gnet, name in sorted(want.items()):
        path = os.path.join(out_dir or OUT_DIR, "%s.json" % name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                have = json.load(fh).get("gnetId")
        except (OSError, ValueError, AttributeError):
            have = None
        if isinstance(have, bool) or have != gnet:
            foreign.append("%s.json (declared as board %d)" % (name, gnet))
    return foreign


def main(argv):
    want = declared_dashboards(_load(paths.resolve("config/fleet.yml")))
    if "--list" in argv:
        for gnet, name in sorted(want.items()):
            print("%-8d %s" % (gnet, name))
        return 0
    foreign = foreign_targets(want)
    if foreign:
        raise SystemExit("fetch-dashboards: refusing to run — a file under dashboards/grafana/dashboards/ would be "
                         "overwritten by a board it is not: %s. A hand-authored dashboard is never replaced by a "
                         "download. Declare the board under another name, or delete the file to fetch the board "
                         "anew." % "; ".join(foreign))
    os.makedirs(OUT_DIR, exist_ok=True)
    for gnet, name in sorted(want.items()):
        rev, title, dash = fetch(gnet)
        with open(os.path.join(OUT_DIR, "%s.json" % name), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(dash, fh, indent=2)
            fh.write("\n")
        print("fetched %r (gnet %d rev %d) -> dashboards/grafana/dashboards/%s.json" % (title, gnet, rev, name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
