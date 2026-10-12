"""F3 (trust_mode, Rung A): the OPTIONAL two-tine trust fork's FLAG slice — chosen at fresh-init, validated at
deploy, surfaced read-only in Settings. This edition ships NO auto-promoter (the GUI/API trust boundary is
UNTOUCHED in both modes).

WHY (the failures these guard): a trust posture is the one setting kontroll must never silently default (unlike
tls_mode, which fail-SAFEs to self_signed) — guessing "human-reviewed vs auto promote" is never safe. These pin the
"never a silent default" property at its THREE defences (birth/template/deploy — the init-side birth pin lives in
test_kontroll_init.py), the read-only Settings surfacing, and that no auto-promoter / promote write was introduced
(the whole point of Rung A: posture is explicit + auditable, residual is zero).
"""
import os

import pytest
import yaml

from kontroll.service import settings

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_template_ships_trust_mode_commented_no_active_default():
    """Defence #2 (template): instance.example/instance.yml documents `trust_mode` and ships it COMMENTED — a
    copied-but-unedited overlay has NO active value (so deploy-stack's assert fires). Guards a shipped active
    default silently picking a posture for every fresh node."""
    text = open(os.path.join(ROOT, "instance.example", "instance.yml"), encoding="utf-8").read()
    assert "# trust_mode: separated" in text                     # the assignment line is commented
    lines = [ln.rstrip("\n") for ln in text.splitlines()]
    active = [ln for ln in lines if ln.lstrip().startswith("trust_mode:")]   # an UNcommented assignment
    assert active == [], "instance.example ships an ACTIVE trust_mode (must be commented — never a default): %s" % active


def test_deploy_stack_fail_closed_on_unset_trust_mode():
    """Defence #3 (deploy): deploy-stack.yml derives `_trust_mode` from instance.yml and ASSERTS it is in
    ['solo','separated'] (gated on the network-service tier) — an unset/garbage value HARD-FAILS the deploy.
    Guards a copied/typo'd/hand-deleted trust_mode silently falling back to prod-or-homelab at deploy time."""
    plays = yaml.safe_load(open(os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8"))
    text = open(os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8").read()
    assert "_trust_mode:" in text and "_instance.trust_mode" in text          # the var is derived from instance.yml
    # the assert task exists, checks the closed set, and is gated on the privileged tier (not a metrics-only deploy)
    found = []
    for play in plays if isinstance(plays, list) else []:
        for task in (play.get("tasks") or []) if isinstance(play, dict) else []:
            a = task.get("ansible.builtin.assert") or task.get("assert")
            if isinstance(a, dict) and "_trust_mode in ['solo', 'separated']" in str(a.get("that")):
                found.append(task)
    assert found, "deploy-stack.yml has no fail-closed `_trust_mode in ['solo','separated']` assert"
    gate = str(found[0].get("when") or "")
    assert "stack_services" in gate, "the trust_mode assert must be gated on the network-service tier"


def test_settings_surfaces_unset_trust_mode_as_none(tmp_repo):
    """The read-only Settings surface (F3): when instance.yml carries NO trust_mode (the commented-template state),
    `read_view().identity.trust_mode` is None — the panel shows 'not set' rather than inventing a posture. Guards
    the surface fabricating a default the instance didn't choose."""
    (tmp_repo / "config" / "instance.yml").write_text(
        "mgmt_ip: 192.0.2.50\ndomain: x.test\nfrontend:\n  tls_mode: self_signed\n", encoding="utf-8")
    assert settings.read_view()["identity"]["trust_mode"] is None


def test_settings_panel_renders_trust_mode_readonly_no_edit_knob():
    """The Settings panel renders a `settings-trust-mode` READ-ONLY row (no `edit` knob — this edition ships no
    auto-promoter, so the value changes nothing a control could set). Guards the flag silently gaining a write
    control before its security review (the row must stay read-only)."""
    html = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert "settings-trust-mode" in html                                     # the read-only surfacing row
    # it is a plain meta row, NOT a settingsKnobRow (which would add an /api/settings/<knob> edit control)
    assert "settingsKnobRow(g, 'trust_mode'" not in html, "trust_mode must be read-only (no edit knob) in Rung A"


def test_no_auto_promoter_introduced_in_rung_a():
    """This edition ships the FLAG only — NO auto-promoter (zero new security residual). The documents say so in
    the present tense ("this edition has no auto-promoter"), and this test is what makes that sentence machine-held:
    the permissive edition's script and service fragment must not arrive here by a merge."""
    assert not os.path.exists(os.path.join(ROOT, "scripts", "kontroll-autopromoter.py"))
    assert not os.path.exists(os.path.join(ROOT, "docker", "services", "autopromoter.yaml"))
