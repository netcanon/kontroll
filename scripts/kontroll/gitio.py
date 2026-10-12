"""Repo-mutation primitives shared by the onboard / capture-exception services.

_run — a narrated subprocess (the git/ansible shell-out SEAM the tests monkeypatch,
kontroll.gitio._run); _write_new — an idempotent, no-clobber drop-in file write (never
overwrite an operator edit); sops_set — encrypt one credential into a SOPS domain in
place. Every write + git op targets `paths.write_root()` (the WRITE root) read DYNAMICALLY,
so the tmp_repo fixture's repoint diverts it; a BAKED deploy points it at the propose clone
(the read-only image at ROOT has no .git, so git MUST run in the clone). Verbatim from galaxy.py.
"""
import json
import os
import subprocess

from kontroll import paths

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


def _out(cmd, cwd=None):
    """The READ twin of _run: capture one value (a ref's object id) for promote_ref's compare-and-swap. Reads
    only — every write still goes through _run, the one seam the tests monkeypatch and the read-only pins watch."""
    p = subprocess.run(cmd, cwd=cwd or paths.write_root(), capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


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
    """The branch `commit_and_push` pushes the canonical to. A network service deployed with
    `KONTROLL_STAGE_PUSHES=1` (the propose-then-promote posture, SECURITY.md C10) pushes a per-request STAGING
    ref `proposed/<run_id>` — never `main` — that the trusted `promote_ref` step fast-forwards into `main`; so a
    leaked token can only STAGE a proposal: the token never pushes `main` and cannot promote — only a trusted
    promoter can. Unset env (the operator CLI / a non-staging deploy) ⇒ direct to `main`.

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
    """Stage `paths`, commit with the given -m `message_lines`, push the LOCAL canonical, and
    optionally push origin. Returns {"committed","pushed_canonical","pushed_origin","commit_rc","target_ref",
    "staged"} (+ "add_rc" when the stage itself was refused). A failed commit pushes NOTHING (never a stale
    HEAD), and so does a refused `git add`: git exits non-zero for a path a .gitignore rule ignores (having staged
    the others) and for one that is not there (having staged nothing) — so committing after it would publish a
    proposal that is MISSING a file and report it as staged. All of `paths` land, or none; and a request that
    does not commit leaves nothing of itself in the index: the staging service's clone is rolled back (below),
    any other caller gets the request's paths taken back out of its index (`_unstage`) and keeps its working
    tree. The canonical push targets `main` by default, OR a `proposed/<run_id>` STAGING ref when the caller is a
    staging network service (`KONTROLL_STAGE_PUSHES=1`, C10) — pass `run_id` so the operator can promote the
    exact proposal. The API + GUI routes share this; the CLI keeps its own inline gate. Goes through _run, so
    tests patch the one seam.

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
    """The trusted PROMOTE half of C10's propose-then-promote: fast-forward `main` to the staged proposal
    `proposed/<run_id>`. REFUSES (returns False) unless the proposal is a strict fast-forward of `main` (so a
    proposal can never rewrite history — defence-in-depth with the bare repo's `receive.denyNonFastForwards`),
    then advances `main` and deletes the promoted proposal. Operator-run (the trusted human / a Semaphore
    approval task) against the canonical — the network service can only CREATE proposals, never run this. Goes
    through _run (the one monkeypatched seam), so the FF-gate is testable offline."""
    ref = "refs/heads/proposed/%s" % run_id
    # COMPARE-AND-SWAP, not check-then-set. Read the object id `main` has NOW, run the fast-forward check against
    # it, then hand that id to `update-ref` as the old-value operand: git refuses the update if `main` moved in
    # between ("is at X but expected Y"). Without it, two promotes racing (the Semaphore task and a CLI run, or
    # two CLI runs) let the second one overwrite the first with a ref that is no longer a fast-forward of what it
    # replaces — a promote lost silently while both report success (2026-10-08 review, finding 9).
    rc, main_was = _out(["git", "rev-parse", "--verify", "--quiet", "refs/heads/main"], cwd=cwd)
    if rc != 0 or not main_was:
        print("  ! refuse: cannot read refs/heads/main (no canonical main to fast-forward) — nothing promoted")
        return False
    if _run(["git", "merge-base", "--is-ancestor", "refs/heads/main", ref], cwd=cwd) != 0:
        print("  ! refuse: proposed/%s is not a fast-forward of main (re-propose against current main)" % run_id)
        return False
    if _run(["git", "update-ref", "refs/heads/main", ref, main_was], cwd=cwd) != 0:
        print("  ! refuse: main moved while promoting proposed/%s (another promote landed first) — "
              "re-run to re-check it against the new main" % run_id)
        return False
    if _run(["git", "update-ref", "-d", ref], cwd=cwd) != 0:   # best-effort: main is already advanced
        print("  ~ note: promoted main, but could not delete proposed/%s (stale ref left behind)" % run_id)
    print("  + promoted proposed/%s -> main" % run_id)
    return True
