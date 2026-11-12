"""Tracked executable bits: a file that starts with a shebang is tracked 100755, and nothing else is.

WHY — the public-repository cut (2026-10-08) was produced by `git archive | tar -x` on a Windows host, which
dropped the executable bit on every entry point: `./bootstrap.sh` (docs/SETUP.md's first command), `scripts/kontroll.sh`
(the CLI), the installer entrypoint and the bundle/launch-kit scripts all landed as 100644, so a Linux clone of the
public tree failed its own quick-start with "Permission denied" while every CI gate stayed green (CI invokes them via
`bash`/`python3`). The private tree was inconsistent too — 11 of 48 shebang'd files were executable. The rule this pins
is the POSIX one and needs no list to maintain: outside docs/ (prose + dossier data), a tracked file is mode 100755
IFF its first two bytes are `#!`. Reads the INDEX mode (`git ls-files -s`), which is what a commit carries and is
authoritative on a Windows checkout with core.filemode=false.
"""
import os
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKIP_PREFIXES = ("docs/",)


def _tracked_modes():
    """{path: mode} for every tracked file, from the git index (the committed mode, not the filesystem's)."""
    out = subprocess.run(["git", "ls-files", "-s"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    modes = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        modes[path] = meta.split()[0]
    return modes


def _has_shebang(rel):
    with open(os.path.join(ROOT, rel), "rb") as fh:
        return fh.read(2) == b"#!"


@pytest.mark.requires_checkout  # reads the tree through git: skipped outside a checkout (I-51)
def test_shebang_files_are_executable_and_only_those():
    """Every tracked file (outside docs/) with a `#!` first line is mode 100755, and every 100755 file has one.
    Guards both the cut dropping the bit (a Linux clone cannot run ./bootstrap.sh) and a stray executable bit on a
    data file. Symlinks (120000) and submodules are not files and are ignored."""
    modes = _tracked_modes()
    assert modes, "git ls-files returned nothing — the test must run inside the repository"
    not_executable, stray_executable = [], []
    for rel, mode in sorted(modes.items()):
        if rel.startswith(_SKIP_PREFIXES) or mode not in ("100644", "100755"):
            continue
        if not os.path.isfile(os.path.join(ROOT, rel)):
            continue  # sparse/partial checkout: judge only what is on disk
        shebang = _has_shebang(rel)
        if shebang and mode != "100755":
            not_executable.append(rel)
        elif mode == "100755" and not shebang:
            stray_executable.append(rel)
    assert not not_executable, (
        "shebang'd files tracked without the executable bit (git update-index --chmod=+x <file>): %s" % not_executable)
    assert not stray_executable, (
        "executable bit on files with no shebang (git update-index --chmod=-x <file>): %s" % stray_executable)
