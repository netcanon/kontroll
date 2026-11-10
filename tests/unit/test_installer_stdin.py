"""What stdin each installer verb gets: both launchers RUN with a stand-in docker that records what its `compose` read.

WHY: `docker compose run` keeps the container's stdin open and reads it, whether or not the verb wants input. So a
verb run where stdin carries a later command's input ate that input: a redeploy run as `ssh <node>
'scripts/kontroll-installer.sh init; bash -s' < verify.sh` ran no verify, since the installer read the script and
`bash -s` got nothing (I-4). Both launchers now run every verb but `shell` with stdin at /dev/null when stdin is no
terminal. No verb but `shell` needs stdin: nothing in the installer prompts, and an ansible-playbook flag that prompts
(`--ask-vault-pass`, `-K`, `-k`, `--step`), forwarded to a verb, reads a terminal only. `shell` keeps its stdin, so
`shell < script.sh` still runs the script, and a terminal is handed over as it is, so `shell` typed at a terminal still
reads what is typed. A text pin can say the redirect is there; only running the launcher says which verb's compose
reads what, for a pipe, a regular file and a terminal.

These tests copy each launcher into a tree under tmp_path with the few files it reads, put a stand-in `docker` first
on PATH, and run the launcher itself, with stdin set by the row. Nothing is pulled, built or deployed, and HOME is a
scratch directory. `pty` is imported in the one row that uses it: Windows has no termios, so a module-level import
would stop the whole session at collection, before this module's skip is read.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(os.name == "nt" or not all(shutil.which(tool) for tool in ("sh", "bash", "git", "awk")),
                       reason="runs the POSIX launchers with a stand-in docker on PATH (CI has sh, bash, git and awk)"),
]

# Every verb but `shell`, as typed: no verb is `init`.
NOT_SHELL = [None, "init", "check", "fresh-init", "configure-semaphore", "playbooks/local-canonical.yml"]
LAUNCHERS = ["kit", "source"]
# What stdin holds the probe line: a pipe (`ssh <node> '…' < file` gives the remote shell one), or a regular file
# (a local `bash -c '…' < file`, and `shell < script.sh`).
STDINS = ["pipe", "file"]
PROBE = "probe-line\n"
KIT_IMAGE_FLAGS = ["-e", "use_published_images=true", "-e", "bake_code=true"]


def _stub_docker(tmp_path):
    """A stand-in docker. Its `compose` appends `<its arguments>|<what it got on stdin>` to a log: `tty` when stdin is
    a terminal (read nothing, so a row cannot hang on it), else `read:` and all it read. Any other docker call (`image
    inspect`, `pull`) reads nothing and exits 0. Returns (the PATH to run under, a function giving the lines)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        '#!/bin/sh\n[ "$1" = compose ] || exit 0\n'
        'if [ -t 0 ]; then got=tty; else got="read:$(cat)"; fi\n'
        'printf \'%%s|%%s\\n\' "$*" "$got" >> "%s"\n' % str(log).replace("\\", "/"), encoding="utf-8", newline="\n")
    os.chmod(str(bin_dir / "docker"), 0o755)
    return (str(bin_dir) + os.pathsep + os.environ["PATH"],
            lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def _env(tmp_path, path):
    """The launcher's environment: no KONTROLL_* of the tests' own, the stand-in docker first on PATH, and a
    scratch HOME."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=path, HOME=str(home))
    return env


def _launcher(tmp_path, which):
    """The launcher as a shell runs it: (its shell, its path). `kit` is a launch kit reduced to what scripts/kontroll.sh
    reads before it hands over to docker; `source` a checkout reduced to scripts/kontroll-installer.sh (without the
    canonical-group library, which it sources only when present)."""
    if which == "kit":
        kit = tmp_path / "kit"
        (kit / "scripts" / "lib").mkdir(parents=True)
        (kit / "docker" / "services").mkdir(parents=True)
        shutil.copy(os.path.join(ROOT, "scripts", "kontroll.sh"), str(kit / "kontroll"))
        shutil.copy(os.path.join(ROOT, "scripts", "lib", "plaintext-guard.sh"), str(kit / "scripts" / "lib"))
        (kit / "docker" / "services" / "installer.yaml").write_text("services: {}\n", encoding="utf-8")
        (kit / "images.env").write_text(
            "KONTROLL_INSTALLER_IMAGE=registry.example.test/installer@sha256:%s\n" % ("0" * 64), encoding="utf-8")
        (kit / "README.md").write_text("the tool\n", encoding="utf-8")
        return "sh", str(kit / "kontroll")
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll-installer.sh"), str(tree / "scripts"))
    return "bash", str(tree / "scripts" / "kontroll-installer.sh")


def _handed(which, verb):
    """What compose should get after its `--rm init` (the service): the verb and its arguments. The source launcher
    passes on what was typed; the kit names the verb (`init` when none is typed) and adds its two image flags to
    `init` and `check`."""
    if which == "source":
        return [verb] if verb else []
    verb = verb or "init"
    return [verb] + (KIT_IMAGE_FLAGS if verb in ("init", "check") else [])


def _compose(calls, which, verb):
    """The one `compose … run` the launcher handed over to. Asserts it runs the `init` service with the verb and the
    arguments this launcher hands it for `verb` (so a row cannot pass on a launcher that ran the wrong verb), and
    returns what it got on stdin."""
    runs = [c for c in calls() if c.startswith("compose ")]
    assert len(runs) == 1, calls()
    args, got = runs[0].rsplit("|", 1)
    head, service, tail = args.partition(" --rm init")
    assert service and " run" in head, runs
    assert tail.split() == _handed(which, verb), runs
    return got


def _run(cmd, tmp_path, path, stdin):
    """Run `cmd` in the launcher's environment with the probe line on stdin, as a pipe (`pipe`) or a regular file
    (`file`). A file is the shape a test of `-p /dev/stdin` would miss, and the one where a reader and the command
    after it share the file's offset."""
    kwargs = dict(capture_output=True, text=True, env=_env(tmp_path, path), cwd=str(tmp_path), timeout=60)
    if stdin == "pipe":
        return subprocess.run(cmd, input=PROBE, **kwargs)
    src = tmp_path / "stdin.txt"
    src.write_text(PROBE, encoding="utf-8", newline="\n")
    with open(str(src), "rb") as handle:
        return subprocess.run(cmd, stdin=handle, **kwargs)


@pytest.mark.parametrize("stdin", STDINS)
@pytest.mark.parametrize("which", LAUNCHERS)
@pytest.mark.parametrize("verb", NOT_SHELL, ids=[v or "no-verb" for v in NOT_SHELL])
def test_a_verb_other_than_shell_reads_none_of_its_stdin(tmp_path, which, verb, stdin):
    """With stdin a pipe or a regular file holding a line, every verb but `shell` (no verb, `init`, `check`,
    `fresh-init`, `configure-semaphore`, a playbook passthrough) hands compose an empty stdin, from either launcher,
    and compose gets the verb it was given. Guards I-4: the installer reading input meant for whatever runs after it
    in the same shell, so that command runs nothing."""
    path, calls = _stub_docker(tmp_path)
    shell, launcher = _launcher(tmp_path, which)
    run = _run([shell, launcher] + ([verb] if verb else []), tmp_path, path, stdin)
    assert run.returncode == 0, run.stderr
    assert _compose(calls, which, verb) == "read:", calls()


@pytest.mark.parametrize("stdin", STDINS)
@pytest.mark.parametrize("which", LAUNCHERS)
def test_shell_keeps_its_stdin(tmp_path, which, stdin):
    """`shell` with stdin a pipe or a regular file hands compose that stdin, so `shell < script.sh` still runs the
    script in the container. Guards the fix overreaching: the one verb that reads stdin given /dev/null, so a
    scripted `shell` runs nothing and says nothing."""
    path, calls = _stub_docker(tmp_path)
    shell, launcher = _launcher(tmp_path, which)
    run = _run([shell, launcher, "shell"], tmp_path, path, stdin)
    assert run.returncode == 0, run.stderr
    assert _compose(calls, which, "shell") == "read:" + PROBE.strip(), calls()


@pytest.mark.parametrize("stdin", STDINS)
@pytest.mark.parametrize("which", LAUNCHERS)
def test_the_command_after_a_verb_gets_the_input_left_to_it(tmp_path, which, stdin):
    """I-4's own shape: `<launcher> init; cat > out`, run by a shell whose stdin is a pipe holding a line (as `ssh
    <node> '…init; bash -s' < verify.sh` gives the remote shell) or a regular file (a local `bash -c '…' < file`,
    whose offset the launcher and `cat` share). The command after the launcher gets the line, and the installer's
    compose got nothing; the launcher's own exit code is the row's. Guards the redeploy whose verify step ran nothing
    because the installer had read the verify script."""
    path, calls = _stub_docker(tmp_path)
    shell, launcher = _launcher(tmp_path, which)
    out = tmp_path / "after.out"
    run = _run(["bash", "-c", '"$1" "$2" init; rc=$?; cat > "$3"; exit "$rc"', "_", shell, launcher, str(out)],
               tmp_path, path, stdin)
    assert run.returncode == 0, run.stderr
    assert _compose(calls, which, "init") == "read:", calls()
    assert out.read_text(encoding="utf-8") == PROBE


@pytest.mark.parametrize("which", LAUNCHERS)
@pytest.mark.parametrize("verb", ["init", "shell"])
def test_a_terminal_is_handed_over_as_a_terminal(tmp_path, which, verb):
    """With stdin a terminal (a pty), `init` and `shell` hand compose that terminal, not /dev/null. Guards the fix
    reaching an operator at a terminal: a launcher that redirected whatever stdin is would give `shell` typed at a
    terminal an empty input, so it would end at once, and would read the terminal test on the wrong descriptor
    (stdout, say) without a row to say so."""
    import pty  # here, not at the top: see the module docstring (Windows has no termios)

    path, calls = _stub_docker(tmp_path)
    shell, launcher = _launcher(tmp_path, which)
    leader, follower = pty.openpty()
    try:
        run = subprocess.run([shell, launcher, verb], stdin=follower, capture_output=True, text=True,
                             env=_env(tmp_path, path), cwd=str(tmp_path), timeout=60)
    finally:
        os.close(follower)
        os.close(leader)
    assert run.returncode == 0, run.stderr
    assert _compose(calls, which, verb) == "tty", calls()


@pytest.mark.parametrize("rel, verb", [("scripts/kontroll-installer.sh", '"${1:-init}"'),
                                       ("scripts/kontroll.sh", '"$verb"')])
def test_the_two_run_lines_are_one_command_but_for_the_redirect(rel, verb):
    """Each launcher runs compose `run` on two lines only: every line that is not a comment and runs it is counted
    (test_installer_phase1's pattern), so a third hand-over, `[ … ] && exec docker compose … run …`, fails the row.
    The two are the `exec docker compose … run` under `if [ <verb> != shell ] && [ ! -t 0 ]`, ending in `</dev/null`,
    and the plain one after it, one command but for that redirect. Guards a hand-over that skips the redirect, and the
    two lines drifting apart (a flag added to one, `--build` on one only), so a verb's behaviour would turn on whether
    stdin is a terminal, which no other row here holds for every flag; the redirect stands on the run line since the
    launchers hold no `exec` redirect (test_installer_phase1)."""
    lines = open(os.path.join(ROOT, rel), encoding="utf-8").read().splitlines()
    runs = [i for i, line in enumerate(lines)
            if not line.lstrip().startswith("#") and re.search(r"\bcompose\b.*\brun\b", line)]
    assert len(runs) == 2, [lines[i] for i in runs]
    first, last = runs
    assert lines[first - 1] == "if [ %s != shell ] && [ ! -t 0 ]; then" % verb, lines[first - 1]
    assert lines[first + 1] == "fi" and last == first + 2, lines[first - 1:last + 1]
    assert lines[last].startswith("exec docker compose "), lines[last]
    assert lines[first] == "  " + lines[last] + " </dev/null", (lines[first], lines[last])
