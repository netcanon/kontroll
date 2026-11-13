"""tests/_landing_guard.py — no row lands this checkout's HEAD on the canonical its `local` remote names (I-70).

WHY: `gitio.land_main` (what a non-staging `commit_and_push`, galaxy.py's two commit sites and the operator CLI
call) lands the write root's HEAD on the canonical the write root's `local` remote names, judged by the promote
gate. A row that mocks `gitio._run` but not `land_main` runs that landing for real, from the write root, which is
this checkout unless the row moved it. Where the checkout has no `local` remote (a clone, a CI runner, an archive)
the landing finds no canonical, says so, and the row passes. Where it has one (a control node's checkout:
local-canonical.yml adds it) the row lands the developer's HEAD on that canonical's `main`, and still passes:
measured with seven rows, which landed a commit the developer had not landed (I-70).

HOW: `install()`, called by tests/conftest.py at import time beside `_hermetic.install()`, wraps the landing's two
functions that take a worktree, `gitio._canonical_of` (land_main's lookup of the `local` remote) and
`gitio.land_commit`, so that asked about this checkout, or a directory inside it, they fail the row before any git
runs, naming the remedy. A worktree outside the checkout (the landing tests' scratch repositories) passes through
to the real function. The row fails on EVERY checkout, not only on one with a `local` remote, so a row that forgets
the mock fails on CI. `pytest.fail` raises an exception that an `except Exception` in the code under test does not
catch.

There is no production switch for this: it exists only in the test process. A subprocess a row starts (a script
run by its own interpreter) is not covered; those rows give the script a scratch tree or `--repo`.
"""
import os

import pytest

from kontroll import gitio

REAL_ROOT = os.path.normcase(os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_real_canonical_of = gitio._canonical_of
_real_land_commit = gitio.land_commit


def in_this_checkout(worktree):
    """True when `worktree` is this checkout or a directory inside it (git run there answers for this repository)."""
    w = os.path.normcase(os.path.realpath(worktree))
    return w == REAL_ROOT or w.startswith(REAL_ROOT + os.sep)


def _refuse(name, worktree):
    pytest.fail("%s was asked to land from this checkout (%s): a row that mocks gitio._run mocks gitio.land_main "
                "too, or moves the write root to a scratch tree; otherwise it lands this checkout's HEAD on the "
                "canonical its `local` remote names (I-70)" % (name, worktree))


def _canonical_of(worktree):
    if in_this_checkout(worktree):
        _refuse("gitio._canonical_of", worktree)
    return _real_canonical_of(worktree)


def _land_commit(worktree, *args, **kwargs):
    if in_this_checkout(worktree):
        _refuse("gitio.land_commit", worktree)
    return _real_land_commit(worktree, *args, **kwargs)


def install():
    """Wrap the landing's two worktree functions for this test process. Idempotent."""
    gitio._canonical_of = _canonical_of
    gitio.land_commit = _land_commit
