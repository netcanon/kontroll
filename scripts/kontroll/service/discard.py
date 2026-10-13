"""discard domain — delete a STALE staged proposal from the canonical (FF-race remediation, Move 2 / C10).

`discard_proposal(run_id, canonical)` removes a `refs/heads/proposed/<run_id>` ref from the canonical bare repo —
the ONE write verb in the pending surface, kept in its OWN module so `pending.py` stays strictly read-only (the
lister must never be the reaper). It is C10-SAFE by construction: un-staging a proposal removes Key-1's OWN output
— it CANNOT advance `main`, enact anything, or grow the promotable set (unlike promote, which is the separate,
admin-authed Semaphore app). WHY it exists: the auto-promoter (or any out-of-band advance) can leave earlier-staged
proposals non-fast-forwardable (the FF-race). Part 1 badges them "stale"; Move 1 offers re-propose; this reaps the
dead refs re-propose leaves behind so the Pending list doesn't accrete un-promotable cruft.

SECURITY (the design-of-record's must-fixes, folded in — docs/reviews/2026-07-05-ff-race-parked-proposals/):
  - SEC-B1: the run_id validator (`canon.valid_run_id`) accepts BOTH real minter shapes anchored (`uuid4().hex[:12]`
    = 12 hex; the API's `mint_run_id` = `YYYYMMDDTHHMMSSZ-<6hex>`) and NOTHING else — so `/`, whitespace, `..`, a
    leading `-`, or `main` can never reach git. Structurally, the ref is built (`canon.proposal_ref`) and passed
    AFTER `--end-of-options`, so even a hypothetical leading-`-` leaf can never be read as a flag (defence in depth
    with the regex).
  - SEC-M1/m2: COMPARE-AND-DELETE. The ref's value is read first and the delete is `git update-ref -d <ref> <oldsha>`
    — it deletes ONLY if `<ref>` STILL points at `<oldsha>`, closing the read-then-delete TOCTOU: a concurrent
    promote/rebase that moved or removed the ref makes the delete a no-op (a 409 to the caller), never a blind
    delete. The load-bearing SEC-m2 defense is PROVENANCE — the oldsha comes from git's own read and is passed as
    update-ref's oldvalue operand; `canon.read_ref` returns only a non-zero 40/64-hex id (a null OID would delete
    UNCONDITIONALLY).
  - MF-4: the delete argv is a SINGLE literal list (`["git", "update-ref", …, ref, sha]`), NOT a `[...] + [...]`
    concat — so the fail-closed read-only completeness gate (`_static_list_prefix`, which reads only a BinOp's LEFT
    operand) SEES `update-ref` and forces `discard_proposal` to be registered in `WRITE_VERBS`.
  - THE REF ITSELF, NEVER WHAT IT POINTS AT. A proposal is an ordinary ref that names a commit. `canon.read_ref`
    says when the ref under that name is a SYMBOLIC ref (or names a tag or a blob), and such a ref is refused: it
    is not a proposal, and `git update-ref -d` on a symbolic ref deletes the ref it POINTS at — a proposal written
    as `ref: refs/heads/main` would have turned this verb into the deletion of `main`. The delete also passes
    `--no-deref`, so that what is deleted is the name given even if the ref changed kind after the read.
  - IN THE SEAM. Both git processes run in `canon.git_kwargs(<canonical>)`: no hook of the repository runs on the
    delete (`update-ref` runs `reference-transaction`), and no other store of it stands in for the one named.

NEVER raises for the caller's benefit: a bad run_id / missing canonical / vanished ref returns a structured
`{"ok": False, "error", "code"}` the route maps to 400/404/409 (never a 500).
"""
import os
import subprocess

from kontroll import canon
from kontroll.service.pending import DEFAULT_CANONICAL   # the SAME canonical bind pending.py reads (no drift)


def _canonical(canonical=None):
    """Resolve the canonical bare-repo path: explicit arg (tests) -> $KONTROLL_CANONICAL_GIT -> the default bind
    (mirrors pending._canonical so the reaper and the lister always target the same repo)."""
    return canonical or os.environ.get("KONTROLL_CANONICAL_GIT") or DEFAULT_CANONICAL


def discard_proposal(run_id, canonical=None):
    """Delete the staged `proposed/<run_id>` ref from the canonical, compare-and-delete on its current tip. Returns
    a structured result (never raises):
        {"ok": True,  "run_id", "ref": "refs/heads/proposed/<run_id>", "sha": <deleted tip>}
        {"ok": False, "error": "<why>", "code": 400|404|409, "run_id"}
    400 = invalid run_id (SEC-B1); 404 = no canonical / no such proposal (already promoted/discarded/never staged,
    or the ref under that name is not a proposal: a symbolic ref, a ref that names no commit); 409 = the ref moved
    or vanished between the read and the delete (TOCTOU — refresh and retry), or git would not delete it (a lock
    held, a directory that is not writable), or the canonical could not be read.
    C10-safe: this removes a `proposed/*` ref only; it never advances main, enacts, or promotes."""
    if not canon.valid_run_id(run_id):
        return {"ok": False, "error": "invalid run_id", "code": 400, "run_id": run_id}
    cdir = _canonical(canonical)
    if not os.path.isdir(cdir):
        return {"ok": False, "error": "canonical repo not present (no proposals on this deploy)",
                "code": 404, "run_id": run_id}
    ref = canon.proposal_ref(run_id)
    try:
        current = canon.read_ref(cdir, ref)                  # compare-and-delete: capture the exact value first
    except canon.Unreadable as e:
        return {"ok": False, "error": "discard failed: %s" % e, "code": 409, "run_id": run_id}
    if current is None:
        return {"ok": False, "error": "no such proposal (already promoted, discarded, or never staged)",
                "code": 404, "run_id": run_id}
    sha, objtype, symref = current
    if symref or objtype != "commit":
        return {"ok": False, "error": "no such proposal (the ref of that name is not one: it is a symbolic ref, or "
                                      "names no commit) — nothing was deleted",
                "code": 404, "run_id": run_id}
    # MF-4: a SINGLE literal argv so the completeness gate sees `update-ref` and requires this verb be registered.
    # SEC-M1: `-d <ref> <oldsha>` deletes ONLY if ref still points at oldsha (a moved/removed ref -> rc!=0 -> 409).
    # `--no-deref`: the name given is what is deleted, never the ref a symbolic ref points at.
    try:
        rc = subprocess.run(["git", "update-ref", "--no-deref", "-d", "--end-of-options", ref, sha],
                            capture_output=True, timeout=5, **canon.git_kwargs(cdir)).returncode
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        return {"ok": False, "error": "discard failed (git unavailable)", "code": 409, "run_id": run_id}
    if rc != 0:
        return {"ok": False, "error": "the proposal moved or vanished since it was listed, or the canonical is "
                                      "locked or not writable — refresh and retry",
                "code": 409, "run_id": run_id}
    return {"ok": True, "run_id": run_id, "ref": ref, "sha": sha}
