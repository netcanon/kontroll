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
    operator-CLI / non-staging path, target == main) commit_and_push(push_origin=True) MUST still push origin and
    advance canonical `main` — the fix suppresses the origin push ONLY under staging, never the legitimate direct-main
    path. Guards an over-correction (a blanket 'never push origin') that would silently break the operator's
    offsite/canonical backup push. Real-git so 'advanced main' is observed on a real object DB — the exact contrast to
    test_staging_push_origin_never_advances_canonical_main."""
    clone, canonical = content_clone_with_origin
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)          # CLI / non-staging deploy -> target == main
    main_before = _git(canonical, "rev-parse", "refs/heads/main", capture=True)

    (clone / "state.yml").write_text("remotes: [cli]\n", encoding="utf-8")
    out = gitio.commit_and_push(["state.yml"], ["operator cli edit"], push_origin=True)

    assert out["staged"] is False and out["target_ref"] == "main"
    assert out["pushed_canonical"] is True and out["pushed_origin"] is True   # the sharp C3 guard: origin push fired
    main_after = _git(canonical, "rev-parse", "refs/heads/main", capture=True)
    assert main_after != main_before, \
        "the operator-CLI direct-main push must still advance main (C3) — the fix must scope suppression to staging"
    assert _show(canonical, "main:state.yml") == "remotes: [cli]\n"


# --- the canonical update-hook (defence-in-depth): tests the REAL committed artifact, not a twin (MF-1) ---------

_HOOK_SRC = os.path.join(ROOT, "ansible", "playbooks", "files", "canonical-update-hook.sh")


def _fake_id_bin(tmp_path):
    """A tmp dir holding an `id` executable that prints $FAKE_UID — PATH-shadowed so the REAL hook's unspoofable
    `$(id -u)` resolves to a uid the test controls (a unit test can't actually be uid 1001, and the production hook
    intentionally has NO env override an attacker could set)."""
    d = tmp_path / "fakebin"
    d.mkdir()
    idf = d / "id"
    idf.write_text('#!/bin/sh\necho "${FAKE_UID:-0}"\n', encoding="utf-8")
    idf.chmod(0o755)
    return d


def test_canonical_update_hook_refuses_uid1001_push_to_main_but_allows_proposals_and_promote(tmp_path):
    """Defence-in-depth (C10 origin-push hook): the canonical's `update` hook REFUSES a receive-pack push to
    `refs/heads/main` from the network-service uid (1001) — the origin-push breach's landing ref — while ALLOWING a
    push to `refs/heads/proposed/*` and a push to main from any OTHER uid (the mirror, C1), and never interfering with
    `promote_ref` (which advances main via `git update-ref`, NOT receive-pack — C2).

    WHY (the failure this guards): even if the gitio `and not staging` code guard regressed, a uid-1001 network
    service's `git push …:main` must be rejected AT the canonical, and the ONLY legitimate main-advances (the
    operator/deploy mirror — a DIFFERENT uid — and the promote) must keep working (C1/C2). This exercises the EXACT
    committed artifact (ansible/playbooks/files/canonical-update-hook.sh — the one the playbook installs), driving the
    real `$(id -u)` through a PATH-shadowed fake `id`, so a typo/shebang/argv bug in the shipped hook FAILS the test
    (MF-1: no hand-copied twin). The static half (file present, shebang, playbook installs THIS file) runs
    everywhere; the real-git execution half needs POSIX sh (skipped on win32)."""
    # (static, portable) the artifact exists, is a POSIX sh script, and the playbook installs THIS file (no twin drift)
    assert os.path.isfile(_HOOK_SRC), "the committed C10 update-hook is missing"
    body = open(_HOOK_SRC, encoding="utf-8").read()
    assert body.startswith("#!/bin/sh"), "the hook must have a POSIX sh shebang (a missing one fails OPEN)"
    assert 'id -u' in body and 'refs/heads/main' in body     # keyed on the unspoofable uid + the guarded ref
    playbook = open(os.path.join(ROOT, "ansible", "playbooks", "local-canonical.yml"), encoding="utf-8").read()
    assert "src: files/canonical-update-hook.sh" in playbook, "the playbook must install the committed hook file"
    assert 'dest: "{{ bare }}/hooks/update"' in playbook

    if sys.platform == "win32" or not shutil.which("sh"):
        pytest.skip("the hook-execution legs need POSIX sh (CI runs on Linux; live-verified on the armed VM too)")

    fakebin = _fake_id_bin(tmp_path)
    env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", "")}

    def run_hook(refname, uid):
        return subprocess.run(["sh", _HOOK_SRC, refname, "0" * 40, "1" * 40],
                              env={**env, "FAKE_UID": str(uid)}, capture_output=True, text=True).returncode

    # (a) REFUSE: uid 1001 pushing main is rejected; (b) ALLOW proposals even as 1001; (c) ALLOW main from a non-1001 uid
    assert run_hook("refs/heads/main", 1001) != 0, "the hook must REFUSE a uid-1001 update of refs/heads/main"
    assert run_hook("refs/heads/proposed/0123456789ab", 1001) == 0, "the hook must ALLOW a proposed/<run_id> staging push"
    assert run_hook("refs/heads/main", 1000) == 0, "the hook must ALLOW the non-1001 operator/deploy mirror (C1)"

    # (real git) install the SAME artifact into a bare canonical; a real receive-pack push exercises git's invocation
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

    # seed canonical `main` via a NON-1001 push (the hook allows it AND it lands the objects in the bare — an
    # `update-ref` to a still-unpushed object would fail); the uid the hook sees comes from FAKE_UID, not the runner.
    assert push("HEAD:refs/heads/main", 1000).returncode == 0, "a non-1001 push must seed main (C1 mirror)"
    seed = _git(canonical, "rev-parse", "refs/heads/main", capture=True)
    (clone / "f").write_text("2\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "next")
    next_tip = _git(clone, "rev-parse", "HEAD", capture=True)

    # git INVOKES the hook: a uid-1001 push to main is REJECTED and main stays at seed; a proposed/* push is accepted;
    # a non-1001 push to main is accepted (the C1 mirror) and advances main.
    assert push("HEAD:refs/heads/main", 1001).returncode != 0, "git must reject the uid-1001 push to main (hook fires)"
    assert _git(canonical, "rev-parse", "refs/heads/main", capture=True) == seed, "main unchanged after the refusal"
    assert push("HEAD:refs/heads/proposed/0123456789ab", 1001).returncode == 0, "a uid-1001 proposed/* push is accepted"
    assert push("HEAD:refs/heads/main", 1000).returncode == 0, "the non-1001 (mirror) push to main is accepted (C1)"
    assert _git(canonical, "rev-parse", "refs/heads/main", capture=True) == next_tip

    # (C2) promote_ref advances main via update-ref (NOT receive-pack) → the hook never fires on it. Stage a FF
    # proposal ahead of main, then promote it and assert main advanced (the promote is not blocked by the hook).
    (clone / "f").write_text("3\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "0000000000c0")
    third = _git(clone, "rev-parse", "HEAD", capture=True)
    assert push("HEAD:refs/heads/proposed/0000000000c0", 1000).returncode == 0     # stage the FF proposal
    assert gitio.promote_ref("0000000000c0", cwd=str(canonical)) is True, \
        "promote_ref (update-ref, not receive-pack) must advance main — the update hook never fires on it (C2)"
    assert _git(canonical, "rev-parse", "refs/heads/main", capture=True) == third, "promote advanced main via update-ref"
