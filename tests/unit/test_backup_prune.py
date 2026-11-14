"""backup-configs.yml: the #125 retention-prune play must be GATED, FAIL-CLOSED, and --check-safe — and the
truncation primitive must drop only commits older than the cutoff while preserving the latest snapshot.

WHY (the failures these guard): #123 declared + carried a `retention` window but NOTHING enforced it — a 30d
window was a lie. #125 adds a DESTRUCTIVE git history rewrite of the local-only captures repo. Three regressions
would be catastrophic or silent: (a) the prune runs when it shouldn't — keep-all/undefined MUST be a 0-changed
no-op, else every operator who never set a window silently loses history; (b) the prune runs on the WRONG repo —
without the fail-closed guard (no remote / not bare / not the canonical / real worktree) a refactor could point it
at /srv/kontroll.git and rewrite the source of truth; (c) the prune actuates under --check — the CLAUDE.md
`--check --diff` dry-run must never rewrite history. The structural tests pin (a)/(b)/(c) + the ordering (prune
AFTER the snapshot-commit play, before the SSH-key cleanup). The behavioral tests RUN the exact prune shell
embedded in the playbook against a real tmp repo with back-dated commits and prove it drops only the pre-cutoff
commits, leaves the latest snapshot's TREE byte-identical + the working tree clean, and is idempotent — the tests
that prove the destructive op does the right thing (a wrong-direction truncation would pass every structural test
and still destroy data). Design: docs/reviews/2026-06-23-backup-history-prune/.
"""
import os
import shutil
import subprocess

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")


def _plays():
    return yaml.safe_load(open(PLAYBOOK, encoding="utf-8")) or []


def _is_prune_play(p):
    return isinstance(p, dict) and any("PRUNE_DONE" in _shell(t) for t in (p.get("tasks") or []))


def _prune_play():
    """The retention-prune play — the one whose tasks embed the PRUNE_DONE marker shell. Returns the play or None."""
    return next((p for p in _plays() if _is_prune_play(p)), None)


def _tasks(play):
    return list((play or {}).get("tasks") or [])


def _shell(task):
    v = task.get("ansible.builtin.shell") or task.get("shell")
    return v if isinstance(v, str) else ""


def _cmd(task):
    for k in ("ansible.builtin.command", "command"):
        v = task.get(k)
        if isinstance(v, dict):
            return str(v.get("cmd", "") or "")
        if v is not None:
            return str(v)
    return _shell(task)


# ----------------------------------------------------------------------------------------------------------------
# STRUCTURAL — the play is wired right
# ----------------------------------------------------------------------------------------------------------------

def test_prune_play_exists_and_is_gated_on_retention():
    """A dedicated prune play exists, runs control-node-local, and its actuation is gated by
    kontroll_backup_retention (via the `_retention`/`_retention_days` vars). WHY: #123 carried the knob but
    nothing enforced it; if the play is absent or ungated the window is unenforced (a lie) OR prunes
    unconditionally (history loss for keep-all operators). Pins that the enforcement exists AND is conditioned on
    the carried var — the heart of #125."""
    p = _prune_play()
    assert p is not None, "no retention-prune play (PRUNE_DONE shell) found in backup-configs.yml — #125 not enforced"
    # localhost:all + run_once, every task delegated to localhost: tests/unit/test_backup_controller_plays.py (I-90)
    assert p.get("hosts") == "localhost:all" and p.get("connection") == "local", "the prune is a control-node-local op"
    assert p.get("gather_facts") is False, "match the snapshot play's gather_facts:false idiom"
    vars_blob = yaml.safe_dump(p.get("vars") or {})
    assert "kontroll_backup_retention" in vars_blob, "the prune must derive its window from kontroll_backup_retention"


def test_keep_all_and_undefined_are_a_noop_via_closed_window_set():
    """The window map admits ONLY the finite windows; keep-all + an undefined var resolve to 0 days ⇒ every
    actuation task is gated off (clean skip). The map's keys MUST equal the finite members of service/backup.py
    _RETENTIONS (30d/90d/365d) — no drift — so a GUI-offered window always prunes and a typo can never widen the
    cut. WHY: a gate that defaulted keep-all to a finite window, or omitted a real window, would silently over- or
    under-prune. Pins the no-op default AND the service<->playbook window agreement."""
    p = _prune_play()
    windows = (p.get("vars") or {}).get("_windows") or {}
    import importlib
    backup = importlib.import_module("kontroll.service.backup")
    finite = sorted(r for r in backup._RETENTIONS if r != "keep-all")
    assert sorted(str(k) for k in windows.keys()) == finite, \
        "the playbook _windows map keys must match service/backup.py finite _RETENTIONS exactly (no drift)"
    assert "keep-all" not in windows, "keep-all must NOT be a prunable window (it is the safe no-op default)"
    # every actuation task is gated on a positive day count (keep-all/undefined -> 0 -> skip)
    actuation = [t for t in _tasks(p) if (_shell(t) and "PRUNE_DONE" in _shell(t))
                 or t.get("ansible.builtin.command") or t.get("command")]
    assert actuation, "expected gated resolver/prune tasks"
    for t in actuation:
        assert "_retention_days" in str(t.get("when", "")), \
            "each resolver/prune task must be gated on _retention_days (>0) so keep-all/undefined is a clean no-op"


def test_unconditional_allow_list_assert_rejects_bad_values():
    """An UNCONDITIONAL assert rejects a retention value outside {keep-all,30d,90d,365d} — a hand-run
    `-e kontroll_backup_retention=7d` fails loudly rather than silently falling through to keep-all. WHY: the
    propose path allow-lists, but an operator can bypass it with a raw -e; a typo must surface, not vanish. Pins
    the ungated allow-list assert."""
    p = _prune_play()
    asserts = [t for t in _tasks(p) if isinstance(t.get("ansible.builtin.assert") or t.get("assert"), dict)]
    ungated = [t for t in asserts if not t.get("when")]
    assert ungated, "an UNCONDITIONAL assert (no when:) must reject non-preset retention values loudly"
    blob = yaml.safe_dump(ungated)
    assert "keep-all" in blob and "30d" in blob and "365d" in blob, "the allow-list assert must list the presets"


def test_prune_has_fail_closed_no_remote_and_not_canonical_guard():
    """Before pruning, the play reads `git remote` (asserting it is EMPTY) and refuses the canonical path, via a
    fail-closed `assert`. WHY (the catastrophic mode): a destructive history rewrite aimed at the WRONG repo —
    one with a remote, or the canonical /srv/kontroll.git — corrupts the instance source of truth. The no-remote
    guard is the load-bearing distinguisher (the captures repo never has a remote; the canonical + deploy tree
    DO). Pins that the guard exists, reads `git remote`, and a fail-closed assert gates on remote-emptiness +
    not-canonical; and that the read-only guards are check_mode:false (run under --check to PROVE the decision)."""
    p = _prune_play()
    tasks = _tasks(p)
    assert any("remote" in _cmd(t) for t in tasks), "a guard must read `git remote` to prove the captures repo has none"
    asserts = [t for t in tasks if isinstance(t.get("ansible.builtin.assert") or t.get("assert"), dict)]
    gated = [t for t in asserts if t.get("when")]
    assert gated, "a fail-closed `assert` (gated on the finite window) must guard the prune"
    blob = yaml.safe_dump(gated)
    assert "_prune_remotes" in blob and "kontroll.git" in blob, \
        "the guard assert must check remote-emptiness AND refuse the canonical /srv/kontroll.git path"
    # the read-only guard reads must run under --check (prove guards without actuating)
    for t in tasks:
        c = _cmd(t)
        if ("remote" in c or "rev-parse" in c) and (t.get("ansible.builtin.command") or t.get("command")):
            assert t.get("changed_when") is False, "guard reads are read-only — changed_when:false"
            assert t.get("check_mode") is False, "guard reads must run under --check (check_mode:false)"


def test_prune_writer_is_not_check_mode_false_and_changed_when_is_conditional():
    """The destructive prune shell must NOT carry check_mode:false (a plain shell is check-SKIPPED ⇒ a --check
    dry-run rewrites nothing), must gate on `not ansible_check_mode`, and `changed_when` must be conditional on
    PRUNE_DONE (rolling-cutoff idempotence), never unconditional. WHY (mirrors the deploy-stack resolver-vs-mutator
    boundary): if the prune ran under --check it would rewrite history during the mandatory dry-run — the inverse
    of safe; and an unconditional changed_when would report `changed` every cron tick."""
    p = _prune_play()
    writers = [t for t in _tasks(p) if "PRUNE_DONE" in _shell(t)]
    assert writers, "no prune writer task (PRUNE_DONE shell) found"
    w = writers[0]
    assert w.get("check_mode") is not False, "the destructive prune must NOT be check_mode:false (would rewrite under --check)"
    assert "not ansible_check_mode" in str(w.get("when", "")), "the prune must gate on `not ansible_check_mode`"
    cw = str(w.get("changed_when", ""))
    assert "PRUNE_DONE" in cw, "changed_when must key off PRUNE_DONE (true only when a commit was actually dropped)"
    # belt-and-suspenders: the writer's when: must be a FAITHFUL MIRROR of the assert (SEC-MF3) — the no-remote +
    # not-canonical LITERAL + the kontroll.git REGEX (not just /srv/kontroll.git), so a future edit must defeat
    # both layers to reach a wrong-repo rewrite.
    wb = str(w.get("when", ""))
    assert "_prune_remotes" in wb and "/srv/kontroll.git" in wb, \
        "the prune writer's when: must re-assert the no-remote + not-canonical guards (defense in depth)"
    assert "regex_search" in wb and "kontroll" in wb, \
        "the writer's when: must mirror the assert's kontroll.git regex, not just the /srv literal (SEC-MF3)"


def test_prune_runs_after_the_snapshot_commit_play_and_before_cleanup():
    """The prune play must come AFTER the 'Snapshot the captures into local history' play and BEFORE the
    `_cleanup-ssh-keys.yml` import. WHY: pruning only makes sense post-commit (a prune before the snapshot would
    run against a stale HEAD); and the SSH-key cleanup must stay the LAST import (MF-4). Pins both edges. The
    cleanup is found by either spelling of `import_playbook` and must be there: until I-9 this row read the short
    key only, the playbook writes `ansible.builtin.import_playbook`, and a guard skipped the second edge when the
    import was not found, so that edge was never checked."""
    plays = _plays()
    def _idx(pred):
        return next((i for i, p in enumerate(plays) if isinstance(p, dict) and pred(p)), None)
    snap = _idx(lambda p: "snapshot the captures" in str(p.get("name", "")).lower())
    prune = _idx(_is_prune_play)
    cleanup = _idx(lambda p: "_cleanup-ssh-keys" in str(p.get("ansible.builtin.import_playbook",
                                                              p.get("import_playbook", ""))))
    assert snap is not None and prune is not None, "missing snapshot or prune play"
    assert cleanup is not None, "missing the _cleanup-ssh-keys import"
    assert snap < prune, "the prune must run AFTER the snapshot-commit play (post-commit only)"
    assert prune < cleanup, "the prune must run BEFORE the _cleanup-ssh-keys import (it must stay last, MF-4)"


# ----------------------------------------------------------------------------------------------------------------
# BEHAVIORAL — the embedded prune shell does the right thing (runs the EXACT playbook shell on a real tmp repo)
# ----------------------------------------------------------------------------------------------------------------

def _working_bash():
    """A bash that actually runs (Windows' WSL `bash.exe` shim is on PATH but fails execvpe — probe it). Returns
    the path to a working bash, or None. CI linux has a real bash; this just keeps the local Windows run honest."""
    if not shutil.which("git"):
        return None
    for cand in (shutil.which("bash"), "/bin/bash", "/usr/bin/bash",
                 r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if not cand or not os.path.exists(cand) and cand != shutil.which("bash"):
            continue
        try:
            if subprocess.run([cand, "-c", "echo ok"], capture_output=True, text=True, timeout=15).stdout.strip() == "ok":
                return cand
        except Exception:
            continue
    return None


_BASH = _working_bash()
behavioral = pytest.mark.skipif(_BASH is None, reason="behavioral prune test needs a working bash + git (CI linux has both)")


def _git(cwd, *args, capture=True, env=None):
    r = subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True, text=True, env=env)
    return r.stdout.strip() if capture else r


def _run_prune(repo, window_days):
    """Run the EXACT prune shell embedded in backup-configs.yml against `repo` (so this tests the shipped code,
    not a copy). Substitutes the two Jinja vars the play interpolates."""
    p = _prune_play()
    sh = next(_shell(t) for t in _tasks(p) if "PRUNE_DONE" in _shell(t))
    sh = sh.replace("{{ config_backup_dir }}", str(repo)).replace("{{ _retention_days }}", str(window_days))
    return subprocess.run([_BASH, "-c", sh], capture_output=True, text=True)


@pytest.fixture
def dated_capture_repo(tmp_path):
    """A real local-only captures repo: 5 full-tree snapshots of device.cfg committed at 90/60/40/10/1 days old
    (messages 'backup rev 0'..'backup rev 4'). NO remote (the captures invariant). Returns the repo path."""
    import time
    repo = tmp_path / "captures"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    now = int(time.time())
    for i, age in enumerate([90, 60, 40, 10, 1]):
        (repo / "device.cfg").write_text("rev %d\n" % i, encoding="utf-8")
        _git(repo, "add", "-A")
        ts = "%d +0000" % (now - age * 86400)
        env = {**os.environ, "GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts}
        _git(repo, "commit", "-q", "-m", "backup rev %d" % i, capture=False, env=env)
    assert _git(repo, "remote") == "", "fixture invariant: the captures repo has NO remote"
    return repo


@behavioral
def test_prune_drops_pre_cutoff_keeps_latest_tree_and_clean_worktree(dated_capture_repo):
    """A 30-day prune against commits 90/60/40/10/1 days old drops exactly the three pre-cutoff commits, keeps the
    two within-window commits, leaves the latest snapshot's TREE byte-identical + device.cfg unchanged + the
    working tree clean, and the repo fsck-clean. Asserted by MESSAGE/TREE/count (filter-branch rewrites SHAs, so
    SHA-equality would be wrong). WHY: this is the one test that proves the DESTRUCTIVE op loses only old history,
    never the current capture — a wrong-direction truncation passes every structural test and still destroys data."""
    repo = dated_capture_repo
    tree_before = _git(repo, "rev-parse", "HEAD^{tree}")
    body_before = (repo / "device.cfg").read_text(encoding="utf-8")

    r = _run_prune(repo, 30)
    assert r.returncode == 0, "prune must exit 0\nSTDOUT:%s\nSTDERR:%s" % (r.stdout, r.stderr)
    assert "PRUNE_DONE dropped=3" in r.stdout, "must report dropping the three pre-cutoff commits: %s" % r.stdout

    msgs = _git(repo, "log", "--format=%s").splitlines()
    assert "backup rev 4" in msgs and "backup rev 3" in msgs, "the within-window commits (10d/1d) must survive"
    assert not any(m in msgs for m in ("backup rev 0", "backup rev 1", "backup rev 2")), \
        "the pre-cutoff commits (90/60/40d) must be gone from history"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree_before, "the latest TREE must be byte-identical"
    assert (repo / "device.cfg").read_text(encoding="utf-8") == body_before, "the current capture must be untouched"
    assert _git(repo, "status", "--porcelain") == "", "the working tree must be clean (history-only rewrite)"
    _git(repo, "fsck", "--full", capture=False)        # exits 0 -> repo integrity intact


@behavioral
def test_prune_is_idempotent_on_a_second_run_in_the_same_window(dated_capture_repo):
    """A second 30d prune in the same wall-clock window drops NOTHING (after the first prune no commit is older
    than the cutoff). WHY: the scheduled job runs this every cron tick; a non-idempotent prune would churn objects
    and report spurious `changed` (CLAUDE.md: a second apply must report 0 changed). This pins the drop-set-count
    gate that the synthesis added to fix report 10's idempotency bug."""
    repo = dated_capture_repo
    _run_prune(repo, 30)
    log1 = _git(repo, "log", "--format=%s")
    r2 = _run_prune(repo, 30)
    assert r2.returncode == 0 and "PRUNE_NOOP" in r2.stdout, "the re-run must be a no-op (PRUNE_NOOP): %s" % r2.stdout
    assert "PRUNE_DONE" not in r2.stdout, "the re-run must NOT prune again"
    assert _git(repo, "log", "--format=%s") == log1, "history unchanged on the idempotent re-run"


@behavioral
def test_prune_all_older_keeps_the_latest_snapshot_never_empty(tmp_path):
    """When EVERY commit is older than the window (here 400/300/200/100 days, pruned to 90d), the prune still
    keeps the latest snapshot re-rooted onto a baseline — history is NEVER emptied. WHY (the critical safety
    edge): the all-older path forces keep_root=HEAD; re-rooting must never leave a zero-commit history, and the
    current capture (HEAD's tree + content) must always survive even when the whole window is in the past."""
    import time
    repo = tmp_path / "captures-old"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    now = int(time.time())
    for i, age in enumerate([400, 300, 200, 100]):
        (repo / "device.cfg").write_text("rev %d\n" % i, encoding="utf-8")
        _git(repo, "add", "-A")
        ts = "%d +0000" % (now - age * 86400)
        env = {**os.environ, "GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts}
        _git(repo, "commit", "-q", "-m", "backup rev %d" % i, capture=False, env=env)
    tree_before = _git(repo, "rev-parse", "HEAD^{tree}")
    body_before = (repo / "device.cfg").read_text(encoding="utf-8")

    r = _run_prune(repo, 90)
    assert r.returncode == 0 and "PRUNE_DONE" in r.stdout, "all-older must still prune (keep latest): %s" % r.stdout
    assert int(_git(repo, "rev-list", "--count", "HEAD")) >= 1, "history must NEVER be empty after a prune"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree_before, "the latest snapshot's tree must survive"
    assert (repo / "device.cfg").read_text(encoding="utf-8") == body_before, "the latest capture must survive"
    msgs = _git(repo, "log", "--format=%s").splitlines()
    assert "backup rev 3" in msgs, "the newest commit must be kept"
    assert "backup rev 0" not in msgs, "the oldest (400d) commit must be dropped"


@behavioral
def test_prune_recovers_from_a_stale_replace_ref(dated_capture_repo):
    """A stale `refs/replace/<keep_root>` left by a KILLED prior run (the crash-recovery hole, review COR-MF1)
    must be CLEARED at the start so the drop-set gate reads the REAL history and actually prunes — never masked
    as a clean no-op by the lingering virtual graft. WHY: the gate (`git rev-list --count --before`) traverses
    the grafted view, so without the cleanup a half-run reports PRUNE_NOOP while old objects are never reclaimed
    and history stays permanently grafted; a later prune could then mis-resolve keep_root and drop within-window
    commits. This seeds the stale graft and asserts the prune recovers (PRUNE_DONE, no replace refs left)."""
    repo = dated_capture_repo
    # simulate a prior killed run: an active virtual graft re-rooting the within-window keep at a `now` baseline,
    # which makes a NAIVE drop-set gate see drop=0 (the pre-cutoff ancestors are cut off the grafted view).
    keep = _git(repo, "rev-list", "--reverse", "--since=30 days ago", "HEAD").splitlines()[0]
    baseline = _git(repo, "commit-tree", keep + "^{tree}", "-m", "stale baseline")
    _git(repo, "replace", "--graft", keep, baseline)
    assert _git(repo, "for-each-ref", "refs/replace/") != "", "fixture: a stale replace ref is active"

    r = _run_prune(repo, 30)
    assert r.returncode == 0, "prune must exit 0\nSTDOUT:%s\nSTDERR:%s" % (r.stdout, r.stderr)
    assert "PRUNE_DONE" in r.stdout, "the prune must CLEAR the stale graft and actually prune, not mask as NOOP: %s" % r.stdout
    assert _git(repo, "for-each-ref", "refs/replace/") == "", "no stale refs/replace may remain after the prune"
    _git(repo, "fsck", "--full", capture=False)
    msgs = _git(repo, "log", "--format=%s").splitlines()
    assert "backup rev 4" in msgs and "backup rev 0" not in msgs, "pre-cutoff dropped, latest kept after recovery"


@behavioral
def test_prune_skips_when_the_lock_is_held(dated_capture_repo):
    """A held mkdir lock (`<git-dir>/kontroll-prune.lock`) makes the prune SKIP with PRUNE_NOOP — it does NOT
    rewrite history (review SEC-MF1: two concurrent prunes on the same captures dir must not race). WHY: a
    second prune racing a first could corrupt refs mid-filter-branch; the lock serializes them and the loser
    cleanly defers to the next cron tick. Pins that a held lock is a no-op, not a prune."""
    repo = dated_capture_repo
    lockdir = repo / ".git" / "kontroll-prune.lock"
    lockdir.mkdir()
    log_before = _git(repo, "log", "--format=%s")
    r = _run_prune(repo, 30)
    assert r.returncode == 0 and "PRUNE_NOOP locked" in r.stdout, "a held lock must skip (PRUNE_NOOP locked): %s" % r.stdout
    assert "PRUNE_DONE" not in r.stdout, "the prune must NOT rewrite history while the lock is held"
    assert _git(repo, "log", "--format=%s") == log_before, "history unchanged when locked"
    lockdir.rmdir()
