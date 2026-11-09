"""The host's journal directory — both launchers RUN with a stand-in docker that records what they hand it (I-25).

WHY: Vector binds the host's journal at the directory its journald source reads, and a bind to a /var/log/journal
the host lacks makes Docker create it (root:root 0755); journald's default `Storage=auto` then reads that directory as
"persist" from its next start, so a deploy changed a host setting. deploy-stack binds /var/log/journal only where the
host has it, but in the installer container it cannot see the host's /var/log: the launchers, which run on the host,
read it there and export KONTROLL_HOST_JOURNAL_DIR, and docker/services/installer.yaml passes it in. A text pin can
say each launcher has the line; only running it says that the value reaching docker is this host's answer, and that a
value the operator's shell exported does not win.

These tests copy each launcher into a tree under tmp_path with the few files it reads, put a stand-in `docker` first
on PATH, and run the launcher itself, once with the operator's shell exporting the wrong answer and once exporting
none. The right answer is the host's own: /var/log/journal when this host has that directory, else
/run/log/journal, as the test reads it here. Nothing is pulled, built or deployed, and HOME is a scratch directory.
"""
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PERSISTENT, VOLATILE = "/var/log/journal", "/run/log/journal"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(os.name == "nt" or not all(shutil.which(tool) for tool in ("sh", "bash", "git", "awk")),
                       reason="runs the POSIX launchers with a stand-in docker on PATH (CI has sh, bash, git and awk)"),
]


def _truth():
    return PERSISTENT if os.path.isdir(PERSISTENT) else VOLATILE


def _stub_docker(tmp_path):
    """A stand-in docker that appends `<KONTROLL_HOST_JOURNAL_DIR or unset>|<its arguments>` to a log. Returns (the
    PATH to run under, a function giving the recorded lines)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        '#!/bin/sh\nprintf "%%s|%%s\\n" "${KONTROLL_HOST_JOURNAL_DIR-unset}" "$*" >> "%s"\nexit 0\n'
        % str(log).replace("\\", "/"), encoding="utf-8", newline="\n")
    os.chmod(str(bin_dir / "docker"), 0o755)
    return (str(bin_dir) + os.pathsep + os.environ["PATH"],
            lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def _envs(tmp_path, path):
    """The launcher's two environments, with no KONTROLL_* of the tests' own: the operator's shell exporting the
    wrong answer for this host, which the launcher must not pass on, and exporting none."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=path, HOME=str(home))
    wrong = VOLATILE if _truth() == PERSISTENT else PERSISTENT
    return [dict(env, KONTROLL_HOST_JOURNAL_DIR=wrong), env]


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
    return kit


def _source_tree(tmp_path):
    """A source checkout reduced to scripts/kontroll-installer.sh (without the canonical-group library, which it
    sources only when present)."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll-installer.sh"), str(tree / "scripts"))
    return tree


@pytest.mark.parametrize("verb", [None, "check", "shell"], ids=["no-verb", "check", "shell"])
def test_the_launch_kit_launcher_hands_docker_this_hosts_journal_dir(tmp_path, verb):
    """scripts/kontroll.sh hands docker's `compose … run` KONTROLL_HOST_JOURNAL_DIR=/var/log/journal on a host that
    has that directory and /run/log/journal on one that does not, for a deploy (`init`, `check`) and any other verb
    alike, whether or not the operator's shell exported the other value. Guards the installer-container deploy binding
    a /var/log/journal the host lacks (Docker creates it; journald then persists from its next start) because the
    launcher passed nothing and the deploy guessed, or passed the shell's value on."""
    path, calls = _stub_docker(tmp_path)
    kit = _launch_kit(tmp_path)
    for env in _envs(tmp_path, path):
        run = subprocess.run(["sh", str(kit / "kontroll")] + ([verb] if verb else []), capture_output=True,
                             text=True, env=env, cwd=str(tmp_path))
        assert run.returncode == 0, run.stderr
        last = calls()[-1]
        assert " run --rm init" in last and last.split("|", 1)[0] == _truth(), calls()


@pytest.mark.parametrize("verb", [None, "check", "shell"], ids=["no-verb", "check", "shell"])
def test_the_source_launcher_hands_docker_this_hosts_journal_dir(tmp_path, verb):
    """scripts/kontroll-installer.sh hands docker's `compose … run` this host's journal directory, as the launch
    kit's launcher does, whether or not the operator's shell exported the other value. Guards the same failure: the
    in-container deploy, which cannot see the host's /var/log, binding a journal directory the host lacks."""
    path, calls = _stub_docker(tmp_path)
    tree = _source_tree(tmp_path)
    for env in _envs(tmp_path, path):
        run = subprocess.run(["bash", str(tree / "scripts" / "kontroll-installer.sh")] + ([verb] if verb else []),
                             capture_output=True, text=True, env=env, cwd=str(tmp_path))
        assert run.returncode == 0, run.stderr
        last = calls()[-1]
        assert " run --build --rm init" in last and last.split("|", 1)[0] == _truth(), calls()


def test_the_installer_passes_the_hosts_journal_dir_in():
    """docker/services/installer.yaml's `init` service passes KONTROLL_HOST_JOURNAL_DIR into the container, empty
    when the launcher was bypassed (deploy-stack then says NOT KNOWN and binds the volatile journal). Guards the
    launcher's answer stopping at compose's interpolation: exported on the host but absent in the container, where
    deploy-stack reads it, so every installer deploy fell back to the volatile journal on a persistent host."""
    with open(os.path.join(ROOT, "docker", "services", "installer.yaml"), encoding="utf-8") as fh:
        env = yaml.safe_load(fh)["services"]["init"]["environment"]
    assert env.get("KONTROLL_HOST_JOURNAL_DIR") == "${KONTROLL_HOST_JOURNAL_DIR:-}", env
