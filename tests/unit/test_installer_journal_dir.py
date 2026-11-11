"""The host's journal directory — both launchers RUN with a stand-in docker that records what they hand it (I-25).

WHY: Vector binds the host's journal at the directory its journald source reads, and a bind to a /var/log/journal
the host lacks makes Docker create it (root:root 0755); journald's default `Storage=auto` then reads that directory as
"persist" from the next boot, so a deploy changed a host setting. deploy-stack binds /var/log/journal only where the
host has it, but in the installer container it cannot see the host's /var/log: the launchers, which run on the host,
read it there and export KONTROLL_HOST_JOURNAL_DIR, and docker/services/installer.yaml passes it in. A text pin can
say each launcher has the line; only running it says that the value reaching docker is this host's answer, and that a
value the operator's shell exported does not win.

These tests copy each launcher into a tree under tmp_path with the few files it reads, the path its one
`[ -d /var/log/journal ]` tests pointed at a stand-in under tmp_path in each shape /var/log/journal can take (a directory, a link to one, a file, a
link to one, a dangling link, nothing), put a stand-in `docker` first on PATH, and run the launcher itself, once with
the operator's shell exporting the wrong answer and once exporting none. The right answer is the persistent journal
for a directory or a link to one, else the volatile one. A row of the machine's own /var/log/journal tested only the
branch that machine takes (the I-25 reads' N4); the stand-in runs both branches on every machine, and a pin holds the
three scripts to the same lines, so the rewritten copy is the script that ships. Nothing is pulled, built or deployed,
and HOME is a scratch directory.
"""
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PERSISTENT, VOLATILE = "/var/log/journal", "/run/log/journal"
PROBE = "[ -d /var/log/journal ]"
PROBE_PATH = " /var/log/journal ]"   # the path the probe tests, as the rows rewrite it
SHAPES = ["dir", "link-to-dir", "file", "link-to-file", "dangling", "absent"]
VERBS = [None, "check", "shell"]

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(os.name == "nt" or not all(shutil.which(tool) for tool in ("sh", "bash", "git", "awk")),
                       reason="runs the POSIX launchers with a stand-in docker on PATH (CI has sh, bash, git and awk)"),
]


def _shape(tmp_path, shape):
    """A stand-in for /var/log/journal in `shape`; returns (its path, the answer a launcher must give for it): the
    persistent journal for a directory or a link to one, else the volatile one."""
    stand_in, target = tmp_path / "var-log-journal", tmp_path / "journal-target"
    if shape == "dir":
        stand_in.mkdir()
    elif shape == "link-to-dir":
        target.mkdir()
        stand_in.symlink_to(target)
    elif shape == "file":
        stand_in.write_text("", encoding="utf-8")
    elif shape == "link-to-file":
        target.write_text("", encoding="utf-8")
        stand_in.symlink_to(target)
    elif shape == "dangling":
        stand_in.symlink_to(target)
    return str(stand_in), (PERSISTENT if shape in ("dir", "link-to-dir") else VOLATILE)


def _copy_probing(src, dest, stand_in):
    """Copy the script at `src` to `dest` with the path its one probe tests pointed at `stand_in`; the test itself and
    the values it exports stay as they are, so a changed test (`-e` for `-d`, a link refused) meets every shape."""
    with open(src, encoding="utf-8", newline="") as fh:
        text = fh.read()
    assert text.count(PROBE_PATH) == 1, "%s: the probe's path changed; point the stand-in at the new one" % src
    with open(str(dest), "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(PROBE_PATH, " %s ]" % stand_in))


def _stub_docker(tmp_path):
    """A stand-in docker that appends `<KONTROLL_HOST_JOURNAL_DIR or unset>|<its arguments>` to a log. Beside it, an
    `id` whose `id -u` answers 1000 where the account running the suite has uid 1001 (a GitHub-hosted runner's), which
    both launchers refuse before they hand docker anything (I-18; tests/unit/test_operator_uid_1001_refused.py holds
    that), and the real answer otherwise; every other question goes to the real `id`. Returns (the PATH to run under,
    a function giving the recorded lines)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        '#!/bin/sh\nprintf "%%s|%%s\\n" "${KONTROLL_HOST_JOURNAL_DIR-unset}" "$*" >> "%s"\nexit 0\n'
        % str(log).replace("\\", "/"), encoding="utf-8", newline="\n")
    os.chmod(str(bin_dir / "docker"), 0o755)
    real_id = shutil.which("id")
    (bin_dir / "id").write_text(
        '#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = -u ]; then\n'
        '  u=$(\'%s\' -u); [ "$u" = 1001 ] && u=1000; echo "$u"; exit 0\nfi\n'
        'exec \'%s\' "$@"\n' % (real_id, real_id), encoding="utf-8", newline="\n")
    os.chmod(str(bin_dir / "id"), 0o755)
    return (str(bin_dir) + os.pathsep + os.environ["PATH"],
            lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def _envs(tmp_path, path, want):
    """The launcher's two environments, with no KONTROLL_* of the tests' own: the operator's shell exporting the
    wrong answer for this shape, which the launcher must not pass on, and exporting none."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=path, HOME=str(home))
    return [dict(env, KONTROLL_HOST_JOURNAL_DIR=VOLATILE if want == PERSISTENT else PERSISTENT), env]


def _launch_kit(tmp_path, stand_in):
    """A launch kit reduced to what scripts/kontroll.sh reads before it hands over to docker."""
    kit = tmp_path / "kit"
    (kit / "scripts" / "lib").mkdir(parents=True)
    (kit / "docker" / "services").mkdir(parents=True)
    _copy_probing(os.path.join(ROOT, "scripts", "kontroll.sh"), kit / "kontroll", stand_in)
    shutil.copy(os.path.join(ROOT, "scripts", "lib", "plaintext-guard.sh"), str(kit / "scripts" / "lib"))
    (kit / "docker" / "services" / "installer.yaml").write_text("services: {}\n", encoding="utf-8")
    (kit / "images.env").write_text("KONTROLL_INSTALLER_IMAGE=registry.example.test/installer@sha256:%s\n" % ("0" * 64),
                                    encoding="utf-8")
    (kit / "README.md").write_text("the tool\n", encoding="utf-8")
    return kit


def _source_tree(tmp_path, stand_in):
    """A source checkout reduced to scripts/kontroll-installer.sh (without the canonical-group library, which it
    sources only when present)."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    _copy_probing(os.path.join(ROOT, "scripts", "kontroll-installer.sh"), tree / "scripts" / "kontroll-installer.sh",
                  stand_in)
    return tree


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("verb", VERBS, ids=["no-verb", "check", "shell"])
def test_the_launch_kit_launcher_hands_docker_this_hosts_journal_dir(tmp_path, verb, shape):
    """scripts/kontroll.sh hands docker's `compose … run` KONTROLL_HOST_JOURNAL_DIR=/var/log/journal where that path is
    a directory or a link to one, and /run/log/journal where it is a file, a link to one, a dangling link or nothing,
    for a deploy (`init`, `check`) and any other verb alike, whether or not the operator's shell exported the other
    value. Guards the installer-container deploy binding a /var/log/journal the host lacks (Docker creates it;
    journald then persists from the next boot) because the launcher passed nothing and the deploy guessed, passed the
    shell's value on, or tested the path with `-e` (a file there) or without following a link; and the persistent
    journal missed where the host keeps one."""
    stand_in, want = _shape(tmp_path, shape)
    path, calls = _stub_docker(tmp_path)
    kit = _launch_kit(tmp_path, stand_in)
    for env in _envs(tmp_path, path, want):
        run = subprocess.run(["sh", str(kit / "kontroll")] + ([verb] if verb else []), capture_output=True,
                             text=True, env=env, cwd=str(tmp_path))
        assert run.returncode == 0, run.stderr
        last = calls()[-1]
        assert " run --rm init" in last and last.split("|", 1)[0] == want, calls()


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("verb", VERBS, ids=["no-verb", "check", "shell"])
def test_the_source_launcher_hands_docker_this_hosts_journal_dir(tmp_path, verb, shape):
    """scripts/kontroll-installer.sh hands docker's `compose … run` the same answer for each shape as the launch kit's
    launcher does, whether or not the operator's shell exported the other value. Guards the same failure: the
    in-container deploy, which cannot see the host's /var/log, binding a journal directory the host lacks, or missing
    the persistent journal the host keeps."""
    stand_in, want = _shape(tmp_path, shape)
    path, calls = _stub_docker(tmp_path)
    tree = _source_tree(tmp_path, stand_in)
    for env in _envs(tmp_path, path, want):
        run = subprocess.run(["bash", str(tree / "scripts" / "kontroll-installer.sh")] + ([verb] if verb else []),
                             capture_output=True, text=True, env=env, cwd=str(tmp_path))
        assert run.returncode == 0, run.stderr
        last = calls()[-1]
        assert " run --build --rm init" in last and last.split("|", 1)[0] == want, calls()


def test_the_launchers_and_the_fresh_node_check_read_the_journal_alike():
    """scripts/kontroll.sh, scripts/kontroll-installer.sh and tests/fresh-node-check.sh each hold the same three lines
    once (`[ -d /var/log/journal ]`, the two values, the export), and the probe's path once. Guards the shape rows,
    which run each script with that one path pointed at a stand-in: a script whose test or values drifted from the
    others, or a second probe the rewrite would miss, would leave the rows testing a script that is not the one that
    ships."""
    block = ("if [ -d /var/log/journal ]; then KONTROLL_HOST_JOURNAL_DIR=/var/log/journal\n"
             "else KONTROLL_HOST_JOURNAL_DIR=/run/log/journal; fi\nexport KONTROLL_HOST_JOURNAL_DIR\n")
    for rel in ("scripts/kontroll.sh", "scripts/kontroll-installer.sh", "tests/fresh-node-check.sh"):
        with open(os.path.join(ROOT, rel), encoding="utf-8", newline="") as fh:
            text = fh.read()
        assert text.count(block) == 1 and text.count(PROBE_PATH) == 1, rel


def test_the_installer_passes_the_hosts_journal_dir_in():
    """docker/services/installer.yaml's `init` service passes KONTROLL_HOST_JOURNAL_DIR into the container, empty
    when the launcher was bypassed (deploy-stack then says NOT KNOWN and binds the volatile journal). Guards the
    launcher's answer stopping at compose's interpolation: exported on the host but absent in the container, where
    deploy-stack reads it, so every installer deploy fell back to the volatile journal on a persistent host."""
    with open(os.path.join(ROOT, "docker", "services", "installer.yaml"), encoding="utf-8") as fh:
        env = yaml.safe_load(fh)["services"]["init"]["environment"]
    assert env.get("KONTROLL_HOST_JOURNAL_DIR") == "${KONTROLL_HOST_JOURNAL_DIR:-}", env
