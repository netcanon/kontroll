"""Two onboard-form UX gates (operator request, 2026-06-19):
  (1) the 'also push origin (offsite backup)' checkbox shows ONLY when an OFFSITE git remote exists — hidden on a
      fresh node, where `git push origin` would just no-op/fail and the box only confuses;
  (2) the form collapses only while PRISTINE — a peek can be dismissed, but entered text / ticked boxes are never
      silently dropped (the toggle disables itself when the form holds input).

`gitio.offsite_remote_exists` is the server predicate (passed to the template as `has_remote`); the template
wires both gates client-side. The live click behaviour is exercised by e2e (test_onboard_flow.py); here we pin
the server predicate and the template wiring hermetically.
"""
import os
import subprocess

import pytest

from kontroll import gitio

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_offsite_remote_exists_distinguishes_local_from_offsite(tmp_path, monkeypatch):
    """No origin -> False; a local `file://` canonical -> False (not an offsite backup); a real https/ssh remote
    -> True. This is the predicate that hides the push checkbox on a fresh node."""
    monkeypatch.setattr(gitio.paths, "ROOT", str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    assert gitio.offsite_remote_exists() is False                                              # no origin
    subprocess.run(["git", "remote", "add", "origin", "file:///srv/kontroll.git"],
                   cwd=str(tmp_path), check=True)
    assert gitio.offsite_remote_exists() is False                                              # local file:// canonical
    subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/acme/x.git"],
                   cwd=str(tmp_path), check=True)
    assert gitio.offsite_remote_exists() is True                                               # real offsite target


def test_onboard_form_template_wires_both_gates():
    """The template embeds the server flag, gates the push checkbox on it, defines the collapse predicate +
    disables the toggle while dirty, and guards the now-optional push box in the submit body. The push box is part
    of the form's LITERAL markup and is REMOVED when there is no offsite remote: it is no longer interpolated into
    the markup, because only literals reach innerHTML (SECURITY.md C19). The gate — no offsite remote, no push box
    — is what this pins; a box that stayed on a fresh node would offer a push that can only fail."""
    html = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert "const HAS_REMOTE = {{" in html                                  # server flag embedded into the page
    assert 'name="push" data-testid="field-push"' in html                   # the box is authored in the literal…
    assert "if (!HAS_REMOTE) f.querySelector('[name=push]').closest('label').remove();" in html   # …and gated here
    assert "function onbPristine" in html                                   # collapse-gate predicate
    assert "btn.disabled = open && !onbPristine" in html                    # toggle disabled while the form is dirty
    assert "push:(g('push') ? g('push').checked : false)" in html           # submit guards the now-optional box


def test_the_collapse_gate_counts_a_typed_secrets_domain_as_input():
    """The onboard form's collapse predicate reads the secrets-domain box among its named fields. Guards the box,
    which ships empty since S27 (its placeholder names the backend's default), being dropped from the list: a typed
    domain would then be discarded by a collapse with no question asked (mutant G of T18's read, D5)."""
    html = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert "const named = ['group','host','host_name','secrets'].map(" in html


def test_the_domain_box_says_a_domain_is_required_when_the_backend_names_no_default():
    """The onboard form's domain-box placeholder falls back, for a backend whose registry entry declares no default
    domain, to a line saying one is required, and, for a request that names no backend, to the backend's default.

    Guards the box going silently blank for such a backend (T18's read,
    D13; mutant M5 of its fold read restored the former `if (sd && r.secrets)` line and survived every suite): the
    operator would then submit no domain and meet a refusal the form never warned of."""
    html = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert "if (sd) sd.placeholder = r.secrets || (backend ? 'required — this backend names no default domain'" in html
    assert ": \"the backend's default\");" in html          # no backend named (fold-2 read, N3: X6)
