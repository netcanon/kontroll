"""tests/_checkout.py — is the tree under test a git checkout? The answer for the `requires_checkout` marker.

A row marked `requires_checkout` reads the repository through git: the index's modes, the tracked list, what is
ignored, `git archive HEAD`. In a tree that is not a checkout (a `git archive` copy, as every container check runs on,
or a source tarball an operator unpacks) git says "not a git repository" and the row used to FAIL, with a
CalledProcessError that says nothing of why (I-51). `tests/conftest.py` skips such a row there, with the reason
`checkout_reason()` gives. The answer is a checkout only when git names `root` ITSELF as the top of its work tree:
an archive unpacked inside some other repository's directory is not a checkout of this tree, though git would
answer from the outer one.
"""
import os
import subprocess

# A variable a parent git hands down (a suite started from a hook) would point the probe at another repository.
_GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def checkout_reason(root):
    """None when `root` is the top of a git work tree; otherwise the reason a row that needs one is skipped."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_VARS}
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           env=env, timeout=30)
    except FileNotFoundError:
        return "needs a git checkout: git is not on the PATH"
    except subprocess.TimeoutExpired:
        return "needs a git checkout: `git rev-parse` did not answer in 30 s"
    if r.returncode != 0:
        return "needs a git checkout: %s is not one (%s)" % (root, (r.stderr.strip().splitlines() or ["no reason"])[-1])
    top = r.stdout.strip()
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(root)):
        return "needs a git checkout of this tree: %s sits inside another one (%s)" % (root, top)
    return None
