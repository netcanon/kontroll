"""local-canonical.yml: the C10 canonical's gid-1001 write-grant must provision the group + the operator's
membership + operator OWNERSHIP — not just the group.

WHY (the failure this guards — hit LIVE during the #119 prod cutover, "F-CANON"): the canonical
`/srv/kontroll.git` is group-1001 (the runtime uid:gid the privileged API/runner writes as), but a FRESH
control node has no NAMED group at gid 1001 and the operator is not a member of it. The Mirror push at the end
of local-canonical.yml runs AS THE OPERATOR (no become); the moment a canonical object is group-1001-owned but
not operator-owned, that push dies with "Permission denied: unable to write objects" and the whole deploy fails.
The #119 box needed a manual `groupadd -g 1001 kontroll; usermod -aG 1001 admin; chown -R admin:1001 <canonical>`.

The fix lands those three as idempotent tasks: (1) ensure the gid-1001 group, (2) add the operator to it, and
(3) the grant on existing objects sets owner=operator too (not just group) — as detect-then-fix `find` commands that
PRUNE `hooks/`, which stays root:root 0755 so the uid-1001 writer can never plant a hook (finding 2). The owner half is load-bearing: a freshly
added supplementary group is NOT effective in the operator's current login session, so on the very first run the
push can't rely on membership yet — operator OWNERSHIP makes it succeed via OWNER perms regardless. This test
pins all three, plus the ordering (group/membership before the operator's push), so a refactor that drops any
half silently re-breaks a fresh install's first deploy.
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
    """A `user` task appends the operator to the gid-1001 group (append=true, become) so its `git push local`
    can write the group-1001 canonical. Guards the second half of the F-CANON manual fix (`usermod -aG`)."""
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


def test_the_ownership_grant_sets_operator_owner_and_prunes_hooks():
    """The write-grant on EXISTING objects (become) must set BOTH owner=operator AND group=1001 — the owner half is
    what lets the operator's first-run push succeed before the just-added group membership becomes effective (next
    login) — and it must PRUNE hooks/ so the hook files never become operator- or group-owned. It is a detect-then-fix
    pair of `find` commands now (the `file` module cannot exclude a subtree), so a second run reports 0 changed.
    Guards a regression to group-only (re-breaks F-CANON) or to a blanket recurse that re-grants hooks/."""
    chowns = [t for t in _tasks() if "chown" in _cmd(t)]
    assert len(chowns) == 1, "exactly one chown grant task: %r" % [t.get("name") for t in chowns]
    cmd = _cmd(chowns[0])
    assert "{{ operator_uid }}:1001" in cmd, "owner=operator AND group=1001 (the F-CANON owner half)"
    assert "-path {{ bare }}/hooks -prune" in cmd, "hooks/ must be pruned from the ownership grant"
    assert chowns[0].get("become") is True and chowns[0].get("when"), "become, and gated on the detector"
    blanket = [t for t in _tasks() if (m := _mod(t, "ansible.builtin.file", "file")) and m.get("recurse")
               and str(m.get("path")) == "{{ bare }}"]
    assert blanket == [], "no blanket recurse over the whole canonical — it would re-grant hooks/ every run"


def test_hooks_stay_root_owned_and_outside_every_grant():
    """hooks/ is root:root 0755 (recurse) and PRUNED from every chmod/chown grant, and the re-own task runs AFTER the
    grants. The canonical is mounted :rw into the uid-1001 service (api_privileged); with a group-writable hooks/
    that writer could plant a `reference-transaction` or `update` hook that the next `sudo kontroll-promote` runs as
    ROOT (2026-10-08 review, finding 2). Every git actor only needs to READ hooks. Guards the grant growing back over
    hooks/, the re-own going missing, or it running before a grant that would undo it."""
    tasks = _tasks()
    grants = [i for i, t in enumerate(tasks)
              if ("chmod" in _cmd(t) or "chown" in _cmd(t)) and "{{ bare }}" in _cmd(t)]
    assert grants, "the grant tasks must exist"
    for i in grants:
        assert "-path {{ bare }}/hooks -prune" in _cmd(tasks[i]), "a grant reaches into hooks/: %s" % tasks[i].get("name")
    hooks = [i for i, t in enumerate(tasks)
             if (m := _mod(t, "ansible.builtin.file", "file")) and str(m.get("path")) == "{{ bare }}/hooks"]
    assert len(hooks) == 1, "exactly one task owns hooks/"
    m = _mod(tasks[hooks[0]], "ansible.builtin.file", "file")
    assert m.get("owner") == "root" and m.get("group") == "root", "hooks/ is root:root"
    assert str(m.get("mode")) == "0755" and m.get("recurse") is True, "0755 recursively — readable+executable, never writable"
    assert tasks[hooks[0]].get("become") is True
    assert hooks[0] > max(grants), "the hooks re-own must be the last word, after every grant"
    install = [t for t in tasks if (m := _mod(t, "ansible.builtin.copy", "copy"))
               and str(m.get("dest", "")).endswith("/hooks/update")]
    assert install and str(_mod(install[0], "ansible.builtin.copy", "copy").get("mode")) == "0755", \
        "the update hook is installed 0755 (readable + executable by every git actor, writable by none but root)"


def test_group_and_membership_precede_the_operator_push():
    """The group + membership tasks must come BEFORE the Mirror push (`git push local …`, run as the operator) —
    provisioning the write-grant after the push that needs it would be useless. Pins the ordering."""
    tasks = _tasks()
    def _idx(pred):
        return next((i for i, t in enumerate(tasks) if pred(t)), None)
    def _is_push(t):                                      # the Mirror push COMMAND only — not a task whose NAME
        m = _mod(t, "ansible.builtin.command", "command")  # merely mentions "git push local" (e.g. the user task)
        return bool(m) and "push local" in str(m.get("cmd", "")).lower()
    grp = _idx(lambda t: _mod(t, "ansible.builtin.group", "group"))
    usr = _idx(lambda t: _mod(t, "ansible.builtin.user", "user"))
    push = _idx(_is_push)
    assert grp is not None and usr is not None and push is not None, "missing group/user/push task"
    assert grp < push and usr < push, "the gid-1001 group + operator membership must be provisioned before the push"


def _cmd_text(task):
    for k in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
        v = task.get(k)
        if isinstance(v, dict):
            return str(v.get("cmd", v.get("_raw_params", "")))
        if isinstance(v, str):
            return v
    return ""


def test_the_canonical_mirror_refuses_to_discard_promoted_work():
    """The mirror is a fast-forward-only push, and worktree/canonical diverge the moment propose-then-promote is
    used — a promote advances canonical `main` with a commit the worktree never had. The push then fails with
    git's generic "Updates were rejected... fetch first", which on a control plane reads like a transient git
    problem rather than "your promoted proposals live only in the canonical" (live-caught 2026-07-28: a deploy
    died here right after a hand-promote, with no indication why).

    Pins the actionable refusal AND, more importantly, that the remedy is never `--force`: force-pushing here
    would silently delete every promoted proposal the canonical holds.

    The check's own two faults, found by running it (the tine's 2026-07-28 follow-up, ported): it must READ the
    canonical with `ls-remote` — a `git fetch` as root inside the installer container leaves a root-owned
    `.git/FETCH_HEAD` in the operator's worktree, breaking the very `git fetch` the remedy prescribes — and the
    remedy must be `rebase local/main`, because this play commits an instance-state commit of its own on every run,
    so the two histories genuinely diverge and `merge --ff-only` cannot work. An ancestry check that cannot PROVE
    the ancestor (rc 128: objects never fetched) refuses like a real divergence (rc 1) — fail closed, both ways."""
    with open(PLAYBOOK, encoding="utf-8") as fh:
        src = fh.read()
    assert "git ls-remote local refs/heads/main" in src, "the canonical is READ with ls-remote (writes nothing)"
    assert all("FETCH_HEAD" not in _cmd_text(t) for t in _tasks()), \
        "no command may use FETCH_HEAD — a root fetch leaves it root-owned in the operator's worktree"
    assert "merge-base --is-ancestor {{ canonical_main }} HEAD" in src, "the divergence check runs on the read sha"
    assert "REFUSING to mirror" in src and "would discard them" in src
    assert "rebase local/main" in src and "merge --ff-only local/main" not in src, \
        "the remedy is a rebase — the histories diverge (instance-state commits), a fast-forward cannot work"
    assert "(_canon_behind.rc | default(0)) != 0" in src, "any non-zero ancestry rc refuses (rc 128 = unproven)"
    fetches = [t for t in _tasks() if "git fetch" in _cmd_text(t)]
    assert fetches == [], "no task may `git fetch` into the operator's worktree: %r" % [t.get("name") for t in fetches]
    pushes = [str(_mod(t, "ansible.builtin.command", "command").get("cmd", "")) for t in _tasks()
              if _mod(t, "ansible.builtin.command", "command") and "git push" in
              str(_mod(t, "ansible.builtin.command", "command").get("cmd", ""))]
    assert pushes, "the mirror push task must exist"
    assert all("--force" not in cmd and " -f " not in cmd for cmd in pushes),         "the mirror must never force-push the canonical: %r" % pushes


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

