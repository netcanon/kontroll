"""local-canonical.yml: the C10 canonical's gid-1001 write-grant must provision the group + the operator's
membership + operator OWNERSHIP — not just the group.

WHY (the failure this guards — hit LIVE during the #119 prod cutover, "F-CANON"): the canonical
`/srv/kontroll.git` is group-1001 (the runtime uid:gid the privileged API/runner writes as), but a FRESH
control node has no NAMED group at gid 1001 and the operator is not a member of it. The landing at the end of
local-canonical.yml (`scripts/kontroll-land.py`, once a mirror push) runs AS THE OPERATOR (no become); the moment a
canonical object is group-1001-owned but not operator-owned, it dies with "Permission denied: unable to write
objects" and the whole deploy fails.
The #119 box needed a manual `groupadd -g 1001 kontroll; usermod -aG 1001 admin; chown -R admin:1001 <canonical>`.

The fix lands those three as idempotent tasks: (1) ensure the gid-1001 group, (2) add the operator to it, and
(3) the grant on existing objects sets owner=operator too (not just group) — as a detect-then-apply pair around the
ownership pass of `scripts/kontroll-canonical.py --normalise` (the audit's walker: every entry opened from the
directory it was listed in and changed through its own descriptor — `tests/unit/test_canonical_normalise.py`), which
PASSES OVER `hooks/`, which stays root:root 0755 so the uid-1001 writer can never plant a hook (finding 2). The owner half is load-bearing: a freshly
added supplementary group is NOT effective in the operator's current login session, so on the very first run the
push can't rely on membership yet — operator OWNERSHIP makes it succeed via OWNER perms regardless. This test
pins all three, plus the ordering (group/membership before the operator's landing), so a refactor that drops any
half silently re-breaks a fresh install's first deploy. And it pins the landing itself as the play's LAST write
into the canonical: two tasks that run `kontroll-land.py` (`--check` first, read-only, under the dry-run too), no
`git push`, no `ls-remote`, no `fetch` — `main` moves through the gate or not at all (C10, rule 8;
`tests/unit/test_land.py` holds the command).
"""
import os
import re
import shlex
import subprocess

import jinja2
import pytest
import yaml

import _ansible_display
from kontroll import gitio

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "local-canonical.yml")
STOP_TASKS = "tasks/stop-after-sync.yml"


def _canonical_play(plays):
    """local-canonical.yml's one play (`hosts`): its first entry imports the run-id stamp (I-41)."""
    (play,) = [p for p in plays if "hosts" in p]
    return play


def _tasks():
    plays = yaml.safe_load(open(PLAYBOOK, encoding="utf-8"))
    out = []
    for play in plays if isinstance(plays, list) else []:
        if isinstance(play, dict):
            out += list(play.get("tasks") or [])
    return out


def _mod(task, *keys):
    """Return the first present module dict among keys (e.g. 'ansible.builtin.group','group')."""
    for k in keys:
        v = task.get(k)
        if isinstance(v, dict):
            return v
    return None


def test_group_gid_1001_is_created():
    """A `group` task creates the gid-1001 group with become — without a named group at 1001 the canonical's
    group assignment has no member the operator can join. Guards the first half of the F-CANON manual fix."""
    hits = [t for t in _tasks() if _mod(t, "ansible.builtin.group", "group")]
    assert hits, "no group task in local-canonical.yml — the gid-1001 group is not provisioned"
    g = _mod(hits[0], "ansible.builtin.group", "group")
    assert str(g.get("gid")) == "1001", "the provisioned group must be gid 1001 (the runtime uid:gid)"
    assert hits[0].get("become") is True, "creating a system group needs become"


def test_operator_is_added_to_gid_1001_group():
    """A `user` task appends the operator to the gid-1001 group (append=true, become) so its landings
    (`kontroll-land.py`, run as the operator) can write the group-1001 canonical. Guards the second half of the
    F-CANON manual fix (`usermod -aG`)."""
    hits = [t for t in _tasks() if _mod(t, "ansible.builtin.user", "user")]
    assert hits, "no user task — the operator is never added to the gid-1001 group"
    u = _mod(hits[0], "ansible.builtin.user", "user")
    assert "1001" in str(u.get("groups")) or "kontroll" in str(u.get("groups")), \
        "the user task must add the operator to the gid-1001 (kontroll) group"
    assert u.get("append") is True, "must append (supplementary) — never replace the operator's primary group"
    assert hits[0].get("become") is True, "modifying a user's groups needs become"


def _cmd(task):
    """The command/shell text of a task, whichever spelling and shape (dict with cmd, or a bare string)."""
    for k in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
        v = task.get(k)
        if isinstance(v, dict):
            return str(v.get("cmd", v.get("_raw_params", "")))
        if isinstance(v, str):
            return v
    return ""


def _pass_tasks():
    """The ownership pass's command tasks, in order: (detect, apply) before the landing, (detect, apply) after it —
    the four tasks that run `kontroll-canonical.py --normalise`."""
    hits = [t for t in _tasks() if "--normalise" in _cmd(t)]
    assert len(hits) == 4, ("a detect-then-apply pair before the landing and one after it run the ownership pass: %r"
                            % [t.get("name") for t in hits])
    return hits[0], hits[1], hits[2], hits[3]


def test_the_harden_task_sets_only_a_key_the_config_does_not_hold_already():
    """The "Harden the bare repo" task is preceded by a READ task — `git config --no-includes --file {{ bare }}/config
    --get-all <key>` over the same five keys (`check_mode: false`, `failed_when: false`, `changed_when: false`,
    registered as `_canonical_config`) — and writes a key only when that read did not return exactly the one value
    asked: `loop_control.index_var: _key_index`, `when: (_canonical_config.results[_key_index].stdout_lines |
    default([])) != [item.value]`, `changed_when: true`, no `check_mode`. The two loops are the SAME list in the
    SAME order, because the harden reads the read's results by index.

    WHY: `git config --replace-all` rewrites the file through a lock and a rename even when the value is the one
    it holds, and the task runs as root in the installer — so every real apply left `config` root's, and the
    ownership pass re-owned it and reported `changed` on every run (the second cause behind `changed=1`, found in
    the fix round of 2026-10-20). The root row `tests/integration/test_local_canonical_as_root.py` holds what a run
    reports; this row holds the shape, so that a reversion is named at the task and not at a count: with the
    harden unconditional again (20-review-land reversion RB), or the read's loop in another order (RC — every key
    then compares with another key's value and is rewritten on every run), the parsed-task rows were all green and
    only the root row failed. `--no-includes` is pinned because the read hands git a PATH: an `[include]` line a
    writer of the canonical puts in `config` must not have this task read a second file (git's default under
    `--file` is already not to; the flag says so)."""
    tasks = _tasks()
    read = [t for t in tasks if "--get-all" in _cmd(t)]
    assert len(read) == 1, "one read task over the config keys: %r" % [t.get("name") for t in read]
    read = read[0]
    harden = next(t for t in tasks if "config --replace-all" in _cmd(t))
    assert tasks.index(read) == tasks.index(harden) - 1, "the read stands directly before the harden"
    assert _cmd(read).startswith("git config --no-includes --file {{ bare }}/config "), _cmd(read)
    assert _cmd(read).rstrip().endswith("--get-all {{ item.key }}"), _cmd(read)
    assert "-C" not in _cmd(read).split(), "the read opens no repository: the file by its path only"
    assert read.get("register") == "_canonical_config" and read.get("check_mode") is False
    assert read.get("changed_when") is False and read.get("failed_when") is False
    assert "become" not in read and "chdir" not in _mod(read, "ansible.builtin.command", "command")
    assert read["loop"] == harden["loop"], "the harden reads the read's results BY INDEX: the two loops must be equal"
    assert [sorted(i) for i in read["loop"]] == [["key", "value"]] * 5
    assert harden.get("loop_control") == {"index_var": "_key_index"}, harden.get("loop_control")
    assert str(harden.get("when")).replace(" ", "") == \
        "(_canonical_config.results[_key_index].stdout_lines|default([]))!=[item.value]", harden.get("when")
    assert harden.get("changed_when") is True and "check_mode" not in harden, \
        "a write that is skipped when nothing is to write is `changed` when it runs, and never runs under --check"


def test_the_ownership_pass_sets_operator_owner_and_is_a_detect_then_apply_pair():
    """The grant on EXISTING objects is the ownership pass of `scripts/kontroll-canonical.py --normalise`, run from
    the worktree with become, as a detect-then-apply pair: the detect task carries `--check`, runs under the play's
    `--check` too (`check_mode: false`) — when there is a canonical to look at (`_canonical_present`), so the dry-run
    of a fresh node is not failed by a path that is not there yet or an empty directory —, is never
    "changed", and fails on any status but 0 (nothing to do) and 3
    (something to do); the apply task carries no `--check`, runs only when the detect task exited 3, and is
    "changed" then — so a second run reports 0 changed. Both say `--owner {{ operator_uid }} --group 1001`: the
    owner half is what lets the operator's first-run push succeed before the just-added group membership becomes
    effective (next login). No task of the play runs `chown` or `chmod` any more, nor `find` over the canonical
    (the one `find` lists, by name and with no action, what in the worktree's git directory is not the
    operator's), and none recurses the `file` module over the canonical. Guards a regression to group-only
    (re-breaks F-CANON), to a path-based grant, to a detect task the dry-run skips, or to an apply that runs — and
    reports a change — on every run.

    The pair runs TWICE: once before the landing (the first run's F-CANON grant), and once after it, on what the
    landing left — the service's files when root landed (rule 5), git's `0444` pack files either way — so that the
    deploy after a landing reports 0 changed instead of re-owning them then (`tests/integration/
    test_local_canonical_as_root.py` runs the play and holds the count). Both pairs are the same two commands
    with the same conditions but one: the detect after the landing asks for a repository (`_canonical_repo`, the
    landing's judgement's condition), where the first asks for any canonical, entries that are no repository too,
    whose owners the real run changes and the dry run names first (I-19's read A). The second follows the landing
    task, and the detect of the second registers its own name (`_normalise_after`) so its apply is gated on it and
    not on the first detect's answer."""
    detect, apply, detect_after, apply_after = _pass_tasks()
    tasks = _tasks()
    land = next(i for i, t in enumerate(tasks) if "kontroll-land.py" in _cmd(t) and "--check" not in _cmd(t)
                and "--sync" not in _cmd(t))
    assert tasks.index(detect_after) == land + 1 and tasks.index(apply_after) == land + 3, \
        "the second pair (detect, show, apply) follows the landing task"
    assert detect_after["register"] == "_normalise_after" and detect_after["register"] != detect["register"]
    assert str(apply_after.get("when")).replace(" ", "") == "(%s.rc|default(0))==3" % detect_after["register"]
    assert apply_after["register"] != apply["register"]
    for task in (detect, apply, detect_after, apply_after):
        cmd = _cmd(task)
        assert cmd.split()[:3] == ["python3", "-B", "scripts/kontroll-canonical.py"], cmd
        assert "--normalise {{ bare }}" in cmd and "--owner {{ operator_uid }} --group 1001" in cmd, cmd
        assert _mod(task, "ansible.builtin.command", "command").get("chdir") == "{{ worktree }}"
        assert task.get("become") is True, "the pass opens entries nobody else may open: become"
        assert "no_log" not in task, "the pass prints names only — hiding them hides what the operator must read"
    for d, a in ((detect, apply), (detect_after, apply_after)):
        assert _cmd(d).rstrip().endswith("--check") and "--check" not in _cmd(a)
        assert d.get("check_mode") is False and d.get("changed_when") is False
        assert str(d.get("failed_when")).replace(" ", "") == "%s.rcnotin[0,3]" % d["register"]
        assert str(a.get("when")).replace(" ", "") == "(%s.rc|default(0))==3" % d["register"]
        assert str(d.get("when")).replace(" ", "") == ("_canonical_presentornotansible_check_mode" if d is detect
                                                       else "_canonical_repoornotansible_check_mode"), (
            "the first detect is skipped only under the dry-run of a node with no canonical yet (no path, an empty "
            "directory), the one after the landing also where there is no repository; both run on every real run")
        assert a.get("changed_when") is True and "check_mode" not in a
    for task in _tasks():
        words = set(_cmd(task).split())
        assert not {"chown", "chmod"} & words, "a path-based grant is back: %s" % task.get("name")
        if "find" in words:            # the leftover listing (T25): the worktree's git directory, names only
            assert '"$common"' in words and "{{ bare }}" not in _cmd(task), task.get("name")
            assert not {"-exec", "-execdir", "-ok", "-okdir", "-delete"} & words, task.get("name")
    blanket = [t for t in _tasks() if (m := _mod(t, "ansible.builtin.file", "file")) and m.get("recurse")
               and str(m.get("path")) == "{{ bare }}"]
    assert blanket == [], "no blanket recurse over the whole canonical — it would re-grant hooks/ every run"


def test_hooks_stay_root_owned_and_outside_every_grant():
    """hooks/ is root:root 0755 (recurse), the ownership pass passes it over (its `_HOOKS` is the entry `hooks` of
    the top directory, read from the script), and the re-own task runs AFTER the pass. The canonical is mounted :rw
    into the uid-1001 service (api_privileged); with a group-writable hooks/ that writer could plant a
    `reference-transaction` or `update` hook that the next `sudo kontroll-promote` runs as ROOT (2026-10-08
    review, finding 2). Every git actor only needs to READ hooks. Guards the grant growing back over hooks/, the
    re-own going missing, or it running before a grant that would undo it. (The pass's second pair, after the
    landing, passes hooks/ over as the first does — `_HOOKS` is the script's, not a task's — so it needs no re-own
    after it.)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("kontroll_canonical_for_hooks",
                                                  os.path.join(ROOT, "scripts", "kontroll-canonical.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool._HOOKS == b"hooks", "the pass must pass over the top directory's `hooks` entry"
    tasks = _tasks()
    grants = [i for i, t in enumerate(tasks) if "--normalise" in _cmd(t)]
    assert len(grants) == 4, "the grant tasks must exist: a pair before the landing and a pair after it"
    land = next(i for i, t in enumerate(tasks) if "kontroll-land.py" in _cmd(t) and "--check" not in _cmd(t)
                and "--sync" not in _cmd(t))
    grants = [i for i in grants if i < land]                    # the pair the first run's grant rests on
    hooks = [i for i, t in enumerate(tasks)
             if (m := _mod(t, "ansible.builtin.file", "file")) and str(m.get("path")) == "{{ bare }}/hooks"]
    assert len(hooks) == 1, "exactly one task owns hooks/"
    m = _mod(tasks[hooks[0]], "ansible.builtin.file", "file")
    assert m.get("owner") == "root" and m.get("group") == "root", "hooks/ is root:root"
    assert str(m.get("mode")) == "0755" and m.get("recurse") is True, "0755 recursively — readable+executable, never writable"
    assert tasks[hooks[0]].get("become") is True
    assert hooks[0] > max(grants), "the hooks re-own must follow the first pair (the pass passes hooks/ over, so " \
                                   "the pair after the landing cannot touch it)"
    install = [t for t in tasks if (m := _mod(t, "ansible.builtin.copy", "copy"))
               and str(m.get("dest", "")).endswith("/hooks/update")]
    assert install and str(_mod(install[0], "ansible.builtin.copy", "copy").get("mode")) == "0755", \
        "the update hook is installed 0755 (readable + executable by every git actor, writable by none but root)"


def test_group_and_membership_precede_the_operator_landing():
    """The group + membership tasks must come BEFORE the landing (`kontroll-land.py`, run as the operator) —
    provisioning the write-grant after the write that needs it would be useless. Pins the ordering."""
    tasks = _tasks()
    def _idx(pred):
        return next((i for i, t in enumerate(tasks) if pred(t)), None)
    def _is_landing(t):                                   # the landing COMMAND only — not a task whose NAME
        m = _mod(t, "ansible.builtin.command", "command")  # merely mentions it (e.g. the user task)
        return bool(m) and "kontroll-land.py" in str(m.get("cmd", ""))
    grp = _idx(lambda t: _mod(t, "ansible.builtin.group", "group"))
    usr = _idx(lambda t: _mod(t, "ansible.builtin.user", "user"))
    land = _idx(_is_landing)
    assert grp is not None and usr is not None and land is not None, "missing group/user/landing task"
    assert grp < land and usr < land, "the gid-1001 group + operator membership must be provisioned before the landing"


def _cmd_text(task):
    for k in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
        v = task.get(k)
        if isinstance(v, dict):
            return str(v.get("cmd", v.get("_raw_params", "")))
        if isinstance(v, str):
            return v
    return ""


def test_the_mirror_is_a_landing_judged_first_and_never_a_push():
    """The play's way into the canonical's `main` is `scripts/kontroll-land.py`, twice: a `--check` task — run
    from the worktree with `--repo {{ bare }}`, `check_mode: false` (the mandated `--check --diff` dry-run names
    a refusal first), `changed_when: false`, `failed_when: false` — followed by a `fail` task on any non-zero
    status whose message quotes the command's stderr, says nothing was landed, and names the override for a
    gate refusal (rc 2) — and then the landing task, same command without `--check`, "changed" when its stdout
    says `landed refs/heads/main`. No task of the play runs `git push`, `git ls-remote` or `git fetch`: the push
    ran the canonical's hooks and config as the pusher (root in the installer), the `ls-remote` ran git's serving
    side there as the caller, and a root fetch leaves a root-owned `.git/FETCH_HEAD` in the operator's worktree
    (live-caught 2026-07-28). The never-discard rule — a canonical with commits the worktree lacks (a promote)
    refuses the landing, with the remedy `kontroll-land.py --sync` and never `--force` — now lives in the
    command and is held by `tests/unit/test_land.py`; here the play must not carry a `--force` or a `-f`
    anywhere near the landing. Guards the reversion of rule 8 at this door (a push coming back, the dry-run
    skipping the judgement, a landing task that runs under check mode), which the hook would then refuse at
    every deploy.

    The `--check` task's CONDITION is pinned to that of the ownership pass's detect after the landing —
    `_canonical_repo or not ansible_check_mode` — and nothing to bring forward first (`(_sync_check.rc | default(0))
    != 3`: under the dry-run after a promote the landing is judged only once the real run's sync has made it; the
    next row pins the sync) — and the refusal's to `(<register>.rc | default(0)) != 0`. WHY: the judgement needs a
    canonical to judge against (`--repo {{ bare }}`), and under the dry-run of a FRESH node the `file` and `git init`
    tasks are only simulated, so the command exits 1 ("--repo must name a bare repository") and the refusal stops
    the mandated `--check --diff` of every first install (measured 2026-10-20 in the gate image: `failed=1` at the
    refusal task). The exact condition is held, not merely "a when": a `when: not ansible_check_mode` on the same
    task would make EVERY dry-run skip the judgement with the rest of this row green (the 20-review-land reversion
    R06 survived before this pin). And `default(0)`: a skipped judgement registers no `rc`, and the refusal must
    read that as "nothing to refuse", not fail on an undefined attribute. The debug task before the judgement says,
    under that one dry-run, why nothing was judged."""
    tasks = _tasks()
    landing = [t for t in tasks if "kontroll-land.py" in _cmd_text(t) and "--sync" not in _cmd_text(t)]
    assert len(landing) == 2, "a --check task and a landing task: %r" % [t.get("name") for t in landing]
    check, land = landing
    for task in (check, land):
        m = _mod(task, "ansible.builtin.command", "command")
        assert m and m.get("chdir") == "{{ worktree }}", task.get("name")
        text, words = _cmd_text(task), _cmd_text(task).split()
        assert words[:3] == ["python3", "-B", "scripts/kontroll-land.py"], words
        assert "--worktree {{ worktree }}" in text and "--repo {{ bare }}" in text, text
        assert "--force" not in words and "-f" not in words and "--skip-conflict-check" not in words, \
            "the play never forces and never skips the gate: the override is the operator's, by hand"
        assert "become" not in task, "the landing runs as the caller (root drops to the service in the seam)"
    assert _cmd_text(check).rstrip().endswith("--check") and "--check" not in _cmd_text(land)
    assert check.get("check_mode") is False and check.get("changed_when") is False and check.get("failed_when") is False
    assert str(check.get("when")).replace(" ", "") == (
        "(_canonical_repoornotansible_check_mode)and(_sync_check.rc|default(0))!=3"), (
        "the judgement runs on every real run and on the dry-run of a node that HAS a repository and nothing to bring "
        "forward; only the dry-run of a fresh node, and the one after a promote, skip it: %r" % check.get("when"))
    detect = [t for t in tasks if "--normalise" in _cmd_text(t) and "--check" in _cmd_text(t)][-1]
    assert str(check.get("when")).replace(" ", "").startswith("(%s)and" % str(detect.get("when")).replace(" ", "")), (
        "the judgement's condition is that of the ownership pass's detect after the landing, in the same words")
    note = tasks[tasks.index(check) - 1]
    assert _mod(note, "ansible.builtin.debug", "debug") and "ansible_check_mode" in str(note.get("when")) \
        and "not _canonical_repo" in str(note.get("when")), \
        "the task before the judgement says, under the fresh node's dry-run, why nothing is judged"
    assert "{{ bare }}" in str(_mod(note, "ansible.builtin.debug", "debug").get("msg")) and "first seed" in \
        str(_mod(note, "ansible.builtin.debug", "debug").get("msg"))
    refuse = tasks[tasks.index(check) + 1]
    assert _mod(refuse, "ansible.builtin.fail", "fail"), "the task after the --check must be the refusal"
    assert str(refuse.get("when")).replace(" ", "") == "(%s.rc|default(0))!=0" % check["register"], \
        "a skipped judgement registers no rc: the refusal reads it as 0, never as an undefined attribute"
    message = str(_mod(refuse, "ansible.builtin.fail", "fail").get("msg"))
    assert "REFUSING to land" in message and check["register"] + ".stderr_lines" in message
    assert "Nothing was landed" in message and "skip-conflict-check" in message and "rc == 2" in message
    assert message.count("\n") == 1 and message.split("\n")[0].endswith("—"), \
        ("the message is ONE folded paragraph after its opening line, a break between them: Ansible then shows it as"
         " a block, every command as written (read B-f9, S1)")
    assert tasks.index(land) == tasks.index(refuse) + 1, "the landing follows its own dry-run and refusal"
    assert "check_mode" not in land, "a command that writes is skipped under --check by Ansible itself"
    assert "landed refs/heads/main" in str(land.get("changed_when")) and land["register"] in str(land.get("changed_when"))
    assert str(land.get("failed_when")).replace(" ", "") == "%s.rc!=0" % land["register"]
    for t in tasks:
        text = _cmd_text(t)
        assert "git push" not in text and "ls-remote" not in text and "git fetch" not in text and "FETCH_HEAD" not in text, \
            "a transport into or out of the canonical is back: %s" % t.get("name")
    with open(PLAYBOOK, encoding="utf-8") as fh:
        src = fh.read()
    assert "git push local HEAD:refs/heads/main" not in src and "REFUSING to mirror" not in src
    commit = next(i for i, t in enumerate(tasks) if "git add -A" in _cmd_text(t))
    assert commit < tasks.index(check), "the instance-state commit comes before the landing that takes HEAD"


def test_an_empty_directory_at_the_canonicals_path_is_no_canonical(tmp_path):
    """Whether there is a canonical is decided once, after the `stat` of its path: one listing of the path's top
    level, run only when the path is a directory, by the interpreter Ansible runs on, read-only under `--check` too,
    never "changed" and never failing the play, that prints one WORD and never a name — `empty`, `repo` (`HEAD`
    among the entries), `headless` (`objects` or `refs`, and no `HEAD`) or `entries` (anything else) —, and one
    `set_fact` of two answers on that word. The listing's program is RUN here on each shape laid out in a scratch
    directory (on POSIX, a name that is not UTF-8 too), and on paths it cannot list, where it prints nothing. Both
    expressions are EVALUATED with Jinja for each word and for none. `_canonical_present` is false for a path that is
    not there and for an EMPTY directory, and true for anything else — a directory with an entry, a path that is no
    directory, and a directory this account could not list (no word). `_canonical_repo` is that, less `entries` and
    `headless`. The audit, its no-report sentence and the first ownership detect ask `_canonical_present` (three
    tasks, by name); the detect after the landing, the landing's judgement, the dry run's note and
    the sync's dry-run check (T25) ask `_canonical_repo` (four); a new task that asks either raises its number here.
    None asks the bare `stat` any more.

    WHY: the installer's compose service binds `/srv/kontroll.git`, and Docker makes a bind source that is missing an
    empty root-owned directory, so after `fresh-init` a fresh node has one. Taken for a canonical, the mandated
    dry-run of every first install through the installer judged the landing against it and failed ("the canonical
    could not be read"; measured 2026-11-07, the quickstart run as written on a GitHub-hosted runner), and the audit
    named three things nobody had done. A path that holds entries and no repository (a filesystem mounted there
    holds `lost+found`) failed the dry run the same way while the real run landed (I-19's read B, measured on both
    cores): it is audited, and the dry run says the real run will seed it. And a word, not `find`: `find` returns
    every name, and one that is not UTF-8 failed its result on both cores, before the audit whose job is to name it,
    and with it every deploy (I-19's read A, measured). Guards the decision going back to the bare `stat` or to a
    module that returns names, a listing that misreads a shape, an unreadable canonical counted as none, which would
    skip its audit in silence where the audit says what it could not read, and a directory with entries, or a
    repository whose `HEAD` is gone, judged as a repository."""
    import subprocess
    import sys

    import jinja2
    tasks = _tasks()
    at = next(i for i, t in enumerate(tasks) if t.get("register") == "_canonical_before")
    listing, present = tasks[at + 1], tasks[at + 2]
    assert set(listing) == {"name", "ansible.builtin.command", "register", "changed_when", "failed_when",
                            "check_mode", "when"}, listing
    assert set(listing["ansible.builtin.command"]) == {"argv"}, listing
    argv = listing["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["{{ ansible_playbook_python }}", "-B", "-c"] and argv[4:] == ["{{ bare }}"], argv
    assert listing["register"] == "_canonical_listing"
    assert listing["when"] == "_canonical_before.stat.isdir | default(false)"
    assert listing["check_mode"] is False and listing["changed_when"] is False and listing["failed_when"] is False, \
        "read-only under --check too, and a listing that fails says no word: it is no failure of the play"
    shapes = [   # the entries, the word
        ([], "empty"),
        (["HEAD", "config", "objects", "refs", "hooks"], "repo"),
        (["HEAD"], "repo"),
        (["config", "objects", "refs", "hooks"], "headless"),
        (["refs"], "headless"),
        (["lost+found"], "entries"),
        (["README", ".HEAD"], "entries"),
    ]
    if os.name == "posix":
        shapes += [([b"\xff\xfe"], "entries"), ([b"\xff\xfe", "HEAD"], "repo")]
    for i, (names, word) in enumerate(shapes):
        top = tmp_path / ("shape%d" % i)
        top.mkdir()
        for name in names:
            os.mkdir(os.path.join(os.fsencode(str(top)), name if isinstance(name, bytes) else name.encode()))
        ran = subprocess.run([sys.executable, "-B", "-c", argv[3], str(top)], capture_output=True, timeout=60)
        assert ran.returncode == 0 and ran.stdout.split() == [word.encode()], (names, ran)
    a_file = tmp_path / "a-file"
    a_file.write_text("x", encoding="utf-8")
    for path in (tmp_path / "no-such", a_file):
        ran = subprocess.run([sys.executable, "-B", "-c", argv[3], str(path)], capture_output=True, timeout=60)
        assert ran.returncode != 0 and ran.stdout == b"", (path, ran)
    facts = present["ansible.builtin.set_fact"]
    assert set(facts) == {"_canonical_present", "_canonical_repo"}, facts
    assert set(present) == {"name", "ansible.builtin.set_fact"}, present
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    decide = {}
    for fact, text in facts.items():
        text = text.strip()
        assert text.startswith("{{") and text.endswith("}}"), text
        decide[fact] = env.compile_expression(text[2:-2])
    skipped = {"changed": False, "skipped": True, "skip_reason": "Conditional result was False"}

    def listed(word, rc=0):
        return {"changed": False, "rc": rc, "stdout": word, "stdout_lines": word.splitlines(), "stderr": ""}
    a_dir = {"exists": True, "isdir": True}
    cases = [   # stat, listing, present, repo
        ({"exists": False}, skipped, False, False, "no path"),
        ({"exists": True, "isdir": False}, skipped, True, True, "a path that is no directory"),
        (a_dir, listed("empty"), False, False, "an empty directory"),
        (a_dir, listed("empty\n"), False, False, "an empty directory, the word with its newline"),
        (a_dir, listed("repo"), True, True, "a repository"),
        (a_dir, listed("headless"), True, False, "a repository whose HEAD is gone"),
        (a_dir, listed("entries"), True, False, "a filesystem mounted at the path: entries, no repository"),
        (a_dir, listed("", rc=1), True, True, "a directory this account could not list"),
    ]
    for st, registered, want_present, want_repo, what in cases:
        scope = {"_canonical_before": {"stat": st}, "_canonical_listing": registered}
        assert decide["_canonical_present"](**scope) is want_present, what
        assert decide["_canonical_repo"](**scope) is want_repo, what
        assert want_present or not want_repo, (what, "a repository is a canonical")
    for fact, number in (("_canonical_present", 3), ("_canonical_repo", 4)):
        asking = [t.get("name") for t in tasks if fact in str(t.get("when", ""))]
        assert len(asking) == number, (fact, asking)
    for task in tasks:
        assert "_canonical_before.stat.exists" not in str(task.get("when", "")), task.get("name")


def test_the_dry_runs_note_says_what_it_saw_and_what_the_real_run_will_do():
    """The note before the landing's judgement, shown under `--check` when there is no repository to judge
    against, is EVALUATED here with Jinja (its one var first, as Ansible resolves it) for each case it is shown
    for. No path, an empty directory, and a directory of entries and no repository each "hold no canonical yet",
    with the case named ("it is not there"; "what Docker leaves for a bind source that is missing"; "the audit above
    named them"), and the real run "lands HEAD as a first seed". A repository whose `HEAD` is gone "holds a
    repository without HEAD", and the real run "restores HEAD, then judges the landing", with no word of a first
    seed. Every message names the path and the way to judge HEAD's tree by hand, in one paragraph.

    WHY: the dry run of a repository whose `HEAD` is gone skips the judgement (it cannot read one), and the note
    said that the path held no canonical and that the real run would land HEAD as a first seed; on a canonical that
    holds a promoted commit the worktree lacks, the real run refuses the landing instead (I-19's read A, measured on
    both cores). Guards those words coming back for that case, and a note that names the wrong case."""
    import jinja2
    tasks = _tasks()
    check = next(t for t in tasks if "kontroll-land.py" in _cmd_text(t) and _cmd_text(t).rstrip().endswith("--check"))
    note = tasks[tasks.index(check) - 1]
    assert set(note) == {"name", "ansible.builtin.debug", "vars", "when"}, note
    assert str(note["when"]).replace(" ", "") == "ansible_check_modeandnot_canonical_repo", note["when"]
    (var, text), = note["vars"].items()
    msg = note["ansible.builtin.debug"]["msg"]
    assert "\n" not in msg.strip(), "the message is ONE folded paragraph"
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)

    def said(stat, registered, present):
        scope = {"_canonical_before": {"stat": stat}, "_canonical_listing": registered,
                 "_canonical_present": present, "bare": "/srv/kontroll.git", "worktree": "/checkout"}
        scope[var] = env.from_string(text).render(**scope)
        return env.from_string(msg).render(**scope)
    head = "local-canonical: /srv/kontroll.git "
    skipped = ", so under --check the landing's judgement is skipped — the real run "
    hand = (" To judge HEAD's tree now: in /checkout, `git init -q --bare /tmp/empty.git && python3 -B "
            "scripts/kontroll-land.py --check --repo /tmp/empty.git`.")
    seed = "creates the canonical and lands HEAD as a first seed, judged then."
    a_dir = {"exists": True, "isdir": True}
    for stat, registered, present, saw in (
            ({"exists": False}, {"skipped": True}, False, "holds no canonical yet (it is not there)"),
            (a_dir, {"stdout": "empty"}, False,
             "holds no canonical yet (it is an empty directory — what Docker leaves for a bind source "
             "that is missing)"),
            (a_dir, {"stdout": "entries"}, True,
             "holds no canonical yet (it holds entries but no repository: the audit above named them)")):
        assert said(stat, registered, present) == head + saw + skipped + seed + hand, saw
    assert said(a_dir, {"stdout": "headless"}, True) == (
        head + "holds a repository without HEAD (the audit above named it)" + skipped
        + "restores HEAD, then judges the landing." + hand)


def test_the_worktree_is_written_as_the_operator_and_brought_forward_before_the_landing():
    """What writes the operator's repository runs AS THE OPERATOR, the sync stands between the state commit and
    the landing, and a sync that brought the worktree forward stops the run (T25):

      - `as_operator` is, inside the installer container, `setpriv` to `operator_uid` / `operator_gid` with no
        supplementary group and no new privileges, then `env -i` with a PATH, `HOME=/nonexistent` and no prompt —
        and host-direct nothing (the play IS the operator there); `operator_gid` is `KONTROLL_OPERATOR_GID` when
        it is set;
      - the `local` remote is added through `{{ as_operator }}` (git rewrites `.git/config` through a lock file:
        root's `remote add` left the config root's);
      - right after the stop, before anything of the worktree runs, a read-only task asks git, as the operator, for
        the worktree's own git directory and lists each name of `sync_busy` — the names `gitio._SYNC_BUSY` refuses
        on — that exists there (a link too; never `--git-path`, which names a link's target) and, when none does,
        HEAD if it is on no branch (asked as the operator too: outside the installer image, which names every
        directory safe, root's git refuses the operator's worktree as another uid's, and HEAD would read as on no
        branch at every run; and asked for a branch, `refs/heads/*`: a HEAD naming a remote-tracking ref took the
        promote's revert in as instance state, read A-f11, N5), and a `fail` refuses on any (or when git could not
        say);
      - right before the state commit, a read-only task (`check_mode: false`, `set -e`) asks git, as the operator,
        for the worktree's common git directory and lists what under it is not the operator's, and a `fail` names it
        and the one-time `chown -R` by hand (or that git could not say): nothing re-owns the tree;
      - the state commit's every `git` runs through `{{ as_operator }}`, under `set -e`;
      - after it, in this order: `--sync --check` (as the operator; under the dry-run of a node with a repository
        to sync from only; never failing on its own), `--sync --stop-as local-canonical` (no `check_mode` key; "changed" on its
        exit 4, failed on any exit but 0: brought forward, it says what came in, removes the mark and fails THIS
        task, the run's last), the check's refusal on an exit other than 0 and 3 (its message reads the check
        alone, ends each line it printed with one full stop, and holds no backslash), the note that the dry-run's
        judgement waits, the fresh node's note, and then the landing's own `--check`;
      - the stop on a mark (`tasks/stop-after-sync.yml`) is imported ONCE, first, right after the uid check and
        before anything of the worktree runs (a sync by hand, or a run killed between its sync and its stop,
        leaves the mark it stops on), with this play's name;
      - the Report prints the landing's own lines.

    Guards the failures this theme found: root's objects and ref files left in the operator's worktree by a root state
    commit (named before anything writes there; in a linked worktree they are in the common directory); a state commit
    onto the detached HEAD of a replay that did not finish (the abort the refusal names would take the state away
    again), and the audit, the fleet check and the plaintext guard run as root on the scripts a checkout cut short left
    half main's before that refusal (read A-f6, N2); a landing refused after every promote because nothing brought the
    worktree forward; and the deploy that, in the same run, went on to run the tree the sync brought in as root, after a
    dry-run of the tree before it (the stop must come before the landing's first command, which runs the worktree's own
    `kontroll-land.py` as root) — and a stop in a task after the sync, which ran after Ansible had forked a worker that
    imported the tree's filter plugins, as root (read A-f3, B1); the run after a sync by hand, which went on without a
    word; and a refusal whose backreference printed `\\1.` in place of each line's last letter (measured in the gate
    image). A task dropped, moved or run as root brings one of them back."""
    play_vars = _canonical_play(yaml.safe_load(open(PLAYBOOK, encoding="utf-8")))["vars"]
    who = str(play_vars["as_operator"]).replace("\n", " ")
    for word in ("setpriv --reuid=' ~ operator_uid ~ ' --regid=' ~ operator_gid", "--clear-groups", "--no-new-privs",
                 "-- env -i", "HOME=/nonexistent", "GIT_TERMINAL_PROMPT=0", "if (in_container | bool) else ''"):
        assert word in who, (word, who)
    assert str(play_vars["operator_gid"]).split()[:5] == ["{{", "lookup('env',", "'KONTROLL_OPERATOR_GID')", "or",
                                                         "(operator_uid"], play_vars["operator_gid"]
    assert play_vars["sync_busy"] == list(gitio._SYNC_BUSY), play_vars["sync_busy"]
    tasks = _tasks()
    commit = next(i for i, t in enumerate(tasks) if "git add -A" in _cmd_text(t))
    leftover = next(i for i, t in enumerate(tasks) if "--git-common-dir" in _cmd_text(t))
    script = _cmd_text(tasks[leftover])
    assert script.lstrip().startswith("set -e") and isinstance(tasks[leftover].get("ansible.builtin.shell"), str)
    for line in ("common=$({{ as_operator }} git -C {{ worktree | quote }} rev-parse --path-format=absolute "
                 "--git-common-dir)", 'echo "$common"', 'find "$common" ! -user {{ operator_uid | quote }} -print'):
        assert line in script.splitlines(), (line, script)
    assert tasks[leftover].get("check_mode") is False and tasks[leftover].get("failed_when") is False
    refuse = tasks[leftover + 1]
    message = str(_mod(refuse, "ansible.builtin.fail", "fail").get("msg"))
    reg = tasks[leftover]["register"]
    assert str(refuse.get("when")).replace(" ", "") == "%s.rc!=0or(%s.stdout_lines|default([])|length>1)" % (reg, reg)
    assert "chown -R" in message and "{{ operator_uid }}:{{ operator_gid }}" in message, message
    assert "Nothing in the worktree was changed" in message and "could not name the git directory" in message
    busy = 2
    script = _cmd_text(tasks[busy])
    assert script.lstrip().startswith("set -e"), script
    assert 'dir=$({{ as_operator }} git rev-parse --path-format=absolute --git-dir)' in script, script
    assert "for n in {% for n in sync_busy %}{{ n | quote }} {% endfor %}; do" in script, script
    assert 'if [ -e "$dir/$n" ] || [ -L "$dir/$n" ]; then echo "$dir/$n"; found=1; fi' in script, script
    assert ('[ -n "$found" ] || case $({{ as_operator }} git symbolic-ref -q HEAD) in\n'
            '  refs/heads/*) ;; *) echo "$dir/HEAD";; esac' in script), (
        "HEAD is asked as the operator (root's git refuses the worktree outside the installer image), and for a "
        "branch: a HEAD naming a ref outside refs/heads/ is listed (read A-f11, N5)")
    assert "--git-path" not in script, "git resolves a link in the path it names: the sync reads the name itself"
    assert tasks[busy].get("check_mode") is False and tasks[busy].get("failed_when") is False
    reg = tasks[busy]["register"]
    assert _mod(tasks[busy + 1], "ansible.builtin.fail", "fail") and str(tasks[busy + 1].get("when")).replace(
        " ", "") == "%s.rc!=0or(%s.stdout_lines|default([])|length>0)" % (reg, reg)
    assert leftover + 2 == commit, "the leftovers are refused right before the state commit"
    assert tasks[busy - 1].get("ansible.builtin.import_tasks") == "tasks/stop-after-sync.yml", tasks[busy - 1]
    assert busy + 2 < leftover, "a rebase in progress is refused right after the stop"
    remote = next(t for t in tasks if "remote add local" in _cmd_text(t))
    assert _cmd_text(remote).startswith("{{ as_operator }} git remote add local"), _cmd_text(remote)
    assert tasks.index(remote) < leftover, "the remote is added before the leftovers are named"
    script = _cmd_text(tasks[commit])
    assert script.lstrip().startswith("set -e")
    gits = [line for line in script.splitlines() if "git " in line]
    assert gits and all("{{ as_operator }} git " in line for line in gits), gits
    syncs = [i for i, t in enumerate(tasks) if "kontroll-land.py --sync" in _cmd_text(t)]
    assert len(syncs) == 2, syncs
    check, sync = (tasks[i] for i in syncs)
    land_check = next(i for i, t in enumerate(tasks)
                      if "kontroll-land.py" in _cmd_text(t) and "--sync" not in _cmd_text(t))
    # the sync's check, the sync, the check's refusal, the dry-run's note, the fresh node's note — then the
    # landing's check
    assert commit < syncs[0] and syncs[1] == syncs[0] + 1 and land_check == syncs[1] + 4, (commit, syncs, land_check)
    for task in (check, sync):
        text = _cmd_text(task).split()
        assert text[:5] == ["{{", "as_operator", "}}", "python3", "-B"], text
        assert "--worktree {{ worktree }} --repo {{ bare }}" in _cmd_text(task) and "become" not in task
    assert " --sync --check " in " ".join(_cmd_text(check).split())
    assert check.get("check_mode") is False and check.get("changed_when") is False and check.get("failed_when") is False
    assert str(check.get("when")).replace(" ", "") == "ansible_check_modeand_canonical_repo"
    assert " --sync --stop-as local-canonical " in " %s " % " ".join(_cmd_text(sync).split()), _cmd_text(sync)
    assert "--check" not in _cmd_text(sync).split() and "check_mode" not in sync
    assert sync.get("failed_when") == "%s.rc != 0" % sync["register"], sync.get("failed_when")
    assert sync.get("changed_when") == "%s.rc == 4" % sync["register"], sync.get("changed_when")
    refused, note = (tasks[syncs[1] + k] for k in (1, 2))
    assert _mod(refused, "ansible.builtin.fail", "fail") and str(refused.get("when")).replace(" ", "") == (
        "(%s.rc|default(0))notin[0,3]" % check["register"]), refused.get("when")
    message = str(_mod(refused, "ansible.builtin.fail", "fail").get("msg"))
    assert message.startswith("local-canonical: REFUSING to land — the sync of the worktree to the canonical's main "
                              "(--check) exited"), message
    assert "| select | map('regex_replace', '(?<![.])$', '.') | join(' ')" in message and chr(92) not in message
    assert sync["register"] + "." not in message, "the refusal reads the check alone: the sync fails on its own"
    first = tasks[1]
    assert _mod(tasks[0], "ansible.builtin.assert", "assert") and "1001" in str(tasks[0]), tasks[0]
    assert first.get("ansible.builtin.import_tasks") == STOP_TASKS and first.get("vars") == {
        "stop_play": "local-canonical"}, "the stop on a mark comes right after the uid check: %r" % first
    assert [t for t in tasks if "import_tasks" in str(sorted(t))] == [first], "the stop is imported once, at the top"
    assert _mod(note, "ansible.builtin.debug", "debug") and str(note.get("when")).replace(" ", "") == (
        "(%s.rc|default(0))==3" % check["register"])
    report = tasks[-1]
    assert report["name"] == "Report" and "(_land.stdout_lines | default([]))" in str(
        _mod(report, "ansible.builtin.debug", "debug").get("msg")), report


def test_the_play_refuses_an_operator_whose_uid_is_the_services():
    """The play's FIRST task asserts `operator_uid` is not `1001`, with a sentence that says the canonical cannot
    tell such an operator from the network service (the hook and the seam are keyed on that one number). Guards
    the gap the hook's reader named (Q-6): an operator whose login uid is 1001 would have the landing run "as the
    service", the hook refuse its proposals' moves, and the chown hand the service the operator's grant — each
    failing later with a message about something else. It names docs/troubleshooting.md section 10, as the scripts' sentences do
    (I-18): the row there quotes all three."""
    tasks = _tasks()
    first = tasks[0]
    m = _mod(first, "ansible.builtin.assert", "assert")
    assert m, "the first task must be the uid assertion: %r" % first.get("name")
    assert "1001" in str(m.get("that")) and "operator_uid" in str(m.get("that"))
    assert "uid 1001" in str(m.get("fail_msg")) and "cannot tell" in str(m.get("fail_msg"))
    assert "Nothing was changed" in str(m.get("fail_msg"))
    # I-18: the scripts' sentences and this one name the troubleshooting row that quotes all three.
    assert "(docs/troubleshooting.md section 10)" in str(m.get("fail_msg"))


def test_the_plaintext_gate_precedes_the_instance_state_commit():
    """The play runs scripts/lib/plaintext-guard.sh over the worktree — with `check_mode: false`, so the mandated
    dry-run names an offender too, and never failing on its own status — then FAILS on any non-zero status, and
    both tasks come before the one whose shell does `git add -A`. Guards a refactor that moves or drops the gate
    while the blanket add stays, a gate that only refuses on its "found something" code (a guard that could not
    run has checked nothing), a refusal that gives a file's remedy when no file was named, a message broken in the
    middle of a sentence (YAML keeps the line break of a more-indented continuation line), and one that is skipped
    in check mode — where it would report nothing and the real
    run would then stop."""
    tasks = _tasks()

    def _argv(task):
        module = _mod(task, "ansible.builtin.command", "command") or {}
        return [str(a) for a in (module.get("argv") or [])] or str(module.get("cmd", "")).split()

    guard = next((i for i, t in enumerate(tasks) if any("plaintext-guard.sh" in a for a in _argv(t))), None)
    assert guard is not None, "no task runs the plaintext guard"
    task = tasks[guard]
    assert task.get("check_mode") is False and task.get("failed_when") is False and task.get("changed_when") is False
    assert "no_log" not in task, "the guard prints names and rules only — hiding them hides what the operator must read"
    assert _argv(task)[0] == "sh" and _argv(task)[-1] == "{{ worktree }}" and "--tree" not in _argv(task)
    register = task.get("register")
    refuse = tasks[guard + 1]
    assert _mod(refuse, "ansible.builtin.fail", "fail"), "the task after the guard must be the refusal"
    assert str(refuse.get("when")).replace(" ", "") == "%s.rc!=0" % register
    message = str(_mod(refuse, "ansible.builtin.fail", "fail").get("msg"))
    assert "docs/install-from-scratch.md" in message, "the refusal must say where the fix for each rule is"
    hint = message.index("sops --config instance/.sops.yaml -e -i")
    assert "'no sops block' in" in message[message.rindex("{{", 0, hint):message.index("}}", hint)], \
        "the encrypt command is the remedy for ONE rule (a file never encrypted) and must be offered only for it"
    move = message.index("Move the named file")
    assert "'REFUSED' in" in message[message.rindex("{{", 0, move):message.index("}}", move)], \
        "'move the named file' is advice for a finding; when the guard could not look there is no file to move"
    assert "\n" not in message, \
        "the message is ONE folded paragraph: a continuation line indented deeper than the first keeps its line break"
    commit = next(i for i, t in enumerate(tasks) if "git add -A" in _cmd_text(t))
    assert guard < guard + 1 < commit, "the gate must stand before the blanket add"


def test_the_installer_image_lets_git_read_the_operators_worktree():
    """docker/installer/Dockerfile keeps `git config --system --add safe.directory '*'`. The play runs as root
    inside that image on a worktree the OPERATOR owns, and the plaintext gate's first act is to ask git about it.
    Guards the line being dropped as "convenience": without it git answers "dubious ownership", the gate cannot
    run, and — because a gate that cannot run refuses — every containerised deploy stops at the commit."""
    dockerfile = open(os.path.join(ROOT, "docker", "installer", "Dockerfile"), encoding="utf-8").read()
    assert "git config --system --add safe.directory '*'" in dockerfile


def test_the_update_hook_is_installed_by_every_run_from_the_committed_file():
    """Exactly one task copies a file to `<canonical>/hooks/update`; its source is the committed
    `files/canonical-update-hook.sh`, it runs with become, and it carries no condition — no `when`, no
    `check_mode`, no tag of its own — so every run of this play (deploy-stack.yml imports it: every deploy, armed
    or not, in the installer container and host-direct) replaces the hook. Guards a play that installs another
    file, or installs the hook on some nodes only: the hook's own tests execute the committed file and would stay
    green, and the CHANGELOG's "the hook file is replaced at the next deploy" would be false. Read from the parsed
    task, not from the play's text: a comment is not a task."""
    tasks = _tasks()
    install = [t for t in tasks if (m := _mod(t, "ansible.builtin.copy", "copy"))
               and str(m.get("dest", "")) == "{{ bare }}/hooks/update"]
    assert len(install) == 1, "exactly one task installs the update hook"
    task = install[0]
    m = _mod(task, "ansible.builtin.copy", "copy")
    assert m.get("src") == "files/canonical-update-hook.sh" and "content" not in m
    assert os.path.isfile(os.path.join(os.path.dirname(PLAYBOOK), m["src"]))
    assert task.get("become") is True, "hooks/ is root's: the copy needs become"
    for key in ("when", "check_mode", "tags", "delegate_to", "ignore_errors", "failed_when"):
        assert key not in task, "the hook's install carries %r — it must run on every run of the play" % key


def _rendered(task, **values):
    """A task's text as Ansible renders it here: Jinja with Ansible's `regex_replace` (Python's `re.sub`), its
    `quote` (`shlex.quote`), its `dirname` (`os.path.dirname`), its `skipped` test (the result says it was skipped)
    and its `search` test (`re.search`)."""
    env = jinja2.Environment()
    env.tests["search"] = lambda value, pattern: re.search(pattern, str(value)) is not None
    env.filters["regex_replace"] = lambda value, pattern, repl: re.sub(pattern, repl, str(value))
    env.tests["skipped"] = lambda result: isinstance(result, dict) and bool(result.get("skipped"))
    env.filters["quote"] = lambda value: shlex.quote(str(value))
    env.filters["dirname"] = os.path.dirname
    return env.from_string(task).render(**values)


def _named(name_start):
    return next(t for t in _tasks() if t.get("name", "").startswith(name_start))


def test_the_sync_refusal_names_the_exit_of_the_check_and_a_real_run_leaves_it_to_the_sync():
    """The refusal after the sync, rendered with what each run registers: under --check the check exits 1 — the refusal
    fires and says `(--check) exited 1:` and, on the next line, the check's run-id line and sentence, each ended by one
    full stop, then `Nothing was changed`; a check that exits 3 (something to bring forward) or 0 does not fire it; on a
    real run the check is skipped (no `rc`) and the refusal does not fire, whatever the sync did. Guards the split of
    fold 4: the real run's sync fails its own task, said as this play's (`--stop-as`), so a refusal after it would never
    run — and one that read the skipped sync under --check said `exited 0` over a refusal
    (tests/integration/test_local_canonical_as_root.py's refused-check row found it)."""
    task = _named("Refuse to land when the worktree cannot be brought forward")
    msg, when = task["ansible.builtin.fail"]["msg"], "{{ %s }}" % task["when"]
    said = ["kontroll_run_id=r sync --check /wt <- refs/heads/main of /c.git",
            "kontroll-land: refs/heads/main of the canonical is a symbolic ref; nothing was changed"]
    check = {"rc": 1, "stdout_lines": said, "stderr_lines": []}
    assert _rendered(when, _sync_check=check) == "True"
    got = _rendered(msg, _sync_check=check, worktree="/wt")
    assert "main (--check) exited 1:\nkontroll_run_id=r sync --check /wt <- refs/heads/main of /c.git." in got, got
    assert "symbolic ref; nothing was changed. Nothing was changed: the canonical and the worktree" in got, got
    for rc in (0, 3):
        assert _rendered(when, _sync_check=dict(check, rc=rc)) == "False"
    assert _rendered(when, _sync_check={"skipped": True, "changed": False}) == "False"


def test_the_landing_refusal_carries_the_checks_run_id_and_ends_each_line():
    """The refusal before the landing, rendered with what `land --check` registers when it refuses (exit 1; its run-id
    line on stdout, its sentence on stderr with no full stop): the message names `(exit 1)`, carries on the next line
    the run-id line, and ends the sentence with ONE full stop before `Nothing was landed`. Guards the refusal's two
    halves: the run-id line is what correlates it with the landing's own log, and a sentence with no full stop ran into
    the next (read B-f1, N4); the integration's refusals end theirs already, so neither rule failed it when dropped
    (read B-f2, S3: R4, R6)."""
    task = _named("Refuse to land what the promote gate refuses")
    check = {"rc": 1, "stdout_lines": ["kontroll_run_id=r land --check HEAD of /wt -> refs/heads/main of /c.git"],
             "stderr_lines": ["kontroll-land: the canonical has commit(s) your worktree does not, so this landing "
                              "would discard them"]}
    got = _rendered(task["ansible.builtin.fail"]["msg"], _land_check=check, worktree="/wt")
    assert "(exit 1) —\nkontroll_run_id=r land --check HEAD of /wt" in got, got
    assert "would discard them. Nothing was landed" in got and ".." not in got, got


def test_the_busy_refusal_says_git_could_not_say_when_its_check_failed():
    """The busy check's git failed (rc 128, nothing listed): the refusal says git could not say, with git's own
    line, and does not claim an operation is in progress. Guards the refusal's two sentences: the integration
    cannot make that git fail while the listing before it succeeds, so only the rendered message shows it (read B,
    fold 1)."""
    task = _named("Refuse to commit or land while git has an operation in progress")
    msg = _rendered(task["ansible.builtin.fail"]["msg"], worktree="/wt",
                    _wt_busy={"rc": 128, "stdout_lines": [], "stderr_lines": ["fatal: not a git repository"]})
    assert "could not say" in msg and "fatal: not a git repository" in msg, msg
    assert "has an operation in progress" not in msg, msg


def test_the_busy_refusal_names_the_abort_when_a_rebase_is_in_progress():
    """The busy refusal, rendered: a rebase in progress (`rebase-merge` or `rebase-apply`) adds that a sync's replay
    cut short is put back with `git -C <worktree> rebase --abort` (the worktree quoted for the shell), that a lock a
    killed git left (`<git dir>/index.lock`, quoted) is removed first, that `git status --untracked-files=all --ignored`
    then names main's files the replay left, that `ls-files -v` names the tracked files that status does not compare, to
    compare with main's and put HEAD's back, and that `git rebase --continue` can drop the commit it was replaying; a
    merge in progress does not; and each names `git -C <worktree> status`, quoted. Guards the remedy for a rebase in
    progress with no mark beside it (the stop before this refusal stops on one that has, and keeps it until the abort),
    and `--continue` —
    "finish it", as it said before — landed on `main` without the operator's commit (read A-f5, N1); the abort
    printed failed on the lock (read B-f6, N2), a path with a space was two words, and main's files the abort left
    untracked were never named, then committed as instance state (read A-f6, N1), main's bytes left in a skip-worktree
    file were not (read A-f7, S1), and the status the refusal names first printed the worktree bare (read A-f7, N3);
    and, since read A-f8, the status is case for case (S1), the listing prints a path as it is and the comparison
    reads main's file as git's checkout writes it, the put-back covering a missing file (N2, N3); the sentence on the
    tracked files that status does not compare held whole, from the listing to `--continue`: its pieces were held
    apart, so the clauses between them could go or say HEAD's for main's (read B-f8, N2: B9-lc-pair-dropped,
    -mid-dropped, -compare-with-head-word); and, since read A-f9, the listing quotes a name as git does by default and
    says how to give a quoted one back (`$'…'`; printed as it is, a name that is not UTF-8 was dropped by grep, N2),
    and a link is compared by its text (`cmp` read the file it points to, N4); and, since read B-f11 (N1), that sentence
    is gitio's own, composed from its constants (`_LIST_FLAGGED`, `_BASHS`, `_QUOTED_NAME`, `_PUT_HEADS_BACK`): a
    literal of today's text passed this copy and its pin moved together, or gitio moved without it; and, since read
    B-f12 (N1), it is taken from `_CUT_SHORT` itself, the words between the constants included: written as the row's
    own, gitio's sentence moved by a word with its pin and passed (B13-gitio-cut-drift-word+pin)."""
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    for name, cut in (("rebase-merge", True), ("rebase-apply", True), ("MERGE_HEAD", False)):
        got = _rendered(msg, worktree="/w t", _wt_busy={"rc": 0, "stdout_lines": ["/w t/.git/" + name],
                                                        "stderr_lines": []})
        assert "/w t/.git/" + name in got and "then run again." in got, got
        assert ("`git -C '/w t' rebase --abort` puts HEAD back where it was before the replay" in got) == cut, got
        assert ("(if git says '/w t/.git/index.lock' exists and no git is running, remove that file first;"
                in got) == cut, got
        assert ("`git -C '/w t' -c core.ignoreCase=false status --untracked-files=all --ignored` names, among your "
                "own" in got) == cut, got
        assert ("can drop the commit it was replaying" in got) == cut, got
        assert "`git -C '/w t' status` says which and how" in got, got
        own = gitio._CUT_SHORT.format(wt="'/w t'", lock="LOCK")
        flagged = own[own.index("`" + gitio._LIST_FLAGGED.format(wt="'/w t'") + "`"):own.index(" can drop") + 9]
        assert flagged.endswith("`git rebase --continue` can drop"), flagged
        assert (flagged in got) == cut, got


def test_the_busy_refusal_names_the_lock_in_the_git_directory_git_named():
    """The busy refusal, rendered for a linked worktree at `/w t`, whose own git directory git names
    `/c t/.git/worktrees/w` (where its index, and so its lock, is): the lock named is that directory's `index.lock`,
    quoted. Guards the lock against `<worktree>/.git/index.lock`: in a linked worktree `.git` is a file, and that lock
    is no path at all — the suite's row had the git directory under the worktree, where the two are the same path
    (read B-f7, N1: F8-lc-lock-under-worktree)."""
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    got = _rendered(msg, worktree="/w t", _wt_busy={"rc": 0, "stdout_lines": ["/c t/.git/worktrees/w/rebase-merge"],
                                                    "stderr_lines": []})
    assert "(if git says '/c t/.git/worktrees/w/index.lock' exists" in got, got


@pytest.mark.skipif(os.name != "posix", reason="runs the printed status with bash and real git")
@pytest.mark.usefixtures("isolated_git")
def test_the_busy_refusals_status_names_a_file_of_mains_whose_name_differs_from_a_tracked_ones_only_in_case(tmp_path):
    """A worktree at `<tmp>/w t` with `core.ignoreCase` true, a tracked `ansible/filter_plugins/kontroll_token.py` and,
    beside it, an untracked `Kontroll_token.py` (a file of main's a replay cut short left), and the busy refusal
    rendered for a rebase in progress there: the status it names for main's files (`… status --untracked-files=all
    --ignored`), taken from between its backquotes and run as printed, names that file; git's own status of those
    words does not (the premise). Guards the status the deploy's refusal tells the operator names main's files the
    replay left: case-blind, it hid a filter plugin the next dry-run then loaded as root (read A-f8, S1)."""
    wt = str(tmp_path / "w t")
    plugins = os.path.join(wt, "ansible", "filter_plugins")
    os.makedirs(plugins)
    who = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test")
    with open(os.path.join(plugins, "kontroll_token.py"), "w", encoding="utf-8") as fh:
        fh.write("own\n")
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "own"],
                 ["config", "core.ignoreCase", "true"]):
        subprocess.run(["git", "-C", wt] + argv, check=True, capture_output=True, env=who)
    with open(os.path.join(plugins, "Kontroll_token.py"), "w", encoding="utf-8") as fh:
        fh.write("main's\n")
    plain = subprocess.run(["git", "-C", wt, "status", "--untracked-files=all", "--ignored"], capture_output=True)
    assert plain.returncode == 0 and b"Kontroll_token.py" not in plain.stdout, plain.stdout
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    got = _rendered(msg, worktree=wt, _wt_busy={"rc": 0, "stdout_lines": [wt + "/.git/rebase-merge"],
                                                "stderr_lines": []})
    statuses = [c for c in re.findall(r"`([^`]*)`", got) if " status --untracked-files=all" in c]
    assert statuses, got
    for command in statuses:
        p = subprocess.run(["bash", "-c", command], capture_output=True)
        assert p.returncode == 0 and b"Kontroll_token.py" in p.stdout, (command, p.stdout, p.stderr)


@pytest.mark.skipif(not (_ansible_display.PLAYBOOK and os.name == "posix"), reason="runs ansible-playbook")
def test_each_command_the_deploys_refusals_print_is_shown_as_written_by_ansibles_yaml_display(tmp_path):
    """The busy refusal (a rebase in progress, and a merge) and the refusal of a git directory the operator does not
    own, rendered for a worktree at `/srv/w t/it's $(touch INJ)` (a space, a quote and a command substitution) and shown
    by `ansible-playbook` as the tree's ansible.cfg shows a failed task: every command between backquotes in each is on
    one line of the result block it prints, as written (not of 2.19's raw `[ERROR]` line, which holds every command
    whatever the block does: the not-operator refusal's line break dropped passed on 2.19, fold 9's own mutant
    G9-lc-owner-blank-off); and each `git … status` taken from those lines and run, from a scratch directory, creates no
    `INJ` there. Every command is on one line in the refusal of a HEAD on no branch too (its `branch`, `reflog` and
    `show … | cmp`), and in the two refusals that carry `kontroll-land`'s sentence: the dry-run's sync refusal (a
    merge's `git -C … merge <id>`) and the landing check's (NOT_A_FAST_FORWARD's `--sync --worktree … --repo …`); only
    the statuses are run (read B-f10, N5). Guards the commands the operator pastes from the deploy's own output: a
    message of one line is shown as a YAML quoted scalar, each `'` doubled and the line wrapped mid-command, so the
    quoted path came apart and its `$(…)` ran when pasted (read A-f8, S2), and those two still printed so (read A-f9,
    N1)."""
    wt = "/srv/w t/it's $(touch INJ)"
    busy = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    owner = _named("Refuse to commit or land while the operator's git cannot")["ansible.builtin.fail"]["msg"]
    messages = [_rendered(busy, worktree=wt, _wt_busy={"rc": 0, "stdout_lines": [wt + "/.git/" + name],
                                                       "stderr_lines": []}) for name in ("rebase-merge", "MERGE_HEAD")]
    messages.append(_rendered(owner, worktree=wt, operator_uid=1000, operator_gid=1000, _wt_not_operator={
        "rc": 0, "stdout_lines": [wt + "/.git", wt + "/.git/config"], "stderr_lines": []}))
    messages.append(_rendered(busy, worktree=wt, _wt_busy={"rc": 0, "stdout_lines": [wt + "/.git/HEAD"],
                                                          "stderr_lines": []}))
    sync = _named("Refuse to land when the worktree cannot be brought forward")["ansible.builtin.fail"]["msg"]
    merge = ("kontroll-land: HEAD's commits since the canonical's main (%s) include a merge, which a replay would "
             "flatten; nothing was replayed and HEAD was not changed. Bring the worktree forward with a merge instead "
             "— `git -C %s merge %s` — then land again" % ("0" * 40, shlex.quote(wt), "0" * 40))
    messages.append(_rendered(sync, worktree=wt, _sync_check={
        "rc": 1, "stdout_lines": ["kontroll_run_id=r sync --check"], "stderr_lines": [merge]}))
    land = _named("Refuse to land what the promote gate refuses")["ansible.builtin.fail"]["msg"]
    messages.append(_rendered(land, worktree=wt, _land_check={
        "rc": 1, "stdout_lines": ["kontroll_run_id=r land --check"],
        "stderr_lines": ["kontroll-land: " + gitio.NOT_A_FAST_FORWARD % ("/c.git", wt, shlex.quote(wt), "/c.git")]}))
    out = _ansible_display.shown(tmp_path, messages)
    lines = out.splitlines()
    for msg in messages:
        commands = re.findall(r"`([^`]*)`", msg)
        assert any("'\"'\"'" in c for c in commands), msg      # a path quoted round its own quote
        for command in commands:
            assert any(command in line for line in lines), (command, out)
    pasted = [c for line in lines for c in re.findall(r"`([^`]*)`", line)
              if c.startswith("git -C ") and re.search(r" status( |$)", c)]
    assert pasted, out
    for command in pasted:
        subprocess.run(["bash", "-c", command], capture_output=True, cwd=str(tmp_path))
    assert not (tmp_path / "INJ").exists(), "a command pasted from the display ran the path's substitution"


@pytest.mark.skipif(os.name != "posix", reason="the check is a bash script")
def test_the_busy_check_lists_each_name_in_the_worktrees_own_git_directory_a_link_too(tmp_path):
    """The busy check's script, rendered and run on a real repository and on a linked worktree of it: nothing in
    progress lists nothing (exit 0); a `MERGE_HEAD` file and a `rebase-merge` that is a dangling link, in the
    linked worktree's OWN git directory, are both listed — by that directory and the name — and the main
    worktree, whose directory holds neither, still lists nothing. Guards the check against `--git-path`, which
    names a link's target (a dangling `rebase-merge` read as nothing in progress, where the sync's own
    `os.path.lexists` refuses it), and against the common directory, where a linked worktree's operation is not."""
    def git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True,
                              env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test",
                                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.test")
                              ).stdout.decode("utf-8").strip()
    main, linked = tmp_path / "main", tmp_path / "linked"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True, capture_output=True)
    git(main, "commit", "-q", "--allow-empty", "-m", "seed")
    git(main, "worktree", "add", "-q", "-b", "lk", str(linked))
    task = _named("List a rebase, merge, cherry-pick, revert or bisect in progress")
    with open(PLAYBOOK, encoding="utf-8") as fh:
        play_vars = _canonical_play(yaml.safe_load(fh))["vars"]

    def listed(wt):
        script = _rendered(_cmd_text(task), worktree=str(wt), as_operator="", sync_busy=play_vars["sync_busy"])
        p = subprocess.run(["bash", "-c", script], capture_output=True)
        assert p.returncode == 0, p
        return p.stdout.decode("utf-8").splitlines()
    assert listed(main) == [] and listed(linked) == []
    own = git(linked, "rev-parse", "--path-format=absolute", "--git-dir")
    assert own != git(main, "rev-parse", "--path-format=absolute", "--git-dir")
    with open(os.path.join(own, "MERGE_HEAD"), "w", encoding="ascii") as fh:
        fh.write("0" * 40 + "\n")
    os.symlink(str(tmp_path / "absent"), os.path.join(own, "rebase-merge"))
    assert listed(linked) == [own + "/rebase-merge", own + "/MERGE_HEAD"]
    assert listed(main) == []


@pytest.mark.skipif(os.name != "posix", reason="the check is a bash script")
@pytest.mark.usefixtures("isolated_git")
def test_the_busy_check_lists_a_head_on_no_branch_when_nothing_else_is_in_progress(tmp_path):
    """The busy check's script, rendered and run on a real repository: HEAD on a branch lists nothing; HEAD detached,
    as `git rebase --quit` leaves it, lists `<git dir>/HEAD`; detached with a `rebase-merge` beside it lists the rebase
    alone (its abort puts HEAD back on the branch). Guards the deploy against a state commit on a detached HEAD: a run
    killed in the pick of the operator's own commit, then `git rebase --quit`, left HEAD detached on main with nothing
    in progress, and the next run committed the half-replayed pick as instance state there and landed it, the rest of
    the operator's commits deployed by no run (read A-f9, N3)."""
    wt = str(tmp_path / "w")
    who = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test")
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "seed"]):
        subprocess.run(["git", "-C", wt] + argv if argv[0] != "init" else ["git"] + argv + [wt], check=True,
                       capture_output=True, env=who)
    own = subprocess.run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-dir"], check=True,
                         capture_output=True).stdout.decode("utf-8").strip()
    task = _named("List a rebase, merge, cherry-pick, revert or bisect in progress")
    with open(PLAYBOOK, encoding="utf-8") as fh:
        play_vars = _canonical_play(yaml.safe_load(fh))["vars"]
    script = _rendered(_cmd_text(task), worktree=wt, as_operator="", sync_busy=play_vars["sync_busy"])

    def listed():
        p = subprocess.run(["bash", "-c", script], capture_output=True)
        assert p.returncode == 0, p
        return p.stdout.decode("utf-8").splitlines()
    assert listed() == []
    subprocess.run(["git", "-C", wt, "checkout", "-q", "--detach"], check=True, capture_output=True)
    assert listed() == [own + "/HEAD"]
    os.mkdir(os.path.join(own, "rebase-merge"))
    assert listed() == [own + "/rebase-merge"]


@pytest.mark.skipif(os.name != "posix", reason="the check is a bash script")
@pytest.mark.usefixtures("isolated_git")
def test_the_busy_check_lists_nothing_for_a_branch_with_no_commit_yet(tmp_path):
    """The busy check's script, rendered and run on `git init -b main` with no commit yet: it lists nothing (HEAD
    names a branch, unborn). Guards the HEAD line against a test that asks for a commit (`rev-parse --verify
    --symbolic-full-name HEAD`): a worktree with no commit yet — a first run from a fresh `git init`, which lands the
    first instance-state commit — would be refused as on no branch (read A-f10's mutant A11-head-line-refuses-unborn,
    which only a text pin killed; its kill row)."""
    wt = str(tmp_path / "w")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    task = _named("List a rebase, merge, cherry-pick, revert or bisect in progress")
    with open(PLAYBOOK, encoding="utf-8") as fh:
        play_vars = _canonical_play(yaml.safe_load(fh))["vars"]
    script = _rendered(_cmd_text(task), worktree=wt, as_operator="", sync_busy=play_vars["sync_busy"])
    p = subprocess.run(["bash", "-c", script], capture_output=True)
    assert p.returncode == 0 and p.stdout.decode("utf-8").splitlines() == [], p


@pytest.mark.skipif(os.name != "posix", reason="the check is a bash script")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("plumbing", [
    [["update-ref", "refs/remotes/x/main", "HEAD"], ["symbolic-ref", "HEAD", "refs/remotes/x/main"]],
    [["update-ref", "refs/tags/x", "HEAD"], ["symbolic-ref", "HEAD", "refs/tags/x"]],
    [["update-ref", "refs/x/main", "HEAD"], ["symbolic-ref", "HEAD", "refs/x/main"]],
    [["update-ref", "refs/headsx/main", "HEAD"], ["symbolic-ref", "HEAD", "refs/headsx/main"]],
    [["update-ref", "refs/remotes/x/main", "HEAD"], ["symbolic-ref", "refs/heads/alias", "refs/remotes/x/main"],
     ["symbolic-ref", "HEAD", "refs/heads/alias"]],
], ids=["remote", "tag", "under-neither", "headsx", "chain-to-remote"])
def test_the_busy_check_lists_a_head_that_names_a_ref_outside_refs_heads(tmp_path, plumbing):
    """The busy check's script, rendered and run on a repository whose HEAD is a symbolic ref, as plumbing by hand
    leaves it, to a ref outside `refs/heads/`: a remote-tracking ref (`refs/remotes/x/main`), a tag, a ref under
    neither (`refs/x/main`), one whose name only begins `refs/heads` (`refs/headsx/main`), and a branch-named symref
    (`refs/heads/alias`) that points on to a remote-tracking ref: it lists `<git dir>/HEAD`; HEAD back on
    `refs/heads/main` lists nothing. Guards the HEAD line against taking any symbolic ref for a branch
    (`symbolic-ref -q HEAD` alone): the sync fetches into `refs/remotes/local/main`, so with HEAD naming it the next
    run's state commit recorded the promote's revert as instance state and landed it, the promote silently undone
    (read A-f11, N5); and, since read B-f12 (N5) and read A-f12, against a widening the one remote row did not see:
    a pattern that also took `refs/tags/`, one without the slash, or `--no-recurse` (which stops at the alias) each
    passed it, and the state commit would move a tag or a ref that is no branch (B13-head-case-widened-tags)."""
    wt = str(tmp_path / "w")
    who = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    for argv in [["commit", "-q", "--allow-empty", "-m", "seed"]] + plumbing:
        subprocess.run(["git", "-C", wt] + argv, check=True, capture_output=True, env=who)
    own = subprocess.run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-dir"], check=True,
                         capture_output=True).stdout.decode("utf-8").strip()
    task = _named("List a rebase, merge, cherry-pick, revert or bisect in progress")
    with open(PLAYBOOK, encoding="utf-8") as fh:
        play_vars = _canonical_play(yaml.safe_load(fh))["vars"]
    script = _rendered(_cmd_text(task), worktree=wt, as_operator="", sync_busy=play_vars["sync_busy"])

    def listed():
        p = subprocess.run(["bash", "-c", script], capture_output=True)
        assert p.returncode == 0, p
        return p.stdout.decode("utf-8").splitlines()
    assert listed() == [own + "/HEAD"]
    subprocess.run(["git", "-C", wt, "symbolic-ref", "HEAD", "refs/heads/main"], check=True, capture_output=True)
    assert listed() == []


def test_the_busy_refusal_says_head_is_on_no_branch_and_how_to_switch_back():
    """The busy refusal, rendered for the check's `<git dir>/HEAD` line (HEAD on no branch, nothing in progress): it
    says HEAD is on no branch in the worktree, that a replay quit part-way leaves it so, or that it names a ref that is
    not a branch (`symbolic-ref HEAD` says which; `git branch` fails, `--points-at` too, until a switch, and
    `for-each-ref --format='%(refname:lstrip=2)' refs/heads/` lists the branches meanwhile, by the name the switch
    takes), and to switch back to the branch `git -C <worktree> branch --points-at ORIG_HEAD` names after a quit (the
    one the sync started on, unless a reset, stash, merge, pull, rebase or `git am`, or a commit on it, came since), the
    reflog naming one after a switch or detach of the operator's own (a `checkout: moving from` above its newest `rebase
    (start)`) and winning where the two differ, `branch` listing the operator's; that a file in the switch's way,
    untracked or tracked with local changes, is the cut replay's copy of the operator's commit's, with the compare with
    their branch's as checkout writes it (`cat-file --filters refs/heads/<branch>:'<path>'`) and a link's by its text
    (`readlink -nv` in bash), to remove it or put HEAD's back where it is the same and move it out of the worktree where
    it differs, to a directory of its own (`mkdir -p ~/kontroll-moved && mv --backup=numbered -- … ~/kontroll-moved/`;
    moved aside inside it, the next deploy commits it as instance state), each `<path>` quoted in bash (a `'` in it
    written `'\\''`) and the put-back's read literally (`--literal-pathspecs … '<path>'`), the switch named with
    `--no-guess` — each command with the worktree quoted — not that an operation is in progress or to abort one, and
    ends with what the run did not do; a rebase in progress is said as before. Guards the remedy of the refusal that
    keeps a state commit off a detached HEAD: "finish it or abort it" names nothing to finish there (read A-f9, N3);
    after `git rebase --quit` the reflog names no branch, and the switch is refused over the cut pick's untracked copy
    of a file of the operator's commit (read A-f10, N1); and, since read A-f11, a tracked file with local changes is
    in the way too (N1), `git branch` alone does not say which of two branches the sync started on, and the wrong one
    lands unfinished work (N2), and the raw compare (`show … | cmp`) said "differs" for the operator's own file under
    an eol attribute and for every link, whose target `cmp` reads (N3; read B-f11, N3); and, since read A-f12,
    `--points-at ORIG_HEAD` named the branch a stash, a pull, `git am` or a commit on it had moved ORIG_HEAD past, or
    the sync's start tip after a detach of the operator's own (N1), a file moved aside in the worktree was the next
    deploy's instance state (N2), and a HEAD naming a ref that is not a branch was told it is on no branch while `git
    branch` failed (N3); and, since read A-f13, the reflog's newest `checkout: moving from`, older than the sync,
    outvoted `--points-at` (S1), the move overwrote a file of that name in the home and the first of two that share
    one (S2), the switch made a branch since renamed or deleted afresh from `local/main` (N1), and no listing the arm
    offered worked on a ref that is not a branch (N2); and, since read A-f14, a `<path>` filled in bare, as git lists
    it, split at a space (N1), the listing printed `heads/v1` beside a tag `v1`, a name the switch refuses, and a
    compare of `v1:<path>` read the tag (N2), and a dotfile moved into the home took effect there (read B-f14, seen);
    and, since read A-f15, the put-back read a name that starts with `:` as a pathspec's magic and put another file
    back (N1), and nothing said how to write a `'` into a `'<path>'` (N2); this docstring was fold 13's until read
    B-f14 (S1)."""
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    got = _rendered(msg, worktree="/w t", _wt_busy={"rc": 0, "stdout_lines": ["/w t/.git/HEAD"], "stderr_lines": []})
    assert "HEAD is on no branch in /w t, and no operation is in progress: a replay quit part-way" in got, got
    assert ("; or HEAD names a ref that is not a branch (`git -C '/w t' symbolic-ref HEAD` says which; `git branch` "
            "then fails, `--points-at` too, until you switch; `git -C '/w t' for-each-ref "
            "--format='%(refname:lstrip=2)' refs/heads/` lists your branches). " in got), got
    assert ("Switch back to your branch — after a replay quit part-way, `git -C '/w t' branch --points-at "
            "ORIG_HEAD` names the branch the sync started on (unless a reset, stash, merge, pull, rebase or `git am` "
            "of your own, or a commit on that branch, came since); after a switch or detach of your own, `git -C "
            "'/w t' reflog` names it (`checkout: moving from <branch>`, above its newest `rebase (start)`: one "
            "below it is older than the sync), and where the two differ the reflog's is yours; `git -C '/w t' branch` "
            "lists yours. If git says a file is in the way — "
            "untracked, or tracked with local changes — it is the cut replay's copy of your own commit's (in the bash "
            "commands below, each `'<path>'` is its name as git lists it, a `'` in it written `'\\''`): compare it "
            "with your branch's (`git -C '/w t' cat-file --filters refs/heads/<branch>:'<path>' | cmp - "
            "'/w t'/'<path>'`; for a link, its text, in bash: `git -C '/w t' cat-file -p refs/heads/<branch>:'<path>' "
            "| cmp - <(readlink -nv -- '/w t'/'<path>') && test -L '/w t'/'<path>'`); where it is the same, remove "
            "it (untracked) or put HEAD's "
            "back (`git -C '/w t' --literal-pathspecs checkout HEAD -- '<path>'`); where it differs, move it out "
            "of the worktree, to a directory of its own (`mkdir -p ~/kontroll-moved && mv --backup=numbered -- "
            "'/w t'/'<path>' ~/kontroll-moved/`), and look; then switch (`git -C '/w t' switch --no-guess "
            "<branch>`) — then run again: the next run brings your commit forward again." in got), got
    assert "has an operation in progress" not in got and "abort" not in got, got
    assert got.endswith("forward again. This run committed and landed nothing, and changed nothing in the "
                        "worktree."), got
    other = _rendered(msg, worktree="/w t", _wt_busy={"rc": 0, "stdout_lines": ["/w t/.git/rebase-merge"],
                                                      "stderr_lines": []})
    assert "has an operation in progress" in other and "on no branch" not in other, other


def test_the_busy_refusal_reads_the_head_line_by_its_end_not_a_head_in_the_worktrees_path():
    """The busy refusal, rendered for a merge in progress in a worktree whose path holds `/HEAD` (`/srv/HEAD q`,
    `/srv/HEADS`): it says an operation is in progress and names the merge, not that HEAD is on no branch. Guards the
    HEAD arm's test (`select('search', '/HEAD$')`) against losing its anchor: unanchored, any listed line in a worktree
    under a directory named `HEAD…` takes the on-no-branch arm and tells the operator to switch branches over a merge
    in progress (read A-f10's mutant A11-head-arm-search-unanchored; its kill row)."""
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    for wt in ("/srv/HEAD q", "/srv/HEADS"):
        got = _rendered(msg, worktree=wt, _wt_busy={"rc": 0, "stdout_lines": [wt + "/.git/MERGE_HEAD"],
                                                    "stderr_lines": []})
        assert "has an operation in progress" in got and "MERGE_HEAD" in got and "on no branch" not in got, got


@pytest.mark.parametrize("head", ["/srv/m/.git/worktrees/lk/HEAD", "/srv/gd/HEAD"])
def test_the_busy_refusal_says_on_no_branch_for_a_linked_worktrees_or_a_separate_git_dirs_head(head):
    """The busy refusal, rendered for the busy check's HEAD line of a linked worktree
    (`<main>/.git/worktrees/<name>/HEAD`: the check asks git for the worktree's own git directory) and of a separate
    git directory (`git init --separate-git-dir`): it takes the on-no-branch arm, not "an operation in progress".
    Guards the arm's test against an anchor on `.git/HEAD`, which every row before passed, each rendering
    `<wt>/.git/HEAD`: a linked worktree left on no branch by a quit replay would be told that `git status` names an
    operation to finish or abort (read A-f11's mutant A12-head-arm-dotgit-anchor; its kill row)."""
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    got = _rendered(msg, worktree="/srv/lk", _wt_busy={"rc": 0, "stdout_lines": [head], "stderr_lines": []})
    assert "HEAD is on no branch in /srv/lk" in got and "has an operation in progress" not in got, got


@pytest.mark.skipif(os.name != "posix", reason="the remedy's link compare is bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_head_refusals_compares_tell_the_branchs_own_file_and_link_from_another(tmp_path):
    """The busy refusal's HEAD arm, rendered for a worktree whose path holds a space and a `'`: its two compares (`git
    -C <wt> cat-file --filters refs/heads/<branch>:'<path>' | cmp - <wt>/'<path>'`, and for a link `cat-file -p … | cmp
    - <(readlink -nv -- …)`), `<branch>` and `<path>` filled in and run in bash on a branch holding a file under
    `eol=crlf` and a link: each exits 0 for the file and the link as checkout writes them, where the raw blob's compare
    of the file (`show <branch>:<path> | cmp`) exits 1, and 1 once they differ (other bytes; the link retargeted).
    Guards the way back after a replay quit part-way, which no row ran (it pinned the text): the raw compare said
    "differs" for the operator's own file under their eol attribute and for every link, whose target `cmp` reads, so an
    operator kept a file that was their branch's and could not switch (read A-f11, N3; read B-f11, N3)."""
    wt = tmp_path / "it's w"
    who = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test")

    def git(*args):
        subprocess.run(["git", "-C", str(wt)] + list(args), check=True, capture_output=True, env=who)
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True, capture_output=True)
    (wt / ".gitattributes").write_bytes(b"a.txt eol=crlf\n")
    (wt / "a.txt").write_bytes(b"x\ny\n")
    for name in ("t1", "t2"):
        (wt / name).write_bytes(name.encode("ascii") + b"\n")
    os.symlink("t2", str(wt / "l"))
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    os.remove(str(wt / "a.txt"))
    git("checkout", "--", "a.txt")
    assert (wt / "a.txt").read_bytes() == b"x\r\ny\r\n"
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    got = _rendered(msg, worktree=str(wt), _wt_busy={"rc": 0, "stdout_lines": [str(wt) + "/.git/HEAD"],
                                                     "stderr_lines": []})
    spans = re.findall(r"`([^`]+)`", got)
    plain = [c for c in spans if " cat-file --filters " in c]
    link = [c for c in spans if " cat-file -p " in c]
    assert len(plain) == 1 and len(link) == 1, spans

    def same(command, path):
        p = subprocess.run(["bash", "-c", command.replace("<branch>", "main").replace("<path>", path)],
                           capture_output=True)
        assert p.returncode in (0, 1), p
        return p.returncode == 0
    q = shlex.quote(str(wt))
    assert same(plain[0], "a.txt") and same(link[0], "l")
    assert not same("git -C %s show main:a.txt | cmp - %s/a.txt" % (q, q), "a.txt")
    (wt / "a.txt").write_bytes(b"x\r\nz\r\n")
    os.remove(str(wt / "l"))
    os.symlink("t1", str(wt / "l"))
    assert not same(plain[0], "a.txt") and not same(link[0], "l")


@pytest.mark.skipif(os.name != "posix", reason="the remedy's link compare is bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_head_refusals_compares_read_the_branch_with_head_elsewhere(tmp_path):
    """The busy refusal's HEAD arm, rendered and its two compares run (`<branch>` = main) with HEAD detached on an
    older commit whose file and link differ from main's, and the worktree holding main's file (under `eol=crlf`) and
    link, as a replay quit part-way leaves its cut copy: each compare exits 0. Guards the compares reading the branch
    and not HEAD: on the row above HEAD is main, so a compare against `HEAD:<path>` passed it, while after a quit HEAD
    is the canonical's main and that compare calls every cut copy "differs" (read A-f12's kill row)."""
    wt = tmp_path / "w"
    who = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test")

    def git(*args):
        subprocess.run(["git", "-C", str(wt)] + list(args), check=True, capture_output=True, env=who)
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True, capture_output=True)
    (wt / ".gitattributes").write_bytes(b"a.txt eol=crlf\n")
    (wt / "a.txt").write_bytes(b"p\n")
    for name in ("t1", "t2"):
        (wt / name).write_bytes(name.encode("ascii") + b"\n")
    os.symlink("t1", str(wt / "l"))
    git("add", "-A")
    git("commit", "-q", "-m", "older")
    (wt / "a.txt").write_bytes(b"x\ny\n")
    os.remove(str(wt / "l"))
    os.symlink("t2", str(wt / "l"))
    git("add", "-A")
    git("commit", "-q", "-m", "mine")
    git("checkout", "-q", "--detach", "HEAD~1")
    (wt / "a.txt").write_bytes(b"x\r\ny\r\n")
    os.remove(str(wt / "l"))
    os.symlink("t2", str(wt / "l"))
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    got = _rendered(msg, worktree=str(wt), _wt_busy={"rc": 0, "stdout_lines": [str(wt) + "/.git/HEAD"],
                                                     "stderr_lines": []})
    spans = re.findall(r"`([^`]+)`", got)
    plain = [c for c in spans if " cat-file --filters " in c]
    link = [c for c in spans if " cat-file -p " in c]
    assert len(plain) == 1 and len(link) == 1, spans
    for command, path in ((plain[0], "a.txt"), (link[0], "l")):
        p = subprocess.run(["bash", "-c", command.replace("<branch>", "main").replace("<path>", path)],
                           capture_output=True)
        assert p.returncode == 0, (command, p)


def _answering(tmp_path, answers):
    """An `as_operator` that answers each command holding a word of `answers` itself (exit 0), as the operator's git
    would where it differs from root's, and runs any other."""
    lines = ["#!/bin/bash"]
    for word, out in answers.items():
        lines.append('case "$*" in *%s*) printf "%%s\\n" %s; exit 0;; esac' % (word, shlex.quote(out)))
    lines.append('exec "$@"')
    asop = tmp_path / "asop"
    asop.write_text("\n".join(lines) + "\n")
    asop.chmod(0o755)
    return asop


@pytest.mark.skipif(os.name != "posix", reason="the check is a bash script")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("asked", ["head", "tag", "git-dir"])
def test_the_busy_checks_answers_are_the_operators(tmp_path, asked):
    """The busy check, rendered with `as_operator` a wrapper whose answers differ from the git root runs: "head" — a
    repository whose HEAD is detached, the wrapper answering `symbolic-ref` with `refs/heads/main`: nothing is listed;
    "tag" — a repository on `main`, the wrapper answering `symbolic-ref` with `refs/tags/v1`: `<git dir>/HEAD` is
    listed; "git-dir" — a repository on `main`, the wrapper answering `rev-parse` with a directory holding
    `rebase-merge`: that is what is listed. Guards each of the check's two questions being asked AND answered as the
    operator: asked as root, git refuses the operator's repository outside an image that trusts every directory, which
    the suite's images, trusting every directory, cannot see by behaviour alone (read A-f12's kill row); fold 13's row
    recorded that `git symbolic-ref -q HEAD` was run through the wrapper, so a line that asked the operator and read
    root's answer passed it, the git-dir line asked as root passed every row, and the exact line failed a `-q` written
    `2>/dev/null`, which is no fault (read B-f13, N3: B14-case-asop-read-root-np, B14-busy-dir-as-root-np; read A-f13:
    A14-case-both-reads-root-np, A13-case-noq-control); and fold 14's row dropped read A-f13's tag case, the one where
    root's git reads a branch and the operator's does not, so a line that asked root first, or took a branch from either
    answer, passed it (read B-f14, N1: B15-case-root-first-np, B15-case-both-joined-np)."""
    wt = str(tmp_path / "w")
    own = _seeded(wt)
    fake = tmp_path / "opgd"
    fake.mkdir()
    if asked == "head":
        subprocess.run(["git", "-C", wt, "checkout", "-q", "--detach"], check=True, capture_output=True)
        asop, want = _answering(tmp_path, {"symbolic-ref": "refs/heads/main"}), []
    elif asked == "tag":
        asop, want = _answering(tmp_path, {"symbolic-ref": "refs/tags/v1"}), [own + "/HEAD"]
    else:
        (fake / "rebase-merge").mkdir()
        asop, want = _answering(tmp_path, {"rev-parse": str(fake)}), [str(fake) + "/rebase-merge"]
    task = _named("List a rebase, merge, cherry-pick, revert or bisect in progress")
    with open(PLAYBOOK, encoding="utf-8") as fh:
        play_vars = _canonical_play(yaml.safe_load(fh))["vars"]
    script = _rendered(_cmd_text(task), worktree=wt, as_operator=str(asop), sync_busy=play_vars["sync_busy"])
    p = subprocess.run(["bash", "-c", script], capture_output=True)
    assert p.returncode == 0 and p.stdout.decode("utf-8").splitlines() == want, p


# The test's author, merged into the environment at each call: an `os.environ` copied when the module is imported
# would carry the runner's GIT_DIR or GIT_*_PATHSPECS past `isolated_git` (read B-f17, S1).
_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test"}


def _seeded(wt):
    """A repository at `wt` on `main` with one empty commit; returns its git directory."""
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    subprocess.run(["git", "-C", wt, "commit", "-q", "--allow-empty", "-m", "seed"], check=True, capture_output=True,
                   env=dict(os.environ, **_WHO))
    return subprocess.run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-dir"], check=True,
                          capture_output=True).stdout.decode("utf-8").strip()


def _head_arm_text(wt):
    """The busy refusal's HEAD arm, rendered for `wt`, its line breaks folded."""
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    return _rendered(msg, worktree=wt, _wt_busy={"rc": 0, "stdout_lines": [wt + "/.git/HEAD"], "stderr_lines": []})


def _head_arm_span(wt, start):
    """The one code span of the busy refusal's HEAD arm, rendered for `wt`, that starts with `start`."""
    spans = [c for c in re.findall(r"`([^`]+)`", _head_arm_text(wt)) if c.startswith(start)]
    assert len(spans) == 1, spans
    return spans[0]


def _wt_git(wt, *args, check=True):
    """git -C wt <args> as the test's author; the completed process."""
    return subprocess.run(["git", "-C", wt] + list(args), check=check, capture_output=True,
                          env=dict(os.environ, **_WHO))


def _wt_commit(wt, name, text, msg):
    """Write `name` with `text` in `wt` and commit it."""
    with open(os.path.join(wt, name), "w") as fh:
        fh.write(text)
    _wt_git(wt, "add", "--", name)
    _wt_git(wt, "commit", "-q", "-m", msg)


def _bash(cmd, **kw):
    """`cmd` run in bash, its output captured, no stdin; the completed process."""
    return subprocess.run(["bash", "-c", cmd], capture_output=True, stdin=subprocess.DEVNULL, **kw)


@pytest.mark.usefixtures("_runner_git", "isolated_git")
def test_the_tests_git_sees_the_fixtures_environment_not_the_import_times(tmp_path, monkeypatch):
    """`_wt_git`, called under `isolated_git` with a hook's variables set before it ran (`_runner_git`), hands git the
    environment the fixture made: its `GIT_CONFIG_GLOBAL` (the empty file), and no `GIT_DIR`; and `_WHO`, merged into
    it at each call, holds the author's four names and nothing else. Guards `_WHO` as a copy of `os.environ` taken
    when the module is imported, before any fixture runs (passed as the environment, or merged over the fixture's):
    every `_wt_git` call and `_seeded`'s commit then carried the runner's own environment, so a suite started with
    `GIT_DIR` (or `GIT_INDEX_FILE`) naming the developer's repository, as a git hook hands it down, committed and
    staged the tests' files there, and the runner's global config (hooks, signing) reached the scratch repositories,
    as conftest's `isolated_git` says it must not (read B-f17, S1: R18-who-at-import). The hook's variables are set
    first since read B-f18, N2: run on a runner that set none, "no `GIT_DIR`" held whatever the fixture removed."""
    seen = {}

    def fake(cmd, **kw):
        seen.update(os.environ if kw.get("env") is None else kw["env"])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake)
    _wt_git(str(tmp_path), "status")
    assert seen.get("GIT_CONFIG_GLOBAL") == os.environ["GIT_CONFIG_GLOBAL"], seen.get("GIT_CONFIG_GLOBAL")
    assert "GIT_DIR" not in seen, seen.get("GIT_DIR")
    assert sorted(_WHO) == ["GIT_AUTHOR_EMAIL", "GIT_AUTHOR_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_NAME"], _WHO


# What a git hook, a parent git or the runner's shell may hand the suite: the repository a hook runs in, a parent's
# keys and switches, a pull's reflog message, a namespace, a quarantine, a template and an attribute source, and the
# four pathspec readings.
_RUNNER_GIT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
               "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_NO_REPLACE_OBJECTS", "GIT_DEFAULT_HASH",
               "GIT_REFLOG_ACTION", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_TEMPLATE_DIR", "GIT_ATTR_SOURCE",
               "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")
_RUNNER_PATHS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                 "GIT_QUARANTINE_PATH", "GIT_TEMPLATE_DIR")


@pytest.fixture
def _runner_git(monkeypatch, tmp_path):
    """Each of `_RUNNER_GIT` set, as a hook or a runner's shell hands them down: the ones that name a path naming a
    `runner` directory nothing else uses, the rest `1` (requested before `isolated_git`, so the fixture finds them
    set)."""
    for v in _RUNNER_GIT:
        monkeypatch.setenv(v, str(tmp_path / "runner") if v in _RUNNER_PATHS else "1")


@pytest.mark.usefixtures("_runner_git", "isolated_git")
def test_isolated_git_leaves_no_variable_of_gits_but_its_own_two():
    """With the variables a hook, a parent git or the runner's shell hands down set before it runs (`_RUNNER_GIT`),
    `isolated_git` leaves no `GIT_*` set but its `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM`. Guards the removal,
    which named its variables one by one: GIT_DIR or GIT_INDEX_FILE could leave the list with no row seeing it (the
    row that read "no GIT_DIR" ran on a runner that set none; read B-f18, N2: R19-isolated-keeps-gitdir,
    R19-isolated-keeps-index), a pull hook's GIT_REFLOG_ACTION, which the list never named, failed two reflog rows
    and GIT_NAMESPACE or GIT_QUARANTINE_PATH more (read B-f18, N1), and with a runner's GIT_LITERAL_PATHSPECS=1 left
    in, the put-back without `--literal-pathspecs` (O17-no-literal+pin) passed every row (read B-f17, S1:
    R18-isolated-keeps-pathspecs)."""
    assert sorted(k for k in os.environ if k.startswith("GIT_")) == ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"]


@pytest.mark.usefixtures("_runner_git", "isolated_git")
def test_seeded_runs_every_git_in_the_fixtures_environment(tmp_path, monkeypatch):
    """`_seeded`, called under `isolated_git` with a hook's variables set before it ran: each of its three gits is
    handed the fixture's `GIT_CONFIG_GLOBAL` and no `GIT_DIR`, and it returns the git directory git names. Guards
    `_seeded`'s commit, which the S1 row's docstring names but which that row does not run (it calls `_wt_git`
    alone): the commit given `_WHO` as its whole environment, or an `os.environ` copied at import, passed every row
    (read B-f18, N2: R19-seeded-who-only, R19-seeded-import-env)."""
    seen = []

    def fake(cmd, **kw):
        seen.append(dict(os.environ if kw.get("env") is None else kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, b"/x/.git\n", b"")

    monkeypatch.setattr(subprocess, "run", fake)
    assert _seeded(str(tmp_path / "w")) == "/x/.git"
    want = os.environ["GIT_CONFIG_GLOBAL"]
    assert [(e.get("GIT_CONFIG_GLOBAL") == want, "GIT_DIR" in e) for e in seen] == [(True, False)] * 3, seen


def _reflog_rule(wt):
    """The arm's reflog rule, as its words read: run the printed reflog command, take the `checkout: moving from`
    lines on the side of the newest anchor line the words name (above it or below it), and return the first name
    there (a branch, or an id; or None), with whether the anchor is in the reflog at all."""
    m = re.search(r"`checkout: moving from <branch>`, (above|below) its newest `([^`]+)`", _head_arm_text(wt))
    assert m, _head_arm_text(wt)
    side, anchor = m.groups()
    rl = _head_arm_span(wt, "git -C %s reflog" % shlex.quote(wt))
    lines = subprocess.run(["bash", "-c", rl], check=True, capture_output=True).stdout.decode().splitlines()
    at = next((i for i, line in enumerate(lines) if (": " + anchor + ": ") in line), None)
    part = lines if at is None else (lines[:at] if side == "above" else lines[at + 1:])
    for line in part:
        n = re.search(r"checkout: moving from (\S+) to ", line)
        if n:
            return n.group(1), at is not None
    return None, at is not None


def _promoted(wt, up, name, text):
    """A promote's commit, as the sync's onto is one: `wt`'s main cloned to `up`, `name` committed there with `text`,
    and fetched back into `wt`; its id. Fetched, it is no branch of the worktree's and no line of its HEAD reflog."""
    subprocess.run(["git", "clone", "-q", "-b", "main", wt, up], check=True, capture_output=True,
                   env=dict(os.environ, **_WHO))
    _wt_commit(up, name, text, "a promote")
    _wt_git(wt, "fetch", "-q", up, "main")
    return _wt_git(wt, "rev-parse", "FETCH_HEAD").stdout.decode().strip()


def _quit_after_switch(wt, up):
    """Read A-f13 S1b's shape: `feature` worked on and left (`switch feature; commit; switch main`), a commit of the
    operator's on main, a replay of it onto a promote's commit fetched as the sync fetches the canonical's main,
    stopped at its first pick and quit (`git rebase --quit`)."""
    _seeded(wt)
    _wt_commit(wt, "f.txt", "base\n", "base")
    _wt_git(wt, "switch", "-q", "-c", "feature")
    _wt_commit(wt, "feat.txt", "wip\n", "feature work")
    _wt_git(wt, "switch", "-q", "main")
    onto = _promoted(wt, up, "f.txt", "the promote's\n")
    _wt_commit(wt, "f.txt", "mine\n", "the operator's own")
    assert _wt_git(wt, "rebase", "-q", onto, check=False).returncode != 0, "the replay stops on its pick"
    _wt_git(wt, "rebase", "--quit")


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_reflog_rule_and_points_at_name_the_branch_the_replay_started_on(tmp_path):
    """The HEAD arm's after-a-quit remedy, run on git's own state: on read A-f13 S1b's shape (a switch from `feature`
    before the sync, a replay quit part-way), the printed `branch --points-at ORIG_HEAD` names `main`, the words'
    anchor line is in the printed reflog, and the words' reflog rule names no branch on their side of it — so the
    two do not differ and the operator switches to `main`. Guards the rule's scope turned over (`below` its anchor
    names `feature`, whose unfinished work the runs then land: read A-f13, S1), an anchor a quit never writes
    (`rebase (finish)`: the whole reflog is read, and `feature` wins again), and `--points-at HEAD` (the quit's
    detached tip: no branch) — rows that move the arm, its pin and §10 together, which no text pin can kill (read
    A-f14's kill row: A15-reflog-scope-inverted+pin, A15-reflog-anchor-finish+pin, A15-pointsat-head+pin; its
    onto, a commit the worktree made on a detached HEAD, put three lines in the reflog a sync's does not, two of them
    `checkout:` lines, the newest of those naming an id where `feature` stood)."""
    wt = str(tmp_path / "it's w")
    _quit_after_switch(wt, str(tmp_path / "up"))
    pa = _head_arm_span(wt, "git -C %s branch --points-at" % shlex.quote(wt))
    names = subprocess.run(["bash", "-c", pa], check=True, capture_output=True).stdout.decode().split()
    assert names == ["main"], names
    assert _reflog_rule(wt) == (None, True)


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_reflog_rule_names_the_branch_a_detach_left_with_a_commit_since(tmp_path):
    """The HEAD arm's after-a-detach remedy, run on git's own state: a replay that finished (as a sync's does), then
    a detach of the operator's own and a commit on the detached HEAD — the words' reflog rule, read from the printed
    reflog, names `main`. Guards the printed reflog cut to its newest line (`reflog -1`: the commit's line, no
    branch) and the rule turned over (`below` the anchor: `feature`, older than the sync) — each with the arm, its
    pin and §10 moved together (read A-f14's kill row: A15-reflog-dash1+pin, A15-reflog-scope-inverted+pin)."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    _wt_commit(wt, "f.txt", "base\n", "base")
    _wt_git(wt, "switch", "-q", "-c", "feature")
    _wt_commit(wt, "feat.txt", "wip\n", "feature work")
    _wt_git(wt, "switch", "-q", "main")
    onto = _promoted(wt, str(tmp_path / "up"), "h.txt", "the promote's\n")
    _wt_commit(wt, "g.txt", "mine\n", "the operator's own")
    _wt_git(wt, "rebase", "-q", onto)
    _wt_git(wt, "switch", "-q", "--detach")
    _wt_commit(wt, "i.txt", "on the detached HEAD\n", "a commit on the detached HEAD")
    assert _reflog_rule(wt) == ("main", True)


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_switch_takes_only_a_branch_of_the_worktrees_own(tmp_path):
    """The HEAD arm's `git -C <wt> switch --no-guess <branch>`, rendered and run on a detached HEAD in a repository
    whose remote has a branch `rel` it does not: with `rel` it fails, makes no branch and leaves HEAD detached; with
    `main` HEAD then names `refs/heads/main`. Guards a guessing switch (git's guess, and `--no-track`, which still
    guesses, make a branch the operator renamed or deleted afresh from a remote's, and the runs land nothing of
    theirs: read A-f13, N1), which the arm's pin and §10's row, holding the word, passed with the guess back in all
    three, and a switch that leaves HEAD on no branch (`--detach`: the next run refuses again) — each with the arm,
    its pin and §10 moved together (read A-f14's kill row: A15-switch-no-track+pin, A15-switch-detach+pin; read
    B-f14, N2: B15-switch-guess+pin)."""
    up = str(tmp_path / "up")
    wt = str(tmp_path / "w")
    _seeded(up)
    _wt_git(up, "branch", "rel")
    subprocess.run(["git", "clone", "-q", up, wt], check=True, capture_output=True, env=dict(os.environ, **_WHO))
    _wt_git(wt, "switch", "-q", "--detach")
    sw = _head_arm_span(wt, "git -C %s switch" % shlex.quote(wt))
    p = subprocess.run(["bash", "-c", sw.replace("<branch>", "rel")], capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode != 0, p
    assert _wt_git(wt, "for-each-ref", "refs/heads/rel").stdout == b""
    assert _wt_git(wt, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0
    p = subprocess.run(["bash", "-c", sw.replace("<branch>", "main")], capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p
    assert _wt_git(wt, "symbolic-ref", "HEAD").stdout == b"refs/heads/main\n"


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_listing_names_the_branches_and_nothing_else(tmp_path):
    """The HEAD arm's `for-each-ref` listing, rendered and run on a repository whose HEAD names a tag (the case it
    is printed for), where `git branch` fails as the arm says, and which holds branches `main` and `feature`, a tag,
    a remote's branch and a ref under `refs/kontroll/`, and a branch `op/work`: it prints `feature`, `main` and
    `op/work`, one a line, nothing else. Guards the listing widened past `refs/heads/` (a tag or a remote's branch
    offered as "your branches"), printing ids for names, and `%(refname)`'s `refs/heads/main`, which the printed
    switch refuses — each with the arm, its pin and §10 moved together, which the text pins passed (read A-f14's
    kill row: A15-fe-all-refs+pin, A15-fe-objectname+pin; read B-f14, N2: B15-foreach-all-refs+pin,
    B15-foreach-long+pin); and a branch whose name holds a slash, which no row had: `%(refname:lstrip=-1)` prints
    `work`, a name the switch refuses, and a pattern `'refs/heads/*'`, a glob that stops at a slash, leaves
    `op/work` out (read A-f15's kill row: A16-fe-lstrip-neg1+pin, A16-fe-glob+pin; read B-f15, N2:
    B16-fe-lstrip-neg1+pin, B16-fe-glob-star+pin)."""
    up = str(tmp_path / "up")
    wt = str(tmp_path / "w")
    _seeded(up)
    _wt_git(up, "branch", "rel")
    subprocess.run(["git", "clone", "-q", up, wt], check=True, capture_output=True, env=dict(os.environ, **_WHO))
    _wt_git(wt, "branch", "feature")
    _wt_git(wt, "branch", "op/work")
    _wt_git(wt, "tag", "t1")
    _wt_git(wt, "update-ref", "refs/kontroll/x", "HEAD")
    _wt_git(wt, "symbolic-ref", "HEAD", "refs/tags/t1")
    assert _wt_git(wt, "branch", check=False).returncode != 0
    fe = _head_arm_span(wt, "git -C %s for-each-ref" % shlex.quote(wt))
    p = subprocess.run(["bash", "-c", fe], capture_output=True)
    assert p.returncode == 0 and p.stdout == b"feature\nmain\nop/work\n", p


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_listing_compares_and_switch_read_a_branch_beside_a_tag_of_its_name(tmp_path):
    """On a repository with a branch `v1` and a tag `v1` whose file `x y.txt` and link `l k` differ, HEAD detached on
    the tag and the worktree holding the branch's file and link: the HEAD arm's listing prints `v1`; its two compares,
    given `v1` and each name as git lists it (bare), exit 0, where the same compares of `v1:<path>` (the tag, as git
    resolves the name) exit 1; its put-back, given `x y.txt`, puts HEAD's file back; and its switch given `v1` puts
    HEAD on `refs/heads/v1`. Guards read A-f14 N2: `%(refname:short)` printed `heads/v1` when a tag shares the name,
    and `switch --no-guess heads/v1` fails (`a branch is expected`), and a compare of `<branch>:<path>` read the tag's
    file and called the branch's own copy "differs"; and read A-f14 N1 beyond the move: a `<path>` written bare split
    at the space, so each compare and the put-back failed on a name git lists unquoted."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    xy, lk = os.path.join(wt, "x y.txt"), os.path.join(wt, "l k")
    _wt_commit(wt, "x y.txt", "the branch's\n", "the branch's file")
    os.symlink("t1", lk)
    _wt_git(wt, "add", "--", "l k")
    _wt_git(wt, "commit", "-q", "-m", "the branch's link")
    _wt_git(wt, "branch", "v1")
    _wt_commit(wt, "x y.txt", "the tag's\n", "the tag's file")
    os.remove(lk)
    os.symlink("t2", lk)
    _wt_git(wt, "add", "--", "l k")
    _wt_git(wt, "commit", "-q", "-m", "the tag's link")
    _wt_git(wt, "tag", "v1")
    _wt_git(wt, "switch", "-q", "--detach", "refs/tags/v1")
    with open(xy, "w") as fh:
        fh.write("the branch's\n")
    os.remove(lk)
    os.symlink("t1", lk)
    q = shlex.quote(wt)
    fe = _head_arm_span(wt, "git -C %s for-each-ref" % q)
    names = subprocess.run(["bash", "-c", fe], check=True, capture_output=True).stdout.decode().split()
    assert "v1" in names and "heads/v1" not in names, names
    for cmp, path in ((_head_arm_span(wt, "git -C %s cat-file --filters" % q), "x y.txt"), (_link_compare(wt), "l k")):
        cmp = cmp.replace("<branch>", "v1").replace("<path>", path)
        assert subprocess.run(["bash", "-c", cmp], capture_output=True).returncode == 0, cmp
        tag = cmp.replace("refs/heads/v1:", "v1:")
        assert tag != cmp and subprocess.run(["bash", "-c", tag], capture_output=True).returncode == 1, tag
    back = _put_back(wt).replace("<path>", "x y.txt")
    assert subprocess.run(["bash", "-c", back], capture_output=True).returncode == 0, back
    with open(xy) as fh:
        assert fh.read() == "the tag's\n"
    _wt_git(wt, "checkout", "--", "l k")
    assert _wt_git(wt, "status", "--porcelain").stdout == b""
    sw = _head_arm_span(wt, "git -C %s switch" % q)
    p = subprocess.run(["bash", "-c", sw.replace("<branch>", "v1")], capture_output=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p
    assert _wt_git(wt, "symbolic-ref", "HEAD").stdout == b"refs/heads/v1\n"


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_move_filled_in_as_git_lists_the_name_moves_that_file_only(tmp_path):
    """The HEAD arm's move, its `<path>` filled in as git lists a file in the switch's way (bare: git does not quote
    a space there) for `modules/alpha copy/module.yml` beside a tracked `modules/alpha/`, run from `/` with HOME the
    test's: the file is in `~/kontroll-moved/`, the home holds nothing else, and nothing tracked has left the
    worktree. Guards read A-f14 N1: with the arm's `<path>` bare, mv read two sources, moved the tracked
    `modules/alpha/` into HOME, failed on the rest (rc 1), and the next run's state commit landed the module's
    deletion as instance state."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    os.makedirs(os.path.join(wt, "modules", "alpha"))
    _wt_commit(wt, "modules/alpha/module.yml", "key: alpha\n", "alpha")
    os.makedirs(os.path.join(wt, "modules", "alpha copy"))
    with open(os.path.join(wt, "modules", "alpha copy", "module.yml"), "w") as fh:
        fh.write("mine\n")
    home = tmp_path / "home"
    home.mkdir()
    mv = _head_arm_span(wt, "mkdir ")
    p = subprocess.run(["bash", "-c", mv.replace("<path>", "modules/alpha copy/module.yml")], capture_output=True,
                       env=dict(os.environ, HOME=str(home)), cwd="/", stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p
    st = _wt_git(wt, "status", "--porcelain", "--untracked-files=all").stdout
    assert st == b"", st
    moved = sorted(f.relative_to(home).as_posix() for f in home.rglob("*"))
    assert moved == ["kontroll-moved", "kontroll-moved/module.yml"], moved


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_head_arms_move_takes_the_file_out_of_the_worktree_and_keeps_what_is_in_home(tmp_path):
    """The HEAD arm's move, rendered for a worktree whose path holds a space and a `'` and run in bash with HOME the
    test's, which holds a `main.yml` of the operator's own, for three files in the way that share a name (`a/main.yml`,
    `b/main.yml`, `c/main.yml`, as roles' tasks do), one after the other: each is out of the worktree (`git status
    --untracked-files=all --ignored` is empty) and in `~/kontroll-moved/`, each kept beside the next, and the home holds
    nothing else, its `main.yml` as it was. Guards the move being OUT of the worktree: moved beside itself or to the
    worktree's top, the next deploy's state commit lands it on the canonical as instance state (read A-f12, N2), and no
    row ran the printed command (read A-f13: A14-mv-in-wt+pin, A14-mv-dest-wt-top+pin); and the move overwriting what it
    lands on: a plain `mv` replaces an existing `~/<name>` without a word, and a file the arm moves out is one that
    differs (read A-f13, S2: fold 13's `mv --` lost the operator's file and the first of the two), and a backup kept
    once, not numbered (`--backup=simple`, or `existing` where none is numbered yet), loses the first of three: the move
    into a directory of its own left both alive on two files (read B-f14's B15-mv-backup-simple+pin and
    B15-mv-backup-existing+pin); and the move into the home itself, where a moved dotfile (`.sops.yaml`, `.yamllint`)
    takes effect for the operator's own commands (read B-f14, seen)."""
    wt = str(tmp_path / "it's w")
    _seeded(wt)
    home = tmp_path / "home"
    home.mkdir()
    (home / "main.yml").write_text("their own\n")
    for d, text in (("a", "A edit\n"), ("b", "B edit\n"), ("c", "C edit\n")):
        os.makedirs(os.path.join(wt, d))
        with open(os.path.join(wt, d, "main.yml"), "w") as fh:
            fh.write(text)
    mv = _head_arm_span(wt, "mkdir ")
    for d in ("a", "b", "c"):
        p = subprocess.run(["bash", "-c", mv.replace("<path>", d + "/main.yml")], capture_output=True,
                           env=dict(os.environ, HOME=str(home)), cwd="/", stdin=subprocess.DEVNULL)
        assert p.returncode == 0, p
    st = subprocess.run(["git", "-C", wt, "status", "--porcelain", "--untracked-files=all", "--ignored"], check=True,
                        capture_output=True).stdout
    assert st == b"", st
    assert sorted(p.name for p in home.iterdir()) == ["kontroll-moved", "main.yml"]
    assert (home / "main.yml").read_text() == "their own\n"
    assert sorted(f.read_text() for f in (home / "kontroll-moved").iterdir()) == ["A edit\n", "B edit\n", "C edit\n"]


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("shape", ["tag", "chain"])
def test_the_head_arms_symbolic_ref_names_the_ref(tmp_path, shape):
    """The HEAD arm's `git -C <wt> symbolic-ref HEAD`, rendered and run on a repository whose HEAD names a tag
    (`refs/tags/v1`), and on one whose HEAD names `refs/heads/alias`, a symbolic ref to that tag (the busy check
    lists it: its own `symbolic-ref -q HEAD` follows the chain): it prints `refs/tags/v1`, the kind of ref with its
    name. Guards "says which": asked `--short` it prints `v1`, and the operator cannot tell a tag from a branch (read
    A-f13: A14-symref-short+pin); asked `--no-recurse` on the chain, it prints `refs/heads/alias`, a name under
    `refs/heads/`, for a HEAD the arm has just called no branch (read B-f14, N2: B15-symref-no-recurse+pin2)."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    _wt_git(wt, "update-ref", "refs/tags/v1", "HEAD")
    if shape == "chain":
        _wt_git(wt, "symbolic-ref", "refs/heads/alias", "refs/tags/v1")
        _wt_git(wt, "symbolic-ref", "HEAD", "refs/heads/alias")
    else:
        _wt_git(wt, "symbolic-ref", "HEAD", "refs/tags/v1")
    sr = _head_arm_span(wt, "git -C %s symbolic-ref" % shlex.quote(wt))
    p = subprocess.run(["bash", "-c", sr], capture_output=True)
    assert p.returncode == 0 and p.stdout == b"refs/tags/v1\n", p


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_head_arms_symbolic_ref_says_a_detached_head_is_no_symbolic_ref(tmp_path):
    """The HEAD arm's `git -C <wt> symbolic-ref HEAD`, rendered and run on a detached HEAD (the replay quit part-way
    that the same refusal names first): it fails and says HEAD is not a symbolic ref. Guards "says which" for the case
    the operator runs it to tell apart: `symbolic-ref -q HEAD` prints the same for a ref, but nothing at all for a
    detached HEAD (exit 1, no word), and passed every row, which ran it only on a tag and a chain (read B-f15, N1:
    B15-symref-q+pin2, which fold 15's message called an equivalent)."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    _wt_git(wt, "switch", "-q", "--detach")
    p = _bash(_head_arm_span(wt, "git -C %s symbolic-ref" % shlex.quote(wt)))
    assert p.returncode != 0 and b"not a symbolic ref" in p.stderr, p


def _put_back(wt):
    """The busy refusal's HEAD arm's put-back, rendered for `wt`: the one code span that runs `checkout` (so a
    mutant of its arguments moves no locator)."""
    spans = [c for c in re.findall(r"`([^`]+)`", _head_arm_text(wt)) if " checkout " in c]
    assert len(spans) == 1, spans
    return spans[0]


def _link_compare(wt):
    """The busy refusal's HEAD arm's link compare, rendered for `wt`: the one code span that runs `cat-file -p`,
    whatever comes before or after it (so the `test -L` last, or a mutant of its arguments, moves no locator)."""
    spans = [c for c in re.findall(r"`([^`]+)`", _head_arm_text(wt)) if " cat-file -p " in c]
    assert len(spans) == 1, spans
    return spans[0]


def _branch_link(wt, name, target):
    """`wt` seeded, a link `name` to `target` committed on a branch `op`, then HEAD detached on `main`, where `name`
    is not tracked and so not in the worktree."""
    _seeded(wt)
    _wt_git(wt, "switch", "-q", "-c", "op")
    os.symlink(target, os.path.join(wt, name))
    _wt_git(wt, "--literal-pathspecs", "add", "--", name)
    _wt_git(wt, "commit", "-q", "-m", "a link")
    _wt_git(wt, "switch", "-q", "--detach", "main")


def _tracked_each_changed(wt, names):
    """`wt` seeded, each of `names` committed holding "HEAD's", then each given a local change of its own ("my edit
    of <name>")."""
    _seeded(wt)
    for f in names:
        os.makedirs(os.path.dirname(os.path.join(wt, f)), exist_ok=True)
        with open(os.path.join(wt, f), "w") as fh:
            fh.write("HEAD's\n")
        _wt_git(wt, "--literal-pathspecs", "add", "--", f)
        _wt_git(wt, "commit", "-q", "-m", "a file")
    for f in names:
        with open(os.path.join(wt, f), "w") as fh:
            fh.write("my edit of %s\n" % f)


def _text_of(wt, f):
    """The text of `f` in `wt`."""
    with open(os.path.join(wt, f)) as fh:
        return fh.read()


def _no_case_fold(tmp_path):
    """Skip where the file system folds case: there `a.txt` and `A.txt` are one file."""
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "a").write_text("")
    if (probe / "A").exists():
        pytest.skip("this file system folds case")


def test_the_case_fold_probe_skips_only_where_two_names_that_differ_in_case_are_one_file(tmp_path):
    """`_no_case_fold` beside a directory where `x` and `X` were each written: where that listing holds two names
    (the file system keeps case) the probe does not skip, and where it holds one it does. Guards the probe that
    decides whether the case-twin and ICASE rows run at all: a probe that asks for the file it made, or the inverted
    test, skips both rows on every Linux runner and passes every row (a skip is no failure), so the rows fold 17
    added for A-f16 N1 held nothing (read B-f17, N5: B18T-nocasefold-always-skips, B18T-nocasefold-inverted)."""
    probe = tmp_path / "independent"
    probe.mkdir()
    (probe / "x").write_text("1")
    (probe / "X").write_text("2")
    keeps_case = len(os.listdir(probe)) == 2
    sub = tmp_path / "for-the-helper"
    sub.mkdir()
    if keeps_case:
        try:
            _no_case_fold(sub)
        except pytest.skip.Exception:
            pytest.fail("_no_case_fold skipped on a file system that keeps case")
    else:
        with pytest.raises(pytest.skip.Exception):
            _no_case_fold(sub)


@pytest.mark.skipif(os.name != "posix", reason="the names hold `:` and `*`")
@pytest.mark.usefixtures("isolated_git")
def test_each_name_the_put_back_rows_track_holds_its_own_change(tmp_path):
    """`_tracked_each_changed` on three names: each holds "my edit of <name>", and `git status` lists all three
    modified. Guards the helper the put-back rows build on: one that leaves a name as HEAD's passes every put-back
    row but the ICASE one (which skips where the file system folds case), since a put-back that does nothing then
    finds HEAD's text there anyway (read B-f17's kill row: B18T-tec-name-left-as-head)."""
    wt = str(tmp_path / "w")
    names = [":x.txt", "a*.txt", "d/y.txt"]
    _tracked_each_changed(wt, names)
    for f in names:
        assert _text_of(wt, f) == "my edit of %s\n" % f, f
    st = _wt_git(wt, "-c", "core.quotePath=false", "status", "--porcelain").stdout.decode().splitlines()
    assert sorted(line[3:] for line in st if line.startswith(" M ")) == sorted(names), st


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("name, other", [(":x.txt", "x.txt"), (":!x.txt", "y.txt"), (":/x.txt", "x.txt"),
                                         ("-lead.txt", "x.txt"), ("a*.txt", "ab.txt"), ("a[b].txt", "ab.txt"),
                                         ("d/*.md", "d/sub/x.md"), ("HEAD", "x.txt"), ("a$HOME.txt", "x.txt"),
                                         ("a\\*.txt", "a*.txt"), ("a\\tb.txt", "x.txt")])
def test_the_printed_put_back_puts_back_that_file_only(tmp_path, name, other):
    """Tracked files `name` and `other`, each with a local change: the HEAD arm's put-back, given `name` as git lists
    it, exits 0, puts HEAD's `name` back and leaves `other`'s change as it was. Guards the put-back read as a pathspec
    (read A-f15, N1): a name that starts with `:` is magic to git, so `checkout HEAD -- ':x.txt'` put back `x.txt`, not
    `:x.txt`, and `':!x.txt'` every tracked file but `x.txt`, at exit 0, the operator's edit made after the quit gone
    without a word and the switch still refused. The glob names (`a*.txt`, `a[b].txt`, `d/*.md`) passed before too, git
    taking a tracked name's literal match first; they hold the literal reading to the same, and `a\\*.txt` beside a
    tracked `a*.txt` is one they do not: read as a glob, or after `:(top)`, its backslash escapes the `*`, so both are
    put back at exit 0 and the switch goes with `a*.txt`'s change gone (read A-f16's kill row:
    A17-putback-glob+pin, A17-putback-top+pin). The `--` guards two things: a name that starts with `-` (without it git
    reads `-lead.txt` as options: "unknown switch `e'"), and a file named `HEAD` at the worktree's top, which makes the
    revision `HEAD` ambiguous whatever name is put back ("ambiguous argument 'HEAD'") (read A-f16, N2:
    A17-putback-nodashdash+pin, which `-lead.txt` kills first; read A-f17, N2). `a$HOME.txt` holds the single quotes: in
    double quotes bash puts the home in the name, and git finds no such file (A17-putback-dq+pin); `a\\tb.txt` (a
    backslash, then `t`) holds them against `$'<path>'`, where bash reads `\\t` as a tab and git finds no such file
    (read B-f17, N7: B18-putback-ansi-c+pin)."""
    wt = str(tmp_path / "w")
    _tracked_each_changed(wt, [name, other])
    p = _bash(_put_back(wt).replace("<path>", name))
    assert p.returncode == 0, p
    assert _text_of(wt, name) == "HEAD's\n"
    assert _text_of(wt, other) == "my edit of %s\n" % other


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("name", ["it's.txt", "'lead.txt", "trail'", "a''b.txt", "it's/x.txt", "a' 'x.txt"])
def test_the_printed_put_back_takes_a_name_with_a_quote_written_as_the_rule_says(tmp_path, name):
    """Tracked `name` (a `'` in it at the start, at the end, two in a row, in a directory part, two apart) beside
    tracked `a` and `x.txt`, each with a local change: the put-back, each `'` in the name written as the arm's rule
    says, exits 0, puts HEAD's back and leaves the others' changes. Guards the rule's reach into the put-back (read
    A-f16, N3): filled in raw, an odd count of `'` leaves bash a quote it never closes, and an even count splits the
    name in two, so `a' 'x.txt` put back the tracked `a` and `x.txt` at exit 0, both edits gone and the switch still
    refused. The put-back holds `'<path>'`, the token the rule names, since fold 17. The rule is read from "in the
    bash commands below", the word fold 16 answered A-f15 N3 with (under sh with HOME unset, `~` stays a word): the
    sentence without `bash`, in the arm, its pin and §10 together, passed every row (read B-f16, N3:
    B17-rule-not-bash+pin)."""
    wt = str(tmp_path / "w")
    _tracked_each_changed(wt, [name, "a", "x.txt"])
    rule = re.search(r"in the bash commands below, each `'<path>'` is its name as git lists it, a `'` in it written "
                     r"`([^`]+)`", _head_arm_text(wt))
    assert rule, _head_arm_text(wt)
    assert "'<path>'" in _put_back(wt)
    p = _bash(_put_back(wt).replace("<path>", name.replace("'", rule.group(1))))
    assert p.returncode == 0, p
    assert _text_of(wt, name) == "HEAD's\n"
    assert _text_of(wt, "a") == "my edit of a\n"
    assert _text_of(wt, "x.txt") == "my edit of x.txt\n"


def test_the_quote_rule_comes_before_every_command_it_governs():
    """The HEAD arm, rendered: its rule ("in the bash commands below, each `'<path>'` is …") stands before the first
    code span that holds a `'<path>'`. Guards the rule's place, which no row read: moved to just before the switch,
    in the arm, its pin and §10 together, its "below" named no command that takes a name and every row passed, each
    reading the rule wherever it stands (read B-f17, N7: B18-rule-before-the-switch+pin)."""
    text = _head_arm_text("/w t")
    rule = text.index("in the bash commands below, each `'<path>'`")
    first = min(text.index("`" + c + "`") for c in re.findall(r"`([^`]+)`", text) if "'<path>'" in c and " " in c)
    assert rule < first, (rule, first)


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_put_back_puts_back_a_name_and_not_its_case_twin(tmp_path):
    """Tracked `a.txt` and `A.txt`, each with a local change: the put-back, given `a.txt`, exits 0, puts HEAD's `a.txt`
    back and leaves `A.txt`'s change. Guards a put-back that matches without case (`':(literal,icase)<path>'`, or
    `--icase-pathspecs`, which every row before fold 17 let survive): it puts back both, the twin's change gone at exit
    0 (read A-f16's kill row: A17-putback-literal-icase+pin; and O17-icase-for-literal+pin; read B-f17, N2). Skips where
    the file system folds case, since there the two names are one file."""
    _no_case_fold(tmp_path)
    wt = str(tmp_path / "w")
    _tracked_each_changed(wt, ["a.txt", "A.txt"])
    p = _bash(_put_back(wt).replace("<path>", "a.txt"))
    assert p.returncode == 0, p
    assert _text_of(wt, "a.txt") == "HEAD's\n"
    assert _text_of(wt, "A.txt") == "my edit of A.txt\n"


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_put_back_under_icase_pathspecs_puts_back_no_other_file(tmp_path):
    """Tracked `a.txt` and `A.txt`, each with a local change, GIT_ICASE_PATHSPECS=1 in the operator's environment: the
    put-back, given `a.txt`, leaves `A.txt`'s change, and either puts HEAD's `a.txt` back at exit 0 or fails loud with
    `a.txt` untouched. Guards the operator's own environment reaching the put-back (read A-f16, N1): fold 16's element
    `:(literal)` kept a global icase, so git put back `A.txt` (and on git 2.39 and 2.47 not `a.txt`) at exit 0, the
    twin's change gone without a word and the switch still refused; `--literal-pathspecs` refuses the pair at once
    ("incompatible with all other global pathspec settings"), with nothing changed. It first checks that git reads the
    variable (`ls-files -- a.txt` lists `A.txt`): with its name mistyped the row passed whatever the put-back did (read
    B-f17, N5: B18T-icase-row-env-typo). Skips where the file system folds case."""
    _no_case_fold(tmp_path)
    wt = str(tmp_path / "w")
    _tracked_each_changed(wt, ["a.txt", "A.txt"])
    env = dict(os.environ, GIT_ICASE_PATHSPECS="1")
    assert b"A.txt" in _bash("git -C %s ls-files -- a.txt" % shlex.quote(wt), env=env).stdout  # in effect
    p = _bash(_put_back(wt).replace("<path>", "a.txt"), env=env)
    assert _text_of(wt, "A.txt") == "my edit of A.txt\n", p
    assert _text_of(wt, "a.txt") == ("HEAD's\n" if p.returncode == 0 else "my edit of a.txt\n"), p


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_put_back_under_literal_pathspecs_puts_the_file_back(tmp_path):
    """Tracked `x.txt` and `y.txt`, each with a local change, GIT_LITERAL_PATHSPECS=1 in the operator's environment:
    the put-back, given `x.txt`, exits 0, puts HEAD's back and leaves `y.txt`'s change. Guards the put-back's literal
    reading written as name magic (read A-f16, N1): under that variable git read fold 16's `':(literal)x.txt'` as a
    file of that name ("did not match any file(s) known to git"), and the way back stopped there. It first checks that
    git reads the variable (`ls-files -- ':!x.txt'` lists nothing, the name read as a file's): with its name mistyped
    the row passed whatever the put-back did (read B-f17, N5: B18T-literal-row-env-typo)."""
    wt = str(tmp_path / "w")
    _tracked_each_changed(wt, ["x.txt", "y.txt"])
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    assert _bash("git -C %s ls-files -- ':!x.txt'" % shlex.quote(wt), env=env).stdout == b""  # in effect
    p = _bash(_put_back(wt).replace("<path>", "x.txt"), env=env)
    assert p.returncode == 0, p
    assert _text_of(wt, "x.txt") == "HEAD's\n"
    assert _text_of(wt, "y.txt") == "my edit of y.txt\n"


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_put_back_takes_heads_file_where_the_index_holds_another(tmp_path):
    """A tracked `x.txt` whose index entry holds a staged edit and whose worktree copy another: the HEAD arm's
    put-back, given `x.txt`, leaves both HEAD's (`git status --porcelain` empty). Guards a put-back that reads the
    index (`checkout -- '<path>'`): it writes the staged edit back, not HEAD's, the status still shows the file
    changed, and the printed switch is still refused for it (read A-f15's kill row: A16-putback-index+pin2)."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    _wt_commit(wt, "x.txt", "HEAD's\n", "x")
    with open(os.path.join(wt, "x.txt"), "w") as fh:
        fh.write("staged\n")
    _wt_git(wt, "add", "--", "x.txt")
    with open(os.path.join(wt, "x.txt"), "w") as fh:
        fh.write("the cut copy\n")
    p = _bash(_put_back(wt).replace("<path>", "x.txt"))
    assert p.returncode == 0, p
    with open(os.path.join(wt, "x.txt")) as fh:
        assert fh.read() == "HEAD's\n"
    assert _wt_git(wt, "status", "--porcelain").stdout == b""


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_move_takes_a_name_with_a_quote_written_as_the_arm_says(tmp_path):
    """Untracked `modules/x' 'y/m.yml` in the switch's way beside a tracked `modules/x/m.yml`: the HEAD arm's move,
    each `'` in the name written as the arm says, run from `/` with HOME the test's, takes that file into
    `~/kontroll-moved/` and nothing tracked leaves the worktree. Guards a `'` in the name with no rule for it (read
    A-f15, N2): filled in as git lists it, the quotes close and reopen, mv reads `<wt>/modules/x` and `y/m.yml`, moves
    the tracked `modules/x/` into the home and fails on the rest."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    os.makedirs(os.path.join(wt, "modules", "x"))
    _wt_commit(wt, "modules/x/m.yml", "tracked\n", "x")
    name = "modules/x' 'y/m.yml"
    os.makedirs(os.path.join(wt, "modules", "x' 'y"))
    with open(os.path.join(wt, name), "w") as fh:
        fh.write("the cut copy\n")
    home = tmp_path / "home"
    home.mkdir()
    rule = re.search(r"a `'` in it written `([^`]+)`", _head_arm_text(wt))
    assert rule, _head_arm_text(wt)
    p = _bash(_head_arm_span(wt, "mkdir ").replace("<path>", name.replace("'", rule.group(1))),
              env=dict(os.environ, HOME=str(home)), cwd="/")
    assert p.returncode == 0, p
    assert (home / "kontroll-moved" / "m.yml").read_text() == "the cut copy\n"
    assert _wt_git(wt, "status", "--porcelain", "--untracked-files=all").stdout == b""


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_compare_and_move_take_a_name_with_a_dollar_as_it_is(tmp_path):
    """A file `a$HOME.txt` in the switch's way (git lists it as it is), the worktree's copy not the branch's: the HEAD
    arm's compare, given the branch and that name, exits 1 (cmp read both and found them different), and its move, run
    from `/` with HOME the test's, takes that file into `~/kontroll-moved/` under its own name, the worktree then
    clean. Guards the file compare's and the move's `'<path>'` written `"<path>"` (the link compare and the put-back
    have rows of their own), which holds a space as well and passed every row (each filled in a name with a space
    only): the shell expands `$HOME` inside the name, the compare reads no file (exit 2: "differs"
    for the wrong reason) and the move fails, the file left in the switch's way (read A-f15's kill row:
    A16-path-dquote+pin; read B-f15, N4: B16-path-dq+pin)."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    name = "a$HOME.txt"
    _wt_git(wt, "switch", "-q", "-c", "op")
    _wt_commit(wt, name, "the branch's\n", "the branch's file")
    _wt_git(wt, "switch", "-q", "--detach", "main")
    with open(os.path.join(wt, name), "w") as fh:
        fh.write("the cut copy\n")
    home = tmp_path / "home"
    home.mkdir()
    q = shlex.quote(wt)
    cmp = _head_arm_span(wt, "git -C %s cat-file --filters" % q).replace("<branch>", "op").replace("<path>", name)
    p = _bash(cmp, cwd="/")
    assert p.returncode == 1 and b"differ" in p.stdout, p
    p = _bash(_head_arm_span(wt, "mkdir ").replace("<path>", name), env=dict(os.environ, HOME=str(home)), cwd="/")
    assert p.returncode == 0, p
    assert (home / "kontroll-moved" / name).read_text() == "the cut copy\n"
    assert _wt_git(wt, "status", "--porcelain", "--untracked-files=all").stdout == b""


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_link_compare_takes_a_name_with_a_dollar_as_it_is(tmp_path):
    """A link `l$HOME` on a branch `op` (to `t1`), another link of that name in the worktree on a detached HEAD (to
    `t2`): the HEAD arm's link compare, given `op` and that name, run from `/`, exits 1 (they differ), and 0 once the
    worktree's link is the branch's. Guards the link compare's `'<path>'` written `"<path>"` (read B-f16's kill row:
    B17-link-dq+pin): bash expands `$HOME` and neither side names anything, so fold 17's compare read two empty streams
    and said "the same" for links that differ, and with the `test -L` it says "differs" for links that are the same
    (read A-f17, N1), which the second half holds; the `$` row above runs the file compare and the move only."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    name = "l$HOME"
    _wt_git(wt, "switch", "-q", "-c", "op")
    os.symlink("t1", os.path.join(wt, name))
    _wt_git(wt, "--literal-pathspecs", "add", "--", name)
    _wt_git(wt, "commit", "-q", "-m", "a link")
    _wt_git(wt, "switch", "-q", "--detach", "main")
    os.symlink("t2", os.path.join(wt, name))
    cmp = _link_compare(wt).replace("<branch>", "op").replace("<path>", name)
    p = _bash(cmp, cwd="/")
    assert p.returncode == 1, p
    os.remove(os.path.join(wt, name))
    os.symlink("t1", os.path.join(wt, name))
    p = _bash(cmp, cwd="/")
    assert p.returncode == 0, p


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("fill", ["nothere", '"l"'])
def test_the_printed_link_compare_of_a_name_that_names_nothing_does_not_say_the_same(tmp_path, fill):
    """Links `l` that differ (the branch's to `t1`, the worktree's to `t2`): the HEAD arm's link compare, given `op`
    and a name that names nothing (a typo, or `"l"` filled in with its double quotes, as git prints a name it quotes
    and the HEAD arm has no rule for), run from `/`, exits non-zero. Guards the compare's two empty streams (read
    B-f16, N2; read A-f17, N1: O18-no-test-L+pin): git names nothing and prints nothing, `readlink` fails inside
    `<(…)`, whose exit bash drops, and `cmp` reads two empty streams and exits 0, "the same", so the arm said to
    remove the file, and an operator who then removed it by its right name (a tab completion) lost a link that
    differs. Git says the name is not on the branch, and the `test -L` the compare ends with fails it."""
    wt = str(tmp_path / "w")
    _branch_link(wt, "l", "t1")
    os.symlink("t2", os.path.join(wt, "l"))
    p = _bash(_link_compare(wt).replace("<branch>", "op").replace("<path>", fill), cwd="/")
    assert p.returncode != 0, p


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("there", ["nothing", "empty file", "file", "dir"])
def test_the_printed_link_compare_of_an_empty_file_and_no_link_does_not_say_the_same(tmp_path, there):
    """An empty file `e` on a branch `op`, and in the worktree nothing named `e`, an empty file `e`, a file `e`
    holding `x`, or a directory `e`: the HEAD arm's link compare, given `op` and `e`, run from `/`, exits non-zero and
    prints something. Guards a check of git's side alone (`set -o pipefail`, or a `git cat-file -e` first; read A-f17,
    N1: A18F-pipefail, A18F-cat-file-e): git reads the empty blob and exits 0, `readlink` fails, and `cmp` again reads
    two empty streams and exits 0, "the same"; only a check of the worktree's side (the `test -L`) fails it. And
    guards the compare read by its output (read A-f19, N1): with `readlink -n`, which says nothing of what is not a
    link, the `test -L` failed without a word, and nothing printed reads as "the same"; `-v` prints readlink's
    complaint."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    _wt_git(wt, "switch", "-q", "-c", "op")
    _wt_commit(wt, "e", "", "an empty file")
    _wt_git(wt, "switch", "-q", "--detach", "main")
    path = os.path.join(wt, "e")
    if there == "empty file":
        open(path, "wb").close()
    elif there == "file":
        with open(path, "wb") as fh:
            fh.write(b"x")
    elif there == "dir":
        os.mkdir(path)
    p = _bash(_link_compare(wt).replace("<branch>", "op").replace("<path>", "e"), cwd="/")
    assert p.returncode != 0 and (p.stdout + p.stderr).strip(), p


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("what, fill", [("file", "l"), ("dir", "l"), ("link", "l"), ("prefix", "l"),
                                        ("link", "nothere"), ("link", '"l"')])
def test_the_printed_link_compare_prints_something_whenever_it_does_not_say_the_same(tmp_path, what, fill):
    """A link `l` on a branch `op` (to `t1`), and in the worktree on a detached `main` a file `l`, a directory `l`, or
    a link `l` to `t2` or to `t` (a prefix of the branch's), given by its name or as a name that names nothing (a
    typo, or `"l"` with git's double quotes): the HEAD arm's link compare, run from `/`, exits non-zero and prints
    something. Guards the compare read by its output, as cmp is read, nothing printed meaning the same (read A-f18,
    N1): with the `test -L` first, each of these failed without a word, as "the same" reads, and the arm's next words
    are "where it is the same, remove it (untracked) or put HEAD's back", so an operator who then removed it by its
    right name, or put HEAD's back over a file edited after the quit, lost what differed; with the `test -L` last,
    git's or cmp's complaint is printed first. Two links that differ are the cases only cmp's words answer (readlink,
    `-v`, speaks wherever there is no link): for `t2` cmp prints `differ` on stdout, for `t` `EOF` on stderr. `cmp -s`
    passed the other four cases, and with cmp's stderr thrown away the `t2` case passed too (T25 fold 20's runs:
    O19-cmp-s+pin, A20-cmp-stderr-dropped+pin, B20-cmp-stderr-null+pin)."""
    wt = str(tmp_path / "w")
    _branch_link(wt, "l", "t1")
    p = os.path.join(wt, "l")
    if what == "file":
        with open(p, "w") as fh:
            fh.write("my edit\n")
    elif what == "dir":
        os.mkdir(p)
        with open(os.path.join(p, "m"), "w") as fh:
            fh.write("mine\n")
    elif what == "prefix":
        os.symlink("t", p)
    else:
        os.symlink("t2", p)
    r = _bash(_link_compare(wt).replace("<branch>", "op").replace("<path>", fill), cwd="/")
    assert r.returncode != 0 and (r.stdout + r.stderr).strip(), r


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_link_compare_reads_a_worktree_whose_path_needs_quoting(tmp_path):
    """A link `l` on a branch `op` (to `t1`), the worktree at a path holding a space, a `'` and a `$`, on a detached
    `main`, holding a link `l`: the HEAD arm's link compare, run from `/`, exits 0 while the worktree's link is the
    branch's and 1 once it is not. Guards each `{{ worktree | quote }}` of the compare, the `test -L`'s among them, by
    reading the exits themselves, 0 where the links are the same and 1 where they differ: a `test -L` written `{{
    worktree }}` answers "differs" (`test`: binary operator expected) for every link in a worktree whose path holds a
    space (read A-f18's table: A19-test-L-noquote-wt+pin, which
    test_the_head_refusals_compares_tell_the_branchs_own_file_and_link_from_another also kills, rendering the arm for
    `it's w`), and one written `"{{ worktree }}"` reads a path holding a `$` as another path, so every same link read
    "differs" and was moved, not removed (read A-f19, N2: A20-test-L-dq-wt+pin)."""
    wt = str(tmp_path / "it's $w")
    _branch_link(wt, "l", "t1")
    os.symlink("t1", os.path.join(wt, "l"))
    cmp = _link_compare(wt).replace("<branch>", "op").replace("<path>", "l")
    r = _bash(cmp, cwd="/")
    assert r.returncode == 0, r
    os.remove(os.path.join(wt, "l"))
    os.symlink("t2", os.path.join(wt, "l"))
    r = _bash(cmp, cwd="/")
    assert r.returncode == 1, r


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.parametrize("target", ["t1", "t2"])
def test_the_printed_link_compare_leaves_the_operators_shell_as_it_was(tmp_path, target):
    """The HEAD arm's link compare pasted into a bash between two snapshots of its options (`set +o`, `shopt -p`)
    and its directory (`pwd`), where the links are the same and where they differ: the two snapshots are the same.
    Guards a check that changes the operator's own shell: one that turned pipefail on (read B-f16 N2's `set -o
    pipefail;`, ruled on in read A-f17, N1: A18F-pipefail) left every later pipeline the operator types exiting as its
    first failing command does, one that starts `set -e;` (read B-f18, N3: B19-set-e-first+pin, which a row reading
    pipefail alone passed) closes the operator's shell at the next command that fails, and one that starts `cd
    <worktree> &&` (read B-f19: B20-cd-first+pin, which a row reading the options alone passed) leaves the operator
    in the worktree, where the next relative path they type names another file."""
    wt = str(tmp_path / "w")
    _branch_link(wt, "l", "t1")
    os.symlink(target, os.path.join(wt, "l"))
    cmp = _link_compare(wt).replace("<branch>", "op").replace("<path>", "l")
    snap = "{ set +o; shopt -p; pwd; } > %s/%%s" % shlex.quote(str(tmp_path))
    _bash("\n".join([snap % "before", cmp, snap % "after"]), cwd="/")
    before = (tmp_path / "before").read_text()
    assert "pipefail" in before and "errexit" in before and before.splitlines()[-1] == "/", before
    assert (tmp_path / "after").exists() and (tmp_path / "after").read_text() == before


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_put_back_of_a_glob_name_not_in_head_puts_no_other_file_back(tmp_path):
    """A tracked `ab.txt` with a local change, and `a*.txt` staged new (in the index, not in HEAD): the HEAD arm's
    put-back, given `a*.txt`, leaves `ab.txt`'s change (it may fail: HEAD has no `a*.txt`). Guards the literal reading
    where git has no literal match to take first: the put-back row's glob names are all in HEAD, so a put-back read as
    a glob passed it but for `a\\*.txt`; with the name not in HEAD, `:(glob)` and `:(top)` read it as a pattern and
    put `ab.txt` back, the change gone at exit 0 (read B-f16's kill row: B17-putback-glob+pin, B17-putback-top+pin)."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    _wt_commit(wt, "ab.txt", "HEAD's\n", "ab")
    with open(os.path.join(wt, "ab.txt"), "w") as fh:
        fh.write("my edit after the quit\n")
    with open(os.path.join(wt, "a*.txt"), "w") as fh:
        fh.write("the cut copy\n")
    _wt_git(wt, "--literal-pathspecs", "add", "--", "a*.txt")
    _bash(_put_back(wt).replace("<path>", "a*.txt"))
    assert _text_of(wt, "ab.txt") == "my edit after the quit\n"


@pytest.mark.skipif(os.name != "posix", reason="the arm's commands are bash's")
@pytest.mark.usefixtures("isolated_git")
def test_the_printed_move_leaves_a_file_named_kontroll_moved_alone(tmp_path):
    """The HEAD arm's move, run in bash from `/` with HOME the test's, where `~/kontroll-moved` is a regular file of
    the operator's: it fails (non-zero), the file in the way is still in the worktree, and the home holds that one
    file, as it was. Guards the move's `&&` and its destination's `/` together: with `;` and no `/`, the failed `mkdir
    -p` does not stop the `mv`, which renames the cut copy ONTO `~/kontroll-moved` (the operator's file kept only as
    `kontroll-moved.~1~`) and exits 0, the arm's "a directory of its own" a file, and the operator's file under a name
    they did not give it (read A-f15's kill row: A16-and-semi-noslash+pin)."""
    wt = str(tmp_path / "w")
    _seeded(wt)
    os.makedirs(os.path.join(wt, "a"))
    with open(os.path.join(wt, "a", "m.yml"), "w") as fh:
        fh.write("the cut copy\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "kontroll-moved").write_text("my notes\n")
    p = _bash(_head_arm_span(wt, "mkdir ").replace("<path>", "a/m.yml"), env=dict(os.environ, HOME=str(home)),
              cwd="/")
    assert p.returncode != 0, p
    assert os.path.exists(os.path.join(wt, "a", "m.yml"))
    assert sorted(f.name for f in home.iterdir()) == ["kontroll-moved"]
    assert (home / "kontroll-moved").read_text() == "my notes\n"


def test_troubleshootings_busy_row_quotes_the_head_refusals_commands():
    """The busy refusal's HEAD arm, rendered for a worktree `WTREE` (so `quote` leaves it bare, then written
    `<worktree>`): each command it prints that runs git in the worktree (the `symbolic-ref`, the `for-each-ref`, the two
    `branch`, the reflog, the two compares, the put-back and the switch: each span that holds ` -C <worktree> `, so the
    link compare's `test -L` and an option of git's before `-C` move no locator, read B-f17, N6 c) is in troubleshooting
    §10's busy row in a code span, its pipe escaped for the table, and so are its clause on a ref that is not a branch,
    from the `symbolic-ref` command to "lists your branches)", and its remedy, from "switch back to your branch" to the
    switch, each whole; the row says a HEAD that names a ref outside `refs/heads/` is refused, and ends the remedy with
    the next run. Guards §10's HEAD remedy, which no row read: its compare could go, or its `branch --points-at` turn
    back into fold 10's
    reflog, and pass every row (read B-f11, N1:
    B12-tr379-head-compare-dropped, B12-tr379-branch-to-reflog); and the clauses a command alone does not hold —
    the tracked-file case, "where it differs", "in bash", a command out of its code span, `git branch` dropped (a
    prefix of the `--points-at` command), the outside-refs case — went and passed (read B-f12, N3: B13-tr379-*); and
    the clause on a ref that is not a branch sat before the remedy's slice, so a command of it moved out of its code
    span (GFM then reads `<worktree>` as HTML), "`git branch` then fails" dropped from §10 or from the arm and its pin,
    and the next-run sentence dropped each passed (read B-f13, N2: B14-tr379-symref-out-of-code,
    B14-tr379-symref-fails-dropped, B14-arm+pin-symref-fails-dropped, B14-tr379-next-run-dropped)."""
    msg = _named("Refuse to commit or land while git has an operation in progress")["ansible.builtin.fail"]["msg"]
    got = _rendered(msg, worktree="WTREE", _wt_busy={"rc": 0, "stdout_lines": ["WTREE/.git/HEAD"],
                                                     "stderr_lines": []})
    commands = [c.replace("WTREE", "<worktree>").replace("|", "\\|") for c in re.findall(r"`([^`]+)`", got)
                if " -C WTREE " in c]
    assert len(commands) == 9, commands
    with open(os.path.join(ROOT, "docs", "troubleshooting.md"), encoding="utf-8") as fh:
        rows = [line for line in fh
                if line.startswith("| `local-canonical: REFUSING to commit or land — git has an operation in progress")]
    assert len(rows) == 1, len(rows)
    for c in commands:
        assert "`" + c + "`" in rows[0], c
    start = got.index("`git -C WTREE symbolic-ref HEAD`")
    end = got.index("lists your branches)", start) + len("lists your branches)")
    clause = got[start:end].replace("WTREE", "<worktree>")
    assert clause in rows[0], clause
    start = got.index("witch back to your branch — ")
    end = got.index(" switch --no-guess <branch>`)", start) + len(" switch --no-guess <branch>`)")
    remedy = got[start:end].replace("WTREE", "<worktree>").replace("|", "\\|")
    assert remedy in rows[0], remedy
    assert "a HEAD that names a ref outside `refs/heads/`" in rows[0], rows[0][-400:]
    assert "switch --no-guess <branch>`). The next run brings your commit forward again." in rows[0], rows[0][-400:]
