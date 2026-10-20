"""gitio.promote_commit — what is judged is what is promoted (C10; 2026-10-08 review, finding 9).

WHY: a promote reads two refs (`main`, the proposal), decides something about what it read (is it a fast-forward;
does the gate take the tree; is it the commit the operator named), and then moves `main`. Each of those reads is
a moment, and the canonical is a repository other processes write: another promote can move `main`, and a
proposal is a name that can come to mean another commit. If the move names a REF, or checks one of the two and
not the other, what lands is not what was judged:

  * `main` moved in between — the second of two racing promotes overwrote the first with a ref that was no longer
    a fast-forward of what it replaced, a promote lost silently while both reported success (finding 9);
  * the proposal re-pointed in between — the commit that landed was never read by the gate or by the human.

`promote_commit(run_id, <commit>, <main's commit>, <git dir>)` takes the two IDS that were read and runs one ref
transaction that verifies the proposal is still that commit and moves `main` only from that old value — both
without following a symbolic ref. These tests run REAL git in a temporary bare repository (tests/unit/
_canon_repo.py): only git can say which operands a transaction accepts, and each race is injected at the one
place it can happen — between the read and the transaction.
"""
import os
import subprocess

import pytest

import _canon_repo as R
from _readonly_pins import WRITE_VERBS, assert_git_runs_in_the_seam
from kontroll import canon, gitio

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]


def _at(bare, name):
    """The commit `name` (a full ref name) points at, or None — read the way the promote reads."""
    got = canon.read_ref(bare, name)
    return got[0] if got else None


def _between_the_read_and_the_transaction(monkeypatch, do):
    """Run `do()` once, right before the promote's `update-ref --stdin` starts — the only moment a writer can get
    between what was read and what is moved."""
    real = subprocess.run
    done = []

    def run(argv, *a, **kw):
        if list(argv[:3]) == ["git", "update-ref", "--stdin"] and not done:
            done.append(1)
            do()
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", run)


def test_a_clean_promote_fast_forwards_main_with_real_git(tmp_path, capsys):
    """The happy path against real git: `main` lands on the commit that was handed in, the promoted proposal's ref
    is gone, the other proposal is untouched, and the line printed names the run id AND the commit. A mocked git
    could not tell a transaction git accepts from one it does not."""
    r = R.build(tmp_path)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    assert _at(r.bare, canon.MAIN) == r.ff
    assert _at(r.bare, canon.PROPOSED + R.FF) is None, "the promoted proposal ref is cleaned up"
    assert _at(r.bare, canon.PROPOSED + R.STALE) == r.stale, "the other proposal is untouched"
    out = capsys.readouterr().out
    assert "promoted proposed/%s" % R.FF in out and r.ff in out


def test_a_promote_that_loses_the_race_is_refused_and_the_winner_is_kept(tmp_path, monkeypatch, capsys):
    """THE RACE ON `main`. Another promote advances `main` between this promote's read and its transaction. The
    transaction carries `main`'s old value, so git refuses it: `main` stays at the winner, this promote returns
    False and says that `main` moved, and its proposal survives for a re-check. Check-then-set overwrote the
    winner and reported success."""
    r = R.build(tmp_path)
    other = R.commit(r.bare, R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode(), [r.main], subject=b"the winner")
    _between_the_read_and_the_transaction(monkeypatch, lambda: R.set_ref(r.bare, canon.MAIN, other))
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert _at(r.bare, canon.MAIN) == other, "main keeps the promote that landed first"
    assert _at(r.bare, canon.PROPOSED + R.FF) == r.ff, "the refused proposal survives for a re-check"
    assert "main moved" in capsys.readouterr().out


def test_a_proposal_re_pointed_after_it_was_judged_is_not_promoted(tmp_path, monkeypatch, capsys):
    """THE RACE ON THE PROPOSAL. Between the read (and the judgment made on it) and the transaction, the proposal's
    ref is pushed again and names another commit — also a fast-forward of `main`. The transaction verifies the
    proposal's value, so git refuses: `main` is untouched, NEITHER commit lands, and the refusal says the proposal
    is no longer the commit that was judged. Guards the worst outcome of the two-step promote: it resolved the
    name a second time for the move, so the commit that reached `main` was one no gate and no human had read."""
    r = R.build(tmp_path)
    unread = R.commit(r.bare, R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode(), [r.main], subject=b"unread")
    _between_the_read_and_the_transaction(monkeypatch, lambda: R.set_ref(r.bare, canon.PROPOSED + R.FF, unread))
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert _at(r.bare, canon.MAIN) == r.main, "main is untouched"
    assert _at(r.bare, canon.PROPOSED + R.FF) == unread, "the re-pointed proposal is left for a person to look at"
    assert "no longer the commit that was judged" in capsys.readouterr().out


def test_a_commit_that_is_not_a_fast_forward_is_refused_by_its_id(tmp_path, capsys):
    """A proposal `main` has moved past, `main`'s own commit, and ids that are not ids are each refused before any
    transaction; `main` and the proposal are untouched. The fast-forward question is asked of the two IDS that
    were handed in — beside a tag called `main` it used to be asked of that name."""
    r = R.build(tmp_path)
    R.plant_short_names(r)
    assert gitio.promote_commit(R.STALE, r.stale, r.main, r.bare) is False
    assert "not a fast-forward" in capsys.readouterr().out
    assert gitio.promote_commit(R.FF, r.main, r.main, r.bare) is False
    capsys.readouterr()
    assert gitio.promote_commit(R.FF, "refs/heads/proposed/" + R.FF, r.main, r.bare) is False
    assert gitio.promote_commit(R.FF, r.ff, "main", r.bare) is False
    said = capsys.readouterr().out
    assert said.count("not named by full ids") == 2, "a NAME got as far as the fast-forward question: %r" % said
    assert _at(r.bare, canon.MAIN) == r.main and _at(r.bare, canon.PROPOSED + R.STALE) == r.stale
    # and beside every short name that would win, the right commit still lands
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True and _at(r.bare, canon.MAIN) == r.ff


def test_a_symbolic_proposal_costs_its_own_name_and_never_the_ref_it_points_at(tmp_path):
    """`promote_commit` handed a proposal ref that is a SYMBOLIC ref to another branch holding the commit: `main`
    lands that commit, the symbolic ref is deleted, and the branch it pointed at is still there. Guards
    `--no-deref` on the retire: without it the clean-up deleted the
    ref the pointer named — for a proposal written as a pointer to `main`, `main` itself. (The command refuses a
    symbolic proposal before it gets here; this is what holds if the ref turns symbolic after it was read.)"""
    r = R.build(tmp_path)
    R.set_ref(r.bare, "refs/heads/elsewhere", r.ff)
    R.write_ref_file(r.bare, canon.PROPOSED + "bbbbbbbbbbbb", "ref: refs/heads/elsewhere")
    assert gitio.promote_commit("bbbbbbbbbbbb", r.ff, r.main, r.bare) is True
    assert _at(r.bare, canon.MAIN) == r.ff
    assert _at(r.bare, "refs/heads/elsewhere") == r.ff, "the ref the pointer named was deleted"
    assert canon.read_ref(r.bare, canon.PROPOSED + "bbbbbbbbbbbb") is None


def test_a_main_that_turned_symbolic_is_replaced_not_followed(tmp_path):
    """`refs/heads/main` is a SYMBOLIC ref to another branch that holds main's commit when the transaction runs:
    afterwards `main` is an ordinary ref at the promoted commit, and the branch it pointed at has not moved.
    Guards `option no-deref` on the update: without it the transaction moves the ref the pointer names and leaves
    `main` a pointer — a promote that reports success while `main` is still whatever somebody aimed it at."""
    r = R.build(tmp_path)
    R.set_ref(r.bare, "refs/heads/trunk", r.main)
    os.remove(os.path.join(r.bare, "refs", "heads", "main"))
    R.write_ref_file(r.bare, canon.MAIN, "ref: refs/heads/trunk")
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    assert canon.read_ref(r.bare, canon.MAIN) == (r.ff, "commit", ""), "main is still a pointer, or did not move"
    assert _at(r.bare, "refs/heads/trunk") == r.main, "the ref main pointed at was moved instead"


def test_the_transaction_is_the_ruled_text(tmp_path, monkeypatch):
    """The bytes handed to `git update-ref --stdin` are exactly: `start`, `option no-deref` + `verify <proposal>
    <commit>`, `option no-deref` + `update refs/heads/main <commit> <main's commit>`, `prepare`, `commit`. Captured
    from a real promote. Guards the one line no outcome can show: `option no-deref` before the `verify` changes
    which ref git LOCKS for a symbolic proposal, not what the verify answers — so dropping it passes every
    behaviour test, and the transaction would then lock the ref a pointer names."""
    r = R.build(tmp_path)
    real, seen = subprocess.run, []

    def run(argv, *a, **kw):
        if list(argv[:3]) == ["git", "update-ref", "--stdin"]:
            seen.append(kw.get("input"))
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", run)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    assert seen == [("start\noption no-deref\nverify refs/heads/proposed/%s %s\noption no-deref\n"
                     "update refs/heads/main %s %s\nprepare\ncommit\n" % (R.FF, r.ff, r.ff, r.main)).encode("ascii")]


def test_a_proposal_staged_again_after_the_move_is_not_deleted(tmp_path, monkeypatch, capsys):
    """Between the transaction that moved `main` and the retire of the proposal, the service stages the same run
    id again at another commit: the retire, which names the commit that was promoted, leaves that new ref alone
    (and says a ref was left behind). Guards the old-value operand of the delete: an unconditional delete would
    throw away a proposal nobody has looked at."""
    r = R.build(tmp_path)
    again = R.commit(r.bare, R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode(), [r.ff], subject=b"staged again")
    real = subprocess.run

    def run(argv, *a, **kw):
        if list(argv[:2]) == ["git", "update-ref"] and "-d" in argv:
            R.set_ref(r.bare, canon.PROPOSED + R.FF, again)
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", run)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True and _at(r.bare, canon.MAIN) == r.ff
    assert _at(r.bare, canon.PROPOSED + R.FF) == again, "a proposal staged after the promote was deleted with it"
    assert "could not delete" in capsys.readouterr().out


def test_an_ancestry_that_cannot_be_told_is_not_a_fast_forward(tmp_path, monkeypatch, capsys):
    """When the fast-forward question has no answer (`canon.is_ancestor` returns None: git could not say), the
    promote refuses and `main` does not move. Guards "unknown is not yes" at the last place it matters: read as a
    pass, a promote would hand git a transaction for a commit nobody showed to be a fast-forward."""
    r = R.build(tmp_path)
    monkeypatch.setattr(canon, "is_ancestor", lambda *a, **k: None)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert _at(r.bare, canon.MAIN) == r.main and "not a fast-forward" in capsys.readouterr().out


def test_a_promote_reports_success_even_if_the_proposal_cannot_be_retired(tmp_path, monkeypatch, capsys):
    """When the transaction moved `main` and the delete of the proposal then fails (a lock, a permission edge on a
    ref the container created), the promote still returns True — `main` IS the commit — and prints a note that the
    proposal ref was left behind. Guards a half-completed promote being reported as a failure (an operator would
    run it again against a `main` that has moved) or as a silent success."""
    r = R.build(tmp_path)
    real = subprocess.run

    def run(argv, *a, **kw):
        if list(argv[:2]) == ["git", "update-ref"] and "-d" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", run)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    assert _at(r.bare, canon.MAIN) == r.ff and _at(r.bare, canon.PROPOSED + R.FF) == r.ff
    assert "could not delete proposed/%s" % R.FF in capsys.readouterr().out


def test_no_hook_of_the_canonical_runs_on_a_promote(tmp_path):
    """A `reference-transaction` hook in the canonical's `hooks/`, and one in a directory its config names, do not
    run when a proposal is promoted. Control: the same transaction outside the seam runs the hook. Guards the
    seam on the promote: the hook is a program the repository chose, the network service can write that
    repository, and the promote is run by an identity that holds more than the service does."""
    control, r = R.build(tmp_path, "control"), R.build(tmp_path)
    ran_c, ran = tmp_path / "ran-control", tmp_path / "ran"
    R.plant_hook(control, ran_c)
    R.git(control.bare, "update-ref", canon.MAIN, control.ff, control.main)
    assert ran_c.exists(), "control: this host's git was expected to run the hook outside the seam"
    R.plant_hook(r, ran)
    R.plant_hook(r, ran, directory=str(tmp_path / "named-hooks"))
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True and _at(r.bare, canon.MAIN) == r.ff
    assert not ran.exists(), "a hook of the canonical ran during the promote"


def test_a_canonical_the_seam_refuses_is_not_promoted_into(tmp_path, capsys):
    """A canonical that holds a `commondir` file is refused: nothing is promoted, and neither the repository nor
    the directory the file names changes. Guards the refusal reaching the promote at all: here it is the
    fast-forward question that cannot be answered, so nothing is tried. (Each write's own refusal is
    `test_a_canonical_refused_or_a_git_that_cannot_start_at_each_write_is_a_sentence`.)"""
    r, other = R.build(tmp_path), R.build(tmp_path, "planted")
    R.plant_commondir(r, other.bare)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert _at(other.bare, canon.MAIN) == other.main and _at(other.bare, canon.PROPOSED + R.FF) == other.ff


def test_promote_ref_reads_each_ref_once_and_refuses_what_is_not_a_proposal(tmp_path, capsys):
    """`promote_ref(<run id>)` — the entry for a caller that holds a run id and no commit — lands the proposal's
    commit; and it refuses, with `main` untouched, a run id that names no ref, a string that is not a run id, a
    symbolic ref under a proposal's name, and a canonical with no `main`. Guards the wrapper being a second door
    with rules of its own: it reads by full name through the same reader and hands the ids on."""
    r = R.build(tmp_path)
    # a pointer to a branch that IS a fast-forward of main: followed, it would be promoted
    R.set_ref(r.bare, "refs/heads/elsewhere", r.ff)
    R.write_ref_file(r.bare, canon.PROPOSED + "bbbbbbbbbbbb", "ref: refs/heads/elsewhere")
    assert gitio.promote_ref("ffffffffffff", cwd=r.bare) is False
    assert gitio.promote_ref("../main", cwd=r.bare) is False
    assert gitio.promote_ref("bbbbbbbbbbbb", cwd=r.bare) is False
    assert _at(r.bare, canon.MAIN) == r.main
    assert gitio.promote_ref(R.FF, cwd=r.bare) is True and _at(r.bare, canon.MAIN) == r.ff
    # … and handed a CHECKOUT (the default: the write root), it names that checkout's git directory
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", r.bare, str(work)], check=True, capture_output=True)
    R.git(work, "update-ref", canon.PROPOSED + R.STALE, R.git(work, "commit-tree", "-p", r.ff, "-m", "next",
                                                              r.ff + "^{tree}").decode())
    assert gitio.promote_ref(R.STALE, cwd=str(work)) is True
    assert _at(str(work / ".git"), canon.PROPOSED + R.STALE) is None
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(empty)], check=True, capture_output=True)
    assert gitio.promote_ref(R.FF, cwd=str(empty)) is False


def test_the_promote_is_a_registered_write_and_every_git_of_it_runs_in_the_seam():
    """`promote_commit` is in `WRITE_VERBS` (a read view that reached it would fail its pin), and inside it and
    `_what_moved` every process is `git update-ref …` passing `**canon.git_kwargs(…)` with a timeout. Guards the
    one function that moves `main` by a promote: a git of it that inherited the caller's environment, or named the
    repository some other way, would run the canonical's hooks and config as whoever promotes. (The other way
    `main` moves — a landing — has the same pin in `tests/unit/test_land.py`.)"""
    assert "promote_commit" in WRITE_VERBS
    assert_git_runs_in_the_seam("scripts/kontroll/gitio.py", verbs={"update-ref"},
                                functions=("promote_commit", "_what_moved", "promote_ref"))


# ---- the write at its edges: what reaches the transaction, locks, a git that cannot run, the pin's reach ----------- #

def test_a_string_that_is_not_a_run_id_never_reaches_the_transaction(tmp_path, monkeypatch, capsys):
    """`promote_commit` handed, in the run id's place, a string that carries a newline and a transaction command of
    its own (`<id> <commit>\\ncreate refs/heads/elsewhere`), a path, an empty string and an id with a newline:
    each returns False with "not a run id", starts no git at all, and leaves `main`, the proposal and the ref the
    planted command names as they were. Guards the one string of the caller's that is formatted into the text
    `update-ref --stdin` executes: the command validates it, but this function is the write and has its own callers
    (`promote_ref`, the permissive edition's automaton) — formatted in unchecked, the string is a second command."""
    r = R.build(tmp_path)
    started, real = [], subprocess.run
    monkeypatch.setattr(gitio.subprocess, "run", lambda argv, *a, **kw: started.append(argv) or real(argv, *a, **kw))
    planted = "%s %s\ncreate refs/heads/elsewhere" % (R.FF, r.ff)
    for bad in (planted, "../main", "", R.FF + "\n"):
        assert gitio.promote_commit(bad, r.ff, r.main, r.bare) is False, bad
        assert "not a run id" in capsys.readouterr().out
    assert started == [], "git was started for a string that is not a run id"
    monkeypatch.undo()
    assert _at(r.bare, canon.MAIN) == r.main and _at(r.bare, canon.PROPOSED + R.FF) == r.ff
    assert _at(r.bare, "refs/heads/elsewhere") is None


def test_a_proposal_that_is_mains_commit_is_neither_promoted_nor_retired(tmp_path, capsys):
    """A proposal ref that names `main`'s own commit (what a promote leaves behind when it cannot delete the ref):
    `promote_commit` returns False, says it is not a fast-forward, and the ref is still there. Guards the `P == M`
    half of the fast-forward rule in the function that writes: without it git finds `main` an ancestor of itself,
    the transaction "moves" `main` to where it is, the retire deletes the ref, and a promote of nothing is
    reported as a success. (The row above hands in `main`'s id under a proposal that is NOT at it, so the
    transaction's `verify` refused it for another reason.)"""
    r = R.build(tmp_path)
    R.set_ref(r.bare, canon.PROPOSED + "0000000000a2", r.main)
    assert gitio.promote_commit("0000000000a2", r.main, r.main, r.bare) is False
    assert "not a fast-forward" in capsys.readouterr().out
    assert _at(r.bare, canon.PROPOSED + "0000000000a2") == r.main and _at(r.bare, canon.MAIN) == r.main


def test_a_held_lock_is_said_as_a_lock_and_nothing_of_the_promotes_own_is_left(tmp_path, capsys):
    """With `refs/heads/main.lock` held, then with the proposal's own `.lock` held: the promote returns False,
    `main` is untouched, the sentence names a lock and does NOT say that `main` moved or that the
    proposal was re-pointed, and no lock file but the planted one is left in the repository. Guards two things:
    the seam's lock time limits (git gives up by itself and removes what it took, where a git killed at the
    caller's timeout leaves its locks behind), and the last branch of `_what_moved` — "main moved, run the promote
    again" said of a canonical that is merely locked sends an operator round a loop that cannot end."""
    for held in (canon.MAIN, canon.PROPOSED + R.FF):
        r = R.build(tmp_path / held.rsplit("/", 1)[-1])
        lock = os.path.join(r.bare, *held.split("/")) + ".lock"
        open(lock, "w").close()
        assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
        out = capsys.readouterr().out
        assert "lock" in out and "main moved" not in out and "no longer the commit" not in out, out
        assert _at(r.bare, canon.MAIN) == r.main and _at(r.bare, canon.PROPOSED + R.FF) == r.ff
        left = [os.path.join(d, f) for d, _, names in os.walk(r.bare) for f in names if f.endswith(".lock")]
        assert left == [lock], "the promote left a lock file of its own: %r" % left


def test_a_canonical_refused_or_a_git_that_cannot_start_at_each_write_is_a_sentence(tmp_path, monkeypatch, capsys):
    """The seam refusing the repository at the TRANSACTION (a `commondir` file planted after the fast-forward
    check passed), git not starting for the transaction, and the reads of `_what_moved` failing after a refused
    transaction: each returns False with a sentence, `main` untouched — never a traceback. And the seam refusing
    the repository at the RETIRE, after `main` has moved: True, with the note that the proposal was left behind.
    Guards the three `except` clauses of the write, which no other row reaches: the row for a `commondir` file is
    answered by the fast-forward check (it cannot say, so nothing is promoted) before the transaction is tried —
    narrowed to `OSError`, each clause passed every test, and the last one is an exception raised AFTER `main`
    moved, which a caller reads as a failed promote."""
    # the seam refuses at the transaction
    r, other = R.build(tmp_path / "a"), R.build(tmp_path / "a-planted")
    monkeypatch.setattr(canon, "is_ancestor", lambda *a, **k: True)
    R.plant_commondir(r, other.bare)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert "git could not run the promote" in capsys.readouterr().out
    assert _at(other.bare, canon.MAIN) == other.main
    monkeypatch.undo()

    # git cannot be started for the transaction
    r, real = R.build(tmp_path / "b"), subprocess.run

    def no_git(argv, *a, **kw):
        if list(argv[:3]) == ["git", "update-ref", "--stdin"]:
            raise FileNotFoundError("git")
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", no_git)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert "git could not run the promote" in capsys.readouterr().out
    monkeypatch.undo()
    assert _at(r.bare, canon.MAIN) == r.main

    # the transaction is refused (a lock), and then the canonical cannot be read to say why
    r = R.build(tmp_path / "c")
    open(os.path.join(r.bare, "refs", "heads", "main.lock"), "w").close()

    def unreadable(*a, **k):
        raise canon.Unreadable("planted")

    monkeypatch.setattr(canon, "read_ref", unreadable)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert "could not be read after the promote was refused" in capsys.readouterr().out
    monkeypatch.undo()

    # the seam refuses at the retire: main HAS moved, so this is a success with a note
    r, other, real = R.build(tmp_path / "d"), R.build(tmp_path / "d-planted"), subprocess.run

    def plant_after_the_move(argv, *a, **kw):
        done = real(argv, *a, **kw)
        if list(argv[:3]) == ["git", "update-ref", "--stdin"]:
            R.plant_commondir(r, other.bare)
        return done

    monkeypatch.setattr(gitio.subprocess, "run", plant_after_the_move)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    assert "could not delete proposed/%s" % R.FF in capsys.readouterr().out
    monkeypatch.undo()
    os.remove(os.path.join(r.bare, "commondir"))
    assert _at(r.bare, canon.MAIN) == r.ff and _at(other.bare, canon.MAIN) == other.main


def test_a_git_stopped_at_its_time_limit_is_reported_by_what_main_is(tmp_path, monkeypatch, capsys):
    """The transaction's git is stopped at the time limit (a `TimeoutExpired`): when it had not moved `main`, the
    promote returns False and says `main` is not this commit; when it HAD — the move is one rename, and the limit
    can fall after it — the promote returns True and retires the proposal. Guards the sentence "nothing promoted"
    being said of a git whose outcome nobody read: said after a move, an operator runs the promote again against a
    `main` that already is the commit and is told to stage the change again."""
    r, real = R.build(tmp_path / "before"), subprocess.run

    def stopped_before(argv, *a, **kw):
        if list(argv[:3]) == ["git", "update-ref", "--stdin"]:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", stopped_before)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is False
    assert "main is not this commit" in capsys.readouterr().out
    monkeypatch.undo()
    assert _at(r.bare, canon.MAIN) == r.main

    r = R.build(tmp_path / "after")

    def stopped_after(argv, *a, **kw):
        if list(argv[:3]) == ["git", "update-ref", "--stdin"]:
            real(argv, *a, **kw)
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", stopped_after)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    monkeypatch.undo()
    assert _at(r.bare, canon.MAIN) == r.ff and _at(r.bare, canon.PROPOSED + R.FF) is None
    assert "promoted proposed/%s" % R.FF in capsys.readouterr().out


def test_promote_ref_refuses_a_pointer_for_main_a_tag_for_a_proposal_and_an_unreadable_canonical(tmp_path, capsys):
    """`promote_ref` returns False, with nothing moved, for a `main` that is a symbolic ref (to a branch that holds
    main's commit — followed, the promote would go through), for a proposal ref that holds a TAG object of a
    fast-forward commit, and for a canonical the seam refuses — each with a sentence. Guards the wrapper's own
    three checks: it is kept as the entry for a caller that holds a run id, and each of them could be dropped with
    the rows above green (they hand it a missing ref, a string that is not an id, a symbolic PROPOSAL and a
    repository with no `main` at all)."""
    r = R.build(tmp_path)
    tag = R.git(r.bare, "mktag", stdin=("object %s\ntype commit\ntag t\ntagger t <t@example.test> 1750000000 +0000"
                                        "\n\nt\n" % r.ff).encode()).decode()
    R.write_ref_file(r.bare, canon.PROPOSED + "cccccccccccc", tag)
    assert gitio.promote_ref("cccccccccccc", cwd=r.bare) is False
    assert "not a proposal" in capsys.readouterr().out and _at(r.bare, canon.MAIN) == r.main

    R.set_ref(r.bare, "refs/heads/trunk", r.main)
    os.remove(os.path.join(r.bare, "refs", "heads", "main"))
    R.write_ref_file(r.bare, canon.MAIN, "ref: refs/heads/trunk")
    assert gitio.promote_ref(R.FF, cwd=r.bare) is False
    assert "cannot read refs/heads/main as a branch" in capsys.readouterr().out
    assert canon.read_ref(r.bare, canon.MAIN)[2] == "refs/heads/trunk" and _at(r.bare, "refs/heads/trunk") == r.main
    assert _at(r.bare, canon.PROPOSED + R.FF) == r.ff

    other = R.build(tmp_path, "planted")
    R.plant_commondir(r, other.bare)
    assert gitio.promote_ref(R.FF, cwd=r.bare) is False
    assert "could not be read" in capsys.readouterr().out and _at(other.bare, canon.MAIN) == other.main


def test_the_retire_is_the_ruled_argv(tmp_path, monkeypatch):
    """The argv of the retire is exactly `git update-ref --no-deref -d --end-of-options <proposal> <commit>`,
    captured from a real promote. Guards `--end-of-options`, which no outcome shows while the ref name is a
    validated one: it is what keeps the two operands operands if a name ever reaches this call another way."""
    r = R.build(tmp_path)
    real, seen = subprocess.run, []

    def run(argv, *a, **kw):
        if list(argv[:2]) == ["git", "update-ref"] and "-d" in argv:
            seen.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", run)
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    assert seen == [["git", "update-ref", "--no-deref", "-d", "--end-of-options", canon.PROPOSED + R.FF, r.ff]]


def test_the_seam_pin_follows_a_pinned_function_into_the_helpers_it_calls(tmp_path, monkeypatch):
    """With `functions=("f",)`, a git call outside the seam in a module-level helper that `f` calls — directly, and
    through a second helper — fails the pin; the same call in a function `f` does not reach is not looked at.
    Guards the narrowed pin against the obvious refactor: the promote's git moved out of `promote_commit` into a
    helper of `gitio.py` ran outside the seam with every pin green, because the pin read the three named
    functions and nothing they call."""
    import _readonly_pins
    module = ("import subprocess\nfrom kontroll import canon\n\n\n"
              "def f(repo):\n    return g(repo), subprocess.run(['git', 'update-ref', '--stdin'], timeout=5, "
              "**canon.git_kwargs(repo))\n\n\n"
              "def g(repo):\n    return h(repo)\n\n\n"
              "def h(repo):\n    return %s\n\n\n"
              "def elsewhere(repo):\n    return subprocess.run(['git', 'status'], cwd=repo)\n")
    monkeypatch.setattr(_readonly_pins, "ROOT", str(tmp_path))
    (tmp_path / "ok.py").write_text(module % "repo", encoding="utf-8")
    _readonly_pins.assert_git_runs_in_the_seam("ok.py", verbs={"update-ref"}, functions=("f",))
    (tmp_path / "m.py").write_text(module % "subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo)", encoding="utf-8")
    with pytest.raises(AssertionError, match="rev-parse"):
        _readonly_pins.assert_git_runs_in_the_seam("m.py", verbs={"update-ref"}, functions=("f",))
