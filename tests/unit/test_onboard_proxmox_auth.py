"""F1 seam S1 (Rung 1) — onboarding a PVE node through the CURATED proxmox class derives its coherent multi-field
api-token auth_set and stores the token FLAT under the domain field names the pve-exporter reads, so telemetry is fed
and Grafana populates (the empty-Grafana dogfood symptom closed end-to-end).

WHY (the failures these guard): pre-Rung-1 a proxmox onboard showed the generic single api_token box and stored the
value HOST-keyed (<host>_api_token) — but the pve-exporter reads flat proxmox_api_user/_token_id/_token_secret from
the proxmox SOPS domain, so it found nothing → empty Grafana. These pin: the reuse path derives the 3-field auth_set
(not the union); the SHARED creds land FLAT (not host-keyed) under the exporter's exact names; a half-filled required
set is refused (coherence — you can't half-authenticate); and a values-free dry-run still plans (INVARIANT D*: a
read/preview never gates onboarding).
"""
import pytest

from kontroll import catalog, probe
from kontroll.service import onboard

pytestmark = pytest.mark.unit

# A complete proxmox API-token credential (the three coherent parts). Values are synthetic — they only ever live in
# the in-memory creds_to_set and are never echoed; the tests assert on the KEYS, not by leaking the values.
PROXMOX_CREDS = {"api_user": "monitor@pve", "api_token_id": "kontroll", "api_token_secret": "SYNTHETIC-not-a-secret"}


def _proxmox_plan(monkeypatch, creds=None):
    """A REUSE onboard of community.proxmox as the curated `proxmox` class (key == the shipped module's key, so
    build_onboard_plan reuses modules/proxmox with its auth: block). No paths.ROOT repoint ⇒ it reads the SHIPPED
    module — the point: exercise the real curated auth_set, not a synthetic stub. Canned deep-probe so the collection
    reads installed."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"community.proxmox": "1.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "1.0.0", "local", "deep")
        f["modules"] = ["proxmox_kvm"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)
    return onboard.build_onboard_plan("community.proxmox", "proxmox", "hypervisors", "192.0.2.40",
                                      host_name="pve-1", secrets="proxmox", creds=creds)


def test_proxmox_reuse_derives_the_grouped_api_token_auth_set(monkeypatch):
    """The reuse path derives the curated 3-field auth_set (user/token_id/token_secret, all in group proxmox_api),
    NOT the generic single api_token / SSH union — so the blind user sees one credential with parts. Guards the
    multi-field-mangled-into-one-box defect at the planner."""
    plan = _proxmox_plan(monkeypatch)
    assert plan["error"] is None and plan["module_reuse"] is True
    fields = {cf["field"]: cf for cf in plan["cred_fields"]}
    assert set(fields) == {"api_user", "api_token_id", "api_token_secret"}
    assert all(cf["auth_set"] == "proxmox_api" for cf in fields.values())
    assert "api_token" not in fields and "ssh_private_key" not in fields   # not the generic union
    assert fields["api_token_secret"]["secret"] is True
    assert fields["api_user"]["secret"] is False and fields["api_token_id"]["secret"] is False


def test_shared_creds_land_flat_under_the_exporter_names_not_host_keyed(monkeypatch):
    """The SHARED proxmox creds store FLAT as proxmox_api_user/_token_id/_token_secret — the exact names the
    pve-exporter's secret_env_map reads — NOT host-keyed (<host>_…). THE fix for empty Grafana: a host-keyed token is
    invisible to the one control-node exporter that serves the whole domain. Guards the keying regressing."""
    plan = _proxmox_plan(monkeypatch, creds=PROXMOX_CREDS)
    keys = set(plan["creds_to_set"])
    assert keys == {"proxmox_api_user", "proxmox_api_token_id", "proxmox_api_token_secret"}, \
        "shared exporter creds must be flat (the exporter reads them by these names), never <host>_-prefixed"
    assert not any(k.startswith("pve_1_") for k in keys)
    # the secret transits the in-memory creds_to_set only (never echoed) and is NOT mangled into a host inline var
    assert "proxmox_api_token_secret" in plan["creds_to_set"]
    assert "SYNTHETIC-not-a-secret" not in plan["host_text"]   # the secret never lands in the inventory drop-in text


def test_partial_auth_set_is_refused_for_coherence(monkeypatch):
    """A PARTIALLY-filled required auth_set (user only, token_id + secret missing) is refused with the planner's
    designed refusal, `OnboardRefusal`, BEFORE any creds_to_set write — you can't half-authenticate a proxmox token (a
    broken cred → silently empty Grafana). Guards a half credential being stored, and the refusal's type: the API and
    the GUI answer `OnboardRefusal` with a 422 and its sentence, while since T20 a plain ValueError is an unexpected
    exception and answers a 500 `internal error` (T20's read B, S2: `pytest.raises(ValueError)` passed either)."""
    with pytest.raises(onboard.OnboardRefusal, match="incomplete credential set"):
        _proxmox_plan(monkeypatch, creds={"api_user": "monitor@pve"})


def test_empty_auth_set_still_plans_a_dry_run(monkeypatch):
    """A values-free dry-run (the GUI's first step — preview the fields before typing) plans fine: coherence only
    bites a PARTIALLY-filled set, never an empty one, so a blind user is never blocked from previewing (INVARIANT
    D*). Guards the coherence guard over-firing on the common preview path."""
    plan = _proxmox_plan(monkeypatch, creds={})
    assert plan["error"] is None and plan["creds_to_set"] == {}
    assert {cf["field"] for cf in plan["cred_fields"]} == {"api_user", "api_token_id", "api_token_secret"}
