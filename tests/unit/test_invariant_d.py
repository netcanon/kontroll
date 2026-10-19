"""INVARIANT D* — no secondary capability gates onboarding (docs/observability/secondary-capability-dialog.md §5).

The seam's load-bearing safety property: a device must be fully Ansible-usable after onboarding ALONE, with no
secondary capability (telemetry, backup, future) able to gate, block, or even influence it. These are the pins.
The PRIMARY pin is a positive allow-list that survives an EMPTY or broken registry (it cannot silently vanish
if the registry fails to load). The SECONDARY pins are registry-PARAMETRIZED — they grow as capabilities are
added, with a min-count guard so an empty parametrization is RED, never a vacuous green. Together they guard:
onboarding writes only its own known keys (never a capability block), it survives a suggester that raises, and
a suggester never mutates the repo.
"""
import os

import pytest
import yaml

from kontroll import catalog, probe
from kontroll.service import capability
from kontroll.service.onboard import apply_onboard_plan, build_onboard_plan
from kontroll.service.scaffold import build_scaffold

pytestmark = pytest.mark.unit

# The CLOSED set of module.yml top-level keys the onboard data-write is allowed to emit. F2 (de-bespoke) relaxed
# the old "no capability block" form of the decoupling for the AUTO-DERIVED universal floor only: `metrics`/`logs`
# (the agent-less monitoring/logging a blind class gets for free) + the class-level `derived` flag are now
# legitimately written — but ONLY when marked `derived: true` (test_onboard_apply_writes_only_the_derived_floor),
# and the floor never GATES onboarding (the load-bearing D* property, pinned by the failure-degrades tests below).
# Still structurally EXCLUDES backup + any future hand-curated/credentialed block.
_ONBOARD_KEYS = {"key", "description", "collections", "role", "backend",
                 "backend_params", "inventory_group", "secrets_domain",
                 "metrics", "logs", "derived"}   # F2: the auto-derived universal floor + its class-level flag

# Telemetry at Phase 7; +backup at Phase 8. The secondary pins must cover AT LEAST this many (no empty green).
_MIN_CAPABILITIES = 1

_CAPS = [c["name"] for c in catalog.load_capabilities()]


@pytest.fixture
def _onboardable(monkeypatch, make_facts):
    """deep_probe every collection as a cliconf device (→ a real backend) + nothing installed, so
    build_onboard_plan computes a full plan offline."""
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None: make_facts(collection=coll, plugins={"cliconf": ["ios"]},
                                                              modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {})


def _snapshot(root):
    """{path: (mtime, size)} for every file under `root` — a cheap 'did anything write?' fingerprint."""
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            snap[p] = (os.path.getmtime(p), os.path.getsize(p))
    return snap


def _apply_and_read(tmp_repo):
    plan = build_onboard_plan("ns.demo", "demo_x", "core_switch", "192.0.2.9")
    assert plan["error"] is None
    apply_onboard_plan(plan)
    return yaml.safe_load((tmp_repo / "modules" / "demo_x" / "module.yml").read_text(encoding="utf-8"))


def test_onboard_apply_writes_only_known_keys(tmp_repo, _onboardable):
    """PRIMARY (positive allow-list, cannot vanish): the written module.yml's top-level keys are a SUBSET of the
    onboard-allowed set (base identity + the F2 auto-derived floor metrics/logs/derived) — so onboarding can never
    grow an UNKNOWN block (a backup:, a future credentialed capability) that would couple it to a secondary
    capability. Holds even if the registry is empty/fails to load (the can't-vanish property)."""
    written = _apply_and_read(tmp_repo)
    assert set(written) <= _ONBOARD_KEYS, "onboard wrote unexpected key(s): %s" % (set(written) - _ONBOARD_KEYS)


def test_onboard_and_scaffold_write_no_lifecycle_status_key(tmp_repo, _onboardable):
    """No writer of a module emits a `status` key. It was a lifecycle field (`staged`/`active`) that nothing read:
    a class runs the moment `instance/fleet.yml` lists it, and the onboard enables it in the same apply. The pin
    above is a SUBSET check, so it cannot fail on a key that is still written; this row can. Guards the inert field
    coming back, and with it text telling an operator to "flip" a switch that does nothing."""
    written = _apply_and_read(tmp_repo)
    assert "status" not in written
    plan = build_onboard_plan("ns.other", "demo_y", "core_switch", "192.0.2.10")
    assert plan["error"] is None and "status" not in plan["module"]
    scaffolded = build_scaffold("ns.demo", "demo_z", "core_switch", "network")
    assert scaffolded["error"] is None and "status" not in scaffolded["module"]


def test_onboard_apply_writes_only_the_derived_floor(tmp_repo, _onboardable):
    """F2 successor to the old never-writes-a-block pin: a blind onboard MAY write the AUTO-DERIVED universal floor
    (metrics/logs), but EVERY entry it writes is marked `derived: true` (gen-class-capabilities owns it) and the
    class carries the class-level `derived` flag. It NEVER writes a hand-curated/credentialed block or backup.
    Guards onboarding emitting a capability block it can't justify (a faked vendor exporter)."""
    written = _apply_and_read(tmp_repo)
    for cap in ("metrics", "logs"):
        for entry in (written.get(cap) or []):
            assert entry.get("derived") is True, "%s entry not marked derived (hand-curated?): %r" % (cap, entry)
    assert "backup" not in written                       # backup is the role-conferred path, not an onboard write
    if written.get("metrics") or written.get("logs"):
        assert written.get("derived") is True            # the class-level flag accompanies a derived floor


@pytest.mark.parametrize("cap", _CAPS)
def test_onboard_apply_writes_no_underived_capability_block(tmp_repo, _onboardable, cap):
    """SECONDARY (registry-parametrized): for EACH registered capability, any block onboarding writes is the F2
    auto-DERIVED floor (every entry `derived: true`) — never a HAND-CURATED block. A capability with no
    derive_default (backup) is not written at all. Grows with the registry; the count guard keeps it non-vacuous."""
    written = _apply_and_read(tmp_repo)
    block = written.get(capability.get_descriptor(cap)["block_key"])
    if block is None:
        return                                           # not written (e.g. backup — no derive_default) — fine
    assert isinstance(block, list) and block and all(e.get("derived") for e in block), \
        "%s block written by onboard contains a non-derived (hand-curated) entry: %r" % (cap, block)


def test_secondary_pins_are_not_vacuous():
    """The registry-parametrized pins cover AT LEAST the expected number of capabilities (telemetry now; +
    backup at Phase 8). Guards the failure mode where an empty/broken registry makes the parametrized pins
    vacuously green — an empty parametrization would otherwise read as 'all passed'."""
    assert len(catalog.registered_capabilities()) >= _MIN_CAPABILITIES


def test_onboard_survives_a_suggester_failure(tmp_repo, _onboardable, monkeypatch):
    """Onboarding still produces a COMPLETE plan when the telemetry suggester RAISES — the nudge degrades to
    None, it never gates onboarding (INVARIANT D*: the result is independent of any capability's state).
    Guards a suggester bug taking down the core onboard path."""
    def boom(*a, **k):
        raise RuntimeError("suggester exploded")
    monkeypatch.setattr("kontroll.service.onboard.suggest_telemetry", boom)
    plan = build_onboard_plan("ns.demo", "demo_x", "core_switch", "192.0.2.9")
    assert plan["error"] is None and plan["telemetry"] is None
    assert plan["module"]["key"] == "demo_x"          # the plan is complete despite the suggester failure


def test_onboard_surfaces_provisioning_when_declared(tmp_repo, _onboardable, monkeypatch):
    """When the device class declares credential prerequisites, the plan SURFACES them (the blind-joe constraint:
    a manual step beyond IP+creds is shown at onboarding). The plan carries the records read by collection — but
    they are advisory metadata, never written into the module (the _ONBOARD_KEYS pin above still holds). Guards
    the surface silently dropping a class's declared prerequisites."""
    monkeypatch.setattr(catalog, "module_provisioning",
                        lambda coll: [{"grant": "PVEAuditor", "note": "grant it"}])
    plan = build_onboard_plan("ns.demo", "demo_x", "core_switch", "192.0.2.9")
    assert plan["error"] is None
    assert [r["grant"] for r in plan["provisioning"]] == ["PVEAuditor"]


def test_onboard_survives_a_provisioning_read_failure(tmp_repo, _onboardable, monkeypatch):
    """Onboarding still produces a COMPLETE plan when the provisioning reader RAISES — the surface degrades to
    [], it never gates onboarding (INVARIANT D*: the result is independent of any advisory's state, exactly like
    the telemetry nudge). Guards a malformed-descriptor or reader bug taking down the core onboard path."""
    def boom(*a, **k):
        raise RuntimeError("provisioning reader exploded")
    monkeypatch.setattr(catalog, "module_provisioning", boom)
    plan = build_onboard_plan("ns.demo", "demo_x", "core_switch", "192.0.2.9")
    assert plan["error"] is None and plan["provisioning"] == []
    assert plan["module"]["key"] == "demo_x"          # the plan is complete despite the reader failure


@pytest.mark.parametrize("cap", _CAPS)
def test_capability_suggester_does_not_mutate_the_repo(tmp_repo, _onboardable, make_facts, cap):
    """For each capability, calling its suggester leaves the repo byte-identical — suggesters are read-only (a
    suggester that wrote could never satisfy 'never gates onboarding'). Guards a future suggester sneaking in a
    write under the read-only contract."""
    before = _snapshot(tmp_repo)
    capability.suggest(cap, make_facts(plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    assert _snapshot(tmp_repo) == before
