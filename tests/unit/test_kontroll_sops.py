"""ansible/filter_plugins/kontroll_sops.py — the fail-soft decrypt-BY-NAME reader (the generic union decrypt that
kills the hardcoded _sops_domains map).

Pins the PER-DOMAIN fail-soft contract the deploy-stack `.env` render depends on (M1): an absent / undecryptable
domain yields {} and NEVER raises, so `map('kontroll_sops_domain', secrets_dir)` over the union can't abort the
other domains or hard-fail the deploy — "a fresh node without proxmox onboarded still deploys, that token empty".
The value path (a present, decryptable file -> its dict) is faked here (no real age key needed). Design + must-fix
bar: docs/reviews/2026-06-17-secret-injection/.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "ansible", "filter_plugins"))
import kontroll_sops  # noqa: E402
from kontroll_sops import kontroll_sops_domain  # noqa: E402

pytestmark = pytest.mark.unit


def test_absent_domain_file_is_fail_soft_empty():
    """An un-onboarded domain (the .sops.yml absent on a fresh node), or an empty domain/dir, -> {} — NEVER a raise.
    This is the per-domain fail-soft the union-decrypt relies on (M1): one absent file can't abort the others."""
    assert kontroll_sops_domain("proxmox", "/no/such/dir") == {}
    assert kontroll_sops_domain("", "/tmp") == {}
    assert kontroll_sops_domain("proxmox", "") == {}
    assert kontroll_sops_domain(None, None) == {}


def test_decrypt_failure_is_fail_soft_never_raises(tmp_path, monkeypatch):
    """A PRESENT file whose decrypt FAILS (no key / bad recipients -> sops rc!=0) OR whose sops binary is missing
    -> {}, never an exception. A down/unreadable cred degrades one stream, never the whole deploy (fail-SOFT)."""
    (tmp_path / "proxmox.sops.yml").write_text("enc: x")
    monkeypatch.setattr(kontroll_sops.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    assert kontroll_sops_domain("proxmox", str(tmp_path)) == {}              # rc != 0 -> {}

    def _boom(*a, **k):
        raise FileNotFoundError("sops not installed")
    monkeypatch.setattr(kontroll_sops.subprocess, "run", _boom)
    assert kontroll_sops_domain("proxmox", str(tmp_path)) == {}              # sops missing -> {} (no raise)


def test_successful_decrypt_returns_the_parsed_dict(tmp_path, monkeypatch):
    """A present, decryptable file -> the parsed YAML dict (the value path). The filter returns the dict but never
    logs/prints it; it runs inside the no_log render task, so the value stays inside that boundary (C12)."""
    (tmp_path / "proxmox.sops.yml").write_text("enc")
    monkeypatch.setattr(kontroll_sops.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="proxmox_api_user: root@pam\n"))
    assert kontroll_sops_domain("proxmox", str(tmp_path)) == {"proxmox_api_user": "root@pam"}


def test_a_domain_that_is_not_a_secret_domain_name_reads_no_file(tmp_path, monkeypatch):
    """A domain spelled as a path (`./pve-lab`, `x/../pve-lab` beside a directory `x`, `../<dir>/pve-lab`, an
    absolute one, `pve-lab.x`), one the Secrets dialog would not take (`Pve-lab`, `pve-lab `, `-pve`, a list), and a
    platform-core domain (`dashboards`, `semaphore`) each yield {} without running sops, though the file each names
    is there; `pve-lab` and `9site`, which the dialog writes, decrypt. Why: the filter joins the domain into a path,
    so `./dashboards` reached the platform-core dashboards file (the Grafana admin password, the alert URL) for a
    drop-in that gen-secret-env's exact-name refusal let through (T32 fold 3 read B, S1); a rule weakened to its first
    letter passed every spelling this tried while no file it named was there, and the filter read a domain the dialog
    writes as {} (fold 4's reads 90, N5 and 100, N1, N2). The generator refuses the same names, so the deploy does not
    rest on the committed manifest alone."""
    (tmp_path / "x").mkdir()
    for name in ("pve-lab", "9site", "dashboards", "semaphore", "pve-lab.x", "Pve-lab", "pve-lab ", "-pve"):
        (tmp_path / ("%s.sops.yml" % name)).write_text("enc")
    ran = []
    monkeypatch.setattr(kontroll_sops.subprocess, "run", lambda *a, **k: ran.append(a) or types.SimpleNamespace(
        returncode=0, stdout="alert_notifier_url: https://example.test/x"))
    for spelling in ("./pve-lab", ".//pve-lab", "x/../pve-lab", "../%s/pve-lab" % tmp_path.name,
                     str(tmp_path / "pve-lab"), "pve-lab.x", "Pve-lab", "pve-lab ", "-pve", "dashboards", "semaphore"):
        assert os.path.exists(os.path.join(str(tmp_path), "%s.sops.yml" % spelling)), spelling
        assert kontroll_sops_domain(spelling, str(tmp_path)) == {}, spelling
    assert kontroll_sops_domain(["pve-lab"], str(tmp_path)) == {}
    assert ran == []
    for domain in ("pve-lab", "9site"):
        assert kontroll_sops_domain(domain, str(tmp_path)) == {"alert_notifier_url": "https://example.test/x"}
