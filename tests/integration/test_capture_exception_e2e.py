"""capture-exception add subcommand — dry-run plans nothing; --commit mutates a tmp repo
idempotently and drives the local-canonical commit/push (git mocked). Mirrors onboard's gate.
kontroll.paths.ROOT is repointed at tmp_repo so it never touches the real matrix.
"""
import types

import pytest
import yaml

import galaxy
from kontroll import gitio

pytestmark = pytest.mark.integration


def _args(**over):
    """Build a SimpleNamespace of subcommand args, overriding defaults per test."""
    base = dict(subject="acme", match="Acme_*", behavior="non_deterministic",
                disposition="exclude_from_history", observed="re-serializes each fetch",
                source="user", commit=False, push=False)
    base.update(over)
    return types.SimpleNamespace(**base)


def _matches(tmp_repo):
    """The `match` values from the PARSED matrix file (robust to quoting style)."""
    doc = yaml.safe_load((tmp_repo / "config" / "capture-exceptions.yml").read_text(encoding="utf-8"))
    return [e["match"] for e in (doc or {}).get("exceptions", [])]


def test_dryrun_writes_nothing(tmp_repo, capsys):
    """A bare add (no --commit) prints the plan but leaves the matrix untouched."""
    galaxy.cmd_capture_exception_add(_args())
    assert "capture-exception add" in capsys.readouterr().out
    assert "Acme_*" not in _matches(tmp_repo)                   # inert


def test_commit_applies_and_lands_on_the_local_canonical(tmp_repo, capsys, monkeypatch):
    """--commit appends the entry, then git-add/commits and LANDS on the LOCAL canonical through `gitio.land_main`
    (not origin, and never a `git push` of `main` — the canonical's hook refuses one from everyone). Both seams
    are patched: a real `land_main` would land on whatever canonical the checkout's `local` remote names."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(cmd) or 0)
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: calls.append(["kontroll-land", "main"]) or 0)
    galaxy.cmd_capture_exception_add(_args(commit=True))
    out = capsys.readouterr().out
    assert "Acme_*" in _matches(tmp_repo)                       # applied
    joined = [" ".join(c) for c in calls]
    assert any("git add" in j for j in joined)
    assert any("git commit" in j for j in joined)
    assert "kontroll-land main" in joined and not any("push local" in j for j in joined)
    assert "landed on the local canonical" in out
    assert not any("push origin" in j for j in joined)         # no --push


def test_commit_is_idempotent(tmp_repo, capsys, monkeypatch):
    """A second --commit with the same match is a no-op: reports 'already present', no second entry."""
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: 0)
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: 0)
    galaxy.cmd_capture_exception_add(_args(commit=True))
    capsys.readouterr()
    galaxy.cmd_capture_exception_add(_args(commit=True))        # second time
    assert "already present" in capsys.readouterr().out
    assert _matches(tmp_repo).count("Acme_*") == 1


def test_push_also_pushes_origin(tmp_repo, capsys, monkeypatch):
    """--push additionally pushes origin (the optional offsite backup) after the landing on the local canonical."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(cmd) or 0)
    monkeypatch.setattr(gitio, "land_main", lambda run_id=None, skip_gate=False: calls.append(["kontroll-land", "main"]) or 0)
    galaxy.cmd_capture_exception_add(_args(commit=True, push=True))
    capsys.readouterr()
    joined = [" ".join(c) for c in calls]
    assert "kontroll-land main" in joined and any("push origin" in j for j in joined)
    assert joined.index("kontroll-land main") < next(i for i, j in enumerate(joined) if "push origin" in j)


def test_failed_commit_does_not_push(tmp_repo, capsys, monkeypatch):
    """If `git commit` fails, the function must NOT push a stale HEAD or report success — it
    reports the failure and pushes nothing (guards the misleading-success / wrong-state-push bug)."""
    calls = []

    def fake_run(cmd, cwd=None, quiet_args=0):
        calls.append(cmd)
        return 1 if (len(cmd) > 1 and cmd[1] == "commit") else 0   # commit fails

    monkeypatch.setattr(gitio, "_run", fake_run)
    galaxy.cmd_capture_exception_add(_args(commit=True))
    assert "commit failed" in capsys.readouterr().out.lower()
    assert not any("push" in " ".join(c) for c in calls)       # never pushed after a failed commit
