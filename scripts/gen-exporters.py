#!/usr/bin/env python3
"""scripts/gen-exporters.py — Docker compose fragments for proxy-exporter CONTAINERS, GENERATED from the
telemetry registry (Phase 2b of the Option-A observability design).

A proxy-exporter method (pve; snmp/blackbox later) needs a control-node exporter CONTAINER that queries the
device on Prometheus's behalf. That container used to be a hand-authored docker/services/<name>.yaml; this
generator emits it from the descriptor's `exporter:` block into docker/services/<container>.generated.yaml,
so adding a proxy exporter is a telemetry/<name>.yml drop-in (the compose `include:` + one line is the only
hub touch — the composition seam). No host port is ever published (kontroll-net-only; SECURITY.md C3) — a
tests/validate check enforces no `ports:` in exporter fragments.

Standalone except kontroll.catalog (no ansible). Kept honest by `--check`. yamllint-clean (the custom Dumper
indents block sequences, per indent-sequences: true).

Usage:  python3 scripts/gen-exporters.py              # write from THIS node's fleet (the instance overlay)
        python3 scripts/gen-exporters.py --check      # validate the committed fragments against instance.example/
        python3 scripts/gen-exporters.py --example    # regenerate the committed fragments from instance.example/
"""
import glob
import os
import sys

import yaml

from kontroll import catalog, endpoints, paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_optional(rel):
    """{} for an absent file — the instance overlay (instance.yml) a fresh node may not have. Reads the OPTIONAL
    `device_trust` per-class CA pins (the instance DECISION half of the no-bespoke vendor-fact seam)."""
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class _Indent(yaml.SafeDumper):
    """Indent block sequences under their key (so the output passes yamllint indent-sequences: true)."""
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _used_proxy_methods(fleet, methods):
    """The proxy-exporter methods actually referenced by an enabled module (dedup by name) — so we never emit a
    compose fragment for an exporter nobody scrapes. Each entry carries the consuming module(s)' `vendor_defaults`
    so a method that derives an env from the consuming CLASS's TLS posture (`tls_verify_env`) can resolve it
    (the no-bespoke vendor-fact seam) -> {name: {"method": m, "vendor_defaults_by_module": {key: vd}}}."""
    used = {}
    for key in paths.enabled_keys(fleet):
        mp = "modules/%s/module.yml" % key
        if not os.path.exists(os.path.join(ROOT, mp)):
            continue
        module = _load(mp)
        vd = module.get("vendor_defaults")
        for met in (module.get("metrics") or []):
            m = methods.get(met.get("method"))
            if m and m.get("kind") == "proxy-exporter":
                slot = used.setdefault(m["name"], {"method": m, "vendor_defaults_by_module": {}})
                if vd is not None:
                    slot["vendor_defaults_by_module"][key] = vd
    return used


def _resolve_class_tls_env(method, exp, vendor_defaults_by_module, instance_trust):
    """If the exporter declares `tls_verify_env`, fill that env var from the consuming CLASS's TLS posture
    (`vendor_defaults.tls_posture` ⊕ the instance CA pin) via kontroll.endpoints — the SAME vendor fact the
    logging pull (`logging/<m>.yml tls_from_class`) derives, so the two consumers CAN'T drift (no-bespoke tenet).
    Returns `exp` unchanged when there is no `tls_verify_env`. Fail-CLOSED: zero or conflicting consuming-class
    postures, or an unknown posture, exits (never silently `verify=false`).

    When the instance PINS a CA (the class's `device_trust.tls_ca_file`), this ALSO points the exporter at it +
    mounts it: `PVE_VERIFY_SSL` (and exporters generally) is bool-coerced and CANNOT take a CA path, so the
    research-verified mechanism is `REQUESTS_CA_BUNDLE`=<the mounted bundle> + a :ro volume of the host CA bundle
    to the fixed container target. The SAME bundle Vector verifies against (one fact, both consumers — no drift)."""
    tve = exp.get("tls_verify_env")
    if not tve:
        return exp
    postures = {tuple(sorted((vd or {}).items())) for vd in vendor_defaults_by_module.values()}
    if len(postures) != 1:
        sys.exit("gen-exporters: %r tls_verify_env=%r but its consuming class(es) declare 0 or conflicting "
                 "vendor_defaults — cannot resolve one TLS posture" % (method["name"], tve))
    only_key = next(iter(vendor_defaults_by_module))
    try:
        verify, ca_file = endpoints.tls_verify(vendor_defaults_by_module[only_key], instance_trust.get(only_key))
    except ValueError as exc:
        sys.exit("gen-exporters: %r tls_verify_env: %s" % (method["name"], exc))
    exp = dict(exp)
    exp.pop("tls_verify_env", None)                          # consumed -> not a compose field
    exp["env"] = dict(exp.get("env") or {})
    exp["env"][tve] = "true" if verify else "false"          # the exporter reads the string form
    if ca_file:
        # Operator pinned a CA: trust it via REQUESTS_CA_BUNDLE (a path; the verify env is bool-only) + mount the
        # assembled host bundle RO at the fixed container target — the SAME bundle Vector reads.
        exp["env"]["REQUESTS_CA_BUNDLE"] = endpoints.EXPORTER_CA_BUNDLE
        exp["extra_volumes"] = list(exp.get("extra_volumes") or []) \
            + ["%s:%s:ro" % (endpoints.CA_BUNDLE_HOST, endpoints.EXPORTER_CA_BUNDLE)]
    return exp


def _header(method, exp):
    ac = exp.get("access_chain") or {}
    return ("# GENERATED by scripts/gen-exporters.py from telemetry/%s.yml. Do NOT edit by hand — change the\n"
            "# descriptor's `exporter:` block and re-run (deploy-stack regenerates it before bring-up).\n"
            "#\n"
            "# Access chain used:   %s\n"
            "# May break:           %s\n"
            "# Fallback required:   %s\n"
            "# Blast radius:        %s\n"
            "#\n"
            "# No host port is published — reachable only on the internal kontroll network, never WAN\n"
            "# (SECURITY.md C3); a tests/validate check enforces no `ports:` in exporter fragments.\n"
            % (method["name"], ac.get("chain", "?"), ac.get("may_break", "?"),
               ac.get("fallback", "?"), ac.get("blast_radius", "?")))


def _service_doc(exp):
    """The compose document for one exporter — image/env/network only, NEVER a published host port."""
    svc = {"image": exp["image"], "container_name": exp["container_name"], "restart": "unless-stopped"}
    if exp.get("env"):
        svc["environment"] = exp["env"]
    vols = []
    if exp.get("config_mount"):
        vols.append("%s:%s:ro" % (exp["config_mount"], exp.get("config_container_path", exp["config_mount"])))
    vols += list(exp.get("extra_volumes") or [])       # e.g. the pinned-CA :ro mount (_resolve_class_tls_env)
    if vols:
        svc["volumes"] = vols
    if exp.get("cap_add"):
        svc["cap_add"] = list(exp["cap_add"])         # e.g. NET_RAW for blackbox ICMP (least-privilege raw socket)
    svc["networks"] = ["kontroll"]
    return {"services": {exp["container_name"]: svc},
            "networks": {"kontroll": {"external": True, "name": "kontroll"}}}


def exporter_files(fleet, methods=None, instance_trust=None):
    """-> {repo_rel_path: rendered_body}: one docker/services/<container>.generated.yaml per proxy-exporter
    method an enabled module uses. Fails loud if a used proxy method has no `exporter:` block. `instance_trust`
    is the optional `device_trust` map (per-class CA pins) the class-TLS-env resolution consumes."""
    if methods is None:
        methods = {m["name"]: m for m in catalog.load_telemetry()}
    instance_trust = instance_trust or {}
    out = {}
    for info in _used_proxy_methods(fleet, methods).values():
        m = info["method"]
        exp = m.get("exporter")
        if not exp:
            sys.exit("gen-exporters: proxy method %r has no `exporter:` block (cannot generate its container)"
                     % m["name"])
        exp = _resolve_class_tls_env(m, exp, info["vendor_defaults_by_module"], instance_trust)
        body = _header(m, exp) + yaml.dump(_service_doc(exp), Dumper=_Indent, sort_keys=False,
                                           default_flow_style=False, indent=2, width=4096)
        out["docker/services/%s.generated.yaml" % exp["container_name"]] = body
    return out


def main(argv):
    # The committed output is the PUBLIC, example-derived reference: `--check` VALIDATES it and `--example`
    # REGENERATES it from instance.example/ (TEST-NET data only), on every checkout — so an operator's own fleet can
    # never turn this product gate red or make a committed file stale. The bare write (deploy-stack, the capability
    # regen on a node) reads the instance OVERLAY and writes that node's real file.
    example = "--check" in argv or "--example" in argv
    fleet = _load("instance.example/fleet.yml" if example else paths.resolve("config/fleet.yml"))
    for problem in paths.fleet_problems(fleet):              # skipped, and said: the position, never the entry
        sys.stderr.write("gen-exporters: ignoring: instance/fleet.yml: %s\n" % problem)
    if paths.fleet_list_unreadable(fleet):                   # not an EMPTY fleet: render nothing, prune nothing
        sys.stderr.write("gen-exporters: not rendering: instance/fleet.yml holds no list of classes — nothing is written "
                         "and nothing is pruned until the file is corrected\n")
        return 1
    # the per-class trust decisions follow the same switch: the example's under --check/--example, the node's bare
    instance_trust = _load_optional(
        "instance.example/instance.yml" if example else "instance/instance.yml").get("device_trust") or {}
    want = {os.path.normpath(os.path.join(ROOT, rel)): body
            for rel, body in exporter_files(fleet, instance_trust=instance_trust).items()}
    have = {os.path.normpath(p)
            for p in glob.glob(os.path.join(ROOT, "docker", "services", "*.generated.yaml"))}

    if "--check" in argv:
        stale = []
        for path, body in want.items():
            cur = open(path, encoding="utf-8").read() if os.path.exists(path) else None
            if cur != body:
                stale.append(os.path.relpath(path, ROOT))
        for path in have - set(want):
            stale.append(os.path.relpath(path, ROOT) + " (orphaned)")
        if stale:
            print("STALE exporter fragments — re-run scripts/gen-exporters.py --example:\n  " + "\n  ".join(stale))
            return 1
        print("exporter fragments up to date (%d file(s))" % len(want))
        return 0

    for path in have - set(want):            # prune orphans first (a removed proxy method/module)
        os.remove(path)
    for path, body in want.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:   # LF on every platform
            fh.write(body)
    print("wrote %d exporter fragment(s): %s"
          % (len(want), ", ".join(sorted(os.path.relpath(p, ROOT) for p in want))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
