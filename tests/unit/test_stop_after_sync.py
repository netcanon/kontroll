"""The stop after a sync (T25; SECURITY.md C10): `ansible/playbooks/tasks/stop-after-sync.yml`, imported by
local-canonical.yml (once, at its top) and by `_stop-after-sync.yml`'s play, which bootstrap.yml imports and
deploy-stack.yml holds a copy of, right after the run-id stamp. A play's own sync stops its run itself
(`kontroll-land.py --sync --stop-as`).

WHY: a sync of the worktree to the canonical's `main` — the deploy's own, or one by hand — brings in a tree whose
playbooks and scripts the next run executes as root. The sync leaves a mark in the worktree's own git directory
(`gitio.SYNC_MARK`); a play that imports the stop reads it, says what came in — in plain git, as the operator,
never a script of the worktree — removes it on a real run and fails. These rows hold the tasks file to the sync's own
code (the mark's name, the listing's reads, the escape of what a terminal acts on), bootstrap's and deploy-stack's entry
after the stamp to the stop, and the stop play's variables to local-canonical.yml's; and they run the two shell
scripts the stop renders, in a scratch repository, against `gitio.came_in`: the listing the stop prints must be the one
`kontroll-land.py --sync` printed, line for line. One row shows the stop's message through `ansible-playbook`'s own
display, as the tree's ansible.cfg sets it. The play itself runs as root in
tests/integration/test_local_canonical_as_root.py.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys

import jinja2
import pytest
import yaml

import _ansible_display
from kontroll import gitio

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(ROOT, "ansible", "playbooks")
STOP_TASKS = os.path.join(PLAYBOOKS, "tasks", "stop-after-sync.yml")
_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
_SHELL = pytest.mark.skipif(not (sys.platform.startswith("linux") and shutil.which("bash") and shutil.which("git")),
                            reason="runs the stop's bash scripts and real git (Linux, as the installer does)")


def _load(name):
    with open(os.path.join(PLAYBOOKS, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _stop_tasks():
    with open(STOP_TASKS, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _task(start):
    return next(t for t in _stop_tasks() if t["name"].startswith(start))


def _render(text, **values):
    """A task's text as Ansible renders it for these scripts: plain Jinja, `quote` being `shlex.quote`, and Ansible's
    `match` and `search` tests (`re.match`, `re.search`)."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["quote"] = lambda s: shlex.quote(str(s))
    env.tests["match"] = lambda s, pattern: re.match(pattern, str(s)) is not None
    env.tests["search"] = lambda s, pattern: re.search(pattern, str(s)) is not None
    return env.from_string(text).render(**values)


def _bash(script):
    return subprocess.run(["bash", "-c", script], capture_output=True)


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, env=dict(os.environ, **_WHO))
    assert p.returncode == 0, p.stderr
    return p.stdout.decode("utf-8", "replace").strip()


def _commit(wt, files, subject):
    for rel, text in files.items():
        with open(os.path.join(wt, rel), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", subject)
    return _git(wt, "rev-parse", "HEAD")


@pytest.fixture
def synced(tmp_path):
    """A worktree as a sync leaves it: `before` (HEAD before the sync), then what the canonical's `main` gained —
    21 commits and a newest one that changes a file's mode, adds a symlink, deletes a file and has a subject with
    an ESC and a C1 character in it — named by `refs/remotes/local/main`, and the worktree's own commit replayed
    on top. Returns (the worktree, before, the newest of main's)."""
    wt = str(tmp_path / "worktree")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    before = _commit(wt, {"a.txt": "a\n", "gone.txt": "g\n"}, "the worktree before")
    for i in range(21):
        _commit(wt, {"f.txt": "%d\n" % i}, "filler %d" % i)
    os.chmod(os.path.join(wt, "a.txt"), 0o755)
    os.symlink("a.txt", os.path.join(wt, "link"))
    os.remove(os.path.join(wt, "gone.txt"))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "newest esc\x1b[2J c1\x9b end")
    newest = _git(wt, "rev-parse", "HEAD")
    _git(wt, "update-ref", "refs/remotes/local/main", newest)
    _commit(wt, {"own.txt": "o\n"}, "the worktree's own replayed")
    return wt, before, newest


def test_bootstrap_stops_right_after_the_stamp_with_local_canonicals_own_variables():
    """bootstrap.yml's entries are the run-id stamp (`_log-run-id.yml`), then `_stop-after-sync.yml`, then plays
    only; the stop play runs on localhost, with facts, its one task the import of `tasks/stop-after-sync.yml`; its
    `stop_play` is `bootstrap`; and the variables the stop reads (`worktree`, `operator_uid`, `in_container`,
    `operator_gid`, `as_operator`) are local-canonical.yml's, the same text. Guards the stop's place in bootstrap —
    anything of the worktree before it (the fleet check, the generator, the collections) runs the tree that came in
    before a word is said; only the stamp comes first (I-41: every playbook a run starts with prints its id first,
    and the stop's message names that id) — and the operator the stop runs its git as: a copy that drifted from
    local-canonical's would read or remove the mark as someone else."""
    boot = _load("bootstrap.yml")
    assert boot[0] == {"name": "Stamp the run correlation id",
                       "ansible.builtin.import_playbook": "_log-run-id.yml"}, boot[0]
    assert boot[1] == {"name": "Stop after a sync that brought the worktree forward",
                       "ansible.builtin.import_playbook": "_stop-after-sync.yml"}, boot[1]
    assert all("import_playbook" not in str(sorted(p)) for p in boot[2:]), "the stop is imported once, after the stamp"
    stop = _load("_stop-after-sync.yml")
    assert len(stop) == 1 and stop[0]["hosts"] == "localhost" and stop[0]["connection"] == "local"
    assert stop[0]["gather_facts"] is True, "operator_uid falls back to a gathered fact host-direct"
    assert [sorted(t) for t in stop[0]["tasks"]] == [["ansible.builtin.import_tasks", "name"]]
    assert stop[0]["tasks"][0]["ansible.builtin.import_tasks"] == "tasks/stop-after-sync.yml"
    (canonical,) = [p["vars"] for p in _load("local-canonical.yml") if "hosts" in p]
    names = ("worktree", "operator_uid", "in_container", "operator_gid", "as_operator")
    assert sorted(stop[0]["vars"]) == sorted(names + ("stop_play",)), sorted(stop[0]["vars"])
    for name in names:
        assert stop[0]["vars"][name] == canonical[name], name
    assert stop[0]["vars"]["stop_play"] == "bootstrap"


def test_deploy_stack_stops_right_after_the_stamp_with_the_stop_play_named_as_itself():
    """deploy-stack.yml's first entry is the run-id stamp and its second is `_stop-after-sync.yml`'s play — the same
    keys and values — but its `stop_play`, `deploy-stack`, where the shared play's is `bootstrap`; no later entry
    names the stop play or its tasks file. Guards the stop's place in deploy-stack: its own stop came with
    local-canonical.yml, imported after the root play that installs Docker from the worktree's tasks, so a
    host-direct deploy after a sync by hand ran that play before a word was said (read A-f2, N4); only the stamp
    comes before it (I-41), which runs no task of the worktree but templates the run id from the inventory's vars for
    the first host, which a promote can change, so a fleet vars file that does not parse ends that run before the
    stop speaks (read A-r21, N1); the name the stop says it is; and the copy: held in place because an import's
    `vars:`, which would set the name, reach every play the import brings in and the deploy's walk
    (tests/unit/_deploy_walk.py) refuses them, a copy that drifted from the play bootstrap imports would read or remove
    the mark as someone else, or stop under another name."""
    stack = _load("deploy-stack.yml")
    assert stack[0] == {"name": "Stamp the run correlation id",
                        "ansible.builtin.import_playbook": "_log-run-id.yml"}, stack[0]
    (stop,) = _load("_stop-after-sync.yml")
    assert stop["vars"]["stop_play"] == "bootstrap", stop["vars"]
    assert stack[1] == dict(stop, vars=dict(stop["vars"], stop_play="deploy-stack")), stack[1]
    assert all("stop-after-sync" not in yaml.safe_dump(p) for p in stack[2:]), "the stop comes once, after the stamp"


def test_the_worktree_each_stop_and_sync_names_by_default_is_the_top_of_the_tree_the_play_runs_from():
    """The `worktree` local-canonical.yml and _stop-after-sync.yml give themselves when no caller passes one (every
    production caller: deploy-stack, bootstrap, the installer) is the top of the tree the play runs from. The
    integration rows pass `-e worktree=…`, so nothing else pins the default. Guards a default below the top: the
    play's sync (and its `--check` in the dry-run) then refuses every deploy as "not the top", and the scripts the
    play runs from the worktree are named relative to it (read A-f5: R6-lc-worktree-subdir; _stop-after-sync.yml's
    copy is the same text, pinned with it)."""
    for name in ("local-canonical.yml", "_stop-after-sync.yml"):
        (wt,) = [p["vars"]["worktree"] for p in _load(name) if "hosts" in p]
        got = jinja2.Template(wt).render(playbook_dir=PLAYBOOKS)
        assert os.path.realpath(got) == os.path.realpath(ROOT), (name, wt, got)


def test_the_stop_runs_its_git_as_the_operator_reads_the_syncs_mark_and_escapes_what_it_prints():
    """The tasks file, held to the sync's own code: the mark it reads is `gitio.SYNC_MARK`, in the git directory the
    operator's git names (`--git-dir`, never `--git-path`); every git and every read of the mark runs through `{{
    as_operator }}`, and so does the removal, of the path git named, on a real run only (no `check_mode` key: a command
    is skipped under --check) and only when there is a mark with no rebase in progress beside it; the reads change
    nothing and never fail on their own; the
    listing's count of commits named one by one is `gitio._CAME_IN_COMMITS` and its filter of `--summary` lines is
    `gitio._SUMMARY`; every text the message prints from git or the mark is escaped with `gitio._CONTROL`; the mark's
    path is printed before `head` reads it, and a path with a line break — any character Python's `splitlines` splits on
    — is refused before anything is printed; whether
    HEAD holds the canonical's main is asked as the operator, read-only, beside no rebase in progress; and the stop
    fails when the mark could not be read, or when there is one (a play's own sync stops its run itself, in the sync's
    task: read A-f3, B1). Guards the stop against drifting from the sync — a mark of another name is never seen, a
    listing of other reads says other things than the sync said — and against root's git or root's `rm` in the
    operator's repository, a `--check` that removes the mark, and a subject or path that moves the reader's terminal;
    and a path a line break splits into two (read A-f6, N3; read A-f7, N1: a carriage return, a form feed or U+2028
    split it as a line feed did), a mark removed beside a rebase in progress, after which bootstrap, which refuses no
    operation in progress, ran the tree that came in (read A-f7, N2), a mark of root's whose path the remedy cannot name
    (read
    B-f6, S1), and the HEAD check run as root."""
    read = _task("Read the mark")
    script = read["ansible.builtin.shell"]
    assert '--git-dir)/%s"' % gitio.SYNC_MARK in script and "[ -e .git ] || exit 0" in script, script
    assert "--git-path" not in script, "git resolves a link in the path it names: the mark is never followed"
    for line in script.splitlines():
        if re.search(r"(^|[\s$(])(git|head) ", line):
            assert "{{ as_operator }} git " in line or "{{ as_operator }} head " in line, line
    assert script.lstrip().startswith("set -e"), "a mark that cannot be read fails the read, and the stop with it"
    lines = [ln.strip() for ln in script.splitlines()]
    at = lines.index("first=$({{ as_operator }} head -c 100 -- \"$mark\")")
    assert lines[at - 1] == 'echo "$mark"', "the mark's path is printed before `head` reads it, so exit 1 names it"
    guard = r"case $mark in *[$'\n\r\v\f\x1c\x1d\x1e']*|*$'\xc2\x85'*|*$'\xe2\x80\xa8'*|*$'\xe2\x80\xa9'*)"
    assert lines.index(guard) < lines.index('if [ -L "$mark" ] || { [ -e "$mark" ] && [ ! -f "$mark" ]; }; '
                                            'then'), script
    head = _task("Say whether HEAD holds")
    script = head["ansible.builtin.shell"]
    assert "{{ as_operator }} git merge-base --is-ancestor refs/remotes/local/main HEAD && exit 0" in script, script
    for line in script.splitlines():
        if re.search(r"(^|[\s$(])git ", line):
            assert "{{ as_operator }} git " in line, line
    assert head.get("check_mode") is False and head.get("changed_when") is False and head.get("failed_when") is False
    assert head["when"] == "(_stop_marked | bool) and not (_stop_cut | bool)" and head["register"] == "_stop_head"
    listing = _task("Say what came in")
    script = listing["ansible.builtin.shell"]
    assert 'g() { {{ as_operator }} git "$@"; }' in script
    bare = [ln for ln in script.splitlines() if re.search(r"(^|[\s$(;{])git ", re.sub(r'"[^"]*"', "", ln))]
    assert bare == ['g() { {{ as_operator }} git "$@"; }'], bare
    n = gitio._CAME_IN_COMMITS
    assert "-n %d " % n in script and '[ "$n" -le %d ]' % n in script and "$((n - %d))" % n in script, script
    assert "grep -E '^%s'" % gitio._SUMMARY.pattern in script, script
    for word in ("--no-renames", "--no-ext-diff", "--no-color", "--no-show-signature", "--name-status", "--summary"):
        assert word in script, word
    for task in (read, listing):
        assert task.get("check_mode") is False and task.get("changed_when") is False
        assert task.get("failed_when") is False and task["args"] == {"executable": "/bin/bash"}
    assert listing["when"] == "_stop_before | length > 0"
    remove = _task("Remove the mark")
    assert remove["ansible.builtin.command"]["cmd"] == (
        "{{ as_operator }} rm -f -- {{ _stop_mark.stdout_lines[0] | quote }}")
    assert "check_mode" not in remove and remove["failed_when"] is False
    assert remove["when"] == "(_stop_marked | bool) and not (_stop_cut | bool)", "beside a rebase the mark stays"
    stop = _task("Stop")
    msg = stop["ansible.builtin.fail"]["msg"]
    assert msg.count("regex_replace") == msg.count("'%s', '?')" % gitio._CONTROL.pattern) == 3, msg
    assert stop["when"] == "_stop_mark.rc != 0 or (_stop_marked | bool)"
    assert "stop_synced" not in msg and "before landing, deploying or running the worktree's scripts" in msg, msg
    assert "SECURITY.md C10" in msg and "Nothing was landed or deployed" in msg and "--check --diff" in msg
    before = _task("Name HEAD before")["ansible.builtin.set_fact"]["_stop_before"]
    assert "select('match', '^[0-9a-f]{40}([0-9a-f]{24})?$')" in before, "only a commit id is listed from"


def _stop_message(**values):
    """The stop's message, rendered as Ansible renders it: plain Jinja, with Ansible's `regex_replace`, `bool`,
    `quote` and `dirname` (`os.path.dirname`, as Ansible's own); the HEAD check skipped unless a row gives it."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["regex_replace"] = lambda s, pattern, repl: re.sub(pattern, repl, str(s))
    env.filters["bool"] = lambda v: v is True or str(v).lower() in ("true", "yes", "1")
    env.filters["quote"] = lambda s: shlex.quote(str(s))
    env.filters["dirname"] = os.path.dirname
    given = dict(stop_play="deploy-stack", worktree="/srv/wt", ansible_check_mode=False, _stop_before="",
                 _stop_marked=False, _stop_cut=False, _stop_listing={"stdout_lines": []}, _stop_head={"skipped": True},
                 operator_uid=1000, operator_gid=1000)
    given.update(values)
    return env.from_string(_task("Stop")["ansible.builtin.fail"]["msg"]).render(**given)


def _flagged_remedy(q):
    """The stop's sentence on the tracked files git's status does not compare, for a worktree printed as `q`: gitio's
    `_CUT_SHORT` itself, from the listing to the put-back's full stop, the words between its constants included, so a
    copy that drifts from gitio's text by a word fails the rows that use it, whichever side moved (test_sync's rows pin
    gitio's text). A literal of today's sentence passed a copy and its pin moved together (read B-f11, N1); composed
    from the constants with the words between them a literal of the row's own, gitio's sentence moved by a word, with
    its pin, and passed (read B-f12, N1: B13-gitio-cut-drift-word+pin); and its end was searched from the string's
    start, so a first sentence naming `git rebase --continue` made it return ".", which every arm holds (read B-f13,
    N4: B14-flagged-end-early+stop-drift)."""
    cut = gitio._CUT_SHORT.format(wt=q, lock="LOCK")
    start = cut.index("`" + gitio._LIST_FLAGGED.format(wt=q) + "`")
    own = cut[start:cut.index(". `git rebase --continue`", start)] + "."
    assert len(own) > 200 and own.endswith(">`)."), own
    return own


def test_each_mark_the_stop_cannot_read_has_its_own_remedy_and_only_head_holding_main_is_in_step():
    """The stop's message, rendered. A path no sync writes (the read's exit 3, the path its one line) says to remove it
    by hand, as yourself, naming it (`rm -r -- <path>`, quoted as one word for the shell). A mark `head` could not open
    as the operator (exit 1, the path its one line) says to read the reflog and remove it as yourself (`rm -f --
    <path>`), and names the owner check. A path with a line break (exit 4) says to move it. Exit 1 with no path (the
    `cd` failed), 128 (git could not name the worktree's git directory: a `.git` it cannot read, the owner check) and 2
    say to do what git's sentence says, name the `chown` of the top for git's ownership refusal, and that git's
    `safe.directory` suggestion does not reach the installer's git — never to remove a mark. A rebase in progress
    beside the mark (a replay cut short) says, if it is that, `git -C <worktree> rebase --abort` (the worktree quoted),
    the lock to remove first, the status that names main's files left, the `ls-files -v` that names the tracked files
    the status does not compare — to compare with main's and put HEAD's back with `--ignore-skip-worktree-bits` — and
    that `--continue` can drop the commit it was replaying; and that the mark stays until the abort, under --check too,
    never that it is removed. A mark read and removed: HEAD holding the canonical's main (the HEAD check's exit 0) is
    "in step, unless
    another promote lands first"; HEAD where it was before the sync (exit 3) says the sync did not finish and names the
    status and the flagged files' remedy; any other says HEAD holds neither. Only the first is `in step`, and none but
    exits 3 and 1 says to remove
    a path. Guards the remedy the operator follows: the stop removes no mark it could not read, so every run stops
    there until the operator does (read A-f4, N5); after git's own failure there is no mark to remove (read B-f5, N4),
    and git's suggestion does not reach the deploy's git (read A-f6, N4); exit 1 fell to "in step" or to `rm -r` of a
    literal default, and "no removal mends it" was false for it (read B-f6, S1 and N1: F7-stop-other-gt1,
    F7-stop-rc3-or-1); every exit but 0 is unreadable (read B-f5, S2: M6-n5-rc-eq1); after a replay cut short
    `--continue` landed on `main` without the operator's commit (read A-f5, N1) and the printed abort failed on a
    killed git's lock (read B-f6, N2); and a sync undone, its mark kept, was said to be "in step" with HEAD where it
    was and main's files left untracked (read A-f6, N1; read B-f6, N5); a path with a space or a `$(…)` printed
    unquoted passed every row (fold 6's mutant F6-stop-rc3-unquoted); a stop that named only the status after a replay
    that left main's bytes in a skip-worktree file let the next run run them as root (read A-f7, S1); and a mark removed
    beside a rebase let bootstrap, which refuses none, run the tree that came in (read A-f7, N2); and, since read
    A-f8: each status it names is case for case (`-c core.ignoreCase=false`, S1), the listing prints a path as it is
    and the comparison reads main's file as git's checkout writes it, the put-back covering a missing file (N2, N3),
    and HEAD back names `checkout HEAD -- <path>` for a tracked file a replay quit, not aborted, left (N1)."""
    a, mark = "a" * 40, "/w/.git/KONTROLL_SYNCED"
    path = _stop_message(_stop_mark={"rc": 3, "stdout_lines": [mark],
                                     "stderr_lines": [mark + " is not a file a sync writes"]})
    assert "could not be read (exit 3" in path and "Then remove it by hand, as yourself (`rm -r -- %s`" % mark in path
    assert "in step, unless" not in path and "git's sentence" not in path, path
    odd = "/w x/.git/$(id)"
    said = _stop_message(_stop_mark={"rc": 3, "stdout_lines": [odd], "stderr_lines": [odd + " is not a file"]})
    assert "(`rm -r -- '/w x/.git/$(id)'`" in said, said
    said = _stop_message(_stop_mark={"rc": 1, "stdout_lines": [odd], "stderr_lines": [
        "head: cannot open '%s' for reading: Permission denied" % odd]})
    assert "could not be read (exit 1: head: cannot open" in said and "The mark could not be read as you" in said
    assert "(`rm -f -- '/w x/.git/$(id)'`;" in said and "`git -C /srv/wt reflog`" in said and "§10" in said, said
    assert "in step, unless" not in said and "rm -r --" not in said and "git's sentence" not in said, said
    said = _stop_message(_stop_mark={"rc": 4, "stdout_lines": [], "stderr_lines": [
        "the mark's directory has a line break in its path, which this stop cannot name line by line"]})
    assert "could not be read (exit 4: the mark's directory has a line break" in said, said
    assert "Then move the worktree, or its git directory, to a path with no line break" in said, said
    assert "in step, unless" not in said and "rm -" not in said and "git's sentence" not in said, said
    for rc in (1, 128, 2):
        said = _stop_message(_stop_mark={"rc": rc, "stdout_lines": [],
                                         "stderr_lines": ["fatal: not a git repository: /w/.git/worktrees/x"]})
        assert "could not be read (exit %d" % rc in said and "Then do what git's sentence says" in said, said
        assert "`sudo chown 1000:1000 /srv/wt`" in said and "`safe.directory` suggestion does not reach" in said, said
        assert "in step, unless" not in said and "rm -" not in said and "remove it" not in said, said
    for check in (False, True):     # the removal is skipped beside a rebase: its result says so
        cut = _stop_message(_stop_mark={"rc": 0, "stdout_lines": [mark, a, "/w/.git/rebase-merge"]}, _stop_marked=True,
                            _stop_before=a, _stop_rm={"skipped": True}, _stop_cut=True, worktree="/srv/w t",
                            ansible_check_mode=check)
        assert "A rebase is in progress in the worktree. If it is a sync's replay cut short" in cut, cut
        assert "`git -C '/srv/w t' rebase --abort` puts HEAD back" in cut, cut
        assert "(if git says /w/.git/index.lock exists and no git is running, remove that file first;" in cut, cut
        assert ("`git -C '/srv/w t' -c core.ignoreCase=false status --untracked-files=all --ignored` names, among "
                "your own" in cut), cut
        assert _flagged_remedy("'/srv/w t'") in cut, cut
        assert "can drop the commit it was replaying" in cut and "in step, unless" not in cut, cut
        assert ("The mark (%s) stays while the rebase is in progress: every run stops here until it is aborted" % mark
                in cut and "removed" not in cut and "Under --check" not in cut), cut
    held = dict(_stop_mark={"rc": 0, "stdout_lines": [mark, a]}, _stop_marked=True, _stop_before=a,
                _stop_rm={"rc": 0})
    read = _stop_message(_stop_head={"rc": 0}, **held)
    assert "The mark is removed, so the next run goes on." in read and "in step, unless another promote" in read, read
    assert "remove it by hand" not in read and "HEAD before the sync: %s;" % a in read, read
    back = _stop_message(_stop_head={"rc": 3}, **held)
    assert "The sync did not finish: HEAD is where it was before it" in back, back
    assert ("`git -C /srv/wt -c core.ignoreCase=false status --untracked-files=all --ignored` names, among your own"
            in back), back
    assert _flagged_remedy("/srv/wt") in back, back
    assert "(the replay was undone, or cut short and then aborted or quit)" in back, back
    assert ("(a replay quit, `git rebase --quit`, rather than aborted): `git -C /srv/wt checkout HEAD -- <path>` puts "
            "HEAD's back." in back), back
    assert "in step" not in back and "rm -" not in back, back
    for rc in (1, 128):
        other = _stop_message(_stop_head={"rc": rc}, **held)
        assert "HEAD does not hold the canonical's main, nor is it the commit before the sync" in other, other
        assert "in step" not in other and "rm -" not in other, other


def test_the_stop_names_the_stamps_id_before_a_callers_and_none_when_each_is_empty():
    """The stop's message, rendered with the mark read and HEAD holding main: with the stamp's `_kontroll_run_id`, and
    with it and a caller's `kontroll_run_id`, it ends `reflog`. kontroll_run_id=<the stamp's>`; with the caller's
    alone, or the stamp's empty and the caller's set, the caller's; with neither, or each empty, it names no
    `kontroll_run_id` at all. Guards the order and the emptiness of the id the stop names (read B-r21, N4): the
    stamp's is the one the run printed first, where a caller's may be a template that renders anew, and
    `kontroll_run_id=` with nothing after it names no run."""
    held = dict(_stop_mark={"rc": 0, "stdout_lines": ["/srv/wt/.git/KONTROLL_SYNCED", "a" * 40]}, _stop_marked=True,
                _stop_before="a" * 40, _stop_rm={"rc": 0}, _stop_head={"rc": 0})
    stamp, caller = "20260101T000000Z", "caller-run-id"
    cases = [({"_kontroll_run_id": stamp}, stamp), ({"_kontroll_run_id": stamp, "kontroll_run_id": caller}, stamp),
             ({"kontroll_run_id": caller}, caller), ({"_kontroll_run_id": "", "kontroll_run_id": caller}, caller),
             ({}, None), ({"kontroll_run_id": ""}, None), ({"_kontroll_run_id": "", "kontroll_run_id": ""}, None)]
    for given, want in cases:
        said = _stop_message(**dict(held, **given))
        if want:
            assert said.endswith("reflog`. kontroll_run_id=" + want), (given, said[-160:])
        else:
            assert "kontroll_run_id" not in said and said.endswith("reflog`."), (given, said[-160:])


def test_a_rebase_beside_the_mark_is_a_replay_cut_short_and_an_empty_mark_is_a_mark():
    """The facts the stop names from its read, rendered: `_stop_cut` is true when a line after the mark's first names
    `rebase-merge` or `rebase-apply` (beside an empty first line too), and false when none does or the read did not
    exit 0, or the only line naming one is the mark's first (a hand-made mark); `_stop_marked` is true for a mark
    whose read printed only its path — an empty mark (Ansible's stdout drops the empty first line). Guards the reading
    of a replay cut short (read A-f5, N1), and `_stop_marked` against counting a first line (read A-f5:
    R6-stop-empty-first-line); and the cut read from the lines after the first only (reads A-f6 and B-f6:
    F6-stop-cut-first-line-too, equivalent for every mark a sync writes)."""
    facts = _task("Name HEAD before")["ansible.builtin.set_fact"]
    m = "/w/.git/KONTROLL_SYNCED"
    for lines, rc, cut in (([m, "a" * 40, "/w/.git/rebase-merge"], 0, True), ([m, "", "/w/.git/rebase-apply"], 0, True),
                           ([m, "a" * 40], 0, False), ([m], 0, False), ([m], 3, False),
                           ([m, "x/rebase-merge"], 0, False),
                           ([m, "a" * 40, "/w/.git/rebase-merge"], 1, False)):
        got = _render(facts["_stop_cut"], _stop_mark={"rc": rc, "stdout_lines": lines}).strip()
        assert got == str(cut), (lines, rc, got)
    assert _render(facts["_stop_marked"], _stop_mark={"rc": 0, "stdout_lines": [m]}).strip() == "True"


@_SHELL
def test_an_empty_mark_beside_a_rebase_is_read_as_a_replay_cut_short(tmp_path):
    """The read script, run on a repository holding an EMPTY mark and a `rebase-merge` beside it: exit 0, and the
    lines Ansible makes of its output (stdout without its last line feed, split) render `_stop_cut` and `_stop_marked`
    true. Guards the empty first line the read prints: dropped, the rebase moves to the line `_stop_cut` does not read,
    and the stop says "in step" beside a rebase in progress (read B-f6: F7-stop-empty-line-dropped; the facts row
    renders `[m, "", rebase-apply]` by hand)."""
    wt = str(tmp_path / "wt")
    subprocess.run(["git", "init", "-q", wt], check=True, capture_output=True)
    open(os.path.join(wt, ".git", gitio.SYNC_MARK), "w").close()
    os.mkdir(os.path.join(wt, ".git", "rebase-merge"))
    p = _bash(_render(_task("Read the mark")["ansible.builtin.shell"], worktree=wt, as_operator=""))
    lines = p.stdout.decode("utf-8", "replace").rstrip("\r\n").splitlines()
    facts = _task("Name HEAD before")["ansible.builtin.set_fact"]
    assert p.returncode == 0, p.stderr
    assert _render(facts["_stop_cut"], _stop_mark={"rc": 0, "stdout_lines": lines}).strip() == "True", lines
    assert _render(facts["_stop_marked"], _stop_mark={"rc": 0, "stdout_lines": lines}).strip() == "True", lines


@_SHELL
def test_the_read_names_a_rebase_beside_the_mark_that_is_a_dangling_link(tmp_path):
    """The read with a mark and, beside it, a `rebase-merge` that is a link to nothing (as the sync's busy check reads
    it: `lexists`): exit 0, the mark's first line, then the link's path, so the stop says a rebase is in progress.
    Guards the read's `-L` beside its `-e`: without it a dangling link is not there, and the stop says "in step" where
    the sync refuses (read A-f6: A7-stop-read-cut-link-off)."""
    wt = str(tmp_path / "w")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    head = _commit(wt, {"a.txt": "a\n"}, "a")
    with open(os.path.join(wt, ".git", gitio.SYNC_MARK), "w", encoding="ascii") as fh:
        fh.write(head + "\n")
    os.symlink(str(tmp_path / "nothing"), os.path.join(wt, ".git", "rebase-merge"))
    p = _bash(_render(_task("Read the mark")["ansible.builtin.shell"], worktree=wt, as_operator=""))
    lines = p.stdout.decode().splitlines()
    assert p.returncode == 0 and lines[1] == head and lines[2:] == [os.path.join(wt, ".git", "rebase-merge")], lines


@_SHELL
@pytest.mark.parametrize("sep", ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
                         ids=["LF", "CR", "VT", "FF", "FS", "GS", "RS", "NEL", "LS", "PS"])
def test_the_read_refuses_a_git_directory_whose_path_holds_a_line_break_before_it_prints_anything(tmp_path, sep):
    """A worktree at `<tmp>/n<sep>l` holding a mark, `<sep>` each character Python's `str.splitlines` splits on (how
    Ansible makes `stdout_lines`): a line feed, a carriage return, a vertical tab, a form feed, the three information
    separators, NEL, U+2028 and U+2029. The read exits 4, prints nothing on its output, and says on its error output
    that the path has a line break. Guards the stop's line-by-line reading of what the read prints: the path split at
    the line feed made the second half the mark's first line, the exit-3 remedy named `rm -r -- <tmp>/n` (another
    directory), and the removal task removed a decoy and said the mark was gone while it stayed (read A-f6, N3); a
    guard on the line feed alone let each of the others split the path the same way (read A-f7, N1)."""
    assert len(("n" + sep + "l").splitlines()) == 2, "Ansible's stdout_lines splits the path there"
    wt = str(tmp_path / ("n" + sep + "l"))
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    with open(os.path.join(wt, ".git", gitio.SYNC_MARK), "w", encoding="ascii") as fh:
        fh.write("a" * 40 + "\n")
    p = _bash(_render(_task("Read the mark")["ansible.builtin.shell"], worktree=wt, as_operator=""))
    assert p.returncode == 4 and p.stdout == b"", (p.returncode, p.stdout)
    assert b"has a line break in its path" in p.stderr, p.stderr


@_SHELL
def test_a_mark_the_operator_cannot_read_is_named_before_the_read_fails(tmp_path):
    """The read run with a `head` that cannot open the mark (a `head` first on the PATH the operator's prefix sets,
    which fails as `head` fails on a mark of root's, mode 0600): exit 1, and the mark's path is the one line of its
    output, so the stop names it in its `rm -f --` remedy. Guards the order of the read: the path printed after
    `head` read the mark left exit 1 with no path, and the remedy could name none (read B-f6, S1 and N1)."""
    wt, fake = str(tmp_path / "w"), tmp_path / "bin"
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    mark = os.path.join(wt, ".git", gitio.SYNC_MARK)
    with open(mark, "w", encoding="ascii") as fh:
        fh.write("a" * 40 + "\n")
    fake.mkdir()
    say = "echo \"head: cannot open '$4' for reading: Permission denied\" >&2"   # $4: the mark
    (fake / "head").write_text("#!/bin/sh\n%s\nexit 1\n" % say, encoding="ascii")
    os.chmod(str(fake / "head"), 0o755)
    prefix = "env PATH=%s:%s" % (fake, os.environ.get("PATH", "/usr/bin:/bin"))
    p = _bash(_render(_task("Read the mark")["ansible.builtin.shell"], worktree=wt, as_operator=prefix))
    assert p.returncode == 1 and p.stdout.decode().splitlines() == [mark], (p.returncode, p.stdout, p.stderr)
    assert b"cannot open" in p.stderr, p.stderr


@_SHELL
def test_the_head_check_says_whether_head_holds_main_or_is_where_it_was_before_the_sync(synced):
    """The HEAD check, rendered as the play renders it and run in a worktree a sync left: exit 0 (HEAD descends from
    `refs/remotes/local/main`); with HEAD reset to the commit before the sync, exit 3; with HEAD on a commit of its
    own off that one, exit 1; with HEAD back but no commit id known before (`_stop_before` empty), exit 1. Guards
    the stop's "in step" on what HEAD holds: a sync undone — its mark kept because the undo could not show the
    worktree put back — was said to be in step, with HEAD where it was and main's files left in the worktree
    (read A-f6, N1; read B-f6, N5)."""
    wt, before, newest = synced
    script = _task("Say whether HEAD holds")["ansible.builtin.shell"]

    def rc(since):
        return _bash(_render(script, worktree=wt, _stop_before=since, as_operator="")).returncode
    assert rc(before) == 0
    _git(wt, "reset", "-q", "--hard", before)
    assert rc(before) == 3 and rc("") == 1
    _commit(wt, {"aside.txt": "a\n"}, "a commit of HEAD's own off the one before")
    assert rc(before) == 1


@_SHELL
def test_the_listing_the_stop_prints_is_what_the_sync_printed(synced):
    """The listing script, rendered as the play renders it and run in a worktree a sync left (22 commits on
    `main` since HEAD before; the newest changes a file's mode, adds a symlink, deletes a file, and carries an ESC
    and a C1 character in its subject), says, once escaped as the stop escapes it, exactly what `gitio.came_in`
    says — the newest 20 commits, `and 2 older commit(s) not shown`, every path, the mode change and the symlink —
    and not the worktree's own replayed commit. Guards the stop's listing as the twin of the sync's: the stop runs
    when the sync's own listing scrolled by or was never seen (a hand sync's terminal, a killed run), so what it
    says must be the same reads, and a writer of `main` must not hide a commit past a cut or move the terminal."""
    wt, before, newest = synced
    p = _bash(_render(_task("Say what came in")["ansible.builtin.shell"], worktree=wt, _stop_before=before,
                      as_operator=""))
    assert p.returncode == 0, p.stderr
    lines = p.stdout.decode("utf-8", "replace").splitlines()
    shown = [gitio._CONTROL.sub("?", ln) for ln in lines]
    assert shown == gitio.came_in(wt, before), (shown, gitio.came_in(wt, before))
    assert len([ln for ln in lines if ln.startswith(("filler", newest[:7]))]) <= 20
    assert shown[0] == "%s t: newest esc?[2J c1? end" % _git(wt, "rev-parse", "--short", newest), shown[0]
    assert shown[20] == "and 2 older commit(s) not shown", shown[20]
    for line in ("mode change 100644 => 100755 a.txt", "create mode 120000 link", "D\tgone.txt", "A\town.txt"):
        assert line in shown, line
    assert not any("the worktree's own replayed" in ln for ln in shown)


@_SHELL
def test_the_listing_says_git_could_not_list_it(synced, tmp_path):
    """A HEAD before that the worktree does not hold, or a worktree that is not there: the listing prints `git
    could not list it` and nothing else, and exits 0 (the stop then says so). Guards the `&&` chain of the listing:
    a read that failed half-way must not print half a listing as if it were all that came in."""
    wt, before, newest = synced
    script = _task("Say what came in")["ansible.builtin.shell"]
    for where, since in ((wt, "0" * 40), (str(tmp_path / "nowhere"), before)):
        p = _bash(_render(script, worktree=where, _stop_before=since, as_operator=""))
        assert p.returncode == 0 and p.stdout.decode().splitlines() == ["git could not list it"], (where, p.stdout)


@_SHELL
def test_the_mark_is_read_as_its_path_and_first_line_and_one_that_cannot_be_read_fails(synced, tmp_path):
    """The read of the mark: no git directory, or no mark, prints nothing and exits 0; a mark prints its path (in
    the worktree's own git directory, where `gitio` writes it) and its first line only, and, with a rebase in
    progress beside it, that rebase's path as a third line (a replay cut short); a mark that is a symlink — to a
    file that holds a commit id, or dangling — or a directory makes the read exit 3, its path its one line, with `is
    not a file a sync writes`, which the stop reads as a path to remove by hand and stops on, and the link's target
    is untouched. Guards the read's answers — not synced, synced since HEAD X, cut short, not known — and the
    fail-closed one: git's `--git-path` names a link's TARGET, so a dangling link read as "no mark" and the run went
    on, and a link to a file was read, and would have been removed, at its target; and exit 3 as the read's own,
    which the stop's remedy keys on (read B-f5, N4)."""
    wt, before, newest = synced
    script = _task("Read the mark")["ansible.builtin.shell"]

    def read(where):
        p = _bash(_render(script, worktree=where, as_operator=""))
        return p.returncode, p.stdout.decode("utf-8", "replace").splitlines()

    plain = str(tmp_path / "plain")
    os.mkdir(plain)
    assert read(plain) == (0, [])
    assert read(wt) == (0, [])
    mark = os.path.join(wt, ".git", gitio.SYNC_MARK)
    with open(mark, "w", encoding="ascii", newline="\n") as fh:
        fh.write(before + "\n" + newest + "\n")
    rc, lines = read(wt)
    assert rc == 0 and len(lines) == 2 and os.path.samefile(lines[0], mark) and lines[1] == before, lines
    assert gitio.synced_since(wt) == before, "the sync's own reader reads the same first line"
    for cut in ("rebase-merge", "rebase-apply"):
        os.mkdir(os.path.join(wt, ".git", cut))
        rc, lines = read(wt)
        assert rc == 0 and lines[1] == before and lines[2:] == [os.path.join(os.path.dirname(lines[0]), cut)], lines
        os.rmdir(os.path.join(wt, ".git", cut))
    os.remove(mark)
    target = tmp_path / "elsewhere.txt"
    target.write_text(before + "\n", encoding="ascii")
    for planted in (str(target), str(tmp_path / "absent"), None):
        if planted:
            os.symlink(planted, mark)
        else:
            os.mkdir(mark)
        p = _bash(_render(script, worktree=wt, as_operator=""))
        assert p.returncode == 3 and b"is not a file a sync writes" in p.stderr, (planted, p)
        named = p.stdout.decode("utf-8", "replace").splitlines()
        assert len(named) == 1 and os.path.basename(named[0]) == gitio.SYNC_MARK, (planted, named)
        assert os.path.samefile(os.path.dirname(named[0]), os.path.dirname(mark)), (planted, named)
        assert gitio.synced_since(wt) is None, planted
        (os.rmdir if planted is None else os.remove)(mark)
    assert target.read_text(encoding="ascii") == before + "\n" and not os.path.lexists(str(tmp_path / "absent"))


@pytest.fixture
def fresh(tmp_path):
    """A worktree with one commit, HEAD before a sync: (the worktree, its id). It holds `r.txt`, forty lines."""
    wt = str(tmp_path / "fresh")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    return wt, _commit(wt, {"r.txt": "".join("line %d\n" % i for i in range(40))}, "before")


def _onto_main(wt):
    """Name HEAD as the canonical's main in the worktree, as a sync leaves it."""
    _git(wt, "update-ref", "refs/remotes/local/main", _git(wt, "rev-parse", "HEAD"))


def _listing(wt, before):
    """The stop's listing, rendered as the play renders it and run in `wt`, escaped as the stop escapes it."""
    p = _bash(_render(_task("Say what came in")["ansible.builtin.shell"], worktree=wt, _stop_before=before,
                      as_operator=""))
    assert p.returncode == 0, p.stderr
    return [gitio._CONTROL.sub("?", ln) for ln in p.stdout.decode("utf-8", "replace").splitlines()]


@_SHELL
def test_a_moved_path_is_named_where_it_was_and_where_it_is_by_both_listings(fresh):
    """A path moved since HEAD before (`git mv`, the same bytes): `gitio.came_in` says `D r.txt` and `A
    moved.txt`, and the stop's listing says the same lines. Guards `--no-renames` in both reads: each path that
    changed is named on a line of its own, and the two listings are one — with git's rename detection the sync said
    `R100 r.txt moved.txt` where the stop said two lines (read B-f2, S3: CI1)."""
    wt, before = fresh
    _git(wt, "mv", "r.txt", "moved.txt")
    _git(wt, "commit", "-q", "-m", "moved")
    _onto_main(wt)
    got = gitio.came_in(wt, before)
    assert "A\tmoved.txt" in got and "D\tr.txt" in got, got
    assert _listing(wt, before) == got


@_SHELL
def test_exactly_as_many_commits_as_are_named_have_no_older_line(fresh):
    """`gitio._CAME_IN_COMMITS` commits since HEAD before: both listings name every one, neither has an `older
    commit(s) not shown` line, and they are the same. Guards the cut at exactly the count: `and 0 older commit(s)
    not shown` says something was cut when nothing was (read B-f2, S3: CI4)."""
    wt, before = fresh
    for i in range(gitio._CAME_IN_COMMITS):
        _commit(wt, {"f.txt": "%d\n" % i}, "c%d" % i)
    _onto_main(wt)
    got = gitio.came_in(wt, before)
    assert not any("older commit" in ln for ln in got) and _listing(wt, before) == got, got
    assert len([ln for ln in got if " t: c" in ln]) == gitio._CAME_IN_COMMITS, got


def test_each_command_the_stop_prints_quotes_a_worktree_and_a_git_directory_whose_paths_hold_a_space():
    """The stop's message for a linked worktree at `/srv/w t` whose own git directory is `/srv/c t/.git/worktrees/w`
    (a space in each): the cut-short arm's lock is `<that directory>/index.lock`, quoted; the closing reflog, the exit-1
    arm's reflog, git's arm's `sudo chown` of the top, HEAD-back's status and HEAD-neither's merge-base and status each
    name the worktree quoted. Guards each command the operator pastes: unquoted, a path with a space is two words — the
    pasted `sudo chown` re-owns two other paths, as root — and the suite's rows rendered `/srv/wt` and `/w/.git`, which
    quoting leaves as they are, so dropping any one quote passed (read B-f7, N1: F8-stop-cut-lock-unquoted,
    -last-reflog-unquoted, -rc1-reflog-unquoted, -chown-unquoted, -head3-status-unquoted, -else-unquoted); each status
    pinned as it now reads, case for case (read A-f8, S1); and HEAD-back's sentence on the tracked files git's status
    does not compare, whole, and its put-back after a replay quit, each command quoted: the remedy row renders that arm
    with `/srv/wt`, so its four quotes were held by no row (read B-f8, N2: B9-stop-head3-*-unquoted)."""
    a, wt, own = "a" * 40, "/srv/w t", "/srv/c t/.git/worktrees/w"
    mark = own + "/" + gitio.SYNC_MARK
    cut = _stop_message(worktree=wt, _stop_mark={"rc": 0, "stdout_lines": [mark, a, own + "/rebase-merge"]},
                        _stop_marked=True, _stop_before=a, _stop_rm={"skipped": True}, _stop_cut=True)
    assert "(if git says '%s/index.lock' exists and no git is running" % own in cut, cut
    held = dict(worktree=wt, _stop_mark={"rc": 0, "stdout_lines": [mark, a]}, _stop_marked=True, _stop_before=a,
                _stop_rm={"rc": 0})
    assert "and in `git -C '/srv/w t' reflog`." in _stop_message(_stop_head={"rc": 0}, **held)
    said = _stop_message(worktree=wt, _stop_mark={"rc": 1, "stdout_lines": [mark], "stderr_lines": [
        "head: cannot open '%s' for reading: Permission denied" % mark]})
    assert "(`git -C '/srv/w t' reflog` names the HEAD before each sync)" in said, said
    said = _stop_message(worktree=wt, _stop_mark={"rc": 128, "stdout_lines": [], "stderr_lines": [
        "fatal: detected dubious ownership in repository at '/srv/w t'"]})
    assert "`sudo chown 1000:1000 '/srv/w t'`" in said, said
    back = _stop_message(_stop_head={"rc": 3}, **held)
    assert "`git -C '/srv/w t' -c core.ignoreCase=false status --untracked-files=all --ignored` names" in back, back
    assert _flagged_remedy("'/srv/w t'") in back, back
    assert "`git -C '/srv/w t' checkout HEAD -- <path>` puts HEAD's back." in back, back
    other = _stop_message(_stop_head={"rc": 1}, **held)
    assert "`git -C '/srv/w t' merge-base --is-ancestor refs/remotes/local/main HEAD`" in other, other
    assert "`git -C '/srv/w t' -c core.ignoreCase=false status` and the reflog" in other, other


@_SHELL
@pytest.mark.parametrize("sep", ["\n", "\u2028"], ids=["LF", "LS"])
def test_a_separate_git_directory_with_a_line_break_is_refused_though_the_worktree_has_none(tmp_path, sep):
    """A worktree with no line break in its path whose separate git directory (`<tmp>/g<sep>d`) holds one, and a
    mark: the read exits 4 and prints nothing. Guards the guard's reading of the path the operator's git names (the
    mark's), not the worktree's: the suite's row put the line break in the worktree's own name, where its git directory
    has it too, so a guard on the worktree's path passed (read B-f7: F8-stop-lf-guard-pwd)."""
    gd, wt = str(tmp_path / ("g" + sep + "d")), str(tmp_path / "wt")
    subprocess.run(["git", "init", "-q", "--separate-git-dir", gd, wt], check=True, capture_output=True)
    with open(os.path.join(gd, gitio.SYNC_MARK), "w", encoding="ascii") as fh:
        fh.write("a" * 40 + "\n")
    p = _bash(_render(_task("Read the mark")["ansible.builtin.shell"], worktree=wt, as_operator=""))
    assert p.returncode == 4 and p.stdout == b"", (p.returncode, p.stdout, p.stderr)


@_SHELL
def test_every_status_the_stop_prints_names_a_file_of_mains_whose_name_differs_from_a_tracked_ones_only_in_case(
        tmp_path):
    """A worktree at `<tmp>/w t` with `core.ignoreCase` true, a tracked `ansible/filter_plugins/kontroll_token.py` and,
    beside it, an untracked `Kontroll_token.py` (a file of main's a replay left): every `git … status …` the stop
    prints — in its cut-short arm, its HEAD-back arm and its HEAD-neither arm — and the sync's own cut-short sentence
    (`gitio._CUT_SHORT`), each taken from between its backquotes and run as printed, names that file; git's own
    `status --untracked-files=all --ignored` there does not (the premise). Guards the status the operator is told
    names main's files the replay left: case-blind, it hid the filter plugin, the real run then removed the mark, and
    the next dry-run loaded the plugin as root (read A-f8, S1)."""
    wt = str(tmp_path / "w t")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    plugins = os.path.join(wt, "ansible", "filter_plugins")
    os.makedirs(plugins)
    _commit(wt, {"ansible/filter_plugins/kontroll_token.py": "own\n"}, "own")
    _git(wt, "config", "core.ignoreCase", "true")
    with open(os.path.join(plugins, "Kontroll_token.py"), "w", encoding="utf-8") as fh:
        fh.write("main's\n")
    plain = subprocess.run(["git", "-C", wt, "status", "--untracked-files=all", "--ignored"], capture_output=True)
    assert plain.returncode == 0 and b"Kontroll_token.py" not in plain.stdout, plain.stdout
    a, mark = "a" * 40, os.path.join(wt, ".git", gitio.SYNC_MARK)
    held = dict(worktree=wt, _stop_mark={"rc": 0, "stdout_lines": [mark, a]}, _stop_marked=True, _stop_before=a,
                _stop_rm={"rc": 0})
    cut = {"rc": 0, "stdout_lines": [mark, a, wt + "/.git/rebase-merge"]}
    said = {"cut": _stop_message(worktree=wt, _stop_mark=cut, _stop_marked=True, _stop_before=a,
                                 _stop_rm={"skipped": True}, _stop_cut=True),
            "head-back": _stop_message(_stop_head={"rc": 3}, **held),
            "head-neither": _stop_message(_stop_head={"rc": 1}, **held),
            "sync": gitio._CUT_SHORT.format(wt=shlex.quote(wt),
                                            lock=shlex.quote(os.path.join(wt, ".git", "index.lock")))}
    for arm, text in said.items():
        statuses = [c for c in re.findall(r"`([^`]*)`", text) if re.search(r" status( |$)", c)]
        assert statuses, (arm, text)
        for command in statuses:
            p = _bash(command)
            assert p.returncode == 0 and b"Kontroll_token.py" in p.stdout, (arm, command, p.stdout, p.stderr)


@pytest.mark.skipif(not (_ansible_display.PLAYBOOK and sys.platform.startswith("linux")),
                    reason="runs ansible-playbook (Linux)")
def test_each_command_the_stop_prints_is_shown_as_written_by_ansibles_yaml_display(tmp_path):
    """The stop's message, rendered for a worktree at `/srv/w t/it's $(touch INJ)` (a space, a quote and a command
    substitution) in each arm that prints a command — a replay cut short, HEAD back, HEAD neither, in step, a path no
    sync writes (exit 3), a mark the operator cannot read (exit 1), git's own failure (128), a line break (4) — and
    shown by `ansible-playbook` as the tree's ansible.cfg shows a failed task: every command between backquotes in
    each message is on one line of the result block it prints, as written (not of 2.19's raw `[ERROR]` line, which
    holds every command whatever the block does); and each `git … reflog` or `git … status` taken from
    those lines and run, from a scratch directory, creates no `INJ` there. Guards the commands the operator pastes
    from the run's own output: a message of one line is shown as a YAML quoted scalar, each `'` doubled and the line
    wrapped mid-command, so the quoted path came apart and its `$(…)` ran when pasted, and five commands in eight were
    split over two lines (read A-f8, S2)."""
    a, wt = "a" * 40, "/srv/w t/it's $(touch INJ)"
    own = wt + "/.git"
    mark = own + "/" + gitio.SYNC_MARK
    held = dict(worktree=wt, _stop_mark={"rc": 0, "stdout_lines": [mark, a]}, _stop_marked=True, _stop_before=a,
                _stop_rm={"rc": 0})
    messages = [
        _stop_message(worktree=wt, _stop_mark={"rc": 0, "stdout_lines": [mark, a, own + "/rebase-merge"]},
                      _stop_marked=True, _stop_before=a, _stop_rm={"skipped": True}, _stop_cut=True),
        _stop_message(_stop_head={"rc": 3}, **held), _stop_message(_stop_head={"rc": 1}, **held),
        _stop_message(_stop_head={"rc": 0}, **held),
        _stop_message(worktree=wt, _stop_mark={"rc": 3, "stdout_lines": [mark], "stderr_lines": [mark + " is not"]}),
        _stop_message(worktree=wt, _stop_mark={"rc": 1, "stdout_lines": [mark], "stderr_lines": ["head: cannot"]}),
        _stop_message(worktree=wt, _stop_mark={"rc": 128, "stdout_lines": [], "stderr_lines": ["fatal: dubious"]}),
        _stop_message(worktree=wt, _stop_mark={"rc": 4, "stdout_lines": [], "stderr_lines": ["a line break"]})]
    out = _ansible_display.shown(tmp_path, messages)
    lines = out.splitlines()
    for msg in messages:
        commands = re.findall(r"`([^`]*)`", msg)
        assert any("git -C %s " % shlex.quote(wt) in c for c in commands), msg
        for command in commands:
            assert any(command in line for line in lines), (command, out)
    pasted = [c for line in lines for c in re.findall(r"`([^`]*)`", line)
              if c.startswith("git -C ") and re.search(r" (reflog|status)( |$)", c)]
    assert pasted, out
    for command in pasted:
        subprocess.run(["bash", "-c", command], capture_output=True, cwd=str(tmp_path))
    assert not (tmp_path / "INJ").exists(), "a command pasted from the display ran the path's substitution"


@_SHELL
@pytest.mark.parametrize("first,before", [("a" * 40 + "\r/w/.git/rebase-merge", ""), ("a" * 40 + "\r", "a" * 40)],
                         ids=["cr-inside", "crlf"])
def test_a_carriage_return_in_the_marks_first_line_fakes_no_rebase_and_one_at_its_end_is_dropped(tmp_path, first,
                                                                                                 before):
    """The read script, run on a mark whose first line holds a carriage return: one inside it (`<id>`, a carriage
    return, `/w/.git/rebase-merge`) is a first line that is not a commit id — the read prints it empty, so `_stop_cut`
    is false, HEAD before the sync is not known and the mark is still a mark; one at its end (a mark saved with CRLF)
    is dropped, and the id is read. Guards the lines Ansible makes of what the read prints: it splits at a carriage
    return too, so the first line printed whole became two, its second naming a rebase beside the mark — the stop then
    said a replay was cut short where none was, and kept the mark (read A-f8, N4)."""
    wt = str(tmp_path / "w")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    with open(os.path.join(wt, ".git", gitio.SYNC_MARK), "w", encoding="ascii", newline="") as fh:
        fh.write(first + "\n")
    p = _bash(_render(_task("Read the mark")["ansible.builtin.shell"], worktree=wt, as_operator=""))
    assert p.returncode == 0, p.stderr
    lines = p.stdout.decode("utf-8", "replace").rstrip("\r\n").splitlines()
    facts = _task("Name HEAD before")["ansible.builtin.set_fact"]
    read = {"rc": 0, "stdout_lines": lines}
    assert _render(facts["_stop_cut"], _stop_mark=read).strip() == "False", lines
    assert _render(facts["_stop_before"], _stop_mark=read).strip() == before, lines
    assert _render(facts["_stop_marked"], _stop_mark=read).strip() == "True", lines


@_SHELL
def test_a_mark_of_two_syncs_saved_with_crlf_reads_its_first_id(tmp_path):
    """A mark saved with CRLF after two syncs (`<id>` CR LF `<id2>` CR LF): the read drops the first line's CR and
    names `<id>` as HEAD before the sync. Guards the CR strip on each line: moved before the cut at the first LF, it
    stripped the whole read's trailing CR, the first line kept its own, and the id read as not known (read B-f9's
    mutant B10-read-cr-before-lf; its kill row)."""
    wt = str(tmp_path / "w")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    with open(os.path.join(wt, ".git", gitio.SYNC_MARK), "w", encoding="ascii", newline="") as fh:
        fh.write("a" * 40 + "\r\n" + "b" * 40 + "\r\n")
    p = _bash(_render(_task("Read the mark")["ansible.builtin.shell"], worktree=wt, as_operator=""))
    assert p.returncode == 0, p.stderr
    lines = p.stdout.decode("utf-8", "replace").rstrip("\r\n").splitlines()
    facts = _task("Name HEAD before")["ansible.builtin.set_fact"]
    read = {"rc": 0, "stdout_lines": lines}
    assert _render(facts["_stop_before"], _stop_mark=read).strip() == "a" * 40, lines


@_SHELL
def test_the_stop_reads_a_linked_worktrees_mark_in_its_own_git_directory(tmp_path):
    """The stop's read of the mark, rendered and run on a worktree linked with `git worktree add` whose sync left its
    mark in the linked worktree's own git directory (`.git/worktrees/<name>`, where kontroll-land writes it: test_sync's
    linked-worktree row): it prints that path and the mark's commit id; run on the main worktree beside it, it prints
    nothing. Guards the read against asking git for the common directory (`--git-common-dir`), where a linked
    worktree's mark is not: the run after a sync from a linked worktree would go on, without the stop, into the tree
    that came in, its playbooks and scripts run as root (SECURITY.md C10). Only a pin on the read's text killed that
    mutant (read A-f11's mutant A12-stop-mark-common-dir; its kill row)."""
    main, linked = str(tmp_path / "main"), str(tmp_path / "linked")
    _git(str(tmp_path), "init", "-q", "-b", "main", main)
    _commit(main, {"a.txt": "a\n"}, "seed")
    _git(main, "worktree", "add", "-q", "-b", "lk", linked)
    own = _git(linked, "rev-parse", "--path-format=absolute", "--git-dir")
    assert own != _git(main, "rev-parse", "--path-format=absolute", "--git-dir")
    with open(os.path.join(own, gitio.SYNC_MARK), "w", encoding="ascii") as fh:
        fh.write("a" * 40 + "\n")
    script = _task("Read the mark")["ansible.builtin.shell"]
    p = _bash(_render(script, worktree=linked, as_operator=""))
    assert p.returncode == 0, p
    assert p.stdout.decode("utf-8").splitlines() == [own + "/" + gitio.SYNC_MARK, "a" * 40], p
    q = _bash(_render(script, worktree=main, as_operator=""))
    assert q.returncode == 0 and q.stdout.decode("utf-8") == "", q
