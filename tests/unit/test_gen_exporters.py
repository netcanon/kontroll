"""scripts/gen-exporters.py — proxy-exporter CONTAINER compose fragments generated from the telemetry registry.

Pins Phase 2b of the Option-A observability design: the exporter container (pve; snmp/blackbox later) is
GENERATED from the descriptor's `exporter:` block, so adding a proxy exporter is a telemetry/<name>.yml
drop-in, not a hand-authored compose fragment. These guard that pve's fragment generates with the right
image/env/network, that it publishes NO host port (the kontroll-net-only / SSRF-exposure rule, SECURITY C3),
that a proxy method missing its `exporter:` block fails loud, and that --check tracks staleness. The body
parses as a compose document (yaml.safe_load).
"""
import importlib.util
import os

import pytest
import yaml

from kontroll import catalog

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_exporters", os.path.join(ROOT, "scripts", "gen-exporters.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _gen()
FLEET = gen._load("instance.example/fleet.yml")     # the shipped example, by name — never an operator's fleet


def test_pve_exporter_fragment_generated_with_image_env_network():
    """The pve proxy-exporter method generates docker/services/pve-exporter.generated.yaml with the exporter
    image, the PVE_* environment (read from .env), and the kontroll network — the container that used to be a
    hand-authored compose fragment, now derived from telemetry/pve.yml's `exporter:` block. Parsing the body
    proves it is a valid compose document."""
    body = gen.exporter_files(FLEET)["docker/services/pve-exporter.generated.yaml"]
    svc = yaml.safe_load(body)["services"]["pve-exporter"]
    assert svc["image"] == "prompve/prometheus-pve-exporter:3.9.0"   # pinned (G4: no floating :latest)
    assert set(svc["environment"]) == {"PVE_USER", "PVE_TOKEN_NAME", "PVE_TOKEN_VALUE", "PVE_VERIFY_SSL"}
    assert svc["networks"] == ["kontroll"]


def test_exporter_fragment_publishes_no_host_port():
    """The generated exporter fragment declares NO `ports:` — exporters are reachable only on the internal
    kontroll network, never via a published host port (SECURITY C3; a blackbox/snmp exporter with a host
    port + caller-influenced ?target is an SSRF/exposure surface). Guards the no-host-port invariant at the
    generator, paired with the tests/validate grep over committed exporter fragments."""
    svc = yaml.safe_load(gen.exporter_files(FLEET)["docker/services/pve-exporter.generated.yaml"])["services"]
    assert "ports" not in svc["pve-exporter"]


def test_snmp_exporter_fragment_mounts_config_no_host_port():
    """The snmp proxy-exporter method generates docker/services/snmp-exporter.generated.yaml mounting the
    rendered snmp.yml config read-only, with NO host port — the agent-less SNMP exporter for the switch/
    firewall, a descriptor drop-in alongside pve."""
    svc = yaml.safe_load(
        gen.exporter_files(FLEET)["docker/services/snmp-exporter.generated.yaml"])["services"]["snmp-exporter"]
    assert svc["image"] == "prom/snmp-exporter:v0.30.1" and "ports" not in svc
    assert svc["volumes"] == ["../../prometheus/exporters/snmp/snmp.yml:/etc/snmp_exporter/snmp.yml:ro"]


def test_blackbox_exporter_fragment_has_cap_add_and_no_host_port():
    """The blackbox proxy-exporter method generates docker/services/blackbox-exporter.generated.yaml with
    cap_add: [NET_RAW] (ICMP needs a raw socket — the least-privilege grant for ping in a container), the
    static probe config mounted read-only, and NO host port. Guards the cap_add rendering (a descriptor field
    only blackbox uses) and that the reachability exporter stays kontroll-net-only like the others."""
    svc = yaml.safe_load(gen.exporter_files(FLEET)
                         ["docker/services/blackbox-exporter.generated.yaml"])["services"]["blackbox-exporter"]
    assert svc["image"] == "prom/blackbox-exporter:v0.28.0" and "ports" not in svc   # pinned (G4)
    assert svc["cap_add"] == ["NET_RAW"]
    assert svc["volumes"] == ["../../prometheus/exporters/blackbox/blackbox.yml:/etc/blackbox_exporter/config.yml:ro"]


def test_proxy_method_without_exporter_block_fails_loud():
    """A proxy-exporter method that is used by a module but has no `exporter:` block exits non-zero — the
    generator never emits a half-defined container. Guards a descriptor that declares a proxy method without
    telling the generator how to stand up its exporter."""
    methods = {m["name"]: m for m in catalog.load_telemetry()}
    methods["pve"] = {k: v for k, v in methods["pve"].items() if k != "exporter"}    # strip the exporter block
    with pytest.raises(SystemExit):
        gen.exporter_files({"enabled_modules": ["proxmox"]}, methods=methods)


def test_pve_verify_ssl_is_derived_from_the_class_vendor_default_not_hardcoded():
    """PVE_VERIFY_SSL is no longer a literal in telemetry/pve.yml (no-bespoke-config tenet, the B2 twin) —
    gen-exporters DERIVES it from the consuming proxmox class's `vendor_defaults.tls_posture` (self_signed →
    "false") via the SHARED kontroll.endpoints resolver, the SAME vendor fact logging/proxmox_api.yml derives.
    Guards a regression that re-hardcodes the posture, AND proves the rendered value equals the historical literal
    (so the generated fragment is unchanged: derive, don't drift)."""
    desc = gen._load("telemetry/pve.yml")
    assert "PVE_VERIFY_SSL" not in (desc.get("exporter", {}).get("env") or {}), \
        "pve.yml must NOT hardcode PVE_VERIFY_SSL in exporter.env — derive it from the class posture"
    assert desc["exporter"]["tls_verify_env"] == "PVE_VERIFY_SSL", "pve.yml must declare tls_verify_env"
    svc = yaml.safe_load(
        gen.exporter_files(FLEET)["docker/services/pve-exporter.generated.yaml"])["services"]["pve-exporter"]
    assert svc["environment"]["PVE_VERIFY_SSL"] == "false", \
        "self_signed proxmox vendor_defaults must render PVE_VERIFY_SSL:false (== the retired hardcode)"


def test_pve_verify_ssl_ca_pin_flips_to_true_and_mounts_the_bundle():
    """An instance CA pin (device_trust.proxmox.tls_ca_file) flips PVE_VERIFY_SSL to "true" AND wires the cert end
    to end: PVE_VERIFY_SSL is bool-coerced (can't take a path — research-verified), so the exporter trusts the CA
    via REQUESTS_CA_BUNDLE=<the mounted bundle> + a :ro volume of the host CA bundle to that fixed container
    target. The SAME bundle Vector verifies against (one fact, both consumers). Guards the CA being half-wired
    (verify on but the cert never reaching the container — the exact gap this PR closes)."""
    trust = {"proxmox": {"tls_ca_file": "instance/certs/pve-ca.pem"}}
    svc = yaml.safe_load(gen.exporter_files(FLEET, instance_trust=trust)
                         ["docker/services/pve-exporter.generated.yaml"])["services"]["pve-exporter"]
    assert svc["environment"]["PVE_VERIFY_SSL"] == "true"
    assert svc["environment"]["REQUESTS_CA_BUNDLE"] == gen.endpoints.EXPORTER_CA_BUNDLE, \
        "the exporter must trust the CA via REQUESTS_CA_BUNDLE (PVE_VERIFY_SSL is bool-only, not a path)"
    assert any(v.endswith("%s:ro" % gen.endpoints.EXPORTER_CA_BUNDLE) for v in svc.get("volumes", [])), \
        "the host CA bundle must be bind-mounted RO at the fixed container target"


def test_pve_no_ca_pin_renders_no_requests_ca_bundle_or_mount():
    """With NO CA pinned (the default), the pve-exporter fragment carries NO REQUESTS_CA_BUNDLE and NO CA volume
    — only the verify bool ("false"). Guards the CA-mount leaking into the common no-pin case (which would also
    break --check byte-identity / reference a bundle that's just an empty placeholder)."""
    svc = yaml.safe_load(gen.exporter_files(FLEET)["docker/services/pve-exporter.generated.yaml"])["services"]["pve-exporter"]
    assert "REQUESTS_CA_BUNDLE" not in svc["environment"]
    assert "volumes" not in svc, "no CA pinned => no CA mount on the pve-exporter (it has no config_mount either)"


def test_tls_verify_env_without_a_class_posture_fails_closed():
    """_resolve_class_tls_env exits when a `tls_verify_env` exporter has NO consuming-class vendor_defaults to
    resolve from (fail-closed — never silently verify=false). Guards a telemetry method opting into class-TLS
    while the consuming class forgot to declare the posture (the unsafe-default trap)."""
    exp = {"container_name": "x", "image": "y", "env": {}, "tls_verify_env": "PVE_VERIFY_SSL"}
    with pytest.raises(SystemExit):
        gen._resolve_class_tls_env({"name": "pve"}, exp, {}, {})     # empty vendor_defaults_by_module → exit


def test_check_mode_passes_when_committed_fragments_are_fresh():
    """`gen-exporters.py --check` returns 0 when docker/services/*.generated.yaml match what the registry
    produces — the tests/validate guard that the committed exporter fragments cannot drift from the telemetry
    descriptors (generated-never-hand-maintained, machine-enforced)."""
    assert gen.main(["--check"]) == 0
