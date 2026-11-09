"""The host floor makes a missing storage root root's 0711 before the installer's first `up`, and leaves one that is there.

WHY: deploy-stack.yml makes a missing KONTROLL_STORAGE_ROOT root's 0711 (passable, listed and written by nobody else;
SECURITY.md C10), but on the installer path the installer container binds the root (docker/services/installer.yaml),
so Docker had already made it root:root 0755 when the play looked, and the play leaves a root that exists as it is:
every installer-path node had a 0755 root, which docs/SETUP.md and docker/.env.example said could not happen (I-24,
2026-11-08). scripts/install-prereqs.sh, which runs as root on the host before the installer, now makes it through
scripts/lib/storage-root.sh. These rows run that helper as install-prereqs.sh does (sourced, then called), against
paths under tmp_path, and hold install-prereqs.sh to calling it with the variable the deploy reads.

The rows that make a root need root (the helper gives what it makes to uid and gid 0) and are skipped elsewhere; the
rest run as any user. Needs POSIX sh.
"""
import os
import re
import shutil
import stat
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "scripts", "lib", "storage-root.sh")
PREREQS = os.path.join(ROOT, "scripts", "install-prereqs.sh")

_POSIX = pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="runs the POSIX helper under sh")
_AS_ROOT = pytest.mark.skipif(os.name == "nt" or os.geteuid() != 0, reason="making a root's directory needs root")


def _ensure(path, cwd):
    """Source the helper and call storage_root_ensure on `path`, as install-prereqs.sh does; return the result."""
    return subprocess.run(["sh", "-c", '. "$1" && storage_root_ensure "$2"', "sh", LIB, path],
                          capture_output=True, text=True, timeout=60, cwd=str(cwd))


def _owned(path):
    st = os.lstat(path)
    return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)


@_POSIX
@_AS_ROOT
def test_a_missing_root_and_each_missing_parent_are_made_roots_0711_once(tmp_path):
    """A missing root two levels below a directory that is there is made, with both missing parents, each uid 0,
    gid 0, mode exactly 0711, as deploy-stack's `file` task makes a root; and a second call leaves them as they are
    and says so. The directory that was there is setgid and of group 1001, so a directory made in it inherits that
    group and the setgid bit unless the helper gives both back, which `file` would. Guards I-24 (a 0755 root) and a
    helper that makes only the last directory so, or leaves an inherited group or bit."""
    there = tmp_path / "there"
    there.mkdir()
    os.chown(str(there), 0, 1001)
    os.chmod(str(there), 0o2775)
    root = there / "a" / "b" / "kontroll"
    r = _ensure(str(root), tmp_path)
    assert r.returncode == 0 and "made the storage root" in r.stdout, (r.stdout, r.stderr)
    for made in (there / "a", there / "a" / "b", root):
        assert _owned(str(made)) == (0, 0, 0o711), (str(made), _owned(str(made)))
    assert _owned(str(there)) == (0, 1001, 0o2775), "the directory that was there is not touched"
    again = _ensure(str(root), tmp_path)
    assert again.returncode == 0 and "left as it is" in again.stdout, (again.stdout, again.stderr)
    assert _owned(str(root)) == (0, 0, 0o711)


@_POSIX
def test_a_root_that_is_there_is_left_as_it_is_whatever_it_is(tmp_path):
    """A directory of the operator's own at 0700, a regular file and a link to nothing are each left exactly as they
    are (owner, mode, type; the link is not followed and nothing is made at its target), and the call succeeds: the
    deploy judges a root that is there and prints the fix, as docs/SETUP.md says, and a mount or link the operator
    placed is theirs. Guards a helper that tightens, widens or replaces a root it did not make."""
    mine = tmp_path / "mine"
    mine.mkdir()
    os.chmod(str(mine), 0o700)
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    os.symlink(str(tmp_path / "nowhere"), str(link))
    for path in (mine, afile, link):
        before = _owned(str(path))
        r = _ensure(str(path), tmp_path)
        assert r.returncode == 0 and "left as it is" in r.stdout, (str(path), r.stdout, r.stderr)
        assert _owned(str(path)) == before, str(path)
    assert not (tmp_path / "nowhere").exists(), "the link was followed"


@_POSIX
@pytest.mark.parametrize("unsafe", ["/", "//", "///", "relative/kontroll", "./kontroll"])
def test_an_unsafe_root_is_refused_and_nothing_is_made(tmp_path, unsafe):
    """The filesystem root (however many slashes) and a relative path are refused with exit 2 and a reason, and
    nothing is made, the same values deploy-stack's assert refuses (absolute, and not `/` once slashes collapse).
    Guards a helper that would run `chown`/`chmod` on `/`, or make a root relative to wherever it was run."""
    r = _ensure(unsafe, tmp_path)
    assert r.returncode == 2 and "nothing made" in r.stderr, (unsafe, r.returncode, r.stderr)
    assert os.listdir(str(tmp_path)) == [], os.listdir(str(tmp_path))


@_POSIX
def test_a_parent_that_is_not_a_directory_fails_with_the_reason(tmp_path):
    """A root whose parent is a regular file cannot be made: the call fails (exit 1) naming the directory it could
    not make, and install-prereqs.sh then prints where the manual steps are. Guards a helper that reports a root it
    did not make as made."""
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    r = _ensure(str(tmp_path / "afile" / "kontroll"), tmp_path)
    assert r.returncode == 1 and "could not make" in r.stderr, (r.returncode, r.stderr)


def test_install_prereqs_makes_the_root_the_deploy_reads_before_it_says_done():
    """install-prereqs.sh checks for the helper with its other libraries (so a copy without it stops before installing
    anything), sources it, and calls storage_root_ensure with `${KONTROLL_STORAGE_ROOT:-}` (the variable the
    installer binds and the deploy reads; empty means the helper's default) after it has installed Docker and
    before it prints that it is done. Guards the helper being dropped, called with another variable, or called only
    after the operator was told to run the installer."""
    with open(PREREQS, encoding="utf-8") as fh:
        src = fh.read()
    assert re.search(r"^for lib in [^;]*\bstorage-root\.sh\b", src, re.M), "the library check must name storage-root.sh"
    assert '. "$SCRIPT_DIR/lib/storage-root.sh"' in src
    call = src.index('storage_root_ensure "${KONTROLL_STORAGE_ROOT:-}"')
    assert src.index("svc_enable_now docker") < call < src.index("cat <<'NEXT'"), "the root is made before 'Done'"


def test_the_helpers_default_root_is_the_installers_and_the_deploys():
    """The helper's default root is the one the installer binds when KONTROLL_STORAGE_ROOT is unset
    (docker/services/installer.yaml) and the one deploy-stack.yml falls back to. Guards the three drifting apart,
    which would have the host floor make a root nothing binds while Docker makes the real one 0755 again."""
    with open(LIB, encoding="utf-8") as fh:
        default = re.search(r"^KONTROLL_STORAGE_ROOT_DEFAULT=(\S+)$", fh.read(), re.M).group(1)
    with open(os.path.join(ROOT, "docker", "services", "installer.yaml"), encoding="utf-8") as fh:
        vols = [v for svc in yaml.safe_load(fh)["services"].values() for v in svc.get("volumes") or []
                if isinstance(v, str) and v.startswith("${KONTROLL_STORAGE_ROOT")]
    sources = [re.match(r"\$\{KONTROLL_STORAGE_ROOT:-([^}]*)\}:", v) for v in vols]
    assert vols and all(m and m.group(1) == default for m in sources), (default, vols)
    with open(os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8") as fh:
        assert "kontroll_storage_root | default('%s', true)" % default in fh.read()
