"""An operator whose uid is 1001 is refused by install-prereqs.sh and by each launcher, before anything is made.

WHY (I-18, 2026-11-07): the canonical's grant and hook tell the network service from everyone else by one number,
uid 1001, so the deploy's first task (local-canonical.yml) refuses an operator who has it. That refusal came at the
quickstart's third line, `check`, after `fresh-init` had scaffolded instance/ and minted the control key, and nothing
before it said so; uid 1001 is an ordinary uid (a host's second account, a GitHub-hosted runner's own). The scripts
now say it first, in the same words: install-prereqs.sh before its root check (run as root, the operator is the
account that ran sudo; not root, the account running it), and each launcher for every verb, before it makes anything
(its operator is the account running it, whoever ran sudo); `shell`, the escape hatch, is told and goes on.
bootstrap.sh, run as root, hands install-prereqs.sh the outer sudo's operator.

Each row puts stand-ins first on PATH. An `id` whose `id -u` answers what the row needs (every other question goes to
the real `id`), so the account running the suite decides nothing. A docker, a sudo, a groupadd and a usermod that each
write their call down and run nothing, beside a `getent` that knows no gid-1001 group, and KONTROLL_STORAGE_ROOT a
missing directory: so in every launcher row that goes on, the two steps after the check that can change the system
rather than the home reach their sudo: the canonical-group step (scripts/lib/canonical-group.sh) its `sudo -n
groupadd`, the storage-root step (scripts/lib/storage-root.sh, I-35) its `sudo -n sh -c`; a refusal that came after
either would show. The launchers run in an empty HOME, and a refusal leaves HOME and the launcher's own tree (the kit,
the checkout) as they began.
install-prereqs.sh runs beside a stand-in lib/ whose libraries stop the script the moment one is
sourced: a run that gets past both checks installs nothing, even as root in a container.
"""
import os
import re
import shutil
import subprocess

import pytest

pytestmark = [pytest.mark.unit]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREREQS = os.path.join(ROOT, "scripts", "install-prereqs.sh")
CANONICAL_GROUP = os.path.join(ROOT, "scripts", "lib", "canonical-group.sh")
SENTENCE = "has uid 1001, the uid the network service runs as"
GROUP_STEP = "sudo -n groupadd -g 1001 kontroll"
STORAGE_STEP = 'sudo -n sh -c . "$1" && storage_root_ensure "$2" sh '   # then the helper's path and the root
_POSIX = pytest.mark.skipif(os.name == "nt" or not all(shutil.which(t) for t in ("sh", "bash", "git", "id")),
                            reason="runs the POSIX scripts with a stand-in id and docker on PATH (CI has them)")


def _bin(tmp_path, uid):
    """A directory first on PATH holding the stand-ins: an `id` whose `id -u` prints `uid` and which hands every other
    question to the real `id`; a docker, sudo, groupadd and usermod that each write `<name> <arguments>` down, one line
    per call, and exit 0 having run nothing; and a `getent` that answers no group at gid 1001 (every other question
    goes to the real one, where there is one). Returns (the PATH to run under, a function giving the calls)."""
    real_id, real_getent = shutil.which("id"), shutil.which("getent")
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "calls.log"
    stand = {
        "id": '#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = -u ]; then echo %s; exit 0; fi\nexec \'%s\' "$@"\n'
              % (uid, real_id),
        "getent": '#!/bin/sh\nif [ "$1" = group ] && [ "$2" = 1001 ]; then exit 2; fi\n'
                  + ("exec '%s' \"$@\"\n" % real_getent if real_getent else "exit 2\n"),
    }
    for name in ("docker", "sudo", "groupadd", "usermod"):
        stand[name] = "#!/bin/sh\necho \"%s $*\" >> '%s'\nexit 0\n" % (name, log)
    for name, text in stand.items():
        (b / name).write_text(text, encoding="utf-8", newline="\n")
        os.chmod(str(b / name), 0o755)
    return (str(b) + os.pathsep + os.environ["PATH"],
            lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def _env(path, **extra):
    """The suite's environment with PATH set, and none of its KONTROLL_* or SUDO_* (a suite run under sudo would
    otherwise name its operator), then `extra`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KONTROLL_", "SUDO_"))}
    env.update(PATH=path, **extra)
    return env


def _paths(top):
    """Every path under `top`, relative, but the stand-ins' (bin/, calls.log) and HOME's, which the rows read apart."""
    out = set()
    for d, dirs, files in os.walk(str(top)):
        rel = os.path.relpath(d, str(top))
        if rel == ".":
            dirs[:] = [x for x in dirs if x not in ("bin", "home")]
            files = [f for f in files if f != "calls.log"]
        out.update(os.path.normpath(os.path.join(rel, n)) for n in dirs + files)
    return out


def _real_name():
    """The account running the suite, as the real `id -un` names it (the stand-in hands it that question)."""
    return subprocess.run([shutil.which("id"), "-un"], capture_output=True, text=True, timeout=30).stdout.strip()


def _launch_kit(tmp_path):
    """A launch kit reduced to what scripts/kontroll.sh reads before it hands over to docker, its canonical-group
    and storage-root libraries among them."""
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
    return ["sh", str(kit / "kontroll")]


def _source_tree(tmp_path):
    """A source checkout reduced to scripts/kontroll-installer.sh and its canonical-group and storage-root
    libraries."""
    tree = tmp_path / "tree"
    (tree / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll-installer.sh"), str(tree / "scripts"))
    for lib in (CANONICAL_GROUP, os.path.join(ROOT, "scripts", "lib", "storage-root.sh")):
        shutil.copy(lib, str(tree / "scripts" / "lib"))
    return ["bash", str(tree / "scripts" / "kontroll-installer.sh")]


LAUNCHERS = pytest.mark.parametrize("launcher", [_launch_kit, _source_tree],
                                    ids=["launch-kit kontroll", "kontroll-installer.sh"])


def _launch(tmp_path, launcher, uid, verb, **extra):
    home = tmp_path / "home"
    home.mkdir()
    path, calls = _bin(tmp_path, uid)
    argv = launcher(tmp_path) + ([verb] if verb else [])
    before = _paths(tmp_path)
    extra.setdefault("KONTROLL_STORAGE_ROOT", str(tmp_path / "store"))
    run = subprocess.run(argv, capture_output=True, text=True, env=_env(path, HOME=str(home), **extra),
                         cwd=str(tmp_path))
    return run, calls(), home, sorted(_paths(tmp_path) ^ before)


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("verb", [None, "init", "check", "fresh-init", "configure-semaphore",
                                  "playbooks/deploy-stack.yml"])
@LAUNCHERS
def test_each_launcher_refuses_uid_1001_before_it_makes_anything(tmp_path, launcher, verb):
    """With `id -u` 1001, each launcher, for each verb that deploys or scaffolds (and none, which is `init`), exits 1
    saying the operator has uid 1001, the network service's, naming docs/troubleshooting.md section 10 and "another
    uid", and that nothing was made or run; no stand-in is called (no docker, no sudo, groupadd or usermod), HOME is
    as empty as it began and the launcher's own tree holds what it held. Guards I-18: `fresh-init` scaffolded
    instance/ and minted the control key for an account the deploy then refused; a refusal placed after the
    launcher's mkdirs, which would leave the binds' directories made for it; one placed after a write outside HOME
    (the kit's `git init`, an instance/ in the checkout; the I-18 read B's N-1); and one placed after the
    canonical-group step, which would have run `sudo -n groupadd` and `usermod` for it, changing the system for an
    account it then refuses (the I-18 read A's S2), or after the storage-root step, which would make a root for it
    through `sudo -n sh -c` (I-35)."""
    run, calls, home, made = _launch(tmp_path, launcher, "1001", verb)
    assert run.returncode == 1, run.stderr
    assert SENTENCE in run.stderr and "troubleshooting.md section 10" in run.stderr, run.stderr
    assert "another uid" in run.stderr and "nothing was made or run" in run.stderr, run.stderr
    assert calls == [] and os.listdir(str(home)) == [] and made == [], (calls, os.listdir(str(home)), made)


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@LAUNCHERS
def test_shell_is_told_and_goes_on(tmp_path, launcher):
    """`shell` deploys nothing: with `id -u` 1001 each launcher says the same sentence and goes on to docker (exit
    0, its last call the compose run), without saying that nothing was made. Guards a refusal that locks the escape
    hatch out, which the launchers keep open past every other check of theirs (the run-log directory, I-23; the
    capture's gate)."""
    run, calls, _, _ = _launch(tmp_path, launcher, "1001", "shell")
    assert run.returncode == 0, run.stderr
    assert SENTENCE in run.stderr and "nothing was made" not in run.stderr, run.stderr
    assert calls and calls[-1].startswith("docker compose "), calls


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("uid", ["1000", "10010", "11001"])
@LAUNCHERS
def test_every_other_uid_goes_on(tmp_path, launcher, uid):
    """With any other `id -u`, one beginning or ending in 1001 among them, each launcher says nothing of uid 1001,
    runs the canonical-group step (its `sudo -n groupadd`, through the stand-ins), the storage-root step (its `sudo -n
    sh -c` for the missing root) and then the compose service. Guards a check that refuses everyone, or matches the
    number by its prefix or suffix; and the stand-ins themselves: were the two steps not reached here, the refusal
    rows' "no stand-in is called" would hold for nothing."""
    run, calls, _, _ = _launch(tmp_path, launcher, uid, "init")
    assert run.returncode == 0, run.stderr
    assert "uid 1001" not in run.stderr, run.stderr
    assert GROUP_STEP in calls and calls[-1].startswith("docker compose "), calls
    assert [c for c in calls if c.startswith(STORAGE_STEP) and c.endswith(str(tmp_path / "store"))], calls


@_POSIX
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("uid,sudo,refused", [("1000", {"SUDO_UID": "1001", "SUDO_USER": "second"}, False),
                                              ("1001", {"SUDO_UID": "1000", "SUDO_USER": "first"}, True)],
                         ids=["uid 1000 started by sudo from uid 1001", "uid 1001 started by sudo from uid 1000"])
@LAUNCHERS
def test_a_launchers_operator_is_the_account_running_it(tmp_path, launcher, uid, sudo, refused):
    """A launcher's operator is the account running it (`id -u`), never the one that ran sudo: an account with uid
    1000 started by `sudo -u <it> -H` from a uid-1001 account (SUDO_UID 1001: the T34 smoke's shape on a
    GitHub-hosted runner, whose own account has uid 1001) goes on to the compose run, and a uid-1001 account started
    from a uid-1000 one is refused, calling nothing. Guards a launcher that reads SUDO_UID as install-prereqs.sh does
    as root, which would refuse the first and let the second on (the I-18 reads: B's mutant, A's S3)."""
    run, calls, _, made = _launch(tmp_path, launcher, uid, "init", **sudo)
    if refused:
        assert run.returncode == 1 and SENTENCE in run.stderr and calls == [] and made == [], (run.stderr, calls, made)
    else:
        assert run.returncode == 0 and "uid 1001" not in run.stderr, run.stderr
        assert calls and calls[-1].startswith("docker compose "), calls


def _prereqs(tmp_path):
    """install-prereqs.sh in a scripts/ beside a stand-in lib/: each library says it was sourced and stops the script
    with exit 99, so a run that gets past the checks this file is about installs and makes nothing. The first one
    sourced is storage-root.sh, whose step comes right after the root check (I-24)."""
    s = tmp_path / "scripts"
    (s / "lib").mkdir(parents=True)
    shutil.copy(PREREQS, str(s / "install-prereqs.sh"))
    for lib in ("storage-root.sh", "pkg.sh", "canonical-group.sh"):
        (s / "lib" / lib).write_text('echo "%s sourced" >&2\nexit 99\n' % lib, encoding="utf-8", newline="\n")
    return ["bash", str(s / "install-prereqs.sh")]


@_POSIX
@pytest.mark.parametrize("uid,sudo", [("0", {"SUDO_UID": "1001", "SUDO_USER": "second"}), ("1001", {}),
                                      ("1001", {"SUDO_UID": "1000", "SUDO_USER": "first"})],
                         ids=["sudo by uid 1001", "uid 1001 without sudo", "uid 1001 holding an inherited SUDO_UID"])
def test_install_prereqs_refuses_an_operator_with_uid_1001_first(tmp_path, uid, sudo):
    """Run under sudo by an account with uid 1001 (SUDO_UID 1001, `id -u` 0), or by that account without sudo, even
    with a SUDO_UID its shell inherited (a `su` without `-` from a sudo shell keeps one), install-prereqs.sh exits 1
    with the sentence, naming the account (SUDO_USER as root, else the account itself), the section and that nothing
    was installed; it sources no library and does not say it needs root. Guards I-18 (it made the gid-1001
    group for this operator and pointed at fresh-init, and the refusal came two steps later); a check of `id -u`
    alone, which is 0 under sudo; of SUDO_UID alone, unset without sudo; one that takes SUDO_UID when not root (the
    I-18 read A's N1), which let the last row on to "needs root"; and one after the root check, which tells a run
    without sudo only that it needs root."""
    path, _ = _bin(tmp_path, uid)
    run = subprocess.run(_prereqs(tmp_path), capture_output=True, text=True, env=_env(path, **sudo),
                         cwd=str(tmp_path), timeout=60)
    said = " ".join(run.stderr.split())
    name = sudo["SUDO_USER"] if uid == "0" else _real_name()
    assert run.returncode == 1, run.stderr
    assert re.search(r"install-prereqs\.sh: the operator \(%s\) %s" % (re.escape(name), SENTENCE), said), said
    assert "troubleshooting.md section 10" in said and "Nothing was installed." in said, said
    assert "sourced" not in said and "needs root" not in said, said


@_POSIX
@pytest.mark.parametrize("uid,sudo,rc", [("0", {"SUDO_UID": "1000", "SUDO_USER": "first"}, 99),
                                         ("0", {"SUDO_UID": "10010", "SUDO_USER": "first"}, 99),
                                         ("0", {"SUDO_UID": "11001", "SUDO_USER": "first"}, 99), ("0", {}, 99),
                                         ("1000", {}, 1), ("1000", {"SUDO_UID": "1001", "SUDO_USER": "second"}, 1)],
                         ids=["sudo by uid 1000", "sudo by uid 10010", "sudo by uid 11001", "root without sudo",
                              "uid 1000 without sudo", "uid 1000 holding an inherited SUDO_UID 1001"])
def test_install_prereqs_lets_every_other_operator_on(tmp_path, uid, sudo, rc):
    """Under sudo by any other uid (one beginning or ending in 1001 among them), or as root itself, the script
    passes both checks and sources its first library, the storage root's (the stand-in stops it, exit 99); by uid
    1000 without sudo, even holding a SUDO_UID 1001 its shell inherited, it says it needs root (exit 1). None of
    them names uid 1001. Guards a check that refuses every run under sudo or as root, one that matches the number by
    its prefix or suffix, and one that reads a uid it should not: a SUDO_UID not set by sudo for this run (the I-18
    read A's N1)."""
    path, _ = _bin(tmp_path, uid)
    run = subprocess.run(_prereqs(tmp_path), capture_output=True, text=True, env=_env(path, **sudo),
                         cwd=str(tmp_path), timeout=60)
    assert run.returncode == rc, run.stderr
    assert "uid 1001" not in run.stderr, run.stderr
    assert ("storage-root.sh sourced" if rc == 99 else "needs root") in run.stderr, run.stderr


def _bootstrap(tmp_path):
    """bootstrap.sh beside the scripts/ of `_prereqs`, as it sits at a checkout's top."""
    _prereqs(tmp_path)
    shutil.copy(os.path.join(ROOT, "bootstrap.sh"), str(tmp_path / "bootstrap.sh"))
    return ["bash", str(tmp_path / "bootstrap.sh")]


@_POSIX
@pytest.mark.parametrize("uid,sudo,rc", [("0", {"SUDO_UID": "1001", "SUDO_USER": "second"}, 1),
                                         ("0", {"SUDO_UID": "1000", "SUDO_USER": "first"}, 99), ("1000", {}, 0)],
                         ids=["sudo ./bootstrap.sh by uid 1001", "sudo ./bootstrap.sh by uid 1000", "not root"])
def test_bootstrap_hands_install_prereqs_the_outer_sudos_operator(tmp_path, uid, sudo, rc):
    """bootstrap.sh run as root (`sudo ./bootstrap.sh`) runs install-prereqs.sh itself, so the outer sudo's SUDO_UID
    and SUDO_USER, the operator, reach it: by a uid-1001 account it is refused, naming that account; by another it
    goes on (the stand-in storage-root library stops it, exit 99); sudo is not called in either. Run by an account
    that is not root, it hands install-prereqs.sh to sudo. Guards the shim's unconditional `exec sudo` (the I-18
    read A's N2): under an outer sudo the inner one is invoked by root and sets SUDO_UID 0, so install-prereqs.sh
    took root for the operator, installed for a uid-1001 account and gave the docker and gid-1001 memberships to
    root."""
    path, calls = _bin(tmp_path, uid)
    run = subprocess.run(_bootstrap(tmp_path), capture_output=True, text=True, env=_env(path, **sudo),
                         cwd=str(tmp_path), timeout=60)
    said = " ".join(run.stderr.split())
    assert run.returncode == rc, run.stderr
    if uid != "0":
        assert len(calls()) == 1 and calls()[0].startswith("sudo bash "), calls()
        assert calls()[0].endswith("/scripts/install-prereqs.sh"), calls()
        return
    assert calls() == [], calls()
    assert ("the operator (second) " + SENTENCE in said) == (rc == 1), said
    assert ("storage-root.sh sourced" in said) == (rc == 99), said


_SOURCES = {"scripts/install-prereqs.sh": "install-prereqs.sh: the operator (",
            "scripts/kontroll-installer.sh": "kontroll: the operator (",
            "scripts/kontroll.sh": "kontroll: the operator (",
            "ansible/playbooks/local-canonical.yml": "local-canonical: the operator ("}


def _said_after(path, opening):
    """What `path`'s sentence says after its opening words and the operator's name: its text, whitespace folded, from
    the `) ` that closes the name to the end of that string (a shell `echo` segment's closing quote; in the playbook,
    the folded fail_msg and what follows it, of which the row's words must be a start)."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        text = " ".join(fh.read().split())
    assert text.count(opening) == 1, (path, opening, text.count(opening))
    rest = text[text.index(opening) + len(opening):]
    return rest[rest.index(") ") + 2:].split('"', 1)[0]


def test_the_troubleshooting_row_quotes_each_refusal_as_its_source_says_it():
    """docs/troubleshooting.md section 10 holds one row quoting each of the three sentences: install-prereqs.sh's, the
    launchers' (both say the same one) and the deploy's (local-canonical.yml's fail_msg). Each quote, `<opening>(…)
    <words> …`, is read against its source: the opening words as the script or playbook says them, and the words after
    the operator's name a start of what the source says there. Guards the sentences' pointer (each of the three names
    that section: the launcher and install-prereqs rows above, test_local_canonical.py for the deploy's): a row that
    quotes only the deploy's would leave an operator refused at install-prereqs.sh with nothing to find, and a
    sentence reworded in its source, or a quote reworded in the row, would send the operator looking for words the
    row does not hold (the I-18 read B's N-2)."""
    with open(os.path.join(ROOT, "docs", "troubleshooting.md"), encoding="utf-8") as fh:
        doc = fh.read()
    section = doc.split("\n## 10.", 1)[1].split("\n## 11.", 1)[0]
    row = [ln for ln in section.splitlines() if ln.startswith("|") and "has uid 1001" in ln]
    assert len(row) == 1, row
    quotes = re.findall(r"`([^`]*)`", row[0])
    for path, opening in _SOURCES.items():
        mine = [q for q in quotes if q.startswith(opening + "…) ") and q.endswith(" …")]
        assert len(mine) == 1, (path, quotes)
        words, said = mine[0][len(opening) + 3:-2], _said_after(path, opening)
        assert words and said.startswith(words), (path, words, said[:120])
