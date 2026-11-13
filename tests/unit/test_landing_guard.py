"""The suite's landing guard (tests/_landing_guard.py, I-70): a row whose code reaches the landing from this checkout
fails, naming the mock it lacks, on every checkout; a scratch worktree passes through to the real landing.

Why: seven rows mocked `gitio._run` but not `gitio.land_main`, so a non-staging `commit_and_push` ran the real
landing from the write root, this checkout. They passed where the checkout has no `local` remote (CI, a clone) and,
where it has one (a control node's checkout), landed the developer's HEAD on that canonical's `main` and passed
too. These rows hold the guard that makes the omission fail everywhere instead.
"""
import os
import subprocess

import pytest

import _landing_guard
from kontroll import gitio, paths

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _NoProcess:
    """Stands in for `subprocess` inside gitio: records and refuses every process start, passes the rest through."""

    STARTS = ("run", "Popen", "call", "check_call", "check_output")

    def __init__(self):
        self.started = []

    def __getattr__(self, name):
        real = getattr(subprocess, name)
        if name not in self.STARTS:
            return real

        def refuse(*args, **kwargs):
            self.started.append((name, args))
            raise AssertionError("a process was started: %s %r" % (name, args))
        return refuse


def test_the_guard_is_installed_for_the_whole_suite():
    """tests/conftest.py installs the guard at import time, so every row runs with it: `gitio._canonical_of` and
    `gitio.land_commit` are the guard's wrappers. Guards against the install line being dropped, which would let a
    row that forgets to mock `land_main` land this checkout's HEAD again, silently (I-70)."""
    assert gitio._canonical_of is _landing_guard._canonical_of
    assert gitio.land_commit is _landing_guard._land_commit


@pytest.mark.parametrize("sub", ["", "scripts", os.path.join("tests", "unit")], ids=["root", "scripts", "deeper"])
@pytest.mark.parametrize("door", ["_canonical_of", "land_commit"])
def test_asked_to_land_from_this_checkout_the_row_fails_before_any_process_starts(monkeypatch, door, sub):
    """Each of the landing's two worktree functions, asked about this checkout or a directory inside it (git run
    there answers for this repository), fails the row with the I-70 message, and starts no process, so not even the
    `git remote get-url local` lookup runs. Guards the refusal itself, and its reach below the checkout's top."""
    stub = _NoProcess()
    monkeypatch.setattr(gitio, "subprocess", stub)
    worktree = os.path.join(ROOT, sub) if sub else ROOT
    args = (worktree,) if door == "_canonical_of" else (worktree, os.path.join(ROOT, "no-such-canonical.git"))
    with pytest.raises(pytest.fail.Exception, match=r"gitio\.%s was asked to land from this checkout.*I-70" % door):
        getattr(gitio, door)(*args)
    assert stub.started == []


def test_a_row_that_mocks_run_but_not_the_landing_fails_on_any_checkout(monkeypatch):
    """The I-70 shape end to end: `_run` mocked, `land_main` not, a non-staging `commit_and_push` from this
    checkout. The row fails at the landing's lookup, whether or not this checkout has a `local` remote (the shape
    passed silently on CI and landed on a control node's canonical). `pytest.fail` is not an `Exception`, so no
    `except Exception` on the way swallows it. Guards the guard's place on the path the seven rows took."""
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    monkeypatch.setattr(paths, "ROOT", ROOT)
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: 0)
    with pytest.raises(pytest.fail.Exception, match=r"gitio\._canonical_of was asked to land from this checkout"):
        gitio.commit_and_push(["x.yml"], ["m"])


@pytest.mark.usefixtures("isolated_git")
def test_a_worktree_outside_this_checkout_reaches_the_real_landing(tmp_path, monkeypatch):
    """A scratch worktree passes through untouched: `_canonical_of` on a scratch repository whose `local` remote
    names a scratch bare repository returns that directory (the real lookup ran), and a sibling of the checkout
    whose name begins with the checkout's own is not inside it. Guards the landing tests (tests/unit/test_land.py),
    which land between scratch repositories and must keep doing so."""
    wt, bare = tmp_path / "wt", tmp_path / "canonical.git"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(wt), "remote", "add", "local", str(bare)], check=True)
    assert gitio._canonical_of(str(wt)) == os.path.abspath(str(bare))
    assert not _landing_guard.in_this_checkout(ROOT.rstrip(os.sep) + "-sibling")
    assert _landing_guard.in_this_checkout(ROOT)
