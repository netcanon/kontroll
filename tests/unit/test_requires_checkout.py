"""The `requires_checkout` marker: a row that reads this tree through git skips, with the reason, outside a checkout.

WHY (I-51) — rows that read the repository through git (the index's modes, the tracked list, what is ignored, a
`git archive HEAD` build) failed in a tree that is not a git checkout, with a CalledProcessError or a "not a git
repository" that said nothing of why. Every container check here runs on a `git archive` copy, and an operator may
unpack a source tarball, so those rows were failures to explain away on every run: 9 on Linux in the I-15 read's
whole-suite run on an archive, the launch kit's build in I-4's checks. tests/_checkout.py answers "is this tree a
checkout", and tests/conftest.py skips each marked row with that answer when it is not. These rows pin the answer
(a tree that is not one, a checkout's top, a copy inside another repository, no git at all) and that the marked
rows skip, not fail, in a copy of this tree that is not a checkout.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

import _checkout

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_a_tree_that_is_not_a_checkout_has_a_reason(tmp_path):
    """A plain directory (an unpacked archive) is not a checkout: the reason names it and git's own answer. Guards
    the probe answering "checkout" where git refuses, which would let a marked row run and fail as before."""
    if _checkout.checkout_reason(str(tmp_path)) is None:
        pytest.skip("the temporary directory sits inside a git work tree here")
    reason = _checkout.checkout_reason(str(tmp_path))
    assert reason.startswith("needs a git checkout: ") and str(tmp_path) in reason, reason


def test_the_top_of_a_checkout_has_none(tmp_path):
    """At the top of a git work tree there is no reason, so a marked row runs. Guards a probe that skips every
    marked row everywhere, the gate and CI included, where they must run."""
    _git("init", "-q", str(tmp_path), cwd=str(tmp_path))
    assert _checkout.checkout_reason(str(tmp_path)) is None


def test_a_copy_inside_another_checkout_is_not_a_checkout_of_this_tree(tmp_path):
    """An archive unpacked in a subdirectory of some other repository is not a checkout of THIS tree, though git
    answers from the outer one: the reason names both. Guards a marked row then reading the outer repository's index
    (another project's files, modes and ignores) as this tree's."""
    outer = tmp_path / "outer"
    inner = outer / "unpacked" / "kontroll"
    inner.mkdir(parents=True)
    _git("init", "-q", str(outer), cwd=str(tmp_path))
    reason = _checkout.checkout_reason(str(inner))
    assert reason and "sits inside another one" in reason, reason


def test_a_git_dir_handed_down_does_not_make_a_tree_a_checkout(tmp_path, monkeypatch):
    """A suite started from a git hook inherits `GIT_DIR` (and kin) pointing at the repository the hook runs in. With
    it, git takes any directory as that repository's work tree and names it the top, so an unpacked archive would
    read as a checkout and its marked rows would run against another repository's index. The probe drops those
    variables: the tree is still not a checkout. Guards the probe trusting the environment it was started in."""
    outer = tmp_path / "outer"
    plain = tmp_path / "plain"
    plain.mkdir()
    _git("init", "-q", str(outer), cwd=str(tmp_path))
    if _checkout.checkout_reason(str(plain)) is None:
        pytest.skip("the temporary directory sits inside a git work tree here")
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    reason = _checkout.checkout_reason(str(plain))
    assert reason and reason.startswith("needs a git checkout: "), reason


def test_no_git_on_the_path_is_a_reason(tmp_path, monkeypatch):
    """With no git to ask, the answer is a reason (git is not on the PATH), not an exception that stops the session.
    Guards a minimal image without git turning every marked row into a collection error."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _checkout.checkout_reason(str(tmp_path)) == "needs a git checkout: git is not on the PATH"


def test_the_marker_is_registered(request):
    """`requires_checkout` is a registered marker (pyproject.toml), so `--strict-markers` (the suite's addopts)
    accepts it and a misspelt one is an error, not a mark that silently skips nothing. Guards the registration being
    dropped, which turns every marked file into a collection error under the suite's own options."""
    assert any(line.split(":", 1)[0].strip() == "requires_checkout" for line in request.config.getini("markers"))


def test_the_hook_skips_the_marked_rows_exactly_when_this_tree_is_not_a_checkout(request):
    """In this session, every collected row that carries the marker has the hook's skip with this tree's own reason
    when the tree is not a checkout, and none has it when the tree is one. Guards a hook that probes the wrong
    directory or skips regardless: in the gate and in CI it would skip every marked row, this file's own end-to-end
    row with them, and nothing would say so."""
    reason = _checkout.checkout_reason(ROOT)
    marked = [it for it in request.session.items if it.get_closest_marker("requires_checkout")]
    if not marked:
        pytest.skip("no row carrying the marker was collected in this session")
    skipped = [it.nodeid for it in marked
               if any(m.name == "skip" and m.kwargs.get("reason") == reason for m in it.iter_markers())]
    if reason is None:
        assert not [it.nodeid for it in marked if any(m.name == "skip" and "needs a git checkout" in
                                                      str(m.kwargs.get("reason", "")) for m in it.iter_markers())]
    else:
        assert len(skipped) == len(marked), (reason, len(skipped), len(marked))


def _marked_files():
    """The test files that carry the marker, read as text: each names `requires_checkout` in a mark."""
    found = []
    for base in ("tests/unit", "tests/integration"):
        for name in sorted(os.listdir(os.path.join(ROOT, base))):
            if name.startswith("test_") and name.endswith(".py") and name != "test_requires_checkout.py":
                with open(os.path.join(ROOT, base, name), encoding="utf-8") as fh:
                    if re.search(r"mark\.requires_checkout\b", fh.read()):
                        found.append(base + "/" + name)
    return found


@pytest.mark.requires_checkout
def test_the_marked_rows_skip_with_the_reason_in_a_copy_that_is_not_a_checkout(tmp_path):
    """Copy this tree's files (tracked, and untracked but not ignored, as they are on disk, so an uncommitted change
    is in the copy) to a directory that is not a checkout, and run every file carrying the marker there: each marked
    row is SKIPPED with "needs a git checkout", and none fails or errors. Guards the conftest hook (or the marker's
    registration, which --strict-markers would turn into an error) not reaching the rows it names. Needs a checkout
    itself, to list the files."""
    files = _marked_files()
    assert files, "no test file carries the requires_checkout marker"
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, check=True).stdout
    copy = tmp_path / "copy"
    for rel in tracked.decode("utf-8").split("\0"):
        src = os.path.join(ROOT, rel)
        if rel and os.path.isfile(src):
            dst = copy / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if _checkout.checkout_reason(str(copy)) is None:
        pytest.skip("the temporary directory sits inside a git work tree here")
    # `--strict-markers` on the command line: given through `-o addopts=` it does not take (pytest 9).
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-o", "addopts=", "--strict-markers",
                        "-p", "no:cacheprovider", "-m", "requires_checkout", "-rs", *files],
                       cwd=str(copy), capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    last = (r.stdout.strip().splitlines() or [""])[-1]
    assert r.returncode in (0, 5), out[-3000:]
    assert " skipped" in last and "failed" not in last and "error" not in last, last
    assert "needs a git checkout" in out, out[-3000:]
    assert not re.search(r"\b\d+ passed\b", last), "a marked row ran outside a checkout: " + last
