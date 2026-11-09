"""Every bind a service takes from the operator's home exists, as the operator's, before docker is first called.

WHY: Docker makes a missing bind source, and each missing parent, owned by root. Vector binds
`${KONTROLL_OPERATOR_HOME}/.local/state/kontroll/runs` read-only (the operator scripts' run-logs,
scripts/lib/run-log.sh), and nothing made it before `up`: on a node no such script had run on yet, the first `up`
with Vector left `~/.local`, `~/.local/state` and the rest root's, so every later script ran without its run-log and
the operator's other tools could not write under `~/.local` (I-23, 2026-11-08). tests/check-storage-chown.py holds
the storage root's binds to a provisioner and leaves the operator's home to the launchers; these rows hold the
launchers to every such bind, read from docker/services/*.yaml, so a new one is covered without a list to update.

The launcher rows copy each launcher into a tree under tmp_path with the few files it reads, put a stand-in `docker`
first on PATH that records which of the binds' directories exist when it is called, and run the launcher itself.
Nothing is pulled, built or deployed, and HOME is a scratch directory. The host deploy's own task for the same
directory is run by ansible-playbook in tests/integration/test_run_log_dir_task.py.
"""
import glob
import os
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OPERATOR_HOME = re.compile(r"^\$\{KONTROLL_OPERATOR_HOME(?::[-?][^}]*)?\}(/[^:]+):")

pytestmark = [pytest.mark.unit]

_POSIX = pytest.mark.skipif(os.name == "nt" or not all(shutil.which(t) for t in ("sh", "bash", "git")),
                            reason="runs the POSIX launchers with a stand-in docker on PATH (CI has sh, bash and git)")


def _home_binds():
    """{service file: [the path under the operator's home of each bind source it takes from there]}."""
    found = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "docker", "services", "*.yaml"))):
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for svc in (doc.get("services") or {}).values():
            for vol in svc.get("volumes") or []:
                m = _OPERATOR_HOME.match(str(vol)) if isinstance(vol, str) else None
                if m:
                    found.setdefault(os.path.basename(path), []).append(m.group(1))
    return found


def test_the_binds_from_the_operators_home_are_read_from_the_service_files():
    """The rows below are only as good as this read: it must find the installer's three binds (age, ssh, semaphore)
    and Vector's run-log bind, I-23's. Guards a parse that finds nothing (every launcher row would then pass on an
    empty list) and a change of the bind's spelling that hides it from the read."""
    binds = _home_binds()
    assert "/.local/state/kontroll/runs" in binds.get("vector.yaml", []), binds
    assert {"/.config/sops/age", "/.ssh", "/.config/semaphore"} <= set(binds.get("installer.yaml", [])), binds


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


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("launcher", [_launch_kit, _source_tree], ids=["launch-kit kontroll", "kontroll-installer.sh"])
def test_each_launcher_makes_every_operator_home_bind_before_docker_runs(tmp_path, launcher):
    """Run each launcher (`init`, with a stand-in docker) in an empty HOME: every directory a service binds from the
    operator's home, Vector's run-log directory among them, exists when docker is first called, owned by the user who
    ran the launcher, and the directories it had to make for the run-log one are 0755 or wider for others to read
    and enter (Vector reads it as uid 10002). Guards I-23: the launchers made age, ssh and semaphore only, so the
    first `up` with Vector had Docker make `~/.local/state/kontroll/runs` and its parents root's."""
    subs = sorted({sub for subs in _home_binds().values() for sub in subs})
    home = tmp_path / "home"
    home.mkdir()
    path, calls = _stub_docker(tmp_path, home, subs)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=path, HOME=str(home))
    old = os.umask(0o022)
    try:
        run = subprocess.run(launcher(tmp_path) + ["init"], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    finally:
        os.umask(old)
    assert run.returncode == 0, run.stderr
    first = calls()[0].split()
    assert first == ["yes"] * len(subs), dict(zip(subs, first))
    made = home
    for part in "/.local/state/kontroll/runs".strip("/").split("/"):
        made = made / part
        st = os.stat(str(made))
        assert st.st_uid == os.getuid() and st.st_mode & 0o005 == 0o005, (str(made), oct(st.st_mode), st.st_uid)
