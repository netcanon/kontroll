"""scripts/gen-logging.py — the Loki per-class retention OVERRIDES enactment (S5). Guards the M-1 dogfood
findings (docs/reviews/2026-06-16-storage-logging/40-m1-loki-retention-dogfood.md) in code so they can't
silently regress: the override is keyed on tenant `fake` (NOT `*` — F3), every emitted period is >= 24h (F4,
below which Loki rejects the file FATALLY at startup), the file is FAIL-CLOSED (no class sets retention =>
`overrides: {}`, never empty=infinite), the static debug/audit baseline is CARRIED forward (a per-tenant
retention_stream REPLACES, not merges — dropping it would regress the 90d audit retention to 30d, a C12
data-loss), and the longest preset stays within reject_old_samples_max_age (the §3.3 cross-knob coupling).
"""
import importlib.util
import os

import pytest
import yaml

from kontroll import paths

pytestmark = pytest.mark.unit


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_logging", os.path.join(paths.ROOT, "scripts", "gen-logging.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _override_doc(gen, fleet):
    return yaml.safe_load(gen.loki_retention_files(fleet)[gen.LOKI_OVERRIDES_REL])


def test_no_retention_emits_empty_overrides_failclosed():
    """The shipped example fleet (no module sets a `retention` param) emits `overrides: {}` — the FAIL-CLOSED default: the
    capability streams inherit the bounded global LOKI_RETENTION_PERIOD, never an empty=infinite window. Guards
    a generator that emits a partial/garbage override (which, being invalid, would make Loki refuse to start)."""
    gen = _gen()
    fleet = gen._load("instance.example/fleet.yml")
    assert _override_doc(gen, fleet) == {"overrides": {}}


def test_retention_param_emits_fake_tenant_override_with_carried_baseline(tmp_path, monkeypatch):
    """A class that sets `retention: 90d` produces overrides.**fake**.retention_stream with its
    {source=capability, device=<key>} entry at 2160h/priority 10 — AND the static debug/audit baseline carried
    forward. Guards the two M-1 corrections at once: the tenant key is `fake` (F3), and the baseline survives the
    replace-not-merge semantics (else the 90d audit retention silently regresses to the 30d global)."""
    gen = _gen()
    monkeypatch.setattr(gen, "ROOT", str(tmp_path))
    (tmp_path / "modules" / "demo").mkdir(parents=True)
    (tmp_path / "modules" / "demo" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo", "inventory_group": "demo_grp",
        "logs": [{"method": "syslog_push", "params": {"retention": "90d"}}]}), encoding="utf-8")
    (tmp_path / "docker" / "loki").mkdir(parents=True)
    baseline = [{"selector": '{level="debug"}', "priority": 1, "period": "168h"},
                {"selector": '{source="internal", service=~"api-audit|gui-audit"}', "priority": 2, "period": "2160h"}]
    (tmp_path / "docker" / "loki" / "loki-config.yml").write_text(
        yaml.safe_dump({"limits_config": {"retention_stream": baseline}}), encoding="utf-8")

    streams = _override_doc(gen, {"enabled_modules": ["demo"]})["overrides"]["fake"]["retention_stream"]
    assert {"selector": '{source="capability", device="demo"}', "priority": 10, "period": "2160h"} in streams
    for b in baseline:                                  # the baseline is CARRIED (replace-not-merge safety)
        assert b in streams


def test_every_preset_is_at_least_24h():
    """M-1 F4: Loki rejects a retention_stream period < 24h, and an invalid runtime_config is FATAL at startup.
    Every preset in the closed allow-list maps to >= 24h, so the generator can never emit a file that bricks a
    Loki restart. Guards someone adding a `1h`/`12h` preset that would be fatal."""
    gen = _gen()
    for preset, hours in gen._RETENTION_PRESET_HOURS.items():
        assert hours.endswith("h") and int(hours[:-1]) >= 24, "%s -> %s is below Loki's 24h floor" % (preset, hours)


def test_tenant_key_is_fake_not_wildcard():
    """M-1 F3: with auth_enabled:false Loki's tenant is `fake` and per-tenant overrides key on it EXACTLY (the
    design's assumed `*` would target a non-existent tenant => a silently inert knob). Pin the constant."""
    assert _gen().LOKI_TENANT == "fake"


def test_generated_override_is_always_valid_yaml():
    """The example-fleet override file parses as a mapping with an `overrides` key — the always-valid invariant
    (M-1 F4: a malformed runtime_config is fatal at Loki startup, so the committed/generated file must always
    parse)."""
    gen = _gen()
    fleet = gen._load("instance.example/fleet.yml")
    doc = yaml.safe_load(gen.loki_retention_files(fleet)[gen.LOKI_OVERRIDES_REL])
    assert isinstance(doc, dict) and "overrides" in doc


def test_longest_preset_within_reject_old_samples_window():
    """Cross-knob coupling (design §3.3): the longest enacted retention must stay <= reject_old_samples_max_age,
    or backfilled history past the accept-window is silently dropped on first ingest. The 90d max (2160h) must
    not exceed the loki-config.yml accept window. Guards D1's paradigm widening retention past the window."""
    gen = _gen()
    max_preset_h = max(int(h[:-1]) for h in gen._RETENTION_PRESET_HOURS.values())
    cfg = gen._load(gen.LOKI_CONFIG_REL)
    accept_h = int(str(cfg["limits_config"]["reject_old_samples_max_age"])[:-1])
    assert max_preset_h <= accept_h, \
        "max retention %dh exceeds reject_old_samples_max_age %dh" % (max_preset_h, accept_h)


def test_carried_baseline_is_single_sourced_from_loki_config():
    """`_static_baseline_streams()` reads the LIVE loki-config.yml retention_stream — the baseline is carried,
    never duplicated. A drift pin: if the static debug/audit policy changes, the carried baseline follows it
    automatically (no second copy to forget)."""
    gen = _gen()
    cfg = gen._load(gen.LOKI_CONFIG_REL)
    assert gen._static_baseline_streams() == (cfg["limits_config"]["retention_stream"])


def test_conflicting_retention_across_entries_is_rejected(tmp_path, monkeypatch):
    """A class whose logs entries set DIFFERENT retention windows is rejected (sys.exit) — one device label maps
    to one retention window, so an ambiguous pair must fail loud at generate time, never silently pick one."""
    gen = _gen()
    monkeypatch.setattr(gen, "ROOT", str(tmp_path))
    (tmp_path / "modules" / "demo").mkdir(parents=True)
    (tmp_path / "modules" / "demo" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo", "logs": [{"method": "syslog_push", "params": {"retention": "7d"}},
                                 {"method": "journald_remote", "params": {"retention": "90d"}}]}), encoding="utf-8")
    (tmp_path / "docker" / "loki").mkdir(parents=True)
    (tmp_path / "docker" / "loki" / "loki-config.yml").write_text(
        yaml.safe_dump({"limits_config": {"retention_stream": []}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        gen.loki_retention_files({"enabled_modules": ["demo"]})
