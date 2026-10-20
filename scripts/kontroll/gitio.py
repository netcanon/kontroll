"""Repo-mutation primitives shared by the onboard / capture-exception services.

_run — a narrated subprocess (the git/ansible shell-out SEAM the tests monkeypatch,
kontroll.gitio._run); _write_new — an idempotent, no-clobber drop-in file write (never
overwrite an operator edit); sops_set — encrypt one credential into a SOPS domain in
place. Every write + git op targets `paths.write_root()` (the WRITE root) read DYNAMICALLY,
so the tmp_repo fixture's repoint diverts it; a BAKED deploy points it at the propose clone
(the read-only image at ROOT has no .git, so git MUST run in the clone). Verbatim from galaxy.py.
"""
import importlib
import json
import os
import subprocess
import sys

from kontroll import canon, paths

# The attribution trailer every machine-made commit carries (the API/GUI propose paths and the operator CLI's
# `--commit`), in ONE place. It used to be eighteen string literals naming the model that wrote the code — an
# attribution that was wrong on every commit the control plane makes on an operator's behalf (public-split
# review 2026-10-08, F9). Configurable per deployment: KONTROLL_COMMIT_TRAILER overrides the text — the
# containers read it from docker/.env (rendered by deploy-stack from `kontroll_commit_trailer`), the operator CLI
# (galaxy.py --commit) from the calling shell; `none`/`off`/`disabled` turn the trailer off (the repo's off-switch
# vocabulary, api/ratelimit.py); unset/empty keeps the default. The default names the tool (the same identity the
# containers commit as — docker/semaphore-runner/Dockerfile), never a person.
COMMIT_TRAILER_ENV = "KONTROLL_COMMIT_TRAILER"
DEFAULT_COMMIT_TRAILER = "Co-Authored-By: kontroll <kontroll@localhost>"
_TRAILER_OFF = ("none", "off", "disabled")


def commit_trailer():
    """The trailer line to append, or "" when disabled. Read DYNAMICALLY (not at import) so a deploy's .env and
    the tests' monkeypatch both take effect without a reload."""
    raw = (os.environ.get(COMMIT_TRAILER_ENV) or "").strip()
    if not raw:
        return DEFAULT_COMMIT_TRAILER
    if raw.lower() in _TRAILER_OFF:
        return ""
    return raw


def commit_message_args(message_lines):
    """`git commit` -m arguments: the caller's subject/body lines plus the one trailer — appended ONCE, last,
    and never duplicated when a caller already carries it. The single seam every commit path builds its message
    through (commit_and_push below; galaxy.py's inline `--commit` paths), so a trailer edit is a setting change."""
    lines = [m for m in message_lines]
    trailer = commit_trailer()
    if trailer and trailer not in lines:
        lines.append(trailer)
    args = []
    for m in lines:
        args += ["-m", m]
    return args


def _run(cmd, cwd=None, quiet_args=0):
    shown = cmd[:len(cmd) - quiet_args] + (["***"] if quiet_args else [])
    print("  $ %s" % " ".join(shown))
    return subprocess.run(cmd, cwd=cwd or paths.write_root()).returncode


def offsite_remote_exists():
    """True iff this repo has an `origin` remote pointing OFFSITE — a real backup target, NOT a local `file://`
    path / the on-box canonical. The onboarding GUI gates the 'also push origin (offsite backup)' checkbox on
    this: a fresh node has no offsite remote, so that push would just no-op/fail and the box only confuses."""
    p = subprocess.run(["git", "remote", "get-url", "origin"], cwd=paths.write_root(),
                       capture_output=True, text=True)
    url = (p.stdout or "").strip()
    if p.returncode != 0 or not url:
        return False
    return not (url.startswith("file:") or url.startswith("/") or url.startswith("."))


class WriteConflict(Exception):
    """A drop-in target already exists with DIFFERENT content. RAISED (never `sys.exit`) so the in-process GUI/API
    can catch it and return a clean 409. A `sys.exit` here raises SystemExit, which Flask/Werkzeug does NOT catch
    (it derives from BaseException) — so it kills the request worker and the browser just sees "request failed"
    with no message (live-caught onboarding cisco_ios, a shipped module). The service-degrades-not-crashes
    principle (see service/_blockwrite.py). The CLI catches it at dispatch and exits cleanly (old UX preserved)."""


def _write_new(rel_path, content, banner, overwrite=False):
    """Write a NEW drop-in file. Idempotent: identical content -> noop; a different existing file raises
    WriteConflict (never clobber operator edits) UNLESS `overwrite=True` — the GUI's explicit 'Overwrite' escape
    hatch, the operator's deliberate choice to replace the existing file."""
    path = paths.confined(rel_path)                        # never outside write_root() (finding 1)
    body = banner + content
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            if fh.read() == body:
                print("  = %s already current" % rel_path)
                return False
        if not overwrite:
            raise WriteConflict(
                "%s already exists with different content — pick a new device-class key, "
                "or use Overwrite to replace it" % rel_path)
        print("  ! overwriting divergent %s (operator-requested)" % rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print("  + wrote %s" % rel_path)
    return True


def owned_merge(text, group, hosts):
    """Merge `hosts` ({name: vars}) under `group` into the banner-owned inventory `text`, returning
    `(merged_doc, collisions)`. A collision is a (host, var) present in `text` with a DIFFERENT incoming value —
    the silent last-writer-wins `.update()` lifted into a REPORTED diff (design 22 §5.3, G3): a host-var EDIT (or
    a 2nd-device onboard that re-touches a host) can now SHOW + gate what it overwrites instead of clobbering
    invisibly. The merge is REPLACE-per-host-NAME (identical to the prior `.update(hosts)` — so onboard's
    behaviour is unchanged); the EDIT path passes the host's FULL updated var set, so unlisted vars survive.
    PURE — computes the merged dict + the collisions; the caller serializes + writes. `text` MUST be banner-owned
    (the caller checks ownership)."""
    import yaml
    doc = yaml.safe_load(text) or {}
    existing = ((doc.get(group) or {}).get("hosts") or {})
    collisions = []
    for name, vars_ in (hosts or {}).items():
        prior = existing.get(name) or {}
        for var, after in (vars_ or {}).items():
            if var in prior and prior[var] != after:
                collisions.append({"host": name, "var": var, "before": prior[var], "after": after})
    doc.setdefault(group, {}).setdefault("hosts", {}).update(hosts)   # REPLACE per host name (the prior .update)
    return doc, collisions


def write_inventory_host(rel_path, group, hosts, banner, overwrite=False):
    """Write/refresh the drop-in inventory file at `rel_path`, placing `hosts` ({name: vars}) under `group`.

    Unlike `_write_new` (whole-file no-clobber), this MERGES via `owned_merge`: if the file already carries our
    `banner` (we wrote it), the new host(s) are added ALONGSIDE any siblings already there — so onboarding a 2nd
    device of an existing class never drops the 1st (#124, the "add a host to an existing class" path). A FOREIGN
    file (no banner — an operator rewrite) still hard-stops with WriteConflict unless `overwrite=True` (the
    deliberate escape hatch), preserving the no-clobber-operator-edits guarantee. Idempotent: an identical result
    is a no-op (returns False). The key-level overwrite is now visible through `owned_merge`'s collisions (G3) —
    the host-var reconfigure surface (service/hostvars) consumes them; onboard's additive add ignores them."""
    import yaml
    path = paths.confined(rel_path)                        # never outside write_root() (finding 1)
    doc = {group: {"hosts": dict(hosts)}}                     # the default (a fresh drop-in, or an overwrite-replace)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if text.startswith(banner):                          # we own this file -> MERGE, never clobber siblings
            doc, _ = owned_merge(text, group, hosts)
        elif not overwrite:
            raise WriteConflict(
                "%s already exists with different content — pick a new device-class key, "
                "or use Overwrite to replace it" % rel_path)
    body = banner + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=4096)
    if os.path.exists(path) and open(path, encoding="utf-8").read() == body:
        print("  = %s already current" % rel_path)
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print("  + wrote %s" % rel_path)
    return True


def sops_set(domain, key, value):
    """Encrypt a single credential into a SOPS domain file, in place (on a host
    holding a valid age key). The value is namespaced per host so onboarded hosts
    never collide on a shared cred. Returns True on success."""
    path = paths.resolve("ansible/secrets/%s.sops.yml" % paths.component(domain, "secret domain"))
    if not os.path.exists(path):
        print("  ! secrets domain %s.sops.yml missing — create it first (sops)" % domain)
        return False
    # sops --set '["key"] <json-value>' file  — value JSON-encoded; runs locally.
    rc = subprocess.run(["sops", "--set", '["%s"] %s' % (key, json.dumps(value)), path]).returncode
    print("  %s sops set %s.%s" % ("+" if rc == 0 else "!", domain, key))
    return rc == 0


def _domain_path(domain):
    return paths.resolve("ansible/secrets/%s.sops.yml" % paths.component(domain, "secret domain"))


def sops_decrypt_domain(domain):
    """Decrypt instance/secrets/<domain>.sops.yml to a plaintext dict (in memory). Returns {} if the domain
    file is absent (a NEW domain), or None on a decrypt failure (no/invalid age key). NEVER prints the values
    — only used by the secret-onboarding service, which holds them in memory and never logs them. Needs a
    valid age key (the onboard-gui surface mounts one; SECURITY.md). Goes through subprocess directly (like
    sops_set); tests monkeypatch this function, not the shell-out."""
    path = _domain_path(domain)
    if not os.path.exists(path):
        return {}
    p = subprocess.run(["sops", "--decrypt", path], capture_output=True)
    if p.returncode != 0:
        return None
    import yaml
    return yaml.safe_load(p.stdout) or {}


def sops_write_domain(domain, mapping):
    """Encrypt `mapping` into instance/secrets/<domain>.sops.yml, creating the file if missing. The plaintext
    transits sops' STDIN only — never argv (unlike `sops --set`) and never a temp file — so the values can't
    leak via `ps` or a half-written cleartext file. `--filename-override` makes sops apply the correct
    /.sops.yaml creation_rule (the right recipients) even though the data arrives on stdin. Returns True on
    success. The whole-file re-encrypt is how the secret-onboarding service writes a domain (decrypt → merge
    in memory → write). Tests monkeypatch this; nothing here prints a value."""
    # WRITE/CREATE target: overlay_target (dir-active) so a NEW domain lands in the ACTIVE overlay, and the
    # --filename-override MATCHES it so sops applies the right .sops.yaml creation_rule (recipients). _domain_path
    # (resolve, file-existence) is for the decrypt READ; the write must agree with the staged git-add path.
    rel = paths.overlay_target("ansible/secrets/%s.sops.yml" % paths.component(domain, "secret domain"))
    path = os.path.join(paths.write_root(), rel)
    import yaml
    plaintext = yaml.safe_dump(mapping, sort_keys=True, allow_unicode=True).encode()
    # --config is REQUIRED: sops discovers .sops.yaml by walking up from the input file's dir, but the input is
    # /dev/stdin (no real location) and the overlay's .sops.yaml lives in a SUBdir (instance/), so cwd-walk-up
    # can't find it. Point sops at it explicitly. (The per-domain path_regex is basename-anchored — sops matches
    # the filename relative to the .sops.yaml dir, so a dir prefix would never match; manifest coupling 1.)
    p = subprocess.run(
        ["sops", "--encrypt", "--input-type", "yaml", "--output-type", "yaml",
         "--config", paths.resolve(".sops.yaml"),
         "--filename-override", rel, "/dev/stdin"],
        input=plaintext, capture_output=True)
    if p.returncode != 0:
        print("  ! sops encrypt of %s failed (rc=%d) — domain unchanged" % (domain, p.returncode))
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)   # a fresh node's overlay has no secrets/ dir yet (scaffold skips it)
    with open(path, "wb") as fh:
        fh.write(p.stdout)
    print("  + sops wrote %s (%d field(s))" % (domain, len(mapping)))
    return True


def age_keygen():
    """Mint a fresh age keypair via `age-keygen` (its CSPRNG). Returns {"public": "age1…", "private":
    "AGE-SECRET-KEY-…"} or None on failure. The PRIVATE key is the crown jewel: it is returned to the caller
    IN MEMORY only — this function deliberately prints NOTHING (unlike the narrated git/sops shell-outs, whose
    `$ cmd` echo would here risk surfacing the key) and never writes it to disk. The service shows it to the
    operator exactly once and discards it; nothing logs or commits it. Tests monkeypatch this (no real age)."""
    try:
        p = subprocess.run(["age-keygen"], capture_output=True, text=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    public = private = None
    for line in (p.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("AGE-SECRET-KEY-"):
            private = s
        elif s.lower().startswith("# public key:"):
            public = s.split(":", 1)[1].strip()
    return {"public": public, "private": private} if public and private else None


def _push_target(run_id=None):
    """The branch `commit_and_push` takes the commit to on the canonical. A network service deployed with
    `KONTROLL_STAGE_PUSHES=1` (the propose-then-promote posture, SECURITY.md C10) pushes a per-request STAGING
    ref `proposed/<run_id>` — never `main` — that the trusted `promote_ref` step fast-forwards into `main`; so a
    leaked token can only STAGE a proposal: the token never pushes `main` and cannot promote — only a trusted
    promoter can. Unset env (the operator CLI / a non-staging deploy) ⇒ `main` — which is not pushed either: it
    is LANDED through the promote gate (`land_main`), the one way `refs/heads/main` moves.

    FAIL-CLOSED: when staging is on but no `run_id` is supplied, this RAISES — it never silently falls back to a
    direct `main` push. The whole job of this primitive is to be fail-closed; an unnamed proposal from a staging
    service is a caller bug (a route that forgot to thread `run_id`), not a 'safe default'. Surfacing it loudly
    keeps a future refactor / a None slipping through from collapsing the C10 invariant to a direct-main write."""
    if os.environ.get("KONTROLL_STAGE_PUSHES"):
        if not run_id:
            raise ValueError("KONTROLL_STAGE_PUSHES is set but no run_id was given — refusing to push main from "
                             "a staging service (a proposal must be named). C10 fail-closed; this is a caller bug.")
        return "proposed/%s" % run_id
    return "main"


def _reset_content_clone_to_canonical():
    """Return the STAGING content clone's working tree to the canonical `main` after a stage — so the GUI/API
    read-back ("current") always reflects the canonical source of truth, never an accumulation of un-promoted
    proposals. Best-effort (`_run`, unchecked): the proposal is already safely on the canonical `proposed/<run_id>`
    ref, so a transient fetch/reset failure must never fail an otherwise-successful stage (it degrades to the old
    drift for one cycle; a deploy re-clone resets it). Staging-only — see commit_and_push.

    WHY (the bug this closes, live-caught on a real deploy, dogfood 2026-06-20): commit_and_push commits each
    staged reconfigure on the clone's LOCAL `main`, advancing it. Without this reset the clone drifts forward with
    every stage, so a fresh `build_plan` reads the drifted tree — proposals STACK (proposal B = `main + A + B`),
    a REJECTED edit (its canonical ref deleted) keeps contaminating later diffs, and — since promote is FF-only —
    promoting B silently also enacts the abandoned A. Resetting to `local/main` makes each proposal INDEPENDENT
    off the canonical main (`promote_ref` already expects "re-propose against current main" once main moves). The
    unit suite uses a fresh tmp repo per test, so it cannot see this cross-request accumulation — covered by
    tests/unit/test_staging_isolation.py (two stages, one persistent clone). C10 /
    docs/privileged-mutation-enablement.md."""
    _run(["git", "fetch", "local"])
    _run(["git", "reset", "--hard", "local/main"])


def _rebase_proposal_onto_canonical():
    """Before pushing a staged proposal, REBASE the just-committed change onto the CURRENT canonical `main` — so a
    proposal is always a fast-forward of main even when an EARLIER proposal was PROMOTED out-of-band between two
    stages (the promote advances canonical main while this content clone's `local/main` tracking ref is still stale).

    WHY (the bug this closes, dogfood-caught 2026-06-29 on the .52 baked prod box): the clone commits each proposal
    on its LOCAL `main`, which `_reset_content_clone_to_canonical` last set to `local/main` AFTER the *previous*
    stage — i.e. BEFORE that proposal was promoted. So once the operator promotes proposal A (canonical main →
    seed+A), the NEXT stage B commits on the stale `seed` and pushes `proposed/B = seed+B`, which is NOT a
    fast-forward of canonical main (= seed+A) → `kontroll-promote`/`promote_ref` refuses it ("not a fast-forward;
    re-propose against current main"). Re-fetching + rebasing the single proposal commit onto current `local/main`
    makes `proposed/B = seed+A+B'`, a clean FF — no manual `git fetch && reset --hard local/main` between promotes
    (the live workaround this replaces). On a GENUINE conflict (the proposal and the promoted change touch the same
    lines) the rebase is ABORTED and the proposal pushed as-is — `promote_ref`'s FF-gate then correctly refuses it
    (today's behaviour for a real conflict; strictly better than a silently non-FF proposal). Best-effort `_run`
    throughout: the working tree is clean here (the apply's write is already committed), and a no-op rebase (when
    `local/main` is an ancestor of HEAD — e.g. NO out-of-band promote) leaves the proposal sha untouched. Staging-
    only — see commit_and_push. C10 / docs/privileged-mutation-enablement.md."""
    _run(["git", "fetch", "local"])
    if _run(["git", "rebase", "local/main"]) != 0:
        _run(["git", "rebase", "--abort"])


def _for_run(run_id):
    """The run id as the tail of a narrated line — what ties the line to the request's audit entry — or nothing."""
    return " (run_id %s)" % run_id if run_id else ""


def _drop_refused(rel_paths, run_id=None):
    """Remove what an apply wrote and git would not take: the FILES among `rel_paths` still on disk and untracked
    after the staging roll-back. `git reset --hard` restores every tracked file and removes one that was added,
    but it leaves an IGNORED file where it is — and the service's readers then see a write that reached no
    proposal (a unit descriptor that exists nowhere but this clone, listed as registered). Files only, and matched
    literally: a directory is never handed to `git clean` (it removes one recursively, with or without `-d`), and
    a name holding a wildcard stands for that one name — so nothing but a file at one of the request's own paths
    can go. A path that does not resolve inside the write root is skipped. A clean that fails says so: under `-q`
    there would be no line of ours for a file that is still there. Staging-only — see commit_and_push."""
    files = [rel for rel in rel_paths if paths.confined_file(rel)]
    if files and _run(["git", "--literal-pathspecs", "clean", "-f", "-x", "-q", "--"] + files) != 0:
        print("  ! could not remove a file git refused — it is still in the clone%s" % _for_run(run_id))


def _unstage(rel_paths, run_id=None):
    """Take `rel_paths` back out of the index, and touch no file. For a caller that is NOT the staging service —
    the GUI run from a checkout: its tree is the operator's and is never rolled back, but a request that did not
    commit must not leave a part of itself staged either. The next request's commit takes the whole index, and
    would publish that part under its own message, after the first was answered "nothing committed". Matched
    literally, like the clean. (The add before it is not: no producer builds a path with a wildcard or a leading
    colon — paths.component.) Something staged by hand at one of the same paths is un-staged with it — the add had
    already replaced it in the index; its content stays in the working tree. With NO path there is nothing to
    take back, and nothing is run: a `git reset` without a path resets the whole index. A reset that fails (the
    index is locked) says so, because the request's paths are then still staged. See commit_and_push."""
    if not rel_paths:
        return
    if _run(["git", "--literal-pathspecs", "reset", "-q", "--"] + list(rel_paths)) != 0:
        print("  ! could not take the request's paths back out of the index — they are still staged%s"
              % _for_run(run_id))


def commit_and_push(paths, message_lines, push_origin=False, run_id=None):
    """Stage `paths`, commit with the given -m `message_lines`, take the commit to the LOCAL canonical, and
    optionally push origin. Returns {"committed","pushed_canonical","pushed_origin","commit_rc","target_ref",
    "staged"} (+ "add_rc" when the stage itself was refused). A failed commit pushes NOTHING (never a stale
    HEAD), and so does a refused `git add`: git exits non-zero for a path a .gitignore rule ignores (having staged
    the others) and for one that is not there (having staged nothing) — so committing after it would publish a
    proposal that is MISSING a file and report it as staged. All of `paths` land, or none; and a request that
    does not commit leaves nothing of itself in the index: the staging service's clone is rolled back (below),
    any other caller gets the request's paths taken back out of its index (`_unstage`) and keeps its working
    tree. A staging network service (`KONTROLL_STAGE_PUSHES=1`, C10) PUSHES a `proposed/<run_id>` STAGING ref —
    pass `run_id` so the operator can promote the exact proposal. Any other caller (the GUI run from a checkout,
    a non-staging deploy) LANDS the commit on `main` through `land_main`: judged by the promote gate, brought
    across as a pack on a pipe, compare-and-swapped — never a push (the canonical's hook refuses a push of `main`
    from everyone). `pushed_canonical` is then "the commit is `main`" (landed, or there already). The API + GUI
    routes share this; the CLI keeps its own inline gate. Goes through _run (and `land_main`), so tests patch
    the two seams.

    The optional `origin` push is the OPERATOR CLI's offsite backup ONLY: it is unreachable while STAGING
    (target != main, `and not staging`), so a staging network service can never push HEAD (-> main) to any remote —
    including the common topology where the content clone's `origin` IS the canonical (C10 origin-push breach,
    dogfood VM 144 2026-07-05). `pushed_origin` is therefore always False under staging (honest: nothing was pushed
    to origin).

    STAGING (target != main) resets the content clone back to the canonical `main` after the push (and after a
    failed commit or a refused add, to drop the half-written tree — after a refused add the files git would not
    take are removed as well, `_drop_refused`) so the next read-back is the source of truth, never this stage's
    drift — see `_reset_content_clone_to_canonical`. A caller with target == main is NEVER reset: it advances
    `main` directly and its working tree is the operator's."""
    target = _push_target(run_id)   # resolve FIRST: a staging service without a run_id raises before any mutation
    staging = target != "main"
    arc = _run(["git", "add"] + list(paths))
    if arc != 0:                    # git refused a path and staged the rest: never commit a part of a change
        print("  ! git add refused a path (ignored by a .gitignore rule, or missing) — nothing committed%s"
              % _for_run(run_id))
        if staging:                 # drop the index, the tracked edits, and the refused files the apply wrote
            _reset_content_clone_to_canonical()
            _drop_refused(paths, run_id)
        else:                       # the operator's tree: only this request's paths leave the index
            _unstage(paths, run_id)
        return {"committed": False, "pushed_canonical": False, "pushed_origin": False,
                "commit_rc": None, "add_rc": arc, "target_ref": None, "staged": False}
    crc = _run(["git", "commit"] + commit_message_args(message_lines))   # + the one configurable trailer
    if crc != 0:
        if staging:                 # a failed commit left the apply's write in the tree — drop it so the next read is clean
            _reset_content_clone_to_canonical()
        else:                       # ...and outside staging what it staged must not ride in the next request's commit
            _unstage(paths, run_id)
        return {"committed": False, "pushed_canonical": False, "pushed_origin": False,
                "commit_rc": crc, "target_ref": None, "staged": False}
    if staging:                     # rebase the proposal onto CURRENT canonical main so it stays a FF even after an
        _rebase_proposal_onto_canonical()   # out-of-band promote moved main between this stage and the last (see helper)
        canonical = _run(["git", "push", "local", "HEAD:refs/heads/%s" % target]) == 0
    else:                           # main is never pushed: it is landed, judged first (rule 8 of the canonical's doors)
        canonical = land_main(run_id=run_id) == 0
    # C10 (dogfood VM 144, 2026-07-05): a STAGING network service must have NO reachable path to push HEAD (-> its
    # checked-out `main`) to ANY remote. On the content clone `origin` IS the canonical (git clone file:///srv/
    # kontroll.git; deploy-stack.yml api ~812/836, gui ~931/955), so `git push origin HEAD` fast-forwarded canonical
    # `main` UNREVIEWED from the network service -- defeating propose-then-promote. `staging` (target != main, above)
    # is the exact discriminator: every armed service resolves True, every operator-CLI/non-staging path False. `not
    # staging` makes the origin push DEAD CODE while staging (the operator-CLI path is byte-identical to before this
    # fix). The FF-only blast was bounded because the clone is always rebased onto canonical main before the push
    # (_rebase_proposal_onto_canonical, above) -- never a force -- but it advanced main all the same. SECURITY.md C10.
    origin = (not staging) and bool(push_origin) and _run(["git", "push", "origin", "HEAD"]) == 0
    if staging:                     # the proposal lives on as proposed/<run_id>; return the clone to canonical main
        _reset_content_clone_to_canonical()
    return {"committed": True, "pushed_canonical": canonical, "pushed_origin": origin, "commit_rc": 0,
            "target_ref": target, "staged": target != "main"}


def promote_ref(run_id, cwd=None):
    """Promote the proposal a RUN ID names: read `main` and `refs/heads/proposed/<run_id>` once, each by its full
    name (`canon.read_ref`), and land the commit that read returned (`promote_commit`). Kept for a caller that
    holds a run id and nothing else (the permissive edition's automaton is one; in this tree its callers are the
    tests); `scripts/kontroll-promote.py` — the promote a person or the Semaphore job
    runs — reads the refs itself, judges the commit, and calls `promote_commit` with the id it judged. Returns
    False (and says why) when either ref is absent, is a symbolic ref, or names no commit."""
    repo = canon.git_dir(cwd or paths.write_root())
    try:
        main = canon.read_ref(repo, canon.MAIN)
        proposal = canon.read_ref(repo, canon.proposal_ref(run_id))
    except ValueError:
        print("  ! refuse: that is not a run id — nothing promoted")
        return False
    except canon.Unreadable as e:
        print("  ! refuse: the canonical could not be read (%s) — nothing promoted" % e)
        return False
    if main is None or main[2] or main[1] != "commit":
        print("  ! refuse: cannot read refs/heads/main as a branch that names a commit (no canonical main to "
              "fast-forward) — nothing promoted")
        return False
    if proposal is None or proposal[2] or proposal[1] != "commit":
        print("  ! refuse: no proposal proposed/%s (no such ref, or the ref of that name is not a proposal) — "
              "nothing promoted" % run_id)
        return False
    return promote_commit(run_id, proposal[0], main[0], repo)


def promote_commit(run_id, oid, main_id, repo):
    """The trusted PROMOTE half of C10's propose-then-promote, for ONE COMMIT: make `refs/heads/main` the commit
    `oid`, provided `main` is still `main_id` and `refs/heads/proposed/<run_id>` is still `oid` — then retire the
    proposal. `repo` is the canonical's git directory. Returns True when `main` is `oid`.

    WHAT IS JUDGED IS WHAT IS PROMOTED. The caller read both refs once and judged the commit `oid` (the gate, the
    human's pin). Nothing here looks a name up again: the fast-forward question is asked of the two ids, and ONE
    ref transaction (`update-ref --stdin`: `verify` the proposal, `update` main with its old value, both under
    `option no-deref`) either finds both refs exactly as they were read and moves `main`, or changes nothing. A
    proposal re-pointed after it was judged — a second push to the same name — fails the `verify`; a `main` moved
    by another promote fails the old value (2026-10-08 review, finding 9: two promotes racing used to let the
    second overwrite the first). `no-deref`: a ref that turned SYMBOLIC is compared and moved as the name it is,
    never as the ref it points at.

    The proposal is retired by compare-and-delete (`update-ref --no-deref -d <ref> <oid>`), also without following
    a pointer: a proposal that is a symbolic ref costs that one name, not the ref it points at. A failure there is
    a note — `main` has moved, and that is what was asked for.

    Every git here runs in the seam (`canon.git_kwargs`): no hook of the canonical runs on the update, and of the
    caller's environment git is handed where programs and temporary files are and the locale, and nothing else —
    a promote is run by an identity that holds more than the service does."""
    try:
        ref = canon.proposal_ref(run_id)                       # the only way a string of the caller's gets into the
    except ValueError:                                         # transaction text below: a minted id, or nothing
        print("  ! refuse: that is not a run id — nothing promoted")
        return False
    if not (canon.valid_oid(oid) and canon.valid_oid(main_id)):
        print("  ! refuse: the commits to promote are not named by full ids — nothing promoted")
        return False
    if oid == main_id or canon.is_ancestor(repo, main_id, oid) is not True:
        print("  ! refuse: proposed/%s is not a fast-forward of main (re-propose against current main)" % run_id)
        return False
    txn = ("start\n"
           "option no-deref\nverify %s %s\n"
           "option no-deref\nupdate %s %s %s\n"
           "prepare\ncommit\n" % (ref, oid, canon.MAIN, oid, main_id)).encode("ascii")
    try:
        p = subprocess.run(["git", "update-ref", "--stdin"], input=txn, capture_output=True, timeout=30,
                           **canon.git_kwargs(repo, fed=True))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        # git did not start, was refused the repository, or was stopped at its time limit. Only the last can have
        # moved anything, and a git that is stopped leaves its lock files: say what `main` IS, from a fresh read.
        try:
            now = canon.read_ref(repo, canon.MAIN)
        except canon.Unreadable:
            now = None
        if now != (oid, "commit", ""):
            print("  ! refuse: git could not run the promote on the canonical, or did not finish — %s"
                  % ("the canonical could not be read afterwards" if now is None else
                     "nothing promoted, main is not this commit"))
            return False
        p = None                                               # it finished the move before it was stopped
    if p is not None and p.returncode != 0:
        print("  ! refuse: %s — nothing promoted, main is untouched" % _what_moved(repo, ref, oid, main_id))
        return False
    try:
        rc = subprocess.run(["git", "update-ref", "--no-deref", "-d", "--end-of-options", ref, oid],
                            capture_output=True, timeout=30, **canon.git_kwargs(repo)).returncode
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        rc = 1
    if rc != 0:                                                # best-effort: main is already advanced
        print("  ~ note: promoted main, but could not delete proposed/%s (stale ref left behind)" % run_id)
    print("  + promoted proposed/%s (commit %s) -> main" % (run_id, oid))
    return True


def _what_moved(repo, ref, oid, main_id):
    """Why the promote's transaction was refused, said from a fresh read of the two refs — never from git's own
    error text, which is in the caller's locale and quotes names."""
    try:
        main, proposal = canon.read_ref(repo, canon.MAIN), canon.read_ref(repo, ref)
    except canon.Unreadable:
        return "the canonical could not be read after the promote was refused"
    if main is None or main[0] != main_id or main[2]:
        return ("main moved while this proposal was being promoted (another promote landed first) — run the "
                "promote again to check it against the new main")
    if proposal is None or proposal[0] != oid or proposal[2]:
        return ("the proposal is no longer the commit that was judged (it was re-pointed or removed) — look at "
                "it again before promoting")
    return ("git refused the transaction although both refs are as they were read: a lock file is held in the "
            "canonical (another promote is running — run this one again; or a git that was stopped left one "
            "behind — docs/troubleshooting.md §7), or this user cannot write the canonical")


# ---- the landing: the one way `main` moves that is not a promote (C10, rule 8 of the canonical's doors) ----------- #

class GateCouldNotRun(Exception):
    """The promote gate did not judge a tree: this checkout has no generator, git could not read the repository,
    the generator could not start or did not finish. Not a verdict — and not a pass: a landing is refused with
    this sentence (exit 2), and `--skip-conflict-check` is the human's way past a gate that cannot run.
    `scripts/kontroll-promote.py`'s own class is a subclass, so one `except` here catches the gate's own raise."""


NOT_A_FAST_FORWARD = ("the canonical (%s) has commit(s) your worktree (%s) does not, so this landing would discard "
                      "them. That is normal once propose-then-promote is in use: a promote advances canonical "
                      "`main` directly. Bring the worktree forward first — `git -C %s fetch local && git -C %s "
                      "rebase local/main` — then run again. Nothing was packed and the canonical is untouched.")
_LAND_SECONDS = 600          # a pack of the whole shipped tree takes a second; this is the bound, not the budget


def _the_gate():
    """The promote gate — `scripts/kontroll-promote.py`'s `_tree_refusal(commit_id, gitdir, main_id)`, the one
    judge of a tree this tool has (it runs THIS checkout's collection generator over the commit's files, read
    object by object and re-hashed). Loaded from the script beside this package, so that the gate the landing
    runs is the gate the promote runs and no second copy of it can drift. Raises `GateCouldNotRun` when the
    script is not there to load: a checkout without it cannot judge, and a landing that cannot judge refuses."""
    scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        return importlib.import_module("kontroll-promote")._tree_refusal
    except Exception:                                          # ImportError, or a script that does not import
        raise GateCouldNotRun("this checkout has no scripts/kontroll-promote.py to judge the tree with") from None


def _wt(worktree, argv, stdin=None, timeout=60):
    """Plain git in the CALLER'S OWN repository — the worktree a landing takes its commit from. Not the seam: the
    seam is for the canonical, which somebody else writes; this repository is the caller's, and git runs in it as
    it does for the caller's own `git commit`. Returns the completed process (stdout and stderr captured)."""
    return subprocess.run(["git", "-C", worktree] + list(argv), input=stdin, capture_output=True, timeout=timeout)


def _pack(worktree, oid, old):
    """Start `git pack-objects --revs --stdout` in the worktree: a pack of every object reachable from `oid` and
    not from `old` (every object, when `old` is None — a first seed), on its standard output. No `--thin`: a thin
    pack would be made of deltas against objects the packer ASSUMES the canonical holds, and nothing here reads
    the canonical's store to find out. The canonical holds `old` whole (it is its own `main`), so the pack holds
    exactly what it lacks."""
    revs = (oid + "\n" + ("^" + old + "\n" if old else "")).encode("ascii")
    p = subprocess.Popen(["git", "-C", worktree, "pack-objects", "--revs", "--stdout", "-q"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p.stdin.write(revs)
    p.stdin.close()
    return p


def _land_in_canonical(gitdir, target, oid, old, base, packer):
    """The canonical's side of a landing, every git of it in the seam (`canon.git_kwargs`): read the pack
    `packer` writes into the canonical's object store (`git index-pack --stdin`, which refuses a pack whose
    checksum or whose objects do not hash to their names); check that `oid` and everything it names is now
    reachable there (`git rev-list --objects --quiet oid ^base`, `base` being the canonical's `main` the pack was
    made against — a pack short of an object is refused before any ref names it); then ONE ref transaction under
    `option no-deref`: `target` becomes `oid` provided it is still `old` (`old` None, the zero id: provided it
    does not exist — a proposal, or `main` on a first seed), or nothing changes. Returns `(rc, message)`.

    No transport. A push or a fetch into the canonical would run its hooks and the programs its config names, as
    the pusher (`core.hooksPath`, `receive.*`, `uploadpack.*`, `remote.<name>.uploadpack`) — this reads a pipe
    and writes a pack, and the seam points every such key at nothing and runs root's git as the service. The
    first seed (`old` None) creates the ref with the zero id as its old value: a second seed answers "exists"."""
    zero = "0" * len(oid)
    try:
        received = subprocess.run(["git", "index-pack", "--stdin"], stdin=packer.stdout, capture_output=True,
                                  timeout=_LAND_SECONDS, **canon.git_kwargs(gitdir, fed=True))
    except canon.Unreadable as e:
        packer.kill()
        return 1, "the canonical could not be written to (%s)" % e
    except (OSError, subprocess.SubprocessError):
        packer.kill()
        return 1, "git could not receive the pack into the canonical, or did not finish; nothing was landed"
    finally:
        packer.stdout.close()
        packer.wait()
    if packer.returncode != 0:
        return 1, "the worktree could not pack the commit (is `%s` a commit of it?); nothing was landed" % oid
    if received.returncode != 0:
        return 1, ("the canonical refused the pack (an object in it is not what its name says, or the pack is cut "
                   "short); nothing was landed")
    try:                                                       # two literal argv: the seam pin reads each as written
        if base:
            whole = subprocess.run(["git", "rev-list", "--objects", "--quiet", oid, "^" + base],
                                   capture_output=True, timeout=_LAND_SECONDS, **canon.git_kwargs(gitdir))
        else:
            whole = subprocess.run(["git", "rev-list", "--objects", "--quiet", oid],
                                   capture_output=True, timeout=_LAND_SECONDS, **canon.git_kwargs(gitdir))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        return 1, "the canonical could not be read back after the pack was received; nothing was landed"
    if whole.returncode != 0:
        return 1, ("the commit is not whole in the canonical after the pack was received (an object it names is "
                   "missing there); nothing was landed")
    txn = ("start\noption no-deref\nupdate %s %s %s\nprepare\ncommit\n" % (target, oid, old or zero)).encode("ascii")
    try:
        moved = subprocess.run(["git", "update-ref", "--stdin"], input=txn, capture_output=True, timeout=30,
                               **canon.git_kwargs(gitdir, fed=True))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        moved = None
    try:
        now = canon.read_ref(gitdir, target)
    except canon.Unreadable:
        now = None
    if now == (oid, "commit", ""):
        return 0, "landed %s %s (was %s)" % (target, oid, old or "nothing: a first seed")
    if moved is None:
        return 1, ("git could not run the transaction on the canonical, or did not finish — %s is not this commit"
                   % target)
    if old is None:
        return 1, "%s exists already (%s): it is created once, and this canonical has it" % (
            target, now[0] if now else "it could not be read")
    if now is None or now[2] or now[0] != old:
        return 1, ("%s moved while this landing was being judged (%s); nothing was landed — run again to judge "
                   "against it as it is now" % (target, "it is %s now" % now[0] if now and not now[2] else
                                                "it is gone, or symbolic"))
    return 1, ("git refused the transaction although %s is as it was read: a lock file is held in the canonical "
               "(another landing or promote is running — run this one again; or a git that was stopped left one "
               "behind — docs/troubleshooting.md §7), or this user cannot write the canonical" % target)


def land_commit(worktree, canonical, target=canon.MAIN, skip_gate=False, check=False, gate=None):
    """Bring the commit at `worktree`'s HEAD into the canonical at `canonical` and make `target` name it — the one
    way into `refs/heads/main` that is not a promote, and the only way a proposal is made by hand (`target` a
    `refs/heads/proposed/<run id>`, created). Returns `(rc, message)`: 0 landed, or up to date, or (`check`) would
    land; 1 nothing to land as asked (not a repository, no HEAD, the canonical unreadable or its `main` not a
    branch, not a fast-forward, the ref moved meanwhile, git failed); 2 REFUSED by the gate, or the gate could
    not run — `skip_gate` is the human's override and nothing automatic passes it. After any rc but 0 the
    worktree keeps its commit, the canonical its refs, and the running stack is as it was.

    THE RULE (SECURITY.md C10): a commit id, judged by the gate, brought across as a pack on a pipe,
    compare-and-swapped. In order: C = HEAD's commit (`rev-parse`, in the worktree); M = the canonical's
    `refs/heads/main`, read once by its full name in the seam (`canon.read_ref`; None on a first seed; a
    symbolic ref or a non-commit refuses); M must be an ancestor of C IN THE WORKTREE (`merge-base
    --is-ancestor`: rc 1 — a real divergence — and rc 128 — M never fetched — both refuse, with the sentence the
    play printed before); `gate(C, <the worktree's git dir>, M)` judges C's tree as the promote would judge a
    proposal's — read object by object from the CALLER'S repository, re-hashed, this checkout's generator over
    the files — before anything touches the canonical; `check` stops here. Then `_land_in_canonical`.

    What this is not: a push. `scripts/update.sh`, `scripts/update-from-bundle.sh`, the operator CLI's onboard
    and the deploy's mirror used to `git push … HEAD:refs/heads/main`, which ran the canonical's `update` hook
    and whatever its config named, as the pusher (the operator, or root in the installer) — and passed no gate,
    so a fleet line the promote refuses was on `main` the moment a deploy ended. Now the canonical's hook refuses
    a push of `main` from everyone, and the four doors call this. `gate` is the promote's own (`_the_gate`),
    unless a caller hands one in."""
    worktree = os.path.abspath(worktree)
    gitdir = canon.git_dir(os.path.abspath(canonical))
    if target != canon.MAIN:
        run_id = target[len(canon.PROPOSED):] if target.startswith(canon.PROPOSED) else ""
        if not canon.valid_run_id(run_id):
            return 1, "the target must be refs/heads/main or refs/heads/proposed/<a minted run id>"
    try:
        head = _wt(worktree, ["rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD^{commit}"])
        where = _wt(worktree, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    except (OSError, subprocess.SubprocessError):
        return 1, "git could not be run in the worktree %s; nothing was landed" % worktree
    commit_id = head.stdout.decode("ascii", "replace").strip()
    if head.returncode != 0 or not canon.valid_oid(commit_id) or where.returncode != 0:
        return 1, ("the worktree %s has no commit at HEAD (is it a repository, with a commit?); nothing was landed"
                   % worktree)
    wt_gitdir = where.stdout.decode("utf-8", "replace").strip()
    try:
        main_ref = canon.read_ref(gitdir, canon.MAIN)
        existing = main_ref if target == canon.MAIN else canon.read_ref(gitdir, target)
    except canon.Unreadable as e:
        return 1, "the canonical could not be read (%s); nothing was landed" % e
    if main_ref is not None and (main_ref[2] or main_ref[1] != "commit"):
        return 1, ("refs/heads/main of the canonical is a symbolic ref, or names no commit: there is nothing to "
                   "land on, and nothing is landed beside it")
    main_id = main_ref[0] if main_ref else None
    if target != canon.MAIN and existing is not None:
        return 1, "%s exists already: a proposal is created once (stage a new run id)" % target
    if main_id:
        if main_id == commit_id:
            if target == canon.MAIN:
                return 0, "up to date — refs/heads/main is %s already" % commit_id
            return 1, "HEAD is the canonical's main already (%s): there is nothing to propose" % commit_id
        try:
            behind = _wt(worktree, ["merge-base", "--is-ancestor", main_id, commit_id]).returncode
        except (OSError, subprocess.SubprocessError):
            behind = -1
        if behind != 0:
            return 1, NOT_A_FAST_FORWARD % (gitdir, worktree, worktree, worktree)
    if not skip_gate:
        try:
            verdict = (gate or _the_gate())(commit_id, wt_gitdir, main_id)
        except GateCouldNotRun as e:
            return 2, ("the gate could not run, so the tree was not judged: %s. Nothing was landed. Correct what "
                       "that names and run again (--skip-conflict-check lands WITHOUT the gate — the human's "
                       "override, for a node that must move while the gate cannot run)." % e)
        if verdict:
            return 2, ("the tree does not resolve, so the next bootstrap (and, for a fleet line, the next deploy) "
                       "would stop: %s. Nothing was landed; the fix is a commit in the worktree "
                       "(--skip-conflict-check overrides this gate)." % verdict)
    if check:
        return 0, "would land %s %s (was %s); nothing was changed" % (target, commit_id,
                                                                      main_id or "nothing: a first seed")
    try:
        packer = _pack(worktree, commit_id, main_id)
    except OSError:
        return 1, "git could not be run in the worktree %s; nothing was landed" % worktree
    return _land_in_canonical(gitdir, target, commit_id, main_id if target == canon.MAIN else None, main_id, packer)


def _canonical_of(worktree):
    """The canonical the worktree's `local` remote names, as a directory: `git remote get-url local`, a path or a
    `file://` URL (what `local-canonical.yml` adds, and what a content clone's `origin` is). None when there is
    no such remote, or it is not a directory on this machine — the canonical is on the node by definition, and
    a landing uses no transport."""
    try:
        p = _wt(worktree, ["remote", "get-url", "local"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    url = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0 or not url:
        return None
    if url.startswith("file://"):
        url = url[len("file://"):]
    if "://" in url:
        return None                                            # ssh://, https://: not on this machine
    if not os.path.isabs(url):
        url = os.path.join(worktree, url)                      # git takes a relative URL from the worktree
    return os.path.abspath(url) if os.path.isdir(url) else None


def land_main(run_id=None, skip_gate=False):
    """Land the write root's HEAD on the canonical its `local` remote names — what the operator CLI and a
    non-staging `commit_and_push` do where they pushed `HEAD:refs/heads/main` before. Narrated like `_run` (a
    `$` line, then the verdict), so an operator at the CLI reads what happened; `run_id` ties the line to the
    request's audit entry. Returns the landing's rc (0 landed or up to date; 1 nothing to land, or no `local`
    remote; 2 refused by the gate). The second seam tests patch beside `_run`: a test that lets this run would
    land on whatever canonical the checkout's `local` remote names."""
    worktree = paths.write_root()
    canonical = _canonical_of(worktree)
    if canonical is None:
        print("  ! the worktree has no `local` remote naming a canonical on this machine — nothing landed; run "
              "playbooks/local-canonical.yml (it sets the 'local' remote)%s" % _for_run(run_id))
        return 1
    print("  $ kontroll-land --repo %s   (refs/heads/main <- HEAD, judged first; never a push)" % canonical)
    rc, said = land_commit(worktree, canonical, skip_gate=skip_gate)
    print("  %s %s%s" % ("+" if rc == 0 else "!", said, _for_run(run_id)))
    return rc
