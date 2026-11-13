"""The ONE configurable attribution trailer on machine-made commits (gitio.commit_trailer / commit_message_args).

WHY — until the 2026-10-08 public-split review (F9) eighteen call sites in the API, the GUI and galaxy.py each
carried a literal `Co-Authored-By: <the model that wrote the code>` trailer, stamped into every commit the control
plane makes on an OPERATOR's behalf: wrong attribution, eighteen places to edit, and a model name baked into a
public tool's runtime output. The fix is one seam — every commit path builds its `-m` list through
`gitio.commit_message_args`, which appends the trailer from `KONTROLL_COMMIT_TRAILER` (default: the tool's own
identity; `none`/`off`/`disabled` turn it off). These tests pin the seam's contract AND two gates that keep the
seam the only way to commit: no attribution literal may come back into a call site, and no `["git", "commit"]`
argv outside gitio may bypass `commit_message_args` (a raw `-m` list is how the literals got there to begin with).
"""
import ast
import os
import re
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from kontroll import gitio  # noqa: E402

_SEAM = "scripts/kontroll/gitio.py"
# Every tree a runtime commit path lives in. docs/ is excluded on purpose: prose may quote the trailer.
_SCAN_DIRS = ("api", "gui", "scripts")


def _runtime_py_files():
    for top in _SCAN_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules")]
            for fn in filenames:
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")
                    if rel != _SEAM:
                        yield rel


def test_default_trailer_is_appended_once_and_last(monkeypatch):
    """With the variable unset (and with it empty — the compose `${VAR:-}` soft default passes an EMPTY string, not
    an absent one), commit_message_args appends the tool's default trailer exactly once, as the LAST -m. Guards the
    default silently disappearing under the compose soft-default, and the trailer landing before the body."""
    for value in (None, "", "   "):
        if value is None:
            monkeypatch.delenv(gitio.COMMIT_TRAILER_ENV, raising=False)
        else:
            monkeypatch.setenv(gitio.COMMIT_TRAILER_ENV, value)
        args = gitio.commit_message_args(["feat(x): subject", "the body"])
        assert args == ["-m", "feat(x): subject", "-m", "the body", "-m", gitio.DEFAULT_COMMIT_TRAILER], value
        assert args.count(gitio.DEFAULT_COMMIT_TRAILER) == 1


def test_default_trailer_names_the_tool_never_a_model_or_person():
    """The default attribution is the control plane's own commit identity (the containers commit as
    `kontroll <kontroll@localhost>`). A model name or a personal address here would re-create the defect the seam
    exists to remove."""
    assert gitio.DEFAULT_COMMIT_TRAILER == "Co-Authored-By: kontroll <kontroll@localhost>"
    low = gitio.DEFAULT_COMMIT_TRAILER.lower()
    assert "claude" not in low and "anthropic" not in low and "@gmail" not in low


def test_operator_override_replaces_the_trailer(monkeypatch):
    """KONTROLL_COMMIT_TRAILER=<text> makes <text> the one trailer (whitespace-trimmed) — the documented .env
    contract (docker/.env.example): a deployment that wants its own attribution gets exactly that line."""
    monkeypatch.setenv(gitio.COMMIT_TRAILER_ENV, "  Co-Authored-By: ops-bot <ops@example.com>  ")
    assert gitio.commit_trailer() == "Co-Authored-By: ops-bot <ops@example.com>"
    assert gitio.commit_message_args(["s"]) == ["-m", "s", "-m", "Co-Authored-By: ops-bot <ops@example.com>"]


@pytest.mark.parametrize("off", ["none", "NONE", " None ", "off", "OFF", "disabled", "Disabled"])
def test_off_switch_tokens_disable_the_trailer(monkeypatch, off):
    """`none`, `off` and `disabled` (any case, whitespace-trimmed) yield NO trailer at all — the same off-switch
    vocabulary as the API rate-limit policy (api/ratelimit.py), so an operator who writes
    KONTROLL_COMMIT_TRAILER=off gets no trailer instead of the literal word `off` stamped into every commit."""
    monkeypatch.setenv(gitio.COMMIT_TRAILER_ENV, off)
    assert gitio.commit_trailer() == ""
    assert gitio.commit_message_args(["s", "b"]) == ["-m", "s", "-m", "b"]


def test_trailer_is_read_dynamically_and_never_duplicated(monkeypatch):
    """The trailer is resolved per call (a deploy's .env and a test's monkeypatch both take effect without a module
    reload), and a caller that already carries the trailer line gets it ONCE. Guards an import-time snapshot and a
    double trailer on a migrated call site."""
    monkeypatch.setenv(gitio.COMMIT_TRAILER_ENV, "Signed-off-by: a <a@example.com>")
    first = gitio.commit_message_args(["s"])
    monkeypatch.setenv(gitio.COMMIT_TRAILER_ENV, "Signed-off-by: b <b@example.com>")
    second = gitio.commit_message_args(["s"])
    assert first[-1] != second[-1] and second[-1] == "Signed-off-by: b <b@example.com>"
    dup = gitio.commit_message_args(["s", "Signed-off-by: b <b@example.com>"])
    assert dup.count("Signed-off-by: b <b@example.com>") == 1


def test_commit_and_push_commits_through_the_seam(monkeypatch):
    """commit_and_push (the API/GUI write seam) must build its `git commit` from commit_message_args — so the one
    trailer reaches every network-service commit, with the caller's lines untouched and in order. Captures the
    narrated `_run` calls (the repo's standard git seam) instead of running git, and mocks the landing beside it
    (`land_main`: else the row lands this checkout's HEAD on the canonical its `local` remote names, I-70)."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: 0)
    monkeypatch.setattr(gitio, "_push_target", lambda run_id=None: "main")
    monkeypatch.setenv(gitio.COMMIT_TRAILER_ENV, "Co-Authored-By: ops-bot <ops@example.com>")
    out = gitio.commit_and_push(["instance/fleet.yml"], ["feat(onboard): x", "body line"])
    assert out["committed"] is True
    commit = next(c for c in calls if c[:2] == ["git", "commit"])
    assert commit == ["git", "commit", "-m", "feat(onboard): x", "-m", "body line",
                      "-m", "Co-Authored-By: ops-bot <ops@example.com>"]


def test_no_call_site_carries_an_attribution_literal():
    """Grep-gate: outside the seam itself, no runtime Python under api/, gui/ or scripts/ may contain a
    `Co-Authored-By:` literal — that is how eighteen copies of a wrong attribution accumulated. A new commit path
    builds its message through gitio.commit_message_args (or commit_and_push), full stop."""
    pat = re.compile(r"Co-Authored-By:", re.I)
    offenders = []
    for rel in _runtime_py_files():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if pat.search(line):
                    offenders.append("%s:%d" % (rel, n))
    assert offenders == [], "attribution literal outside gitio (use gitio.commit_message_args): %s" % offenders


def _list_prefix(node):
    """The leading string constants of a list literal, or None if `node` is not a list."""
    if not isinstance(node, ast.List):
        return None
    out = []
    for el in node.elts:
        out.append(el.value if isinstance(el, ast.Constant) and isinstance(el.value, str) else None)
    return out


def _is_seam_call(node):
    """True for `commit_message_args(...)` / `gitio.commit_message_args(...)`."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
    return name == "commit_message_args"


def test_no_git_commit_argv_bypasses_the_seam():
    """AST gate: every `["git", "commit", …]` argv literal in runtime Python outside gitio must be the left operand
    of `+ commit_message_args(...)` — the shape galaxy.py's two inline commits use. A raw `["git", "commit", "-m",
    …]` list (what the eighteen literal sites were) would commit without the configurable trailer and the grep-gate
    above would never see it, because the bypass carries no literal. Pins the seam as the ONLY way a runtime path
    commits."""
    offenders = []
    for rel in _runtime_py_files():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            prefix = _list_prefix(node)
            if not prefix or prefix[:2] != ["git", "commit"]:
                continue
            parent = parents.get(node)
            ok = (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add) and parent.left is node
                  and _is_seam_call(parent.right))
            if not ok:
                offenders.append("%s:%d" % (rel, node.lineno))
    assert offenders == [], (
        "a `git commit` argv that does not go through gitio.commit_message_args (use "
        "`[\"git\", \"commit\"] + gitio.commit_message_args([...])` or gitio.commit_and_push): %s" % offenders)


def test_galaxy_inline_commits_use_the_seam():
    """The two operator-CLI commits in scripts/galaxy.py (`onboard --commit`, the capture-exception add) are the only
    runtime `git commit` argvs outside commit_and_push; both must exist and both must build through the seam. A
    refactor that routes one of them around commit_message_args is caught by the AST gate above; this test pins
    that the sites are still there (so the gate is not vacuously green)."""
    with open(os.path.join(ROOT, "scripts", "galaxy.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count('["git", "commit"] + gitio.commit_message_args([') == 2, \
        "galaxy.py must carry exactly its two seam-built commits"
