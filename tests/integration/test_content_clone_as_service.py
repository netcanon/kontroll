"""The deploy's git in a content clone runs as whoever owns the clone — with REAL uids and the REAL tasks (C10).

WHY: armed, a network service (uid 1001) commits into its content clone (`…/api/repo`, `…/onboard-gui/repo`), so
it writes that clone's config and hooks. deploy-stack.yml runs as root. A git started in a repository does what
the repository's files say — a `core.hooksPath` hook, `core.fsmonitor`, a filter — so a root git in the clone would
run whatever the service put there AS ROOT: a container's compromise becomes the host's at the next deploy. The
rule (ansible/playbooks/tasks/content-clone.yml): root never runs git in a clone the service uid can write, and
never changes an owner by path in a tree that uid writes; armed, git in the clone is the service uid's (setpriv,
no group, no new privileges, a session of its own, an environment of its own); a clone held by the wrong uid is
removed and made again by the right one. These rows run that task file — the one deploy-stack.yml imports — and,
for a fresh node, deploy-stack.yml's own tasks that make the storage root and the stores before the clone, with
ansible-playbook as root, against a canonical laid out as the play leaves it (another uid's, group 1001), and plant
in the clone programs that write down who they run as.

ansible-playbook is started with a CONTROLLING TERMINAL (a pseudo-terminal it is made the session leader of), as
an operator's `sudo` deploy is: a program of the service's that kept it could push keystrokes into the operator's
shell (TIOCSTI). Every record must show none.

It needs ROOT, a Linux kernel, setpriv, setsid and ansible-playbook. tests/validate.sh runs this file in its
as-root step, which fails when a row here was SKIPPED; in a plain `pytest` run by an ordinary user every row is
skipped.
"""
import fcntl
import json
import os
import pty
import re
import shutil
import subprocess
import tempfile
import termios

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASKS = os.path.join(ROOT, "ansible", "playbooks", "tasks", "content-clone.yml")
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not (hasattr(os, "geteuid") and os.geteuid() == 0 and os.uname().sysname == "Linux"
                            and all(shutil.which(t) for t in ("git", "setpriv", "setsid", "ansible-playbook"))),
                       reason="needs root on Linux (it changes ownership and starts git as the service uid), with "
                              "git, setpriv, setsid and ansible-playbook — tests/validate.sh runs it as root"),
]

SERVICE = 1001
OPERATOR = 1000                       # the canonical's owner: neither root nor the service (the play's operator)
_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
_AS_SERVICE = ["setpriv", "--reuid=%d" % SERVICE, "--regid=%d" % SERVICE, "--clear-groups"]

# What a planted program writes, one line per run: uid, gid, every group, no-new-privileges, effective
# capabilities, the controlling terminal (field 7 of /proc/<pid>/stat; 0 = none), HOME, and how many variables of
# the deploy's own environment reached it (ansible-playbook's ANSIBLE_*, sudo's SUDO_*: env -i lets none in).
_SAY_WHO = ('#!/bin/sh\n'
            'printf "uid=%s gid=%s groups=%s nnp=%s cap=%s tty=%s home=%s leaked=%s\\n" "$(id -u)" "$(id -g)" '
            '"$(id -G)" "$(sed -n "s/^NoNewPrivs:[[:space:]]*//p" /proc/$$/status)" '
            '"$(sed -n "s/^CapEff:[[:space:]]*//p" /proc/$$/status)" '
            '"$(sed "s/^.*) //" /proc/$$/stat | cut -d" " -f5)" "$HOME" '
            '"$(env | grep -c -e ^ANSIBLE_ -e ^SUDO_)" >> "@LOG@"\n')
_CLEAN = "uid=1001 gid=1001 groups=1001 nnp=1 cap=0000000000000000 tty=0 home=/nonexistent leaked=0"


class World:
    """A base directory every uid can enter, holding the canonical, a storage root, the clone's 1001-owned parent,
    a 1001-owned directory for what the planted programs write, and the playbook that imports the task file."""

    def __init__(self, base):
        self.base = base
        self.canonical = os.path.join(base, "canonical.git")
        self.work = os.path.join(base, "work")
        self.storage = os.path.join(base, "storage")
        self.parent = os.path.join(self.storage, "api")
        self.clone = os.path.join(self.parent, "repo")
        self.who = os.path.join(base, "who")
        self.log = os.path.join(self.who, "log")
        self.gitconfig = os.path.join(base, "root-gitconfig")
        with open(self.gitconfig, "w", encoding="utf-8") as fh:      # the rows' own root git: trust their canonical
            fh.write("[safe]\n\tdirectory = *\n")
        os.makedirs(self.parent)                                   # as the play's host-dirs task leaves it: root's
        os.makedirs(self.who)
        os.chown(self.who, SERVICE, SERVICE)
        self.git(self.base, "init", "-q", "--bare", "-b", "main", self.canonical)
        self.git(self.base, "init", "-q", "-b", "main", self.work)
        self.commit("first")
        self.playbook = os.path.join(base, "play.yml")
        with open(self.playbook, "w", encoding="utf-8") as fh:
            fh.write(
                "- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
                "    - name: The content clone, as deploy-stack.yml makes it\n"
                "      ansible.builtin.import_tasks: %s\n"
                "      vars:\n        _clone_dir: %s\n        _clone_service: api\n        _clone_canonical: %s\n"
                % (json.dumps(TASKS), json.dumps(self.clone), json.dumps(self.canonical)))

    def git(self, cwd, *argv):
        """Root's git, for the rows' own set-up (never in the clone)."""
        env = dict(os.environ, GIT_CONFIG_GLOBAL=self.gitconfig, **_WHO)
        p = subprocess.run(["git"] + list(argv), cwd=cwd, env=env, capture_output=True, text=True)
        assert p.returncode == 0, "git %s: %s" % (" ".join(argv), p.stderr)
        return p.stdout.strip()

    def commit(self, text):
        """A new commit on the canonical's main, pushed by root, then the canonical laid out as the play leaves
        it: the operator's, group 1001, directories 2775, files group-writable."""
        with open(os.path.join(self.work, "f"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        self.git(self.work, "add", "f")
        self.git(self.work, "commit", "-q", "-m", text)
        self.git(self.work, "push", "-q", self.canonical, "HEAD:refs/heads/main")
        for here, _dirs, files in os.walk(self.canonical):
            os.chown(here, OPERATOR, SERVICE)
            os.chmod(here, 0o2775)
            for name in files:
                os.chown(os.path.join(here, name), OPERATOR, SERVICE)
                os.chmod(os.path.join(here, name), 0o664)
        return self.git(self.work, "rev-parse", "HEAD")

    def as_service(self, *argv):
        """Run a command as the service would (uid and gid 1001, no other group), e.g. to plant in its clone."""
        env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull}
        p = subprocess.run(_AS_SERVICE + list(argv), cwd="/", env=env, capture_output=True, text=True)
        assert p.returncode == 0, "as the service, %s: %s" % (" ".join(argv), p.stderr)
        return p.stdout.strip()

    def plant(self, git_dir_hooks=False):
        """What a compromised service writes into its clone: a hooks path whose `post-checkout`,
        `reference-transaction` and `post-merge` hooks, and a `core.fsmonitor` program, write down who they run
        as. `git_dir_hooks` puts the hooks in the clone's own `.git/hooks` instead (a directory of the service's
        inside a clone that is otherwise root's)."""
        hooks = os.path.join(self.clone, ".git", "hooks") if git_dir_hooks else os.path.join(self.who, "hooks")
        script = _SAY_WHO.replace("@LOG@", self.log)
        self.as_service("sh", "-c", 'mkdir -p "$0" && for h in post-checkout reference-transaction post-merge '
                                    'fsmonitor; do printf "%s" "$1" > "$0/$h" && chmod 0755 "$0/$h"; done',
                        hooks, script)
        if not git_dir_hooks:
            self.as_service("git", "-C", self.clone, "config", "core.hooksPath", hooks)
            self.as_service("git", "-C", self.clone, "config", "core.fsmonitor", os.path.join(hooks, "fsmonitor"))
        return hooks

    def records(self):
        """The planted programs' lines (none when no program ran)."""
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]

    def play(self, armed, check=False, expect_ok=True):
        """Run the task file with ansible-playbook as root, holding a controlling terminal. Returns the recap's
        counters; a failed task fails the row with the run's output. With `expect_ok=False` a failed run is
        returned (`rc`, `out`), not asserted."""
        home = os.path.join(self.base, "ansible-home")
        os.makedirs(home, exist_ok=True)
        cfg = os.path.join(home, "ansible.cfg")                 # nothing of the developer's or the repository's
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("[defaults]" + chr(10))
        extra = {"api_privileged": armed, "_fresh_preview": False, "kontroll_storage_root": self.storage}
        argv = ["ansible-playbook", "-i", "localhost,", self.playbook, "-e", json.dumps(extra)]
        if check:
            argv += ["--check", "--diff"]
        env = dict(os.environ, ANSIBLE_HOME=home, ANSIBLE_LOCAL_TEMP=os.path.join(home, "tmp"),
                   ANSIBLE_NOCOLOR="1", ANSIBLE_RETRY_FILES_ENABLED="0", ANSIBLE_PYTHON_INTERPRETER="auto_silent",
                   ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_CONFIG=cfg,
                   SUDO_USER="operator", SUDO_COMMAND="ansible-playbook")   # as an operator's `sudo` deploy has
        env.update(getattr(self, "extra_env", {}))         # a row may add to the deploy's environment
        master, slave = pty.openpty()

        def _terminal():                                 # the child leads a session whose terminal is the pty
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        try:
            p = subprocess.run(argv, cwd=self.base, env=env, stdin=slave, capture_output=True, text=True,
                               timeout=600, preexec_fn=_terminal)
        finally:
            os.close(slave)
            os.close(master)
        recap = re.search(r"localhost\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", p.stdout)
        if not expect_ok:
            return {"rc": p.returncode, "out": p.stdout + p.stderr}
        assert p.returncode == 0 and recap, "ansible-playbook rc=%d\n%s\n%s" % (p.returncode, p.stdout, p.stderr)
        return {"changed": int(recap.group(2)), "failed": int(recap.group(4)), "out": p.stdout, "rc": 0}

    def owners(self):
        """Every owner uid of the clone's top, files, directories and links."""
        seen = {os.lstat(self.clone).st_uid}
        for here, dirs, files in os.walk(self.clone):
            for name in dirs + files:
                seen.add(os.lstat(os.path.join(here, name)).st_uid)
        return seen

    def head(self):
        with open(os.path.join(self.clone, "f"), encoding="utf-8") as fh:
            return fh.read().strip()


@pytest.fixture
def w():
    """A World under /tmp that every uid can enter (pytest's `tmp_path` is under a directory only root enters)."""
    base = tempfile.mkdtemp(prefix="kontroll-clone-", dir="/tmp")
    os.chmod(base, 0o755)
    try:
        yield World(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_an_armed_deploy_makes_the_clone_as_the_service_and_a_second_changes_nothing(w):
    """Armed, with no clone: uid 1001 makes it — every file of it is the service's (root wrote nothing in it), it
    holds the canonical's main, and it has the `local` remote gitio pushes a proposal to. The next deploy reports
    nothing changed (a role must be idempotent)."""
    first = w.play(armed=True)
    assert first["changed"] > 0
    assert w.owners() == {SERVICE}, "root wrote into the service's clone: %r" % w.owners()
    assert w.head() == "first"
    assert w.as_service("git", "-C", w.clone, "remote", "get-url", "local") == "file://" + w.canonical
    assert w.play(armed=True)["changed"] == 0, "a second armed deploy changed something"


def test_what_the_services_clone_makes_git_run_runs_as_the_service_never_as_root(w):
    """The service plants hooks and an fsmonitor program in its clone, and main moves. `--check --diff` first:
    it says the clone would change and changes nothing. Then the deploy brings the clone to main — and every
    program the clone made git run ran as uid 1001 with no other group, no new privileges, no capability and no
    terminal. Before this change root's git module ran them as root."""
    w.play(armed=True)
    w.plant()
    w.commit("second")
    preview = w.play(armed=True, check=True)
    assert preview["changed"] > 0, "the preview must say the clone would move"
    assert w.head() == "first", "--check changed the clone"
    w.play(armed=True)
    assert w.head() == "second"
    said = w.records()
    assert said, "no planted program ran — the row proves nothing"
    assert all(line == _CLEAN for line in said), "a program the clone chose ran with more than the service:\n" + \
        "\n".join(sorted(set(said)))
    assert w.play(armed=True)["changed"] == 0


def test_the_first_armed_deploy_makes_a_root_owned_clone_again_as_the_service(w):
    """A clone root made while the deploy was unarmed is root's. Arming does not chown it (a recursive chown by
    path while the service can write part of the tree is a race the service can win): it is removed and uid
    1001 makes it again — a new directory, wholly the service's. `--check --diff` first says so and changes
    nothing (and runs no git of the service's in root's clone — 1001's git would refuse it)."""
    w.play(armed=False)
    assert w.owners() == {0}
    marker = os.path.join(w.clone, "untracked-mark")          # survives a reset and a chown, not a removal
    open(marker, "w").close()
    assert w.play(armed=True, check=True)["changed"] > 0, "the preview must say the clone would be made again"
    assert os.path.exists(marker) and w.owners() == {0}, "--check changed the clone"
    assert w.play(armed=True)["changed"] > 0
    assert w.owners() == {SERVICE}
    assert not os.path.exists(marker), "the clone was changed in place, not made again"
    assert w.play(armed=True)["changed"] == 0


def test_disarming_removes_the_services_clone_and_never_runs_git_in_it(w):
    """A clone of the service's — with programs planted in it — when the deploy is unarmed: `--check --diff`
    leaves it as it is and runs nothing of it; the deploy removes it and root makes it again, wholly root's.
    Root's git never runs in the service's clone, so no planted program ever ran as root."""
    w.play(armed=True)
    w.plant()
    w.commit("second")
    marker = os.path.join(w.clone, "untracked-mark")
    w.as_service("touch", marker)
    w.play(armed=False, check=True)
    assert os.path.exists(marker) and w.owners() == {SERVICE}, "--check changed the clone"
    w.play(armed=False)
    assert not os.path.exists(marker) and w.owners() == {0}
    assert w.head() == "second"
    assert not any(line.startswith("uid=0 ") for line in w.records()), "root ran the service's program"
    assert w.play(armed=False)["changed"] == 0


def test_a_root_clone_holding_one_directory_of_the_services_is_not_roots(w):
    """A clone whose top is root's but whose `.git/hooks` is the service's (what an earlier build's recursive
    chown left when it stopped part-way — it changed the top LAST) is not root's: the unarmed deploy removes it
    rather than run root's git there, so the hook the service wrote into that directory never runs as root."""
    w.play(armed=False)
    os.chown(os.path.join(w.clone, ".git", "hooks"), SERVICE, SERVICE)
    w.plant(git_dir_hooks=True)
    w.commit("second")
    w.play(armed=False)
    assert w.owners() == {0}
    assert w.head() == "second"
    assert not any(line.startswith("uid=0 ") for line in w.records()), "root ran the service's hook"


@pytest.mark.parametrize("armed", [True, False], ids=["armed", "unarmed"])
def test_a_link_where_the_clone_belongs_is_removed_and_what_it_points_at_is_left(w, armed):
    """A symbolic link at the clone's path (to a directory of root's elsewhere — the clone's parent is the
    service's, so the service's uid can put one there): the deploy removes the LINK and makes the clone — it
    neither follows the link into the other directory nor changes anything there. Unarmed too: a look that
    followed the link would find a directory of root's there and let root's git work in it."""
    elsewhere = os.path.join(w.base, "elsewhere")
    os.makedirs(elsewhere)
    with open(os.path.join(elsewhere, "keep"), "w", encoding="utf-8") as fh:
        fh.write("root's\n")
    os.symlink(elsewhere, w.clone)
    w.play(armed=armed)
    assert not os.path.islink(w.clone) and w.owners() == ({SERVICE} if armed else {0})
    assert os.listdir(elsewhere) == ["keep"] and os.lstat(os.path.join(elsewhere, "keep")).st_uid == 0
    assert os.lstat(elsewhere).st_uid == 0


def _armed_argv(w, mode="apply"):
    """The armed task's command, as tasks/content-clone.yml spells it, with the play's values put in for `w`."""
    with open(TASKS, encoding="utf-8") as fh:
        tasks = yaml.safe_load(fh)
    task = [t for t in tasks if str(t.get("name", "")).startswith("Armed — clone/update")]
    assert len(task) == 1, "the armed task is not where this row looks for it"
    task = task[0]
    values = {"{{ _clone_as_service }}": task["vars"]["_clone_as_service"], "{{ _clone_dir }}": w.clone,
              "{{ _clone_canonical }}": w.canonical, "{{ kontroll_storage_root }}": w.storage,
              "{{ _clone_dir | dirname }}": w.parent, "{{ 'check' if ansible_check_mode else 'apply' }}": mode}
    argv = []
    for word in task["ansible.builtin.command"]["argv"]:
        for name, value in values.items():
            word = word.replace(name, value)
        assert "{{" not in word, "a value this row does not know: %r" % word
        argv.append(word)
    return argv, task["ansible.builtin.command"]["chdir"]


def test_the_services_git_holds_no_terminal_even_where_ansible_hands_its_own_down(w):
    """ansible-core 2.18 — the installer image's (it pins below 2.19) — starts a task's command in
    ansible-playbook's own session, so on an operator's `sudo` deploy that command holds the operator's terminal
    (measured: 2.19's workers start a session of their own, 2.18's do not). A program the service planted in its
    clone could then push keystrokes into it. The armed task's `setsid` is what takes the terminal away. This row
    runs the task's own command, as the task file spells it, from a process that holds a terminal — so it holds
    whatever ansible-core the gate has; and every program the clone made git run held none."""
    w.play(armed=True)
    w.plant()
    w.commit("second")
    argv, cwd = _armed_argv(w)
    master, slave = pty.openpty()

    def _terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    try:
        p = subprocess.run(argv, cwd=cwd, stdin=slave, capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, ANSIBLE_FORKS="5", SUDO_USER="operator"), preexec_fn=_terminal)
    finally:
        os.close(slave)
        os.close(master)
    assert p.returncode == 0 and "state=changed" in p.stdout.split(), (p.returncode, p.stdout, p.stderr)
    assert w.head() == "second"
    said = w.records()
    assert said, "no planted program ran — the row proves nothing"
    assert all(line == _CLEAN for line in said), "a program the clone chose ran with more than the service:\n" + \
        "\n".join(sorted(set(said)))


@pytest.mark.parametrize("mode", ["group-1001-writable", "world-writable"])
def test_an_unarmed_entry_the_service_may_write_by_its_mode_alone_makes_the_clone_not_roots(w, mode):
    """Root owns every entry of the clone, but one directory's MODE lets the service write it — group 1001 with
    the group's write bit, or anybody's write bit. The service could plant a hook there before the next deploy,
    so the clone is not root's: the unarmed deploy makes it again, wholly root's and writable by root alone,
    though nothing of the service's is in it yet. One row per mode arm of the look, each alone (the row above
    that plants a file of the service's is caught by the look's owner arm first)."""
    w.play(armed=False)
    hooks = os.path.join(w.clone, ".git", "hooks")
    if mode == "group-1001-writable":
        os.chown(hooks, 0, SERVICE)
        os.chmod(hooks, 0o775)
    else:
        os.chmod(hooks, 0o757)
    marker = os.path.join(w.clone, "untracked-mark")
    open(marker, "w").close()
    assert w.play(armed=False)["changed"] > 0
    assert not os.path.exists(marker), "a clone the service could write was kept for root's git"
    st = os.lstat(hooks)
    assert w.owners() == {0} and st.st_gid == 0 and not st.st_mode & 0o022, oct(st.st_mode)
    assert w.play(armed=False)["changed"] == 0


def test_an_unarmed_clone_holding_a_link_and_an_entry_its_group_only_reads_is_kept(w):
    """What the look must NOT call foreign: a link (its own mode is 0777 and means nothing; git checks a tracked
    link out as one) and an entry whose group is 1001 but which that group may not write. Such a clone is kept
    and a second unarmed deploy reports nothing changed — a look that counted either would make the clone again,
    and restart its service, on every deploy."""
    os.symlink("f", os.path.join(w.work, "link"))
    w.git(w.work, "add", "link")
    w.commit("with a link")
    w.play(armed=False)
    assert os.path.islink(os.path.join(w.clone, "link"))
    os.chown(os.path.join(w.clone, ".git", "description"), 0, SERVICE)    # 0644: the group may read it, no more
    marker = os.path.join(w.clone, "untracked-mark")
    open(marker, "w").close()
    assert w.play(armed=False)["changed"] == 0
    assert os.path.exists(marker), "a clone wholly root's was made again"


def test_an_armed_directory_of_the_services_that_holds_no_clone_stops_the_deploy_with_the_remedy(w):
    """A directory of the service's at the clone's path that holds something but no clone (a clone that stopped
    part-way, or a directory made by hand): the armed deploy stops there and says what to do — the sentence
    docs/troubleshooting.md section 9 quotes — rather than report the clone updated; nothing in it is changed.
    (An EMPTY directory of the service's is what root makes for the service's git to clone into.)"""
    os.makedirs(w.clone)
    os.chown(w.clone, SERVICE, SERVICE)
    w.as_service("touch", os.path.join(w.clone, "stray"))
    out = w.play(armed=True, expect_ok=False)
    assert out["rc"] != 0
    assert "%s is not a git clone: remove it and run the deploy again" % w.clone in out["out"], out["out"]
    assert os.listdir(w.clone) == ["stray"]


def test_an_armed_clone_gets_its_local_remote_back_and_is_updated_from_the_canonical_whatever_it_names(w):
    """The service writes its clone's remotes. With `local` removed, the deploy adds it back (gitio pushes a
    proposal to a remote of that name) and the next deploy changes nothing; with `local` pointed at another
    repository, the update still comes from the canonical — the script fetches by URL, never by the remote's
    name — so the clone holds the canonical's main, not what the service named."""
    w.play(armed=True)
    w.as_service("git", "-C", w.clone, "remote", "remove", "local")
    assert w.play(armed=True)["changed"] > 0
    assert w.as_service("git", "-C", w.clone, "remote", "get-url", "local") == "file://" + w.canonical
    assert w.play(armed=True)["changed"] == 0
    decoy = os.path.join(w.base, "decoy.git")
    w.git(w.base, "clone", "-q", "--bare", w.canonical, decoy)
    w.as_service("git", "-C", w.clone, "remote", "set-url", "local", "file://" + decoy)
    w.commit("second")
    w.play(armed=True)
    assert w.head() == "second"


def test_an_armed_preview_of_a_node_with_no_clone_writes_nothing(w):
    """`--check --diff` first, on an armed node with no clone and no configuration file yet: it says the clone
    would be made and writes nothing — no clone, no configuration file, nothing in the clone's parent. Then the
    deploy makes it, wholly the service's."""
    assert w.play(armed=True, check=True)["changed"] > 0
    assert not os.path.lexists(w.clone) and os.listdir(w.parent) == []
    assert not os.path.exists(os.path.join(w.storage, "content-clone.gitconfig"))
    w.play(armed=True)
    assert w.owners() == {SERVICE}


def test_an_armed_deploy_fails_when_the_service_cannot_read_the_canonical(w):
    """An armed clone whose canonical uid 1001 can no longer read (its mode changed): the deploy FAILS — it must
    not reset the clone to the last main it fetched and call that `ok`. Every step of the script stops it
    (`set -e`)."""
    w.play(armed=True)
    w.commit("second")
    os.chown(w.canonical, OPERATOR, OPERATOR)
    os.chmod(w.canonical, 0o2770)
    assert w.play(armed=True, expect_ok=False)["rc"] != 0
    assert w.head() == "first"


def test_the_restart_after_up_names_exactly_the_replaced_services_that_up_brought_up(w):
    """deploy-stack.yml's restart task — its `when` and its command, read from the file as they stand — evaluated
    by ansible-playbook: a service whose clone was made again and that `up` brought up is restarted; one `up` did
    not bring up is not; nothing is restarted when no clone was made again. A condition that is never true would
    leave a container on the directory the deploy removed; one that ignored `up` would start a service the
    operator did not deploy."""
    with open(os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml"), encoding="utf-8") as fh:
        play = yaml.safe_load(fh)
    found = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if str(node.get("name", "")).startswith("Restart a service whose content clone was made again"):
                found.append(node)
            for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
                walk(node.get(key))
    walk(play)
    assert len(found) == 1
    task = found[0]
    probe = os.path.join(w.base, "restart.yml")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("- hosts: localhost\n  connection: local\n  gather_facts: false\n  vars:\n    stack_dir: /stack\n"
                 "  tasks:\n    - name: the restart task's condition and command\n      ansible.builtin.debug:\n"
                 "        msg: %s\n      when: %s\n"
                 % (json.dumps("RESTART<" + task["ansible.builtin.command"]["cmd"] + ">"), json.dumps(task["when"])))

    def restart(**facts):
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", probe, "-e", json.dumps(facts)], cwd=w.base,
                           capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0",
                                    ANSIBLE_PYTHON_INTERPRETER="auto_silent"))
        assert p.returncode == 0, p.stdout + p.stderr
        said = re.findall(r"RESTART<([^>]*)>", p.stdout)
        return said[0].split(" restart ", 1)[1].split() if said else None
    assert restart(_clones_replaced=["api"], _up_services=["api", "semaphore"]) == ["api"]
    assert sorted(restart(_clones_replaced=["api", "onboard-gui"], _up_services=["onboard-gui", "api"])) == [
        "api", "onboard-gui"]
    assert restart(_clones_replaced=["onboard-gui"], _up_services=["api"]) is None
    assert restart(_up_services=["api"]) is None


def test_the_two_imports_in_one_play_each_decide_their_own_clone(w):
    """deploy-stack.yml imports the task file twice in ONE play — the API's clone, then onboard-gui's — so the
    second import runs with the first one's registered results still in the play (its look, its decision, its
    removal). Only the API's clone is not root's here: the unarmed deploy makes the API's again and keeps
    onboard-gui's, and `_clones_replaced` names the API alone — what the restart after `up` reads. The other rows
    import the file once, with `_clone_service: api`."""
    gui_parent = os.path.join(w.storage, "onboard-gui")
    os.makedirs(gui_parent)                                   # root's, as the play's host-dirs task leaves it
    gui = os.path.join(gui_parent, "repo")
    both = os.path.join(w.base, "both.yml")
    with open(both, "w", encoding="utf-8") as fh:
        fh.write("- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n")
        for service, clone in (("api", w.clone), ("onboard-gui", gui)):
            fh.write("    - name: the %s content clone\n      ansible.builtin.import_tasks: %s\n"
                     "      vars:\n        _clone_dir: %s\n        _clone_service: %s\n        _clone_canonical: %s\n"
                     % (service, json.dumps(TASKS), json.dumps(clone), service, json.dumps(w.canonical)))
        fh.write("    - name: what the restart reads\n      ansible.builtin.debug:\n"
                 "        msg: \"REPLACED<{{ _clones_replaced | default([]) | join(',') }}>\"\n")
    w.playbook = both
    w.play(armed=False)
    os.chown(os.path.join(w.clone, ".git", "hooks"), SERVICE, SERVICE)
    api_mark, gui_mark = os.path.join(w.clone, "mark"), os.path.join(gui, "mark")
    open(api_mark, "w").close()
    open(gui_mark, "w").close()
    out = w.play(armed=False)
    assert re.findall(r"REPLACED<([^>]*)>", out["out"]) == ["api"], out["out"]
    assert not os.path.exists(api_mark) and os.path.exists(gui_mark)
    out = w.play(armed=False)
    assert out["changed"] == 0 and re.findall(r"REPLACED<([^>]*)>", out["out"]) == [""]


def test_roots_git_in_the_clone_reads_no_system_configuration(w):
    """The unarmed git module runs with GIT_CONFIG_NOSYSTEM=1, set by the task itself: a system git file — the
    installer image's holds `safe.directory '*'`, a host's may hold anything — must not configure root's git in
    the clone (with `'*'` it would trust a clone of the service's put in its place after the look). Here the
    deploy's own environment leaves the system file ON and that file names a hooks path whose hooks write down
    who ran them: making the clone and updating it, none ran."""
    hooks = os.path.join(w.base, "system-hooks")
    os.makedirs(hooks)
    for name in ("post-checkout", "reference-transaction", "post-merge"):
        with open(os.path.join(hooks, name), "w", encoding="utf-8") as fh:
            fh.write(_SAY_WHO.replace("@LOG@", w.log))
        os.chmod(os.path.join(hooks, name), 0o755)
    system = os.path.join(w.base, "system-gitconfig")
    with open(system, "w", encoding="utf-8") as fh:
        fh.write("[core]\n\thooksPath = %s\n" % hooks)
    w.extra_env = {"GIT_CONFIG_SYSTEM": system, "GIT_CONFIG_NOSYSTEM": "0"}
    w.play(armed=False)
    w.commit("second")
    w.play(armed=False)
    assert w.head() == "second"
    assert w.records() == [], "the system file configured root's git in the clone"


def test_an_armed_clone_of_another_uids_is_made_again_as_the_service(w):
    """Armed, a clone that is neither root's nor the service's — the operator's, say, made or copied by hand — is
    not the service's to keep: uid 1001's git refuses it (dubious ownership), so a deploy that kept it would stop
    there on every run, and the service could not commit into it. It is removed and the service makes it again;
    the next deploy changes nothing."""
    w.play(armed=False)
    for here, dirs, files in os.walk(w.clone):
        for name in dirs + files:
            os.lchown(os.path.join(here, name), OPERATOR, OPERATOR)
    os.lchown(w.clone, OPERATOR, OPERATOR)
    assert w.play(armed=True)["changed"] > 0
    assert w.owners() == {SERVICE}
    assert w.play(armed=True)["changed"] == 0


def test_a_clone_the_service_makes_already_holds_local_main(w):
    """gitio's staging rolls the clone back to `local/main` and rebases a proposal onto it: right after the
    armed deploy makes a clone, `refs/remotes/local/main` is there and names the clone's HEAD — the clone does
    not wait for its first proposal (or the next deploy) to have the ref gitio reads."""
    w.play(armed=True)
    assert w.as_service("git", "-C", w.clone, "rev-parse", "--verify", "refs/remotes/local/main") == \
        w.as_service("git", "-C", w.clone, "rev-parse", "HEAD")


def test_an_armed_update_says_so_when_it_discards_a_change_made_in_the_clone(w):
    """A tracked file changed in the clone and not proposed: the update discards it (after every deploy the clone
    is the canonical's main) — and SAYS so. The preview reports the clone would change and leaves the change; the
    deploy reports it changed; the next deploy reports nothing. A discard reported as `ok` hides that something
    of the service's was thrown away, and a preview that says nothing is rosier than the run."""
    w.play(armed=True)
    w.as_service("sh", "-c", 'echo local >> "$0"', os.path.join(w.clone, "f"))
    assert w.play(armed=True, check=True)["changed"] > 0
    assert w.head() == "first\nlocal", "--check changed the clone"
    assert w.play(armed=True)["changed"] > 0
    assert w.head() == "first"
    assert w.play(armed=True)["changed"] == 0


def test_an_armed_update_puts_the_clone_back_on_main(w):
    """The service may leave its clone on another branch, or on none. The update leaves it where the git module
    (`version: main`) left it — on `main`, at the canonical's main — not on the service's branch moved to main's
    commit with the clone's own `main` left behind: gitio stages on the clone's `main`."""
    w.play(armed=True)
    w.as_service("git", "-C", w.clone, "checkout", "-q", "-b", "side")
    w.commit("second")
    assert w.play(armed=True)["changed"] > 0
    assert w.as_service("git", "-C", w.clone, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert w.head() == "second"
    w.as_service("git", "-C", w.clone, "checkout", "-q", "--detach")
    assert w.play(armed=True)["changed"] > 0
    assert w.as_service("git", "-C", w.clone, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert w.play(armed=True)["changed"] == 0


def test_an_armed_update_points_local_back_at_the_canonical(w):
    """The service may point its clone's `local` remote anywhere; gitio pushes a proposal to `local`. The deploy
    puts it back at the canonical (the clone's known state — the same deploy also brings HEAD to main), says so,
    and the next deploy changes nothing."""
    w.play(armed=True)
    decoy = os.path.join(w.base, "decoy.git")
    w.git(w.base, "clone", "-q", "--bare", w.canonical, decoy)
    w.as_service("git", "-C", w.clone, "remote", "set-url", "local", "file://" + decoy)
    assert w.play(armed=True)["changed"] > 0
    assert w.as_service("git", "-C", w.clone, "remote", "get-url", "local") == "file://" + w.canonical
    assert w.play(armed=True)["changed"] == 0


def test_the_armed_preview_compares_main_only_when_it_can_read_it(w):
    """The armed script in check mode, as the task file spells it: with the configuration file in place it names
    the canonical's main; without it (the first preview after an upgrade — a preview writes nothing, so uid
    1001's git cannot yet read the operator's canonical) it says main was not compared rather than print an
    empty id; and when the canonical cannot be read it FAILS, as the deploy then does. A preview is never rosier
    than the run."""
    w.play(armed=True)
    w.commit("second")
    want = w.git(w.base, "--git-dir", w.canonical, "rev-parse", "refs/heads/main")
    argv, cwd = _armed_argv(w, mode="check")
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and ("-> %s" % want) in p.stdout and "state=changed" in p.stdout.split(), p.stdout
    conf = os.path.join(w.storage, "content-clone.gitconfig")
    os.rename(conf, conf + ".away")
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "main not compared" in p.stdout and not re.search(r"-> *$", p.stdout, re.M), \
        (p.stdout, p.stderr)
    os.rename(conf + ".away", conf)
    os.chown(w.canonical, OPERATOR, OPERATOR)
    os.chmod(w.canonical, 0o2770)
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert p.returncode != 0 and "cannot read refs/heads/main" in p.stderr, (p.stdout, p.stderr)
    assert w.head() == "first"


def test_the_armed_script_reaches_sh_as_written_whatever_the_deploys_environment_holds(w):
    """ansible's command module expands `$NAME` in every argument from the deploy's own environment before it
    starts the command (`expand_argument_vars`, on by default since ansible-core 2.16). The armed script's own
    variables — `$dest`, `$url`, `$mode`, `$changed` — would then be the environment's: a `dest` exported where
    the deploy runs makes the clone somewhere else, a `changed` makes every deploy report a change. The task
    switches the expansion off, so the clone is made where the import says, and a second deploy changes
    nothing."""
    elsewhere = os.path.join(w.base, "elsewhere")
    os.makedirs(elsewhere)
    os.chown(elsewhere, SERVICE, SERVICE)
    w.extra_env = {"dest": os.path.join(elsewhere, "clone"), "changed": "yes"}
    w.play(armed=True)
    assert os.path.isdir(os.path.join(w.clone, ".git")) and os.listdir(elsewhere) == []
    assert w.play(armed=True)["changed"] == 0


@pytest.mark.parametrize("which", ["parent", "storage-root"])
@pytest.mark.parametrize("armed", [True, False], ids=["armed", "unarmed"])
def test_a_parent_or_storage_root_the_service_could_write_stops_the_deploy_before_any_git(w, armed, which):
    """What stands at the clone's path is root's to place. The clone's parent is where the clone's entry lives: a
    writer of it could exchange the clone for a link — to a repository it writes — between the deploy's look and
    root's git, and root's git would follow it. The storage root holds the one configuration file both gits read.
    So each must be a directory of root's that neither its group nor anybody else may write: a parent of the
    service's (as the previous build left it — the play's host-dirs task now makes it root's first), or a storage
    root the service's group may write, stops the deploy with the remedy, before anything in the clone is touched
    and before any git runs."""
    w.play(armed=armed)
    marker = os.path.join(w.clone, "untracked-mark")
    if armed:
        w.as_service("touch", marker)
    else:
        open(marker, "w").close()
    if which == "parent":
        os.chown(w.parent, SERVICE, SERVICE)
    else:
        os.chown(w.storage, 0, SERVICE)
        os.chmod(w.storage, 0o775)
    w.commit("second")
    out = w.play(armed=armed, expect_ok=False)
    assert out["rc"] != 0 and "must be a directory of root's" in out["out"], out["out"]
    assert os.path.exists(marker) and w.head() == "first", "the deploy touched the clone before it refused"
    assert not any(line.startswith("uid=0 ") for line in w.records())


@pytest.mark.parametrize("mode", [0o700, 0o750], ids=["0700", "0750"])
@pytest.mark.parametrize("armed", [True, False], ids=["armed", "unarmed"])
def test_a_storage_root_closed_to_the_service_stops_the_deploy_and_the_printed_fix_opens_it(w, armed, mode):
    """The storage root must be one that anybody may pass through: armed, the service's git (uid and gid 1001, no
    supplementary group) reaches its clone and the configuration file root writes there through it by the bit for
    others, and a root-owned 0700 root — what `chown root:root` alone leaves of a root an older build made 1001's
    0700 — stops it at `could not create leading directories`. So does 0750: the root's group is root's, not the
    service's, so the refusal reads the bit for others, not the group's. The refusal says so before any git runs,
    unarmed too (so arming never finds the root closed), and the fix it prints is enough: after it the deploy
    updates the clone."""
    w.play(armed=armed)
    marker = os.path.join(w.clone, "untracked-mark")
    if armed:
        w.as_service("touch", marker)
    else:
        open(marker, "w").close()
    os.chown(w.storage, 0, 0)
    os.chmod(w.storage, mode)
    w.commit("second")
    out = w.play(armed=armed, expect_ok=False)
    assert out["rc"] != 0 and "that anybody may pass through" in out["out"], out["out"]
    assert "sudo chmod 0711 %s" % w.storage in " ".join(out["out"].split()), out["out"]
    assert os.path.exists(marker) and w.head() == "first", "the deploy touched the clone before it refused"
    os.chmod(w.storage, 0o711)                                    # the fix the message prints
    w.play(armed=armed)
    assert w.head() == "second"


def _deploy_tasks_named(*name_starts):
    """The tasks of deploy-stack.yml whose names begin so — one each, blocks descended — in the PLAY's order."""
    with open(DEPLOY, encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    order = []

    def walk(nodes):
        for node in nodes:
            if isinstance(node, dict):
                order.append(node)
                for key in ("tasks", "block"):
                    walk(node.get(key, []))
    walk(plays)
    found = []
    for start in name_starts:
        hits = [t for t in order if str(t.get("name", "")).startswith(start) and "block" not in t]
        assert len(hits) == 1, "%d tasks of deploy-stack.yml are named %r" % (len(hits), start)
        found.append(hits[0])
    return sorted(found, key=lambda t: next(i for i, o in enumerate(order) if o is t))


@pytest.mark.parametrize("armed", [True, False], ids=["armed", "unarmed"])
def test_a_fresh_nodes_storage_root_is_made_roots_before_its_first_store_and_the_clone_is_made(w, armed):
    """On a fresh host-direct node nothing stands at the storage root. Ansible's `file` gives every parent it creates
    the owner and mode of the path it was asked for, so the deploy's first store (the Vector dir, 10002 0700) used
    to make the root 10002's 0700, and the clone file's refusal then stopped the first apply. deploy-stack.yml's
    REAL tasks run here in the play's order — look at the root, make a missing one, the Vector dir, the API's audit
    dir (which makes `api` 1001's on its way), the API host dirs (which make it root's again), the content clone —
    on a node with no storage root: the root ends root's 0711, the Vector dir keeps its
    own owner, and the clone is made by the uid the flag names."""
    shutil.rmtree(w.storage)
    tasks = _deploy_tasks_named("Look at the storage root", "Make a missing storage root",
                                "Ensure the Vector host dir exists", "Ensure the persistent API audit-log dir exists",
                                "Ensure the API host dirs exist")
    tasks.append({"name": "The content clone, as deploy-stack.yml makes it", "ansible.builtin.import_tasks": TASKS,
                  "vars": {"_clone_dir": w.clone, "_clone_service": "api", "_clone_canonical": w.canonical}})
    w.playbook = os.path.join(w.base, "fresh.yml")
    with open(w.playbook, "w", encoding="utf-8") as fh:
        yaml.safe_dump([{"hosts": "localhost", "connection": "local", "gather_facts": False,
                         "vars": {"stack_services": ["vector", "api"],
                                  "kontroll_vector_dir": os.path.join(w.storage, "vector"),
                                  "kontroll_api_audit_dir": os.path.join(w.parent, "audit"),
                                  "kontroll_api_dir": w.parent},
                         "tasks": tasks}], fh, sort_keys=False)
    w.play(armed=armed)
    root, vector = os.stat(w.storage), os.stat(os.path.join(w.storage, "vector"))
    assert (root.st_uid, root.st_gid, root.st_mode & 0o7777) == (0, 0, 0o711), oct(root.st_mode)
    assert (vector.st_uid, vector.st_mode & 0o7777) == (10002, 0o700)
    assert w.head() == "first" and w.owners() == ({SERVICE} if armed else {0})


@pytest.mark.parametrize("armed", [True, False], ids=["armed", "unarmed"])
def test_a_preview_refuses_a_storage_root_closed_to_the_service_as_the_apply_does(w, armed):
    """The mandated `--check --diff` shows what the apply would stop at. On a provisioned node a storage root the
    service's git cannot pass through — root's 0700, what the old printed fix left of a root an older build made
    1001's 0700 — is refused in check mode too, with the fix, and the preview runs no git in the clone. Only a
    MISSING root passes a preview (the apply makes it); an existing closed one must not."""
    w.play(armed=armed)
    os.chown(w.storage, 0, 0)
    os.chmod(w.storage, 0o700)
    w.commit("second")
    out = w.play(armed=armed, check=True, expect_ok=False)
    assert out["rc"] != 0 and "that anybody may pass through" in out["out"], out["out"]
    assert "sudo chmod 0711 %s" % w.storage in " ".join(out["out"].split()), out["out"]
    assert w.head() == "first"
