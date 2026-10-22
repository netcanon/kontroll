"""install-prereqs.sh, run without the scripts/lib/ beside it, says what is missing instead of dying on a bare error.

WHY (the failure these guard): on a scratch VM (2026-10-20) the script was piped alone (`cat … | ssh host sudo bash`)
and died with `line 35: /tmp/lib/pkg.sh: No such file or directory`. Nothing said that it sources helpers from
scripts/lib/ and has to run from a checkout or the launch kit. It now checks its libraries before anything else,
the root check included, so these tests run it as an ordinary user; whichever way it fails, it installs nothing.
"""
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREREQS = os.path.join(ROOT, "scripts", "install-prereqs.sh")
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _assert_names_the_lib_and_where_to_run(rc, stderr):
    assert rc == 1, "expected exit 1, got %r: %s" % (rc, stderr)
    assert "lib/pkg.sh not found" in stderr, stderr
    assert "checkout or the unpacked launch kit" in stderr, stderr


@needs_bash
def test_copied_alone_it_names_the_missing_lib_and_where_to_run_it(tmp_path):
    """Copied into an empty directory and run, the script exits 1 naming the missing lib/pkg.sh and saying to run
    it from a checkout or the launch kit. Why: the bare "No such file" left the operator to read the script to
    learn that it needs scripts/lib/; before the fix a non-root run said only "needs root"."""
    alone = tmp_path / "install-prereqs.sh"
    shutil.copyfile(PREREQS, alone)
    r = subprocess.run([BASH, alone.as_posix()], capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    _assert_names_the_lib_and_where_to_run(r.returncode, r.stderr)


@needs_bash
def test_piped_to_bash_it_names_the_missing_lib_and_where_to_run_it(tmp_path):
    """Piped to bash from a directory with no lib/ (`$0` is then `bash`, so it looks beside the working
    directory), it gives the same message. Why: piping over ssh is how the scratch run met the bare error."""
    with open(PREREQS, "rb") as fh:
        src = fh.read()
    r = subprocess.run([BASH], input=src, capture_output=True, timeout=60, cwd=str(tmp_path))
    _assert_names_the_lib_and_where_to_run(r.returncode, r.stderr.decode("utf-8", "replace"))


@needs_bash
def test_with_only_one_lib_beside_it_it_names_the_other(tmp_path):
    """With lib/pkg.sh beside it but not lib/canonical-group.sh, the script exits 1 naming canonical-group.sh. Why:
    it sources both, the second only after it has installed packages, so a check of the first alone would let a run
    install half the prerequisites and then die on the bare error."""
    alone = tmp_path / "install-prereqs.sh"
    shutil.copyfile(PREREQS, alone)
    (tmp_path / "lib").mkdir()
    shutil.copyfile(os.path.join(ROOT, "scripts", "lib", "pkg.sh"), tmp_path / "lib" / "pkg.sh")
    r = subprocess.run([BASH, alone.as_posix()], capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert r.returncode == 1, "expected exit 1, got %r: %s" % (r.returncode, r.stderr)
    assert "lib/canonical-group.sh not found" in r.stderr, r.stderr
