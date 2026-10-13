"""pending domain — the read-only "what proposals are awaiting promotion?" view (#129, gap 3 / C10).

`list_pending()` lists the canonical bare repo's `refs/heads/proposed/<run_id>` branches — every change STAGED by
the network service (onboard / capability / secret / keygen) that has not yet been promoted to `main`. Each entry
carries the run_id (the load-bearing field the operator copies into the Semaphore `promote-proposal` task), the
commit it points at, the tip subject ("what it changes" at a glance), the staged-at time, the author, and what it
changes — one status and one name per file. It is a PURE READ of the canonical (`/srv/kontroll.git` by default, or
`$KONTROLL_CANONICAL_GIT`) and writes NOTHING — it **never promotes** (promote stays the separate, admin-authed
Semaphore app: C10 two-key safety; the proposer's read surface must never be the promoter — so this module imports
neither `gitio.promote_ref` nor `commit_and_push`, and never `git push`/`update-ref`).

WHY this surface exists: the network service stages `proposed/<run_id>` refs the operator can otherwise see only
in the transient propose response (gone once the dialog closes) or via a manual `git for-each-ref` on the box.
This is the GUI answer to "where do I get the run_id to promote?" — a standing list with a one-click copy + the
exact Semaphore recipe. A missing/empty/non-git canonical → `proposals: []` + an informational note (never raises;
the in-process GUI worker must survive — service-never-sys.exit, mirroring `fleet.list_fleet`'s posture). Serves
metadata + path NAMES only — never file contents (that is the C8 backup-viewer's problem, deliberately not here).

HOW it reads (the rules of `kontroll.canon`, which is where the refs are read):
  - by FULL NAME, once. `canon.read_ref` gives `main`'s commit id and `canon.proposals` each proposal's; every
    later question is asked of those ids. A short name is looked up — beside a tag called `main`, `main..<ref>` is
    the tag's range — so none reaches git from here.
  - in the SEAM. Every git process runs in `canon.git_kwargs(<canonical>)`.
  - as BYTES. A name, an author and a subject are whatever was committed; `canon.show` makes each the text a person
    reads and says when that text is an escaped spelling. Nothing here decodes on git's behalf, so a name that is
    not text cannot raise — it is listed, escaped and marked.
A file the proposal MOVES is listed at both ends (`D old`, `A new`): the old name is where the change takes
something away, and a list that showed the new name alone hid it. The change list is the proposal against the
commit it was staged on (its merge base with `main`), so a proposal `main` has moved past shows its own change and
not `main`'s later ones turned inside out. A ref under `refs/heads/proposed/` that is not a proposal — a name the
service does not mint, a symbolic ref, a ref that names no commit, a name git gives two values — is returned apart
(`unrecognised`), never dropped and never promotable from the panel.

WHAT IT COULD NOT READ IS SAID, NEVER SHOWN AS NOTHING. A change list that could not be read is not an empty one
(`changes_read` False), a `main` that is not an ordinary branch is not a `main` every proposal is fine against
(`main_note`), a ref git left out is not a ref that is not there (`skipped_note`), and an unreadable canonical is
not an empty queue (`note_kind` "unreadable").
"""
import logging
import os
import subprocess
import time

from kontroll import canon

log = logging.getLogger("kontroll.service.pending")

DEFAULT_CANONICAL = "/srv/kontroll.git"     # the in-container bind path; matches configure-semaphore's LOCAL_GIT_URL
PROMOTE_TASK = "promote-proposal"           # config/semaphore/templates/ — handed to the GUI so the pointer isn't
#                                             hard-coded twice (config-as-data: the panel renders this name).
# One budget for the whole list. Every git here has its own short timeout, but those add up per proposal; a
# canonical that makes each of them wait must not hold a GUI worker for their sum. A row reached after the budget
# is listed with what was not read said as not read.
LIST_SECONDS = 20


def _canonical(canonical=None):
    """Resolve the canonical bare-repo path: explicit arg (tests) → $KONTROLL_CANONICAL_GIT → the default bind."""
    return canonical or os.environ.get("KONTROLL_CANONICAL_GIT") or DEFAULT_CANONICAL


def _merge_base(cdir, main_id, oid):
    """The commit a proposal was staged on: the best common ancestor of `main` and the proposal, both given as ids.
    None when they share no history, or git cannot say (the caller then lists no change rather than a wrong one)."""
    if not (canon.valid_oid(main_id) and canon.valid_oid(oid)):
        return None
    try:
        p = subprocess.run(["git", "merge-base", main_id, oid],
                           capture_output=True, timeout=5, **canon.git_kwargs(cdir))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        return None
    base = p.stdout.split(b"\n")[0].decode("ascii", "replace")
    return base if p.returncode == 0 and canon.valid_oid(base) else None


def _changes(cdir, base_id, oid):
    """What the proposal changes against the commit it was staged on, as `[(status, name bytes)]` — names only,
    never contents (no C8 weight). The flags make the answer the tree's own: `--no-renames` lists a moved file at
    BOTH ends (`D old`, `A new`), `-z` returns each name as its bytes (no quoting, whatever `core.quotePath` says),
    `--no-ext-diff --no-textconv` keep the porcelain from starting a program, and `--ignore-submodules=none` lists
    a changed gitlink whatever `diff.ignoreSubmodules` says — and without it git reads the repository's `index`
    and a `.gitmodules` in the caller's directory to decide. None on any failure: the caller says the list could
    not be read, which is not the same as a proposal that changes nothing."""
    if not (canon.valid_oid(base_id) and canon.valid_oid(oid)):
        return None
    try:
        p = subprocess.run(["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                            "--ignore-submodules=none", "--name-status", "-z", base_id, oid, "--"],
                           capture_output=True, timeout=5, **canon.git_kwargs(cdir))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        return None
    tok = p.stdout.split(b"\0")[:-1]                       # every status and every name is NUL-terminated
    if p.returncode != 0 or len(tok) % 2:
        return None
    return [(tok[i].decode("ascii", "replace"), tok[i + 1]) for i in range(0, len(tok), 2)]


def _repropose_hint(paths):
    """A read-only routing hint for the GUI 'Re-propose' button on a STALE proposal (the FF-race remediation, Move
    1). Returns {"kind": "onboard", "key": <class key>} ONLY when the proposal's changed-path NAMES carry an onboard
    drop-in (`…/onboarded-<key>.yml`) — the class key is the SINGLE fact a ref's path names yield. Host/group/creds
    live INSIDE the inventory YAML, never in a path name, so they are deliberately NOT reconstructed here: the
    existing onboard dialog owns reconstruction, the human re-enters them (SOPS is irreversible; a base-moved HIGH
    proposal WANTS that re-review). Returns None for any other proposal shape (a capability/actuation/homepage/secret
    change), for which the panel keeps the plain 'stale' hint. Pure string work over the NAMES list list_pending
    already computes — no new read, no git call (so the read-only pin stays trivially green; MF-2/MF-3)."""
    for p in paths or []:
        base = p.replace("\\", "/").rsplit("/", 1)[-1]
        if base.startswith("onboarded-") and base.endswith(".yml"):
            key = base[len("onboarded-"):-len(".yml")]
            if key:
                return {"kind": "onboard", "key": key}
    return None


def _entry(cdir, main_id, found, deadline):
    """One proposal's row. `promotable` is the fast-forward-ONLY promote gate's own question, asked of the two ids:
    True (`main` is an ancestor of the proposal — a clean fast-forward), False (`main` advanced UNDER it — the
    FF-race — so it is stale and a fresh proposal is needed), or None (it cannot be told; never badge stale on an
    unknown, and never call it promotable either). WHY: the auto-promoter (or any out-of-band advance) can move
    `main` past an earlier-staged HIGH-blast proposal, leaving it non-FF; the gate then refuses it. Surfacing this
    lets the panel flag 'stale — re-propose' instead of listing a dead run_id as promotable.

    `changes_read` says whether `changes` IS the proposal's change list: False when there is no `main` to compare
    with, when git could not print the list, or when the list's time budget ran out before this row."""
    oid = found["oid"]
    promotable, changes, changes_read = None, [], False
    if main_id and time.monotonic() < deadline:
        promotable = canon.is_ancestor(cdir, main_id, oid)
        base_id = _merge_base(cdir, main_id, oid)
        listed = _changes(cdir, base_id, oid) if base_id else None
        if listed is not None:
            changes_read = True
            for status, name in listed:
                text, exact = canon.show(name)
                changes.append({"status": status, "path": text, "exact": exact})
    paths = [c["path"] for c in changes]
    return {"run_id": found["run_id"], "ref": canon.proposal_ref(found["run_id"]), "sha": oid,
            "staged_at": found["date"], "author": canon.show(found["author"])[0],
            "subject": canon.show(found["subject"])[0],
            "paths": paths, "changes": changes, "changes_read": changes_read,
            "promotable": promotable,
            "repropose_hint": _repropose_hint(paths)}


def list_pending(canonical=None):
    """The staged proposals awaiting promotion (C10). See the module docstring. Returns:
        {"proposals": [ {run_id, ref, sha, staged_at, author, subject, paths, changes, changes_read, promotable,
                         repropose_hint}, ... ],
         "unrecognised": [ {ref, sha, why}, ... ],
         "canonical": "<resolved path>", "promote_task": "promote-proposal",
         "note": None|"...", "note_kind": None|"empty"|"absent"|"unreadable",
         "main_note": None|"...", "skipped_note": None|"..."}
    `ref` is the proposal's full name and `sha` its full commit id; `changes` is `[{status, path, exact}]` (one per
    file, a moved file at both ends; `exact` False when `path` is an escaped spelling of a name that is not text),
    `paths` the same names as a plain list, and `changes_read` False when the list could not be read (it is then
    empty and means nothing). `main_note` is set when `refs/heads/main` is not an ordinary branch to compare with;
    `skipped_note` when git said it left a ref out. Newest-first. NEVER raises; a missing/non-git/empty canonical
    yields proposals=[] + a note whose kind says which."""
    cdir = _canonical(canonical)
    base = {"proposals": [], "unrecognised": [], "canonical": cdir, "promote_task": PROMOTE_TASK, "note": None,
            "note_kind": None, "main_note": None, "skipped_note": None}
    if not os.path.isdir(cdir):
        base["note"], base["note_kind"] = "canonical repo not present (no proposals on this deploy)", "absent"
        return base
    deadline = time.monotonic() + LIST_SECONDS
    try:
        found, apart, left_out = canon.proposals(cdir)
        main = canon.read_ref(cdir, canon.MAIN)
    except canon.Unreadable as e:
        base["note"], base["note_kind"] = "could not read the canonical (%s)" % e, "unreadable"
        return base
    # `main` is a commit id or it is nothing: a symbolic `main`, or one that names a tag, is not what a promote
    # would compare against, so no proposal is called promotable or stale against it — and the panel is told.
    main_id = main[0] if main and main[1] == "commit" and not main[2] else None
    found.sort(key=lambda f: f["when"], reverse=True)                # newest first, by the committer's clock
    proposals = [_entry(cdir, main_id, f, deadline) for f in found]
    base["proposals"] = proposals
    base["unrecognised"] = [{"ref": canon.show(a["ref"])[0], "sha": a["oid"], "why": a["why"]} for a in apart]
    if main_id is None and (proposals or apart):
        base["main_note"] = ("`refs/heads/main` is missing, or is not an ordinary branch that names a commit — no "
                             "proposal can be compared with it, and none should be promoted until that is understood")
    if left_out:
        base["skipped_note"] = ("git left out at least one ref under refs/heads/proposed/ that it could not read "
                                "(a file with no object name, or a name git does not accept)")
    if not proposals:
        base["note_kind"] = "empty"
        base["note"] = ("no proposals pending — but refs that are not proposals sit under refs/heads/proposed/ "
                        "(listed below)" if apart else
                        "no proposals pending (this deploy may commit directly to main; proposals appear only "
                        "when the staging deploy is armed)")
    return base
