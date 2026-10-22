"""F2 (de-bespoke): a blind-onboarded device's metrics:/logs: FLOOR is AUTO-DERIVED from the conferred + universal
methods, not hand-typed — so an arbitrary device reaches the SAME universal monitoring/logging the ~7 curated
classes get, with zero curation, OR honestly documents the gap.

WHY (the failure these guard — docs/reviews/2026-06-29-north-star-onboard/20-debespoke-classes.md): a galaxy-onboarded
class mapped to a generic backend role with EMPTY metrics:/logs: — a blank Grafana, no Loki stream. The derivation
reads each backend's `confers.telemetry`/`confers.logs` (the ONE host->capability dispatch seam) + each method's
`derive_default` switch + applies_when, and emits the agent-less floor (snmp/blackbox metrics, syslog logs). The
crown-jewel properties asserted here: the floor NEVER fakes a vendor exporter it has no signal for (fail-honest), is
creds-free by construction (MF-S5), and reproduces the curated cisco_ios class BYTE-FOR-BYTE (the frozen oracle).
"""
import importlib.util
import os

import pytest
import yaml

from kontroll import catalog, predicate
from kontroll.service import classify

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _facts(plugins=None, modules=None, module_options=None, collection="x.y"):
    """A deep_probe-shaped facts dict (the SAME shape predicate.eval_pred + the derivation read)."""
    return {"collection": collection, "version": "1.0.0", "plugins": plugins or {},
            "modules": modules or [], "module_options": module_options or {}}


def _load_gen():
    """Import scripts/gen-class-capabilities.py (a hyphenated standalone script) for its drift() check."""
    path = os.path.join(ROOT, "scripts", "gen-class-capabilities.py")
    spec = importlib.util.spec_from_file_location("gen_class_capabilities", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cliconf_switch_derives_the_full_agent_less_floor():
    """A network_cli device (netcommon_cli confers snmp+blackbox / syslog_push) derives metrics
    [snmp(if_mib), blackbox(icmp)] + logs [syslog_push(tcp)] with an EMPTY gap — the dominant blind-onboard case
    reaches full parity. Guards a regression that drops a conferred method or its universal param default."""
    out = classify.derive_class_capabilities(_facts(plugins={"cliconf": ["ios"]}), "netcommon_cli")
    assert [m["method"] for m in out["metrics"]] == ["snmp", "blackbox"]
    assert out["metrics"][0]["params"] == {"module": "if_mib"}
    assert out["metrics"][1]["params"] == {"probe": "icmp"}
    assert [m["method"] for m in out["logs"]] == ["syslog_push"]
    assert out["logs"][0]["params"] == {"transport": "tcp"}
    assert out["gap"] == []


def test_rest_device_derives_only_blackbox_no_snmp_no_syslog():
    """A REST/httpapi device (api backend confers only blackbox) derives blackbox reachability but NOT snmp or
    syslog (which need a CLI plane it doesn't have) — honest, not faked. Guards conferring a CLI-only method onto
    a REST device."""
    out = classify.derive_class_capabilities(_facts(plugins={"httpapi": ["fortios"]}), "api")
    assert [m["method"] for m in out["metrics"]] == ["blackbox"]
    assert out["logs"] == []


def test_vendor_narrow_credentialed_method_is_never_faked_but_named_in_the_gap():
    """THE NEVER-FAKE GUARD: a proxmox-shaped device (module proxmox_kvm; the class declares raw_ssh) derives the
    universal floor (blackbox + host_node) but the `pve` proxy-exporter — vendor-narrow applies_when AND a per-class
    secret — is NEVER auto-attached; it surfaces in the HONEST gap instead (declare it to enable VM/LXC metrics).
    Guards the empty-Grafana anti-pattern's inverse: silently shipping a vendor exporter a blind device can't
    actually feed."""
    out = classify.derive_class_capabilities(_facts(modules=["proxmox_kvm"]), "raw_ssh")
    metric_methods = {m["method"] for m in out["metrics"]}
    assert metric_methods == {"blackbox", "host_node"} and "pve" not in metric_methods
    assert any(g["method"] == "pve" and g["capability"] == "telemetry" for g in out["gap"])


def test_every_conferred_method_is_creds_free_to_derive():
    """MF-S5 — the auto-derivability SECRET invariant: every method ANY backend confers in telemetry/logs is safe to
    auto-attach (secret_domain null/absent, OR the SHARED already-provisioned read-only snmp_observability domain).
    Guards someone adding a PER-CLASS-secret method (pve, rest_pull) to a confer list, which would silently ship a
    blind class with an unfilled token. snmp is the ONE allowed shared-domain method (v3 read-only, fleet-wide)."""
    tel = {m["name"]: m for m in catalog.load_telemetry()}
    logm = {m["name"]: m for m in catalog.load_logging()}
    for b in catalog.load_backends():
        confers = b.get("confers") or {}
        for name in (confers.get("telemetry") or []):
            assert name in tel, "%s confers unknown telemetry method %r" % (b["name"], name)
            assert classify._is_creds_free_to_derive(tel[name]), \
                "%s confers %r which needs a per-class secret — not safe to auto-derive (MF-S5)" % (b["name"], name)
        for name in (confers.get("logs") or []):
            assert name in logm, "%s confers unknown logging method %r" % (b["name"], name)
            assert classify._is_creds_free_to_derive(logm[name]), \
                "%s confers %r which needs a per-class secret — not safe to auto-derive (MF-S5)" % (b["name"], name)


def test_snmp_is_the_only_shared_secret_domain_method_derivable():
    """MF-S5 posture pin: snmp (secret_domain snmp_observability — the SHARED v3 read-only domain) IS derivable, but
    a hypothetical per-class-secret method is NOT. Guards the whitelist silently widening to admit a per-class
    credential method (which would re-open the unfilled-token hole)."""
    snmp = next(m for m in catalog.load_telemetry() if m["name"] == "snmp")
    assert snmp.get("secret_domain") == "snmp_observability"
    assert classify._is_creds_free_to_derive(snmp) is True
    assert classify._is_creds_free_to_derive({"secret_domain": "proxmox"}) is False   # a per-class secret
    assert classify.SHARED_DERIVABLE_SECRET_DOMAINS == frozenset({"snmp_observability"})


def test_cisco_ios_derived_floor_reproduces_the_curated_module_exactly():
    """THE FROZEN ORACLE (migration step 1): the derivation, fed cisco_ios's PINNED facts, reproduces the curated
    class's `derived: true`-marked metrics/logs entries EXACTLY (marker stripped). This is the proof the migration
    changed no generated byte — the curated module is the oracle, the derivation must match it. Guards the
    de-bespoke silently changing what the curated class produced."""
    facts = yaml.safe_load(open(os.path.join(ROOT, "modules", "cisco_ios", "facts.pinned.yml"), encoding="utf-8"))
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", "cisco_ios", "module.yml"), encoding="utf-8"))
    backend = predicate.classify(facts, catalog.load_backends())[0]
    assert backend == "netcommon_cli"
    out = classify.derive_class_capabilities(facts, backend)

    def _strip(block):
        return [{k: v for k, v in e.items() if k != "derived"} for e in (block or []) if e.get("derived")]
    assert out["metrics"] == _strip(module["metrics"])
    assert out["logs"] == _strip(module["logs"])
    assert out["dashboards"] == _strip(module["dashboards"]) == [   # Rung 4a: snmp -> snmp_if, blackbox -> blackbox
        {"gnet": 1124, "name": "snmp_if"}, {"gnet": 13659, "name": "blackbox"}]
    assert out["gap"] == []


def test_fortigate_derived_floor_reproduces_the_curated_module_exactly():
    """FROZEN ORACLE (Rung 4, fortigate — the byte-identical residual-isolation proof): fortinet.fortios ships an
    `httpapi` plugin and no cliconf, so predicate.classify → the `api` backend, which confers telemetry [blackbox]
    ONLY. The derivation, fed fortigate's PINNED facts, reproduces its single `derived: true` metrics entry
    (blackbox) EXACTLY, while the curated `snmp` entry stays a BARE (non-derived) override — `api` cannot confer snmp
    on an httpapi device, so the interface-counter scrape is the operator-declared vendor residual. Guards the
    migration silently changing the class's generated targets AND the derivation ever faking snmp onto an httpapi
    class (the never-fake honesty contract)."""
    facts = yaml.safe_load(open(os.path.join(ROOT, "modules", "fortigate", "facts.pinned.yml"), encoding="utf-8"))
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", "fortigate", "module.yml"), encoding="utf-8"))
    backend = predicate.classify(facts, catalog.load_backends())[0]
    assert backend == "api"
    out = classify.derive_class_capabilities(facts, backend)

    def _strip(block):
        return [{k: v for k, v in e.items() if k != "derived"} for e in (block or []) if e.get("derived")]
    assert out["metrics"] == _strip(module["metrics"]) == [{"method": "blackbox", "params": {"probe": "icmp"}}]
    # snmp is present in the module but is the BARE curated override (not derived) — --check must not own it
    assert {m["method"] for m in module["metrics"]} == {"snmp", "blackbox"}
    assert not any(m.get("derived") for m in module["metrics"] if m["method"] == "snmp")
    # Rung 4a: api derives ONLY blackbox -> the sole derived board is the reachability board (no snmp board — the
    # bare snmp metric is an operator override, so it carries no derived board; the never-fake honesty guard).
    assert out["dashboards"] == _strip(module["dashboards"]) == [{"gnet": 13659, "name": "blackbox"}]
    assert out["logs"] == [] and out["gap"] == []


def _oracle(key, want_backend):
    """Shared frozen-oracle assertion: the derivation, fed <key>'s PINNED facts, classifies to want_backend and
    reproduces the module's `derived: true`-marked metrics/logs entries EXACTLY (marker stripped), gap empty."""
    facts = yaml.safe_load(open(os.path.join(ROOT, "modules", key, "facts.pinned.yml"), encoding="utf-8"))
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", key, "module.yml"), encoding="utf-8"))
    backend = predicate.classify(facts, catalog.load_backends())[0]
    assert backend == want_backend, (key, backend)
    out = classify.derive_class_capabilities(facts, backend)

    def _strip(block):
        return [{k: v for k, v in e.items() if k != "derived"} for e in (block or []) if e.get("derived")]
    assert out["metrics"] == _strip(module["metrics"]), (key, out["metrics"])
    assert out["logs"] == _strip(module["logs"] if "logs" in module else None), (key, out["logs"])
    # Rung 4a: the DERIVED dashboards: floor reproduces the module's `derived: true`-marked board entries EXACTLY.
    assert out["dashboards"] == _strip(module.get("dashboards")), (key, out["dashboards"])
    assert out["gap"] == [], (key, out["gap"])
    return out


def _declared_oracle(key, backend):
    """The frozen oracle for a class whose backend is DECLARED, not classified: <key>'s pinned facts classify to NO
    backend (so the declaration is load-bearing, not cosmetic), its module declares `backend`, and the floor derived
    under that backend reproduces the module's `derived: true`-marked metrics/logs/dashboards EXACTLY, gap empty."""
    facts = yaml.safe_load(open(os.path.join(ROOT, "modules", key, "facts.pinned.yml"), encoding="utf-8"))
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", key, "module.yml"), encoding="utf-8"))
    assert predicate.classify(facts, catalog.load_backends()) == [], key
    assert module.get("backend") == backend, (key, module.get("backend"))
    out = classify.derive_class_capabilities(facts, backend)

    def _strip(block):
        return [{k: v for k, v in e.items() if k != "derived"} for e in (block or []) if e.get("derived")]
    assert out["metrics"] == _strip(module["metrics"]), (key, out["metrics"])
    assert out["logs"] == _strip(module.get("logs")), (key, out["logs"])
    assert out["dashboards"] == _strip(module.get("dashboards")), (key, out["dashboards"])
    assert out["gap"] == [], (key, out["gap"])
    return out


def test_routeros_derived_floor_reproduces_the_curated_module_exactly():
    """FROZEN ORACLE (Rung 4, routeros — the STAGED full-floor proof): community.routeros ships a cliconf plugin, so
    predicate.classify → `netcommon_cli` → the full network-gear floor (snmp + blackbox metrics, syslog_push logs) —
    a MikroTik switch derives the SAME floor as cisco_ios. routeros is STAGED (commented out of the fleet), so
    gen-class-capabilities --check does not visit it; THIS unit test is its drift gate until cutover. Guards the
    de-bespoke silently changing a staged class's floor + guards the cliconf-classification assumption regressing to
    an api/blackbox-only downgrade (the load-bearing tripwire the adversary flagged)."""
    out = _oracle("routeros", "netcommon_cli")
    assert [m["method"] for m in out["metrics"]] == ["snmp", "blackbox"]
    assert [m["method"] for m in out["logs"]] == ["syslog_push"]


def test_opnsense_derived_floor_reproduces_the_curated_module_exactly():
    """FROZEN ORACLE (Rung 4, opnsense — the STAGED no-connection-plugin proof): ansibleguy.opnsense ships NO
    connection plugin (NOT `api` — a real probe corrected the design), so its pinned facts classify to NO backend and
    the module DECLARES `backend: raw_ssh` (S24 — raw_ssh is never a catch-all), which confers telemetry [blackbox,
    host_node]. opnsense is STAGED, so this unit test is its drift gate until cutover. Guards the floor drifting under
    the declaration, and the declaration being cosmetic: a classifier change that made the facts classify would turn
    it into a hidden override."""
    out = _declared_oracle("opnsense", "raw_ssh")
    assert [m["method"] for m in out["metrics"]] == ["blackbox", "host_node"]
    assert out["logs"] == []


def test_openwrt_derived_floor_reproduces_the_curated_module_exactly():
    """FROZEN ORACLE (Rung 4, openwrt — the EMPTY-FACTS proof): openwrt has no collection, so its facts.pinned.yml is
    empty; empty facts classify to NO backend (absence is not evidence — S24), so the module DECLARES `backend:
    raw_ssh` → telemetry [blackbox, host_node]. openwrt is ACTIVE + LIVE, so gen-class-capabilities --check gates it
    too; this test additionally pins that the empty-facts artifact classifies to nothing (so the declaration is what
    chooses) and that the floor is unchanged under it. The host_node target is honest-DOWN for an AP with no
    node_exporter — asserted present (never faked away), consistent with the one-seam raw_ssh confer."""
    out = _declared_oracle("openwrt", "raw_ssh")
    assert [m["method"] for m in out["metrics"]] == ["blackbox", "host_node"]
    assert out["logs"] == []


def test_docker_host_derived_floor_uses_the_declared_backend():
    """FROZEN ORACLE (Rung 4, docker_host — the DECLARED-BACKEND proof): community.docker does NOT classify —
    nothing in its facts is positive evidence of a backend (no cliconf/httpapi/netconf plugin; raw_ssh is never
    auto-matched, S24) — so the module DECLARES `backend: raw_ssh`, and gen-class-capabilities._backend_for honors the
    declared backend → the floor is raw_ssh's [blackbox, host_node]. This test pins BOTH load-bearing facts: (1) the
    raw facts really do NOT resolve a backend (so the declaration is required, not cosmetic), and (2) the derived
    floor reproduces the module's `derived: true` entries. Guards a classifier change silently making
    community.docker classify (which would make the declaration a hidden override)."""
    facts = yaml.safe_load(open(os.path.join(ROOT, "modules", "docker_host", "facts.pinned.yml"), encoding="utf-8"))
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", "docker_host", "module.yml"), encoding="utf-8"))
    # premise: the real docker facts classify to NO backend -> the declaration is required
    assert predicate.classify(facts, catalog.load_backends()) == []
    assert module["backend"] == "raw_ssh"
    out = classify.derive_class_capabilities(facts, module["backend"])

    def _strip(block):
        return [{k: v for k, v in e.items() if k != "derived"} for e in (block or []) if e.get("derived")]
    assert out["metrics"] == _strip(module["metrics"]) == [
        {"method": "blackbox", "params": {"probe": "icmp"}}, {"method": "host_node"}]
    assert out["dashboards"] == _strip(module["dashboards"]) == [   # Rung 4a: blackbox + node derived boards
        {"gnet": 13659, "name": "blackbox"}, {"gnet": 1860, "name": "node"}]
    assert out["logs"] == [] and out["gap"] == []


# --- Rung 4a: the DERIVED dashboards: floor (classify._derive_dashboards + the dashboard gap) ------------------

_TEL = [{"name": "host_node", "derive_dashboard": {"search": "node"}},
        {"name": "snmp", "derive_dashboard": {"search": "snmp"}},
        {"name": "syslog_push"}]                                   # a method with NO derive_dashboard -> never a board
_LOCKS = {"host_node": {"id": 1860, "name": "node"}, "snmp": {"id": 1124, "name": "snmp_if"}}


def test_derive_dashboards_rides_the_derived_metric():
    """A board is emitted ONLY for a method actually derived into metrics: a host_node-deriving class gets the node
    board; a class deriving only snmp gets the snmp board and NO node board. Guards hanging a node_exporter board off
    a class whose floor emits no node series (the honesty guard — the board rides the series the method emits)."""
    assert classify._derive_dashboards([{"method": "host_node"}], _TEL, _LOCKS) == [{"gnet": 1860, "name": "node"}]
    assert classify._derive_dashboards([{"method": "snmp"}], _TEL, _LOCKS) == [{"gnet": 1124, "name": "snmp_if"}]


def test_derive_dashboards_omits_when_no_lock_or_no_selector():
    """A method with a derive_dashboard: selector but NO resolved lock emits no board (fail-honest — never fabricates
    an id from a selector alone); a derived method with NO selector (syslog_push) also emits none. Guards a faked
    board id."""
    assert classify._derive_dashboards([{"method": "host_node"}], _TEL, {}) == []          # selector but no lock
    assert classify._derive_dashboards([{"method": "syslog_push"}], _TEL, _LOCKS) == []     # no selector at all


def test_derive_dashboards_dedups_by_gnet():
    """Two methods resolving to the SAME gnet emit ONE entry (the within-class dedup guard). Guards N duplicate
    boards for a class that derives two methods pinned to the same community board."""
    tel = [{"name": "a", "derive_dashboard": {"search": "x"}}, {"name": "b", "derive_dashboard": {"search": "y"}}]
    locks = {"a": {"id": 99, "name": "same"}, "b": {"id": 99, "name": "same"}}
    assert classify._derive_dashboards([{"method": "a"}, {"method": "b"}], tel, locks) == [{"gnet": 99, "name": "same"}]


def test_derive_gap_names_a_derived_method_with_no_resolved_board():
    """The never-fake contract for dashboards: a method WE DERIVED into metrics that declares a derive_dashboard:
    selector but has NO resolved lock surfaces a `capability: dashboard` gap (the series scrapes but has no board),
    never a silent blank. Guards a floor method's board silently vanishing when its lock is unresolved."""
    gap = classify.derive_gap(_facts(), [{"method": "host_node"}], [], telemetry=_TEL, logging=[], locks={})
    assert any(g["capability"] == "dashboard" and g["method"] == "host_node" for g in gap)
    # with the lock present there is NO dashboard gap (the resolved floor is honest-complete)
    assert not any(g["capability"] == "dashboard"
                   for g in classify.derive_gap(_facts(), [{"method": "host_node"}], [],
                                                telemetry=_TEL, logging=[], locks=_LOCKS))


def test_gen_class_capabilities_check_is_green_then_red_on_drift(monkeypatch):
    """The no-bespoke `--check` TEETH: drift() returns [] for the real cisco_ios pinned floor (green), and reports
    a drift the instant the recomputed floor diverges from the module's pinned entries (red). Guards silent rot
    between the derivation and the pinned class floor — the gate that fails CI when a method/confer churn lands."""
    gcc = _load_gen()
    fleet = {"enabled_modules": ["cisco_ios"]}
    assert gcc.drift(fleet) == []                                  # the real pinned floor matches the derivation
    monkeypatch.setattr(gcc.classify, "derive_class_capabilities",
                        lambda *a, **k: {"metrics": [{"method": "tampered"}], "logs": [], "dashboards": [], "gap": []})
    msgs = gcc.drift(fleet)
    assert msgs and "cisco_ios metrics" in msgs[0]                 # the drift is caught + named (metrics checked first)
