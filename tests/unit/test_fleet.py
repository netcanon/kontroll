"""fleet — the read-only onboarded-fleet view (#121).

These pin the service that powers the GUI Fleet panel: `list_fleet()` is a PURE read of instance/fleet.yml +
modules/<key>/module.yml + instance/inventory/* that groups inventory hosts under their device-class and surfaces
which secondary capabilities (telemetry/backup/logging) each class declares — so the operator can SEE what's
onboarded and the panel's per-class badges open the SAME capability dialog the search cards do. WHY (the gap this
fills): before #121 the GUI was search→onboard + dialogs only, with no page listing onboarded devices.
"""
import os

import pytest

from kontroll.service import fleet
from kontroll.service.onboard import enable_in_fleet

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _class(tmp_repo, key, group, caps=("metrics", "backup", "logs"), status="active"):
    d = tmp_repo / "modules" / key
    d.mkdir(parents=True)
    body = ("key: %s\nstatus: %s\nrole: %s\ninventory_group: %s\ncollections:\n  - name: ns.%s\n"
            % (key, status, key, group, key))
    if "metrics" in caps:
        body += "metrics:\n  - {method: snmp}\n"
    if "backup" in caps:
        body += 'backup:\n  capable: true\n  schedule: "0 2 * * *"\n'
    if "logs" in caps:
        body += "logs:\n  - {method: syslog_push}\n"
    (d / "module.yml").write_text(body, encoding="utf-8")


def _host(tmp_repo, group, host, ip):
    (tmp_repo / "ansible" / "inventory" / ("onboarded-%s.yml" % group)).write_text(
        "%s:\n  hosts:\n    %s:\n      ansible_host: %s\n      device_role: %s\n" % (group, host, ip, group),
        encoding="utf-8")


def test_list_fleet_groups_hosts_under_their_class_with_caps(tmp_repo):
    """An enabled class with all three capability blocks + an inventory host appears once, with its hosts grouped
    under it and capabilities {telemetry, backup, logging} all true. Guards the class→hosts join + the capability
    derivation that the Fleet panel's clickable badges key off."""
    _class(tmp_repo, "demo_sw", "core_switch")
    enable_in_fleet("demo_sw")
    _host(tmp_repo, "core_switch", "sw1", "192.0.2.10")
    entry = next(c for c in fleet.list_fleet() if c["key"] == "demo_sw")
    assert entry["capabilities"] == {"telemetry": True, "backup": True, "logging": True}
    assert entry["group"] == "core_switch" and entry["status"] == "active"
    assert {"name": "sw1", "ansible_host": "192.0.2.10"} in entry["hosts"]


def test_capabilities_reflect_only_declared_blocks(tmp_repo):
    """A class declaring ONLY metrics shows telemetry true, backup/logging false — the badges mirror the module
    blocks exactly (no phantom capability), and a `backup: {capable: false}` would NOT count. Guards a misleading
    'backed up' badge on a class that isn't."""
    _class(tmp_repo, "demo_obs", "hypervisors", caps=("metrics",))
    enable_in_fleet("demo_obs")
    entry = next(c for c in fleet.list_fleet() if c["key"] == "demo_obs")
    assert entry["capabilities"] == {"telemetry": True, "backup": False, "logging": False}
    assert entry["hosts"] == []                              # no inventory hosts for this group in the tmp tree


def test_enabled_without_a_module_file_is_skipped(tmp_repo):
    """A key enabled in fleet.yml with no modules/<key>/module.yml is skipped, not a crash — the real copied
    fleet enables classes whose modules aren't in the tmp tree, and the view must degrade to what it can read."""
    out = fleet.list_fleet()        # real enabled keys, none of whose modules exist in tmp_repo
    assert isinstance(out, list) and all(c.get("key") for c in out)


def test_template_wires_the_fleet_panel():
    """The header exposes a `fleet-open` button → `openFleetPanel()`, and the panel's per-class capability badge
    (`fleet-class-cap`) re-uses `openCapabilityDialog` — so the operator actuates telemetry/backup/logging per
    class from the Fleet view. Pins the GUI round-trip (the testids are recorded in testid_reference.md)."""
    tpl = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert 'data-testid="fleet-open"' in tpl and "openFleetPanel()" in tpl
    assert "fleet-class-cap" in tpl and "openCapabilityDialog(cap, c.key" in tpl
    assert "/api/fleet" in tpl


def test_template_wires_the_two_section_index():
    """The Index panel (#128) fetches BOTH /api/services and /api/fleet and renders a `services-body` section
    (with `service-row`s) above the unchanged `fleet-classes` section — devices AND services in one panel. The
    header button relabels to 'Index' while keeping the `fleet-open`/`fleet-panel` testid contract (label widens,
    testid frozen — §2 of the design). Pins the device-AND-services round-trip (testids in testid_reference.md)."""
    tpl = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    # the section testids are set programmatically via E(...), so they appear as bare quoted args, not HTML attrs
    assert "/api/services" in tpl and "'services-body'" in tpl
    assert "'service-row'" in tpl and "'fleet-classes'" in tpl
    assert ">Index<" in tpl                                  # the header button label widened to "Index"
    assert 'data-testid="fleet-open"' in tpl                # …but the testid contract is unchanged


def test_readers_of_the_fleet_survive_a_line_that_is_not_a_class_key(tmp_repo, monkeypatch):
    """With `- null`, a number and a mapping among `enabled_modules`, the Fleet index and the audit service still
    answer — with the valid classes — instead of raising a request-boundary refusal for a field nobody sent.
    Guards the two readers the classify-route test does not reach: each took the raw list before."""
    from kontroll import catalog
    from kontroll.service.audit import service_audit
    _class(tmp_repo, "demo_sw", "core_switch")
    (tmp_repo / "config" / "fleet.yml").write_text(
        "enabled_modules:\n  - null\n  - 123\n  - {a: 1}\n  - demo_sw\n", encoding="utf-8")
    assert [c["key"] for c in fleet.list_fleet()] == ["demo_sw"]
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    audit = service_audit()
    assert audit["enabled"] == ["demo_sw"] and audit["declared"] == ["ns.demo_sw"]
