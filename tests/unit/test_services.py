"""service/fleet.list_services — the control-plane services read powering the GUI Index panel's services section
(#128). A PURE read of config/services.yml with each URL templated from KONTROLL_MGMT_IP/KONTROLL_DOMAIN.

WHY (the gap this fills): the Index needs the fixed control-plane set (onboard-GUI/Semaphore/Grafana/…) as
config-as-data so the operator jumps to each running service from 'what's running'. These pin the env-templating,
the graceful no-base degradation, the never-raise posture (the in-process GUI worker must survive a bad data
file), and the read-only-by-construction contract (the Index must never gain an actuation path).
"""
import pytest

from _readonly_pins import assert_read_only
from kontroll.service import fleet, observe

pytestmark = pytest.mark.unit

_SAMPLE = (
    "services:\n"
    "  - {name: Semaphore, url_template: 'http://${MGMT_IP}:3001', description: Ansible}\n"
    "  - {name: Homepage, url_template: 'https://control.${DOMAIN}', description: Portal}\n"
)


def _write(tmp_repo, body):
    (tmp_repo / "config" / "services.yml").write_text(body, encoding="utf-8")


def test_list_services_templates_urls_from_env(tmp_repo, monkeypatch):
    """A services.yml entry's ${MGMT_IP}/${DOMAIN} are substituted from KONTROLL_MGMT_IP/KONTROLL_DOMAIN, so the
    panel links to THIS instance's real control plane. Guards the env-templating the services section depends on."""
    _write(tmp_repo, _SAMPLE)
    monkeypatch.setenv("KONTROLL_MGMT_IP", "203.0.113.3")
    monkeypatch.setenv("KONTROLL_DOMAIN", "lab.test")
    out = {s["name"]: s["url"] for s in fleet.list_services()}
    assert out["Semaphore"] == "http://203.0.113.3:3001"
    assert out["Homepage"] == "https://control.lab.test"


def test_list_services_unresolvable_base_yields_null_url(tmp_repo, monkeypatch):
    """With no KONTROLL_MGMT_IP/KONTROLL_DOMAIN (a dev box / relocated deploy), a URL needing one is returned as
    None — so the panel renders the service NAME without a dead 'http://:3001' link. Guards the graceful no-base
    degradation (mirrors the capability deep-link's 'no base ⇒ no link')."""
    _write(tmp_repo, _SAMPLE)
    monkeypatch.delenv("KONTROLL_MGMT_IP", raising=False)
    monkeypatch.delenv("KONTROLL_DOMAIN", raising=False)
    out = {s["name"]: s["url"] for s in fleet.list_services()}
    assert out["Semaphore"] is None and out["Homepage"] is None


def test_list_services_missing_or_malformed_file_is_empty_never_raises(tmp_repo):
    """A missing config/services.yml returns [] (tmp_repo ships none), and a malformed one also returns [] — never
    raises. The in-process GUI worker must survive a bad data file (service-never-sys.exit); the panel degrades to
    an empty services section, not a 500/crash."""
    assert fleet.list_services() == []                       # absent file
    _write(tmp_repo, "services: [ : : malformed")
    assert fleet.list_services() == []                       # malformed YAML


def test_shipped_services_yml_lists_the_core_control_plane(monkeypatch):
    """The SHIPPED config/services.yml (the real tree — no tmp_repo) parses and lists the core control plane
    (onboard-GUI, Semaphore, Grafana) with templated URLs, and the Grafana link is the URL the capability dialog's
    deep-links are built on (`observe._grafana_base()`): plain HTTP on :3002, which is what Grafana serves.
    Guards the data file kontroll ships with being valid + complete (an empty/typo'd shipped file would give every
    fresh install a blank services section), and guards the two Grafana URLs drifting apart again — the Index
    link said `https://` while the deep-link said `http://`, so one of the two was always dead."""
    monkeypatch.setenv("KONTROLL_MGMT_IP", "203.0.113.19")
    monkeypatch.setenv("KONTROLL_DOMAIN", "x.test")
    svcs = fleet.list_services()
    names = {s["name"] for s in svcs}
    assert {"Onboarding GUI", "Semaphore", "Grafana"} <= names
    assert any(s["url"] == "http://203.0.113.19:3001" for s in svcs)   # Semaphore templated from env
    grafana = next(s["url"] for s in svcs if s["name"] == "Grafana")
    assert grafana == "http://203.0.113.19:3002" == observe._grafana_base()


def test_list_services_is_read_only_by_construction():
    """list_services is pure-read: the shared AST pin (#133) parses fleet.py and asserts list_services calls no
    write/actuation verb (commit/push/apply/…) nor opens a file for writing. Guards the Index read ever gaining an
    actuation path (INVARIANT D* / read-only) — a far more robust guard than a substring grep over the source."""
    assert_read_only("scripts/kontroll/service/fleet.py", "list_services")
