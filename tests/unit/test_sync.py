"""The sync — the worktree brought forward to what a promote put on the canonical's `main`, with no transport
(T25; `gitio.sync_worktree`, `scripts/kontroll-land.py --sync`; SECURITY.md C10).

WHY: a promote moves the canonical's `main` to a commit the operator's worktree has never seen, and from then on
every landing from that worktree is refused as not a fast-forward — the deploy's, `update.sh`'s, the bundle
update's. The remedy the refusal named was `git fetch local && git rebase local/main`, by hand: a fetch runs the
canonical's serving side (`upload-pack`, under the canonical's config, as the fetcher — root in the installer
container), which rule 8 of the canonical's doors removed from every other path. The sync is the landing turned
round: the canonical's objects come out as a pack on a pipe (`pack-objects --stdout` in the seam), go into the
worktree's store (`index-pack --strict --stdin`, as the caller), and the worktree's own commits are replayed onto
`main`. These rows run REAL git: only git can say what a pack brings, what a replay keeps, and whether a hook ran.
"""
import ast
import errno
import glob
import hashlib
import importlib.util
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import threading

import pytest

from _readonly_pins import WRITE_VERBS, assert_git_runs_in_the_seam
from kontroll import canon, gitio

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
_SVC = {"GIT_AUTHOR_NAME": "svc", "GIT_AUTHOR_EMAIL": "svc@example.test",
        "GIT_COMMITTER_NAME": "svc", "GIT_COMMITTER_EMAIL": "svc@example.test"}
# The clean registry the promote gate passes (tests/unit/test_land.py's CLEAN), so a landing after a sync is judged.
CLEAN = {"modules/_core.yml": "collections: []\n", "config/fleet.yml": "enabled_modules: [alpha]\n",
         "modules/alpha/module.yml": "key: alpha\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n"}
RID = "0123456789ab"
# The sync's functions, and the argv words none of them may carry: each is a transport (or starts one).
SYNC = ("sync_worktree", "_bring_main", "_held_in_canonical", "_pack_in_canonical", "_receive_in_worktree",
        "_not_replayable", "_undo_replay")
TRANSPORT = {"fetch", "pull", "clone", "ls-remote", "upload-pack", "push", "remote", "fetch-pack"}
# The box's paths as they are, and with a space and a quote in each (`box` parametrised indirectly): a sentence that
# prints a command must quote every path in it for the shell (fold 8's own mutants, G8-*-unquoted).
NAMES = [pytest.param(("canonical.git", "worktree"), id="plain"),
         pytest.param(("canon ical'x.git", "work tree'x"), id="spaced", marks=pytest.mark.skipif(
             os.name != "posix", reason="a directory whose name holds a space and a quote"))]


def _git(cwd, *args, check=True, env=_WHO):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, env=dict(os.environ, **env))
    if check and p.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace")))
    return p.stdout.decode("utf-8", "replace").strip()


def _write(tree, files):
    for rel, text in files.items():
        path = os.path.join(tree, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def _commit(tree, files, subject, env=_WHO):
    _write(tree, files)
    _git(tree, "add", "-A", env=env)
    _git(tree, "commit", "-q", "-m", subject, env=env)
    return _git(tree, "rev-parse", "HEAD")


@pytest.fixture
def box(tmp_path, request):
    """A node in miniature, as test_land.py's: a bare canonical (`bare`) whose `main` is the clean tree (`seed`), and
    the operator's worktree (`wt`) at that commit with a `local` remote naming it. `promote(files)` is what a
    promote leaves: a commit on top of `main`, written by somebody else (the service's own clone), that the worktree
    has never fetched. A test parametrised over `NAMES` (indirectly) runs it also with a space and a quote in
    both paths."""
    names = getattr(request, "param", ("canonical.git", "worktree"))
    bare, wt = str(tmp_path / names[0]), str(tmp_path / names[1])
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    seed = _commit(wt, CLEAN, "seed")
    _git(wt, "remote", "add", "local", bare)
    _git(wt, "push", "-q", "local", "HEAD:refs/heads/main")
    made = []

    def promote(files, subject="a promote"):
        svc = str(tmp_path / ("svc%d" % len(made)))
        subprocess.run(["git", "clone", "-q", bare, svc], check=True, capture_output=True)
        _git(svc, "config", "core.autocrlf", "false")
        made.append(_commit(svc, files, subject, env=_SVC))
        _git(svc, "push", "-q", "origin", "HEAD:refs/heads/main", env=_SVC)
        return made[-1]
    return type("Box", (), {"bare": bare, "wt": wt, "seed": seed, "promote": staticmethod(promote)})()


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_the_box_takes_the_names_it_is_given(box, request):
    """`box`, parametrised over NAMES: the worktree and the canonical are named as the param says (`work tree'x` and
    `canon ical'x.git` in the spaced case, which `shlex.quote` then changes). Guards the parametrisation the quote rows
    stand on: a box that ignored its param ran every spaced row on plain names, which quoting leaves as they are, so
    each sync row's quote passed whether the code quotes or not (read B-f8, N3:
    B9-sync-box-ignores-param+merge-unquoted)."""
    names = request.node.callspec.params["box"]
    assert os.path.basename(box.wt) == names[1] and os.path.basename(box.bare) == names[0], (box.wt, box.bare)
    assert (shlex.quote(box.wt) != box.wt) == (" " in names[1]), box.wt


def _main(bare):
    got = canon.read_ref(bare, canon.MAIN)
    return got[0] if got else None


def _has(repo, oid):
    return subprocess.run(["git", "-C", repo, "cat-file", "-e", oid + "^{commit}"], capture_output=True).returncode == 0


def _state(repo):
    """What a sync that changes nothing leaves as it was: every ref and what it names, HEAD, and the branch HEAD is."""
    return (_git(repo, "for-each-ref", "--format=%(refname) %(objectname)"), _git(repo, "rev-parse", "HEAD"),
            _git(repo, "symbolic-ref", "-q", "HEAD", check=False))


def _files(repo):
    """Every file under `repo` with the hash of its bytes."""
    out = {}
    for where, _dirs, names in os.walk(repo):
        for name in names:
            path = os.path.join(where, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, repo)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _packs(repo):
    """The pack indexes in `repo`'s object store."""
    return sorted(glob.glob(os.path.join(repo, ".git", "objects", "pack", "*.idx")))


def _objects_in(idx):
    """How many objects the pack of index `idx` holds (`git show-index`, one line per object), asked inside the
    repository the pack is in (outside one, git does not know the hash the index is made with)."""
    with open(idx, "rb") as fh:
        return len(subprocess.run(["git", "-C", os.path.dirname(idx), "show-index"], stdin=fh, capture_output=True,
                                  check=True).stdout.splitlines())


def _land_cli():
    path = os.path.join(ROOT, "scripts", "kontroll-land.py")
    spec = importlib.util.spec_from_file_location("kontroll_land_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the rule, through the library ---------------------------------------------------------------------------- #

def test_in_step_is_said_and_changes_nothing(box, tmp_path):
    """Three worktrees the landing needs nothing done to: the canonical has no `main` yet (the landing seeds it);
    `main` IS HEAD; `main` is an ancestor of HEAD (the operator's own commits since — the landing fast-forwards).
    Each answers rc 0 with `in step — …`, with and without `check`, and leaves the worktree's refs, HEAD and branch
    as they were. Guards the deploy's common case: a sync that rewrote or touched a worktree with nothing to bring
    would turn every deploy's 0-changed into a change."""
    empty = str(tmp_path / "empty.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", empty], check=True, capture_output=True)
    before = _state(box.wt)
    for check in (True, False):
        rc, said = gitio.sync_worktree(box.wt, empty, check=check)
        assert (rc, said) == (0, "in step — the canonical has no main yet (the landing seeds it with HEAD)")
        rc, said = gitio.sync_worktree(box.wt, box.bare, check=check)
        assert (rc, said) == (0, "in step — HEAD is the canonical's main (%s)" % box.seed)
    assert _state(box.wt) == before
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "the operator's own change")
    before = _state(box.wt)
    for check in (True, False):
        rc, said = gitio.sync_worktree(box.wt, box.bare, check=check)
        assert rc == 0 and said.startswith("in step — the canonical's main (%s) is an ancestor of HEAD (%s)"
                                           % (box.seed, mine)), said
    assert _state(box.wt) == before


def test_a_promote_the_worktree_has_not_seen_comes_across_and_the_landing_is_then_up_to_date(box):
    """A promote moved `main` to P, which the worktree has never seen, and the worktree has no commit of its own
    since: the sync brings P across (the worktree now holds it), HEAD is P on the same branch, P's file is in the
    working tree, `refs/remotes/local/main` names P (as a `fetch local` would have left it) and the message says
    `brought forward … 0 commit(s)`; the landing that follows is `up to date`, and a second sync says it is in
    step (`HEAD is the canonical's main`). Guards the case every deploy after a promote meets: before the sync, its
    landing was refused."""
    promoted = box.promote({"promoted.txt": "from a promote\n"})
    assert not _has(box.wt, promoted)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said == ("brought forward — HEAD is %s: 0 commit(s) of the worktree's own on the canonical's "
                                "main %s (HEAD was %s)" % (promoted, promoted, box.seed)), said
    assert _git(box.wt, "rev-parse", "HEAD") == promoted and _git(box.wt, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _git(box.wt, "rev-parse", "refs/remotes/local/main") == promoted
    assert open(os.path.join(box.wt, "promoted.txt"), encoding="utf-8").read() == "from a promote\n"
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert rc == 0 and said.startswith("up to date"), said
    assert gitio.sync_worktree(box.wt, box.bare) == (0, "in step — HEAD is the canonical's main (%s)" % promoted)
    assert _main(box.bare) == promoted


def test_the_worktrees_own_commits_are_replayed_onto_the_promote_and_then_land(box, monkeypatch):
    """A promote P on `main`, and two commits of the operator's own on the old `main` (the deploy's instance-state
    commit is one): the sync replays them onto P — HEAD's grandparent is P, the tree holds P's file and both of
    the operator's, each replayed commit keeps its subject and its AUTHOR and is committed as `kontroll` — and the
    landing that follows fast-forwards `main` to HEAD. The caller's environment names another committer
    (`GIT_COMMITTER_*`, which beat git's `-c user.*`), and the worktree's config asks for every commit to be signed
    by a program that cannot sign (`commit.gpgSign`, `gpg.program false`). Guards the replay itself: the
    operator's commits must survive the promote (a sync that reset to `main` would drop the instance's state), and
    must still say who wrote them; the replay is the sync's, so neither the deployer's committer variables nor a
    signing key the deploy's git has no HOME for may decide it (unsigned, it would stop at the first commit)."""
    monkeypatch.setenv("GIT_COMMITTER_NAME", "someone else")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "someone@example.test")
    promoted = box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"state.yml": "remotes: [cli]\n"}, "chore(instance): commit the instance state")
    _commit(box.wt, {"mine.txt": "m\n"}, "the operator's second change")
    _git(box.wt, "config", "commit.gpgSign", "true")
    _git(box.wt, "config", "gpg.program", "false")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    head = _git(box.wt, "rev-parse", "HEAD")
    assert rc == 0 and said.startswith("brought forward — HEAD is %s: 2 commit(s)" % head), said
    assert _git(box.wt, "rev-parse", "HEAD~2") == promoted
    assert set(_git(box.wt, "ls-tree", "--name-only", "HEAD").split()) >= {"promoted.txt", "state.yml", "mine.txt"}
    assert _git(box.wt, "log", "-2", "--format=%s|%an <%ae>|%cn <%ce>").splitlines() == [
        "the operator's second change|t <t@example.test>|kontroll <kontroll@localhost>",
        "chore(instance): commit the instance state|t <t@example.test>|kontroll <kontroll@localhost>"]
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert (rc, _main(box.bare)) == (0, head), said
    assert said == "landed refs/heads/main %s (was %s)" % (head, promoted)


def test_check_says_there_is_something_to_bring_forward_and_brings_nothing(box):
    """With a promote on `main` and a commit of the operator's own, `check` answers 3 (`would bring forward …
    Nothing was changed.`) and changes nothing: the worktree still does not hold P, its refs and HEAD are as they
    were, and the canonical's `main` is P. Guards the dry-run: the deploy's `--check` and both update scripts ask
    this first, and a check that brought objects across or replayed would be the real run under another name."""
    promoted = box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    before = _state(box.wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare, check=True)
    assert rc == 3 and said == ("would bring forward — the canonical's main (%s) has commit(s) the worktree %s does "
                                "not; a real run brings them in and replays HEAD's own commits on top. Nothing was "
                                "changed." % (promoted, os.path.abspath(box.wt))), said
    assert not _has(box.wt, promoted) and _state(box.wt) == before and _main(box.bare) == promoted


def test_a_conflict_is_undone_and_its_files_named(box):
    """The promote and the operator both add `note.txt`, differently: the replay stops on the conflict, and the sync
    undoes it — rc 1, the sentence names `note.txt` and the remedy (`rebase local/main`, which the sync set
    `refs/remotes/local/main` for); HEAD is the operator's commit on the same branch, no rebase is in progress,
    nothing is uncommitted and the file holds the operator's text. Guards the never-strand rule: a deploy that left
    a rebase half done would leave the operator a worktree no git command of the play could commit in."""
    promoted = box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "without a conflict, in note.txt." in said and "rebase local/main" in said, said
    assert _state(box.wt)[1:] == (mine, "refs/heads/main")
    assert _git(box.wt, "rev-parse", "refs/remotes/local/main") == promoted
    assert _git(box.wt, "status", "--porcelain") == ""
    assert not any(os.path.exists(os.path.join(box.wt, ".git", n)) for n in ("rebase-merge", "rebase-apply"))
    assert open(os.path.join(box.wt, "note.txt"), encoding="utf-8").read() == "mine\n"


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_a_merge_in_the_worktrees_own_history_is_refused_not_flattened(box):
    """The operator's history since `main` holds a merge, and a promote moved `main`: `check` answers 3 (the
    worktree does not hold P, so it cannot see the merge yet); the real run brings P across, names it as
    `refs/remotes/local/main` and refuses — rc 1, `include a merge`, the remedy `git -C <worktree> merge <P>` —
    with HEAD as it was; `check` then answers 1 too. After that merge the sync is in step and the landing lands.
    With a space and a quote in the paths, the remedy's worktree is quoted for the shell (`shlex.quote`).
    Guards the replay against flattening a merge into a line (rebase's default), which rewrites history the
    operator chose to keep; and the remedy pasted as printed (unquoted, a path with a space is two words: fold 8's
    own mutant G8-merge-in-mine-unquoted)."""
    _git(box.wt, "checkout", "-q", "-b", "side")
    _commit(box.wt, {"side.txt": "s\n"}, "side")
    _git(box.wt, "checkout", "-q", "main")
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _git(box.wt, "merge", "-q", "--no-edit", "--no-ff", "side")
    merged = _git(box.wt, "rev-parse", "HEAD")
    promoted = box.promote({"promoted.txt": "p\n"})
    assert gitio.sync_worktree(box.wt, box.bare, check=True)[0] == 3
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "include a merge" in said, said
    assert "`git -C %s merge %s`" % (shlex.quote(box.wt), promoted) in said, said
    assert _has(box.wt, promoted) and _git(box.wt, "rev-parse", "refs/remotes/local/main") == promoted
    assert _state(box.wt)[1:] == (merged, "refs/heads/main")
    rc, said = gitio.sync_worktree(box.wt, box.bare, check=True)
    assert rc == 1 and "include a merge" in said, said
    _git(box.wt, "merge", "-q", "--no-edit", promoted)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and "is an ancestor of HEAD" in said, said
    assert gitio.land_commit(box.wt, box.bare)[0] == 0 and _main(box.bare) == _git(box.wt, "rev-parse", "HEAD")


def test_a_worktree_that_shares_no_history_with_the_canonical_is_refused(box, tmp_path):
    """A repository with a root commit of its own, whose `local` remote names the canonical: `check` answers 3 (it
    does not hold `main`, so it cannot compare histories); the real run brings `main` across and refuses — rc 1,
    `share no history` — with HEAD and the branch as they were. Guards the replay against grafting a foreign
    history onto `main`: a rebase of an unrelated history replays every commit it has."""
    other = str(tmp_path / "other")
    subprocess.run(["git", "init", "-q", "-b", "main", other], check=True, capture_output=True)
    _git(other, "config", "core.autocrlf", "false")
    _commit(other, {"x.txt": "x\n"}, "another root")
    _git(other, "remote", "add", "local", box.bare)
    before = _state(other)
    assert gitio.sync_worktree(other, box.bare, check=True)[0] == 3
    rc, said = gitio.sync_worktree(other, box.bare)
    assert rc == 1 and "share no history" in said and "HEAD was not changed" in said, said
    assert _state(other)[1:] == before[1:]


def test_changes_to_tracked_files_refuse_and_untracked_files_do_not(box):
    """A tracked file changed and not committed: the real run refuses (rc 1, `not committed`) before anything comes
    across — the worktree still does not hold P; `check` answers 3, since it does not judge the working tree (a
    deploy commits the instance state first). With the change put back and an untracked file beside, the sync
    brings the worktree forward and the untracked file is still there. Guards the working tree: a replay over an
    uncommitted change would stash it, carry it or lose it — and refusing for an untracked file would refuse every
    checkout with a scratch file in it."""
    promoted = box.promote({"promoted.txt": "p\n"})
    _write(box.wt, {"modules/_core.yml": "collections: [changed]\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "not committed" in said and not _has(box.wt, promoted), said
    assert gitio.sync_worktree(box.wt, box.bare, check=True)[0] == 3
    _git(box.wt, "checkout", "--", "modules/_core.yml")
    _write(box.wt, {"scratch.txt": "mine, untracked\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    assert open(os.path.join(box.wt, "scratch.txt"), encoding="utf-8").read() == "mine, untracked\n"


def test_the_names_of_an_operation_in_progress_are_these():
    """`gitio._SYNC_BUSY` is exactly what git leaves in a worktree's git directory while a rebase (`rebase-merge`,
    `rebase-apply`), a merge (`MERGE_HEAD`), a cherry-pick (`CHERRY_PICK_HEAD`), a revert (`REVERT_HEAD`), a
    sequence of picks or reverts (`sequencer`) or a bisect (`BISECT_LOG`) is in progress. Guards a name dropped:
    the row below takes its cases from the tuple, so a name taken out of it takes its row with it, and the play's
    `sync_busy` is held equal to the tuple (test_local_canonical.py) — only a literal holds the set itself."""
    assert gitio._SYNC_BUSY == ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                                "BISECT_LOG", "sequencer")


@pytest.mark.parametrize("busy", gitio._SYNC_BUSY)
def test_a_rebase_merge_cherry_pick_revert_or_bisect_in_progress_refuses(box, busy):
    """The worktree's git directory holds what an unfinished rebase, merge, cherry-pick or revert (of one commit, or
    of a sequence) or a bisect leaves (here planted, a directory or a file as git leaves it): the sync refuses —
    rc 1, `in progress`, naming it — with and without `check`, and nothing came across; a rebase's refusal alone
    (`rebase-merge`, `rebase-apply`) adds how a sync's replay cut short is put back (`git -C <worktree> rebase
    --abort`), the lock to remove first (`<git dir>/index.lock`), that `git status --untracked-files=all --ignored`
    then names main's files the replay left, and that `git rebase --continue` can drop the commit it was replaying.
    Guards a worktree the operator is in the middle of: a sync that started a replay there would mix its
    commits into the operator's half-done one, and one on a bisect's detached HEAD would replay onto an older commit;
    and the remedy after a run killed in its replay: `--continue`, which "finish it" offered, landed on `main` without
    the operator's commit (read A-f5, N1), the abort printed failed on the lock a killed git leaves (read B-f6, N2), and
    the files of main's it left untracked were never named (read A-f6, N1), nor, by a status case-blind under
    `core.ignoreCase`, one whose name differs from a tracked file's only in case (read A-f8, S1: the status is now
    `-c core.ignoreCase=false status …`)."""
    promoted = box.promote({"promoted.txt": "p\n"})
    path = os.path.join(box.wt, ".git", busy)
    if busy.startswith("rebase-") or busy == "sequencer":
        os.makedirs(path)
    else:
        with open(path, "w", encoding="ascii", newline="\n") as fh:
            fh.write(box.seed + "\n")
    for check in (True, False):
        rc, said = gitio.sync_worktree(box.wt, box.bare, check=check)
        assert rc == 1 and "in progress" in said and busy in said, said
        cut = "`git -C %s rebase --abort` puts HEAD back where it was before the replay" % box.wt
        lock = "(if git says %s exists and no git is running, remove that file first;" % os.path.join(
            box.wt, ".git", "index.lock")
        status = ("then `git -C %s -c core.ignoreCase=false status --untracked-files=all --ignored` names, among your "
                  "own," % box.wt)
        assert all(t in said for t in (cut, lock, status, "can drop the commit it was replaying")) == (
            busy.startswith("rebase-")), said
    assert not _has(box.wt, promoted)


@pytest.mark.skipif(os.name != "posix", reason="a directory whose name holds a space and a quote")
def test_the_abort_a_rebase_in_progress_names_quotes_the_worktree_and_the_lock_for_the_shell(box, tmp_path):
    """A worktree at `<tmp>/w t'x` (a space and a quote in its name) with a rebase of the operator's stopped on a
    conflict in it: the sync's refusal prints the abort, the lock and the status each with its path quoted for the
    shell (`shlex.quote`), as is the listing of the tracked files git's status does not compare (`ls-files -v |
    grep -v '^H '`), saying it and the commands after it are bash's (`$'…'`, `<(…)`), and the abort as printed, run by
    bash from another directory, ends the rebase and puts HEAD back. Guards the remedy printed as a command: unquoted, a
    path with a space is two words, and the pasted abort fails or acts on another path (read A-f6, the unquoted
    worktree; read B-f6, N2); and the flagged files a replay cut short may have left main's bytes in, which that status
    does not see (fold 8's own mutant G8-cutshort-flag-unsaid); the status case for case (read A-f8, S1) and the listing
    printing a path as git quotes it by default, with how to give a quoted one back (read A-f8, N3; read A-f9, N2), a
    link compared by its text (read A-f9, N4); the note that the commands are bash's, which `sh` cannot run (read A-f11,
    N4); and that sentence whole, the comparison with main's and the put-back with it: the row held the listing alone,
    so the rest could go (read B-f8, N2: B9-cutshort-putback-dropped, -mid-dropped)."""
    wt = str(tmp_path / "w t'x")
    subprocess.run(["git", "clone", "-q", box.bare, wt], check=True, capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    mine = _commit(wt, {"note.txt": "mine\n"}, "mine")
    box.promote({"note.txt": "promoted\n"})
    _git(wt, "fetch", "-q", "origin")
    _git(wt, "rebase", "origin/main", check=False)                  # stops on the conflict, a rebase in progress
    assert os.path.isdir(os.path.join(wt, ".git", "rebase-merge"))
    rc, said = gitio.sync_worktree(wt, box.bare)
    q = shlex.quote(wt)
    assert rc == 1 and q != wt and "`git -C %s rebase --abort`" % q in said, said
    assert "if git says %s exists" % shlex.quote(os.path.join(wt, ".git", "index.lock")) in said, said
    assert "`git -C %s -c core.ignoreCase=false status --untracked-files=all --ignored`" % q in said, said
    flagged = ("`git -C %s -c core.quotePath=true ls-files -v | grep -v '^H '` (this and the commands after it are "
               "bash's: `$'…'` and `<(…)`) names any tracked file git's status "
               "does not compare (skip-worktree, assume-unchanged; under `core.ignoreStat`, every file; a name the "
               "listing prints in double quotes is quoted the C way: give it to the shell as `$'…'`, with what is "
               "between the quotes, each `'` in it written `\\'`): the replay may have left main's bytes in one, and "
               "that status does not see them. Compare each with main's (`git -C %s cat-file --filters "
               "refs/remotes/local/main:<path> | cmp - %s/<path>`; for a link, whose target `cmp` would read, its "
               "text: `git -C %s cat-file -p refs/remotes/local/main:<path> | cmp - <(readlink -nv -- %s/<path>)`); "
               "where one holds main's bytes or is missing, put HEAD's back (`git -C %s checkout "
               "--ignore-skip-worktree-bits HEAD -- <path>`). `git rebase --continue` can drop"
               % (q, q, q, q, q, q))
    assert flagged in said, said
    abort = re.search(r"`(git -C .*? rebase --abort)`", said).group(1)
    p = subprocess.run(["bash", "-c", abort], capture_output=True, cwd=str(tmp_path))
    assert p.returncode == 0, p.stderr
    assert _git(wt, "rev-parse", "HEAD") == mine and not os.path.lexists(os.path.join(wt, ".git", "rebase-merge"))


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="the owner is a POSIX uid")
def test_a_caller_that_does_not_own_the_worktree_is_refused_before_anything_is_written(box, monkeypatch):
    """The process's effective uid is not the owner of the worktree's git directory (root running the sync on the
    operator's checkout): the real run refuses — rc 1, `runs as uid`, naming both uids — and nothing came across;
    `check` still answers 3, since it writes nothing. Guards the operator's repository: objects and ref files root
    wrote there are what the operator's own git cannot write over afterwards — the next state commit would fail."""
    promoted = box.promote({"promoted.txt": "p\n"})
    owner = os.stat(os.path.join(box.wt, ".git")).st_uid
    monkeypatch.setattr(gitio.os, "geteuid", lambda: owner + 1)
    assert gitio.sync_worktree(box.wt, box.bare, check=True)[0] == 3
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "runs as uid %d" % (owner + 1) in said and "uid %d's" % owner in said, said
    assert not _has(box.wt, promoted)


@pytest.mark.parametrize("box", NAMES, indirect=True)
@pytest.mark.parametrize("remote", ["origin", "upstream"])
def test_a_commit_already_on_another_remote_is_refused_not_given_a_new_id(box, remote):
    """Two commits of the operator's own since `main`, the first already on another remote (a remote-tracking ref,
    `refs/remotes/<remote>/main` — `origin`, or any other name — names it, as a push or a `git pull` leaves one),
    and a promote on `main`: `check`
    answers 3 (the worktree does not hold P yet); the real run brings P across and refuses — rc 1, `include 1
    already on another remote`, the remedy `git -C <worktree> merge <P>` — with HEAD and the branch as they were;
    `check` then answers 1 too. A remote-tracking ref of `local`'s is not another remote: with `origin`'s removed
    and `refs/remotes/local/proposed` naming HEAD, the sync brings the worktree forward. With a space and a quote
    in the paths, both commands the refusal prints (`branch -r --contains`, `merge`) quote the worktree for the
    shell. Guards a replay of published commits: each would get a new id, and the branch and its upstream would
    part (the next pull merges the old ids back in beside the new; the next push is refused) — whatever the remote
    is called; and the remedy pasted as printed (fold 8's own mutant G8-remote-branch-unquoted)."""
    published = _commit(box.wt, {"a.txt": "a\n"}, "pushed already")
    _git(box.wt, "update-ref", "refs/remotes/%s/main" % remote, published)
    mine = _commit(box.wt, {"b.txt": "b\n"}, "not pushed")
    promoted = box.promote({"promoted.txt": "p\n"})
    assert gitio.sync_worktree(box.wt, box.bare, check=True)[0] == 3
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "include 1 already on another remote" in said, said
    q = shlex.quote(box.wt)
    assert "`git -C %s merge %s`" % (q, promoted) in said and _state(box.wt)[1:] == (mine, "refs/heads/main")
    assert "(`git -C %s branch -r --contains <commit>` names it)" % q in said, said
    rc, said = gitio.sync_worktree(box.wt, box.bare, check=True)
    assert rc == 1 and "already on another remote" in said, said
    _git(box.wt, "update-ref", "-d", "refs/remotes/%s/main" % remote)
    _git(box.wt, "update-ref", "refs/remotes/local/proposed", mine)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward — HEAD is %s: 2 commit(s)" % _git(box.wt, "rev-parse",
                                                                                         "HEAD")), said


def test_a_repository_with_no_commit_is_refused(box, tmp_path):
    """A repository with no commit at HEAD (just `git init`), and a promote on the canonical: rc 1, `has no commit
    at HEAD`, with and without `check`. Guards the first read: a sync that went on from an empty HEAD would hand
    git an empty revision to compare, pack against and replay."""
    empty = str(tmp_path / "nocommit")
    subprocess.run(["git", "init", "-q", "-b", "main", empty], check=True, capture_output=True)
    box.promote({"promoted.txt": "p\n"})
    for check in (True, False):
        rc, said = gitio.sync_worktree(empty, box.bare, check=check)
        assert rc == 1 and "has no commit at HEAD" in said, (check, said)


def test_a_canonical_the_seam_will_not_read_is_refused(box):
    """The canonical holds a `commondir` file (its objects and refs would be read from somewhere else): rc 1,
    `could not be read`, with and without `check`. Guards the seam's refusal reaching the sync: a canonical that is
    not plainly itself is not read, and nothing is brought across from it."""
    box.promote({"promoted.txt": "p\n"})
    with open(os.path.join(box.bare, "commondir"), "w", encoding="ascii", newline="\n") as fh:
        fh.write("elsewhere\n")
    for check in (True, False):
        rc, said = gitio.sync_worktree(box.wt, box.bare, check=check)
        assert rc == 1 and "could not be read" in said, (check, said)


def test_a_symbolic_main_is_refused_and_nothing_comes_across(box):
    """The canonical's `refs/heads/main` is a symbolic ref to another branch, which names the promote: rc 1,
    `symbolic ref`, and the worktree does not hold the promote. Guards the read of `main` by its full name: `main`
    is a branch, and a sync that followed a symbolic ref would bring the worktree forward to what another ref
    names."""
    promoted = box.promote({"promoted.txt": "p\n"})
    _git(box.bare, "update-ref", "refs/heads/other", promoted)
    _git(box.bare, "symbolic-ref", "refs/heads/main", "refs/heads/other")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "symbolic ref" in said and not _has(box.wt, promoted), said


def test_the_pack_holds_only_what_the_worktree_lacks(box):
    """A promote of one file on `main`, and a commit of the operator's own (the worktree repacked first): the sync
    adds one pack to the worktree, and it holds the promote's commit, its tree and its blob — nothing else.
    Guards the offer: the worktree names its newest commits and the canonical leaves out what they reach. A pack of
    everything `main` reaches is as correct, and grows with the history on every deploy after a promote."""
    box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _git(box.wt, "repack", "-adq")
    before = _packs(box.wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0, said
    new = [p for p in _packs(box.wt) if p not in before]
    assert len(new) == 1 and _objects_in(new[0]) == 3, [(p, _objects_in(p)) for p in new]


def test_a_worktree_that_holds_main_already_reads_no_pack(box):
    """The worktree already holds the promote (the operator fetched it by hand) and has a commit of its own: the
    sync replays onto it and adds no pack. Guards the short cut: a sync that read a pack anyway would run the
    canonical's packer for nothing."""
    box.promote({"promoted.txt": "p\n"})
    _git(box.wt, "fetch", "-q", "local")
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _git(box.wt, "repack", "-adq")
    before = _packs(box.wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    assert _packs(box.wt) == before


def test_a_canonical_that_cannot_pack_its_main_is_named_and_nothing_moves(box):
    """The promote's blob is gone from the canonical (its loose object removed), so the canonical's packer fails:
    the sentence is exactly `the canonical could not pack its main; HEAD was not changed`, and the worktree's refs,
    HEAD and branch are as they were. Guards the attribution: the fault is the canonical's, not the worktree's
    refusal of the half pack that came across."""
    promoted = box.promote({"promoted.txt": "p\n"})
    blob = _git(box.bare, "rev-parse", promoted + ":promoted.txt")
    path = os.path.join(box.bare, "objects", blob[:2], blob[2:])
    os.chmod(path, 0o644)
    os.remove(path)
    before = _state(box.wt)
    assert gitio.sync_worktree(box.wt, box.bare) == (1, "the canonical could not pack its main; HEAD was not changed")
    assert _state(box.wt) == before


def test_a_promote_whose_objects_fsck_refuses_is_not_read(box, tmp_path):
    """A commit on `main` whose tree has an entry named `.git` (an fsck error, which `index-pack --strict`
    refuses), written to `main` directly, with no gate: rc 1, `refused the canonical's pack`, and the worktree does
    not hold it. Guards `--strict`: such a tree would write into the worktree's own git directory at checkout, and
    the strict reading is what stands between that `main` and the operator's checkout."""
    svc = str(tmp_path / "svcbad")
    subprocess.run(["git", "clone", "-q", box.bare, svc], check=True, capture_output=True)
    blob = subprocess.run(["git", "-C", svc, "hash-object", "-w", "--stdin"], input=b"x\n", capture_output=True,
                          check=True).stdout.decode().strip()
    listed = _git(svc, "ls-tree", "HEAD") + "\n100644 blob %s\t.git\n" % blob
    tree = subprocess.run(["git", "-C", svc, "mktree"], input=listed.encode(), capture_output=True,
                          check=True).stdout.decode().strip()
    bad = _git(svc, "commit-tree", tree, "-p", "HEAD", "-m", "bad")
    _git(svc, "push", "-q", "origin", bad + ":refs/heads/main")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "refused the canonical's pack" in said and not _has(box.wt, bad), said


def test_a_pack_larger_than_the_bound_is_the_worktrees_refusal(box, monkeypatch):
    """The bound on what a sync reads from the canonical (`_SYNC_MAX_PACK`, `index-pack --max-input-size`), here
    64 bytes, and a promote whose pack is far larger than the pipe between the two gits holds: rc 1, `refused the
    canonical's pack … larger than 64 bytes` — though the canonical's packer was cut off mid-write, which is not
    `the canonical could not pack its main` — HEAD is as it was and the worktree does not hold P. Guards the bound
    (a writer of `main` the gate never saw could otherwise fill the operator's disk through a deploy) and its
    sentence: a packer killed because its reader stopped is not the canonical's fault. The temporary pack the
    refused `index-pack` left is removed, and one that was there before (another git's) is not: a refused pack of
    up to the bound would otherwise stay in the operator's store until git's housekeeping."""
    monkeypatch.setattr(gitio, "_SYNC_MAX_PACK", 64)
    noise = "".join(hashlib.sha256(b"%d" % i).hexdigest() for i in range(8000))
    promoted = box.promote({"noise.txt": noise})
    pack_dir = os.path.join(box.wt, ".git", "objects", "pack")
    os.makedirs(pack_dir, exist_ok=True)
    with open(os.path.join(pack_dir, "tmp_pack_another"), "wb") as fh:
        fh.write(b"somebody else's")
    before = _state(box.wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "refused the canonical's pack" in said and "larger than 64 bytes" in said, said
    assert _state(box.wt) == before and not _has(box.wt, promoted)
    assert sorted(n for n in os.listdir(pack_dir) if n.startswith("tmp_")) == ["tmp_pack_another"]


def test_a_pack_without_main_is_not_whole(box, monkeypatch):
    """The canonical's packer (here replaced) sends a valid pack that does not hold `main`: rc 1, `is not whole`,
    and the worktree does not hold the promote. Guards the check after the pack is read: without it,
    `refs/remotes/local/main` would name a commit the worktree does not hold, and the replay would fail later with
    git's own sentence."""
    promoted = box.promote({"promoted.txt": "p\n"})

    def empty(gitdir, want, held):
        p = subprocess.Popen(["git", "pack-objects", "--revs", "--stdout", "-q"], cwd=box.wt, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p.stdin.close()
        return p
    monkeypatch.setattr(gitio, "_pack_in_canonical", empty)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "is not whole" in said and not _has(box.wt, promoted), said


def test_a_locked_remote_tracking_ref_refuses_before_the_replay(box):
    """A `refs/remotes/local/main.lock` that a killed git left: rc 1, `could not set refs/remotes/local/main`, and
    HEAD is as it was. Guards the order: the name the conflict's remedy uses (`rebase local/main`) is set before the
    replay, and a sync that could not set it does not replay."""
    box.promote({"promoted.txt": "p\n"})
    os.makedirs(os.path.join(box.wt, ".git", "refs", "remotes", "local"), exist_ok=True)
    open(os.path.join(box.wt, ".git", "refs", "remotes", "local", "main.lock"), "w").close()
    head = _git(box.wt, "rev-parse", "HEAD")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not set refs/remotes/local/main" in said and _git(box.wt, "rev-parse", "HEAD") == head


def test_an_abort_that_did_not_put_head_back_is_said(box, monkeypatch):
    """A replay stopped on a conflict, and the undo's `rebase --abort` (here made to report success and do
    nothing) left HEAD where the replay stopped: the sentence is `could not be put back` and names the commit HEAD
    was. Guards the check after the undo: "HEAD is as it was" here would send the operator to land a half-made
    replay."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real = gitio._wt

    def wt(worktree, argv, **kw):
        if argv[-2:] == ["rebase", "--abort"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return real(worktree, argv, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not be put back" in said and "it was %s" % mine in said, said


def test_an_abort_that_failed_is_said_though_head_is_back(box, monkeypatch):
    """The undo's `rebase --abort` puts HEAD back but exits non-zero: the sentence is `could not be put back`, and
    says HEAD is as it was but the replay was not undone — never `HEAD is H (it was H)`. Guards the abort's own
    status: a git that says it failed may have left a rebase directory or an index behind, and the operator must
    look before running again (read A-f3, N3)."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real = gitio._wt

    def wt(worktree, argv, **kw):
        got = real(worktree, argv, **kw)
        if argv[-2:] == ["rebase", "--abort"]:
            return subprocess.CompletedProcess(argv, 1, got.stdout, got.stderr)
        return got
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not be put back" in said, said
    assert "HEAD is %s as it was, but the replay was not undone" % mine in said and "(it was" not in said, said


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_a_replay_that_timed_out_and_an_abort_on_a_lock_say_both_and_name_the_lock(box, monkeypatch):
    """The replay stops on a conflict and then outlasts its bound (TimeoutExpired), and the undo's `rebase --abort`
    fails on the lock a git stopped part-way leaves (`fatal: Unable to create '…/index.lock': File exists.`): the
    sentence says the replay `did not finish in <bound> s`, carries the abort's reason, names `index.lock` in the
    worktree's own git directory to remove when no git is running, and gives `rebase --abort` and the HEAD to
    check, the lock and the worktree quoted for the shell (`shlex.quote`; with a space and a quote in the paths,
    each differs). Guards the remedy the operator follows: the sentence's own `rebase --abort` fails on that lock,
    and without the abort's reason and the lock's path the operator was told to run what had just failed (read
    A-f3, N3); and a path with a space, printed bare, is two words (fold 8's own mutant G8-undo-lock-unquoted), the
    status that shows HEAD too (read B-f8, N3: B9-undo-notundone-status-unquoted)."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real_run, real_wt = gitio.subprocess.run, gitio._wt
    lock = os.path.join(_git(box.wt, "rev-parse", "--path-format=absolute", "--git-dir"), "index.lock")

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            real_run(argv, *a, **kw)                        # the conflict: a rebase is in progress
            raise subprocess.TimeoutExpired(argv, gitio._LAND_SECONDS)
        return real_run(argv, *a, **kw)

    def wt(worktree, argv, **kw):
        if argv[-2:] == ["rebase", "--abort"]:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: Unable to create '%s': File exists.\n"
                                               % lock.encode())
        return real_wt(worktree, argv, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", run)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "did not finish in %d s, and the worktree could not be put back (fatal: Unable to create" \
        % gitio._LAND_SECONDS in said, said
    assert "If %s is there and no git is running, remove it" % shlex.quote(lock) in said, said
    assert "`git -C %s rebase --abort`, check that HEAD is %s" % (shlex.quote(box.wt), mine) in said, said
    assert "check that HEAD is %s (`git -C %s status`)" % (mine, shlex.quote(box.wt)) in said, said


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_a_replay_that_ends_off_main_is_refused(box, monkeypatch):
    """The replay exits 0, but HEAD is then not a descendant of `main` (here reset behind its back, as a hook of the
    operator's or a racing writer could): rc 1, `does not descend`, and the log to look at names the worktree quoted
    for the shell (with a space and a quote in the paths, `shlex.quote` differs). Guards the check after the replay:
    the sync must not say `brought forward` of a HEAD that is not; and the command pasted as printed (fold 8's own
    mutant G8-descend-log-unquoted)."""
    box.promote({"promoted.txt": "p\n"})
    mine = _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        got = real(argv, *a, **kw)
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            real(["git", "-C", box.wt, "reset", "-q", "--hard", mine], capture_output=True, check=True)
        return got
    monkeypatch.setattr(gitio.subprocess, "run", run)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "does not descend" in said, said
    assert "`git -C %s log --oneline -5`" % shlex.quote(box.wt) in said, said


def test_gits_own_reason_is_said_and_not_its_hint(box, monkeypatch):
    """A replay git refuses without a conflict and without starting (here the replay's own run answers rc 1 with an
    `error:` line and a `hint:` after it): the sentence ends with git's reason — `: error: the real reason; the
    replay was undone and HEAD and the tracked files are as they were.` — says nothing of the hint, and HEAD is as
    it was. Guards the sentence
    the deploy prints: git puts its hints after its reason, and a sentence made of git's last line named how to go
    on rather than what went wrong."""
    box.promote({"promoted.txt": "p\n"})
    mine = _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"error: the real reason\nhint: how to go on\n")
        return real(argv, *a, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", run)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and said.endswith(": error: the real reason; the replay was undone and HEAD and the tracked "
                                     "files are as they were."), said
    assert "hint" not in said and _state(box.wt)[1:] == (mine, "refs/heads/main")


def test_the_canonical_is_only_read_and_none_of_its_hooks_or_programs_run(box, tmp_path):
    """With a promote on `main`, an own commit to replay, and the canonical carrying hooks (in `hooks/` and in a
    `core.hooksPath` directory) and a `core.alternateRefsCommand` that each write their name to a file: the sync
    brings the worktree forward, and the canonical's files are byte for byte what they were — no ref, no object,
    no config of it changed — and the file is empty. The control beside it: a push into the same canonical runs
    the hooks. Guards the sync's canonical side: it runs none of the canonical's hooks or config-named programs, and
    writes only the worktree. (No hook runs for `cat-file` or `pack-objects` out of the seam either: that they run
    in it is held by the shallow row below and the seam pin.)"""
    box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    canary = str(tmp_path / "ran")
    for hooks in (os.path.join(box.bare, "hooks"), os.path.join(box.bare, "hooks-named")):
        os.makedirs(hooks, exist_ok=True)
        for name in ("update", "reference-transaction", "post-update", "pre-receive"):
            with open(os.path.join(hooks, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("#!/bin/sh\necho %s >> '%s'\n" % (name, canary.replace("\\", "/")))
            os.chmod(os.path.join(hooks, name), 0o755)
    _git(box.bare, "config", "core.hooksPath", os.path.join(box.bare, "hooks-named").replace("\\", "/"))
    _git(box.bare, "config", "core.alternateRefsCommand", "sh -c 'echo alternate >> %s'" % canary.replace("\\", "/"))
    before = _files(box.bare)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    assert _files(box.bare) == before
    assert not os.path.exists(canary), open(canary, encoding="utf-8").read()
    if shutil.which("sh"):
        _git(box.wt, "push", "-q", "local", "HEAD:refs/heads/proposed/" + RID)
        assert os.path.exists(canary), "control: a push must run the canonical's hooks, or this row shows nothing"


def test_a_shallow_file_in_the_canonical_does_not_cut_the_pack(box):
    """Two promotes on `main`, and a `shallow` file in the canonical naming the second (a canonical made shallow,
    by hand or by a writer of its files): the sync brings both across — the worktree holds the first. Guards the
    seam on the packing side, which the hooks row cannot show: the seam reads no shallow file, and out of it
    `pack-objects` would take `main` for a root and leave its parent out."""
    first = box.promote({"one.txt": "1\n"}, "p1")
    second = box.promote({"two.txt": "2\n"}, "p2")
    with open(os.path.join(box.bare, "shallow"), "w", encoding="ascii", newline="\n") as fh:
        fh.write(second + "\n")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward") and _has(box.wt, first), said


# ---- the command ------------------------------------------------------------------------------------------------ #

def test_the_command_syncs_checks_and_refuses_with_the_exit_statuses_the_doors_read(box, capsys):
    """`scripts/kontroll-land.py --sync`: in step exits 0 with `kontroll_run_id=… sync …` and `kontroll-land: in
    step`; after a promote, `--check` (the canonical taken from the `local` remote) exits 3 with `sync --check` and
    `would bring forward`, and the real run exits 0 with `brought forward`, then `what came in since <HEAD before>`
    — the promote's commit and its file, indented — and that the next deploy runs that tree's own playbooks and
    scripts as root; a conflict exits 1 with its sentence on
    stderr; `--sync` with `--propose` or `--skip-conflict-check` exits 1 and changes nothing. Guards the contract the
    play's two sync tasks and both update scripts read: 3 is "there is something to bring forward", and never 2,
    which the doors read as "refused by the gate"."""
    cli = _land_cli()
    assert cli.main(["--sync", "--worktree", box.wt, "--repo", box.bare]) == 0
    out = capsys.readouterr().out
    assert "kontroll_run_id=" in out and " sync %s <- refs/heads/main of" % os.path.abspath(box.wt) in out
    assert "kontroll-land: in step" in out
    promoted = box.promote({"promoted.txt": "p\n"})
    assert cli.main(["--sync", "--check", "--worktree", box.wt]) == 3
    out, err = capsys.readouterr()
    assert " sync --check " in out and "kontroll-land: would bring forward" in out and err == ""
    for extra in (["--propose", RID], ["--skip-conflict-check"]):
        assert cli.main(["--sync", "--worktree", box.wt, "--repo", box.bare] + extra) == 1
        assert "takes neither --propose nor --skip-conflict-check" in capsys.readouterr().err
    assert not _has(box.wt, promoted) and canon.read_ref(box.bare, canon.PROPOSED + RID) is None
    assert cli.main(["--sync", "--worktree", box.wt]) == 0
    out = capsys.readouterr().out
    assert "kontroll-land: brought forward" in out and "kontroll-land: what came in since %s:" % box.seed in out, out
    short = _git(box.wt, "rev-parse", "--short", promoted)
    assert "\n  %s svc: a promote\n" % short in out and "\n  A\tpromoted.txt\n" in out, out
    assert "runs this tree's own playbooks and scripts, as root" in out, out
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    assert cli.main(["--sync", "--worktree", box.wt]) == 1
    assert "kontroll-land: HEAD's commits do not replay" in capsys.readouterr().err


def test_the_commands_own_refusals_under_sync_say_nothing_was_changed(box, tmp_path, capsys):
    """`kontroll-land.py --sync` on a worktree with no `local` remote, and with `--repo` naming a URL: exit 1, and
    each sentence ends `Nothing was changed.` (not the landing's `Nothing was landed.`); the landing's own refusal
    still says `Nothing was landed.`. Guards the sentence the deploy and the update scripts show for a sync: a
    sync lands nothing whatever it does, so "nothing was landed" says nothing of whether the worktree moved."""
    cli = _land_cli()
    bare_wt = str(tmp_path / "noremote")
    subprocess.run(["git", "init", "-q", "-b", "main", bare_wt], check=True, capture_output=True)
    assert cli.main(["--sync", "--worktree", bare_wt]) == 1
    err = capsys.readouterr().err
    assert "has no `local` remote" in err and err.rstrip().endswith("Nothing was changed."), err
    assert cli.main(["--sync", "--worktree", box.wt, "--repo", "ssh://example.invalid/x.git"]) == 1
    err = capsys.readouterr().err
    assert "use no transport" in err and err.rstrip().endswith("Nothing was changed."), err
    assert cli.main(["--worktree", bare_wt]) == 1
    assert capsys.readouterr().err.rstrip().endswith("Nothing was landed.")


# ---- the mark: what a sync that brought the worktree forward leaves for the deploy's stop ------------------------ #

def _mark(wt):
    return os.path.join(wt, ".git", gitio.SYNC_MARK)


def test_a_sync_that_brought_the_worktree_forward_leaves_the_mark_and_a_second_keeps_its_first_line(box):
    """A `--check`, a sync in step and a refused sync leave no mark. A sync that brings the worktree forward appends
    HEAD before it to `gitio.SYNC_MARK` in the worktree's own git directory; a second promote, synced before any
    run stopped on the mark, appends the HEAD before that sync and keeps the first line — so `synced_since` names
    HEAD before the FIRST sync, and `came_in` since it lists both promotes, their files, and not the worktree's own
    replayed commit. Guards what the deploy's stop reads (ansible/playbooks/tasks/stop-after-sync.yml): a mark
    rewritten by the second sync would list only what the second brought, and the first promote would run
    unread; a mark left by a check or a refusal would stop a deploy that brought nothing in."""
    assert gitio.sync_worktree(box.wt, box.bare)[0] == 0 and not os.path.lexists(_mark(box.wt))
    first = box.promote({"p1.txt": "1\n"}, "the first promote")
    assert gitio.sync_worktree(box.wt, box.bare, check=True)[0] == 3 and not os.path.lexists(_mark(box.wt))
    mine = _commit(box.wt, {"mine.txt": "m\n"}, "the worktree's own")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward") and "(HEAD was %s)" % mine in said, said
    with open(_mark(box.wt), encoding="ascii") as fh:
        assert fh.read() == mine + "\n"
    second = box.promote({"p2.txt": "2\n"}, "the second promote")
    after_first = _git(box.wt, "rev-parse", "HEAD")
    assert gitio.sync_worktree(box.wt, box.bare)[0] == 0
    with open(_mark(box.wt), encoding="ascii") as fh:
        assert fh.read().splitlines() == [mine, after_first]
    assert gitio.synced_since(box.wt) == mine
    listing = gitio.came_in(box.wt, mine)
    for line in ("%s svc: the second promote" % _git(box.wt, "rev-parse", "--short", second),
                 "%s svc: the first promote" % _git(box.wt, "rev-parse", "--short", first), "A\tp1.txt", "A\tp2.txt"):
        assert line in listing, (line, listing)
    assert not any("the worktree's own" in ln for ln in listing) and "A\tmine.txt" not in listing, listing
    os.remove(_mark(box.wt))
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "a conflicting change")
    assert gitio.sync_worktree(box.wt, box.bare)[0] == 1 and not os.path.lexists(_mark(box.wt))


def test_a_mark_that_cannot_be_written_refuses_before_the_replay_and_head_is_as_it_was(box):
    """The mark's path is a directory: the sync cannot write the mark, which it writes before its replay — exit 1, a
    sentence that names the mark and says `could not be written` and `HEAD was not changed`, and HEAD and the branch
    are as they were. Guards the mark's place before the replay: written after it, a sync could bring the worktree
    forward and leave nothing for the next deploy to stop on, and a run killed in its replay left no mark at all
    (read A-f4, S1)."""
    os.mkdir(_mark(box.wt))
    box.promote({"promoted.txt": "p\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not be written" in said and said.endswith("HEAD was not changed"), said
    assert _mark(box.wt) in said and _state(box.wt)[1:] == (box.seed, "refs/heads/main"), said


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO (POSIX)")
def test_a_fifo_at_the_marks_path_refuses_at_once_and_head_is_as_it_was(box):
    """A FIFO at the mark's path, first with no reader, then with one holding it open: each sync returns within 30 s,
    exit 1, `could not be written`, HEAD and the branch as they were, and nothing is written into the FIFO. Guards
    the mark's open against waiting — opened without O_NONBLOCK, a FIFO's open waits for a reader for ever, and the
    sync, the deploy's with it, hangs (read A-f4, N3) — and the mark against being anything but a regular file: a
    FIFO with a reader opens, and HEAD written into it is a mark no stop can read."""
    os.mkfifo(_mark(box.wt))
    box.promote({"promoted.txt": "p\n"})
    for with_reader in (False, True):
        reader = os.open(_mark(box.wt), os.O_RDONLY | os.O_NONBLOCK) if with_reader else None
        got = {}
        t = threading.Thread(target=lambda: got.update(r=gitio.sync_worktree(box.wt, box.bare)), daemon=True)
        t.start()
        t.join(30)
        assert not t.is_alive(), "the sync waited on the FIFO at the mark's path"
        rc, said = got["r"]
        assert rc == 1 and "could not be written" in said, said
        assert _state(box.wt)[1:] == (box.seed, "refs/heads/main"), said
        if reader is not None:
            try:
                assert os.read(reader, 100) == b"", "HEAD was written into the FIFO"
            except BlockingIOError:
                pass
            finally:
                os.close(reader)


def test_a_refusal_before_the_replay_leaves_no_mark(box):
    """A sync that refuses before its replay (here: an ignored file of the operator's where main tracks a path)
    leaves no mark. Guards the mark written only once nothing is left to refuse: a mark from a sync that changed
    nothing stops the next deploy on a listing of nothing (read A-f5: R6-mark-on-clash)."""
    _commit(box.wt, {".gitignore": "p.txt\n"}, "ignore p.txt")
    with open(os.path.join(box.wt, "p.txt"), "w") as fh:
        fh.write("the operator's own, ignored\n")
    box.promote({"p.txt": "promoted\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "holds an ignored file" in said, said
    assert not os.path.lexists(_mark(box.wt)), "a sync that refused before its replay left a mark"


@pytest.mark.parametrize("there_before", [False, True])
def test_a_mark_whose_write_failed_is_removed_when_this_sync_made_it(box, monkeypatch, there_before):
    """The mark's open creates it, and the write of HEAD then fails (ENOSPC, as on a full disk): exit 1, `could not
    be written (No space left on device)`, HEAD as it was, and no mark is left. A hand sync's mark there before keeps
    its bytes. Guards the failed write: the empty mark it left stopped the next dry-run on "a sync brought it
    forward, or began to … git could not list it … HEAD before the sync: not known", though nothing had changed
    (read A-f5, N2); and the removal of a mark this sync did not make, which still names HEAD before a sync that
    brought the worktree forward."""
    if there_before:
        with open(_mark(box.wt), "w", encoding="ascii", newline="\n") as fh:
            fh.write(box.seed + "\n")
    box.promote({"p.txt": "p\n"})
    real_fdopen = gitio.os.fdopen

    class Full:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def fileno(self):
            return self.fh.fileno()

        def write(self, text):
            raise OSError(errno.ENOSPC, "No space left on device")

    def fdopen(fd, *a, **kw):
        fh = real_fdopen(fd, *a, **kw)
        return Full(fh) if "a" in (a[:1] or (kw.get("mode", ""),))[0] else fh
    with monkeypatch.context() as m:
        m.setattr(gitio.os, "fdopen", fdopen)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not be written (No space left on device)" in said, said
    assert _git(box.wt, "rev-parse", "HEAD") == box.seed, said
    if there_before:
        with open(_mark(box.wt), encoding="ascii") as fh:
            assert fh.read() == box.seed + "\n"
    else:
        assert not os.path.lexists(_mark(box.wt)), said


def test_a_sync_killed_in_its_replay_leaves_the_mark_and_what_came_in_can_be_listed(box, monkeypatch):
    """The replay stops on a conflict and the run is then interrupted before any undo (KeyboardInterrupt, as SIGINT
    raises it): a rebase is in progress, the mark is there with HEAD before the sync as its first line (`synced_since`),
    `came_in` from it names the promote's commit — what the next deploy's stop lists before its dry-run runs any script
    of the tree — and the next sync refuses, a rebase in progress, naming the abort, which puts HEAD back. Guards the
    mark written BEFORE the replay: written after it, a run killed in the replay left no mark, the next mandated dry-run
    ran the promote's scripts as root before it refused, and `rebase --continue`, as told, landed them unread (read
    A-f4, S1)."""
    promoted = box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            real(argv, *a, **kw)                            # the conflict: a rebase is in progress
            raise KeyboardInterrupt
        return real(argv, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(gitio.subprocess, "run", run)
        with pytest.raises(KeyboardInterrupt):
            gitio.sync_worktree(box.wt, box.bare)
    own = _git(box.wt, "rev-parse", "--path-format=absolute", "--git-dir")
    assert os.path.isdir(os.path.join(own, "rebase-merge")) and gitio.synced_since(box.wt) == mine
    listing = gitio.came_in(box.wt, mine)
    assert "%s svc: a promote" % _git(box.wt, "rev-parse", "--short", promoted) in listing, listing
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "rebase, merge, cherry-pick, revert or bisect is in progress" in said, said
    assert "`git -C %s rebase --abort` puts HEAD back where it was before the replay" % box.wt in said, said
    _git(box.wt, "rebase", "--abort")
    assert _git(box.wt, "rev-parse", "HEAD") == mine, "the abort the refusal names puts HEAD back"


@pytest.mark.parametrize("how", ["undone", "git not run", "undone, a mark there before", "HEAD elsewhere",
                                 "in progress", "a file flagged"])
def test_a_replay_undone_removes_the_mark_it_wrote_and_no_other(box, monkeypatch, how):
    """A replay stops on a conflict. Put back — HEAD back where it was, nothing in progress, the tracked files HEAD's
    and no untracked file the replay left — the mark this sync wrote is removed (no mark left), and so it is when git
    could not be started for the replay at all, nor after it, so that HEAD cannot be read back (OSError: `git could not
    be run`); a mark a hand sync left before keeps its first line. HEAD not back (the undo's `rebase --abort` followed
    by a reset to the seed) or an operation still in progress (a `BISECT_LOG` after the abort): the mark is left, HEAD
    before the sync its first line; and so it is when a tracked file is flagged assume-unchanged (one main does not
    change), and the conflict's sentence then says that git's status does not compare it, the listing that names it and
    the commands after it said to be bash's. Guards the mark written before the replay both ways: one left by a replay
    undone or never run stops the next deploy on nothing that came in, and one removed while HEAD is elsewhere, or a
    rebase still in progress, lets the next deploy run a tree no one listed (read A-f4, S1); and a flag that hides a
    tracked file from the status (read A-f6, S1: the conflict's arm of the sentence, which names the listing, the
    comparison and the put-back as read A-f8, N2 and N3 left them, and says they are bash's as read A-f11, N4 left
    it)."""
    if how == "undone, a mark there before":
        with open(_mark(box.wt), "w", encoding="ascii", newline="\n") as fh:
            fh.write(box.seed + "\n")
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    if how == "a file flagged":
        _git(box.wt, "update-index", "--assume-unchanged", "modules/_core.yml")
    own = _git(box.wt, "rev-parse", "--path-format=absolute", "--git-dir")
    real, real_run = gitio._wt, gitio.subprocess.run

    def wt(worktree, argv, **kw):
        got = real(worktree, argv, **kw)
        if argv[-2:] == ["rebase", "--abort"] and how == "HEAD elsewhere":
            _git(box.wt, "reset", "-q", "--soft", box.seed)
        if argv[-2:] == ["rebase", "--abort"] and how == "in progress":
            open(os.path.join(own, "BISECT_LOG"), "w").close()
        return got

    gone = []

    def run(argv, *a, **kw):     # git gone from the replay on: the rev-parse after it cannot answer either
        if isinstance(argv, list) and ("rebase" in argv and "--quiet" in argv or gone and argv[:1] == ["git"]):
            gone.append(argv)
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", "git")
        return real_run(argv, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(gitio, "_wt", wt)
        if how == "git not run":
            m.setattr(gitio.subprocess, "run", run)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1, said
    if how == "git not run":
        assert said.startswith("git could not be run in the worktree") and not os.path.lexists(_mark(box.wt)), said
        assert len(gone) > 1, gone       # the HEAD read after the replay was refused too: `not ran` removed it
        assert _git(box.wt, "rev-parse", "HEAD") == mine
    elif how == "undone":
        assert not os.path.lexists(_mark(box.wt)), said
    else:
        assert gitio.synced_since(box.wt) == (box.seed if how.startswith("undone") else mine), said
    if how == "a file flagged":
        assert "without a conflict" in said and said.endswith(
            "then run again. Git's status does not compare 1 tracked file(s) flagged skip-worktree or "
            "assume-unchanged, so it cannot say those are HEAD's: `git -C %s -c core.quotePath=true ls-files -v | grep "
            "-v '^H '` (this and the commands after it are bash's: `$'…'` and `<(…)`) names them (a name the "
            "listing prints in double quotes is quoted the C way: give it to the "
            "shell as `$'…'`, with what is between the quotes, each `'` in it written `\\'`). Compare each with main's "
            "(`git -C %s cat-file --filters refs/remotes/local/main:<path> | cmp - %s/<path>`; for a link, whose "
            "target `cmp` would read, its text: `git -C %s cat-file -p refs/remotes/local/main:<path> | cmp - "
            "<(readlink -nv -- %s/<path>)`); where one holds main's bytes or is missing, put HEAD's back (`git -C %s "
            "checkout --ignore-skip-worktree-bits HEAD -- <path>`) before running again."
            % ((shlex.quote(box.wt),) * 6)), said


@pytest.mark.parametrize("verdict", ["could not say", "git status fails", "tracked files differ",
                                     "a tracked file alone differs", "left over", "unsure what is left", "put back",
                                     "skip-worktree", "assume-unchanged", "flags not listed", "flags cannot be listed"])
def test_a_replay_the_undo_did_not_put_back_leaves_the_mark_it_wrote(box, monkeypatch, verdict):
    """The replay's checkout of `main` stops part-way (no rebase begun, nothing to abort), having written main's
    `config/fleet.yml` over the tracked one and a new `ansible/filter_plugins/zz.py` (in two rows the tracked file
    alone); HEAD is back where it was and nothing is in progress in every row. The undo's `git status` then outlasts its
    bound (it could not say) or fails with nothing on its output (the tracked file alone, so no untracked file is left
    to keep the mark), its reset fails (tracked files differ, with main's new file beside the tracked one and without
    it), main's new file holds bytes not main's (left, and named), or git cannot list the untracked files after it: the
    mark this sync wrote is LEFT, HEAD before the sync its first line. Put back — the tracked file reset, main's file
    removed — it is removed, and the sentence says HEAD and the tracked files are as they were. With `config/fleet.yml`
    flagged skip-worktree or assume-unchanged, main's bytes stay in it, unseen by the status (so no reset runs): the
    mark is LEFT, and the sentence says HEAD is as it was and that the status does not compare 1 tracked file (the
    listing that names it and the commands after it said to be bash's), never that the tracked files are as they were —
    and the remedy it names, run as printed, finds main's bytes in the file and puts HEAD's back; when git cannot list
    the flags (`ls-files -v` fails, or cannot be started), the mark is LEFT and the sentence says so. Guards the mark's
    removal on what the undo put back, not on HEAD alone: removed while main's files were still in the worktree, the
    next deploy's dry-run went on with no stop and imported the promote's new filter plugin as root (read A-f5, S1,
    measured with a real deploy); and the removal on a failed `git status`'s empty output, or on the untracked files
    alone, passed every other row (fold 6's mutants F6-putback-status-rc-off and -out-off); the first passed the failed
    status's own row too while main's new file was left beside the tracked one, which kept the mark by itself (fold 6's
    own probe); and removed with main's bytes in a flagged file (sparse checkout's skip-worktree, a local edit kept out
    of `status`), the next dry-run ran main's scripts as root with no stop, under a sentence saying the tracked files
    were as they were (read A-f6, S1, measured with a real deploy); and an `ls-files -v` that could not be started
    escaped the undo as a traceback, with no sentence (fold 7's mutant G7-flagged-except-uncaught); and the remedy said
    "check each against HEAD", where a plain checkout refuses a skip-worktree path (read B-f7, N4); the listing it names
    prints a name as git quotes it, and the sentence says how to give a quoted one back (read A-f8, N3; read A-f9, N2),
    and how to compare a link (N4)."""
    alone = verdict in ("git status fails", "a tracked file alone differs")   # no untracked file left to keep it
    box.promote({"config/fleet.yml": "enabled_modules: [alpha]  # promoted\n",
                 **({} if alone else {"ansible/filter_plugins/zz.py": "PROMOTED = 1\n"})})
    mine = _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    if verdict in ("skip-worktree", "assume-unchanged"):
        _git(box.wt, "update-index", "--" + verdict, "config/fleet.yml")
    with monkeypatch.context() as m:
        _stops_writing(m, box.wt, {"config/fleet.yml": b"enabled_modules: [alpha]  # promoted\n",
                                   **({} if alone else {"ansible/filter_plugins/zz.py": b"PROMOTED = 1\n"})})
        stopped, real_wt, real_untracked, replayed = gitio.subprocess.run, gitio._wt, gitio._untracked, []

        def run(argv, *a, **kw):
            if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
                replayed.append(argv)
            return stopped(argv, *a, **kw)

        def wt(worktree, argv, *a, **kw):
            if replayed and verdict == "could not say" and argv[-3:] == ["status", "--porcelain",
                                                                         "--untracked-files=no"]:
                raise subprocess.TimeoutExpired(argv, 60)
            if replayed and verdict == "git status fails" and argv[-3:] == ["status", "--porcelain",
                                                                            "--untracked-files=no"]:
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: unable to read the index\n")
            if replayed and verdict in ("tracked files differ", "a tracked file alone differs") and "reset" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"error: unable to write config/fleet.yml\n")
            if replayed and verdict == "flags not listed" and argv[-3:] == ["ls-files", "-v", "-z"]:
                return subprocess.CompletedProcess(argv, 128, b"", b"fatal: index file corrupt\n")
            if replayed and verdict == "flags cannot be listed" and argv[-3:] == ["ls-files", "-v", "-z"]:
                raise OSError(24, "Too many open files")
            return real_wt(worktree, argv, *a, **kw)

        def untracked(worktree):
            return None if replayed and verdict == "unsure what is left" else real_untracked(worktree)
        if verdict == "left over":
            m.setattr(gitio, "_blob_id", lambda path, algo: None)
        m.setattr(gitio.subprocess, "run", run)
        m.setattr(gitio, "_wt", wt)
        m.setattr(gitio, "_untracked", untracked)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and replayed and _git(box.wt, "rev-parse", "HEAD") == mine, said
    if verdict == "put back":
        assert not os.path.lexists(_mark(box.wt)), said
        assert "the replay was undone and HEAD and the tracked files are as they were." in said, said
    else:
        assert gitio.synced_since(box.wt) == mine, (verdict, said)
    if verdict in ("skip-worktree", "assume-unchanged"):
        with open(os.path.join(box.wt, "config", "fleet.yml"), encoding="ascii") as fh:
            assert fh.read() == "enabled_modules: [alpha]  # promoted\n", "main's bytes stay in the flagged file"
        assert "the replay was undone and HEAD is as it was." in said and (
            " Git's status does not compare 1 tracked file(s) flagged skip-worktree or assume-unchanged, so it cannot "
            "say those are HEAD's: `git -C %s -c core.quotePath=true ls-files -v | grep -v '^H '` (this and the "
            "commands after it are bash's: `$'…'` and `<(…)`) names them"
            % shlex.quote(box.wt)
            in said), said
        assert "tracked files are as they were" not in said, said
        commands = re.findall(r"`(git -C [^`]*<path>[^`]*)`", said)     # the remedy, run as printed
        show = next(c for c in commands if "| cmp - " in c)
        back = next(c for c in commands if " checkout " in c)
        rel = "config/fleet.yml"
        assert subprocess.run(["bash", "-c", show.replace("<path>", rel)], capture_output=True).returncode == 0, show
        p = subprocess.run(["bash", "-c", back.replace("<path>", rel)], capture_output=True)
        assert p.returncode == 0, (back, p.stderr)
        with open(os.path.join(box.wt, "config", "fleet.yml"), encoding="ascii") as fh:
            assert fh.read() == CLEAN["config/fleet.yml"], "HEAD's bytes are back"
    if verdict in ("flags not listed", "flags cannot be listed"):
        assert "the replay was undone and HEAD is as it was." in said and said.endswith(
            " Git could not list the tracked files its status does not compare (`git -C %s ls-files -v` failed), so "
            "it cannot say those are HEAD's." % shlex.quote(box.wt)), said


def test_two_flagged_files_are_counted_as_two_and_the_commands_quote_the_worktree(box, tmp_path):
    """A replay stopped on a conflict and undone, in a worktree at `<tmp>/w t` (a space in its path), with two tracked
    files flagged assume-unchanged: the sentence says the status does not compare `2 tracked file(s)`, and names the
    listing, the comparison with main's, the put-back and the conflict's rebase each with the worktree quoted for the
    shell — the listing quoting a name as git does by default whatever the operator's config says (`-c
    core.quotePath=true`), the comparison reading main's file as git's checkout writes it (`cat-file --filters`) and a
    link by its text (`readlink -nv`, in one command), the put-back where one holds main's bytes or is missing — and not
    that `core.ignoreStat` is set, though the row sets it, to false (fold 9's mutant G9-ignorestat-any-value, a read
    that took any value it found as true, passed while no row set it); the mark is left. Guards the count (read B-f7,
    N3: F8-flagged-count-any — every row flagged one file) and the quoting (read A-f7, N3: the undo's commands printed
    the worktree bare, two words for the shell); and the remedy's three blind spots: a path `ls-files` C-quotes, a file
    a filter or a line end converts, and a flagged file main deleted (read A-f8, N2 and N3); and a link, whose `cmp`
    read the file it points to (read A-f9, N4), and whose `readlink` ended its text in a newline git's blob does not
    hold (read A-f10, N4)."""
    wt = str(tmp_path / "w t")
    shutil.move(box.wt, wt)
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(wt, {"note.txt": "mine\n"}, "mine")
    for path in ("modules/_core.yml", "config/fleet.yml"):
        _git(wt, "update-index", "--assume-unchanged", path)
    _git(wt, "config", "core.ignoreStat", "false")
    rc, said = gitio.sync_worktree(wt, box.bare)
    q = shlex.quote(wt)
    assert rc == 1 and q != wt and "without a conflict" in said and "does not compare 2 tracked file(s)" in said, said
    assert "`git -C %s rebase local/main`" % q in said, said
    assert "`git -C %s -c core.quotePath=true ls-files -v | grep -v '^H '`" % q in said, said
    assert ("(`git -C %s cat-file --filters refs/remotes/local/main:<path> | cmp - %s/<path>`; for a link, whose "
            "target `cmp` would read, its text: `git -C %s cat-file -p refs/remotes/local/main:<path> | cmp - "
            "<(readlink -nv -- %s/<path>)`"
            % (q, q, q, q) in said), said
    assert "(`git -C %s checkout --ignore-skip-worktree-bits HEAD -- <path>`) before running again." % q in said, said
    assert "core.ignoreStat` is set" not in said, said
    assert gitio.synced_since(wt) == mine, said


def test_the_undo_says_core_ignorestat_flags_every_file_when_it_is_set(box):
    """A replay stopped on a conflict and undone in a worktree whose git has `core.ignoreStat` true: every index entry
    git writes is flagged assume-unchanged, and the sentence that counts the tracked files git's status does not
    compare says `core.ignoreStat` is set, which flags every file git writes; the row of two files flagged by hand
    (`test_two_flagged_files_are_counted_as_two…`) does not say it. Guards the count the operator reads: under
    `core.ignoreStat` it is every file of the tree, said as files flagged skip-worktree or assume-unchanged, though
    the operator flagged none (read A-f8, N3)."""
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    _git(box.wt, "config", "core.ignoreStat", "true")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "without a conflict" in said and "does not compare" in said, said
    assert "(`core.ignoreStat` is set, which flags every file git writes)" in said, said


@pytest.mark.skipif(os.name != "posix", reason="runs the printed compare with bash")
def test_the_compare_the_sentences_print_reads_mains_file_as_gits_checkout_writes_it(box):
    """A worktree whose own `info/attributes` checks `config/fleet.yml` out with CRLF line ends, the file holding the
    canonical's main's bytes as git's checkout writes them there: the compare the sentences print
    (`gitio._PUT_HEADS_BACK`'s `cat-file --filters … | cmp - …`, `<path>` filled in), run by bash, finds them the same
    (exit 0); git's `show` of main's file there differs from it (the premise). Guards the compare of a file a replay
    may have left main's bytes in: read raw, main's file differs from what checkout wrote whenever a filter or a line
    end converts it, so the compare said "not main's" and the put-back was skipped (read A-f8, N2)."""
    box.promote({"config/fleet.yml": "enabled_modules: [alpha, beta]\n"})
    _git(box.wt, "fetch", "-q", "local")
    os.makedirs(os.path.join(box.wt, ".git", "info"), exist_ok=True)
    with open(os.path.join(box.wt, ".git", "info", "attributes"), "w", encoding="ascii", newline="\n") as fh:
        fh.write("config/fleet.yml text eol=crlf\n")
    with open(os.path.join(box.wt, "config", "fleet.yml"), "wb") as fh:
        fh.write(b"enabled_modules: [alpha, beta]\r\n")
    q = shlex.quote(box.wt)
    said = gitio._PUT_HEADS_BACK.format(wt=q)
    compare = next(c for c in re.findall(r"`([^`]*)`", said) if "| cmp - " in c).replace("<path>", "config/fleet.yml")
    raw = "git -C %s show refs/remotes/local/main:config/fleet.yml | cmp -s - %s/config/fleet.yml" % (q, q)
    assert subprocess.run(["bash", "-c", raw], capture_output=True).returncode == 1, "main's file read raw differs"
    p = subprocess.run(["bash", "-c", compare], capture_output=True)
    assert p.returncode == 0, (compare, p.stdout, p.stderr)


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_flags_whose_listing_times_out_are_said_and_the_mark_kept(box, monkeypatch):
    """A replay stopped on a conflict and undone; `git ls-files -v` outlasts its bound (TimeoutExpired): exit 1 with
    the could-not-list sentence, its `git -C <worktree> ls-files -v` quoted for the shell, never a traceback, and the
    mark is left. Guards the undo's catch of a timeout, not of an OSError alone: the suite's rows made the listing
    fail, or fail to start, never time out (read B-f7, N3: F8-undo-flagged-except-oserror-only); and the command
    pasted as printed (read B-f8, N3: B9-unseen-none-unquoted)."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real = gitio._wt

    def wt(worktree, argv, *a, **kw):
        if list(argv) == ["ls-files", "-v", "-z"]:
            raise subprocess.TimeoutExpired("git ls-files", 60)
        return real(worktree, argv, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(gitio, "_wt", wt)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "Git could not list the tracked files its status does not compare" in said, said
    assert "(`git -C %s ls-files -v` failed)" % shlex.quote(box.wt) in said, said
    assert gitio.synced_since(box.wt) == mine, said


def test_a_case_variant_of_a_tracked_file_main_brought_is_found_whatever_core_ignorecase_says(box, monkeypatch,
                                                                                                tmp_path):
    """On a case-sensitive file system, a worktree with `core.ignoreCase=true` tracking
    `ansible/filter_plugins/kontroll_token.py`; main adds `Kontroll_token.py` beside it, and the replay's checkout stops
    part-way having written it with main's bytes: the undo finds it untracked, removes it (main holds it) and names it,
    and the operator's own file is untouched. Guards the undo's listing of what the replay left against the operator's
    `core.ignoreCase`: with it true, `ls-files --others` does not list a name a tracked one matches but for case, so the
    undo said "HEAD and the tracked files are as they were", removed the mark and left main's filter plugin, which the
    next dry-run imported as root (read A-f7, S2)."""
    probe = tmp_path / "case"
    probe.write_text("x")
    if (tmp_path / "CASE").exists():
        pytest.skip("a case-insensitive file system: the two names are one file")
    box.promote({"ansible/filter_plugins/Kontroll_token.py": "PROMOTED = 1\n"})
    _commit(box.wt, {"ansible/filter_plugins/kontroll_token.py": "MINE = 1\n"}, "mine")
    _git(box.wt, "config", "core.ignoreCase", "true")
    with monkeypatch.context() as m:
        _stops_writing(m, box.wt, {"ansible/filter_plugins/Kontroll_token.py": b"PROMOTED = 1\n"})
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "were removed (main holds them): ansible/filter_plugins/Kontroll_token.py" in said, said
    assert not os.path.lexists(os.path.join(box.wt, "ansible", "filter_plugins", "Kontroll_token.py")), said
    with open(os.path.join(box.wt, "ansible", "filter_plugins", "kontroll_token.py"), encoding="ascii") as fh:
        assert fh.read() == "MINE = 1\n"
    assert not os.path.lexists(_mark(box.wt)), said


def test_a_mark_that_is_no_longer_a_regular_file_after_a_failed_replay_is_left(box, monkeypatch, tmp_path):
    """The removal after a replay put back removes only a regular file: a mark that became a link while the replay
    ran is left for the operator, and its target is untouched. Guards the lstat before the unlink (read A-f5:
    R6-removal-unlink-any)."""
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    target = tmp_path / "target"
    target.write_text("t\n", encoding="ascii")
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        got = real(argv, *a, **kw)
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            os.remove(_mark(box.wt))
            os.symlink(str(target), _mark(box.wt))
        return got
    with monkeypatch.context() as m:
        m.setattr(gitio.subprocess, "run", run)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and os.path.islink(_mark(box.wt)) and target.read_text(encoding="ascii") == "t\n", said


def test_a_conflict_with_an_untracked_file_of_the_operators_there_before_removes_the_mark_it_wrote(box):
    """An untracked file of the operator's (`pre.txt`, not ignored, written after the commit, whose `add -A` would
    take it) is in the worktree before the sync; the replay stops on a conflict and is undone. Put back, with the file
    still there, the mark this sync wrote is removed. Guards the put-back's status reading tracked files only
    (`--untracked-files=no`): a status that lists untracked files keeps the mark after every failed sync in a
    worktree with any file of the operator's, and the next deploy stops on "a sync brought it forward, or began to"
    with HEAD where it was (reads A-f6 and B-f6: A7-putback-untracked-all, F7-putback-status-untracked)."""
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    with open(os.path.join(box.wt, "pre.txt"), "w", encoding="ascii") as fh:
        fh.write("mine, untracked\n")
    box.promote({"note.txt": "theirs\n"})
    assert _git(box.wt, "status", "--porcelain") == "?? pre.txt"
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "without a conflict" in said and os.path.isfile(os.path.join(box.wt, "pre.txt")), said
    assert not os.path.lexists(_mark(box.wt)), said


@pytest.mark.parametrize("how", ["times out", "cannot start"])
def test_a_put_back_status_that_fails_keeps_the_mark_and_is_a_refusal(box, monkeypatch, how):
    """A conflict, undone, HEAD back: the put-back's own `git status` (the third of the sync's: the tracked-change
    check, the undo's, the put-back's) outlasts its bound, or — every status from the abort on — cannot be started
    (OSError): exit 1 with a sentence, never an exception out of the sync, and the mark this sync wrote is LEFT, HEAD
    before the sync its first line. Guards the put-back's `except (OSError, subprocess.SubprocessError)`: either
    error escaping turns a refusal into a traceback, and the deploy's sync task says nothing a person can follow
    (reads A-f6 and B-f6: A7-putback-except-oserror, F7-putback-except-oserror-uncaught)."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real, seen, aborted = gitio._wt, [], []

    def wt(worktree, argv, *a, **kw):
        if list(argv) == ["status", "--porcelain", "--untracked-files=no"]:
            seen.append(1)
            if how == "times out" and len(seen) == 3:
                raise subprocess.TimeoutExpired("git status", 60)
            if how == "cannot start" and aborted:
                raise OSError(errno.EAGAIN, "Resource temporarily unavailable", "git")
        got = real(worktree, argv, *a, **kw)
        if argv[-2:] == ["rebase", "--abort"]:
            aborted.append(True)
        return got
    with monkeypatch.context() as m:
        m.setattr(gitio, "_wt", wt)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and aborted and _git(box.wt, "rev-parse", "HEAD") == mine, said
    assert len(seen) >= 3 if how == "times out" else True, seen
    assert gitio.synced_since(box.wt) == mine, said


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="a symlink (POSIX)")
def test_a_link_put_at_the_mark_after_the_sync_looked_is_left_by_the_failed_write(box, monkeypatch, tmp_path):
    """A link to a file of the operator's at the mark's path that the sync's look did not see (put there between the
    look and the open; here the look is told nothing is there): the open refuses it (O_NOFOLLOW), and the clean-up of
    a mark the failed write made leaves the link and its target. Guards the clean-up's `lstat` and regular-file test:
    without either it removes the link the operator put there, a race the other rows cannot reach (reads A-f6 and
    B-f6: F6-writefail-isreg-off, A7-writefail-stat)."""
    target = tmp_path / "elsewhere.txt"
    target.write_text("T\n", encoding="ascii")
    os.symlink(str(target), _mark(box.wt))
    box.promote({"p.txt": "p\n"})
    real = os.path.lexists
    with monkeypatch.context() as m:
        m.setattr(gitio.os.path, "lexists",
                  lambda path: False if os.path.basename(path) == gitio.SYNC_MARK else real(path))
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not be written" in said, said
    assert os.path.islink(_mark(box.wt)) and target.read_text(encoding="ascii") == "T\n"


@pytest.mark.skipif(os.name != "posix", reason="a directory whose name ends in a space")
def test_a_git_directory_whose_name_ends_in_a_space_holds_the_mark_its_readers_find(box, tmp_path):
    """A worktree whose git directory is `<tmp>/sep ` (a separate git directory, its name ending in a space), brought
    forward by a sync: the mark is in that directory, `_mark_path` names it there, `synced_since` names HEAD before
    the sync from it, and `clear_mark` removes it. Guards `_mark_path` keeping git's name whole: stripping every space
    names `<tmp>/sep/KONTROLL_SYNCED`, where the sync wrote nothing, and the play's own sync (`--stop-as`) then lists
    nothing and leaves the mark for the next run (reads A-f6 and B-f6: F6-markpath-strip, which fold 6 read as
    equivalent)."""
    sep, wt = str(tmp_path / "sep "), str(tmp_path / "wt2")
    subprocess.run(["git", "clone", "-q", "--separate-git-dir", sep, box.bare, wt], check=True, capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    box.promote({"p.txt": "p\n"})
    rc, said = gitio.sync_worktree(wt, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    assert os.path.isfile(os.path.join(sep, gitio.SYNC_MARK)), os.listdir(sep)
    assert gitio._mark_path(wt) == os.path.join(sep, gitio.SYNC_MARK)
    assert gitio.synced_since(wt) == box.seed
    assert gitio.clear_mark(wt) is None and not os.path.lexists(os.path.join(sep, gitio.SYNC_MARK))


def test_head_unreadable_after_the_abort_is_said_and_the_mark_kept(box, monkeypatch):
    """A replay stops on a conflict and is aborted; from the abort on, HEAD cannot be read (`rev-parse` raises):
    the sentence says `HEAD is unreadable (it was <H>)`, and the mark the sync wrote is LEFT (HEAD is not known to
    be back). Guards an unread HEAD taken as HEAD before — the undo then said HEAD and the tracked files are as they
    were — and the same in the mark's removal: the mark removed with HEAD unknown, so the next deploy may run a tree
    no one listed (read B-f5, S2: M6-undo-now-default-head, M6-back-except-head)."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real, aborted = gitio._wt, []

    def wt(worktree, argv, **kw):
        if aborted and argv == ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]:
            raise FileNotFoundError(2, "No such file or directory", "git")
        got = real(worktree, argv, **kw)
        if argv[-2:] == ["rebase", "--abort"]:
            aborted.append(True)
        return got
    with monkeypatch.context() as m:
        m.setattr(gitio, "_wt", wt)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and aborted and ("HEAD is unreadable (it was %s)" % mine) in said, said
    assert gitio.synced_since(box.wt) == mine, said


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="the mark is opened with O_NOFOLLOW (POSIX)")
def test_a_mark_that_is_a_link_is_neither_written_nor_read_through(box, tmp_path):
    """The mark is a symlink — to a file of the operator's that holds a commit id, then dangling: each sync exits 1
    before its replay, `could not be written`, HEAD as it was, the target keeps its bytes and the dangling one is not
    created; and `synced_since` reads nothing through the link (None), though its target holds a commit id. Guards
    the mark against a link in the operator's git directory: git's `--git-path` names a link's target, so the mark
    is named as the git directory and its name and opened with O_NOFOLLOW — a sync that appended to the target, or
    a stop that read it, would say "HEAD before" from a file no sync wrote."""
    target = tmp_path / "elsewhere.txt"
    target.write_text(box.seed + "\n", encoding="ascii")
    os.symlink(str(target), _mark(box.wt))
    box.promote({"promoted.txt": "p\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not be written" in said and _git(box.wt, "rev-parse", "HEAD") == box.seed, said
    assert target.read_text(encoding="ascii") == box.seed + "\n" and gitio.synced_since(box.wt) is None
    os.remove(_mark(box.wt))
    absent = tmp_path / "absent.txt"
    os.symlink(str(absent), _mark(box.wt))
    box.promote({"again.txt": "a\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not be written" in said and not os.path.lexists(str(absent)), said


def test_a_linked_worktrees_sync_leaves_its_mark_in_its_own_git_directory(box, tmp_path):
    """A linked worktree (`git worktree add`) brought forward: the mark is in ITS git directory — where the stop's
    `--git-dir` read looks — `synced_since` names its HEAD before, and the main checkout has no mark. Guards the
    mark's place: written to the common git directory, it is never seen by the stop of a deploy from the linked
    worktree, which then never stops after a hand sync (read B-f2, S3: MK5, SS4)."""
    linked = str(tmp_path / "linked")
    _git(box.wt, "worktree", "add", "-q", "-b", "lk", linked)
    box.promote({"p.txt": "p\n"})
    rc, said = gitio.sync_worktree(linked, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    own = _git(linked, "rev-parse", "--path-format=absolute", "--git-dir")
    assert os.path.isfile(os.path.join(own, gitio.SYNC_MARK)) and not os.path.lexists(_mark(box.wt))
    assert gitio.synced_since(linked) == box.seed


def test_a_mark_whose_first_line_is_not_a_commit_id_names_nothing(box):
    """A mark whose first line is not a commit id (`--output=x`): `synced_since` is None. Guards the first-line rule
    on the sync's side: the line goes into `git diff "$before" HEAD`, where a word starting with `--` is an option
    (read B-f2, S3: SS3)."""
    with open(_mark(box.wt), "w", encoding="ascii", newline="\n") as fh:
        fh.write("--output=x\n")
    assert gitio.synced_since(box.wt) is None


# ---- what fold 1's readers found surviving, and fold 2's ----------------------------------------------------- #

def test_the_bound_is_one_gib_and_above_the_gates_tree_bound():
    """`_SYNC_MAX_PACK` is 1 GiB — above the gate's `canon.MAX_TREE_BYTES`, as its comment says. Guards the
    shipped value both ways: the bound row patches it to 64 bytes, so neither a typo that refuses every real
    promote of a large file (1 << 16) nor a widened door (1 << 60) fails any other row."""
    assert gitio._SYNC_MAX_PACK == 1 << 30 > canon.MAX_TREE_BYTES


def test_an_index_pack_that_did_not_finish_is_said(box, monkeypatch):
    """The worktree's `index-pack` times out: rc 1, `did not start, or did not finish`, and HEAD, the refs and the
    branch are as they were. Guards the branch for a reader that never answered (without it, the sync read the
    status of a process it did not have)."""
    promoted = box.promote({"promoted.txt": "p\n"})
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "index-pack" in argv:
            raise subprocess.TimeoutExpired(argv, 1)
        return real(argv, *a, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", run)
    before = _state(box.wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "did not start, or did not finish" in said, said
    assert _state(box.wt) == before and not _has(box.wt, promoted)


def test_a_published_count_git_could_not_make_refuses(box, monkeypatch):
    """`rev-list --remotes` fails: rc 1, `some (or git could not tell)`. Guards the fail-closed half of the
    published check: a count git could not make is not "none published"."""
    box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    real = gitio._wt

    def wt(worktree, argv, *a, **kw):
        if "--remotes" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: x\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "some (or git could not tell)" in said, said


def test_an_operation_in_progress_in_a_linked_worktree_is_its_own_and_refuses(box, tmp_path):
    """A linked worktree stopped in an interactive rebase (`edit`): git keeps `rebase-merge` in THAT worktree's own
    git directory, not the common one. Its sync refuses (rc 1, in progress) and leaves the rebase as it was. Guards
    the busy names looked up under the worktree's own directory: under the common one the sync ran `git rebase`,
    which refused, and the undo's `rebase --abort` would have ended the operator's own rebase."""
    linked = str(tmp_path / "linked")
    _git(box.wt, "worktree", "add", "-q", "-b", "lk", linked)
    _commit(linked, {"one.txt": "1\n"}, "one")
    _commit(linked, {"two.txt": "2\n"}, "two")
    _git(linked, "rebase", "-i", "HEAD~1", env=dict(_WHO, GIT_SEQUENCE_EDITOR="sed -i 1s/^pick/edit/",
                                                    GIT_EDITOR="true"))
    own = _git(linked, "rev-parse", "--path-format=absolute", "--git-dir")
    assert os.path.isdir(os.path.join(own, "rebase-merge"))
    box.promote({"promoted.txt": "p\n"})
    head = _git(linked, "rev-parse", "HEAD")
    rc, said = gitio.sync_worktree(linked, box.bare)
    assert rc == 1 and "in progress" in said, said
    assert os.path.isdir(os.path.join(own, "rebase-merge")) and _git(linked, "rev-parse", "HEAD") == head


def test_a_replay_stopped_mid_checkout_puts_the_tracked_files_back_and_removes_what_git_wrote_of_mains(box,
                                                                                                    monkeypatch):
    """The replay's checkout of `main` stopped part-way, as git stops over a path it cannot write (`error: unable to
    create file …: Permission denied`, `error: could not detach HEAD` — no rebase begun, nothing to abort), having
    deleted a tracked file of the worktree's own (`mine.txt`) and written two of main's files: `promoted.txt` with
    main's bytes and `other.txt` with other bytes. An untracked file of the operator's from before (`notes.txt`, at a
    path main tracks, with main's bytes) and a new file at a path main does not track (`noise.log`) are there too.
    The undo puts `mine.txt` back (`reset --hard` HEAD), removes `promoted.txt` and names it, keeps `other.txt` and
    names it to be looked at, and keeps the other two without a word: what was there before the replay is not
    git's doing. The sentence carries git's FIRST error and says HEAD and the tracked files are as they
    were. Guards the undo's checks. Measured with real git as the operator over a directory of root's: the sync said
    `HEAD is as it was` with the file deleted, and the next deploy's state commit committed the deletion; main's
    files it left refused the next sync (`would be overwritten`) (read B-f1, B1; read B-f2, N4 and N6)."""
    box.promote({"promoted.txt": "p\n", "other.txt": "o\n", "notes.txt": "the operator's\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _write(box.wt, {"notes.txt": "the operator's\n"})
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            os.remove(os.path.join(box.wt, "mine.txt"))
            _write(box.wt, {"promoted.txt": "p\n", "other.txt": "changed\n", "noise.log": "x\n"})
            return subprocess.CompletedProcess(argv, 1, b"", b"error: unable to create file x: Permission denied\n"
                                                              b"error: could not detach HEAD\n")
        return real(argv, *a, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", run)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and _git(box.wt, "status", "--porcelain", "--untracked-files=no") == "", said
    assert (": error: unable to create file x: Permission denied; the replay was undone and HEAD and the tracked "
            "files are as they were. The files of the canonical's main that git wrote before it stopped were removed "
            "(main holds them): promoted.txt. Files of the canonical's main that git wrote before it stopped are "
            "left, untracked: other.txt (") in said, "git's FIRST error is the reason, not its last: " + said
    assert os.path.isfile(os.path.join(box.wt, "mine.txt")), "the deleted tracked file is put back"
    assert not os.path.exists(os.path.join(box.wt, "promoted.txt")), "main holds it"
    for kept in ("other.txt", "notes.txt", "noise.log"):
        assert os.path.isfile(os.path.join(box.wt, kept)), kept
    assert "notes.txt" not in said and "noise.log" not in said, said


def test_an_untracked_file_in_the_way_is_named_by_gits_reason_and_kept(box):
    """An untracked file of the operator's at a path the promote adds: the replay's checkout of `main` refuses
    (rc 1), the sentence carries git's reason with the path it names (`would be overwritten`, `incoming.txt`),
    HEAD is as it was and the file keeps the operator's bytes. Guards git's reason taken with the indented lines
    under it: git names the path on the line after `error:`, and a reason of the first line alone named nothing.
    Nothing of main's was written, so the sentence names no file git wrote: the one in the way is the operator's,
    untracked before the replay, which a list of what is untracked after it alone would name as main's (read B-f3,
    N5: M3-new-all). An ignored file there is the sync's own refusal, before the replay (git would overwrite it)."""
    mine = _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    box.promote({"incoming.txt": "from the promote\n"})
    with open(os.path.join(box.wt, "incoming.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("the operator's own, untracked\n")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "would be overwritten" in said and "incoming.txt" in said, said
    assert "that git wrote before it stopped" not in said, said
    assert _state(box.wt)[1:] == (mine, "refs/heads/main")
    with open(os.path.join(box.wt, "incoming.txt"), encoding="utf-8") as fh:
        assert fh.read() == "the operator's own, untracked\n"


@pytest.mark.skipif(os.name != "posix", reason="a hook runs only where git can execute it")
def test_no_hook_of_the_operators_hooks_path_runs_in_the_replay_even_one_the_promote_brings(box, tmp_path):
    """The operator's repository points `core.hooksPath` at a TRACKED directory, and a promote adds executable
    `post-checkout` and `post-rewrite` hooks there that write a marker; a commit of the operator's own is replayed
    onto it. The sync brings the worktree forward and no marker is written. Guards `core.hooksPath=/dev/null` on
    the replay: a hooks path into the tree would run the hooks a promote put there, as the caller — root's sync in
    the installer once, the operator's now — before anyone read what came in. (The sync's other gits, and the undo
    after a conflict or a checkout stopped part-way: the rows below.)"""
    _git(box.wt, "config", "core.hooksPath", "githooks")
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    marker = str(tmp_path / "hook-marker")
    svc = str(tmp_path / "svc-hooks")
    subprocess.run(["git", "clone", "-q", box.bare, svc], check=True, capture_output=True)
    os.makedirs(os.path.join(svc, "githooks"))
    for name in ("post-checkout", "post-rewrite"):
        path = os.path.join(svc, "githooks", name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\necho %s >> %s\n" % (name, marker))
        os.chmod(path, 0o755)
    _git(svc, "add", "-A", env=_SVC)
    _git(svc, "commit", "-q", "-m", "docs: a typo", env=_SVC)
    _git(svc, "push", "-q", "origin", "HEAD:refs/heads/main", env=_SVC)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    assert os.access(os.path.join(box.wt, "githooks", "post-checkout"), os.X_OK), "the hook came in executable"
    assert not os.path.exists(marker), open(marker, encoding="utf-8").read()


@pytest.mark.skipif(os.name != "posix", reason="a hook runs only where git can execute it")
@pytest.mark.parametrize("ending", ["brought forward", "a conflict undone"])
def test_no_hook_of_the_operators_runs_in_any_git_of_the_sync(box, tmp_path, ending):
    """The operator's `core.hooksPath` names a directory OUTSIDE the tree whose `post-index-change`,
    `reference-transaction`, `post-checkout` and `post-rewrite` hooks write a marker, and their `core.fsmonitor`
    names a program that writes one too. The sync brings the worktree forward, or stops on a conflict and undoes the
    replay. No marker is written. Guards `core.hooksPath=/dev/null` and `core.fsmonitor=false` in every git the sync
    runs in the operator's repository (`gitio._wt`) and in the replay: the status before the replay ran
    `post-index-change`, naming the canonical's main ran `reference-transaction`, and the undo's abort and status
    ran both. A hooks path into the tree names hooks a promote can bring, and after a checkout git stopped part-way
    they are on disk before anyone read them (read A-f2, N1, N2 and S1; read B-f2, S1)."""
    hooks = tmp_path / "ophooks"
    hooks.mkdir()
    marker = str(tmp_path / "marker")
    for name in ("post-index-change", "reference-transaction", "post-checkout", "post-rewrite"):
        (hooks / name).write_text("#!/bin/sh\necho %s >> %s\n" % (name, marker), encoding="ascii")
        os.chmod(str(hooks / name), 0o755)
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt" if ending == "a conflict undone" else "mine.txt": "mine\n"}, "mine")
    _git(box.wt, "config", "core.hooksPath", str(hooks))
    monitor = tmp_path / "fsmonitor"
    monitor.write_text("#!/bin/sh\necho fsmonitor >> %s\nexit 1\n" % marker, encoding="ascii")
    os.chmod(str(monitor), 0o755)
    _git(box.wt, "config", "core.fsmonitor", str(monitor))
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    if ending == "brought forward":
        assert rc == 0 and said.startswith("brought forward"), said
    else:
        assert rc == 1 and "without a conflict, in note.txt" in said, said
    assert not os.path.exists(marker), open(marker, encoding="ascii").read()


_ROOT_AND_SETPRIV = pytest.mark.skipif(
    not (hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("setpriv") and shutil.which("python3")),
    reason="needs root and setpriv: real git as the operator over a directory of root's")


def _stopped_mid_checkout_as_the_operator(hooks):
    """Real git, as the operator (uid 1000, `setpriv`), stopped part-way through the replay's checkout. A canonical
    and the operator's worktree under /tmp (pytest's tmp is root's alone); the operator's own commit (`mine.txt`)
    and an untracked file of theirs (`notes.txt`); a promote that adds `a.txt` and `zdir/new.txt`, where root's empty
    `zdir/` sits in the worktree. `hooks`: "tracked" — `core.hooksPath=githooks`, the operator's own
    `githooks/post-index-change` committed, and the promote changes it; "untracked" — the same path, and the
    promote adds the hook; "none" — no hooks path. Each hook writes its name to a marker. Runs
    `kontroll-land.py --sync` as the operator and returns (the process, the hooks that ran, `git status
    --porcelain --untracked-files=all` after)."""
    import tempfile
    base = tempfile.mkdtemp(prefix="sync-op-", dir="/tmp")
    try:
        os.chmod(base, 0o755)
        marker = os.path.join(base, "marker")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", **_WHO}
        op = ["setpriv", "--reuid=1000", "--regid=1000", "--clear-groups", "--"]

        def g(*args):
            p = subprocess.run(op + ["git"] + list(args), capture_output=True, env=env, cwd="/")
            assert p.returncode == 0, p.stderr

        def own(top):
            for where, dirs, names in os.walk(top):
                for n in [where] + [os.path.join(where, x) for x in dirs + names]:
                    os.lchown(n, 1000, 1000)
        bare, wt, svc = (os.path.join(base, n) for n in ("canon.git", "wt", "svc"))
        hook = "#!/bin/sh\necho %s >> " + marker + "\n"
        _write(wt, {"s.txt": "s\n"})
        if hooks == "tracked":
            _write(wt, {"githooks/post-index-change": hook % "operator-hook"})
            os.chmod(os.path.join(wt, "githooks", "post-index-change"), 0o755)
        own(base)
        g("init", "-q", "--bare", "-b", "main", bare)
        g("-C", wt, "init", "-q", "-b", "main")
        g("-C", wt, "add", "-A")
        g("-C", wt, "commit", "-q", "-m", "seed")
        g("-C", wt, "remote", "add", "local", bare)
        g("-C", wt, "push", "-q", "local", "HEAD:refs/heads/main")
        g("clone", "-q", bare, svc)
        _write(svc, {"a.txt": "a\n", "zdir/new.txt": "m\n"})
        if hooks != "none":
            _write(svc, {"githooks/post-index-change": hook % "PROMOTED-hook"})
            os.chmod(os.path.join(svc, "githooks", "post-index-change"), 0o755)
        own(svc)
        g("-C", svc, "add", "-A")
        g("-C", svc, "commit", "-q", "-m", "a promote: a.txt, zdir/new.txt (and a hook)")
        g("-C", svc, "push", "-q", "origin", "HEAD:main")
        _write(wt, {"mine.txt": "m\n"})
        own(wt)
        g("-C", wt, "add", "-A")
        g("-C", wt, "commit", "-q", "-m", "mine")
        if hooks != "none":
            g("-C", wt, "config", "core.hooksPath", "githooks")            # after the setup's own commits
        _write(wt, {"notes.txt": "the operator's, untracked\n"})
        own(wt)
        os.mkdir(os.path.join(wt, "zdir"))                                 # root's: the operator cannot write in it
        p = subprocess.run(op + ["python3", "-B", os.path.join(ROOT, "scripts", "kontroll-land.py"), "--sync",
                                 "--worktree", wt, "--repo", bare], capture_output=True, text=True, env=env, cwd=wt)
        ran = open(marker, encoding="ascii").read().split() if os.path.exists(marker) else []
        status = subprocess.run(op + ["git", "-C", wt, "-c", "core.hooksPath=/dev/null", "status", "--porcelain",
                                      "--untracked-files=all"], capture_output=True, text=True, env=env, cwd="/")
        return p, ran, status.stdout
    finally:
        shutil.rmtree(base, ignore_errors=True)


@_ROOT_AND_SETPRIV
@pytest.mark.parametrize("hooks", ["none", "tracked", "untracked"])
def test_a_real_checkout_stopped_part_way_is_put_back_and_no_hook_runs(hooks):
    """Real git, as the operator: the replay's checkout writes main's `a.txt` (and, with a hooks path into the tree,
    main's `githooks/post-index-change`), deletes `mine.txt`, and stops at root's `zdir/`. The sync exits 1 with
    git's `Permission denied`; no hook ran, main's or the operator's; `mine.txt` and the operator's hook are back;
    main's files git wrote are removed and named; the operator's untracked `notes.txt` is kept and not named; `git
    status` shows nothing but it. Guards the undo as real git leaves the worktree — the undo's `git status` ran the
    hook the checkout had just written, as the operator, before anyone read it, and main's files it left refused
    the next sync (read B-f2, S1, N4 and N6; UN1, UN3)."""
    p, ran, status = _stopped_mid_checkout_as_the_operator(hooks)
    assert p.returncode == 1 and "Permission denied" in p.stderr, p.stdout + p.stderr
    assert ran == [], (ran, p.stderr)
    removed = "a.txt, githooks/post-index-change" if hooks == "untracked" else "a.txt"
    assert "were removed (main holds them): %s." % removed in p.stderr, p.stderr
    assert "notes.txt" not in p.stderr and status.splitlines() == ["?? notes.txt"], (status, p.stderr)


@pytest.mark.skipif(os.name != "posix", reason="a symbolic link")
def test_a_link_git_wrote_from_main_is_named_and_kept(box, tmp_path, monkeypatch):
    """Main adds `promoted.txt`, `p2.txt` (both `p`) and a symbolic link to the first (`ln`), and the replay's
    checkout stopped part-way having written `promoted.txt` and `ln` as main has them and, at `p2.txt`, a link to
    `promoted.txt` — main's bytes, through a link: the undo removes `promoted.txt` and keeps `ln` and `p2.txt`,
    naming them to be looked at. Guards what the undo removes by itself: only a regular file whose bytes are main's,
    reached through no link — a hash read through a link would match main's bytes, and the unlink would take a link
    the operator put there."""
    svc = str(tmp_path / "svc-link")
    subprocess.run(["git", "clone", "-q", box.bare, svc], check=True, capture_output=True)
    _write(svc, {"promoted.txt": "p\n", "p2.txt": "p\n"})
    os.symlink("promoted.txt", os.path.join(svc, "ln"))
    _git(svc, "add", "-A", env=_SVC)
    _git(svc, "commit", "-q", "-m", "a promote with a link", env=_SVC)
    _git(svc, "push", "-q", "origin", "HEAD:refs/heads/main", env=_SVC)
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            _write(box.wt, {"promoted.txt": "p\n"})
            os.symlink("promoted.txt", os.path.join(box.wt, "ln"))
            os.symlink("promoted.txt", os.path.join(box.wt, "p2.txt"))
            return subprocess.CompletedProcess(argv, 1, b"", b"error: unable to create file x: Permission denied\n")
        return real(argv, *a, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", run)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "were removed (main holds them): promoted.txt." in said, said
    assert "are left, untracked: ln, p2.txt (a link" in said, said
    assert os.path.islink(os.path.join(box.wt, "ln")) and os.path.islink(os.path.join(box.wt, "p2.txt"))


@pytest.mark.skipif(os.name != "posix", reason="a file name with a newline in it")
def test_a_file_of_mains_whose_name_holds_a_newline_is_removed_and_named_escaped(box, monkeypatch):
    """Main adds `promoted.txt` and a file whose name holds a newline, and the replay's checkout stopped part-way
    having written both with main's bytes: the undo removes both, and names the odd one escaped (`odd\\nname.txt`),
    never with the newline itself. Guards the undo's hash taking each name whole (`_blob_id`, here, byte for byte):
    `hash-object --stdin-paths` read a name a line at a time, so a name with a newline was kept and named instead
    of removed (read A-f3, N4)."""
    odd = "odd\nname.txt"
    box.promote({"promoted.txt": "p\n", odd: "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {"promoted.txt": b"p\n", odd: b"p\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "were removed (main holds them): odd\\nname.txt, promoted.txt." in said, said
    assert "\n" not in said and not os.path.lexists(os.path.join(box.wt, odd)), said


@pytest.mark.skipif(os.name != "posix", reason="a file name with a carriage return, or a leading quote, in it")
@pytest.mark.parametrize("odd", ["cr\rname.txt", '"q.txt'])
def test_a_file_of_mains_whose_name_git_would_unquote_or_split_is_removed(box, monkeypatch, odd):
    """Main adds a file whose name holds a carriage return, or begins with a double quote, and the replay's
    checkout stopped part-way having written it with main's bytes: the undo removes it and names it (a CR escaped).
    Guards the same whole-name hash as the newline row: `hash-object --stdin-paths` dropped a trailing CR and
    C-unquoted a name that begins with `"`, hashing another path or none, so main's file was kept and the next
    sync refused over it (read A-f3, N4)."""
    box.promote({"promoted.txt": "p\n", odd: "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {"promoted.txt": b"p\n", odd: b"p\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "were removed (main holds them): " in said and "are left" not in said, said
    assert "\r" not in said and not os.path.lexists(os.path.join(box.wt, odd)), said


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_a_status_that_fails_after_the_undo_says_git_could_not_say_and_never_as_they_were(box, monkeypatch):
    """After a conflict is undone, the undo's `git status` fails (rc 128, `fatal: index file corrupt`): the sentence
    says HEAD is as it was but git could not say whether the tracked files are HEAD's, with git's reason, and never
    that the tracked files are as they were; the index to rebuild and the worktree are quoted for the shell (with
    a space and a quote in the paths, `shlex.quote` differs). Guards the status's own exit: a status that did not
    run must not be read as "nothing differs" (read B-f2, S3: UN7; N4); and the `rm` pasted as printed, which on a
    bare path with a space removes other files (fold 8's own mutant G8-undo-index-unquoted), and each status and the
    `reset --hard` beside it (read B-f8, N3: B9-undo-statusfail-first, -third, -reset-unquoted)."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real, seen = gitio._wt, {"rebase": False}

    def wt(worktree, argv, *a, **kw):
        if "rebase" in argv:
            seen["rebase"] = True
        if seen["rebase"] and "status" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: index file corrupt\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and ("git could not say whether the tracked files are HEAD's (`git status` failed: fatal: index "
                        "file corrupt)") in said, said
    assert "as they were" not in said, said
    index = os.path.join(_git(box.wt, "rev-parse", "--path-format=absolute", "--git-dir"), "index")
    assert "`rm %s && git -C %s reset`" % (shlex.quote(index), shlex.quote(box.wt)) in said, said
    q = shlex.quote(box.wt)
    assert "If `git -C %s status` fails too (a damaged index)" % q in said, said
    assert "then run `git -C %s status`, keep what you need, `git -C %s reset --hard %s`" % (q, q, mine) in said, said


def test_a_worktree_whose_untracked_files_git_cannot_list_is_refused_before_the_replay(box, monkeypatch):
    """`git ls-files --others` fails in the worktree (rc 128): the sync exits 1 — `could not list the untracked
    files`, `HEAD was not changed` — before the replay, and HEAD is as it was. Guards the list the undo goes by:
    without it, the undo could not tell a file git wrote from main from one that was there before."""
    box.promote({"promoted.txt": "p\n"})
    mine = _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    real = gitio._wt

    def wt(worktree, argv, *a, **kw):
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: x\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not list the untracked files" in said and "HEAD was not changed" in said, said
    assert _state(box.wt)[1:] == (mine, "refs/heads/main")


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_an_undo_that_cannot_list_mains_tree_says_so_and_removes_nothing(box, monkeypatch):
    """The replay's checkout stopped part-way having written main's `promoted.txt`, and `git ls-tree` of main fails in
    the undo: the sentence says git could not say which untracked files the replay left, and to remove main's before the
    next sync, naming the status that lists them with the worktree quoted for the shell; the file is kept. The status is
    case for case and lists each untracked file, ignored ones too (`-c core.ignoreCase=false status
    --untracked-files=all --ignored`). Guards the undo's own doubt: a list it could not make is said, not taken as "none
    left"; the status pasted as printed (fold 8's own mutant G8-clear-unsure-unquoted); and a plain status, which under
    `core.ignoreCase` true hides a file of main's whose name differs from a tracked one's only in case (read A-f8, S1:
    the sentences the read named; this one, fold 9's own find); and one without `--ignored`, which hides a file of
    main's at a path the operator's ignore rules name, though the undo's own list holds it (read B-f9, N4)."""
    box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    real_run, real_wt = gitio.subprocess.run, gitio._wt

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            _write(box.wt, {"promoted.txt": "p\n"})
            return subprocess.CompletedProcess(argv, 1, b"", b"error: unable to create file x: Permission denied\n")
        return real_run(argv, *a, **kw)

    def wt(worktree, argv, *a, **kw):
        if "ls-tree" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: x\n")
        return real_wt(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", run)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "Git could not say which untracked files the replay left" in said, said
    assert ("`git -C %s -c core.ignoreCase=false status --untracked-files=all --ignored` lists them"
            % shlex.quote(box.wt) in said), said
    assert os.path.isfile(os.path.join(box.wt, "promoted.txt"))


def test_gits_reason_takes_five_indented_lines_a_count_a_fatal_after_a_warning_and_skips_hints():
    """`_git_reason`: an `error:` line with seven indented paths gives the line, five paths and `and 2 more`; a
    `fatal:` line after a `warning:` is the reason, with its indented line; space-indented lines are taken as
    tab-indented ones; a `hint:` is skipped for the first plain line; with no `error:` or `fatal:`, the FIRST plain
    line, not the last. Guards the reason the sentence carries: git names the paths it refused on the lines under
    its error, and puts its hints and its `Aborting` after it (read B-f2, S3: GR1-GR6)."""
    seven = (b"error: The following untracked working tree files would be overwritten by checkout:\n"
             + b"".join(b"\td/%d\n" % i for i in range(1, 8))
             + b"Please move or remove them before you switch branches.\nAborting\n")
    assert gitio._git_reason(seven) == ("error: The following untracked working tree files would be overwritten by "
                                        "checkout: d/1 d/2 d/3 d/4 d/5 and 2 more")
    assert gitio._git_reason(b"warning: w\nfatal: f\n\tpath\n") == "fatal: f path"
    assert gitio._git_reason(b"error: e\n  spaced\n") == "error: e spaced"
    assert gitio._git_reason(b"hint: h\nplain\n") == "plain"
    assert gitio._git_reason(b"first\nsecond\n") == "first"


def test_the_check_prints_no_listing_and_a_sync_that_brought_it_forward_says_to_read_it(box, capsys):
    """`--sync --check` after a promote exits 3 and prints no `what came in`; `--sync` then exits 0 and its last
    line says `kontroll-land: read it now`. Guards what each says: the check changes nothing and has nothing to
    list, and the sync's listing is for a human to read before the next deploy runs that tree (read B-f2, S3: KL4,
    KL5)."""
    cli = _land_cli()
    box.promote({"promoted.txt": "p\n"})
    assert cli.main(["--sync", "--check", "--worktree", box.wt, "--repo", box.bare]) == 3
    assert "what came in" not in capsys.readouterr().out
    assert cli.main(["--sync", "--worktree", box.wt, "--repo", box.bare]) == 0
    assert "kontroll-land: read it now" in capsys.readouterr().out


# ---- a play's own sync: `--stop-as <play>` (read A-f3, B1) --------------------------------------------------------- #

@pytest.mark.parametrize("argv", [
    ["--stop-as", "local-canonical"], ["--sync", "--check", "--stop-as", "local-canonical"],
    ["--sync", "--stop-as", "Local Canonical"], ["--sync", "--stop-as=-x"], ["--sync", "--stop-as", "a" * 41]])
def test_stop_as_takes_a_plays_name_and_sync_without_check(box, capsys, argv):
    """`--stop-as` without `--sync`, with `--check`, or naming no play (upper case first, a leading dash, more than
    forty characters; the next row holds the rest of the name's pattern): exit 1, `--stop-as names the play`, nothing
    brought forward. Guards the switch to its one use, a play's own sync: its exit 4 fails the play's task, and its
    name opens the play's stop."""
    promoted = box.promote({"promoted.txt": "p\n"})
    assert _land_cli().main(argv + ["--worktree", box.wt, "--repo", box.bare]) == 1
    assert "--stop-as names the play whose own sync this is" in capsys.readouterr().err
    assert not _has(box.wt, promoted) and not os.path.lexists(_mark(box.wt))


@pytest.mark.parametrize("name, ok", [("a" * 40, True), ("local canonical", False), ("local_canonical", False),
                                      ("1play", False), ("play.yml", False)])
def test_stop_as_takes_forty_characters_and_nothing_but_letters_digits_and_dashes(box, capsys, name, ok):
    """`--stop-as` naming a play of exactly forty characters is taken (in step: exit 0); a name with a space, an
    underscore or a dot after a good first letter, or a digit first, is refused (exit 1, `--stop-as names the
    play`). Guards the name's pattern whole — its upper bound, its tail and its first character: the row above
    refuses names on their FIRST character or their length only, and the name opens every line of the play's stop
    and refusal (read B-f4, S1: M5-stopas-len38, -tail-any, -first-digit)."""
    rc = _land_cli().main(["--sync", "--stop-as", name, "--worktree", box.wt, "--repo", box.bare])
    err = capsys.readouterr().err
    assert (rc == 0 and "--stop-as names" not in err) if ok else (rc == 1 and "--stop-as names the play" in err), err


def _cli_with(monkeypatch, sync, calls=None):
    """kontroll-land's module with `gitio.sync_worktree` answering `sync`; `calls`, when given, records the order of
    `came_in` and `clear_mark` (each answering as a real one could)."""
    cli = _land_cli()
    monkeypatch.setattr(gitio, "sync_worktree", lambda wt, canonical, check=False: sync)
    if calls is not None:
        monkeypatch.setattr(gitio, "came_in", lambda wt, before: calls.append("came_in") or ["x"])
        monkeypatch.setattr(gitio, "clear_mark", lambda wt: calls.append("clear_mark") or None)
    return cli


@pytest.mark.parametrize("said", ["x went wrong.", "x went wrong"])
def test_a_plays_refusal_opens_with_the_play_and_has_one_period_between_the_sentences(box, capsys, monkeypatch,
                                                                                         said):
    """A play's own sync refused: a line opens `local-canonical: REFUSING to land — `, and the sync's sentence and
    `Nothing was landed` are joined by exactly one period, whether the sentence ends with one (a conflict whose
    main's files were left ends `… writes them again.`) or not. Guards the refusal as troubleshooting §10 quotes
    it: the refusal row asserts the pieces only, so neither `..`, a missing period nor a `kontroll-land: ` before the
    play's name fails it (read B-f4, S1: M5-refuse-period-always, -never, M5-refuse-prefixed)."""
    cli = _cli_with(monkeypatch, (1, said))
    assert cli.main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 1
    out = capsys.readouterr().out
    assert "x went wrong. Nothing was landed" in out and ".." not in out, out
    assert any(line.startswith("local-canonical: REFUSING to land — ") for line in out.splitlines()), out


def test_a_plays_own_sync_with_no_head_before_says_git_could_not_list_it(box, capsys, monkeypatch):
    """Brought forward with no HEAD before known (no mark to read, HEAD unreadable before the sync): what came in is
    `git could not list it`, and HEAD before `not known`. Guards the stop against reading as if nothing came in: a
    listing left empty there says nothing at all (read B-f4, S1: M5-came-in-fallback-empty)."""
    cli = _cli_with(monkeypatch, (0, "brought forward — HEAD is %s" % ("b" * 40)))
    monkeypatch.setattr(gitio, "synced_since", lambda wt: None)
    monkeypatch.setattr(gitio, "head_id", lambda wt: None)
    monkeypatch.setattr(gitio, "clear_mark", lambda wt: None)
    assert cli.main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 4
    out = capsys.readouterr().out
    assert "What came in:\n  git could not list it\n" in out and "HEAD before the sync: not known;" in out, out


def test_no_head_before_and_a_mark_git_cannot_list_from_says_git_could_not_list_it(box, capsys, monkeypatch):
    """A play's own sync brought the worktree forward; the mark's first line is a commit the worktree lacks and HEAD
    before the sync could not be read (None): exit 4, `What came in since <that id>:` and `git could not list it`,
    `HEAD before the sync: <that id>`. Guards the fallback taken with no HEAD before: `came_in(worktree, None)`
    raised TypeError, and the play's stop ended in a traceback, the mark not removed and nothing said (read B-f5,
    S2: M6-n4-no-headbefore-guard)."""
    gone = "1" * 40
    cli = _land_cli()
    monkeypatch.setattr(gitio, "sync_worktree", lambda wt, canonical, check=False: (0, "brought forward — HEAD is x"))
    monkeypatch.setattr(gitio, "synced_since", lambda wt: gone)
    monkeypatch.setattr(gitio, "head_id", lambda wt: None)
    monkeypatch.setattr(gitio, "clear_mark", lambda wt: None)
    assert cli.main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 4
    out = capsys.readouterr().out
    assert "What came in since %s:\n  git could not list it\n" % gone in out, out
    assert "HEAD before the sync: %s;" % gone in out, out


def test_the_mark_is_removed_only_after_what_came_in_was_said(box, monkeypatch):
    """A play's own sync that brought the worktree forward lists what came in, THEN removes the mark. Guards the mark
    as the next run's stop: the sync wrote it before its replay so that a run killed later is stopped on by the
    next; removed before the listing, a run killed while listing (or whose listing raised) leaves no mark, and the
    next run goes on with nothing said (read B-f4, S1: M5-clear-before-listing)."""
    calls = []
    cli = _cli_with(monkeypatch, (0, "brought forward — HEAD is %s" % ("b" * 40)), calls)
    monkeypatch.setattr(gitio, "synced_since", lambda wt: box.seed)
    assert cli.main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 4
    assert calls == ["came_in", "clear_mark"], calls


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_a_plays_own_sync_in_step_exits_0_and_brought_forward_stops_the_run_with_exit_4(box, capsys):
    """`--sync --stop-as local-canonical`: in step, exit 0 and `kontroll-land: in step`. After a promote: exit 4,
    and on standard output (nothing on standard error) the run id line, `kontroll-land: brought forward`,
    `local-canonical: STOPPED after bringing the worktree forward to the canonical's main. What came in since <HEAD
    before>:`, the promote's commit and file, `The mark is removed, so the next run goes on.`, what the next run
    runs, and `HEAD before the sync: <HEAD before>`, then the reflog to read (`git -C <worktree> reflog`, the worktree
    quoted for the shell); the mark is gone, so the next run is in step (exit 0). Run also with a space and a quote
    in both paths. Guards the play's stop made the sync's own last step: a stop in a later task ran after Ansible had
    forked a worker that imported the tree's filter plugins, as root (read A-f3, B1); and the reflog it names, which
    printed the worktree bare (two words at a space) where the play's stop quotes it (fold 9, found beside read
    A-f8)."""
    cli = _land_cli()
    stop = ["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]
    assert cli.main(stop) == 0 and "kontroll-land: in step" in capsys.readouterr().out
    promoted = box.promote({"promoted.txt": "p\n"})
    assert cli.main(stop) == 4
    out, err = capsys.readouterr()
    assert err == "" and "kontroll_run_id=" in out and "kontroll-land: brought forward" in out, out
    assert ("local-canonical: STOPPED after bringing the worktree forward to the canonical's main. What came in since "
            "%s:\n" % box.seed) in out, out
    short = _git(box.wt, "rev-parse", "--short", promoted)
    assert "\n  %s svc: a promote\n" % short in out and "\n  A\tpromoted.txt\n" in out, out
    assert "The mark is removed, so the next run goes on. Nothing was landed or deployed" in out, out
    assert "runs that tree's own playbooks and scripts as root" in out and "HEAD before the sync: %s;" % box.seed in out
    assert "; and in `git -C %s reflog`." % shlex.quote(box.wt) in out, out
    assert not os.path.lexists(_mark(box.wt))
    assert cli.main(stop) == 0 and "kontroll-land: in step" in capsys.readouterr().out


def test_a_plays_own_sync_names_head_before_when_the_marks_first_line_is_not_an_id(box, capsys):
    """A mark left whose first line is not a commit id (`--output=x`), then a play's own sync that brings the worktree
    forward: what came in is listed since HEAD as read before the sync, and the mark is removed. Guards the fallback:
    a mark that names nothing must not leave the stop with nothing to list."""
    with open(_mark(box.wt), "w", encoding="ascii", newline="\n") as fh:
        fh.write("--output=x\n")
    box.promote({"promoted.txt": "p\n"})
    assert _land_cli().main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 4
    out = capsys.readouterr().out
    assert "What came in since %s:\n" % box.seed in out and "\n  A\tpromoted.txt\n" in out, out
    assert not os.path.lexists(_mark(box.wt))


def test_a_plays_own_sync_lists_since_head_before_when_git_cannot_list_from_the_marks_first_line(box, capsys):
    """A mark left whose first line is a commit id the worktree does not hold, then a play's own sync that brings the
    worktree forward: exit 4, what came in is listed since HEAD as read before the sync — the promote's file — and
    never `git could not list it`, and the mark is removed. Guards the stop's listing when git cannot list from the
    mark (a commit since pruned, or another clone's): this run removes the mark, so a listing it did not make was
    never made, by it or by any run after (read A-f4, N4)."""
    with open(_mark(box.wt), "w", encoding="ascii", newline="\n") as fh:
        fh.write("1" * 40 + "\n")
    box.promote({"promoted.txt": "p\n"})
    assert _land_cli().main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 4
    out = capsys.readouterr().out
    assert "What came in since %s:\n" % box.seed in out and "\n  A\tpromoted.txt\n" in out, out
    assert "git could not list it" not in out and not os.path.lexists(_mark(box.wt)), out


def test_a_plays_own_sync_lists_what_came_in_since_the_mark_a_plain_sync_left(box, capsys):
    """A plain `--sync` brings the worktree forward and leaves the mark (no run stopped on it); then a play's own sync
    brings it forward again: exit 4, and what came in is listed since the mark's first line, the HEAD before the
    first sync — both promotes' files, and `HEAD before the sync: <that HEAD>`. Guards the stop reading the mark,
    not HEAD as read before its own sync: from HEAD it would list the second promote alone, and the first, which no
    run has stopped on, would never be said (fold 4's own mutant, F4-before-head-only)."""
    cli = _land_cli()
    box.promote({"first.txt": "1\n"})
    assert cli.main(["--sync", "--worktree", box.wt, "--repo", box.bare]) == 0 and os.path.isfile(_mark(box.wt))
    box.promote({"second.txt": "2\n"})
    capsys.readouterr()
    assert cli.main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 4
    out = capsys.readouterr().out
    assert "What came in since %s:\n" % box.seed in out, out
    assert "\n  A\tfirst.txt\n" in out and "\n  A\tsecond.txt\n" in out, out
    assert "HEAD before the sync: %s;" % box.seed in out and not os.path.lexists(_mark(box.wt)), out


def test_a_plays_own_sync_says_a_mark_it_could_not_remove(box, capsys, monkeypatch):
    """`clear_mark` gives a reason (here `…: Permission denied`): exit 4 still, and the output says `The mark was NOT
    removed (…): remove it by hand, as yourself, or the next run stops here again.` Guards the one way the next run
    stops again without a promote: it must be said, with what to do."""
    monkeypatch.setattr(gitio, "clear_mark", lambda wt: "/x/KONTROLL_SYNCED: Permission denied")
    box.promote({"promoted.txt": "p\n"})
    assert _land_cli().main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 4
    out = capsys.readouterr().out
    assert ("The mark was NOT removed (/x/KONTROLL_SYNCED: Permission denied): remove it by hand, as yourself, or the "
            "next run stops here again.") in out, out


def test_a_plays_own_sync_that_is_refused_says_it_as_the_plays_and_exits_1(box, capsys):
    """A conflict under `--sync --stop-as local-canonical`: exit 1, and on standard output `local-canonical: REFUSING
    to land — the sync of the worktree to the canonical's main exited 1: HEAD's commits do not replay …`, then
    `Nothing was landed` and `Do what the sentence says in <worktree>, then run again.`; no mark. Guards the
    refusal the play's failed task prints: the play no longer has a task of its own after the sync to say it."""
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    assert _land_cli().main(["--sync", "--stop-as", "local-canonical", "--worktree", box.wt, "--repo", box.bare]) == 1
    out, err = capsys.readouterr()
    assert err == "" and ("local-canonical: REFUSING to land — the sync of the worktree to the canonical's main "
                          "exited 1: HEAD's commits do not replay") in out, out
    assert "Nothing was landed" in out and "Do what the sentence says in %s, then run again." % box.wt in out, out
    assert not os.path.lexists(_mark(box.wt))


def _onto_main(wt):
    """Name HEAD as the canonical's main in the worktree, as a sync leaves it."""
    _git(wt, "update-ref", "refs/remotes/local/main", _git(wt, "rev-parse", "HEAD"))


def test_a_diff_git_could_not_make_lists_nothing(box, monkeypatch):
    """The commits since HEAD before could be listed but `git diff` failed (rc 128): `came_in` says `git could not
    list it` and nothing else. Guards the listing as a whole: a list of commits with the paths missing is not
    presented as what came in (read B-f2, S3: CI5)."""
    before = box.seed
    _commit(box.wt, {"f.txt": "x\n"}, "c")
    _onto_main(box.wt)
    real = gitio._wt

    def wt(worktree, argv, *a, **kw):
        if "diff" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad object\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    assert gitio.came_in(box.wt, before) == ["git could not list it"]


def test_the_docs_count_of_the_commits_named_is_the_codes():
    """Every prose doc that says how many commits the listing names one by one (`the newest 20`) says
    `gitio._CAME_IN_COMMITS`, and each of the four says it at least once — a line break anywhere among `the`,
    `newest` and the count read as the space it renders as. Guards a count in prose with a check (CLAUDE.md): a change
    of the constant fails here, not silently in four docs, and a count a reflow wrapped is not skipped (read B-f2, S2;
    fold 12's reflow put the break between `the` and `newest`, and the narrower pattern found no count there)."""
    docs = ("SECURITY.md", "CHANGELOG.md", "docs/logging-architecture.md", "docs/troubleshooting.md")
    for rel in docs:
        with open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as fh:
            said = [int(n) for n in re.findall(r"the\s+newest\s+(\d+)", fh.read())]
        assert said and set(said) == {gitio._CAME_IN_COMMITS}, (rel, said)


# ---- the undo's guards: fold 3's readers' rows (read A-f3, read B-f3 S1) ---------------------------------------- #

def _stops_writing(monkeypatch, wt, files, delete=()):
    """The replay's checkout of main stops part-way (rc 1, no rebase begun, git's `Permission denied`) having written
    `files` (a name relative to the worktree -> its bytes) and deleted `delete` — what real git does over a path it
    cannot write (read B-f1, B1)."""
    real = gitio.subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, list) and "rebase" in argv and "--quiet" in argv:
            for rel in delete:
                os.remove(os.path.join(wt, rel))
            for rel, data in files.items():
                path = os.path.join(wt, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
            return subprocess.CompletedProcess(argv, 1, b"", b"error: unable to create file x: Permission denied\n")
        return real(argv, *a, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", run)


def test_an_ignored_file_of_mains_git_wrote_is_removed(box, monkeypatch):
    """HEAD ignores `build/`; main adds `build/out.txt`; the stopped checkout wrote it with main's bytes: it is
    removed and named. Guards the undo's list taking ignored files too (`ls-files --others`, not
    `--exclude-standard`): without them git's file is left unsaid and refuses the next sync (read B-f3, S1:
    M4-untracked-exclude)."""
    box.promote({"build/out.txt": "b\n"})
    _commit(box.wt, {".gitignore": "build/\n", "mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {"build/out.txt": b"b\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "were removed (main holds them): build/out.txt." in said, said
    assert not os.path.exists(os.path.join(box.wt, "build", "out.txt"))


def test_mains_bytes_are_compared_raw_not_through_heads_attributes(box, monkeypatch):
    """Main adds `promoted.txt` holding CRLF bytes; HEAD's `.gitattributes` says `*.txt text`; the stopped checkout
    wrote main's bytes: it is removed. Guards the undo's hash being of the file's bytes (`_blob_id`): a hash through
    HEAD's attributes turns CRLF into LF, is not main's, and main's own file is kept and named (read B-f3, S1:
    M4-hash-filters)."""
    box.promote({"promoted.txt": "p\r\n"})
    _commit(box.wt, {".gitattributes": "*.txt text\n", "mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {"promoted.txt": b"p\r\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "were removed (main holds them): promoted.txt." in said, said
    assert not os.path.exists(os.path.join(box.wt, "promoted.txt"))


@pytest.mark.skipif(os.name != "posix", reason="a filter command run by sh")
def test_no_filter_runs_in_the_undo_even_one_mains_attributes_select(box, tmp_path, monkeypatch):
    """The operator's config names a clean filter `x` that writes a marker; main adds `sub/.gitattributes` (`*
    filter=x`) and `sub/f.txt`; the stopped checkout wrote both with main's bytes: no marker is written, and both are
    removed. Guards the undo's hash against the arming row's residual: a `hash-object` reading the attributes main's
    checkout just wrote ran the operator's program on main's file (read B-f3, S1: M4-hash-filters)."""
    marker = tmp_path / "filter-ran"
    box.promote({"sub/.gitattributes": "* filter=x\n", "sub/f.txt": "f\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _git(box.wt, "config", "filter.x.clean", "sh -c 'echo ran >> %s; cat'" % marker)
    _stops_writing(monkeypatch, box.wt, {"sub/.gitattributes": b"* filter=x\n", "sub/f.txt": b"f\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "were removed (main holds them): sub/.gitattributes, sub/f.txt." in said, said
    assert not marker.exists(), "the operator's clean filter ran in the undo"


@pytest.mark.skipif(os.name != "posix", reason="a symbolic link")
def test_a_worktree_named_through_a_link_still_has_mains_files_removed(box, tmp_path, monkeypatch):
    """The worktree is named through a link to it (`--worktree /srv/link`): main's `promoted.txt` git wrote is still
    removed. Guards the realpath of the worktree in `_a_file_in_place`: without it no file is "in place", none is
    removed, and the next sync is refused over main's files (read B-f3, S1: M4-inplace-wt-norealpath)."""
    link = str(tmp_path / "wt-link")
    os.symlink(box.wt, link)
    box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {"promoted.txt": b"p\n"})
    rc, said = gitio.sync_worktree(link, box.bare)
    assert rc == 1 and "were removed (main holds them): promoted.txt." in said, said
    assert not os.path.exists(os.path.join(box.wt, "promoted.txt"))


def test_twelve_files_of_mains_name_ten_and_a_count(box, monkeypatch):
    """Main adds twelve files and the stopped checkout wrote them all with main's bytes: the sentence names the first
    ten and `and 2 more`. Guards the cap on a sentence the deploy prints in its failed message: a large promote
    that stopped early could name thousands of paths (read B-f3, S1: M3-names-cap, M4-names-more-off)."""
    files = {"f%02d.txt" % i: "p%d\n" % i for i in range(12)}
    box.promote(files)
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {k: v.encode() for k, v in files.items()})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and ("were removed (main holds them): %s and 2 more." % ", ".join(sorted(files)[:10])) in said, said


@pytest.mark.skipif(os.name != "posix", reason="a symbolic link")
def test_a_file_beyond_a_link_is_kept_and_what_the_link_points_at_untouched(box, tmp_path, monkeypatch):
    """Main adds `d/x.txt`; `d` in the worktree is a link to a directory elsewhere holding `x.txt` with main's bytes;
    the undo's list (git's, here widened: a directory swapped for a link after the checkout) names `d/x.txt` as new:
    the file elsewhere is kept and `d/x.txt` named. Guards the directory check in `_a_file_in_place`, the one guard
    between the undo and a deletion outside the worktree (read B-f3, S1: M3-inplace-dir)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_bytes(b"p\n")
    box.promote({"d/x.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    os.symlink(str(outside), os.path.join(box.wt, "d"))
    _stops_writing(monkeypatch, box.wt, {})
    real, calls = gitio._untracked, []

    def untracked(wt):
        got = real(wt)
        calls.append(1)
        return got if len(calls) == 1 or got is None else got | {b"d/x.txt"}
    monkeypatch.setattr(gitio, "_untracked", untracked)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "are left, untracked: d/x.txt (a link" in said, said
    assert (outside / "x.txt").exists(), "a file outside the worktree was removed"


def test_a_file_that_could_not_be_removed_is_named(box, monkeypatch):
    """Main's `promoted.txt` git wrote cannot be removed (the unlink fails): it is named as left. Guards the unlink's
    failure being said: a file said nowhere refuses the next sync with no word of why (read B-f3, S1:
    M4-unlink-fail-silent)."""
    box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {"promoted.txt": b"p\n"})
    real = os.unlink

    def unlink(path, *a, **kw):
        if str(path).endswith("promoted.txt"):
            raise PermissionError(13, "Permission denied", path)
        return real(path, *a, **kw)
    monkeypatch.setattr(gitio.os, "unlink", unlink)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    monkeypatch.setattr(gitio.os, "unlink", real)
    assert rc == 1 and ("are left, untracked: promoted.txt (a link, bytes that are not main's, or a file that could "
                        "not be removed)") in said, said


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_tracked_files_the_reset_did_not_put_back_are_said_to_differ(box, monkeypatch):
    """The stopped checkout deleted the tracked `mine.txt` and the undo's `reset --hard` did nothing: the sentence
    says tracked files differ from HEAD, never `as they were`, and names `git -C <worktree> status` and its `reset
    --hard <HEAD>`, the worktree quoted for the shell. Guards the second status after the reset: without it the sync
    says the worktree is as it was with a tracked file deleted — fold 1's false sentence (read B-f3, S1:
    M4-differ-unreachable); and both commands pasted as printed (read B-f8, N3: B9-undo-differ-status,
    -reset-unquoted)."""
    box.promote({"promoted.txt": "p\n"})
    mine = _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {}, delete=("mine.txt",))
    real = gitio._wt

    def wt(worktree, argv, *a, **kw):
        if "reset" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: x\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "tracked files differ from it" in said and "as they were" not in said, said
    q = shlex.quote(box.wt)
    assert "Run `git -C %s status`, keep what you need, `git -C %s reset --hard %s`" % (q, q, mine) in said, said


def test_an_undo_whose_status_failed_removes_nothing(box, monkeypatch):
    """The stopped checkout wrote main's `promoted.txt` and the undo's `git status` fails: nothing is removed. Guards
    the order of the undo: the clearing runs after the status vouched for the tracked files, never when the undo has
    just said git could not (read B-f3, S1: M4-clear-before-status)."""
    box.promote({"promoted.txt": "p\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {"promoted.txt": b"p\n"})
    real, seen = gitio._wt, []

    def wt(worktree, argv, *a, **kw):
        if "ls-files" in argv:
            seen.append(1)              # the list before the replay is taken after the status before it
        if seen and "status" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: index file corrupt\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "git could not say whether the tracked files are HEAD's" in said, said
    assert os.path.isfile(os.path.join(box.wt, "promoted.txt"))


def test_a_name_left_is_shown_escaped_never_as_the_bytes_a_terminal_acts_on(box, monkeypatch):
    """Main's file whose name holds an escape (ESC [31m), written by git with other bytes, is named escaped: the
    sentence carries no ESC. Guards `_names` showing each name as `canon.show` does: a writer of `main` chooses the
    names, and the deploy prints the sentence to a terminal (read A-f3: M4-names-raw)."""
    odd = "e" + chr(27) + "[31m.txt"
    box.promote({odd: "main\n"})
    _commit(box.wt, {"mine.txt": "m\n"}, "mine")
    _stops_writing(monkeypatch, box.wt, {odd: b"other\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and chr(27) not in said and "are left, untracked: e" in said, said


# ---- an ignored file in main's way: refused before the replay (read A-f3, S1) ------------------------------------- #

@pytest.mark.parametrize("ignore, mine, mains, clash", [
    ("build/\n", "build/out.txt", "build/out.txt", "build/out.txt"),       # at a path main tracks
    ("cache\n", "cache", "cache/x.txt", "cache"),                          # where main has a directory
    ("gen/\n", "gen/a.txt", "gen", "gen/a.txt"),                           # under a path main tracks as a file
])
def test_an_ignored_file_in_mains_way_is_refused_before_the_replay_and_kept(box, ignore, mine, mains, clash):
    """The worktree holds an ignored file of the operator's (no commit holds it) where the checkout of main would
    write a file, make a directory or write a file over its directory: rc 1, the sentence names the file, says to
    move it aside and that HEAD was not changed; HEAD is as it was and the file keeps its bytes. Guards the refusal
    before the replay: git refuses an untracked file in a checkout's way only when it is not ignored, and overwrites
    or removes an ignored one without a word — the operator's file was gone, and no commit held it (read A-f3,
    S1)."""
    head = _commit(box.wt, {".gitignore": ignore}, "ignore")       # the worktree's own: main's adds are not ignored
    box.promote({mains: "from the promote\n"})
    _write(box.wt, {mine: "the operator's own\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "holds an ignored file, which no commit holds: %s." % clash in said, said
    assert "move it aside" in said and said.endswith("HEAD was not changed"), said
    assert _state(box.wt)[1:] == (head, "refs/heads/main")
    with open(os.path.join(box.wt, *mine.split("/")), encoding="utf-8") as fh:
        assert fh.read() == "the operator's own\n"


def test_an_ignored_file_beside_mains_files_is_left_and_the_sync_brings_the_worktree_forward(box):
    """An ignored file at a path main does not track, in a directory main adds files to (`build/local.txt` beside
    main's `build/out.txt`): the sync brings the worktree forward and the file keeps its bytes. Guards the refusal
    against overreach: only a file the checkout would overwrite or remove is in main's way."""
    _commit(box.wt, {".gitignore": "build/local.txt\n"}, "ignore")
    box.promote({"build/out.txt": "from the promote\n"})
    _write(box.wt, {"build/local.txt": "the operator's own\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    with open(os.path.join(box.wt, "build", "local.txt"), encoding="utf-8") as fh:
        assert fh.read() == "the operator's own\n"


@pytest.mark.parametrize("failing", [["--ignored"], ["ls-tree"]])
def test_an_ignored_list_or_mains_list_git_cannot_make_refuses_before_the_replay(box, monkeypatch, failing):
    """`git ls-files --ignored`, or `git ls-tree` of main, fails (rc 128) with an ignored file in the worktree: rc 1,
    `could not list the ignored files … or the files of the canonical's main`, `HEAD was not changed`, and HEAD is as
    it was. Guards the refusal's own doubt: a list it could not make is not "nothing in the way"."""
    head = _commit(box.wt, {".gitignore": "local.txt\n"}, "ignore")
    box.promote({"promoted.txt": "p\n"})
    _write(box.wt, {"local.txt": "x\n"})
    real = gitio._wt

    def wt(worktree, argv, *a, **kw):
        if all(w in argv for w in failing):
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: x\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "could not list the ignored files of the worktree" in said, said
    assert said.endswith("HEAD was not changed") and _state(box.wt)[1:] == (head, "refs/heads/main"), said


def test_an_ignored_nested_repository_in_mains_way_is_refused_and_kept(box):
    """An ignored directory that is a repository of its own (`lib/`, `git init` there) holds the operator's
    `lib/x.txt` where main's promote adds `lib/x.txt`: exit 1, the refusal names `lib`, HEAD is as it was and the
    file keeps its bytes. Guards the names compared: git lists an ignored nested repository as `lib/`, with its
    slash, which matched none of main's paths or directories, so the checkout went ahead (read A-f4, N1)."""
    head = _commit(box.wt, {".gitignore": "lib/\n"}, "ignore")
    box.promote({"lib/x.txt": "from the promote\n"})
    subprocess.run(["git", "init", "-q", os.path.join(box.wt, "lib")], check=True, capture_output=True)
    _write(box.wt, {"lib/x.txt": "the operator's own\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "holds an ignored file, which no commit holds: lib." in said, said
    assert _state(box.wt)[1:] == (head, "refs/heads/main")
    with open(os.path.join(box.wt, "lib", "x.txt"), encoding="utf-8") as fh:
        assert fh.read() == "the operator's own\n"


@pytest.mark.parametrize("ignore, mine, mains", [("b\n", "b", "b/x.txt"), ("g/\n", "g/a.txt", "g")])
def test_a_one_character_path_of_mains_is_in_the_way_too(box, ignore, mine, mains):
    """The ignored-file refusal with a ONE-character name: an ignored file `b` where main has the directory `b/`,
    and an ignored `g/a.txt` under main's file `g`: exit 1, the file named. Guards `_in_mains_way`'s slices of each
    name at every `/`, which start at index 1: the other rows' names (build, cache, gen) are all longer, and a slice
    from index 2 lets the replay overwrite a one-character name's file without a word (read B-f4, S1:
    M5-ignored-dirs-1char, M5-ignored-under-1char)."""
    _commit(box.wt, {".gitignore": ignore}, "ignore")
    box.promote({mains: "from the promote\n"})
    _write(box.wt, {mine: "the operator's own\n"})
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "holds an ignored file, which no commit holds: %s." % mine in said, said


def test_an_abort_that_outlasts_its_bound_says_so_and_where_head_is(box, monkeypatch):
    """The undo's own `rebase --abort` outlasts its bound (TimeoutExpired, after it ran): the sentence says `could
    not be put back (the abort did not finish in <bound> s)`, and `HEAD is <HEAD> as it was`, read after the abort.
    Guards the abort's own timeout branch, which the replay's timeout rows never run, and the HEAD it names: the
    branch said `HEAD is unreadable` with HEAD never read (read B-f4, S1: M5-abort-timeout-unsaid, -uncaught; N5)."""
    box.promote({"note.txt": "promoted\n"})
    mine = _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real = gitio._wt

    def wt(worktree, argv, **kw):
        got = real(worktree, argv, **kw)
        if argv[-2:] == ["rebase", "--abort"]:
            raise subprocess.TimeoutExpired(argv, gitio._LAND_SECONDS)
        return got
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and ("could not be put back (the abort did not finish in %d s)" % gitio._LAND_SECONDS) in said, said
    assert "HEAD is %s as it was" % mine in said and "unreadable" not in said, said


def test_the_could_not_say_remedy_names_the_git_directorys_index(box, monkeypatch):
    """The undo's status fails: the sentence's remedy is `rm <the worktree's git dir>/index && git -C <worktree>
    reset`. Guards the remedy's path: the could-not-say row asserts the sentence up to git's reason, and a remedy
    that removes a file of the operator's named `index` in the worktree (or names none) leaves the damaged index
    and passes it (read B-f4, S1: M5-index-path, M5-index-remedy-off)."""
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    real, seen = gitio._wt, {"rebase": False}

    def wt(worktree, argv, *a, **kw):
        if "rebase" in argv:
            seen["rebase"] = True
        if seen["rebase"] and "status" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: index file corrupt\n")
        return real(worktree, argv, *a, **kw)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    index = os.path.join(_git(box.wt, "rev-parse", "--path-format=absolute", "--git-dir"), "index")
    assert rc == 1 and "`rm %s && git -C %s reset`" % (index, box.wt) in said, said


def test_a_worktree_named_below_the_top_of_its_repository_is_refused_and_its_files_kept(box):
    """The worktree is named as a directory inside the repository (`sub`), which holds an ignored file of the
    operator's at a path main's promote adds: exit 1, `is not the top of its repository`, `nothing was changed`;
    HEAD is as it was and the file keeps its bytes. Guards the refusal of an ignored file in main's way, which
    compares git's list of the ignored files — relative to the directory git runs in — with main's, named from the
    top: from a subdirectory no name matched, and the checkout overwrote the operator's file (read A-f4, S2). The
    sentence names the top git gave, and nothing came across before the refusal: every ref as it was
    (`refs/remotes/local/main` too) and the promote's commit not in the worktree's store — the check after the pack
    and the ref said `nothing was changed` over the canonical's objects and a moved `local/main` (read B-f5, S2:
    M6-top-after-bring, M6-top-msg-notop)."""
    head = _commit(box.wt, {".gitignore": "sub/local.txt\n", "sub/keep.txt": "k\n"}, "ignore")
    before = _state(box.wt)
    promoted = box.promote({"sub/local.txt": "from the promote\n"})
    _write(box.wt, {"sub/local.txt": "the operator's own\n"})
    rc, said = gitio.sync_worktree(os.path.join(box.wt, "sub"), box.bare)
    top = _git(box.wt, "rev-parse", "--show-toplevel")
    assert rc == 1 and "is not the top of its repository (%s)" % top in said, said
    assert said.endswith("nothing was changed") and _state(box.wt)[1:] == (head, "refs/heads/main"), said
    assert _state(box.wt) == before and not _has(box.wt, promoted), "something came across before the refusal"
    with open(os.path.join(box.wt, "sub", "local.txt"), encoding="utf-8") as fh:
        assert fh.read() == "the operator's own\n"


def test_a_directory_git_names_no_top_for_is_refused_as_not_the_top(box):
    """The worktree's `.git` directory, and a bare repository, have no top git can name: each is refused as not
    the top, never answered "in step" or judged as a worktree. Guards the top check's empty answer (read A-f5:
    R6-top-unnamed-taken)."""
    for where in (os.path.join(box.wt, ".git"), box.bare):
        rc, said = gitio.sync_worktree(where, box.bare)
        assert rc == 1 and "is not the top of its repository" in said, (where, said)


def test_a_top_git_cannot_say_is_refused(box, monkeypatch):
    """`git rev-parse --show-toplevel` cannot be run (OSError): exit 1, `(git could not say where that is)`, HEAD as
    it was. Guards a top git could not say taken as the top: the sync went on with its ignored-file check blind to
    the directory it may be in (read B-f5, S2: M6-top-empty-ok)."""
    box.promote({"p.txt": "p\n"})
    real = gitio._wt

    def wt(worktree, argv, **kw):
        if argv == ["rev-parse", "--show-toplevel"]:
            raise FileNotFoundError(2, "No such file or directory", "git")
        return real(worktree, argv, **kw)
    with monkeypatch.context() as m:
        m.setattr(gitio, "_wt", wt)
        rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "(git could not say where that is)" in said, said
    assert _git(box.wt, "rev-parse", "HEAD") == box.seed


@pytest.mark.skipif(os.name != "posix", reason="a directory whose name ends in a space")
def test_a_worktree_whose_name_ends_in_a_space_is_its_own_top_and_is_brought_forward(box, tmp_path):
    """A clone of the worktree in a directory whose name ends in a space, synced after a promote: brought forward,
    its mark in its own git directory naming HEAD before. Guards git's answer read with only its newline taken off:
    a top read with every trailing space stripped is not the worktree, and the sync refused every run from it as
    "not the top" (read A-f5, N3)."""
    spaced = str(tmp_path / "spaced ")
    subprocess.run(["git", "clone", "-q", box.wt, spaced], check=True, capture_output=True)
    box.promote({"p.txt": "p\n"})
    rc, said = gitio.sync_worktree(spaced, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    assert gitio.synced_since(spaced) == box.seed and os.path.isfile(os.path.join(spaced, ".git", gitio.SYNC_MARK))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO (POSIX)")
def test_blob_id_of_a_fifo_or_a_device_is_none_and_never_waits(tmp_path):
    """`_blob_id` of a FIFO with no writer, and of a character device (`os.devnull`), is None, and returns within
    10 s. Guards the undo's hash against what can sit at a path git wrote from main: opened without O_NONBLOCK, a
    FIFO's open waits for a writer for ever and the sync hangs inside its own undo; without the S_ISREG check, a
    FIFO or a device reads as empty and is hashed as the empty blob, which main may hold at that path (read A-f4's
    rows, A5-blob-nonblock and A5-blob-isreg)."""
    fifo = str(tmp_path / "f")
    os.mkfifo(fifo)
    got = {}
    t = threading.Thread(target=lambda: got.update(fifo=gitio._blob_id(fifo, "sha1")), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "_blob_id of a FIFO did not return in 10 s"
    assert got == {"fifo": None} and gitio._blob_id(os.devnull, "sha1") is None, got


def test_blob_id_is_gits_own_id_in_a_sha256_repository(tmp_path):
    """In a repository made with `--object-format=sha256`, `_blob_id(path, "sha256")` is the id `git hash-object
    --no-filters` gives the same file there (64 hex digits), and in a sha1 one the sha1 id. Guards the undo in a
    sha256 repository: an `algo` refused or hashed wrongly there makes every file git wrote from main read as not
    main's, so an undone replay removes none of them (read A-f4's rows, A5-blob-sha256-off)."""
    for fmt in ("sha1", "sha256"):
        repo = tmp_path / fmt
        subprocess.run(["git", "init", "-q", "--object-format=" + fmt, str(repo)], check=True)
        path = repo / "f.txt"
        path.write_bytes(b"main's bytes" + bytes([13, 10]) + bytes([0, 255]))
        want = subprocess.run(["git", "-C", str(repo), "hash-object", "--no-filters", "f.txt"], check=True,
                              capture_output=True).stdout.decode().strip()
        assert len(want) == (64 if fmt == "sha256" else 40), want
        assert gitio._blob_id(str(path), fmt) == want, (fmt, want)
        # No other name of a hash: `""` (git could not say the format) would raise out of `hashlib.new` and end the
        # sync in a traceback instead of its sentence (read B-f4, S1: M5-blob-algo-check-off, M5-blob-sha1-only).
        assert gitio._blob_id(str(path), "") is None and gitio._blob_id(str(path), "md5") is None


def test_a_sha256_repositorys_undo_removes_mains_file(tmp_path):
    """In a sha256 repository, a file of main's that git wrote (untracked now, main's bytes) is removed by
    `_clear_what_main_left`. Guards the undo's hash taken in the repository's own format (`rev-parse
    --show-object-format`): hashed as sha1 it never matches main's 64-digit id, and main's file is left to refuse
    the next sync (read B-f4, S1: M5-algo-hardcoded)."""
    wt = str(tmp_path / "wt")
    subprocess.run(["git", "init", "-q", "--object-format=sha256", "-b", "main", wt], check=True, capture_output=True)
    base = _commit(wt, {"a.txt": "a\n"}, "base")
    main_id = _commit(wt, {"f.txt": "main's\n"}, "main")
    _git(wt, "reset", "-q", "--hard", base)
    _write(wt, {"f.txt": "main's\n"})
    said = gitio._clear_what_main_left(wt, main_id, set())
    assert "were removed (main holds them): f.txt." in said and not os.path.lexists(os.path.join(wt, "f.txt")), said


# ---- what a play's own sync reads and removes: HEAD before, the mark (read A-f3, B1) ------------------------------ #

def test_head_id_is_heads_commit_or_none(box, tmp_path):
    """`head_id` names HEAD's commit in a worktree, and None in a repository with no commit and in a directory that is
    none. Guards what `kontroll-land.py --sync --stop-as` names as HEAD before the sync when the mark cannot be
    read: a word that is not a commit id would be listed from as one."""
    assert gitio.head_id(box.wt) == box.seed
    empty = str(tmp_path / "empty")
    subprocess.run(["git", "init", "-q", empty], check=True, capture_output=True)
    assert gitio.head_id(empty) is None
    assert gitio.head_id(str(tmp_path / "nothing-here")) is None


def test_clear_mark_removes_the_mark_and_leaves_what_no_sync_writes(box, tmp_path):
    """`clear_mark`: a mark a sync wrote is removed (None); no mark is None; a mark that is a link is left, and so
    is its target, and a mark that is a directory is left — each with a reason that names the mark; a worktree git
    cannot name the git directory of says so. Guards the removal a play's own sync does last: through a link it
    would remove a file of the operator's elsewhere, and a mark it could not remove must be said, or the next run
    stops again without a word of why."""
    box.promote({"promoted.txt": "p\n"})
    assert gitio.sync_worktree(box.wt, box.bare)[0] == 0 and os.path.isfile(_mark(box.wt))
    assert gitio.clear_mark(box.wt) is None and not os.path.lexists(_mark(box.wt))
    assert gitio.clear_mark(box.wt) is None
    if hasattr(os, "symlink") and os.name == "posix":
        target = tmp_path / "elsewhere.txt"
        target.write_text(box.seed + "\n", encoding="ascii")
        os.symlink(str(target), _mark(box.wt))
        why = gitio.clear_mark(box.wt)
        assert why and _mark(box.wt) in why and "not a file a sync writes" in why, why
        assert os.path.islink(_mark(box.wt)) and target.read_text(encoding="ascii") == box.seed + "\n"
        os.remove(_mark(box.wt))
    os.mkdir(_mark(box.wt))
    why = gitio.clear_mark(box.wt)
    assert why and "not a file a sync writes" in why and os.path.isdir(_mark(box.wt)), why
    assert gitio.clear_mark(str(tmp_path / "nothing-here")) == "git could not name the worktree's git directory"


def test_clear_mark_says_why_a_regular_mark_it_could_not_remove_is_there(box, monkeypatch):
    """`clear_mark` on a regular mark whose removal fails (`os.unlink` raising EACCES): it returns `<mark>: Permission
    denied`, and the mark is still there. Guards the reason the play's stop prints: a failure read as "no mark"
    would say "The mark is removed" while the next run stops on it again, and nothing would say why (fold 4's own
    mutant, F4-clear-oserror)."""
    with open(_mark(box.wt), "w", encoding="ascii", newline="\n") as fh:
        fh.write(box.seed + "\n")

    def refuse(path, *a, **k):
        raise PermissionError(errno.EACCES, "Permission denied", path)

    with monkeypatch.context() as m:
        m.setattr(gitio.os, "unlink", refuse)
        assert gitio.clear_mark(box.wt) == "%s: Permission denied" % _mark(box.wt)
    assert os.path.isfile(_mark(box.wt))
    os.remove(_mark(box.wt))


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="needs POSIX symlinks")
def test_the_undos_helpers_neither_follow_a_link_nor_hash_through_one(box):
    """`_a_file_in_place` is True for a regular file and False for a link to one; `_blob_id` of a link is None, and of
    the file it names is git's blob id of the bytes (`blob 2\\0x\\n` hashed). Guards the undo's two belts against a
    link: each alone keeps a link the operator made from being removed as main's, so the undo's tests pass with
    either one gone — `os.stat` for `os.lstat` in the one (M3-isreg-follows), no `O_NOFOLLOW` in the other
    (F4-blob-nofollow); here each is held on its own."""
    plain = os.path.join(box.wt, "plain.txt")
    with open(plain, "wb") as fh:
        fh.write(b"x\n")
    os.symlink("plain.txt", os.path.join(box.wt, "l.txt"))
    assert gitio._a_file_in_place(box.wt, b"plain.txt") is True
    assert gitio._a_file_in_place(box.wt, b"l.txt") is False
    assert gitio._blob_id(os.path.join(box.wt, "l.txt"), "sha1") is None
    assert gitio._blob_id(plain, "sha1") == hashlib.sha1(b"blob 2\0x\n").hexdigest()


# ---- the pins: no transport, the canonical's side in the seam, the replay's switches ----------------------------- #

def _functions():
    tree = ast.parse(open(os.path.join(ROOT, "scripts", "kontroll", "gitio.py"), encoding="utf-8").read())
    return {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}


def _argv_words(fn):
    for node in ast.walk(fn):
        if isinstance(node, ast.List):
            yield [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def test_the_sync_is_a_registered_write_runs_no_transport_and_reads_the_canonical_in_the_seam():
    """`sync_worktree` and the helpers that write the worktree (`_bring_main`, `_receive_in_worktree`,
    `_undo_replay`) and start the canonical's packer (`_pack_in_canonical`) are in `WRITE_VERBS`; the canonical's
    side (`_held_in_canonical`, `_pack_in_canonical`) runs `git cat-file | pack-objects` in the seam
    (`**canon.git_kwargs(…)`); no argv of the sync's functions names a transport (`fetch`, `ls-remote`,
    `upload-pack`, `push` …); the replay's argv carries the identity and the switches that keep the operator's
    config from deciding it (no autostash, no autosquash, no update-refs, no rebase-merges, no signing); and its
    environment names no editor, no prompt, `kontroll` as the committer, and no git-lfs download. Guards the
    reversions "the sync fetches again" — the transport this theme removed — and "a switch dropped": with
    `rebase.updateRefs` set in the operator's config, a replay would move their other branches too, and with
    git-lfs installed its smudge would download during the deploy. The worktree's `index-pack` runs through `_wt`
    (no hook, no fsmonitor program: read A-f3, N1), and no other call of `_receive_in_worktree` names it; the undo's
    clearing and the mark's removal (`_clear_what_main_left`, `clear_mark`) are registered writes too."""
    for verb in ("sync_worktree", "_bring_main", "_receive_in_worktree", "_pack_in_canonical", "_undo_replay",
                 "_clear_what_main_left", "clear_mark"):
        assert verb in WRITE_VERBS, verb
    receive = _functions()["_receive_in_worktree"]
    packs = [(getattr(c.func, "id", None) or getattr(c.func, "attr", None)) for c in ast.walk(receive)
             if isinstance(c, ast.Call) and any("index-pack" in w for a in c.args if isinstance(a, ast.List)
                                                for w in [e.value for e in a.elts if isinstance(e, ast.Constant)])]
    assert packs == ["_wt"], packs
    assert_git_runs_in_the_seam("scripts/kontroll/gitio.py", verbs={"cat-file", "pack-objects"},
                                functions=("_held_in_canonical", "_pack_in_canonical"))
    defs = _functions()
    for name in SYNC:
        for words in _argv_words(defs[name]):
            assert not TRANSPORT & set(words), "%s runs a transport: %r" % (name, words)
    replay = [w for w in _argv_words(defs["sync_worktree"]) if "rebase" in w and "--quiet" in w]
    assert len(replay) == 1, replay
    assert {"user.name=kontroll", "user.email=kontroll@localhost", "commit.gpgSign=false", "rebase.autoStash=false",
            "rebase.autoSquash=false", "rebase.updateRefs=false", "rebase.rebaseMerges=false"} <= set(replay[0])
    env = {k.arg: k.value.value for node in ast.walk(defs["sync_worktree"]) if isinstance(node, ast.Call)
           and getattr(node.func, "id", None) == "dict" for k in node.keywords if isinstance(k.value, ast.Constant)}
    assert env == {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0",
                   "GIT_COMMITTER_NAME": "kontroll", "GIT_COMMITTER_EMAIL": "kontroll@localhost",
                   "GIT_LFS_SKIP_SMUDGE": "1"}, env


def test_the_operators_other_branches_stay_where_they_were(box):
    """The operator's config sets `rebase.updateRefs` and a branch `keep` names a commit the replay rewrites: after
    the sync, `keep` still names the commit it named, and only the checked-out branch moved. Guards the
    `rebase.updateRefs=false` switch with behaviour, not a word: the sync brings ONE branch forward, the one the
    deploy lands, and leaves every other ref of the operator's as it was."""
    box.promote({"promoted.txt": "p\n"})
    first = _commit(box.wt, {"a.txt": "a\n"}, "a")
    _git(box.wt, "branch", "keep")
    _commit(box.wt, {"b.txt": "b\n"}, "b")
    _git(box.wt, "config", "rebase.updateRefs", "true")
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 0 and said.startswith("brought forward"), said
    assert _git(box.wt, "rev-parse", "keep") == first != _git(box.wt, "rev-parse", "HEAD~1")


# ---- the update scripts ask first -------------------------------------------------------------------------------- #

def _with_scripts(box):
    """The worktree as a node's checkout: this tree's `scripts/` copied in and committed, on the canonical's `main`."""
    shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(box.wt, "scripts"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    box.seed = _commit(box.wt, {}, "the tool")
    _git(box.wt, "push", "-q", "local", "HEAD:refs/heads/main")
    return box


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("python3"),
                    reason="the shell doors run under bash with a `python3` on the PATH (Linux; the gate image)")
def test_the_update_scripts_refuse_before_merging_when_a_promote_is_ahead_and_update_sh_lands_after_a_sync(box,
                                                                                                         tmp_path):
    """A promote moved `main` past the checkout and upstream has a new commit: `scripts/update.sh` exits 3 with
    `REFUSING to update` before it fetches — HEAD is as it was and nothing of upstream's was fetched;
    `scripts/update-from-bundle.sh` with a bundle of that commit exits 3 the same way. After `kontroll-land.py
    --sync`, `update.sh` fetches, merges and lands: `main` is the merged commit, which holds the promote. Guards
    the order: before the check, each script merged first and was refused at its landing, leaving a merge in the
    checkout the operator then had to replay by hand."""
    _with_scripts(box)
    upstream = str(tmp_path / "upstream")
    subprocess.run(["git", "clone", "-q", box.wt, upstream], check=True, capture_output=True)
    _git(upstream, "config", "core.autocrlf", "false")
    _git(box.wt, "remote", "add", "origin", upstream)
    good = _commit(upstream, {"code.txt": "v2\n"}, "code update")
    promoted = box.promote({"promoted.txt": "p\n"})
    head = _git(box.wt, "rev-parse", "HEAD")
    p = subprocess.run(["bash", "scripts/update.sh"], cwd=box.wt, capture_output=True, text=True,
                       env=dict(os.environ, **_WHO))
    assert p.returncode == 3 and "REFUSING to update" in p.stdout + p.stderr, p.stdout + p.stderr
    assert _git(box.wt, "rev-parse", "HEAD") == head and not _has(box.wt, good)
    bundle = str(tmp_path / "k.bundle")
    _git(upstream, "bundle", "create", bundle, box.seed + "..main")
    p = subprocess.run(["bash", "scripts/update-from-bundle.sh", bundle], cwd=box.wt, capture_output=True,
                       text=True, env=dict(os.environ, **_WHO))
    assert p.returncode == 3 and "REFUSING to update" in p.stdout + p.stderr, p.stdout + p.stderr
    assert _git(box.wt, "rev-parse", "HEAD") == head and not _has(box.wt, good)
    p = subprocess.run(["python3", "-B", "scripts/kontroll-land.py", "--sync"], cwd=box.wt, capture_output=True,
                       text=True, env=dict(os.environ, **_WHO))
    assert p.returncode == 0 and "brought forward" in p.stdout, p.stdout + p.stderr
    p = subprocess.run(["bash", "scripts/update.sh"], cwd=box.wt, capture_output=True, text=True,
                       env=dict(os.environ, **_WHO))
    assert p.returncode == 0 and "landed refs/heads/main" in p.stdout, p.stdout + p.stderr
    assert _main(box.bare) == _git(box.wt, "rev-parse", "HEAD")
    assert subprocess.run(["git", "-C", box.wt, "merge-base", "--is-ancestor", promoted, "HEAD"]).returncode == 0
    assert _has(box.wt, good)


def _upstream_with_a_new_commit(box, tmp_path, script):
    """An `origin` for the checkout with one new commit on it, and the argv that runs `script` on it (with a
    bundle of that commit for `update-from-bundle.sh`). Returns (argv, the new commit)."""
    upstream = str(tmp_path / "upstream")
    subprocess.run(["git", "clone", "-q", box.wt, upstream], check=True, capture_output=True)
    _git(upstream, "config", "core.autocrlf", "false")
    _git(box.wt, "remote", "add", "origin", upstream)
    good = _commit(upstream, {"code.txt": "v2\n"}, "code update")
    argv = ["bash", "scripts/" + script]
    if script == "update-from-bundle.sh":
        bundle = str(tmp_path / "k.bundle")
        _git(upstream, "bundle", "create", bundle, box.seed + "..main")
        argv.append(bundle)
    return argv, good


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("python3"),
                    reason="the shell doors run under bash with a `python3` on the PATH (Linux; the gate image)")
@pytest.mark.parametrize("script", ["update.sh", "update-from-bundle.sh"])
def test_the_update_scripts_refuse_when_the_check_refuses_and_fetch_nothing(box, tmp_path, script):
    """A merge in progress in the checkout (`MERGE_HEAD`), a promote on `main`, and a new upstream commit: the sync's
    check exits 1, and the update script exits 1 with `REFUSING to update`, having fetched nothing. Guards the
    guard's condition: a script that refused only on exit 3 went on to fetch and merge into a checkout in the
    middle of a merge."""
    _with_scripts(box)
    argv, good = _upstream_with_a_new_commit(box, tmp_path, script)
    box.promote({"promoted.txt": "p\n"})
    with open(os.path.join(box.wt, ".git", "MERGE_HEAD"), "w", encoding="ascii", newline="\n") as fh:
        fh.write(box.seed + "\n")
    p = subprocess.run(argv, cwd=box.wt, capture_output=True, text=True, env=dict(os.environ, **_WHO))
    assert p.returncode == 1 and "REFUSING to update" in p.stdout + p.stderr, p.stdout + p.stderr
    assert not _has(box.wt, good)


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("python3"),
                    reason="the shell doors run under bash with a `python3` on the PATH (Linux; the gate image)")
@pytest.mark.parametrize("script", ["update.sh", "update-from-bundle.sh"])
def test_the_update_scripts_refuse_a_caller_that_does_not_own_the_checkout(box, tmp_path, script):
    """The update script runs as a uid that does not own the checkout's git directory (here `id` first on the
    PATH answers 4242; root under sudo is the real case), with a promote on `main` and a new upstream commit: it
    exits 1 — not the sync check's 3 — with `REFUSING to update` and `uid 4242`, having fetched nothing. Guards the
    operator's repository: a fetch and a merge run as root leave root's objects and ref files there, which the
    operator's own git, and the deploy's state commit (which runs as the operator), cannot write over afterwards."""
    _with_scripts(box)
    argv, good = _upstream_with_a_new_commit(box, tmp_path, script)
    box.promote({"promoted.txt": "p\n"})
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "id").write_text("#!/bin/sh\necho 4242\n", encoding="ascii")
    os.chmod(str(fake / "id"), 0o755)
    env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"], **_WHO)
    p = subprocess.run(argv, cwd=box.wt, capture_output=True, text=True, env=env)
    out = p.stdout + p.stderr
    assert p.returncode == 1 and "REFUSING to update" in out and "uid 4242" in out, out
    assert not _has(box.wt, good)


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("python3"),
                    reason="the shell doors run under bash with a `python3` on the PATH (Linux; the gate image)")
def test_update_sh_refuses_a_caller_that_does_not_own_the_checkout_before_it_looks_for_its_upstream(box, tmp_path):
    """`update.sh` run as a uid that does not own the checkout (a fake `id` answering 4242), in a checkout with no
    `origin` remote: exit 1, `REFUSING to update … uid 4242` — not exit 0, `No 'origin' remote`. Guards the guard's
    place before the remote check. Run as root, git's dubious-ownership refusal makes `git remote get-url` fail the
    same way on a checkout that HAS an upstream, and the script said "no upstream" and exited 0 (read A-f2, N3; read
    B-f2, S3: UO1)."""
    _with_scripts(box)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "id").write_text("#!/bin/sh\necho 4242\n", encoding="ascii")
    os.chmod(str(fake / "id"), 0o755)
    env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"], **_WHO)
    p = subprocess.run(["bash", "scripts/update.sh"], cwd=box.wt, capture_output=True, text=True, env=env)
    out = p.stdout + p.stderr
    assert p.returncode == 1 and "REFUSING to update" in out and "uid 4242" in out, out


@pytest.mark.skipif(not (hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("bash")
                         and shutil.which("python3")), reason="needs root: the checkout's directory is chowned")
@pytest.mark.parametrize("script", ["update.sh", "update-from-bundle.sh"])
def test_the_update_scripts_name_the_git_directorys_owner_when_git_cannot(box, tmp_path, script):
    """Git refuses the checkout (a fake `git` failing `rev-parse`, as root's git does on dubious ownership), the
    checkout's directory is the caller's (a fake `id` answering 4242, the directory chowned to it) and its `.git`
    is root's: the script falls back to `.git`, not `.`, and refuses — exit 1, `uid 4242, and the checkout's git
    directory is uid 0's`. Guards the fallback's path: the owner of `.` is not the owner of what git writes (read
    B-f2, S3: UO2)."""
    _with_scripts(box)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "id").write_text("#!/bin/sh\necho 4242\n", encoding="ascii")
    (fake / "git").write_text('#!/bin/sh\n[ "$1" = rev-parse ] && exit 128\nexec %s "$@"\n' % shutil.which("git"),
                              encoding="ascii")
    for name in ("id", "git"):
        os.chmod(str(fake / name), 0o755)
    os.chown(box.wt, 4242, 4242)
    argv = ["bash", "scripts/" + script]
    if script == "update-from-bundle.sh":
        bundle = tmp_path / "k.bundle"
        bundle.write_bytes(b"")
        argv.append(str(bundle))
    env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"], **_WHO)
    p = subprocess.run(argv, cwd=box.wt, capture_output=True, text=True, env=env)
    out = p.stdout + p.stderr
    assert p.returncode == 1 and "uid 4242, and the checkout's git directory is uid 0's" in out, out


@pytest.mark.parametrize("value", ["yes", "on", "1"])
def test_the_undo_reads_core_ignorestat_as_git_does_whatever_true_is_spelled(box, value):
    """A replay stopped on a conflict and undone in a worktree whose git has `core.ignoreStat` spelled `yes`, `on` or
    `1`, which git reads as true and so flags every index entry it writes: the sentence says `core.ignoreStat` is set.
    Guards the read of the value as a boolean (`config --type=bool`): read raw, only the spelling `true` counted, and
    the undo said every file was flagged by hand (read A-f9's mutant A10-ignorestat-type-bool-off)."""
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    _git(box.wt, "config", "core.ignoreStat", value)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "does not compare" in said, said
    assert "(`core.ignoreStat` is set, which flags every file git writes)" in said, said


@pytest.mark.skipif(os.name != "posix", reason="names no Windows file system holds, read by bash")
@pytest.mark.usefixtures("isolated_git")
def test_the_flagged_listing_prints_every_flagged_name_and_a_quoted_one_given_as_dollar_quote_reads_it(tmp_path):
    """A repository with two tracked files flagged assume-unchanged, one whose name holds a byte that is not UTF-8 and
    one whose name holds a TAB: the listing the sentences print (`gitio._LIST_FLAGGED`), run by bash under
    `LC_ALL=C.UTF-8`, prints both, in ASCII, each name in double quotes, and grep calls nothing binary; and the text
    between the quotes, given to bash as `$'…'` (as `gitio._QUOTED_NAME` says), reads that very file. Guards the listing
    the operator compares flagged files from: printed as it is (`core.quotePath=false`), the name that is not UTF-8
    made grep call the listing binary and drop it, and the TAB's stayed C-quoted, so the compare as printed found no
    such path (read A-f9, N2)."""
    wt = str(tmp_path / "w")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test", LC_ALL="C.UTF-8")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    names = [b"x\xffy.txt", b"t\tb.txt"]
    for n in names:
        with open(os.path.join(os.fsencode(wt), n), "wb") as fh:
            fh.write(n + b"\n")
    for argv in (["add", "-A"], ["commit", "-q", "-m", "flagged"],
                 ["update-index", "--assume-unchanged", "--"] + [os.fsdecode(n) for n in names]):
        subprocess.run(["git", "-C", wt] + argv, check=True, capture_output=True, env=env)
    p = subprocess.run(["bash", "-c", gitio._LIST_FLAGGED.format(wt=shlex.quote(wt))], capture_output=True, env=env)
    assert p.returncode == 0 and b"binary" not in p.stderr, (p.stdout, p.stderr)
    lines = p.stdout.decode("ascii").splitlines()
    assert len(lines) == 2, lines
    assert "a name the listing prints in double quotes is quoted the C way" in gitio._QUOTED_NAME
    for line in lines:
        tag, name = line[:2], line[2:]
        assert tag == "h " and name.startswith('"') and name.endswith('"'), line
        got = subprocess.run(["bash", "-c", "cat -- %s/$'%s'" % (shlex.quote(wt), name[1:-1])], capture_output=True)
        assert got.returncode == 0 and got.stdout[:-1] in names, (line, got)


def _flagged_repo(tmp_path, names, env):
    """A repository at `<tmp>/w` with one committed file per name in `names` (bytes), each flagged assume-unchanged,
    made under `env`; returns its path."""
    wt = str(tmp_path / "w")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True, env=env)
    for n in names:
        with open(os.path.join(os.fsencode(wt), n), "wb") as fh:
            fh.write(n + b"\n")
    for argv in (["add", "-A"], ["commit", "-q", "-m", "flagged"],
                 ["update-index", "--assume-unchanged", "--"] + [os.fsdecode(n) for n in names]):
        subprocess.run(["git", "-C", wt] + argv, check=True, capture_output=True, env=env)
    return wt


@pytest.mark.skipif(os.name != "posix", reason="names no Windows file system holds, read by bash")
@pytest.mark.usefixtures("isolated_git")
def test_the_flagged_listing_prints_every_name_whatever_the_operators_own_quotepath_says(tmp_path):
    """Two tracked files flagged assume-unchanged, one whose name holds a byte that is not UTF-8: the listing the
    sentences print (`gitio._LIST_FLAGGED`), run by bash under `LC_ALL=C.UTF-8` for an operator whose global git
    config says `core.quotePath = false`, prints both, in ASCII, and grep calls nothing binary. Guards the listing
    against the operator's own config: fold 10 dropped the listing's `-c core.quotePath=false` for git's default, but
    the printed command read the operator's `core.quotePath`, and with it false the name that is not UTF-8 made grep
    call the listing binary and drop it again (read A-f10, N3; read B-f10, N2; its kill row)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[core]\n\tquotePath = false\n", encoding="ascii")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test", LC_ALL="C.UTF-8", GIT_CONFIG_GLOBAL=str(cfg))
    wt = _flagged_repo(tmp_path, [b"x\xffy.txt", b"plain.txt"], env)
    p = subprocess.run(["bash", "-c", gitio._LIST_FLAGGED.format(wt=shlex.quote(wt))], capture_output=True, env=env)
    assert p.returncode == 0 and b"binary" not in p.stderr, (p.stdout, p.stderr)
    assert len(p.stdout.decode("ascii").splitlines()) == 2, p.stdout


@pytest.mark.skipif(os.name != "posix", reason="names no Windows file system holds, read by bash")
@pytest.mark.usefixtures("isolated_git")
def test_a_quoted_name_holding_a_quote_given_back_as_the_sentence_says_reads_its_file(tmp_path):
    """A tracked file flagged assume-unchanged whose name holds a `'` and a TAB: the listing prints it in double quotes
    (`"it's\\tq.txt"`, the `'` as it is), and the text between the quotes, given to bash as `$'…'` with each `'` in it
    written `\\'`, as `gitio._QUOTED_NAME` says, reads that very file. Guards the remedy for a name git C-quotes:
    inside `$'…'` a bare `'` ends the string, and the command pasted as fold 10 said failed on an unterminated quote
    (read A-f10, N2; read B-f10, N2; its kill row)."""
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test", LC_ALL="C.UTF-8")
    name = b"it's\tq.txt"
    wt = _flagged_repo(tmp_path, [name], env)
    p = subprocess.run(["bash", "-c", gitio._LIST_FLAGGED.format(wt=shlex.quote(wt))], capture_output=True, env=env)
    line = p.stdout.decode("ascii").splitlines()[0]
    assert line[:3] == 'h "' and line.endswith('"'), line
    assert "each `'` in it written `\\'`" in gitio._QUOTED_NAME, gitio._QUOTED_NAME
    inner = line[3:-1].replace("'", "\\'")
    got = subprocess.run(["bash", "-c", "cat -- %s/$'%s'" % (shlex.quote(wt), inner)], capture_output=True)
    assert got.returncode == 0 and got.stdout == name + b"\n", (line, got)


@pytest.mark.skipif(os.name != "posix", reason="runs the printed compare with bash, over a link")
@pytest.mark.usefixtures("isolated_git")
def test_the_link_compare_the_sentences_print_tells_mains_target_from_another(tmp_path):
    """A repository at a path with a space and a quote, whose `refs/remotes/local/main` holds a link `l` to `t2`: the
    link compare the sentences print (`gitio._PUT_HEADS_BACK`'s `cat-file -p … | cmp - <(readlink -nv -- …)`, `<path>`
    filled in), run by bash, finds the worktree's link main's while it points to `t2` (exit 0), and not once it points
    to `t1` (exit 1). Guards the compare of a flagged link: `cmp` on the link reads the file it points to, so a link
    main retargeted compared as "not main's" and was kept (read A-f9, N4); and with `readlink`'s newline, which git's
    blob of a link does not hold, every link compared so and none was put back (read A-f10, N4)."""
    wt = str(tmp_path / "w t'x")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test")
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True, env=env)
    for name in ("t1", "t2"):
        with open(os.path.join(wt, name), "w", encoding="ascii") as fh:
            fh.write(name + "\n")
    os.symlink("t2", os.path.join(wt, "l"))
    for argv in (["add", "-A"], ["commit", "-q", "-m", "main's"], ["update-ref", "refs/remotes/local/main", "HEAD"]):
        subprocess.run(["git", "-C", wt] + argv, check=True, capture_output=True, env=env)
    said = gitio._PUT_HEADS_BACK.format(wt=shlex.quote(wt))
    compare = next(c for c in re.findall(r"`([^`]*)`", said) if "readlink" in c).replace("<path>", "l")
    assert subprocess.run(["bash", "-c", compare], capture_output=True).returncode == 0, compare
    os.remove(os.path.join(wt, "l"))
    os.symlink("t1", os.path.join(wt, "l"))
    assert subprocess.run(["bash", "-c", compare], capture_output=True).returncode == 1, compare


def test_the_undo_leaves_core_ignorestat_unsaid_when_its_read_times_out(box, monkeypatch):
    """A replay stopped on a conflict and undone in a worktree whose git has `core.ignoreStat` true, the read of that
    setting timing out: the undo's sentence is whole (it says the status does not compare the flagged files) and does
    not say `core.ignoreStat` is set. Guards the read's own catch: without it the timeout escaped the undo as a
    traceback, with no sentence (read B-f9's mutant B10-ignorestat-uncaught; its kill row)."""
    box.promote({"note.txt": "promoted\n"})
    _commit(box.wt, {"note.txt": "mine\n"}, "mine")
    _git(box.wt, "config", "core.ignoreStat", "true")
    real = gitio._wt

    def wt(worktree, args, *a, **k):
        if args[:1] == ["config"] and "core.ignoreStat" in args:
            raise subprocess.TimeoutExpired(["git"] + list(args), 1)
        return real(worktree, args, *a, **k)
    monkeypatch.setattr(gitio, "_wt", wt)
    rc, said = gitio.sync_worktree(box.wt, box.bare)
    assert rc == 1 and "does not compare" in said and "core.ignoreStat` is set" not in said, said


def test_each_names_list_holds_a_case_whose_two_names_both_carry_a_space_and_a_quote():
    """`NAMES` here and in test_land.py, the names every box row of the two files runs on, each hold a case whose
    canonical and worktree names both carry a space and a `'`, and on posix no skip mark of that case is true. Guards
    the rows that quote a path for the shell: with that case made plain, every box row passed and each spaced row ran on
    names quoting leaves alone (read B-f9's mutant B10-sync-names-spaced-plain+merge-unquoted; its kill row); and with
    the case kept but its `skipif` turned true on posix, every spaced row was skipped and the row passed (read B-f10,
    N4: B11-sync-names-spaced-skipped); a mark keeps a case from running whatever way it is written — `skip`, a
    `skipif` true by position or by `condition=`, `xfail(run=False)`: reading `skipif`'s first positional argument
    alone passed the other two (read B-f11, N4: B12-sync-names-skipif-kwarg, B12-sync-names-xfail-norun); `run` is
    read as pytest reads it, by its truth: `run=0` kept the case from running and passed (read B-f12, N5:
    B13-names-xfail-run-zero)."""
    import test_land

    def keeps_it_from_running(m):
        if m.name == "skip":
            return True
        if m.name == "skipif":
            return any(bool(c) for c in (list(m.args) or [m.kwargs.get("condition")]))
        return m.name == "xfail" and not m.kwargs.get("run", True)
    for where, listed in (("test_sync", NAMES), ("test_land", test_land.NAMES)):
        cases = [p.values[0] for p in listed]
        spaced = [p for p in listed if all(" " in n and "'" in n for n in p.values[0])]
        assert spaced, (where, cases)
        if os.name == "posix":
            for p in spaced:
                assert not any(keeps_it_from_running(m) for m in p.marks), (where, p.marks)


def test_no_code_span_in_a_troubleshooting_table_row_holds_an_unescaped_pipe():
    """Each table row of docs/troubleshooting.md: no code span in it holds a `|` that is not written `\\|`. Guards the
    rows whose remedy is a pipeline (`ls-files -v | grep -v '^H '`, `cat-file --filters … | cmp - …`): a GFM table
    splits a row at every unescaped `|`, inside a code span too, and drops the cells past the header's, so §10's busy,
    sync-in-progress, could-not-replay and stop rows rendered cut at the listing — the HEAD-on-no-branch remedy, the
    `$'…'` clause and the compare gone (read B-f10, S1; its kill row)."""
    with open(os.path.join(ROOT, "docs", "troubleshooting.md"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    bad = []
    for n, line in enumerate(lines, 1):
        if not line.startswith("|"):
            continue
        for m in re.finditer(r"(`+)(.+?)(?<!`)\1(?!`)", line):
            body = m.group(2)
            if any(ch == "|" and (j == 0 or body[j - 1] != "\\") for j, ch in enumerate(body)):
                bad.append((n, body[:60]))
    assert bad == [], bad


def _cells(line):
    """A table row's cells as GFM splits them: at each `|` no backslash is before (markdown-it's `escapedSplit`: a
    backslash before a `|` escapes it, whether or not that backslash is itself escaped), the outer pipes dropped."""
    out, cur, i, s = [], "", 0, line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur += "\\|"
            i += 2
            continue
        if s[i] == "|":
            out.append(cur)
            cur = ""
        else:
            cur += s[i]
        i += 1
    out.append(cur)
    return out


def _unpaired_backtick_runs(line):
    """The indexes of `line`'s backtick runs that open no code span, as CommonMark pairs them: a run is closed by
    the next run of the same length; one with none is literal text, and the scan goes on after it."""
    runs = [len(m.group(0)) for m in re.finditer(r"`+", line)]
    bad, i = [], 0
    while i < len(runs):
        j = next((k for k in range(i + 1, len(runs)) if runs[k] == runs[i]), None)
        if j is None:
            bad.append(i)
            i += 1
        else:
            i = j + 1
    return bad


@pytest.mark.parametrize("line, want", [("| ``a`b`` | c |", []), ("| `a``b` | c |", []), ("| ``a `b` | c |", [0]),
                                        ("| ``a `b`` c` | d |", [3]), ("| ``a `b | c |", [0, 1])])
def test_the_pairing_helper_pairs_runs_of_one_length_only(line, want):
    """`_unpaired_backtick_runs` on lines with runs of two lengths: a run of one inside a span of two and the reverse
    are paired, and an unclosed run of two is literal while the runs after it still pair. Guards the helper's length
    rule, which no shipped row's verdict turns on (the runs of two in troubleshooting.md hold no pipe): a run closed by
    the next run of any length, or of one at least as long, or a scan that stops at the first unclosed run, passed every
    row (read B-f16, N3 b: B17T-pair-any-length, B17T-pair-ge, B17T-pair-stop; read B-f17, N6 a and g)."""
    assert _unpaired_backtick_runs(line) == want


def test_every_troubleshooting_table_row_has_its_headers_cells_and_no_pipe_is_written_double_escaped():
    """Each table of docs/troubleshooting.md: every row splits, as GFM splits it, into as many cells as its header (a
    bare `|` anywhere, in a code span or out, adds one; GFM drops the cells past the header's count, so the text after
    it is lost), the delimiter row right after the header too (with fewer, GFM makes no table at all); no line that is
    not blank follows a table line without a leading `|` (GFM takes it as a row of its own, so a wrapped tail renders
    as a new row); no `\\\\|` is written (GFM leaves it unsplit but renders it as `\\|`, so a pasted command holds
    a backslash), nor a `\\``; and every backtick run in a row is paired, as CommonMark pairs code spans (a run is
    closed by the next run of its length). Guards the reach of the code-span row above: a bare pipe in a row's prose, or
    a doubled backslash before one, passed it (read B-f11, N2: B12-trprobe-pipe-outside-code,
    B12-trprobe-pipe-double-backslash); and this row reset at a line that is not a row and skipped the delimiter row, so
    a wrapped tail and a short delimiter row passed it (read B-f12, N2: B13-tr379-tail-wrapped, B13-tr366-tail-wrapped,
    B13-trprobe-delimiter-short); and it counted the delimiter row's cells without shaping them, so a cell `:`, a blank
    one or `-:-`, each of which makes GFM render no table, passed (read B-f13, N1: B14-trprobe-delimiter-colon-cell,
    B14-trprobe-delimiter-blank-cell, B14-trprobe-delimiter-dash-colon-dash); and an escaped backtick (`\\``) in a
    row passed every row, though GFM reads it as text: the span it seems to open does not, each backtick after it
    pairs with the next, and the `<worktree>` of the commands after it reads as an HTML tag (read B-f14, N4:
    B15-tr-escaped-backtick); and a backtick dropped from a row (or one too many) shifts the spans just so and
    passed (read B-f15, N3: B16-tr-backtick-dropped)."""
    with open(os.path.join(ROOT, "docs", "troubleshooting.md"), encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    bad, header, delimiter, prev = [], None, False, ""
    for n, line in enumerate(lines, 1):
        if line.startswith("|") and _unpaired_backtick_runs(line):
            bad.append((n, "a backtick run that opens no code span", _unpaired_backtick_runs(line)))
        if not line.startswith("|"):
            if prev.startswith("|") and line.strip():
                bad.append((n, "a line that is not a row, right after one", line[:40]))
            header, prev = None, line
            continue
        if header is None:
            header, delimiter = len(_cells(line)), True
        elif delimiter:
            delimiter = False
            if (not re.match(r"^\|[\s:|-]+\|?\s*$", line) or len(_cells(line)) != header
                    or not all(re.fullmatch(r"\s*:?-+:?\s*", c) for c in _cells(line))):
                bad.append((n, "a delimiter row without its header's cells", len(_cells(line)), header))
        elif len(_cells(line)) != header or "\\\\|" in line or "\\`" in line:
            bad.append((n, len(_cells(line)), header))
        prev = line
    assert not bad, bad


def test_troubleshootings_t25_rows_quote_gitios_commands():
    """Troubleshooting §10's rows that print the listing (the sync's rebase-in-progress refusal, its could-not-replay
    refusal, the stop's, and the deploy's busy refusal) hold gitio's `_LIST_FLAGGED` for `<worktree>`, its pipe
    escaped for the table, and `_BASHS` after it; the two that give the compares hold each command of
    `_PUT_HEADS_BACK` and `_QUOTED_NAME`'s way to give a quoted name back; the busy row says `$'…'` with each `'`
    written `\\'`; and every code span of the file that pipes `ls-files -v` is gitio's `_LIST_FLAGGED` for
    `<worktree>`, its pipe escaped. Guards §10's text, which no row read: its listing could lose `-c
    core.quotePath=true`, its `\\'` clause go or its compares lose `-n`, and pass every row (read B-f11, N1: the B12-tr*
    mutants); and the rows are found by their start, so a fifth that printed fold 9's listing passed (read B-f12, N3:
    B13-trprobe-fifth-row-old-listing)."""
    listing = "`" + gitio._LIST_FLAGGED.format(wt="<worktree>").replace("|", "\\|") + "`" + gitio._BASHS
    commands = [c.replace("|", "\\|") for c in re.findall(r"`([^`]+)`", gitio._PUT_HEADS_BACK.format(wt="<worktree>"))
                if c.startswith("git ")]
    assert len(commands) == 3, commands
    with open(os.path.join(ROOT, "docs", "troubleshooting.md"), encoding="utf-8") as fh:
        text = fh.read().split("\n")

    def row(start):
        rows = [line for line in text if line.startswith("| `" + start)]
        assert len(rows) == 1, (start, len(rows))
        return rows[0]
    rebase = row("kontroll-land: a rebase, merge, cherry-pick, revert or bisect is in progress")
    replay = row("kontroll-land: git could not replay HEAD's commits")
    stop = row("bootstrap: STOPPED before landing")
    busy = row("local-canonical: REFUSING to commit or land — git has an operation in progress")
    for r in (rebase, replay, stop, busy):
        assert listing in r, r[:80]
    for r in (rebase, replay):
        for c in commands:
            assert c in r, (c, r[:80])
        assert gitio._QUOTED_NAME.split("C way: ")[1] in r, r[:80]
    assert "given back as `$'…'`, each `'` in it written `\\'`" in busy, busy[:80]
    spans = re.findall(r"`([^`]*ls-files -v[^`]*\|[^`]*)`", "\n".join(text))
    assert spans and all(s == gitio._LIST_FLAGGED.format(wt="<worktree>").replace("|", "\\|") for s in spans), spans


@pytest.mark.parametrize("past", [1, 2])
def test_a_shallow_clone_whose_cut_is_above_main_is_refused_with_the_shallow_sentence(box, tmp_path, past):
    """A `--depth 1` clone of the worktree after `past` commits of its own on top of the canonical's `main` (an
    upgrade, no promote): `main` is below where the clone stops. The check says it would bring forward, and that the
    worktree is a shallow clone with `main` not in it, naming `git fetch --unshallow`; the sync refuses with the
    shallow sentence — the clone's path quoted in `git -C … fetch --unshallow`, "nothing was replayed and HEAD was not
    changed" — never "share no history", and HEAD is where it was; after `fetch --unshallow` the sync says the
    worktree is in step. Guards the sync's shallow refusal (read A-r21, S1): it said the worktree shares no history
    with the canonical, which sends the operator to re-clone a checkout that is theirs, and the dry run said nothing
    of it."""
    for n in range(past):
        _commit(box.wt, {"own-%d.txt" % n: "%d\n" % n}, "the worktree's own %d" % n)
    clone = str(tmp_path / "a shallow clone")              # a path the remedy must quote
    subprocess.run(["git", "clone", "-q", "--depth", "1", pathlib.Path(box.wt).as_uri(), clone], check=True,
                   capture_output=True)
    _git(clone, "config", "core.autocrlf", "false")
    _git(clone, "remote", "add", "local", box.bare)
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    head = _git(clone, "rev-parse", "HEAD")
    rc, said = gitio.sync_worktree(clone, box.bare, check=True)
    assert rc == 3 and "The worktree is a shallow clone, and main is not in it" in said, said
    assert "`git fetch --unshallow`" in said, said
    rc, said = gitio.sync_worktree(clone, box.bare)
    assert rc == 1 and "is a shallow clone" in said, said
    assert "`git -C %s fetch --unshallow`" % shlex.quote(clone) in said, said
    assert "nothing was replayed and HEAD was not changed" in said and "share no history" not in said, said
    assert _git(clone, "rev-parse", "HEAD") == head
    _git(clone, "fetch", "-q", "--unshallow")
    rc, said = gitio.sync_worktree(clone, box.bare)
    assert rc == 0 and said.startswith("in step"), said
