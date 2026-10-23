"""docs/break-glass.md — the procedure an operator runs against an existing instance's secrets — spells the commands
the way the tools take them.

WHY. The runbook adds a recipient to every domain and re-wraps the ciphertext. A command in it that the tool spells
differently fails half-way through a procedure on live secrets; a blanket `git add` in it would commit whatever else
the operator's checkout holds alongside the re-wrapped files. The commands come from the code: the re-wrap is
`keygen.SOPS_UPDATEKEYS`, the flag is kontroll-init's own.
"""
import contextlib
import importlib.util
import io
import os
import re

import pytest

from kontroll.service import keygen

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _init_help():
    """kontroll-init's --help text, from its own parser."""
    spec = importlib.util.spec_from_file_location("kontroll_init_doc", os.path.join(ROOT, "scripts", "kontroll-init.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        mod.main(["--help"])
    return out.getvalue()


def test_the_runbook_uses_the_tools_own_commands():
    """docs/break-glass.md carries the re-wrap exactly as `keygen.SOPS_UPDATEKEYS` spells it (the `--config` form),
    the `--break-glass-recipient` flag as kontroll-init's parser spells it, the installer shell it runs them in, the
    landing through `local-canonical.yml` with its `--check --diff` first, and a scoped `git add --` with no blanket
    add or commit-all. This checks spelling, not that a command runs: one spelled right still fails on a host without
    its tools, which is why the runbook runs them in the installer shell (MF-04). Guards the runbook drifting from the
    code it tells an operator to run against their secrets, and a blanket add committing more than the re-wrap."""
    doc = open(os.path.join(ROOT, "docs", "break-glass.md"), encoding="utf-8").read()
    assert keygen.SOPS_UPDATEKEYS in doc
    assert "--break-glass-recipient" in _init_help()
    assert "kontroll-init.py --break-glass-recipient age1" in doc
    assert "scripts/kontroll-installer.sh shell" in doc
    assert "kontroll-installer.sh playbooks/local-canonical.yml --check --diff" in doc
    assert re.search(r"git add -- instance/\.sops\.yaml instance/secrets\b", doc)
    assert not re.search(r"git add (-A|--all|\.)(\s|$)|git commit -a", doc)
