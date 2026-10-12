"""scripts/gen-secret-env.py — the UNIFIED secret->env manifest generator (telemetry secret_env_map + logging
secret_env_map -> ONE value-free manifest deploy-stack consumes).

Pins the fail-CLOSED config-injection guards (a malformed env name / template / a platform-core collision exits
non-zero, never reaching the manifest or the rendered .env), the value-free (NAMES-only) C12 contract, the
--check staleness contract, and the platform-core boundary (a device descriptor can never shadow the control
plane's own creds). The design + the must-fix bar: docs/reviews/2026-06-17-secret-injection/.
"""
import importlib.util
import os
import re

import pytest

from kontroll import paths

pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_secret_env", os.path.join(paths.ROOT, "scripts", "gen-secret-env.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_example_fleet_manifest_unifies_telemetry_and_logging_env_value_free():
    """The shipped example fleet's manifest carries BOTH the telemetry exporter env (proxmox declares `metrics: [pve]`, whose
    secret_env_map -> PVE_EXPORTER_*) AND the logging token (`logs: [proxmox_api]` -> KONTROLL_PVE_LOG_TOKEN), each
    keyed by env var with its secret_domain + a str.format template over SOPS field NAMES — and NO composed value /
    auth scheme (C12: names only). Guards (a) the telemetry env silently dropping out of the union (the smoking-gun
    gap this run closed), (b) the logging token going missing, (c) a credential leaking into the committed manifest."""
    gen = _gen()
    man = gen.secret_env_files(gen._load("instance.example/fleet.yml"))[gen.MANIFEST_REL]
    assert "PVE_EXPORTER_USER:" in man and "KONTROLL_PVE_LOG_TOKEN:" in man, "both axes in ONE manifest"
    assert "secret_domain: proxmox" in man and "{proxmox_api_user}" in man, "domain NAME + field-NAME template"
    assert "PVEAPIToken=" not in man and "root@pam" not in man, "NO composed token / auth scheme / value (C12)"


def test_manifest_always_emits_env_key_even_when_empty():
    """An empty fleet (no enabled module declares a secret_env_map) still emits `env: {}` — so --check stays honest
    when the last credentialed method is removed (a missing key would let a stale manifest pass silently)."""
    gen = _gen()
    man = gen.secret_env_files({"enabled_modules": []})[gen.MANIFEST_REL]
    assert "env:" in man, "the manifest ALWAYS emits an env: key (empty when nothing declares secret_env_map)"


def _method(name, sem, domain="proxmox"):
    return {"name": name, "secret_domain": domain, "secret_env_map": sem}


def test_secret_env_map_shape_is_fail_closed():
    """A malformed secret_env_map exits non-zero at GENERATE time (the config-injection guard): a non-UPPER_SNAKE
    env name, an empty/non-string template, an unsafe template field (format-spec / metacharacter), or a map with
    no secret_domain. Guards a bad descriptor smuggling an extra .env line or a broken token through the loop."""
    gen = _gen()
    with pytest.raises(SystemExit):                                          # lower-case env name
        gen._validate_or_exit(_method("m", {"pve_user": "{a}"}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # empty template
        gen._validate_or_exit(_method("m", {"PVE_USER": ""}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # format-spec field (injection)
        gen._validate_or_exit(_method("m", {"PVE_USER": "{a:>9}"}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # metacharacter field
        gen._validate_or_exit(_method("m", {"PVE_USER": "{a.b}"}), "k", "telemetry")
    with pytest.raises(SystemExit):                                          # secret_env_map but no secret_domain
        gen._validate_or_exit({"name": "m", "secret_env_map": {"PVE_USER": "{a}"}}, "k", "telemetry")
    assert gen._validate_or_exit(_method("m", {"PVE_USER": "{a}"}), "k", "telemetry") == {"PVE_USER": "{a}"}  # OK


def test_env_name_colliding_with_platform_core_is_rejected():
    """A device/consumer secret_env_map emitting a PLATFORM-CORE env name (SEMAPHORE_ADMIN_PASSWORD,
    KONTROLL_API_TOKEN, …) exits non-zero — the loop renders AFTER the platform-core literals, so a duplicate key
    could shadow the control plane's own admin password in a later-wins .env parser. The machine-checked form of
    the platform-core boundary (M2 / §5 of the design); without it a device drop-in could overwrite intrinsic creds."""
    gen = _gen()
    for reserved in ("SEMAPHORE_ADMIN_PASSWORD", "KONTROLL_API_TOKEN", "GRAFANA_ADMIN_PASSWORD"):
        with pytest.raises(SystemExit):
            gen._validate_or_exit(_method("evil", {reserved: "{a}"}), "k", "telemetry")


def test_two_methods_claiming_same_env_with_different_spec_is_rejected(monkeypatch):
    """Two methods (here one telemetry + one logging) claiming the SAME env var with a DIFFERENT (domain, template)
    exits non-zero — a silent env-var collision would let one method's secret overwrite another's in the manifest.
    Guards a cross-axis collision shipping the wrong token. Monkeypatches the registries + the module loader so the
    collision is exercised deterministically (the real fleet has a single proxmox domain, no collision)."""
    gen = _gen()
    tele = {"name": "tele_a", "secret_domain": "d1", "secret_env_map": {"DUP_ENV": "{x}"}}
    logg = {"name": "log_b", "secret_domain": "d2", "secret_env_map": {"DUP_ENV": "{y}"}}
    monkeypatch.setattr(gen.catalog, "load_telemetry", lambda: [tele])
    monkeypatch.setattr(gen.catalog, "load_logging", lambda: [logg])
    monkeypatch.setattr(gen.os.path, "exists", lambda p: True)
    monkeypatch.setattr(gen, "_load",
                        lambda rel: {"metrics": [{"method": "tele_a"}], "logs": [{"method": "log_b"}]})
    with pytest.raises(SystemExit):
        gen.secret_env_files({"enabled_modules": ["k"]})


def test_check_mode_passes_when_committed_manifest_is_fresh():
    """gen-secret-env.py --check == 0 on the freshly-generated tree (the in-pytest twin of the tests/validate
    gate, which is skipped where the shell harness can't run). Guards a secret_env_map edit landing without the
    manifest regenerated — the generated-never-hand-maintained contract."""
    gen = _gen()
    assert gen.main(["--check"]) == 0, "the committed manifest must match a fresh generate (run gen-secret-env.py)"


def test_reserved_platform_core_matches_deploy_stack_env_lines():
    """Every LITERAL `NAME=` env line in deploy-stack.yml's .env render (the platform-core block — NOT the generic
    `{{ env }}=` loop) is in RESERVED_PLATFORM_CORE_ENV, so the collision guard above knows about ALL of them. A
    drift pin: adding a platform-core .env line without adding it to RESERVED would silently open a shadow hole
    (M2). Reads deploy-stack.yml as text and captures the literal env names the .env content block defines."""
    gen = _gen()
    text = open(os.path.join(paths.ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8").read()
    # The .env content lines are indented literals `NAME={{...}}` / `NAME=1`; the generic loop line starts with
    # `{{ env }}=` (a `{`, not an UPPER letter) so it is excluded by the leading-[A-Z] anchor.
    literal_env = set(re.findall(r"^\s+([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))
    missing = literal_env - gen.RESERVED_PLATFORM_CORE_ENV
    assert not missing, ("these literal deploy-stack .env names are not in RESERVED_PLATFORM_CORE_ENV "
                         "(the collision guard would miss them): %s" % sorted(missing))
