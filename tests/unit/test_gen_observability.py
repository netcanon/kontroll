"""scripts/gen-observability.py — Prometheus scrape targets GENERATED from the device modules + inventory.

This is the metrics analogue of gen-requirements.py: a device class declares its observability shape ONCE
as data (modules/<key>/module.yml `metrics:`), and the generator fans it out over the inventory hosts in
the class's group — so adding a device/host changes a scrape target with ZERO hand-editing of target files
(the 'generated, never hand-maintained' doctrine extended from collections to metrics). These tests pin
the join (modules x inventory -> targets), both scrape shapes (`via: host` and `via: proxy`), the honest
'no metrics block => no target' behaviour, and the --check staleness guard that keeps the committed
*.generated.yml in sync. The script name is hyphenated, so it is loaded by path (not importable).
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The committed PUBLIC reference inventory. The committed *.generated.yml derive from THIS (never the private
# `instance/` overlay), so the SHIPPED tree (make-bundle, which does NOT strip prometheus/targets/) carries only
# TEST-NET (192.0.2.x) addrs — no real homelab topology (the F3 leak fix). The maintainer's live deploy still
# regenerates from their private overlay; those live artifacts are instance state, never committed to origin.
EXAMPLE = os.path.join(ROOT, "instance.example")
pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_observability", os.path.join(ROOT, "scripts", "gen-observability.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _gen()


def _files():
    # Read the PUBLIC example inventory EXPLICITLY (not paths.resolve, which would shadow it with the private
    # `instance/` overlay): the committed *.generated.yml are the example-derived public reference (F3).
    return gen.metrics_files(gen._load(os.path.join(EXAMPLE, "fleet.yml")),
                             gen._load(os.path.join(EXAMPLE, "inventory", "hosts.yml")))


def test_proxmox_class_emits_both_host_and_proxy_targets():
    """The proxmox class (a `metrics:` list with a `via: host` node block AND a `via: proxy` block) emits
    a node target per host (`host:9100`) AND a proxy target per host = the DEVICE address (no port) — which the
    proxmox job's relabel proxies onto pve-exporter. Proves the generator handles both scrape shapes from one
    self-describing declaration: host = the host exposes the port, proxy = a control-node exporter queries the
    device address. Pinned against the PUBLIC example inventory (TEST-NET addrs, no real topology — F3)."""
    files = _files()
    node = files["prometheus/targets/node/proxmox.generated.yml"]
    assert "192.0.2.10:9100" in node and "host: my-hypervisor" in node
    proxy = files["prometheus/targets/proxmox/proxmox.generated.yml"]
    assert '"192.0.2.10"' in proxy                                               # device addr, not the exporter
    assert "host: my-hypervisor" in proxy and "pve-exporter" not in proxy        # exporter lives in the job relabel


def test_docker_hosts_fan_out_over_every_inventory_host():
    """The docker_host class fans its node_exporter block over EVERY host in the docker_hosts group — so adding a
    host to the inventory adds its scrape target on regeneration, the device-agnostic 'declare once, instantiate
    per host' property. Pinned against the example docker_hosts group (TEST-NET host; the fan-out JOIN is the
    pinned property — a multi-host group on a real instance regenerates the same way)."""
    dh = _files()["prometheus/targets/node/docker_host.generated.yml"]
    assert "192.0.2.20:9100" in dh and "host: my-docker-host" in dh


def test_snmp_class_emits_a_target_while_unconferred_methods_stay_honest():
    """cisco_ios AND fortigate (each a `metrics: [{method: snmp}]` block) emit
    prometheus/targets/network/<key>.generated.yml with their __param_module/__param_auth labels. Post-Rung-4,
    openwrt is `derived: true` and DOES emit its conferred floor (blackbox + node), but it gets **NO**
    network/snmp target — the `raw_ssh` backend does not confer snmp, so the generator never invents an SNMP
    scrape for an AP whose plugin shape can't prove it. (Guards both the snmp fan-out — the core switch AND the
    edge firewall share the one `network` job via the snmp method, gap-d closed for both — AND the standing
    honesty rule in its confer-map form: a class only gets the targets its floor/overrides actually declare, so
    an unconferred method like snmp-on-raw_ssh is never faked onto openwrt.)"""
    files = _files()
    cisco = files["prometheus/targets/network/cisco_ios.generated.yml"]
    assert "192.0.2.2" in cisco and "__param_module: if_mib" in cisco and "__param_auth: v3_kontroll" in cisco
    forti = files["prometheus/targets/network/fortigate.generated.yml"]
    assert "192.0.2.1" in forti and "__param_module: if_mib" in forti and "__param_auth: v3_kontroll" in forti
    assert "host: my-firewall" in forti
    # openwrt derives blackbox + node (raw_ssh floor) but is NEVER given an snmp/network target (not conferred)
    assert "prometheus/targets/network/openwrt.generated.yml" not in files
    assert "prometheus/targets/blackbox/openwrt.generated.yml" in files
    assert "prometheus/targets/node/openwrt.generated.yml" in files


def test_blackbox_class_emits_reachability_targets_without_an_auth_label():
    """cisco_ios and fortigate each carry a SECOND metrics entry ({method: blackbox, params: {probe: icmp}}),
    so the generator emits prometheus/targets/blackbox/<key>.generated.yml with the device addr +
    __param_module: icmp and — unlike snmp — NO __param_auth (blackbox is credential-free). Guards that one
    class can carry two methods on two DIFFERENT jobs (snmp on `network`, blackbox on `blackbox`) and that the
    no-secret method never emits an auth label."""
    files = _files()
    for key, ip in (("cisco_ios", "192.0.2.2"), ("fortigate", "192.0.2.1")):
        bb = files["prometheus/targets/blackbox/%s.generated.yml" % key]
        assert ip in bb and "__param_module: icmp" in bb and "__param_auth" not in bb


def test_committed_targets_match_the_example_inventory():
    """The committed prometheus/targets/*.generated.yml == what the generator produces from the PUBLIC example
    inventory (instance.example/) — the staleness gate keyed off the PUBLIC source, so the SHIPPED tree carries
    only TEST-NET addrs and a real-valued regeneration committed by mistake fails HERE (the F3 leak-regression
    gate; supersedes the old overlay-driven `--check`, which read the private instance/ overlay and is why real
    homelab IPs leaked into the committed lockfile). A module `metrics:` edit not regenerated against the example
    also fails here. (`scripts/gen-observability.py --check` stays the maintainer's OVERLAY-driven CLI for live
    deploy; this PYTEST gate is the public-reference invariant CI enforces.)"""
    for rel, body in _files().items():
        path = os.path.join(ROOT, rel)
        assert os.path.exists(path), "missing committed target %s — re-run gen-observability against instance.example/" % rel
        assert open(path, encoding="utf-8").read() == body, \
            "%s drifted from the example inventory — re-run gen-observability against instance.example/" % rel


def test_a_label_that_is_not_a_plain_word_is_quoted_and_reads_back_as_itself():
    """`_render` writes a group or a host name that YAML would read as another type — `null`, `true`, `123` — or
    that holds a line break, as a quoted scalar: the file parses back to exactly the strings it was given, one
    entry per target. A plain value is written bare, as before. Guards a group named `null` (stored quoted in the
    module file, rendered bare here) reaching Prometheus as an absent label, and a value read back from the
    inventory adding a line to the targets file."""
    import yaml
    entries = [(["192.0.2.9:9100"], {"group": "null", "host": "true"}),
               (["192.0.2.8:9100"], {"group": "123", "host": "sw1" + chr(0x2028) + "- targets: [evil]"}),
               # a host may be NAMED like a date that does not exist; the YAML reader cannot build one, and the
               # generator must quote it — not die, at every deploy and every telemetry promote, on a name
               (["192.0.2.6:9100"], {"group": "0x_", "host": "2026-02-30"}),
               # ...and a target is one quoted scalar whatever its address holds
               (['a"b\\c:9100', "192.0.2.5:9100"], {"group": "core_switch", "host": "0000-00-00"}),
               (["192.0.2.7:9100"], {"group": "core_switch", "host": "sw-01", "__param_module": "if_mib"})]
    text = gen._render(entries)
    parsed = yaml.safe_load(text)
    assert [(e["targets"], e["labels"]) for e in parsed] == entries
    assert "    group: core_switch\n" in text and "    host: sw-01\n" in text       # plain values stay bare
    assert '    group: "null"\n' in text and '    host: "true"\n' in text
