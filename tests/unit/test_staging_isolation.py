"""Staging isolation — the content clone rolls back to canonical main after each stage (F1, dogfood 2026-06-20).

These pin gitio.commit_and_push's STAGING behaviour: after pushing a `proposed/<run_id>` proposal it returns the
content clone's working tree to the canonical `main` (`_reset_content_clone_to_canonical`). WHY (the bug they
guard, live-caught on a real deploy): commit_and_push commits each staged reconfigure on the clone's LOCAL `main`,
advancing it; without the reset the clone drifts forward with every stage, so a fresh `build_plan` reads the
drifted tree — proposals STACK (B's ref = `main + A + B`), a REJECTED edit keeps contaminating later diffs, and —
because promote is FF-only — promoting B silently also enacts the abandoned A. The unit suite uses a fresh tmp
repo per test (monkeypatched `paths.ROOT`), so it could never see this cross-REQUEST accumulation; the second test
here drives TWO stages through ONE persistent real clone, exactly the shape that exposed it. The operator CLI
(target == `main`) must NEVER be reset — covered by the seam test. C10 / docs/privileged-mutation-enablement.md.
"""
import os
import shutil
import subprocess
import sys

import pytest

from kontroll import gitio, paths

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git(cwd, *args, capture=False):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), check=True,
                          capture_output=True, text=True).stdout.strip() if capture else \
        subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True, text=True)


# --- the seam: the reset is STAGING-ONLY, runs AFTER the push, and never touches the operator CLI ----------

def test_staging_resets_clone_to_canonical_after_push_but_cli_never_does(monkeypatch):
    """In staging mode (KONTROLL_STAGE_PUSHES set + a run_id) commit_and_push issues `git fetch local` then
    `git reset --hard local/main` AFTER the canonical push — so the clone returns to the source of truth. The
    operator CLI path (no env → target `main`) issues NEITHER (it advances `main` and owns its tree). Guards the
    gating: the roll-back must be scoped to the staging content clone, never the CLI's working tree."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)

    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    gitio.commit_and_push(["x.yml"], ["m"], run_id="deadbeef0001")
    joined = [" ".join(c) for c in calls]
    push_i = next(i for i, j in enumerate(joined) if "push local HEAD:refs/heads/proposed/" in j)
    assert any(j == "git fetch local" for j in joined[push_i:])             # reset comes AFTER the push
    assert any(j == "git reset --hard local/main" for j in joined[push_i:])

    calls.clear()
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    gitio.commit_and_push(["x.yml"], ["m"])                                 # CLI: target == main
    joined = [" ".join(c) for c in calls]
    assert not any("reset --hard" in j for j in joined)                    # the CLI tree is NEVER reset
    assert not any(j == "git fetch local" for j in joined)


def test_failed_commit_in_staging_still_resets_the_dirty_tree(monkeypatch):
    """A staged commit that FAILS (e.g. nothing staged / a hook) still leaves the apply's write in the tree, so
    staging must reset even on the failure path (drop the half-written change) — else the next read is dirty.
    Guards the failure branch of the roll-back (the success branch is covered above + by the real-clone test)."""
    calls = []

    def fake_run(cmd, cwd=None, quiet_args=0):
        calls.append(list(cmd))
        return 1 if cmd[:2] == ["git", "commit"] else 0                    # commit fails; everything else OK

    monkeypatch.setattr(gitio, "_run", fake_run)
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    out = gitio.commit_and_push(["x.yml"], ["m"], run_id="deadbeef0002")
    assert out["committed"] is False
    joined = [" ".join(c) for c in calls]
    assert not any("push local" in j for j in joined)                      # a failed commit pushes NOTHING
    assert any(j == "git reset --hard local/main" for j in joined)         # …but still drops the dirty tree


# --- the real bug: two stages through ONE persistent clone → proposals are INDEPENDENT, not stacked ---------

@pytest.fixture
def content_clone(tmp_path, monkeypatch):
    """A real canonical bare repo + a content clone with a `local` remote → canonical (mirrors the armed deploy:
    the clone the GUI/API stages from). Seeds `state.yml`, points `paths.ROOT` at the clone, arms staging.
    Returns (clone_path, canonical_path)."""
    canonical = tmp_path / "canonical.git"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(canonical)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    (clone / "state.yml").write_text("remotes: []\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "seed")
    _git(clone, "remote", "add", "local", str(canonical))
    _git(clone, "push", "local", "main")
    monkeypatch.setattr(paths, "ROOT", str(clone))
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    return clone, canonical


def _show(repo, ref_path):
    return subprocess.run(["git", "-C", str(repo), "show", ref_path], check=True,
                          capture_output=True, text=True).stdout


def test_two_stages_are_independent_and_the_clone_never_drifts(content_clone):
    """Stage A then stage B through the ONE persistent clone (no manual reset between). Each `proposed/<run_id>`
    must be a DIRECT child of canonical `main` carrying ONLY its own change (B is NOT `main + A + B`), the clone's
    working tree must return to the canonical seed after every stage (clean, no drift), and REJECTING A (deleting
    its canonical ref) must not contaminate the clone. This is the live-caught F1 stacking/contamination bug; the
    fresh-tmp-repo-per-test unit suite can't see it because the drift is cross-request on one clone."""
    clone, canonical = content_clone
    seed = "remotes: []\n"

    # Stage A
    (clone / "state.yml").write_text("remotes: [a]\n", encoding="utf-8")
    a = gitio.commit_and_push(["state.yml"], ["stage A"], run_id="aaaaaaaa0001")
    assert a["staged"] is True and a["target_ref"] == "proposed/aaaaaaaa0001"
    # the clone rolled BACK to canonical main — no drift
    assert (clone / "state.yml").read_text(encoding="utf-8") == seed
    assert _git(clone, "status", "--porcelain", capture=True) == ""        # clean working tree
    assert _git(clone, "rev-parse", "HEAD", capture=True) == _git(clone, "rev-parse", "local/main", capture=True)

    # Stage B — computed off the (reset) clone, so it must be INDEPENDENT of A
    (clone / "state.yml").write_text("remotes: [b]\n", encoding="utf-8")
    b = gitio.commit_and_push(["state.yml"], ["stage B"], run_id="bbbbbbbb0002")
    assert b["target_ref"] == "proposed/bbbbbbbb0002"

    # proposal B is a DIRECT child of canonical main (not stacked on A) and carries only its own change
    main_sha = _git(canonical, "rev-parse", "main", capture=True)
    assert _git(canonical, "rev-parse", "proposed/bbbbbbbb0002^", capture=True) == main_sha
    assert _show(canonical, "proposed/bbbbbbbb0002:state.yml") == "remotes: [b]\n"
    assert _show(canonical, "proposed/bbbbbbbb0002^:state.yml") == seed     # its parent is the clean seed, NOT [a]
    # and A is likewise independent (its own ref still carries only [a])
    assert _show(canonical, "proposed/aaaaaaaa0001:state.yml") == "remotes: [a]\n"

    # Reject A (the operator deletes the canonical proposal) — the clone must stay clean (no contamination)
    subprocess.run(["git", "-C", str(canonical), "update-ref", "-d", "refs/heads/proposed/aaaaaaaa0001"],
                   check=True, capture_output=True)
    assert (clone / "state.yml").read_text(encoding="utf-8") == seed


def _is_ancestor(repo, anc, desc):
    """True iff `anc` is a strict ancestor of `desc` in `repo` (i.e. `desc` is a fast-forward of `anc`)."""
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", anc, desc],
                          capture_output=True).returncode == 0


def _promote_out_of_band(canonical, run_id):
    """Advance canonical `main` to proposed/<run_id> then delete the consumed ref — exactly what the operator's
    `kontroll-promote` (gitio.promote_ref) does, but driven from outside the clone so the clone's tracking ref goes
    stale (the real ordering: the operator promotes between two GUI stages)."""
    subprocess.run(["git", "-C", str(canonical), "update-ref", "refs/heads/main",
                    "refs/heads/proposed/%s" % run_id], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(canonical), "update-ref", "-d", "refs/heads/proposed/%s" % run_id],
                   check=True, capture_output=True)


def test_disjoint_stage_after_an_out_of_band_promote_stays_a_fast_forward(content_clone):
    """Stage A (adds file dev_a), PROMOTE A out-of-band (advance canonical `main` to A + delete proposed/A — exactly
    what `kontroll-promote` does), then stage B (adds a DIFFERENT file dev_b) through the SAME persistent clone. With
    A and B touching DISJOINT files — the common real case (onboard device A then device B = different inventory files)
    — proposed/B must be a strict FAST-FORWARD of the now-advanced canonical `main` (its parent IS the promoted-A
    commit), so `promote_ref` accepts it without a manual re-sync.

    WHY (the bug this guards — dogfood-caught 2026-06-29 on the .52 baked prod box, task_fd3b458d): the clone commits
    each proposal on its LOCAL `main`, which the post-stage reset last set to `local/main` BEFORE A was promoted. With
    no pre-push rebase, stage B commits on the stale pre-A `main` and pushes `proposed/B = seed+B` — NOT a FF of the
    promoted canonical `main` (= seed+A) — so every promote after the first failed 'not a fast-forward; re-propose',
    forcing a manual `git fetch && reset --hard local/main` between promotes. `_rebase_proposal_onto_canonical` rebases
    the single proposal commit onto current `local/main` so proposed/B = seed+A+B'. The pre-existing two-stages test
    can't catch this because it never promotes A (canonical main never moves), so a stale base is coincidentally a FF."""
    clone, canonical = content_clone

    (clone / "dev_a.yml").write_text("a\n", encoding="utf-8")            # stage A adds a NEW file
    gitio.commit_and_push(["dev_a.yml"], ["stage A"], run_id="aaaaaaaa0001")
    _promote_out_of_band(canonical, "aaaaaaaa0001")
    main_after_a = _git(canonical, "rev-parse", "main", capture=True)

    (clone / "dev_b.yml").write_text("b\n", encoding="utf-8")            # stage B adds a DIFFERENT new file
    gitio.commit_and_push(["dev_b.yml"], ["stage B"], run_id="bbbbbbbb0002")

    # proposed/B is a strict FF of the PROMOTED canonical main (parent IS promoted-A), so promote accepts it
    assert _is_ancestor(canonical, "refs/heads/main", "refs/heads/proposed/bbbbbbbb0002"), \
        "proposed/B is not a fast-forward of the promoted canonical main — the rebase did not re-base it"
    assert _git(canonical, "rev-parse", "proposed/bbbbbbbb0002^", capture=True) == main_after_a
    assert _show(canonical, "proposed/bbbbbbbb0002:dev_b.yml") == "b\n"          # carries B's change…
    assert _show(canonical, "proposed/bbbbbbbb0002:dev_a.yml") == "a\n"          # …on top of promoted A (rebased)


def test_conflicting_stage_after_promote_is_left_non_ff_not_silently_resolved(content_clone):
    """The HONEST boundary: when proposal B and the promoted change A touch the SAME lines (a genuine conflict), the
    rebase is ABORTED and B pushed as-is, so proposed/B is left a NON-fast-forward — `promote_ref`'s FF-gate then
    correctly refuses it and the operator re-proposes against current main. WHY this is a test (not a gap): a
    'helpful' rebase that force-resolved the conflict would SILENTLY drop or clobber the already-promoted change A —
    the rebase must degrade to today's safe non-FF, never auto-merge. Pins that the fix only FF-ifies disjoint
    proposals and never papers over a real divergence."""
    clone, canonical = content_clone

    (clone / "state.yml").write_text("remotes: [a]\n", encoding="utf-8")     # A and B edit the SAME line
    gitio.commit_and_push(["state.yml"], ["stage A"], run_id="aaaaaaaa0001")
    _promote_out_of_band(canonical, "aaaaaaaa0001")

    (clone / "state.yml").write_text("remotes: [b]\n", encoding="utf-8")
    gitio.commit_and_push(["state.yml"], ["stage B"], run_id="bbbbbbbb0002")

    # the rebase aborted (genuine conflict) → proposed/B is NOT a FF of the promoted main; promote will (correctly) refuse
    assert not _is_ancestor(canonical, "refs/heads/main", "refs/heads/proposed/bbbbbbbb0002"), \
        "a conflicting proposal must be left non-FF (re-propose), never silently rebased over the promoted change"
    # and the promoted change A is intact on main — never clobbered by a forced resolve
    assert _show(canonical, "main:state.yml") == "remotes: [a]\n"


# --- the C10 origin-push breach: `origin` IS the canonical, so an origin HEAD push advanced main (VM 144) -------

@pytest.fixture
def content_clone_with_origin(content_clone):
    """The staging content clone as the ARMED DEPLOY actually builds it: BOTH `origin` AND `local` remotes point at
    the SAME bare canonical. The deploy's `git clone file:///srv/kontroll.git` sets `origin`→the canonical
    (deploy-stack.yml api ~812, gui ~931), then local-canonical adds a `local` remote → the same canonical. The base
    `content_clone` fixture models only `local`; this adds the missing `origin`, reproducing the origin-push-breach
    topology (the checked-out branch is `main`, so `git push origin HEAD` targets canonical `refs/heads/main`).
    Returns (clone, canonical)."""
    clone, canonical = content_clone
    _git(clone, "remote", "add", "origin", str(canonical))     # origin == the SAME canonical as `local` (the breach)
    return clone, canonical


def test_staging_push_origin_never_advances_canonical_main(content_clone_with_origin):
    """C10 origin-push breach (secure-edition dogfood, VM 144, 2026-07-05): a STAGING service (KONTROLL_STAGE_PUSHES
    set) that calls commit_and_push with push_origin=True MUST park a `proposed/<run_id>` proposal and leave canonical
    `refs/heads/main` UNCHANGED — it must NEVER advance main without a human promote.

    WHY (the failure this guards): the content clone's `origin` remote points at the SAME bare canonical as `local`
    and its checked-out branch is `main`, so gitio.commit_and_push's optional `git push origin HEAD` (the
    `push_origin` leg) is a clean fast-forward that rewrites canonical `refs/heads/main` — defeating
    propose-then-promote (SECURITY.md C10). The documented `apply:true, push:true` flow (route
    `push_origin=bool(d.get("push"))`) thus silently promoted. This builds the real origin==canonical topology (which
    the mock `_run` seam erases and the base content_clone fixture omits) so the origin push acts on a real object
    database — the direct analogue of the VM-144 experiment (main 2aa9066 -> d4d0345). The fix suppresses the origin
    push under staging (`and not staging`); the proposal (the `local` push) is the only thing that reaches the
    canonical. FAILS pre-fix (main advances, pushed_origin True); PASSES post-fix."""
    clone, canonical = content_clone_with_origin
    main_before = _git(canonical, "rev-parse", "refs/heads/main", capture=True)

    (clone / "state.yml").write_text("remotes: [x]\n", encoding="utf-8")
    out = gitio.commit_and_push(["state.yml"], ["stage X"], push_origin=True, run_id="cafe00000001")

    # the proposal WAS parked (staging still works — only the origin push is neutered, not the local staging push)
    assert out["staged"] is True and out["target_ref"] == "proposed/cafe00000001"
    assert _git(canonical, "rev-parse", "refs/heads/proposed/cafe00000001", capture=True)      # proposal ref exists
    assert _show(canonical, "proposed/cafe00000001:state.yml") == "remotes: [x]\n"
    assert out["pushed_origin"] is False                                          # nothing pushed to origin under staging

    # THE LOAD-BEARING ASSERTION: canonical main is byte-for-byte unchanged — no human promote happened
    main_after = _git(canonical, "rev-parse", "refs/heads/main", capture=True)
    assert main_after == main_before, (
        "STAGING push_origin=True advanced canonical main (%s -> %s) — the C10 origin-push breach; a staging service "
        "must not push HEAD to origin while staging" % (main_before, main_after))


def test_operator_cli_push_origin_still_advances_main(content_clone_with_origin, monkeypatch):
    """The positive control for the C10 origin-push fix (constraint C3): with KONTROLL_STAGE_PUSHES UNSET (the
    GUI run from a checkout / a non-staging deploy, target == main) commit_and_push(push_origin=True) MUST still
    advance canonical `main` — by a LANDING now (`land_main`: judged, a pack on a pipe, compare-and-swapped; the
    canonical's `main` moves and `pushed_canonical` is True) — and still push origin: the fix suppresses the
    origin push ONLY under staging, never the legitimate non-staging path. Guards an over-correction (a blanket
    'never push origin') that would silently break the operator's offsite backup push, and that the landing
    reaches `main` where the push used to. Real-git so 'advanced main' is observed on a real object DB — the exact
    contrast to test_staging_push_origin_never_advances_canonical_main. (Here `origin` IS the canonical, so the
    origin push of a `main` that the landing already moved is a no-op: with the canonical's hook installed it would
    be refused — `tests/unit/test_land.py` holds that door with the hook in place.)"""
    clone, canonical = content_clone_with_origin
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)          # CLI / non-staging deploy -> target == main
    main_before = _git(canonical, "rev-parse", "refs/heads/main", capture=True)

    (clone / "state.yml").write_text("remotes: [cli]\n", encoding="utf-8")
    out = gitio.commit_and_push(["state.yml"], ["operator cli edit"], push_origin=True)

    assert out["staged"] is False and out["target_ref"] == "main"
    assert out["pushed_canonical"] is True and out["pushed_origin"] is True   # the sharp C3 guard: origin push fired
    main_after = _git(canonical, "rev-parse", "refs/heads/main", capture=True)
    assert main_after != main_before, \
        "the non-staging path must still advance main (C3) — by the landing; the fix must scope suppression to staging"
    assert _show(canonical, "main:state.yml") == "remotes: [cli]\n"


# --- the canonical update-hook (defence-in-depth): tests the REAL committed artifact, not a twin (MF-1) ---------

_HOOK_SRC = os.path.join(ROOT, "ansible", "playbooks", "files", "canonical-update-hook.sh")


def _fake_id_bin(tmp_path):
    """A tmp dir holding an `id` executable that prints $FAKE_UID — PATH-shadowed so the REAL hook's unspoofable
    `$(id -u)` resolves to a uid the test controls (a unit test can't actually be uid 1001, and the production hook
    intentionally has NO variable of its own that changes the answer). With FAKE_UID empty it fails, as an `id`
    that cannot be run does; with FAKE_ID_RC set it prints the uid and exits with that status."""
    d = tmp_path / "fakebin"
    d.mkdir()
    idf = d / "id"
    idf.write_text('#!/bin/sh\n[ -n "$FAKE_UID" ] || exit 1\necho "$FAKE_UID"\nexit "${FAKE_ID_RC:-0}"\n',
                   encoding="utf-8")
    idf.chmod(0o755)
    return d


_Z, _A, _B = "0" * 40, "1" * 40, "2" * 40
_P = "refs/heads/proposed/"
# (uid, ref, old, new, allowed) — old all zeros: the ref does not exist; new all zeros: a delete.
_HOOK_TABLE = [
    # uid 1001 — the network service: CREATE a proposal, nothing else
    ("1001", _P + "abc123def456", _Z, _A, True),
    ("1001", _P + "20260705T101112Z-abcdef", _Z, _A, True),
    ("1001", _P + "abc123def456", _A, _B, False),                  # a proposal that exists is not moved
    ("1001", _P + "abc123def456", _A, _Z, False),                  # … nor deleted
    ("1001", "refs/heads/main", _A, _B, False),
    ("1001", "refs/heads/main", _Z, _A, False),
    ("1001", "refs/tags/main", _Z, _A, False),                     # a tag named like the branch
    ("1001", "refs/tags/proposed/abc123def456", _Z, _A, False),
    ("1001", "refs/proposed/abc123def456", _Z, _A, False),
    ("1001", "refs/replace/" + _A, _Z, _B, False),
    ("1001", "refs/heads/mainx", _Z, _A, False),
    ("1001", "refs/heads/proposed", _Z, _A, False),
    ("1001", _P + "abc123def456/x", _Z, _A, False),
    ("1001", _P + "*", _Z, _A, False),                             # a ref name that is itself a pattern
    ("1001", _P + "????????????", _Z, _A, False),
    ("1001", _P + "[0-9a-f]" * 1 + "bc123def456", _Z, _A, False),
    # any other uid — the operator, root in the installer: a proposal (create or move) and nothing else; main from
    # NOBODY (it is landed or promoted — ref transactions, never a push)
    ("1000", "refs/heads/main", _A, _B, False),
    ("0", "refs/heads/main", _Z, _A, False),
    ("0", "refs/heads/main", _A, _B, False),
    ("1000", _P + "abc123def456", _Z, _A, True),
    ("1000", _P + "abc123def456", _A, _B, True),
    ("1000", "refs/heads/main", _A, _Z, False),                    # a push never deletes
    ("1000", "refs/tags/v1", _Z, _A, False),
    ("1000", "refs/heads/feature", _Z, _A, False),
    ("0", "refs/replace/" + _A, _Z, _B, False),
    ("1000", _P + "run1", _Z, _A, False),
    # a uid that cannot be read: nothing
    ("", "refs/heads/main", _A, _B, False),
    ("", _P + "abc123def456", _Z, _A, False),
    # arguments that are not two object ids: nothing (an empty "new" must not read as "not a delete")
    ("1001", _P + "abc123def456", _Z, "", False),
    ("1001", _P + "abc123def456", "", _A, False),
    ("1000", "refs/heads/main", _A, "1" * 39, False),
    ("1000", "refs/heads/main", "refs/heads/main", _B, False),
    ("1000", _P + "abc123def456", "a" * 64, "b" * 64, True),        # a sha256 repository's ids
    ("1000", _P + "abc123def456", _A, "A" * 40, False),             # an id is LOWER-case hex
    ("1000", _P + "abc123def456", _A, "1" * 41, False),             # forty or sixty-four digits, nothing between
    ("1000", _P + "abc123def456", "1" * 63, _B, False),
    # the names and the uid are compared whole
    ("1000", "refs/heads/mainx", _Z, _A, False),
    ("1000", "refs/heads/main/x", _Z, _A, False),
    ("10010", _P + "abc123def456", _A, _B, True),                   # not uid 1001: the other list (a move is allowed)
    ("01001", _P + "abc123def456", _A, _B, True),
    # a delete, whoever asks and whatever it names (git's own receive.denyDeletes answers first on a node)
    ("1000", _P + "abc123def456", _A, _Z, False),
    ("1001", _P + "abc123def456", _Z, _Z, False),                   # … of a ref that is not there
    ("0", "refs/tags/v1", _A, _Z, False),
]


def _needs_sh():
    if sys.platform == "win32" or not shutil.which("sh"):
        pytest.skip("the hook-execution legs need POSIX sh (CI runs on Linux; live-verified on the armed VM too)")


def _shells(tmp_path):
    """The POSIX shells on this machine that a node may have as `/bin/sh`: `sh` itself, and — where installed —
    dash (Debian, the installer image), bash in POSIX mode (a Fedora / Arch / openSUSE host) and busybox `sh`
    (Alpine: the image the network service pushes from, so the one that runs the uid-1001 half on a node) — each
    kept only if the `id` it runs is the one found on the PATH. These tests say who is pushing by putting an `id`
    of their own first on the PATH; a busybox built to run its own applets before the PATH (Ubuntu 24.04's
    `busybox-static` is; its plain `busybox` and Alpine's are not) runs its own `id` whatever the PATH says, so
    under it the table's uid column would be the uid of whoever runs the tests. Such a shell is left out, and `sh`
    itself must not be one."""
    fakebin = tmp_path / "fakebin" if (tmp_path / "fakebin").exists() else _fake_id_bin(tmp_path)
    env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""), "FAKE_UID": "424242",
           "FAKE_ID_RC": "0"}

    def runs_the_shadow(shell):
        p = subprocess.run(list(shell) + ["-c", "id -u"], env=env, capture_output=True, text=True)
        return p.stdout.strip() == "424242"

    found = [["sh"]] + [c for c in (["dash"], ["bash", "--posix"], ["busybox", "sh"]) if shutil.which(c[0])]
    kept = [shell for shell in found if runs_the_shadow(shell)]
    assert ["sh"] in kept, "`sh` on this machine does not run the `id` found on the PATH: these tests cannot say who pushes"
    return kept


def _an_id(s):
    """A lower-case object id of forty or sixty-four digits — what the hook's `is_id` takes."""
    return len(s) in (40, 64) and set(s) <= set("0123456789abcdef")


def _run_hook(tmp_path, uid, ref, old, new, shell=("sh",), id_rc="0"):
    fakebin = _fake_id_bin(tmp_path) if not (tmp_path / "fakebin").exists() else tmp_path / "fakebin"
    env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""), "FAKE_UID": uid,
           "FAKE_ID_RC": id_rc}
    return subprocess.run(list(shell) + [_HOOK_SRC, ref, old, new], env=env, capture_output=True, text=True)


def test_the_committed_hook_is_the_one_the_play_installs():
    """The hook artifact exists, opens with a POSIX `sh` shebang, is keyed on `id -u`, and the canonical play
    copies THIS file to `<canonical>/hooks/update`. Portable (no shell needed). Guards the artifact the other hook
    tests execute being a twin of what a node runs: a hook that opens with another interpreter's name is run by a
    shell none of these tests ran it under (and one that is not executable is ignored by git, which is what
    `test_local_canonical.py` pins — as it pins that the play installs this file on every run)."""
    assert os.path.isfile(_HOOK_SRC), "the committed C10 update-hook is missing"
    body = open(_HOOK_SRC, encoding="utf-8").read()
    assert body.startswith("#!/bin/sh"), "the hook must open with #!/bin/sh — the shell every test here runs it under"
    assert "id -u" in body and "refs/heads/main" in body and "refs/heads/proposed/" in body
    playbook = open(os.path.join(ROOT, "ansible", "playbooks", "local-canonical.yml"), encoding="utf-8").read()
    assert "src: files/canonical-update-hook.sh" in playbook, "the playbook must install the committed hook file"
    assert 'dest: "{{ bare }}/hooks/update"' in playbook


def test_the_hook_is_an_allow_list(tmp_path):
    """The committed hook, run over a table of pushes with a PATH-shadowed `id`: as uid 1001 it allows the CREATION
    of `refs/heads/proposed/<run id>` (either minted shape) and nothing else — not `main`, not a tag, not
    `refs/replace/*`, not `refs/proposed/…`, not a ref name that is itself a pattern, not a move or a delete of a
    proposal that exists; as any other uid it allows a proposal (create or move) and nothing else; `refs/heads/main`
    is refused from EVERY uid — 1001, the operator's, root's — with a sentence that names the landing; a delete
    is refused from everyone; and when the uid cannot be read, or the hook is not handed two object ids, nothing is
    allowed — under every POSIX shell installed here in which the tests' own `id` is the one that runs (`_shells`);
    and an `id` that prints a uid and fails has not said who is pushing. Every refusal names the ref and the rule on stderr.

    WHY: the hook used to refuse ONE thing — uid 1001 pushing `refs/heads/main` — so the service's uid could push
    a tag called `main`, a replace ref, or the same proposal name a second time, and each of those changed what a
    later reader or the promote saw under a name a human had already looked at. A deny-list of one name is a list
    of everything else that is allowed. And `main` from another uid was allowed while the operator's and the
    installer's doors pushed it; since those doors LAND (`gitio.land_commit`, `tests/unit/test_land.py`) nothing
    legitimate pushes `main`, and a hook that still let it through would let a hand-run push skip the gate. (What
    the hook does NOT stop is said in its own header: a write that is not a push runs no hook.)"""
    _needs_sh()
    wrong = []
    for shell in _shells(tmp_path):
        for uid, ref, old, new, allowed in _HOOK_TABLE:
            p = _run_hook(tmp_path, uid, ref, old, new, shell=shell)
            if p.returncode != (0 if allowed else 1):          # exactly 1: a shell that cannot parse the file exits 2
                wrong.append((shell, uid, ref, old[:4], new[:4], p.returncode, p.stderr.strip()))
            if not allowed:
                assert p.stderr.startswith("C10: refused " + ref + " "), (shell, uid, ref, p.stderr)
                # a well-formed push of main (a uid, two ids, not a delete — those sentences come first) is
                # refused with the sentence that names the door
                if ref == "refs/heads/main" and uid and _an_id(old) and _an_id(new) and set(new) != {"0"}:
                    assert "kontroll-land.py" in p.stderr, "the refusal of main names the door: " + p.stderr
        # an `id` that prints a uid and FAILS has not said who is pushing: nothing is allowed
        p = _run_hook(tmp_path, "0", "refs/heads/main", _A, _B, shell=shell, id_rc="1")
        assert p.returncode == 1 and "could not be read" in p.stderr, (shell, p.returncode, p.stderr)
    assert wrong == []


def test_the_hook_takes_exactly_the_run_ids_the_validator_takes(tmp_path):
    """For every sample the Python rule (`canon.valid_run_id`) accepts or refuses, the hook — asked as uid 1001 to
    create `refs/heads/proposed/<sample>` — answers the same. Samples that cannot be a ref name at all (a newline,
    a space) are refused by both. Guards the third spelling of the run-id rule: the hook's two glob patterns are
    written by hand, and one character class too wide would let the service stage a ref the listing then calls
    "not a proposal" — or one too narrow would refuse every proposal of a minter."""
    _needs_sh()
    from kontroll import canon
    samples = ["abc123def456", "0" * 12, "20260705T101112Z-abcdef", "abc123def45", "abc123def4567", "ABC123DEF456",
               "20260705T101112Z-xyz", "20260705T101112Z-ABCDEF", "20260705t101112z-abcdef", "2026070T101112Z-abcdef",
               "20260705t101112Z-abcdef", "20260705T101112z-abcdef", "20260705T101112Z_abcdef",
               "20260705T101112Z-abcde", "20260705T101112Z-abcdef0", "main", "run1", "abc123def456/x", "-d",
               "abc123def456 ", " abc123def456", "abc123def456\n", "g" * 12, "abc123def45g", "",
               "2026070aT101112Z-abcdef", "202607050T101112Z-abcdef", "20260705T1011120Z-abcdef",
               "20260705T10111aZ-abcdef", "20260705T101112Z-abcdeg", "00000000T000000Z-000000"]
    for sample in samples:
        got = _run_hook(tmp_path, "1001", _P + sample, _Z, _A).returncode == 0
        assert got == canon.valid_run_id(sample), "the hook and the validator differ on %r" % sample


def test_a_real_push_meets_the_hook(tmp_path):
    """Installed in a bare repository and reached through real pushes: `main` is SEEDED by a landing
    (`gitio.land_commit` — the deploy's mirror task), never by a push; a push to `main` is refused from uid 1001,
    from the operator's uid and from root alike, and `main` does not move; as uid 1001 a NEW proposal is accepted,
    a second push to the SAME proposal name is refused and the proposal keeps its first commit, a tag called `main`
    and a `refs/replace/*` are refused; one push that carries a new proposal AND a tag lands the proposal and not
    the tag; and after all that a landing still advances `main` (the hook does not see it). Guards what the
    argument table cannot show — that git calls the hook with the arguments the table assumes, once per ref, and
    that a refusal leaves every ref as it was (the objects of a refused push stay in the object store, unreachable).
    The second-push row is the one the promote relies on: a proposal a human has read cannot be re-pointed by the
    service through a push; the main-from-everyone rows are what lets the gate be the only way in."""
    _needs_sh()
    fakebin = _fake_id_bin(tmp_path)
    env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", "")}
    canonical = tmp_path / "canonical.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(canonical)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(canonical), "config", "receive.denyNonFastForwards", "true"],
                   check=True, capture_output=True)
    shutil.copyfile(_HOOK_SRC, str(canonical / "hooks" / "update"))
    (canonical / "hooks" / "update").chmod(0o755)

    clone = tmp_path / "hookclone"
    subprocess.run(["git", "init", "-b", "main", str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    (clone / "f").write_text("1\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "seed")
    _git(clone, "remote", "add", "origin", str(canonical))

    def push(refspec, uid):
        return subprocess.run(["git", "-C", str(clone), "push", "origin", refspec],
                              env={**env, "FAKE_UID": str(uid)}, capture_output=True, text=True)

    def at(ref):
        p = subprocess.run(["git", "-C", str(canonical), "for-each-ref", "--format=%(objectname)", ref],
                           capture_output=True, text=True)
        return p.stdout.strip() or None

    for uid in (1000, 0, 1001):
        refused = push("HEAD:refs/heads/main", uid)
        assert refused.returncode != 0 and "never pushed, by anyone" in refused.stderr, (uid, refused.stderr)
    assert at("refs/heads/main") is None, "a push seeded main"
    rc, said = gitio.land_commit(str(clone), str(canonical), gate=lambda c, g, m: None)
    assert rc == 0, said
    seed = at("refs/heads/main")
    assert seed == _git(clone, "rev-parse", "HEAD", capture=True), "the landing seeds main (the mirror task)"
    (clone / "f").write_text("2\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "next")
    first = _git(clone, "rev-parse", "HEAD", capture=True)

    for uid in (1001, 1000, 0):
        assert push("HEAD:refs/heads/main", uid).returncode != 0, "git must reject a push to main from uid %s" % uid
    assert at("refs/heads/main") == seed, "main unchanged after the refusals"
    assert push("HEAD:refs/heads/proposed/0123456789ab", 1001).returncode == 0, "a new proposal is accepted"
    assert at(_P + "0123456789ab") == first

    (clone / "f").write_text("3\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "again")
    again = push("HEAD:refs/heads/proposed/0123456789ab", 1001)
    assert again.returncode != 0 and "created once" in again.stderr, again.stderr
    assert at(_P + "0123456789ab") == first, "a proposal that exists was moved by a push"

    for refspec in ("HEAD:refs/tags/main", "HEAD:refs/replace/" + seed, "HEAD:refs/heads/proposed/run1",
                    "HEAD:refs/proposed/0123456789ab"):
        assert push(refspec, 1001).returncode != 0, "uid 1001 pushed %s" % refspec
    assert push("HEAD:refs/tags/v1", 1000).returncode != 0, "a tag was pushed to the canonical"
    # one push, two refs: the hook is asked once for each — the proposal lands, the tag does not
    both = subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:refs/heads/proposed/0123456789cd",
                           "HEAD:refs/tags/v2"], env={**env, "FAKE_UID": "1001"}, capture_output=True, text=True)
    assert both.returncode != 0 and at(_P + "0123456789cd") is not None and at("refs/tags/v2") is None
    refs = subprocess.run(["git", "-C", str(canonical), "for-each-ref", "--format=%(refname)"],
                          capture_output=True, text=True).stdout.split()
    assert sorted(refs) == ["refs/heads/main", _P + "0123456789ab", _P + "0123456789cd"]

    assert push("HEAD:refs/heads/main", 1000).returncode != 0, "the operator's push to main is refused too"
    assert at("refs/heads/main") == seed
    rc, said = gitio.land_commit(str(clone), str(canonical), gate=lambda c, g, m: None)
    assert rc == 0 and at("refs/heads/main") == _git(clone, "rev-parse", "HEAD", capture=True), said

    # The promote is a ref update, not a push: the hook is not what bounds it, and does not stand in its way.
    (clone / "f").write_text("4\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "0000000000c0")
    fourth = _git(clone, "rev-parse", "HEAD", capture=True)
    assert push("HEAD:refs/heads/proposed/0000000000c0", 1001).returncode == 0
    assert gitio.promote_ref("0000000000c0", cwd=str(canonical)) is True
    assert at("refs/heads/main") == fourth
