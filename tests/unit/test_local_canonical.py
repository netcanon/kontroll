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

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "local-canonical.yml")


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
    `--check` too (`check_mode: false`) — when there is a canonical to look at (`_canonical_before`), so the dry-run
    of a fresh node is not failed by a path that is not there yet —, is never "changed", and fails on any status but 0 (nothing to do) and 3
    (something to do); the apply task carries no `--check`, runs only when the detect task exited 3, and is
    "changed" then — so a second run reports 0 changed. Both say `--owner {{ operator_uid }} --group 1001`: the
    owner half is what lets the operator's first-run push succeed before the just-added group membership becomes
    effective (next login). No task of the play runs `find`, `chown` or `chmod` over the canonical any more, and
    none recurses the `file` module over it. Guards a regression to group-only (re-breaks F-CANON), to a path-based
    grant, to a detect task the dry-run skips, or to an apply that runs — and reports a change — on every run.

    The pair runs TWICE: once before the landing (the first run's F-CANON grant), and once after it, on what the
    landing left — the service's files when root landed (rule 5), git's `0444` pack files either way — so that the
    deploy after a landing reports 0 changed instead of re-owning them then (`tests/integration/
    test_local_canonical_as_root.py` runs the play and holds the count). Both pairs are the same two commands
    with the same conditions; the second follows the landing task, and the detect of the second registers its own
    name (`_normalise_after`) so its apply is gated on it and not on the first detect's answer."""
    detect, apply, detect_after, apply_after = _pass_tasks()
    tasks = _tasks()
    land = next(i for i, t in enumerate(tasks) if "kontroll-land.py" in _cmd(t) and "--check" not in _cmd(t))
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
        assert str(d.get("when")).replace(" ", "") == "_canonical_before.stat.existsornotansible_check_mode", (
            "the detect must be skipped under the dry-run of a node with no canonical yet (its path is not there), "
            "and run everywhere else")
        assert a.get("changed_when") is True and "check_mode" not in a
    for task in _tasks():
        words = _cmd(task).split()
        assert not {"find", "chown", "chmod"} & set(words), "a path-based grant is back: %s" % task.get("name")
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
    land = next(i for i, t in enumerate(tasks) if "kontroll-land.py" in _cmd(t) and "--check" not in _cmd(t))
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
    refuses the landing, with the remedy `fetch local && rebase local/main` and never `--force` — now lives in
    the command and is held by `tests/unit/test_land.py`; here the play must not carry a `--force` or a `-f`
    anywhere near the landing. Guards the reversion of rule 8 at this door (a push coming back, the dry-run
    skipping the judgement, a landing task that runs under check mode), which the hook would then refuse at
    every deploy.

    The `--check` task's CONDITION is pinned to the ownership pass's — `_canonical_before.stat.exists or not
    ansible_check_mode` — and the refusal's to `(<register>.rc | default(0)) != 0`. WHY: the judgement needs a
    canonical to judge against (`--repo {{ bare }}`), and under the dry-run of a FRESH node the `file` and `git
    init` tasks are only simulated, so the command exits 1 ("--repo must name a bare repository") and the refusal
    stops the mandated `--check --diff` of every first install (measured 2026-10-20 in the gate image: `failed=1`
    at the refusal task). The exact condition is held, not merely "a when": a `when: not ansible_check_mode` on
    the same task would make EVERY dry-run skip the judgement with the rest of this row green (the 20-review-land
    reversion R06 survived before this pin). And `default(0)`: a skipped judgement registers no `rc`, and the
    refusal must read that as "nothing to refuse", not fail on an undefined attribute. The debug task before the
    judgement says, under that one dry-run, why nothing was judged."""
    tasks = _tasks()
    landing = [t for t in tasks if "kontroll-land.py" in _cmd_text(t)]
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
    assert str(check.get("when")).replace(" ", "") == "_canonical_before.stat.existsornotansible_check_mode", (
        "the judgement runs on every real run and on the dry-run of a node that HAS a canonical; only the dry-run "
        "of a fresh node skips it (there is nothing to judge against yet): %r" % check.get("when"))
    detect = next(t for t in tasks if "--normalise" in _cmd_text(t) and "--check" in _cmd_text(t))
    assert str(check.get("when")).replace(" ", "") == str(detect.get("when")).replace(" ", ""), \
        "the judgement's condition is the ownership pass's detect condition, in the same words"
    note = tasks[tasks.index(check) - 1]
    assert _mod(note, "ansible.builtin.debug", "debug") and "ansible_check_mode" in str(note.get("when")) \
        and "not _canonical_before.stat.exists" in str(note.get("when")), \
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
    assert "\n" not in message, "the message is ONE folded paragraph"
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


def test_the_play_refuses_an_operator_whose_uid_is_the_services():
    """The play's FIRST task asserts `operator_uid` is not `1001`, with a sentence that says the canonical cannot
    tell such an operator from the network service (the hook and the seam are keyed on that one number). Guards
    the gap the hook's reader named (Q-6): an operator whose login uid is 1001 would have the landing run "as the
    service", the hook refuse its proposals' moves, and the chown hand the service the operator's grant — each
    failing later with a message about something else."""
    tasks = _tasks()
    first = tasks[0]
    m = _mod(first, "ansible.builtin.assert", "assert")
    assert m, "the first task must be the uid assertion: %r" % first.get("name")
    assert "1001" in str(m.get("that")) and "operator_uid" in str(m.get("that"))
    assert "uid 1001" in str(m.get("fail_msg")) and "cannot tell" in str(m.get("fail_msg"))
    assert "Nothing was changed" in str(m.get("fail_msg"))


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

