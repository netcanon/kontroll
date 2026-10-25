"""The installer's log driver per verb — both launchers RUN with a stand-in docker that records what they set.

WHY: docker/services/installer.yaml takes its log driver from KONTROLL_INSTALLER_LOG_DRIVER, `none` when unset. A
deploy (`init`, `check`) keeps a log: it runs no kontroll-init and prints no secret value, and that log is the
deploy's only record after the fact (`docker logs`, the `services-init` stream in Loki). Every other verb keeps none:
`fresh-init` prints the bootstrap passwords and a minted break-glass key once, and `shell` is where kontroll-init is
run by hand. A text pin can say each launcher has the line; only running it says which value reaches docker for
which verb, and that a value the operator's shell exported does not win.

These tests copy each launcher into a tree under tmp_path with the few files it reads, put a stand-in `docker` first
on PATH, and run the launcher itself. Nothing is pulled, built or deployed, and HOME is a scratch directory.
"""
import os
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

# (the verb as typed, the driver the launcher must choose): no verb is `init`.
VERBS = [(None, "json-file"), ("init", "json-file"), ("check", "json-file"), ("fresh-init", "none"),
         ("shell", "none"), ("configure-semaphore", "none"), ("playbooks/local-canonical.yml", "none")]


def _stub_docker(tmp_path):
    """A stand-in docker that appends `<KONTROLL_INSTALLER_LOG_DRIVER or unset>|<its arguments>` to a log. Returns
    (the PATH to run under, a function giving the recorded lines)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        '#!/bin/sh\nprintf "%%s|%%s\\n" "${KONTROLL_INSTALLER_LOG_DRIVER-unset}" "$*" >> "%s"\nexit 0\n'
        % str(log).replace("\\", "/"), encoding="utf-8", newline="\n")
    os.chmod(str(bin_dir / "docker"), 0o755)
    return (str(bin_dir) + os.pathsep + os.environ["PATH"],
            lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def _envs(tmp_path, path):
    """The launcher's two environments, with no KONTROLL_* of the tests' own: the operator's shell exporting a
    json-file driver, which no verb may inherit, and exporting none, so the launcher's own value must be exported."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=path, HOME=str(home))
    return [dict(env, KONTROLL_INSTALLER_LOG_DRIVER="json-file"), env]


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


@pytest.mark.parametrize("verb,driver", VERBS, ids=[v or "no-verb" for v, _ in VERBS])
def test_the_launch_kit_launcher_keeps_a_log_for_a_deploy_only(tmp_path, verb, driver):
    """scripts/kontroll.sh hands docker's `compose … run` KONTROLL_INSTALLER_LOG_DRIVER=json-file for `init` (also
    when no verb is given) and `check`, and `none` for `fresh-init`, `shell`, `configure-semaphore` and a playbook
    passthrough — whether or not the operator's shell exported json-file. Guards a show-once value reaching a Docker
    log (and Loki, through Vector) because the launcher left the choice to the shell, or chose it for every verb
    alike; and a deploy losing its only record, as when every verb ran with no log or the value was never exported."""
    path, calls = _stub_docker(tmp_path)
    kit = _launch_kit(tmp_path)
    for env in _envs(tmp_path, path):
        run = subprocess.run(["sh", str(kit / "kontroll")] + ([verb] if verb else []), capture_output=True,
                             text=True, env=env, cwd=str(tmp_path))
        assert run.returncode == 0, run.stderr
        last = calls()[-1]
        assert " run --rm init " in last + " " and last.split("|", 1)[0] == driver, calls()


@pytest.mark.parametrize("verb,driver", VERBS, ids=[v or "no-verb" for v, _ in VERBS])
def test_the_source_launcher_keeps_a_log_for_a_deploy_only(tmp_path, verb, driver):
    """scripts/kontroll-installer.sh hands docker's `compose … run` KONTROLL_INSTALLER_LOG_DRIVER=json-file for
    `init` (also when no verb is given) and `check`, and `none` for every other verb, whether or not the operator's
    shell exported json-file. Guards the same two failures as the launch kit's launcher: a show-once value kept in a
    Docker log, and a deploy left with no record."""
    path, calls = _stub_docker(tmp_path)
    tree = _source_tree(tmp_path)
    for env in _envs(tmp_path, path):
        run = subprocess.run(["bash", str(tree / "scripts" / "kontroll-installer.sh")] + ([verb] if verb else []),
                             capture_output=True, text=True, env=env, cwd=str(tmp_path))
        assert run.returncode == 0, run.stderr
        last = calls()[-1]
        assert " run --build --rm init" in last and last.split("|", 1)[0] == driver, calls()
