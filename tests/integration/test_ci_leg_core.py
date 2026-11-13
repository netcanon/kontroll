"""A CI pytest leg runs its rows on the ansible-core the leg pins (I-94).

WHY. Each leg of ci.yml's `tests` job installs the ansible-core its matrix names and sets KONTROLL_ANSIBLE_CORE to it.
The rows that run Ansible find ansible-playbook on PATH, and the GitHub runner image carries one of its own (a pipx
install, unpinned, behind a `python -E` launcher: a core from 2.21, which no shipped piece runs, when I-94 was found).
Were that one first on PATH, or the pin installed into a Python the suite does not run, every row would run on the
image's core while the matrix named another, and a green leg would say a core was tested that was not. This row fails
such a leg. Where the variable is unset (a developer's run, the validate job) it is skipped: there the core is
whatever the run has, and tests/unit/test_ci_ansible_cores.py holds the pins themselves to the cores the project ships.
"""
import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_the_rows_find_the_core_their_leg_pins():
    """Where KONTROLL_ANSIBLE_CORE is set, the first ansible-playbook on PATH reports that core (`[core <pin>]` on its
    first line), and the Python running the suite imports that same ansible-core under `-I` (as
    test_no_log_censor_runs.py's subprocesses import the core they patch a copy of). Guards a leg whose rows ran on the
    runner image's core while ci.yml named another: an earlier PATH entry, or a pin installed into another Python."""
    pin = os.environ.get("KONTROLL_ANSIBLE_CORE", "")
    if not pin:
        pytest.skip("KONTROLL_ANSIBLE_CORE is unset: not a pinned CI leg")
    exe = shutil.which("ansible-playbook")
    assert exe, "the leg pins ansible-core %s and finds no ansible-playbook on PATH" % pin
    run = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120)
    first = (run.stdout.splitlines() or [""])[0]
    assert first.endswith("[core %s]" % pin), (exe, first, run.stderr[-800:])
    imp = subprocess.run([sys.executable, "-I", "-c", "import ansible.release as r; print(r.__version__)"],
                         capture_output=True, text=True, timeout=120)
    assert imp.stdout.strip() == pin, (sys.executable, imp.stdout, imp.stderr[-800:])
