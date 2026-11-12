"""pytest collects the whole suite on a machine whose PATH holds none of the tools the rows shell out to.

WHY. A test file whose rows need a tool (ansible-playbook, sops, git, ssh) skips them with a `skipif`, but the code at
the module's top level runs at collection, before any skip. test_refuse_missing_entrypoint_runs.py read
`ansible-playbook --version` there, so on a runner with no ansible-playbook (the private repo's self-hosted runners,
a developer's machine) every pytest run stopped at collection with FileNotFoundError, while the CI runners that ship
ansible passed it, so CI never saw it (I-98). This row collects the suite as CI's pytest job selects it, with PATH
naming an empty directory, so a top-level call of any tool by name fails here, on every machine that runs the suite.
"""
import os
import re
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name == "nt", reason="the suite's tools and paths are POSIX ones; CI and the validate image "
                                               "run this row"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_the_suite_collects_with_no_tool_on_path(tmp_path):
    """`pytest --collect-only -m "not e2e and not slow"` (CI's offline selection) from the repo root, its own Python
    named by full path and PATH an empty directory, exits 0 with no collection error. Guards I-98: a module-level call
    of ansible-playbook stopped every pytest run on a machine without it. KONTROLL_REQUIRE_CHECKOUT is dropped for the
    inner run: with no git on PATH the conftest finds no checkout, and that rule (a reason is a session error) is
    about the rows that run, which a collection runs none of."""
    empty = tmp_path / "bin"
    empty.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("KONTROLL_REQUIRE_CHECKOUT", "PYTEST_ADDOPTS")}
    env.update(PATH=str(empty), PYTHONDONTWRITEBYTECODE="1")
    # pyproject's addopts (`-q --strict-markers`) set here alone, so the verbosity is the one the asserts read
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts=", "-q", "--strict-markers",
                        "-p", "no:cacheprovider", "-m", "not e2e and not slow"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out[-6000:]
    assert not re.search(r"^ERROR ", out, re.M), out[-6000:]
    counted = re.findall(r"^\d+(?:/\d+)? tests? collected.*$", out, re.M)
    assert len(counted) == 1 and "error" not in counted[0], out[-6000:]
    assert "tests/integration/test_refuse_missing_entrypoint_runs.py::" in out, out[-6000:]
