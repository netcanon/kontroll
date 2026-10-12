"""Telemetry-method registry (telemetry/<name>.yml) + the gen-observability dispatcher over it.

These pin Phase 1 of the Option-A observability design (docs/observability/telemetry-method-registry.md):
promoting the hardcoded `via: host|proxy` if/else into a drop-in registry. They guard that the registry
loads, that node_exporter is ONE descriptor row (not a generator branch), and that the dispatcher is
FAIL-CLOSED — an unknown method, a legacy {via,port} entry, or two methods sharing a job all exit non-zero
rather than silently emitting a wrong/empty target. The byte-identical host/proxy RENDER is pinned by the
existing tests/unit/test_gen_observability.py (left unchanged); these add the registry + fail-closed contract.
"""
import importlib.util
import os

import pytest

from kontroll import catalog

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_observability", os.path.join(ROOT, "scripts", "gen-observability.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _gen()
INV = gen._load(gen.paths.resolve("ansible/inventory/hosts.yml"))


def test_registry_loads_host_node_and_pve_sorted_by_order():
    """catalog.load_telemetry() discovers the telemetry/<name>.yml drop-ins and returns them sorted by
    `order` (host-agents before proxy-exporters) — the registry the dispatcher consumes. Guards that a new
    method is picked up by dropping a file in telemetry/, with no loader edit (the registry contract)."""
    methods = catalog.load_telemetry()
    by_name = {m["name"]: m for m in methods}
    assert {"host_node", "pve"} <= set(by_name)
    assert [m["name"] for m in methods] == sorted(by_name, key=lambda n: by_name[n]["order"])
    assert by_name["host_node"]["kind"] == "host-agent" and by_name["host_node"]["port"] == 9100
    assert by_name["pve"]["kind"] == "proxy-exporter" and by_name["pve"]["target_shape"] == "device"


def test_node_exporter_is_one_descriptor_row_not_a_branch():
    """node_exporter appears as exactly ONE telemetry descriptor (host_node, via its agent_role) — the
    demotion the design requires: it is one row in the registry, not a hardcoded `via: host` branch in the
    generator. Guards a regression to a node_exporter-specific code path."""
    agents = [m for m in catalog.load_telemetry() if m.get("agent_role") == "node_exporter"]
    assert len(agents) == 1 and agents[0]["name"] == "host_node"


def test_blackbox_is_a_no_secret_reachability_method():
    """blackbox loads as a proxy-exporter with its OWN job (not snmp's `network`), a closed `probe` param
    allow-list, and NO secret / no static auth label — the agent-less, credential-free reachability layer.
    Guards that blackbox is honestly modeled as no-creds (secret_domain null, no static_labels) so the
    generators never try to render it an auth label or a secret-render task."""
    by_name = {m["name"]: m for m in catalog.load_telemetry()}
    bb = by_name["blackbox"]
    assert bb["kind"] == "proxy-exporter" and bb["job"] == "blackbox"
    assert bb["secret_domain"] is None and "static_labels" not in bb
    assert set(bb["params"]["probe"]["allowed"]) == {"icmp", "tcp_connect", "http_2xx"}


def test_unknown_method_fails_loud():
    """A module metrics entry referencing a telemetry method with no descriptor exits non-zero (never a
    silent empty target). Guards the fail-closed contract: a typo'd or not-yet-created method is caught at
    generate time, not shipped as a missing scrape. (Empty registry forces every method to be unknown.)"""
    with pytest.raises(SystemExit):
        gen.metrics_files({"enabled_modules": ["proxmox"]}, INV, methods={})


def test_legacy_via_entry_fails_loud(monkeypatch):
    """A legacy `{job, via, port}` metrics entry (no `method:` key) exits non-zero — the clean-cutover guard:
    an un-migrated module cannot silently fall back to a host branch; it must reference a telemetry method."""
    legacy = {"inventory_group": "hypervisors", "metrics": [{"job": "node", "via": "host", "port": 9100}]}
    real_load = gen._load
    monkeypatch.setattr(gen, "_load", lambda rel: legacy if rel.startswith("modules/") else real_load(rel))
    with pytest.raises(SystemExit):
        gen.metrics_files({"enabled_modules": ["proxmox"]}, INV)


def test_snmp_param_outside_allowlist_is_rejected(monkeypatch):
    """A metrics param value not in the method's `allowed` list (including one carrying YAML metacharacters)
    is REJECTED at generate time, never interpolated into a target — the config-injection guard the params
    allow-list provides for the param path the GUI/onboarding opt-in will later influence (Phase 7)."""
    crafted = {"inventory_group": "core_switch",
               "metrics": [{"method": "snmp", "params": {"module": "if_mib\ninjected: pwned"}}]}
    real_load = gen._load
    monkeypatch.setattr(gen, "_load", lambda rel: crafted if rel.startswith("modules/") else real_load(rel))
    with pytest.raises(SystemExit):
        gen.metrics_files({"enabled_modules": ["cisco_ios"]}, INV)


def test_two_methods_sharing_a_job_on_one_class_fails_loud(monkeypatch):
    """Two metrics entries on ONE class that resolve to the same job (hence the same generated path) exit
    non-zero rather than silently overwriting one another. Guards the per-module (job,key) collision the
    method-owned job introduces; two DIFFERENT classes sharing a job stays legal (proxmox + docker_host
    both write under targets/node/)."""
    dup = {"inventory_group": "hypervisors", "metrics": [{"method": "host_node"}, {"method": "host_node"}]}
    real_load = gen._load
    monkeypatch.setattr(gen, "_load", lambda rel: dup if rel.startswith("modules/") else real_load(rel))
    with pytest.raises(SystemExit):
        gen.metrics_files({"enabled_modules": ["proxmox"]}, INV)


def test_every_method_param_renders_safely_inside_a_flow_mapping():
    """A method's params are written into the class's module file as `{method: m, params: {k: v}}` — a hand-formatted
    FLOW mapping, where a `,`, a `}`, a `:` or a `#` in a value is structure. Today every param of every shipped
    telemetry and logging method is an `enum`, an `int` or a `bool`, and every allowed enum value reads back from
    such a line as itself. Guards the day someone adds a free-text param: this test then fails, and the renderer
    has to learn to quote before that param can ship (the dashboards item was the same sink with no such pin)."""
    import yaml
    from kontroll.service._validate import infer_type
    for method in catalog.load_telemetry() + catalog.load_logging():
        for name, pdef in (method.get("params") or {}).items():
            kind = infer_type(pdef)
            assert kind in ("enum", "int", "bool"), "%s.%s is %r — the flow-mapping renderer cannot carry it" % (
                method["name"], name, kind)
            for value in pdef.get("allowed") or []:
                line = "- {method: %s, params: {%s: %s}}" % (method["name"], name, value)
                assert yaml.safe_load(line) == [{"method": method["name"], "params": {name: value}}], line
