"""The host floor makes a missing storage root root's 0711 before the installer's first `up`, and leaves one that is there.

WHY: deploy-stack.yml makes a missing storage root root's 0711 (passable, listed and written by nobody else;
SECURITY.md C10), but on the installer path the installer container binds the root (docker/services/installer.yaml),
so Docker had already made it root:root 0755 when the play looked, and the play leaves a root that exists as it is:
every installer-path node had a 0755 root, which docs/SETUP.md and docker/.env.example said could not happen (I-24,
2026-11-08). scripts/install-prereqs.sh, which runs as root on the host before the installer, now makes it through
scripts/lib/storage-root.sh. The first rows run that helper as install-prereqs.sh does (sourced, then called),
against paths under tmp_path; the last run install-prereqs.sh itself, as root, with stand-ins for its package
manager, Docker and the group tools on a PATH that holds nothing else, so nothing on the host is installed or changed.

The rows that make a root need root (the helper gives what it makes to uid and gid 0), and so do the rows that
refuse a directory above the root for its mode, or fail on a parent that is a file: as any other account the helper
refuses the account's own directories above tmp_path first. They are skipped elsewhere; tests/validate.sh runs this
file as root too. The rest run as any user. Needs POSIX sh and bash.
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
BASH = shutil.which("bash")

_POSIX = pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="runs the POSIX helper under sh")
_IS_ROOT = os.name != "nt" and os.geteuid() == 0
_AS_ROOT = pytest.mark.skipif(not _IS_ROOT, reason="making a root's directory needs root")
_NOT_ROOT = pytest.mark.skipif(os.name == "nt" or _IS_ROOT, reason="shows what happens without root")
_BASH = pytest.mark.skipif(os.name == "nt" or not BASH, reason="runs install-prereqs.sh under bash")


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
    """A missing root three levels below a directory that is there is made, with its two missing parents, each uid 0,
    gid 0, mode exactly 0711, as deploy-stack's `file` task makes a root; and a second call leaves them as they are
    and says so. The directory that was there is setgid and of group 1001, so a directory made in it inherits that
    group and the setgid bit unless the helper gives both back, which `file` would. Guards I-24 (a 0755 root) and a
    helper that makes only the last directory so, or leaves an inherited group or bit."""
    there = tmp_path / "there"
    there.mkdir()
    os.chown(str(there), 0, 1001)
    os.chmod(str(there), 0o2755)
    root = there / "a" / "b" / "kontroll"
    r = _ensure(str(root), tmp_path)
    assert r.returncode == 0 and "made the storage root" in r.stdout, (r.stdout, r.stderr)
    for made in (there / "a", there / "a" / "b", root):
        assert _owned(str(made)) == (0, 0, 0o711), (str(made), _owned(str(made)))
    assert _owned(str(there)) == (0, 1001, 0o2755), "the directory that was there is not touched"
    again = _ensure(str(root), tmp_path)
    assert again.returncode == 0 and "left as it is" in again.stdout, (again.stdout, again.stderr)
    assert _owned(str(root)) == (0, 0, 0o711)


@_POSIX
def test_a_root_that_is_there_is_left_as_it_is_whatever_it_is(tmp_path):
    """A directory at 0700 and one at 0755 (the shape Docker leaves), a regular file and a link to nothing are each
    left exactly as they are (owner, mode, type; the link is not followed and nothing is made at its target), the call
    succeeds, and it says what is there: the deploy judges a root that is there, and a mount or link the operator
    placed is theirs. Guards a helper that tightens, widens or replaces a root it did not make (a 0755 directory is
    the one a tightening helper would change)."""
    mine = tmp_path / "mine"
    mine.mkdir()
    os.chmod(str(mine), 0o700)
    wide = tmp_path / "wide"
    wide.mkdir()
    os.chmod(str(wide), 0o755)
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    os.symlink(str(tmp_path / "nowhere"), str(link))
    for path in (mine, wide, afile, link):
        before = _owned(str(path))
        r = _ensure(str(path), tmp_path)
        assert r.returncode == 0 and "left as it is" in r.stdout, (str(path), r.stdout, r.stderr)
        assert _owned(str(path)) == before, str(path)
        if path != link:
            assert "(%d:%d %o)" % before in r.stdout, (str(path), r.stdout)
    assert not (tmp_path / "nowhere").exists(), "the link was followed"


@_POSIX
@_AS_ROOT
def test_a_root_others_can_list_gets_the_line_that_closes_it(tmp_path):
    """A root:root directory others can list (0755, what Docker makes; 0751) gets the `sudo chmod 0711 <root>` line,
    because the deploy passes it as it is and would never print a fix for it. One at 0711 gets no such line, nor one
    the deploy refuses and prints its own fix for (group-writable 0775, or another uid's 0755: a chmod alone would not
    make that one root's). Each is left as it was. Guards a helper whose "left as it is" hides the 0755 root every
    upgraded installer-path node has (I-24 read, N-2), and one that offers a chmod where it does not close the root."""
    for mode, uid, hint in ((0o755, 0, True), (0o751, 0, True), (0o711, 0, False), (0o775, 0, False),
                            (0o755, 1000, False)):
        d = tmp_path / ("r%o-%d" % (mode, uid))
        d.mkdir()
        os.chown(str(d), uid, 0)
        os.chmod(str(d), mode)
        r = _ensure(str(d), tmp_path)
        assert r.returncode == 0, (oct(mode), uid, r.stderr)
        assert ("sudo chmod 0711 %s" % d in r.stdout) == hint, (oct(mode), uid, r.stdout)
        assert _owned(str(d)) == (uid, 0, mode), (oct(mode), uid)


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
@_AS_ROOT
@pytest.mark.parametrize("mode,below", [(0o777, ""), (0o775, ""), (0o757, ""), (0o777, "sub")])
def test_a_root_under_a_directory_others_can_write_is_refused(tmp_path, mode, below):
    """A root to be made under a directory that others can write and that is not sticky (world- or group-writable,
    the directory itself or one further up) is refused with exit 3 and the reason, and nothing is made. The helper
    makes a directory, then gives it to root and sets its mode, and `chown` and `chmod` follow a link: under such a
    directory another account could swap the new directory for a link between the two and have root's chown and
    chmod land on what the link names. Guards the swap (the I-24 read, reader A's F-3). As root, so that the
    directories above tmp_path are root's and the open one is the reason."""
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    os.chmod(str(open_dir), mode)
    parent = open_dir
    if below:
        parent = open_dir / below
        parent.mkdir()
        os.chmod(str(parent), 0o755)
    before = sorted(os.listdir(str(parent)))
    r = _ensure(str(parent / "kontroll"), tmp_path)
    assert r.returncode == 3 and "writable by others" in r.stderr and "nothing made" in r.stderr, (
        oct(mode), below, r.returncode, r.stderr)
    assert sorted(os.listdir(str(parent))) == before
    assert stat.S_IMODE(os.stat(str(open_dir)).st_mode) == mode


@_POSIX
@_AS_ROOT
def test_a_root_under_a_sticky_directory_is_made(tmp_path):
    """Under a sticky directory others can write (1777, as /tmp is) the root is made root's 0711: there nobody but
    the owner of an entry, or of the directory, can rename or remove it, so what the helper made stays where it made
    it. Guards a refusal so wide it refuses every root under /tmp."""
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    os.chmod(str(sticky), 0o1777)
    r = _ensure(str(sticky / "kontroll"), tmp_path)
    assert r.returncode == 0 and "made the storage root" in r.stdout, (r.stdout, r.stderr)
    assert _owned(str(sticky / "kontroll")) == (0, 0, 0o711)


@_POSIX
@pytest.mark.parametrize("how", ["the-parent", "further-up", "through-a-link", "a-link-in-it"])
def test_a_root_under_another_accounts_directory_is_refused_wherever_its_links_lead(tmp_path, how):
    """A root to be made under a directory another account owns (uid 1000's 0755, as root; the account's own, run
    as any other: its tmp_path is), whether that directory is the parent, one further up, where a link on the way
    leads, or the one that holds such a link, is refused with exit 3, the uid and "nothing made", and nothing is
    made. The owner of a directory can rename what is in it, sticky or not, so it could swap the new directory for a
    link before root's `chown` and `chmod`, which follow one, or swap the link it holds; and a link's own directory
    says nothing about where it leads, so each directory on the way is judged, and judged where `cd -P` lands.
    Guards the owner left unread (only the mode bits judged), the walk taken along the path as written, and one that
    judges only the deepest directory there (the I-24 read, reader A's F-3 in full)."""
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    os.chmod(str(theirs), 0o755)
    uid = os.geteuid()
    inner = theirs / "inner"
    inner.mkdir()
    os.chmod(str(inner), 0o755)
    if _IS_ROOT:
        os.chown(str(theirs), 1000, 1000)
        uid = 1000
    if how == "the-parent":
        parent = lands = theirs
    elif how == "further-up":
        parent = lands = inner
    elif how == "through-a-link":
        parent, lands = tmp_path / "via", inner
        os.symlink(str(inner), str(parent))
    else:
        lands = tmp_path / "elsewhere"
        lands.mkdir()
        os.chmod(str(lands), 0o755)
        parent = theirs / "out"
        os.symlink(str(lands), str(parent))
    before = sorted(os.listdir(str(lands)))
    r = _ensure(str(parent / "kontroll"), tmp_path)
    assert r.returncode == 3 and "is uid %d's" % uid in r.stderr and "nothing made" in r.stderr, (
        how, r.returncode, r.stderr)
    assert sorted(os.listdir(str(lands))) == before


@_POSIX
@_AS_ROOT
def test_a_parent_that_is_not_a_directory_fails_with_the_reason(tmp_path):
    """A root whose parent is a regular file cannot be made: the call fails (exit 1) naming the directory it could
    not make, and install-prereqs.sh then prints where the manual steps are. Guards a helper that reports a root it
    did not make as made. As root, so that the directories above tmp_path are root's and the file is the reason."""
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    r = _ensure(str(tmp_path / "afile" / "kontroll"), tmp_path)
    assert r.returncode == 1 and "could not make" in r.stderr, (r.returncode, r.stderr)


# --- install-prereqs.sh itself --------------------------------------------------------------------------------------

_REAL = ("dirname", "tr", "cut", "grep", "stat", "mkdir", "chown", "chmod", "cat")
_STANDINS = {
    # A package manager the script supports that is not apt, so no step writes under /etc/apt.
    "pacman": 'echo "pacman $*" >> "$LOG"; exit "${PACMAN_RC:-0}"\n',
    "docker": 'echo "docker $*" >> "$LOG"\ncase "$1" in --version) echo "Docker version stand-in";; '
              'compose) echo "Docker Compose stand-in";; esac\nexit 0\n',
    "getent": 'echo "getent $*" >> "$LOG"; echo "kontroll:x:1001:"\n',
    "groupadd": 'echo "groupadd $*" >> "$LOG"; exit 0\n',
    "usermod": 'echo "usermod $*" >> "$LOG"; exit 0\n',
}


def _prereqs(tmp_path, args=(), env=None):
    """Run install-prereqs.sh with a PATH holding only the tools it needs and the stand-ins above, in an environment of
    its own (no SUDO_USER, no KONTROLL_STORAGE_ROOT unless given); return the result and the stand-ins' log. Its `id`
    answers `id -u` 1000 where the account running the suite has uid 1001 (a GitHub-hosted runner's), which
    install-prereqs.sh refuses before its root check (I-18; tests/unit/test_operator_uid_1001_refused.py holds that),
    and hands every other question to the real `id`."""
    bindir = tmp_path / "bin"
    if not bindir.exists():
        bindir.mkdir()
        for tool in _REAL:
            os.symlink(shutil.which(tool), str(bindir / tool))
        for name, body in _STANDINS.items():
            (bindir / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
            os.chmod(str(bindir / name), 0o755)
        real_id = shutil.which("id")
        (bindir / "id").write_text('#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = -u ]; then\n'
                                   '  u=$(\'%s\' -u); [ "$u" = 1001 ] && u=1000; echo "$u"; exit 0\nfi\n'
                                   'exec \'%s\' "$@"\n' % (real_id, real_id), encoding="utf-8")
        os.chmod(str(bindir / "id"), 0o755)
    log = tmp_path / "calls.log"
    run_env = {"PATH": str(bindir), "HOME": str(tmp_path), "LC_ALL": "C", "LOG": str(log)}
    run_env.update(env or {})
    r = subprocess.run([BASH, PREREQS] + list(args), capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
                       env=run_env)
    return r, (log.read_text(encoding="utf-8") if log.exists() else "")


def test_install_prereqs_checks_for_the_helper_with_its_other_libraries():
    """install-prereqs.sh checks for storage-root.sh with its other libraries at the top, so a copy without it says
    so instead of dying on a bare error (tests/unit/test_install_prereqs_alone.py runs that copy). Guards the check
    losing the helper's name."""
    with open(PREREQS, encoding="utf-8") as fh:
        src = fh.read()
    assert re.search(r"^for lib in [^;]*\bstorage-root\.sh\b", src, re.M), "the library check must name storage-root.sh"


@_BASH
@_AS_ROOT
def test_install_prereqs_makes_the_root_before_any_package_and_says_done(tmp_path):
    """Run as root with --storage-root, install-prereqs.sh makes that root and its missing parent root's 0711, says
    so before its first package step, and goes on to "Done", rc 0; with only KONTROLL_STORAGE_ROOT set (sudo lets it
    through where sudoers allows) it makes that one, and with both the argument wins. A package step that fails
    still leaves the root made, because the root comes first. Guards the call dropped, commented out, fenced off or
    moved after the packages (an apt failure under `set -e` would then leave no root), and the argument unread."""
    root = tmp_path / "srv" / "kontroll"
    r, calls = _prereqs(tmp_path, ["--storage-root", str(root)])
    assert r.returncode == 0 and "==> Done" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert _owned(str(root)) == (0, 0, 0o711) and _owned(str(root.parent)) == (0, 0, 0o711)
    assert r.stdout.index("made the storage root") < r.stdout.index("==> pacman: the host floor"), r.stdout
    assert "was NOT made" not in r.stdout
    assert "pacman -S" in calls, calls
    by_env = tmp_path / "env" / "kontroll"
    r, _ = _prereqs(tmp_path, env={"KONTROLL_STORAGE_ROOT": str(by_env)})
    assert r.returncode == 0 and _owned(str(by_env)) == (0, 0, 0o711), (r.stdout, r.stderr)
    named = tmp_path / "named" / "kontroll"
    other = tmp_path / "other" / "kontroll"
    r, _ = _prereqs(tmp_path, ["--storage-root=%s" % named], env={"KONTROLL_STORAGE_ROOT": str(other)})
    assert r.returncode == 0 and os.path.isdir(str(named)) and not os.path.exists(str(other.parent)), r.stdout
    failing = tmp_path / "failing" / "kontroll"
    r, _ = _prereqs(tmp_path, ["--storage-root", str(failing)], env={"PACMAN_RC": "1"})
    assert r.returncode != 0 and "==> Done" not in r.stdout, (r.returncode, r.stdout)
    assert _owned(str(failing)) == (0, 0, 0o711), "a failing package step left the root unmade"


@_BASH
@_AS_ROOT
@pytest.mark.parametrize("given,why,hint", [
    ("relative/kontroll", "not an absolute path", "give --storage-root an absolute path other than /"),
    ("OPEN", "writable by others", "choose a root under directories that are root's and only root can write"),
    ("FILE", "could not make", "says how to make it by hand"),
])
def test_install_prereqs_says_a_root_it_did_not_make_was_not_made_and_carries_on(tmp_path, given, why, hint):
    """When the root is refused (a relative path; a directory others can write above it) or cannot be made (a parent
    that is a file), install-prereqs.sh prints the reason and the remedy for that case, carries on to "Done" with
    rc 0, and says at the end that the storage root was NOT made, instead of claiming it was. Guards the `||`
    dropped (under `set -e` the script would end there, before the packages), a remedy that does not fit the failure,
    and a closing text that says the root was made (I-24 read, N-3)."""
    if given == "OPEN":
        (tmp_path / "open").mkdir()
        os.chmod(str(tmp_path / "open"), 0o777)
        given = str(tmp_path / "open" / "kontroll")
    elif given == "FILE":
        (tmp_path / "afile").write_text("x", encoding="utf-8")
        given = str(tmp_path / "afile" / "kontroll")
    r, _ = _prereqs(tmp_path, ["--storage-root", given])
    assert r.returncode == 0 and "==> Done" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert why in r.stderr and hint in r.stderr, r.stderr
    assert "The storage root was NOT made" in r.stdout, r.stdout
    assert not os.path.exists(str(tmp_path / "relative")) and not os.path.exists(str(tmp_path / "open" / "kontroll"))


@_BASH
@_NOT_ROOT
def test_install_prereqs_without_root_makes_nothing(tmp_path):
    """Run without root, install-prereqs.sh stops (rc 1) at its root check, with the arguments it was given in the
    line it prints to re-run, and makes nothing at --storage-root: as any other account the helper could make the
    directory but not give it to root. Guards the root step moved above the root check, which would leave an
    account's own 0711 directory where root's should be."""
    root = tmp_path / "srv" / "kontroll"
    r, calls = _prereqs(tmp_path, ["--storage-root", str(root)])
    assert r.returncode == 1 and "needs root" in r.stderr and "--storage-root %s" % root in r.stderr, r.stderr
    assert not os.path.exists(str(root.parent)) and calls == ""


@_BASH
@pytest.mark.parametrize("args", [["--storage-root"], ["--storage-roots", "/x"]])
def test_install_prereqs_refuses_a_storage_root_argument_it_cannot_read(tmp_path, args):
    """`--storage-root` with no path, and an argument it does not know, end the script with rc 2 and the usage line
    before anything runs. Guards a missing path taken as an empty one (which would make the default root instead of
    the one meant)."""
    r, calls = _prereqs(tmp_path, args)
    assert r.returncode == 2 and "--storage-root <root>" in r.stderr, (r.returncode, r.stderr)
    assert calls == ""


def test_the_rows_that_need_root_run_in_the_gate_as_root():
    """The rows that make a root, and those that run install-prereqs.sh, need root and are skipped elsewhere, and the
    public CI runs the suite as an ordinary user, so without tests/validate.sh's as-root step they would run nowhere
    (they did not, before this fold). That step fails `--strict` on any skip; it must name this file and deselect
    only the row that shows a run without root. Guards the file dropped from the step, and a deselect that widens."""
    with open(os.path.join(ROOT, "tests", "validate.sh"), encoding="utf-8") as fh:
        text = fh.read()
    step = text.split("step canon-drop-as-root", 1)[1].split("\n  fi\n", 1)[0]
    me = "tests/unit/test_storage_root_floor.py"
    assert re.search(r"^\s*%s \\$" % re.escape(me), step, re.M), step
    mine = [d for d in re.findall(r"--deselect (\S+)", step) if d.startswith(me)]
    assert mine == [me + "::test_install_prereqs_without_root_makes_nothing"], mine


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
