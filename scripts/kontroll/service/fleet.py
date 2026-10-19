"""fleet domain — the read-only "what have I onboarded?" view (#121).

`list_fleet()` assembles a CLASS-centric inventory of the onboarded fleet: one entry per ENABLED device-class
(instance/fleet.yml) with the capabilities it declares (the module's `metrics:`/`backup:`/`logs:` blocks → the
telemetry/backup/logging dialogs) and the inventory hosts in its group. It is PURE reads — instance/fleet.yml +
modules/<key>/module.yml + instance/inventory/* — and writes nothing, so it never gates onboarding (the same
read-only posture as search/classify; INVARIANT D*).

CLASS-centric, not host-centric, because the capability dialogs are keyed by the device-class `key` (a class
backs up / is scraped / ships logs ONE way), so grouping hosts under their class is what lets the GUI jump
straight from "this is onboarded" to "configure its telemetry/backup/logging". The GUI renders it as the Fleet
panel; the per-class capability badges reuse the SAME openCapabilityDialog(cap, key) the search cards use.
"""
import logging
import os

import yaml

from kontroll import paths

log = logging.getLogger("kontroll.service.fleet")


def _inventory_groups():
    """Walk every instance/inventory/*.yml → {group: {host: hostvars}} — the main hosts.yml + the onboarded
    drop-ins, merged. Resolves the `all.children` nesting; a super-group (children-only, no `hosts`) contributes
    none of its own. A malformed file is skipped (a typo in one drop-in can't blank the whole view)."""
    inv_dir = paths.resolve("ansible/inventory")
    groups = {}

    def _walk(node):
        if not isinstance(node, dict):
            return
        for g, body in node.items():
            if not isinstance(body, dict):
                continue
            hosts = body.get("hosts")
            if isinstance(hosts, dict):
                grp = groups.setdefault(g, {})
                for h, hv in hosts.items():
                    grp[h] = hv if isinstance(hv, dict) else {}
            if isinstance(body.get("children"), dict):
                _walk(body["children"])

    if not os.path.isdir(inv_dir):
        return groups
    for fn in sorted(os.listdir(inv_dir)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        try:
            doc = yaml.safe_load(open(os.path.join(inv_dir, fn), encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(doc.get("all"), dict):
            _walk(doc["all"].get("children") or {})
        else:
            _walk(doc)
    return groups


def _module_capabilities(module):
    """The secondary capabilities a class DECLARES, as {cap: bool} — derived from its module.yml blocks: a
    non-empty `metrics:` ⇒ telemetry; a `backup:` block with `capable: true` ⇒ backup; a non-empty `logs:` ⇒
    logging. These are exactly the clickable badges the Fleet panel opens the capability dialog on."""
    bk = module.get("backup") or {}
    return {"telemetry": bool(module.get("metrics")),
            "backup": bool(isinstance(bk, dict) and bk.get("capable")),
            "logging": bool(module.get("logs"))}


def list_services():
    """The control-plane services kontroll itself stands up — a read-only list for the GUI Index panel's
    "Control-plane services" section. PURE read of config/services.yml (the SHIPPED, repo-tracked, config-as-data
    set: onboard-GUI/Semaphore/Homepage/Grafana/Prometheus/API) with each URL templated from THIS instance's
    KONTROLL_MGMT_IP/KONTROLL_DOMAIN env (already in the GUI process — the same source the capability Grafana
    deep-links use). A sibling of list_fleet(), NOT an overload: services are a flat name/url list from a fixed
    file; device-classes are class-centric from modules+inventory — two concerns, two functions.

    Writes nothing, actuates nothing, never gates onboarding (INVARIANT D*). A missing/malformed
    config/services.yml → [] (never raises — the in-process GUI worker must survive; service-never-sys.exit). A
    URL whose ${MGMT_IP}/${DOMAIN} can't be resolved (no env — a dev box / relocated deploy) is returned as None,
    so the panel shows the service name without a dead link. No enabled_when gating: operators omit a service they
    don't run from the data file (review scope-1)."""
    try:
        spec = yaml.safe_load(open(paths.resolve("config/services.yml"), encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    mgmt = os.environ.get("KONTROLL_MGMT_IP", "")
    domain = os.environ.get("KONTROLL_DOMAIN", "")
    out = []
    for svc in spec.get("services") or []:
        if not isinstance(svc, dict) or not svc.get("name"):
            continue                                    # skip a malformed entry, never blank the whole view
        tmpl = svc.get("url_template") or ""
        unresolved = ("${MGMT_IP}" in tmpl and not mgmt) or ("${DOMAIN}" in tmpl and not domain)
        url = None if (unresolved or not tmpl) else tmpl.replace("${MGMT_IP}", mgmt).replace("${DOMAIN}", domain)
        out.append({"name": svc["name"], "url": url,
                    "description": svc.get("description", ""), "enabled": True})
    return out


def list_fleet():
    """The onboarded fleet, class-centric: a list of {key, group, role, collection, capabilities, hosts}
    — one per ENABLED device-class (instance/fleet.yml) whose modules/<key>/module.yml exists. `hosts` are the
    inventory hosts in the class's `inventory_group` ({name, ansible_host}). Read-only; a class with no module
    file or a malformed one is skipped (never a half-failed view)."""
    try:
        fleet = yaml.safe_load(open(paths.resolve("config/fleet.yml"), encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    groups = _inventory_groups()
    out = []
    for key in paths.enabled_keys(fleet):
        mp = paths.module_file(key)
        if not os.path.exists(mp):
            continue
        try:
            m = yaml.safe_load(open(mp, encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        group = m.get("inventory_group")
        hosts = [{"name": h, "ansible_host": (hv or {}).get("ansible_host")}
                 for h, hv in sorted((groups.get(group) or {}).items())]
        out.append({"key": key, "group": group, "role": m.get("role"),
                    "collection": ((m.get("collections") or [{}])[0] or {}).get("name"),
                    "capabilities": _module_capabilities(m), "hosts": hosts})
    return out
