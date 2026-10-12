"""scripts/kontroll.sh — the launch-kit launcher's capture of its tree, RUN with the real git.

WHY: on a launch-kit box the launcher's capture is the FIRST commit of a freshly scaffolded instance/ (the deploy's
own commit then finds a clean tree and never runs), so it carries the plaintext gate. A text pin can say that the
guard's call stands in front of the `git add -A`; it cannot say what the pair DOES. Two things it did, found by
running it (review 2026-10-12, report 38): a kit extracted inside another git repository — a home directory kept
under git — created no repository of its own, so the "capture" staged and committed the OUTER repository, and the
gate, which lists below the directory it is given, never saw those files; and a refusal locked out every verb,
including `shell`, the break-glass verb and the only way to the image's sops on a host that has none.

These tests build a minimal kit under tmp_path (the launcher, the guard, the two files the launcher looks for),
put a stand-in `docker` first on PATH (the launcher needs one to exist, and ends by handing over to it), and run
the launcher itself. Nothing is pulled, nothing is deployed, and HOME is a scratch directory.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(os.name == "nt" or not all(shutil.which(tool) for tool in ("sh", "git", "awk")),
                       reason="runs the POSIX launcher with a stand-in docker on PATH (CI has sh, git and awk)"),
]

SECRET = "hunter2-plaintext-value"


def _git(tree, *args):
    return subprocess.run(["git", "-C", str(tree)] + list(args), check=True, capture_output=True, text=True).stdout


def _kit(where):
    """A launch kit reduced to what the launcher reads before it hands over to docker."""
    kit = where / "kit"
    (kit / "scripts" / "lib").mkdir(parents=True)
    (kit / "docker" / "services").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll.sh"), str(kit / "kontroll"))
    shutil.copy(os.path.join(ROOT, "scripts", "lib", "plaintext-guard.sh"), str(kit / "scripts" / "lib"))
    (kit / "docker" / "services" / "installer.yaml").write_text("services: {}\n", encoding="utf-8")
    (kit / "images.env").write_text("KONTROLL_INSTALLER_IMAGE=registry.example.test/installer@sha256:%s\n" % ("0" * 64),
                                    encoding="utf-8")
    (kit / "README.md").write_text("the tool\n", encoding="utf-8")
    return kit


@pytest.fixture
def launch(tmp_path, isolated_git):
    """Run `<kit>/kontroll <verb>` with a stand-in docker that records how it was called. Returns
    (run, calls): `run(kit, verb)` gives the CompletedProcess, `calls()` the recorded docker command lines. The
    LAUNCHER's environment — not the tests' own git — carries what an operator's shell may export and the launcher
    must ignore: a `GIT_DIR`, and an object directory and a common directory that are somebody else's."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    stub = bin_dir / "docker"
    stub.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexit 0\n' % str(log).replace("\\", "/"), encoding="utf-8",
                    newline="\n")
    os.chmod(str(stub), 0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    env.update(PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], HOME=str(home))
    env["GIT_DIR"] = str(tmp_path / "somebody-elses.git")     # exported in the operator's shell: must not be used
    env["GIT_OBJECT_DIRECTORY"] = str(tmp_path / "somebody-elses-objects")
    env["GIT_COMMON_DIR"] = str(tmp_path / "somebody-elses-common")

    def run(kit, verb):
        return subprocess.run(["sh", str(kit / "kontroll"), verb], capture_output=True, text=True, env=env,
                              cwd=str(tmp_path))

    def calls():
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    return run, calls


def test_a_plaintext_secret_stops_the_capture_and_nothing_is_committed(tmp_path, launch):
    """A clean kit is captured on the first run. With a plaintext `instance/secrets/network.sops.yml` in the tree,
    the next run exits 1 BEFORE it reaches docker, names the file and where the fixes are, never prints the value,
    and commits nothing: the capture's HEAD is unchanged and the file is not tracked. Guards the published
    install path being the ungated one — and a refusal that prints a remedy the host cannot follow (the launch-kit
    host has no sops; the first version of the message told the operator to run it)."""
    run, calls = launch
    kit = _kit(tmp_path)
    first = run(kit, "check")
    assert first.returncode == 0, first.stderr
    head = _git(kit, "rev-parse", "HEAD")
    assert "README.md" in _git(kit, "ls-files") and len(calls()) >= 1
    assert _git(kit, "cat-file", "-t", "HEAD").strip() == "commit", \
        "the capture's objects are not in the kit's own repository (a GIT_OBJECT_DIRECTORY from the shell was used)"

    secrets = kit / "instance" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "network.sops.yml").write_text("password: %s\n" % SECRET, encoding="utf-8")
    before = len(calls())
    refused = run(kit, "check")
    assert refused.returncode == 1
    assert "plaintext-guard: REFUSED instance/secrets/network.sops.yml" in refused.stderr
    assert "docs/install-from-scratch.md" in refused.stderr and "./kontroll shell" in refused.stderr
    assert "sops -e -i" not in refused.stderr, "the host has no sops: the remedy is the document, or the shell verb"
    assert SECRET not in refused.stderr + refused.stdout
    assert len(calls()) == before, "a refused capture must not go on to docker"
    assert _git(kit, "rev-parse", "HEAD") == head
    assert "network.sops.yml" not in _git(kit, "ls-files")


def test_the_shell_verb_is_not_locked_out_by_a_refusal(tmp_path, launch):
    """With a plaintext secrets file in the tree, `kontroll shell` still runs — it reaches docker with the `shell`
    verb — and captures nothing: no commit is made and the file stays untracked. Guards the one remedy a
    launch-kit host has for an unencrypted file (the installer image's sops, reached through this verb) being
    blocked by the refusal it is the remedy for."""
    run, calls = launch
    kit = _kit(tmp_path)
    assert run(kit, "check").returncode == 0
    head = _git(kit, "rev-parse", "HEAD")
    secrets = kit / "instance" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "network.sops.yml").write_text("password: %s\n" % SECRET, encoding="utf-8")
    shell = run(kit, "shell")
    assert shell.returncode == 0, shell.stderr
    assert calls()[-1].endswith("run --rm init shell")
    assert _git(kit, "rev-parse", "HEAD") == head
    assert "network.sops.yml" not in _git(kit, "ls-files")
    assert run(kit, "check").returncode == 1, "the gate is still there for every verb that deploys"


def test_a_kit_inside_another_repository_captures_only_itself(tmp_path, launch):
    """A kit extracted inside ANOTHER git repository gets a repository of its own on the first run, its capture
    holds the kit's files and nothing else, and the outer repository is left exactly as it was — nothing staged,
    no new commit — although an untracked plaintext secrets file and a key lie elsewhere in it. Guards the
    capture's `git add -A` running in somebody's home-directory repository: it staged and committed that whole
    repository, the canonical was then seeded from it, and the plaintext gate — which lists below the directory it
    is given — passed files it had never read."""
    run, _ = launch
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    for key, value in (("user.email", "t@example.test"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(outer, "config", key, value)
    (outer / "notes.txt").write_text("mine\n", encoding="utf-8")
    _git(outer, "add", "notes.txt")
    _git(outer, "commit", "-q", "-m", "the outer repository")
    outer_head = _git(outer, "rev-parse", "HEAD")
    stray = outer / "elsewhere" / "instance" / "secrets"
    stray.mkdir(parents=True)
    (stray / "network.sops.yml").write_text("password: %s\n" % SECRET, encoding="utf-8")
    key = "-".join(("AGE", "SECRET", "KEY", "1" + "".join("Q" for _ in range(58))))     # assembled here, not a constant
    (outer / "elsewhere" / "breakglass.txt").write_text(key + "\n", encoding="utf-8")

    kit = _kit(outer)
    result = run(kit, "check")
    assert result.returncode == 0, result.stderr
    assert (kit / ".git").exists(), "the kit must have a repository of its own"
    captured = _git(kit, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "kontroll" in captured and "README.md" in captured
    assert not any("elsewhere" in name or name == "notes.txt" for name in captured)
    assert _git(outer, "rev-parse", "HEAD") == outer_head
    assert _git(outer, "diff", "--cached", "--name-only") == ""
