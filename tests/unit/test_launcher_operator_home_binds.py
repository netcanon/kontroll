"""Every bind a service takes from the operator's home exists, as the operator's, before docker is first called.

WHY: Docker makes a missing bind source, and each missing parent, owned by root. Vector binds
`${KONTROLL_OPERATOR_HOME}/.local/state/kontroll/runs` read-only (the operator scripts' run-logs,
scripts/lib/run-log.sh), and nothing made it before `up`: on a node no such script had run on yet, the first `up`
with Vector left `~/.local`, `~/.local/state` and the rest root's, so every later script ran without its run-log and
the operator's other tools could not write under `~/.local` (I-23, 2026-11-08). tests/check-storage-chown.py holds
the storage root's binds to a provisioner and leaves the operator's home to the launchers; these rows hold the
launchers to every such bind, read from docker/services/*.yaml: the short form in each spelling compose takes for the
variable, and the long form's `source`. A bind that names the home another way (`~`, `$HOME`) is refused, since the
read would miss it.

The launcher rows copy each launcher into a tree under tmp_path with the few files it reads, put a stand-in `docker`
first on PATH that records which of the binds' directories exist when it is called, and run the launcher itself, for
each verb that reaches docker. Nothing is pulled, built or deployed, and HOME is a scratch directory. The host
deploy's own task for the same directory is run by ansible-playbook in tests/integration/test_run_log_dir_task.py;
its place before the play's `up` is held here.
"""
import glob
import os
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ${VAR}, ${VAR:-x}, ${VAR-x}, ${VAR:?x}, ${VAR?x} and $VAR, then the path under it.
_OPERATOR_HOME = re.compile(r"^(?:\$\{KONTROLL_OPERATOR_HOME(?::?[-?][^}]*)?\}|"
                            r"\$KONTROLL_OPERATOR_HOME(?![A-Za-z0-9_]))"
                            r"(/[^:]+)(?::|$)")
_OTHER_HOME = re.compile(r"^(?:~|\$\{?HOME(?![A-Za-z0-9_]))")

# The launchers refuse uid 1001, a GitHub-hosted runner's own (I-18): a stand-in `id` keeps it from deciding these.
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("not_the_service_uid")]

_POSIX = pytest.mark.skipif(os.name == "nt" or not all(shutil.which(t) for t in ("sh", "bash", "git")),
                            reason="runs the POSIX launchers with a stand-in docker on PATH (CI has sh, bash and git)")


def _sources():
    """[(service file, the bind's source as written)] for every volume of every service: a short form's whole
    string (its source comes first) and a long form's `source`."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "docker", "services", "*.yaml"))):
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for svc in (doc.get("services") or {}).values():
            for vol in svc.get("volumes") or []:
                src = vol.get("source") if isinstance(vol, dict) else vol
                if isinstance(src, str):
                    out.append((os.path.basename(path), src))
    return out


def _home_binds():
    """{service file: [the path under the operator's home of each bind source it takes from there]}."""
    found = {}
    for name, src in _sources():
        m = _OPERATOR_HOME.match(src)
        if m:
            found.setdefault(name, []).append(m.group(1))
    return found


def test_the_binds_from_the_operators_home_are_read_from_the_service_files():
    """The rows below are only as good as this read: it must find the installer's three binds (age, ssh, semaphore)
    and Vector's run-log bind, I-23's. Guards a parse that finds nothing (every launcher row would then pass on an
    empty list) and a change of the bind's spelling that hides it from the read."""
    binds = _home_binds()
    assert "/.local/state/kontroll/runs" in binds.get("vector.yaml", []), binds
    assert {"/.config/sops/age", "/.ssh", "/.config/semaphore"} <= set(binds.get("installer.yaml", [])), binds


@pytest.mark.parametrize("src,path", [
    ("${KONTROLL_OPERATOR_HOME}/a:/x", "/a"), ("${KONTROLL_OPERATOR_HOME:-/home/admin}/a/b:/x:ro", "/a/b"),
    ("${KONTROLL_OPERATOR_HOME-/home/admin}/a:/x", "/a"), ("${KONTROLL_OPERATOR_HOME:?set it}/a:/x", "/a"),
    ("${KONTROLL_OPERATOR_HOME?set it}/a:/x", "/a"), ("$KONTROLL_OPERATOR_HOME/a:/x", "/a"),
    ("${KONTROLL_OPERATOR_HOME}/a", "/a"), ("$KONTROLL_OPERATOR_HOMEX/a:/x", None), ("/srv/a:/x", None)],
    ids=["braced", "default", "posix-default", "required", "posix-required", "unbraced", "long-form-source",
         "another-variable", "a-host-path"])
def test_the_read_takes_each_spelling_of_the_operators_home(src, path):
    """Each spelling compose takes for the variable, `${…}` with or without `:-`, `-`, `:?` or `?`, and `$…`, gives
    the path under the home, a long form's `source` (no `:target` after it) among them; another variable or a host
    path gives none. Guards a new bind written in a spelling the read does not know: no launcher row would then
    hold it, and the first `up` would have Docker make it root's (I-23's failure, for that bind)."""
    m = _OPERATOR_HOME.match(src)
    assert (m.group(1) if m else None) == path, src


def test_no_service_binds_the_operators_home_by_another_name():
    """No bind source starts with `~`, `$HOME` or `${HOME`: the operator's home is named by KONTROLL_OPERATOR_HOME
    only, which the read above takes. Guards a bind the read would miss (and the launchers so not make), and one
    that compose resolves against the shell running compose, which in the installer is root's."""
    other = [(name, src) for name, src in _sources() if _OTHER_HOME.match(src)]
    assert not other, other


def _stub_docker(tmp_path, home, subs):
    """A stand-in docker that writes down, each time it is called, which of `subs` exist under `home` as directories
    (`yes` or `no`, one line per call). Returns (the PATH to run under, a function giving the recorded lines)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    checks = " ".join('$( [ -d "%s%s" ] && echo yes || echo no )' % (home, sub) for sub in subs)
    (bin_dir / "docker").write_text("#!/bin/sh\necho %s >> '%s'\nexit 0\n" % (checks, log),
                                    encoding="utf-8", newline="\n")
    os.chmod(str(bin_dir / "docker"), 0o755)
    return (str(bin_dir) + os.pathsep + os.environ["PATH"],
            lambda: log.read_text(encoding="utf-8").split("\n")[:-1] if log.exists() else [])


def _launch_kit(tmp_path):
    """A launch kit reduced to what scripts/kontroll.sh reads before it hands over to docker."""
    kit = tmp_path / "kit"
    (kit / "scripts" / "lib").mkdir(parents=True)
    (kit / "docker" / "services").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll.sh"), str(kit / "kontroll"))
    shutil.copy(os.path.join(ROOT, "scripts", "lib", "plaintext-guard.sh"), str(kit / "scripts" / "lib"))
    (kit / "docker" / "services" / "installer.yaml").write_text("services: {}\n", encoding="utf-8")
    (kit / "images.env").write_text("KONTROLL_INSTALLER_IMAGE=registry.example.test/installer@sha256:%s\n" % ("0" * 64),
                                    encoding="utf-8")
    (kit / "README.md").write_text("the tool\n", encoding="utf-8")
    return ["sh", str(kit / "kontroll")]


def _source_tree(tmp_path):
    """A source checkout reduced to scripts/kontroll-installer.sh (without the canonical-group library, which it
    sources only when present)."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll-installer.sh"), str(tree / "scripts"))
    return ["bash", str(tree / "scripts" / "kontroll-installer.sh")]


VERBS = ["init", "check", "fresh-init", "shell", "playbooks/deploy-stack.yml"]


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("launcher", [_launch_kit, _source_tree], ids=["launch-kit kontroll", "kontroll-installer.sh"])
def test_each_launcher_makes_every_operator_home_bind_before_docker_runs(tmp_path, launcher, verb):
    """Run each launcher (each verb that reaches docker, with a stand-in docker) in an empty HOME: every directory a
    service binds from the operator's home, Vector's run-log directory among them, exists when docker is first
    called, each the user's who ran the launcher, and the run-log chain is o+rx (at umask 022), which `runs` needs for
    Vector (uid 10002) to read it through the bind. Guards I-23: the launchers made age, ssh and semaphore only, so
    the first `up` with Vector had Docker make `~/.local/state/kontroll/runs` and its parents root's; and a launcher
    that makes it for one verb only, when `check` and a playbook deploy as well."""
    subs = sorted({sub for subs in _home_binds().values() for sub in subs})
    home = tmp_path / "home"
    home.mkdir()
    path, calls = _stub_docker(tmp_path, home, subs)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=path, HOME=str(home))
    old = os.umask(0o022)
    try:
        run = subprocess.run(launcher(tmp_path) + [verb], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    finally:
        os.umask(old)
    assert run.returncode == 0, run.stderr
    first = calls()[0].split()
    assert first == ["yes"] * len(subs), dict(zip(subs, first))
    for sub in subs:
        assert os.stat(str(home) + sub).st_uid == os.getuid(), sub
    made = home
    for part in "/.local/state/kontroll/runs".strip("/").split("/"):
        made = made / part
        st = os.stat(str(made))
        assert st.st_uid == os.getuid() and st.st_mode & 0o005 == 0o005, (str(made), oct(st.st_mode), st.st_uid)


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("verb", ["init", "shell"])
@pytest.mark.parametrize("launcher", [_launch_kit, _source_tree], ids=["launch-kit kontroll", "kontroll-installer.sh"])
def test_a_run_log_dir_the_launcher_cannot_make_stops_no_verb(tmp_path, launcher, verb):
    """With `~/.local` a file (a directory above `runs` the launcher cannot make it under), each launcher says it
    cannot make `~/.local/state/kontroll/runs`, names the troubleshooting section, and goes on to docker, with the
    installer's own binds made. Guards the run-log mkdir on the launchers' fatal line: under `set -e` it stopped
    every verb, `shell` (the escape hatch) among them, where run-log.sh itself only turns the run-logs off."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local").write_text("", encoding="utf-8")
    path, calls = _stub_docker(tmp_path, home, ["/.ssh"])
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=path, HOME=str(home))
    run = subprocess.run(launcher(tmp_path) + [verb], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr
    assert "cannot make ~/.local/state/kontroll/runs" in run.stderr and "troubleshooting.md" in run.stderr, run.stderr
    assert calls() and calls()[0].split() == ["yes"], calls()


def _flat(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _flat(task.get(key))


def test_the_deploys_run_log_task_comes_before_the_up():
    """In deploy-stack.yml's stack play, the task that makes the run-log directory comes before "Bring up the
    selected stack service(s)", blocks included. Guards the task moved after `up`: a fresh host node's first `up`
    would have Docker make all four directories root's, and the task then gives back `runs` only, leaving
    `~/.local` root's (the half of I-23 that breaks the operator's other tools)."""
    with open(os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8") as fh:
        plays = yaml.safe_load(fh.read())
    play = [p for p in plays if str(p.get("name", "")).startswith("Render docker/.env from SOPS and bring up")]
    assert len(play) == 1, [p.get("name") for p in plays]
    names = [str(t.get("name", "")) for t in _flat(play[0].get("tasks"))]
    make = [i for i, n in enumerate(names) if n.startswith("Ensure the operator scripts' run-log dir exists")]
    up = [i for i, n in enumerate(names) if n == "Bring up the selected stack service(s)"]
    assert len(make) == 1 and len(up) == 1 and make[0] < up[0], (make, up)
