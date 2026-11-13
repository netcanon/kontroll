"""Each launcher makes a missing storage root root's 0711 before its first `docker compose run` binds it, or says how.

WHY (I-35): the installer's first `docker compose run` binds the storage root (docker/services/installer.yaml), and a
bind makes a missing one root:root 0755, which others can list. deploy-stack makes a missing root root's 0711 but
leaves one that is there, so it passed the bind's (SECURITY.md C10). I-24 made the root in install-prereqs.sh, which
runs as root before the installer, but a node it never ran on (Docker already there, the step skipped) still met the
bind first, and nothing said so. Each launcher now runs storage_root_ensure_or_warn (scripts/lib/storage-root.sh)
after its canonical-group step: a missing root is made root's 0711 (as root directly, else through `sudo -n` with the
helper sourced again), and where it cannot be, a warning gives the one command that makes it; a root that is there is
left, with the line that closes it when others can list it; a root the deploy would refuse is named. Never fatal.

The helper rows run it under `sh -eu` (as kontroll.sh runs) with a PATH that holds nothing but stand-ins (an `id`
whose `id -u` answers what the row needs; a `sudo` that writes its call down, or none) and links to the few tools it
uses, so the real sudo is never run. The rows that need root (a root's directory to list, a make that succeeds) are
skipped elsewhere; tests/validate.sh runs this file as root too, where every row runs. The launcher rows run each
launcher itself, as tests/unit/test_operator_uid_1001_refused.py does, with stand-ins for docker, sudo and the group
tools first on PATH. Needs POSIX sh and bash.
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
LAUNCHER_VARS = {"kontroll.sh": "$ROOT", "kontroll-installer.sh": "$REPO_ROOT"}
# The call the helper makes through sudo, as `$*` writes it down, before its two arguments (the file, the root).
SUDO_MAKE = '-n sh -c . "$1" && storage_root_ensure "$2" sh'
TOOLS = ("sh", "stat", "tr", "mkdir", "chown", "chmod")
_HOST_TOOLS = {t: shutil.which(t) for t in TOOLS + ("bash", "id", "git")}

_POSIX = pytest.mark.skipif(os.name == "nt" or not all(_HOST_TOOLS.values()),
                            reason="runs the POSIX helper and launchers with stand-ins on PATH (CI has the tools)")
_IS_ROOT = os.name != "nt" and os.geteuid() == 0
_AS_ROOT = pytest.mark.skipif(not _IS_ROOT, reason="a root's directory, made or listed, needs root")

# What each stand-in sudo does after writing its call down: answer `-n true` and run nothing; refuse everything, as a
# sudo that asks for a password does under -n; answer `-n true` and fail the make, saying why; or run the call, which
# only a row that is root may use.
_SUDO = {
    "records": "exit 0",
    "asks": 'echo "sudo: a password is required" >&2; exit 1',
    "fails": '[ "$*" = "-n true" ] && exit 0; echo "stand-in sudo: the make failed here" >&2; exit 1',
    "runs": '[ "$*" = "-n true" ] && exit 0; shift; exec "$@"',
}


def _write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    os.chmod(str(path), 0o755)


def _bin(tmp_path, uid, sudo):
    """A directory to be the whole PATH: links to the tools the helper uses, an `id` whose `id -u` prints `uid` (every
    other question goes to the real one), and, unless `sudo` is None, a sudo that writes `$*` down and then acts as
    _SUDO[sudo] says. Returns (the directory, a function giving the sudo calls)."""
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "sudo.log"
    for tool in TOOLS:
        os.symlink(_HOST_TOOLS[tool], str(b / tool))
    _write(b / "id", '#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = -u ]; then echo %s; exit 0; fi\nexec \'%s\' "$@"\n'
           % (uid, _HOST_TOOLS["id"]))
    if sudo:
        _write(b / "sudo", "#!/bin/sh\nprintf '%%s\\n' \"$*\" >> '%s'\n%s\n" % (log, _SUDO[sudo]))
    return str(b), lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _run(tmp_path, root, uid="1000", sudo="records", shell=("sh", "-eu")):
    """Source the helper and call storage_root_ensure_or_warn on `root` as the launchers do (the root, then the file),
    under `shell`, then echo "went on"; return (the result, the sudo calls)."""
    path, calls = _bin(tmp_path, uid, sudo)
    script = '. "$1"; storage_root_ensure_or_warn "$2" "$1"; echo "went on"'
    run = subprocess.run([_HOST_TOOLS[shell[0]]] + list(shell[1:]) + ["-c", script, "sh", LIB, str(root)],
                         capture_output=True, encoding="utf-8", timeout=60, cwd=str(tmp_path),
                         env={"PATH": path, "HOME": str(tmp_path), "LC_ALL": "C"})
    return run, calls()


def _owned(path):
    st = os.lstat(str(path))
    return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)


def _warning_command(root):
    return "sudo sh -c '. \"%s\" && storage_root_ensure \"%s\"'" % (LIB, root)


def _bind_default():
    """The root the installer's bind uses when KONTROLL_STORAGE_ROOT is unset (every bind of it must agree)."""
    with open(os.path.join(ROOT, "docker", "services", "installer.yaml"), encoding="utf-8") as fh:
        vols = [v for svc in yaml.safe_load(fh)["services"].values() for v in svc.get("volumes") or []
                if isinstance(v, str) and v.startswith("${KONTROLL_STORAGE_ROOT")]
    defaults = {re.match(r"\$\{KONTROLL_STORAGE_ROOT:-([^}]*)\}:", v).group(1) for v in vols}
    assert len(defaults) == 1, vols
    return defaults.pop()


@pytest.mark.parametrize("name", sorted(LAUNCHER_VARS))
def test_each_launcher_runs_the_step_after_its_refusal_and_before_any_compose_run(name):
    """Each launcher sources the helper from its own tree, when it is there, and calls storage_root_ensure_or_warn
    with the root the installer's bind uses (KONTROLL_STORAGE_ROOT, defaulting as the bind and the helper do) and the
    helper's own path; the call comes after the uid-1001 refusal and the canonical-group step, and before the first
    line that runs `docker compose`. Guards the step dropped, placed after the compose run (the bind makes the root
    first, which is I-35), placed before the refusal (it would make a root for an operator then refused), or handed
    a root other than the one bound."""
    with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as fh:
        src = fh.read()
    var = re.escape(LAUNCHER_VARS[name])
    lib = var + r"/scripts/lib/storage-root\.sh"
    m = re.search(r'^if \[ -f "%s" \]; then\n(?:  #[^\n]*\n)*  \. "%s"\n'
                  r'  storage_root_ensure_or_warn "\$\{KONTROLL_STORAGE_ROOT:-([^}]*)\}" "%s"\nfi$' % (lib, lib, lib),
                  src, re.M)
    assert m, "%s: the storage-root step is not there in its shape" % name
    with open(LIB, encoding="utf-8") as fh:
        lib_default = re.search(r"^KONTROLL_STORAGE_ROOT_DEFAULT=(\S+)$", fh.read(), re.M).group(1)
    assert m.group(1) == _bind_default() == lib_default, (m.group(1), _bind_default(), lib_default)
    code = [(i.start(), i.group(0)) for i in re.finditer(r"^[^#\n]*$", src, re.M)]
    compose = min(pos for pos, line in code if "docker compose" in line)
    assert src.index("has uid 1001, the uid the network service runs as") < m.start(), name
    assert src.index("canonical_group_ensure_or_warn \"$KONTROLL_OPERATOR_USER\"") < m.start() < compose, name


@_POSIX
def test_a_missing_root_is_made_through_sudo_n_with_the_helper_sourced_again(tmp_path):
    """Not root, with a sudo that needs no password: the helper asks `sudo -n true`, then makes the root through
    `sudo -n sh -c '. "$1" && storage_root_ensure "$2"' sh <the file> <the root>`, says nothing more and goes on. The
    stand-in runs nothing, so nothing is made. Guards a launcher step that never reaches sudo (so a node without
    install-prereqs.sh keeps the bind's 0755 root), a sudo call that could prompt (no -n), and one that passes the
    root or the file in the script's text rather than as arguments."""
    root = tmp_path / "srv" / "kontroll"
    run, calls = _run(tmp_path, root)
    assert run.returncode == 0 and run.stdout == "went on\n" and run.stderr == "", (run.stdout, run.stderr)
    assert calls == ["-n true", "%s %s %s" % (SUDO_MAKE, LIB, root)], calls
    assert not os.path.lexists(str(root.parent))


@_POSIX
@_AS_ROOT
def test_a_root_made_through_sudo_is_roots_0711_with_each_parent(tmp_path):
    """The same call, run (the stand-in sudo runs it, and this row is root): the root and its missing parent are made
    uid 0, gid 0, mode 0711, and storage_root_ensure's line says so. Guards the call's quoting and the helper's
    sourcing under sudo: a call that does not reach storage_root_ensure would make nothing."""
    root = tmp_path / "srv" / "kontroll"
    run, calls = _run(tmp_path, root, sudo="runs")
    assert run.returncode == 0 and "WARNING" not in run.stderr, run.stderr
    assert "made the storage root, root's 0711: %s %s" % (root.parent, root) in run.stdout, run.stdout
    assert _owned(root.parent) == _owned(root) == (0, 0, 0o711)
    assert calls == ["-n true", "%s %s %s" % (SUDO_MAKE, LIB, root)], calls


@_POSIX
@_AS_ROOT
def test_as_root_a_missing_root_is_made_directly_and_sudo_is_not_called(tmp_path):
    """Run as root, the helper makes the root itself (root's 0711) and calls no sudo. Guards a root launcher sent
    through sudo, which a host without sudo (a root shell in a minimal image) does not have."""
    root = tmp_path / "srv" / "kontroll"
    run, calls = _run(tmp_path, root, uid="0")
    assert run.returncode == 0 and "WARNING" not in run.stderr, run.stderr
    assert _owned(root) == (0, 0, 0o711) and calls == [], calls


@_POSIX
@pytest.mark.parametrize("shell", [("sh", "-eu"), ("bash", "-euo", "pipefail")], ids=["sh -eu", "bash pipefail"])
@pytest.mark.parametrize("sudo", ["asks", None], ids=["a sudo that asks a password", "no sudo"])
def test_without_a_sudo_that_needs_no_password_it_warns_and_goes_on(tmp_path, sudo, shell):
    """Not root, and the only sudo asks for a password (or there is none): nothing is made, the run goes on (rc 0,
    under each launcher's own shell options), and the warning names the root, what the bind will make of it, the one
    command that makes it (the helper's path and the root quoted), the chmod that closes it after, and docs/SETUP.md;
    the only sudo call is the `-n true` probe. Guards a warning that is missing or wrong, a step that prompts or stops
    the launcher, and one that runs the make without asking whether sudo may."""
    root = tmp_path / "srv" / "kontroll"
    run, calls = _run(tmp_path, root, sudo=sudo, shell=shell)
    assert run.returncode == 0 and run.stdout == "went on\n", (run.returncode, run.stdout, run.stderr)
    err = run.stderr
    assert "WARNING — the storage root %s is missing" % root in err, err
    assert "has no sudo that needs no password" in err and "root:root 0755" in err, err
    assert _warning_command(root) in err and "sudo chmod 0711 %s." % root in err, err
    assert 'docs/SETUP.md "Pre-provisioning storage"' in err, err
    assert calls == (["-n true"] if sudo else []), calls
    assert not os.path.lexists(str(root.parent))


@_POSIX
@pytest.mark.parametrize("uid,sudo,reason", [("1000", "fails", "stand-in sudo: the make failed here"),
                                             ("0", "records", "storage root: ")],
                         ids=["through sudo", "as root, under a file"])
def test_a_make_that_fails_warns_under_its_reason_and_goes_on(tmp_path, uid, sudo, reason):
    """A make that fails, through sudo or as root (the root under a regular file; not root, the helper refuses the
    test's own directories above it first, also a failure with its reason), leaves the reason first and then the
    warning that points at it, with the command that makes it, and the run goes on. Guards a failure the step
    swallows (the bind then makes the 0755 root unremarked) or one that stops the launcher."""
    blocker = tmp_path / "afile"
    blocker.write_text("x\n", encoding="utf-8")
    root = blocker / "kontroll"
    run, calls = _run(tmp_path, root, uid=uid, sudo=sudo)
    assert run.returncode == 0 and run.stdout.endswith("went on\n"), (run.stdout, run.stderr)
    said = "WARNING — the storage root %s could not be made root's 0711 (the reason is above)." % root
    assert reason in run.stderr and said in run.stderr, run.stderr
    assert run.stderr.index(reason) < run.stderr.index(said), run.stderr
    assert _warning_command(root) in run.stderr, run.stderr
    assert calls == (["-n true", "%s %s %s" % (SUDO_MAKE, LIB, root)] if uid != "0" else []), calls
    assert blocker.read_text(encoding="utf-8") == "x\n"


@_POSIX
@pytest.mark.parametrize("kind", ["dir 0700", "dir 0711", "dir 0775", "file", "dangling link"])
def test_a_root_that_is_there_and_others_cannot_list_is_left_without_a_word(tmp_path, kind):
    """A root that is there and that others cannot list (or that is not a plain directory at all, or that others may
    write, which the deploy refuses with its own fix) is left exactly as it is: no line, no sudo call. As any user
    the directory is not root's; as root (the gate) it is, and still not listable. Guards a step that changes a root
    the operator made, follows a link, or prints a chmod that would not be the fix."""
    root = tmp_path / "root"
    if kind.startswith("dir"):
        root.mkdir()
        os.chmod(str(root), int(kind.split()[1], 8))
    elif kind == "file":
        root.write_text("", encoding="utf-8")
    else:
        os.symlink(str(tmp_path / "nowhere"), str(root))
    before = _owned(root)
    run, calls = _run(tmp_path, root)
    assert (run.returncode, run.stdout, run.stderr, calls) == (0, "went on\n", "", []), (run.stderr, calls)
    assert _owned(root) == before


@_POSIX
@_AS_ROOT
@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705], ids=oct)
def test_a_root_others_can_list_gets_the_line_that_closes_it(tmp_path, mode):
    """A root's directory that others can list (what a bind makes, 0755, or one its group or the world can read) is
    left as it is, and one line on stderr gives the chmod that closes it; no sudo is called. Guards I-35's other half:
    a root the bind already made, on a node the step reaches too late, is said, not passed in silence."""
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(str(root), mode)
    run, calls = _run(tmp_path, root)
    assert run.returncode == 0 and run.stdout == "went on\n" and calls == [], (run.stderr, calls)
    assert run.stderr == ("kontroll: the storage root %s is root's and others can list it (a bind made it so); root's"
                          " 0711 closes it: sudo chmod 0711 %s\n" % (root, root)), run.stderr
    assert _owned(root) == (0, 0, mode)


@_POSIX
@pytest.mark.parametrize("root", ["relative/kontroll", "/", "//"])
def test_a_root_the_deploy_refuses_is_named_and_left(tmp_path, root):
    """A root that is not an absolute path other than `/` (deploy-stack's own assert) is named in a warning and
    nothing else is done or printed: no make, no sudo, and no chmod line. Guards the `/` case above all: it exists,
    is root's and listable, so without the check the step would print `sudo chmod 0711 /`."""
    run, calls = _run(tmp_path, root, uid="0")
    assert run.returncode == 0 and run.stdout == "went on\n" and calls == [], (run.stderr, calls)
    assert "the storage root '%s' is not an absolute path other than /" % root in run.stderr, run.stderr
    assert "chmod" not in run.stderr and not os.path.lexists(str(tmp_path / "relative")), run.stderr


@_POSIX
@_AS_ROOT
def test_the_command_the_warning_gives_makes_the_root(tmp_path):
    """The command the warning prints, run as root (without its `sudo`, as this row is root), makes the root its
    0711 with each missing parent. Guards a printed command whose quoting or path does not work, which an operator
    would meet only on the node that needs it."""
    root = tmp_path / "srv" / "kontroll"
    run, _ = _run(tmp_path, root, sudo=None)
    line = [ln.strip() for ln in run.stderr.splitlines() if ln.strip().startswith("sudo sh -c ")]
    assert len(line) == 1, run.stderr
    made = subprocess.run([_HOST_TOOLS["sh"], "-c", line[0][len("sudo "):]], capture_output=True, encoding="utf-8",
                          timeout=60)
    assert made.returncode == 0, made.stderr
    assert _owned(root.parent) == _owned(root) == (0, 0, 0o711)


def _launcher_bin(tmp_path, sudo):
    """Stand-ins first on PATH for a whole launcher: an `id` answering `-u` 1000, a docker, groupadd and usermod
    that write their call down and run nothing, a sudo that writes its call down and acts as _SUDO[sudo] says, and a
    `getent` that knows no gid-1001 group. One log, so the order of the calls shows."""
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "calls.log"
    _write(b / "id", '#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = -u ]; then echo 1000; exit 0; fi\nexec \'%s\' "$@"\n'
           % _HOST_TOOLS["id"])
    real_getent = shutil.which("getent")
    _write(b / "getent", '#!/bin/sh\nif [ "$1" = group ] && [ "$2" = 1001 ]; then exit 2; fi\n'
           + ("exec '%s' \"$@\"\n" % real_getent if real_getent else "exit 2\n"))
    for name in ("docker", "groupadd", "usermod"):
        _write(b / name, "#!/bin/sh\necho \"%s $*\" >> '%s'\nexit 0\n" % (name, log))
    _write(b / "sudo", "#!/bin/sh\necho \"sudo $*\" >> '%s'\n%s\n" % (log, _SUDO[sudo]))
    return str(b) + os.pathsep + os.environ["PATH"], lambda: log.read_text(encoding="utf-8").splitlines()


def _kit(tmp_path):
    """A launch kit reduced to what scripts/kontroll.sh reads before it hands over to docker, its libraries among
    them. Returns (the command, the helper's path in it)."""
    kit = tmp_path / "kit"
    (kit / "scripts" / "lib").mkdir(parents=True)
    (kit / "docker" / "services").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll.sh"), str(kit / "kontroll"))
    for lib in ("plaintext-guard.sh", "canonical-group.sh", "storage-root.sh"):
        shutil.copy(os.path.join(ROOT, "scripts", "lib", lib), str(kit / "scripts" / "lib"))
    (kit / "docker" / "services" / "installer.yaml").write_text("services: {}\n", encoding="utf-8")
    (kit / "images.env").write_text("KONTROLL_INSTALLER_IMAGE=registry.example.test/installer@sha256:%s\n" % ("0" * 64),
                                    encoding="utf-8")
    (kit / "README.md").write_text("the tool\n", encoding="utf-8")
    return ["sh", str(kit / "kontroll")], str(kit / "scripts" / "lib" / "storage-root.sh")


def _tree(tmp_path):
    """A source checkout reduced to scripts/kontroll-installer.sh and its libraries."""
    tree = tmp_path / "tree"
    (tree / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll-installer.sh"), str(tree / "scripts"))
    for lib in ("canonical-group.sh", "storage-root.sh"):
        shutil.copy(os.path.join(ROOT, "scripts", "lib", lib), str(tree / "scripts" / "lib"))
    return ["bash", str(tree / "scripts" / "kontroll-installer.sh")], str(tree / "scripts" / "lib" / "storage-root.sh")


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("sudo", ["records", "asks"], ids=["sudo needs no password", "sudo asks a password"])
@pytest.mark.parametrize("launcher", [_kit, _tree], ids=["launch-kit kontroll", "kontroll-installer.sh"])
def test_each_launcher_makes_the_root_or_warns_before_its_first_compose_run(tmp_path, launcher, sudo):
    """Each launcher, run whole for `init` with KONTROLL_STORAGE_ROOT a missing directory: where sudo needs no
    password, its call to make that root (the helper in the launcher's own tree) comes before the first `docker
    compose` call; where sudo asks, the warning is printed and the launcher still goes on to the compose run (rc 0).
    Guards the step wired where it never runs, run after the bind, handed another root or file, or stopping the
    launcher under its own `set -e`."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "store"
    path, calls = _launcher_bin(tmp_path, sudo)
    argv, lib = launcher(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KONTROLL_", "SUDO_"))}
    env.update(PATH=path, HOME=str(home), KONTROLL_STORAGE_ROOT=str(root))
    run = subprocess.run(argv + ["init"], capture_output=True, encoding="utf-8", env=env, cwd=str(tmp_path),
                         timeout=120)
    assert run.returncode == 0, run.stderr
    seen = calls()
    compose = [i for i, c in enumerate(seen) if c.startswith("docker compose ")]
    assert compose and compose[-1] == len(seen) - 1, seen
    make = "sudo %s %s %s" % (SUDO_MAKE, lib, root)
    if sudo == "records":
        assert make in seen and seen.index(make) < compose[0], seen
        assert "the storage root" not in run.stderr, run.stderr
    else:
        assert make not in seen, seen
        assert "WARNING — the storage root %s is missing" % root in run.stderr, run.stderr
    assert not os.path.lexists(str(root))


def test_the_rows_that_need_root_run_in_the_gate_as_root():
    """The rows that make or list a root's directory need root and are skipped elsewhere, and the public CI runs the
    suite as an ordinary user, so without tests/validate.sh's as-root step they would run nowhere. That step fails
    `--strict` on any skip; it must name this file and deselect none of it (every row here runs as root). Guards the
    file dropped from the step, and a deselect added that hides a row."""
    with open(os.path.join(ROOT, "tests", "validate.sh"), encoding="utf-8") as fh:
        text = fh.read()
    step = text.split("step canon-drop-as-root", 1)[1].split("\n  fi\n", 1)[0]
    me = "tests/unit/test_storage_root_launchers.py"
    assert re.search(r"^\s*%s \\$" % re.escape(me), step, re.M), step
    assert not [d for d in re.findall(r"--deselect (\S+)", step) if d.startswith(me)], step
