"""discard — the ONE write verb in the pending surface: delete a STALE proposal's ref (FF-race Move 2 / C10).

These pin `service/discard.discard_proposal`, the reaper for the FF-race: a proposal `main` advanced past is
non-fast-forwardable and can never be promoted, so it lingers as dead weight in the Pending list. discard removes
its `refs/heads/proposed/<run_id>` ref from the canonical. It is C10-SAFE (un-stage != promote: it deletes Key-1's
OWN output; it cannot advance main / enact / grow the promotable set) — so it lives OUTSIDE the read-only pending.py
in its own module, registered in WRITE_VERBS. The security-critical surface is the run_id → git argv path; these
tests are the design-of-record's must-fixes made executable (SEC-B1 both-minter validator + argv-injection refusal,
SEC-M1 compare-and-delete, MF-4 single-literal argv → the completeness gate sees the verb) — and the rule that what
it deletes is the ref of that NAME: a symbolic ref under a proposal's name is refused, never followed to the ref
it points at, and no hook of the repository runs on the delete.
"""
import ast
import inspect
import os
import re
import subprocess
import time

import pytest

import _canon_repo as R
from _readonly_pins import WRITE_VERBS, assert_write_verbs_complete
from kontroll import canon
from kontroll.service import discard

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True, text=True)


@pytest.fixture
def bare_with_proposal(tmp_path):
    """A throwaway CANONICAL bare repo with `main` + one `proposed/<run_id>` ref (a 1-commit FF over main). Returns
    (bare_path, run_id) so a test can discard it and assert the ref is gone. A local filesystem path is the push
    target (cross-platform; no file:// URL)."""
    bare = tmp_path / "canonical.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "seed.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "push", str(bare), "HEAD:refs/heads/main")
    run_id = "abc123def456"
    (work / "p.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "feat(onboard): stage a proposal")
    _git(work, "push", str(bare), "HEAD:refs/heads/proposed/%s" % run_id)
    return bare, run_id


def _ref_exists(bare, name):
    """Does `name` — a run id (the proposal of that id) or a full ref name — exist in `bare`?"""
    full = name if name.startswith("refs/") else "refs/heads/proposed/%s" % name
    p = subprocess.run(["git", "-C", str(bare), "show-ref", "--verify", "--quiet", full], capture_output=True)
    return p.returncode == 0


def test_discards_a_staged_proposal(bare_with_proposal):
    """The happy path: discard_proposal deletes the `proposed/<run_id>` ref and returns {ok, run_id, ref, sha} —
    the ref by its full name, the sha the whole commit id; the ref no longer resolves in the canonical afterward.
    Guards the core reaper against silently no-op'ing (a promoted or lingering stale proposal must actually leave
    the list)."""
    bare, run_id = bare_with_proposal
    assert _ref_exists(bare, run_id)
    out = discard.discard_proposal(run_id, canonical=str(bare))
    assert out["ok"] is True and out["run_id"] == run_id and out["ref"] == "refs/heads/proposed/%s" % run_id
    assert re.fullmatch(r"[0-9a-f]{40}", out["sha"])          # the deleted tip, echoed for audited recovery
    assert not _ref_exists(bare, run_id)                      # actually gone from the canonical
    assert _ref_exists(bare, canon.MAIN)


def test_invalid_run_id_is_400_before_any_git(bare_with_proposal):
    """An invalid run_id returns code 400 and NEVER reaches git — the proposal ref is untouched. Guards the argv
    boundary: a `-d`/`../main` can't slip through to `git update-ref` even if the canonical exists. (The validator
    itself — both minters, every refused shape — is pinned in test_canon.py, where it now lives.)"""
    bare, run_id = bare_with_proposal
    for bad in ("../main", "-d", "main", "abc123def456\n", "proposed/abc123def456", None):
        out = discard.discard_proposal(bad, canonical=str(bare))
        assert out["ok"] is False and out["code"] == 400, "must refuse %r" % (bad,)
    assert _ref_exists(bare, run_id)                          # the real proposal is untouched


def test_nonexistent_proposal_is_404(bare_with_proposal):
    """A valid-SHAPED run_id that names no ref → code 404 (already promoted/discarded/never staged), not a 500 and
    not a false success. Guards the compare-and-delete's read leg: no ref → nothing to delete."""
    bare, _ = bare_with_proposal
    out = discard.discard_proposal("ffffffffffff", canonical=str(bare))
    assert out["ok"] is False and out["code"] == 404


def test_missing_canonical_is_404(tmp_path):
    """An absent canonical path → code 404 (never raises). Guards the degrade posture on an unarmed deploy."""
    out = discard.discard_proposal("abc123def456", canonical=str(tmp_path / "does-not-exist"))
    assert out["ok"] is False and out["code"] == 404


def test_compare_and_delete_guards_a_concurrently_moved_ref(bare_with_proposal, monkeypatch):
    """SEC-M1/TOCTOU: the delete is conditional on the value read first. If the ref MOVES between the read and the
    delete (a concurrent promote/rebase), `git update-ref -d <ref> <stalesha>` refuses → code 409, and the ref is
    LEFT intact (never blind-deleted). Simulated by making the read return a stale (wrong) sha so the compare
    fails — proving the oldsha guard is actually load-bearing, not decorative."""
    bare, run_id = bare_with_proposal
    monkeypatch.setattr(canon, "read_ref", lambda cdir, ref: ("0" * 39 + "1", "commit", ""))   # plausible, wrong
    out = discard.discard_proposal(run_id, canonical=str(bare))
    assert out["ok"] is False and out["code"] == 409
    assert _ref_exists(bare, run_id)                          # the guard refused — ref preserved


def test_a_symbolic_ref_under_a_proposals_name_is_refused_and_main_survives(tmp_path):
    """A ref `refs/heads/proposed/<id>` that is a SYMBOLIC ref to `refs/heads/main` is refused (404, the existing
    "no such proposal" shape), and afterwards `main` still exists and so does the symbolic ref. Guards the worst
    thing this verb could do: `git update-ref -d` on a symbolic ref deletes the ref it POINTS at, so a discard of
    such a "proposal" deleted `main` and left the pointer in place — by the hand of whoever clicked Discard."""
    r = R.build(tmp_path)
    rid = "bbbbbbbbbbbb"
    R.write_ref_file(r.bare, canon.PROPOSED + rid, "ref: " + canon.MAIN)
    out = discard.discard_proposal(rid, canonical=r.bare)
    assert out["ok"] is False and out["code"] == 404 and "not one" in out["error"]
    assert canon.read_ref(r.bare, canon.MAIN) == (r.main, "commit", "")
    assert os.path.exists(os.path.join(r.bare, "refs", "heads", "proposed", rid))


def test_the_delete_never_follows_a_ref_that_turned_symbolic_after_the_read(tmp_path, monkeypatch):
    """If the ref is an ordinary one when it is read and a symbolic ref to `main` by the time it is deleted, the
    delete removes the NAME it was given: `main` survives. Simulated by having the read report the symbolic ref as
    an ordinary ref at `main`'s commit. Guards `--no-deref` on the delete — the check before it is a check at one
    moment, and without the flag the same swap, timed between the two commands, deletes `main`."""
    r = R.build(tmp_path)
    rid = "bbbbbbbbbbbb"
    R.write_ref_file(r.bare, canon.PROPOSED + rid, "ref: " + canon.MAIN)
    monkeypatch.setattr(canon, "read_ref", lambda cdir, ref: (r.main, "commit", ""))
    discard.discard_proposal(rid, canonical=r.bare)
    monkeypatch.undo()
    assert canon.read_ref(r.bare, canon.MAIN) == (r.main, "commit", ""), "the delete followed the pointer to main"


def test_a_ref_that_names_a_tag_is_not_a_proposal_and_is_not_deleted(tmp_path):
    """A ref under a proposal's name whose value is a TAG object is refused (404) and left in place. Guards the
    type half of "the ref's own value": a proposal names a commit, and a reader that peeled the tag would have
    compared-and-deleted on an id the ref does not hold."""
    r = R.build(tmp_path)
    rid = "cccccccccccc"
    tag = R.git(r.bare, "mktag", stdin=("object %s\ntype commit\ntag t\ntagger t <t@example.test> 1750000000 +0000"
                                        "\n\nt\n" % r.ff).encode()).decode()
    R.write_ref_file(r.bare, canon.PROPOSED + rid, tag)
    out = discard.discard_proposal(rid, canonical=r.bare)
    assert out["ok"] is False and out["code"] == 404
    assert canon.read_ref(r.bare, canon.PROPOSED + rid) == (tag, "tag", "")


def test_no_hook_of_the_canonical_runs_on_a_discard(tmp_path):
    """A `reference-transaction` hook in the canonical's `hooks/`, and one in a directory its config names, do not
    run when a proposal is discarded; the discard still succeeds. Guards the seam on the one write of this surface:
    the delete is run by the GUI's worker for whoever clicked, in a repository the service can write, and the hook
    is a program that repository chose. (The control — the hook does run outside the seam — is in test_canon.py.)"""
    r = R.build(tmp_path)
    canary = tmp_path / "ran"
    R.plant_hook(r, canary)
    R.plant_hook(r, canary, directory=str(tmp_path / "named-hooks"))
    out = discard.discard_proposal(R.STALE, canonical=r.bare)
    assert out["ok"] is True and out["sha"] == r.stale and canon.read_ref(r.bare, canon.PROPOSED + R.STALE) is None
    assert not canary.exists(), "a hook of the canonical ran during the discard"


def test_a_canonical_that_cannot_be_read_is_409_and_nothing_is_deleted(tmp_path):
    """A canonical with a `commondir` file — which the seam refuses, because its refs would be another directory's
    — makes the discard answer 409 with the reason, and the proposal is still there once the file is gone. Guards
    the difference between "no such proposal" and "the repository was not read": the first invites a refresh, the
    second must not be mistaken for it, and neither may delete."""
    r, other = R.build(tmp_path), R.build(tmp_path, "planted")
    R.plant_commondir(r, other.bare)
    out = discard.discard_proposal(R.STALE, canonical=r.bare)
    assert out["ok"] is False and out["code"] == 409 and "commondir" in out["error"]
    os.remove(os.path.join(r.bare, "commondir"))
    assert canon.read_ref(r.bare, canon.PROPOSED + R.STALE) == (r.stale, "commit", "")
    assert canon.read_ref(other.bare, canon.PROPOSED + R.STALE) == (other.stale, "commit", "")


def test_a_held_lock_is_a_409_that_leaves_no_lock_of_its_own(tmp_path):
    """With `packed-refs.lock` held in the canonical, a discard answers 409 — saying the canonical may be locked —
    within a few seconds, leaves no `<proposal>.lock` behind, and succeeds once the lock is gone. Guards the
    seam's lock timeouts: git used to wait for the lock for as long as the repository's config said, the caller
    killed it at its own timeout, and a killed git leaves its lock files — after which that proposal could be
    neither discarded nor cleaned up by a promote."""
    r = R.build(tmp_path)
    held = os.path.join(r.bare, "packed-refs.lock")
    R.git(r.bare, "config", "core.packedRefsTimeout", "-1")           # "wait for ever", as a repository may say
    with open(held, "w", encoding="ascii") as fh:
        fh.write("held")
    began = time.monotonic()
    out = discard.discard_proposal(R.STALE, canonical=r.bare)
    assert out["ok"] is False and out["code"] == 409 and "locked" in out["error"]
    assert time.monotonic() - began < 4.5, "git was killed at the caller's timeout instead of giving up by itself"
    assert not os.path.exists(os.path.join(r.bare, "refs", "heads", "proposed", R.STALE + ".lock"))
    os.remove(held)
    assert discard.discard_proposal(R.STALE, canonical=r.bare)["ok"] is True


def test_a_canonical_refused_between_the_read_and_the_delete_is_409_and_nothing_is_deleted(tmp_path, monkeypatch):
    """When the seam refuses the repository for the DELETE after the read went through — a `commondir` planted
    between the two git processes — the discard answers 409 and the proposal is still there. Simulated by letting
    `git_kwargs` answer once and refuse from then on. Guards the second of the two places the refusal can land:
    let out of the verb it is a 500 from the route, and answered as 404 it tells the operator the proposal is gone
    when nothing was deleted."""
    r = R.build(tmp_path)
    real, calls = canon.git_kwargs, []

    def refuses_later(repo):
        calls.append(repo)
        if len(calls) > 1:
            raise canon.Unreadable("planted between the read and the delete")
        return real(repo)

    monkeypatch.setattr(canon, "git_kwargs", refuses_later)
    out = discard.discard_proposal(R.STALE, canonical=r.bare)
    monkeypatch.setattr(canon, "git_kwargs", real)
    assert out["ok"] is False and out["code"] == 409 and len(calls) == 2
    assert canon.read_ref(r.bare, canon.PROPOSED + R.STALE) == (r.stale, "commit", "")


def test_delete_argv_is_a_single_literal_with_the_oldsha(monkeypatch):
    """MF-4 + SEC-M1 (STRUCTURAL): the `git update-ref -d` call in discard.py is a SINGLE list literal (never a
    `[...] + [...]` concat) so the completeness gate's `_static_list_prefix` sees `update-ref`; and it carries
    `-d`, `--no-deref` and the positional oldsha (compare-and-delete of the name given, not an unconditional delete
    and not a delete through a pointer). Parsed from the AST so a refactor that split the argv — hiding the verb
    from the pin or dropping the oldsha — fails HERE."""
    src = inspect.getsource(discard.discard_proposal)
    tree = ast.parse(src)
    update_ref_lists = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            consts = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            if "update-ref" in consts:
                update_ref_lists.append((node, consts))
    assert update_ref_lists, "no `update-ref` list literal found in discard_proposal"
    node, consts = update_ref_lists[0]
    assert consts[:2] == ["git", "update-ref"] and "-d" in consts and "--end-of-options" in consts
    assert "--no-deref" in consts                              # the name given, never the ref it points at
    # the LAST two argv slots are the compare-and-delete operands (<ref>, <oldsha>) — variables, not constants; and
    # `--end-of-options` sits immediately before them so a leading-`-` leaf can never be parsed as a flag.
    assert isinstance(node.elts[-1], ast.Name) and isinstance(node.elts[-2], ast.Name), \
        "expected the ref + oldsha as the two trailing variable operands (compare-and-delete)"
    assert isinstance(node.elts[-3], ast.Constant) and node.elts[-3].value == "--end-of-options"


def test_discard_proposal_is_registered_and_the_completeness_gate_enforces_it(monkeypatch):
    """The read-only pin: `discard_proposal` is in WRITE_VERBS (so a read view that ever calls it trips
    assert_read_only), AND the fail-closed completeness gate would NAME it if it were unregistered — proving MF-4's
    single-literal argv makes the `update-ref` write visible to the detector (P0b/#134). Un-register it in a copy and
    assert the gate fails pointing at discard.py:discard_proposal."""
    assert "discard_proposal" in WRITE_VERBS
    import _readonly_pins as rp
    monkeypatch.setattr(rp, "WRITE_VERBS", WRITE_VERBS - {"discard_proposal"})
    with pytest.raises(AssertionError) as ei:
        assert_write_verbs_complete()
    assert "discard.py:discard_proposal" in str(ei.value)      # the gate names the exact unregistered writer
