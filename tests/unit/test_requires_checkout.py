"""The `requires_checkout` marker: a row that reads this tree through git skips, with the reason, outside a checkout.

WHY (I-51) — rows that read the repository through git (the index's modes, the tracked list, what is ignored, a
`git archive HEAD` build) failed in a tree that is not a git checkout, with a CalledProcessError or a "not a git
repository" that said nothing of why. Every container check here runs on a `git archive` copy, and an operator may
unpack a source tarball, so those rows were failures to explain away on every run: 9 on Linux in the I-15 read's
whole-suite run on an archive, the launch kit's build in I-4's checks. tests/_checkout.py answers "is this tree a
checkout", and tests/conftest.py skips each marked row with that answer when it is not, or, where
KONTROLL_REQUIRE_CHECKOUT=1 (the gate's pytest step, CI's pytest job), stops the session. These rows pin the answer (a
tree that is not one, a checkout's top, one reached through a link, a copy inside another repository, a checkout git
refuses, a repository variable handed down, no git at all) and, in a copy of this tree that is not a checkout, that
the marked rows skip, an unmarked row runs, and the variable turns the skip into an error (on worker processes too).

The rows that build scratch repositories use `isolated_git`; rows 7 and 8 read THIS tree and keep the account's git
configuration (a `safe.directory` an image sets is git's own remedy for a tree another uid owns), dropping only the
variables that would point git at another repository (the I-51 reads' S2).
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

import _checkout

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git on PATH")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _no_repo_vars():
    """os.environ without the variables that point git at a repository, nor the gate's KONTROLL_REQUIRE_CHECKOUT."""
    return {k: v for k, v in os.environ.items()
            if k not in _checkout._GIT_REPO_VARS and k != "KONTROLL_REQUIRE_CHECKOUT"}


@needs_git
@pytest.mark.usefixtures("isolated_git")
def test_a_tree_that_is_not_a_checkout_has_a_reason(tmp_path, monkeypatch):
    """A plain directory (an unpacked archive) is not a checkout: the reason names it and git's own "not a git
    repository". The search stops above it (`GIT_CEILING_DIRECTORIES`), so a temporary directory inside some work
    tree (a home that is a repository) does not change the answer. Guards the probe answering "checkout" where git
    says there is none, which would let a marked row run and fail as before, and a reason that drops git's cause."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    reason = _checkout.checkout_reason(str(plain))
    assert reason and reason.startswith("needs a git checkout: %s is not one (" % plain), reason
    assert "not a git repository" in reason, reason


@needs_git
@pytest.mark.usefixtures("isolated_git")
def test_the_top_of_a_checkout_has_none(tmp_path):
    """At the top of a git work tree there is no reason, so a marked row runs. Guards a probe that skips every
    marked row everywhere, the gate and CI included, where they must run."""
    _git("init", "-q", str(tmp_path), cwd=str(tmp_path))
    assert _checkout.checkout_reason(str(tmp_path)) is None


@needs_git
@pytest.mark.usefixtures("isolated_git")
def test_a_checkout_reached_through_a_link_has_none(tmp_path):
    """A checkout named by a path through a symbolic link is still a checkout: git names the resolved top, and the
    probe compares resolved paths. Guards the comparison dropping `realpath`: a suite run by an absolute path through
    a link would read its own checkout as "inside another one" and skip every marked row, rc 0, silently."""
    real = tmp_path / "real"
    _git("init", "-q", str(real), cwd=str(tmp_path))
    link = tmp_path / "link"
    try:
        os.symlink(str(real), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot make a symbolic link here")
    assert _checkout.checkout_reason(str(link)) is None


@needs_git
@pytest.mark.usefixtures("isolated_git")
def test_a_copy_inside_another_checkout_is_not_a_checkout_of_this_tree(tmp_path):
    """An archive unpacked in a subdirectory of some other repository is not a checkout of THIS tree, though git
    answers from the outer one: the reason names the tree and the outer top. Guards a marked row then reading the
    outer repository's index (another project's files, modes and ignores) as this tree's."""
    outer = tmp_path / "outer"
    inner = outer / "unpacked" / "kontroll"
    inner.mkdir(parents=True)
    _git("init", "-q", str(outer), cwd=str(tmp_path))
    reason = _checkout.checkout_reason(str(inner))
    assert reason and str(inner) in reason and re.search(r"sits inside another one \(.*outer\)$", reason), reason


@needs_git
@pytest.mark.usefixtures("isolated_git")
def test_a_git_dir_handed_down_does_not_make_a_tree_a_checkout(tmp_path, monkeypatch):
    """A suite started from a git hook inherits `GIT_DIR` pointing at the repository the hook runs in. With it, git
    takes any directory as that repository's work tree and names it the top, so an unpacked archive would read as a
    checkout and its marked rows would run against another repository's index. The probe asks without it (and
    without its kin): the tree is still not a checkout. Guards the probe trusting the environment it was started in."""
    outer = tmp_path / "outer"
    plain = tmp_path / "plain"
    plain.mkdir()
    _git("init", "-q", str(outer), cwd=str(tmp_path))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    reason = _checkout.checkout_reason(str(plain))
    assert reason and reason.startswith("needs a git checkout: ") and "not a git repository" in reason, reason


@needs_git
@pytest.mark.usefixtures("isolated_git")
def test_a_git_dir_handed_down_naming_another_repository_is_a_reason(tmp_path, monkeypatch):
    """On a real checkout, a `GIT_DIR` handed down that names ANOTHER repository is a reason: the marked rows inherit
    it, and with it git reads that repository, not this tree's. One naming this checkout's own git directory is not.
    Guards the marked rows running against another repository's index (the I-51 reads' A N2: three failed, and the
    rest passed over its one file), and a probe that skips wherever a variable is handed down at all."""
    mine, other = tmp_path / "mine", tmp_path / "other"
    _git("init", "-q", str(mine), cwd=str(tmp_path))
    _git("init", "-q", str(other), cwd=str(tmp_path))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    reason = _checkout.checkout_reason(str(mine))
    assert reason and re.search(r"^needs a git checkout of this tree: GIT_DIR handed down names another repository "
                                r"\(.*other[\\/]\.git\)$", reason), reason
    monkeypatch.setenv("GIT_DIR", str(mine / ".git"))
    assert _checkout.checkout_reason(str(mine)) is None


def test_only_gits_not_a_repository_is_a_reason(monkeypatch):
    """git's answer decides: "not a git repository" is a reason, named by git's `fatal:` line (not a hint after it);
    any other refusal of the tree (dubious ownership, whose last lines are git's remedy) is NOT, so the marked rows run
    and fail with git's own error. Guards the I-51 reads' S1/S4: a checkout git refuses read as "not one", all its
    marked rows skipped, rc 0, where before they went red, with the remedy given as the reason. (git's answers are
    stood in for here, so this row needs no git and no other uid; the next row refuses a real one.)"""
    def answer(stderr):
        monkeypatch.setattr(_checkout, "_ask", lambda root, env, *args: subprocess.CompletedProcess(
            ["git"], 128, "", stderr))
    answer("fatal: detected dubious ownership in repository at '/srv/co'\nTo add an exception for this directory, "
           "call:\n\n\tgit config --global --add safe.directory /srv/co\n")
    assert _checkout.checkout_reason("/srv/co") is None
    answer("warning: unable to access '/x/.config/git/attributes': Permission denied\n"
           "fatal: not a git repository (or any of the parent directories): .git\nhint: a later line\n")
    assert _checkout.checkout_reason("/srv/co") == (
        "needs a git checkout: /srv/co is not one (fatal: not a git repository (or any of the parent directories): "
        ".git)")


@needs_git
@pytest.mark.skipif(os.name == "nt" or os.geteuid() != 0, reason="makes git refuse a checkout by giving it to "
                    "another uid, which needs root (the container checks run as root)")
@pytest.mark.usefixtures("isolated_git")
def test_a_checkout_git_refuses_is_not_a_reason(tmp_path):
    """A real checkout owned by another uid, which git refuses (dubious ownership; `isolated_git` keeps any
    `safe.directory` away), has no reason: its marked rows run and fail with git's own error, as they did before the
    marker. Guards the probe turning a refused checkout's red rows into skipped ones, which validate's pytest step
    and CI's job, which do not fail on a skip, would pass."""
    co = tmp_path / "co"
    _git("init", "-q", str(co), cwd=str(tmp_path))
    for top, dirs, files in os.walk(str(co)):
        for name in [top] + [os.path.join(top, n) for n in dirs + files]:
            os.lchown(name, 12345, 12345)
    refused = subprocess.run(["git", "-C", str(co), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if refused.returncode == 0:
        pytest.skip("this git does not refuse a checkout another uid owns")
    assert _checkout.checkout_reason(str(co)) is None


def test_no_git_on_the_path_is_a_reason(tmp_path, monkeypatch):
    """With no git to ask, the answer is a reason (git is not on the PATH), not an exception that stops the session.
    Guards a minimal image without git turning every marked row into a collection error."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _checkout.checkout_reason(str(tmp_path)) == "needs a git checkout: git is not on the PATH"


def test_the_marker_is_registered(request):
    """`requires_checkout` is a registered marker (pyproject.toml), so under the suite's strict markers (the
    `strict_markers` ini key from pytest 9.0, the addopts flag before; 9.0.0-9.0.3 ignore the flag from addopts, pytest
    #14442, I-71) a misspelt one is an error; this row holds the registration on any version. Guards the registration
    being dropped: under strict markers every marked file is then a collection error, and without them the mark is a
    warning nobody reads."""
    assert any(line.split(":", 1)[0].strip() == "requires_checkout" for line in request.config.getini("markers"))


def test_the_hook_skips_the_marked_rows_exactly_when_this_tree_is_not_a_checkout(request):
    """In this session, every collected row that carries the marker has the hook's skip with this tree's own reason
    when the tree is not a checkout, none has it when the tree is one, and no UNMARKED row has it either way. Guards a
    hook that probes the wrong directory or skips regardless: in the gate and in CI it would skip every marked row,
    this file's own end-to-end row with them, and nothing would say so."""
    reason = _checkout.checkout_reason(ROOT)
    items = request.session.items

    def skipped(it):
        return any(m.name == "skip" and "needs a git checkout" in str(m.kwargs.get("reason", ""))
                   for m in it.iter_markers())
    assert not [it.nodeid for it in items if not it.get_closest_marker("requires_checkout") and skipped(it)]
    marked = [it for it in items if it.get_closest_marker("requires_checkout")]
    if not marked:
        pytest.skip("no row carrying the marker was collected in this session")
    if reason is None:
        assert not [it.nodeid for it in marked if skipped(it)]
    else:
        assert all(any(m.name == "skip" and m.kwargs.get("reason") == reason for m in it.iter_markers())
                   for it in marked), reason


def test_the_gate_and_ci_run_the_suite_requiring_a_checkout():
    """tests/validate.sh's pytest step and CI's offline pytest job run with KONTROLL_REQUIRE_CHECKOUT=1. Both run in
    a checkout, where a marked row must run: a skip there passes them (neither fails on a skip, and validate's step
    prints no reasons), so the variable makes a would-be skip an error. Guards the variable being dropped from
    either, after which a gate whose probe went wrong (a refused tree, a handed-down GIT_DIR) passes green without
    having run the rows that read git (the I-51 reads' A N3 / B N8)."""
    with open(os.path.join(ROOT, "tests", "validate.sh"), encoding="utf-8") as fh:
        validate = fh.read()
    assert re.search(r'^\s*step pytest 0 env KONTROLL_REQUIRE_CHECKOUT=1 "\$PY" -m pytest -m "not e2e and not slow"',
                     validate, re.M), "tests/validate.sh's pytest step"
    with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as fh:
        jobs = yaml.safe_load(fh)["jobs"]
    runs = [s for job in jobs.values() for s in job.get("steps", [])
            if str(s.get("run", "")).startswith("pytest -m \"not e2e and not slow\"")]
    assert runs and all((s.get("env") or {}).get("KONTROLL_REQUIRE_CHECKOUT") == "1" for s in runs), runs


def _marked_files():
    """The test files that carry the marker, read as text: each names `requires_checkout` in a mark. Only
    tests/unit and tests/integration are scanned, where the git-reading rows live."""
    found = []
    for base in ("tests/unit", "tests/integration"):
        for name in sorted(os.listdir(os.path.join(ROOT, base))):
            if name.startswith("test_") and name.endswith(".py") and name != "test_requires_checkout.py":
                with open(os.path.join(ROOT, base, name), encoding="utf-8") as fh:
                    if re.search(r"mark\.requires_checkout\b", fh.read()):
                        found.append(base + "/" + name)
    return found


PROBE = '''"""Written by test_requires_checkout.py into a copy that is not a checkout: one marked row, one unmarked."""
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.requires_checkout
def test_marked():
    """Marked: outside a checkout the hook skips it, so it never gets to fail."""
    raise AssertionError("a row marked requires_checkout ran outside a checkout")


def test_unmarked():
    """Unmarked: the hook leaves it alone."""
'''


@pytest.mark.requires_checkout
def test_the_marked_rows_skip_with_the_reason_in_a_copy_that_is_not_a_checkout(tmp_path):
    """Copy this tree's files (tracked, and untracked but not ignored, as they are on disk, so an uncommitted change
    is in the copy) to a directory that is not a checkout, and run there, from a working directory that IS a checkout
    (of something else): every file carrying the marker, whose marked rows are each SKIPPED with git's "not a git
    repository", none failing or erroring; and a written file of one marked row (which fails if it runs) and one
    unmarked row: 1 passed, 1 skipped; and that file again with KONTROLL_REQUIRE_CHECKOUT=1, which is a usage error
    naming the reason, also on two worker processes where pytest-xdist is installed (as in CI, which runs the suite
    so). Guards the conftest hook (or the marker's registration, which --strict-markers would turn into an error) not
    reaching the rows it names, a hook that skips UNMARKED rows too (an archive run reading "110 skipped", rc 0), one
    that probes the working directory instead of the tree, the gate's variable not stopping a session that would skip
    them, and, under workers, the refusal raised only in a worker, where xdist reports a crashed worker and an
    INTERNALERROR (exit 3) instead of the reason. Needs a checkout itself, to list the files."""
    files = _marked_files()
    assert files, "no test file carries the requires_checkout marker"
    env = _no_repo_vars()
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, check=True, env=env).stdout
    copy = tmp_path / "copy"
    for rel in tracked.decode("utf-8").split("\0"):
        src = os.path.join(ROOT, rel)
        if rel and os.path.isfile(src):
            dst = copy / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    probe = copy / "tests" / "unit" / "test_written_by_requires_checkout.py"
    probe.write_text(PROBE, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "init", "-q", str(elsewhere)], capture_output=True, check=True, env=env)
    # the copy is no checkout wherever the temporary directory sits; the long skip lines are not cut
    env = dict(env, GIT_CEILING_DIRECTORIES=str(tmp_path), COLUMNS="400")
    # `-o addopts=` drops the suite's addopts, `--strict-markers` with them, so it is given on the command line
    # (pytest 9.0.0-9.0.3 would ignore it through any addopts, #14442)
    base = [sys.executable, "-B", "-m", "pytest", "-q", "-o", "addopts=", "--strict-markers", "-p", "no:cacheprovider",
            "-rs"]

    def run(args, **extra):
        return subprocess.run(base + args, cwd=str(elsewhere), capture_output=True, text=True, timeout=600,
                              env=dict(env, **extra))
    r = run(["-m", "requires_checkout"] + [str(copy / f) for f in files])
    out = r.stdout + r.stderr
    last = (r.stdout.strip().splitlines() or [""])[-1]
    assert r.returncode in (0, 5), out[-3000:]
    assert " skipped" in last and "failed" not in last and "error" not in last, last
    assert "needs a git checkout" in out and "not a git repository" in out, out[-3000:]
    assert not re.search(r"\b\d+ passed\b", last), "a marked row ran outside a checkout: " + last

    r = run([str(probe)])
    last = (r.stdout.strip().splitlines() or [""])[-1]
    assert r.returncode == 0 and re.search(r"^1 passed, 1 skipped\b", last), r.stdout[-3000:] + r.stderr[-2000:]
    assert "not a git repository" in r.stdout, r.stdout[-3000:]

    r = run([str(probe)], KONTROLL_REQUIRE_CHECKOUT="1")
    assert r.returncode == pytest.ExitCode.USAGE_ERROR, r.stdout[-3000:] + r.stderr[-2000:]
    assert "KONTROLL_REQUIRE_CHECKOUT=1" in r.stderr and "not a git repository" in r.stderr, r.stderr[-3000:]

    if importlib.util.find_spec("xdist") is not None:
        r = run([str(probe), "-n", "2"], KONTROLL_REQUIRE_CHECKOUT="1")
        assert r.returncode == pytest.ExitCode.USAGE_ERROR, r.stdout[-3000:] + r.stderr[-2000:]
        assert "KONTROLL_REQUIRE_CHECKOUT=1" in r.stderr and "not a git repository" in r.stderr, r.stderr[-3000:]
        assert "INTERNALERROR" not in r.stdout + r.stderr, r.stdout[-3000:] + r.stderr[-2000:]
