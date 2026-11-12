"""tests/_checkout.py — is the tree under test a git checkout? The answer for the `requires_checkout` marker.

A row marked `requires_checkout` reads the repository through git: the index's modes, the tracked list, what is
ignored, `git archive HEAD`. In a tree that is not a checkout (a `git archive` copy, as every container check runs on,
or a source tarball an operator unpacks) git says "not a git repository" and the row used to FAIL, with a
CalledProcessError that says nothing of why (I-51). `tests/conftest.py` skips such a row there, with the reason
`checkout_reason()` gives. The answer is a checkout only when git names `root` ITSELF as the top of its work tree:
an archive unpacked inside some other repository's directory is not a checkout of this tree, though git would
answer from the outer one.

Only git's own "not a git repository" is a reason. A tree git finds and refuses (dubious ownership: another uid owns
it) is a checkout whose rows must not go quiet: it gets no reason, so they run and fail with git's own error, as they
did before the marker (the I-51 reads' S1/S4).
"""
import os
import subprocess

# A variable a parent git hands down (a suite started from a hook) would point the probe at another repository.
_GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")
NOT_A_REPO = "not a git repository"   # git's words in the C locale, which the probe asks in


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _ask(root, env, *args):
    return subprocess.run(["git", "-C", root, "rev-parse", *args], capture_output=True, text=True, env=env,
                          timeout=30)


def _said(stderr):
    """git's own cause: its first `fatal:` line (the lines after it are hints, such as a remedy), else its first."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    return next((line for line in lines if line.startswith("fatal:")), lines[0] if lines else "no reason")


def checkout_reason(root):
    """None when `root` is the top of a git work tree; otherwise the reason a row that needs one is skipped."""
    handed = sorted(k for k in _GIT_REPO_VARS if k in os.environ)
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_VARS}
    env["LC_ALL"] = "C"                # git's words, not a translation: the reason rests on them
    env.pop("LANGUAGE", None)
    try:
        r = _ask(root, env, "--show-toplevel", "--absolute-git-dir")
    except FileNotFoundError:
        return "needs a git checkout: git is not on the PATH"
    except subprocess.TimeoutExpired:
        return "needs a git checkout: `git rev-parse` did not answer in 30 s"
    if r.returncode != 0:
        said = _said(r.stderr)
        if NOT_A_REPO in said:
            return "needs a git checkout: %s is not one (%s)" % (root, said)
        return None                    # a tree git found and refuses: its rows run and fail with git's own error
    top, git_dir = (r.stdout.strip().splitlines() + ["", ""])[:2]
    if not _same(top, root):
        return "needs a git checkout of this tree: %s sits inside another one (%s)" % (root, top)
    if handed:
        # the marked rows inherit what the probe dropped: with it, git must still name this tree's repository
        inherited = dict(os.environ, LC_ALL="C")
        inherited.pop("LANGUAGE", None)
        try:
            theirs = _ask(root, inherited, "--absolute-git-dir")
        except subprocess.TimeoutExpired:
            return ("needs a git checkout of this tree: `git rev-parse` with %s did not answer in 30 s"
                    % ", ".join(handed))
        if theirs.returncode != 0 or not _same(theirs.stdout.strip(), git_dir):
            return ("needs a git checkout of this tree: %s handed down names another repository (%s)"
                    % (", ".join(handed), theirs.stdout.strip() or _said(theirs.stderr)))
    return None
