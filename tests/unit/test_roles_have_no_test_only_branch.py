"""No role task is skipped by a variable that only a test harness sets.

WHY: every role's `check` and `backup` entrypoint carried `when: not (molecule_test | default(false) | bool)`, and
the Molecule converge that set the variable therefore skipped every device task — its idempotence step could not
fail, while the documents listed it as the role test. The scenario and the branch were removed on 2026-10-12. The
role TEMPLATE is what a new device class is copied from, and old checkouts still carry the branch, so it would come
back with the next role; this keeps it out. Check mode (`--check --diff` against tests/mock-inventory.yml) is how a
role is evaluated without a device.
"""
import os

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _yaml_files(*parts):
    for base, _dirs, files in os.walk(os.path.join(ROOT, *parts)):
        for name in files:
            if name.endswith((".yml", ".yaml")):
                yield os.path.join(base, name)


def test_no_role_or_play_carries_a_test_only_branch():
    """No task file under `ansible/roles/` (the copy-me template included) and no playbook mentions `molecule_test`,
    and the scenario directory is gone. Guards a condition that makes a "role test" skip the tasks it claims to
    test — a gate that cannot fail — returning with a role copied from the template or from an old branch."""
    offenders = [os.path.relpath(path, ROOT) for path in list(_yaml_files("ansible", "roles")) +
                 list(_yaml_files("ansible", "playbooks"))
                 if "molecule_test" in open(path, encoding="utf-8").read()]
    assert not offenders, "a test-only branch is back — evaluate the role in check mode instead: %s" % offenders
    assert not os.path.exists(os.path.join(ROOT, "tests", "molecule")), "the Molecule scenario is back"
