"""The C10 canonical group (gid 1001) must be PROVISIONED by the host floor, not left as a documented manual step.

WHY (the failure this guards — hit LIVE on the 2026-06-29 `.52` baked-prod dogfood, "F-CANON" / task_51334deb):
the canonical bare repo `/srv/kontroll.git` is group-gid-1001 (the uid:gid the privileged api/onboard-gui write
proposals as). The operator's `kontroll-promote` advances `main` and DELETES the consumed `proposed/<run_id>` ref —
which fails "Permission denied" unless the operator is a member of that group. `local-canonical.yml` provisions the
group + membership on a HOST-DIRECT run but SKIPS them inside the installer container (`when: not in_container`),
which is exactly how the published-kit (`kontroll.sh`) and local-build (`kontroll-installer.sh`) launchers run the
deploy. So a published-kit install left the group unprovisioned and every promote failed — the gesture was only
PRINTED as a manual `sudo groupadd … && sudo usermod …` step that the operator never ran.

The fix is the shared `scripts/lib/canonical-group.sh`: the root host-floor installer (`install-prereqs.sh`) CREATES
the group + adds the operator, and the operator launchers ENSURE-OR-WARN (auto-provision via sudo if available, else
print the exact one-liner loudly — never silently). This test pins (1) the helper defines all three verbs and names
gid 1001 + groupadd + usermod, (2) each of the three scripts sources the helper and calls the right verb, and (3) the
old PRINTED manual `groupadd -g 1001` step is GONE from the install prose — so a refactor that drops the gesture or
re-demotes it to a manual step (re-breaking a fresh published-kit install's first promote) fails loudly here.

A live shell run isn't unit-testable hermetically (the Windows dev box vs Linux CI differ on PATH separators and
whether `sudo` exists), so — as with `test_local_canonical.py` for the playbook — this asserts the wiring statically;
the behaviour is dogfood-verified on a real box. SECURITY.md C10.
"""
import os
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "scripts", "lib", "canonical-group.sh")
PREREQS = os.path.join(ROOT, "scripts", "install-prereqs.sh")
LAUNCHER_PUB = os.path.join(ROOT, "scripts", "kontroll.sh")
LAUNCHER_BUILD = os.path.join(ROOT, "scripts", "kontroll-installer.sh")
INSTALL = os.path.join(ROOT, "scripts", "install.sh")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_helper_defines_the_three_verbs_and_the_privileged_gestures():
    """scripts/lib/canonical-group.sh defines satisfied/ensure/ensure-or-warn and names the load-bearing pieces:
    gid 1001, the `kontroll` group, and the actual `groupadd`/`usermod` privileged commands. Guards the helper
    being emptied into a no-op (which would silently re-open F-CANON)."""
    src = _read(LIB)
    for fn in ("canonical_group_satisfied", "canonical_group_ensure", "canonical_group_ensure_or_warn"):
        assert re.search(r"^%s\s*\(\)" % re.escape(fn), src, re.M), "helper must define %s()" % fn
    assert "1001" in src, "the helper must name the canonical gid 1001"
    assert "groupadd" in src and "usermod" in src, "the helper must actually create the group + add the operator"
    # ensure-or-warn must be NON-fatal (the install proceeds) — it always returns 0 on its warn path.
    assert "return 0" in src.split("canonical_group_ensure_or_warn", 1)[1], \
        "ensure_or_warn must never be fatal (warn then return 0) — a missing group must not abort the install"


def test_install_prereqs_sources_helper_and_ensures_as_root():
    """The root host-floor installer sources the helper and calls `canonical_group_ensure` (it is root, so it CREATES
    the group directly) — the primary provisioning path. Guards the regression to a printed-only manual step."""
    src = _read(PREREQS)
    assert "lib/canonical-group.sh" in src, "install-prereqs.sh must source the canonical-group helper"
    assert "canonical_group_ensure" in src, "install-prereqs.sh must call canonical_group_ensure (root provisions it)"


@pytest.mark.parametrize("path,name", [(LAUNCHER_PUB, "kontroll.sh"), (LAUNCHER_BUILD, "kontroll-installer.sh")])
def test_launchers_source_helper_and_ensure_or_warn(path, name):
    """Both launchers (published kit + local build) — which run the deploy IN-CONTAINER, where local-canonical.yml
    skips the host group gesture — source the helper and call `canonical_group_ensure_or_warn` for the invoking
    operator, so a box that skipped install-prereqs.sh (the live `.52` scenario) still auto-provisions or warns loudly.
    Guards each launcher path independently."""
    src = _read(path)
    assert "lib/canonical-group.sh" in src, "%s must source the canonical-group helper" % name
    assert "canonical_group_ensure_or_warn" in src, "%s must call canonical_group_ensure_or_warn" % name
    assert "KONTROLL_OPERATOR_USER" in src, "%s must pass the invoking operator's identity to the gesture" % name


@pytest.mark.parametrize("path,name", [(PREREQS, "install-prereqs.sh"), (INSTALL, "install.sh"),
                                       (os.path.join(ROOT, "docker", "installer", "README.md"),
                                        "the installer README")])
def test_manual_groupadd_step_is_no_longer_printed_as_a_required_action(path, name):
    """The old PRINTED 'sudo groupadd -g 1001 kontroll && sudo usermod …' manual step must be GONE from the install
    prose — the two scripts' closing messages and the installer README, which kept it in a copy of the sequence
    after the scripts had lost it — because it is now automated. Guards a doc-rot regression where the step is
    re-demoted to manual (the original F-CANON footgun: an instruction the operator can skip → first promote fails)."""
    src = _read(path)
    assert not re.search(r"groupadd\s+-g\s+1001\s+kontroll\s*&&\s*sudo\s+usermod", src), \
        "%s still prints the manual groupadd+usermod step — it is automated now (remove the printed instruction)" % name
