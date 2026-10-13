"""promote_ref is a compare-and-swap, not a check-then-set (2026-10-08 review, finding 9).

WHY: the trusted promote ran `merge-base --is-ancestor main proposed/<id>` and then `update-ref refs/heads/main
proposed/<id>` with no old-value operand. Between the two commands another promote (the Semaphore approval task and a
CLI run, or two CLI runs) can advance `main`; the second `update-ref` then overwrites it with a ref that is no longer
a fast-forward of the `main` it replaces — the first promote is LOST, silently, while both report success. `git
update-ref <ref> <new> <old>` refuses when the ref is not at <old> ("is at X but expected Y"); that is the atomic
form. These tests run REAL git in a temporary bare repository: a mocked `_run` cannot prove the operand order git
actually accepts, and the race is injected at the only place it can happen — between the check and the write.
"""
import os
import subprocess

import pytest

from kontroll import gitio

pytestmark = pytest.mark.unit

_GIT_ID = ["-c", "user.name=kontroll-test", "-c", "user.email=test@example.invalid"]


def _git(cwd, *args, capture=False):
    p = subprocess.run(["git", *_GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, "git %s failed: %s" % (" ".join(args), p.stderr)
    return p.stdout.strip() if capture else None


@pytest.fixture
def canonical(tmp_path):
    """A bare canonical with `main` at commit A and two proposals that are BOTH fast-forwards of it:
    proposed/0000000000a1 = A+B, proposed/0000000000a2 = A+C. Yields (bare, {'A','B','C'} object ids)."""
    bare = tmp_path / "canonical.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    (work / "a").write_text("a\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "A")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    oids = {"A": _git(work, "rev-parse", "HEAD", capture=True)}
    for name, leaf in (("0000000000a1", "B"), ("0000000000a2", "C")):
        _git(work, "checkout", "-q", "-B", "p-" + name, oids["A"])
        (work / leaf.lower()).write_text(leaf + "\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", leaf)
        oids[leaf] = _git(work, "rev-parse", "HEAD", capture=True)
        _git(work, "push", "-q", "origin", "HEAD:refs/heads/proposed/" + name)
    return bare, oids


def _ref(bare, ref):
    p = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=str(bare), capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def test_a_clean_promote_fast_forwards_main_with_real_git(canonical):
    """The happy path against real git: the compare-and-swap operands are in the order `update-ref` accepts, main
    lands on the proposal's tip and the promoted proposal ref is deleted. A mocked `_run` could not tell a wrong
    operand order from a right one — git can."""
    bare, oids = canonical
    assert gitio.promote_ref("0000000000a1", cwd=str(bare)) is True
    assert _ref(bare, "refs/heads/main") == oids["B"]
    assert _ref(bare, "refs/heads/proposed/0000000000a1") is None, "the promoted proposal ref is cleaned up"
    assert _ref(bare, "refs/heads/proposed/0000000000a2") == oids["C"], "the other proposal is untouched"


def test_a_promote_that_loses_the_race_is_refused_and_the_winner_is_kept(canonical, monkeypatch):
    """THE RACE. Another promote advances `main` to proposed/0000000000a2 between the fast-forward check of
    proposed/0000000000a1 and its `update-ref`. Check-then-set would overwrite main with 0000000000a1 (losing
    the promote of 0000000000a2 while reporting success);
    compare-and-swap is refused by git because main is no longer at the id the check ran against. main must stay
    at the winner, 0000000000a1 must be refused (False) and its proposal ref must survive for a re-check."""
    bare, oids = canonical
    real_run = gitio._run

    def racing(cmd, cwd=None, quiet_args=0):
        rc = real_run(cmd, cwd=cwd, quiet_args=quiet_args)
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            # the OTHER promote lands right after our check and before our write
            _git(bare, "update-ref", "refs/heads/main", oids["C"], oids["A"])
        return rc

    monkeypatch.setattr(gitio, "_run", racing)
    assert gitio.promote_ref("0000000000a1", cwd=str(bare)) is False, "the loser must be refused, never overwrite the winner"
    assert _ref(bare, "refs/heads/main") == oids["C"], "main keeps the promote that landed first"
    assert _ref(bare, "refs/heads/proposed/0000000000a1") == oids["B"], "the refused proposal survives for a re-check"


def test_a_missing_main_is_refused_rather_than_promoted_blind(tmp_path):
    """A canonical with no `main` yet (nothing was ever mirrored) has nothing to fast-forward; the old code would
    have let `merge-base` fail and reported a refusal for the wrong reason. The read of main's id is the first
    gate and names the real cause."""
    bare = tmp_path / "empty.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    assert gitio.promote_ref("0000000000a1", cwd=str(bare)) is False


def test_the_write_never_runs_without_an_old_value_operand(monkeypatch):
    """The mocked seam pins the SHAPE: every `update-ref refs/heads/main` the promote issues carries exactly five
    argv elements — the proposal ref AND main's old object id. Guards a refactor dropping the operand (which git
    would happily accept as a plain overwrite)."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "_out", lambda cmd, cwd=None: (0, "f" * 40))
    assert gitio.promote_ref("000000000001") is True
    writes = [c for c in calls if c[:2] == ["git", "update-ref"] and c[2] == "refs/heads/main"]
    assert writes == [["git", "update-ref", "refs/heads/main", "refs/heads/proposed/000000000001", "f" * 40]]
