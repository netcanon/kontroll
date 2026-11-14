"""Repo-mutation primitives shared by the onboard / capture-exception services.

_run — a narrated subprocess (the git/ansible shell-out SEAM the tests monkeypatch,
kontroll.gitio._run); _write_new — an idempotent, no-clobber drop-in file write (never
overwrite an operator edit); sops_set — encrypt one credential into a SOPS domain in
place. Every write + git op targets `paths.write_root()` (the WRITE root) read DYNAMICALLY,
so the tmp_repo fixture's repoint diverts it; a BAKED deploy points it at the propose clone
(the read-only image at ROOT has no .git, so git MUST run in the clone). Verbatim from galaxy.py.
"""
import errno
import hashlib
import importlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile

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


def _run(cmd, cwd=None, quiet_args=0, env=None):
    """Run `cmd` (shown, its last `quiet_args` arguments masked) and return its exit code; `env` adds variables to
    the caller's environment for this command alone."""
    shown = cmd[:len(cmd) - quiet_args] + (["***"] if quiet_args else [])
    print("  $ %s" % " ".join(shown))
    return subprocess.run(cmd, cwd=cwd or paths.write_root(), env=dict(os.environ, **env) if env else None).returncode


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

    Unlike `_write_new` (whole-file no-clobber), this MERGES via `owned_merge`: if the file opens with our `banner`
    (we wrote it), the new host(s) are added ALONGSIDE any siblings already there — so onboarding a 2nd device of an
    existing class never drops the 1st (#124, the "add a host to an existing class" path). A FOREIGN file (one that
    does not open with the banner — an operator rewrite) still hard-stops with WriteConflict unless
    `overwrite=True` (the deliberate escape hatch), preserving the no-clobber-operator-edits guarantee. Idempotent:
    an identical result is a no-op (returns False). The key-level overwrite is now visible through `owned_merge`'s
    collisions (G3) — the host-var reconfigure surface (service/hostvars) consumes them; onboard's additive add
    ignores them."""
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
                      "`main` directly. Bring the worktree forward first — `python3 -B scripts/kontroll-land.py "
                      "--sync --worktree %s --repo %s` (a deploy does this itself) — and read what it says came "
                      "in, which the next deploy runs as root; then run again. Nothing was packed and the "
                      "canonical is untouched.")
SHALLOW = ("the worktree %s is a shallow clone (made by `git clone --depth …`), and %s. The canonical takes a commit "
           "only with every commit under it. Fetch the rest of the history — `git -C %s fetch --unshallow` — then "
           "run again. Nothing was packed and the canonical is untouched.")
# The sync's: in a shallow clone, main below where it stops (an upgrade, no promote) shares no history with HEAD
# that git can see, and "not a checkout of this canonical" would be false (read A, S1). Nothing was packed into the
# canonical, but main's own pack came into the worktree, so the closing is the sync's.
# The dry-run cannot tell a promote from main below a shallow clone's cut: it does not hold main (read A, S1).
_SHALLOW_CHECK = (" The worktree is a shallow clone, and main is not in it: if main is a commit below where the clone "
                  "stops (an upgrade, not a promote), the real run cannot tell HEAD descends from it and refuses, "
                  "naming `git fetch --unshallow`.")
SHALLOW_SYNC = ("the worktree %s is a shallow clone (made by `git clone --depth …`), and whether the canonical's main "
                "(%s) and HEAD share history cannot be told: the clone stops before it. Fetch the rest of the history "
                "— `git -C %s fetch --unshallow` — then run again; nothing was replayed and HEAD was not changed")
_LAND_SECONDS = 600         # a pack of the whole shipped tree takes a second; this is the bound, not the budget


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


def _wt(worktree, argv, stdin=None, timeout=60, pipe=None, env=None):
    """Plain git in the CALLER'S OWN repository — the worktree a landing takes its commit from, and a sync brings
    forward. Not the seam: the seam is for the canonical, which somebody else writes; this repository is the
    caller's, and git runs in it under the caller's own config — but with no hook and no fsmonitor program
    (`core.hooksPath=/dev/null`, `core.fsmonitor=false`), not even the caller's own. A hooks path into the tree
    names hooks a promote can bring, and a sync runs git after the promote's files are on disk and before anyone has
    read them: a `git status` runs `post-index-change`, an `update-ref` `reference-transaction` (read A-f2, N1;
    read B-f2, S1). `stdin`: bytes git reads; `pipe`: a file git reads instead (another process's output); `env`
    adds variables to the caller's environment. Every git of a sync in the worktree runs through here (read A-f3,
    N1). Returns the completed process (stdout and stderr captured)."""
    return subprocess.run(["git", "-C", worktree, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
                          + list(argv), input=stdin, stdin=pipe, capture_output=True, timeout=timeout,
                          env=dict(os.environ, **env) if env else None)


def _shallow(worktree):
    """True when the worktree is a shallow clone (`git rev-parse --is-shallow-repository`): its history stops at
    commits whose parents were never fetched. False when git cannot say; the canonical's own read-back after the
    pack is received still refuses a commit that is not whole there."""
    try:
        p = _wt(worktree, ["rev-parse", "--is-shallow-repository"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and p.stdout.strip() == b"true"


# How the worktree's commits are read where a landing judges a shallow clone: no replace ref stands in for a commit
# and no `info/grafts` rewrites its parents (the canonical's seam sets both aside, and the pack is made so too),
# no lazy fetch from a promisor remote (GIT_NO_LAZY_FETCH, git 2.45 and later; `protocol.allow=never` before it), and
# no commit-graph: parents come from the commits. The check and the pack read the worktree alike.
_RECORDED = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull, "GIT_NO_LAZY_FETCH": "1"}
_NO_TRANSPORT = ["-c", "protocol.allow=never", "-c", "core.commitGraph=false"]


def _cut(worktree):
    """The commits where a shallow worktree's history stops: its `shallow` list, found by `rev-parse --git-path
    shallow` (a linked worktree's is its main checkout's). None when the list cannot be read or holds a line that is
    not an object id."""
    try:
        p = _wt(worktree, ["rev-parse", "--path-format=absolute", "--git-path", "shallow"], timeout=10)
        if p.returncode != 0:
            return None
        with open(p.stdout.decode("utf-8", "surrogateescape").strip(), "rb") as fh:
            ids = fh.read().decode("ascii").split()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    return ids if all(canon.valid_oid(i) for i in ids) else None


def _recorded_parents(worktree, oid):
    """The parents the commit `oid` records (`cat-file commit`, no replace ref standing in for it), or None when the
    worktree does not hold it or git cannot say."""
    try:
        p = _wt(worktree, _NO_TRANSPORT + ["cat-file", "commit", oid], env=_RECORDED)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    header = p.stdout.split(b"\n\n", 1)[0].decode("ascii", "replace").splitlines()
    parents = [ln[len("parent "):] for ln in header if ln.startswith("parent ")]
    return parents if all(canon.valid_oid(x) for x in parents) else None


def _held_whole(gitdir, oids, base):
    """Whether the canonical holds the commits `oids` and everything under them that `base` (its `main`; None on a
    first seed) does not name: its own read-back (`rev-list --objects --quiet`, the ids on standard input), in the
    seam. False when git cannot say."""
    revs = "".join(o + "\n" for o in oids) + ("^" + base + "\n" if base else "")
    try:
        p = subprocess.run(["git", "rev-list", "--objects", "--quiet", "--stdin"], input=revs.encode("ascii"),
                           capture_output=True, timeout=_LAND_SECONDS, **canon.git_kwargs(gitdir, fed=True))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        return False
    return p.returncode == 0


def _pack_view(worktree, gitdir, oid, base):
    """For a shallow worktree, the view the pack is made in, judged before anything is packed. Returns `(env,
    None)`, the environment `_pack` makes the pack in, or `(None, why)` when that pack would not be whole in the
    canonical.

    A commit where the clone stops (`_cut`) stays a stop only where the canonical holds the parents it records,
    and all under them (`_held_whole`): the pack may leave those out. Every other stop is lifted, so the walk goes on
    into its parents; where the clone does not hold them, the walk fails. Then the canonical's own read-back,
    `rev-list --objects --quiet oid ^base`, is made in the worktree in that view (a temporary GIT_SHALLOW_FILE
    naming the stops kept, and `_RECORDED`). Where it passes, the pack made in the same view carries every object
    under `oid` that `base` and the kept stops' parents do not name, and the canonical holds those: so it finds the
    commit whole. A merge whose cut-off side rests on history the canonical holds lands; a clone short of history
    the canonical lacks, or of an object a partial clone never fetched, is refused. Fails closed: a list that cannot
    be read, a git that cannot run or does not finish, refuses. `why` is the clause `SHALLOW` takes, and says only
    what was found: what git said, or that it could not judge."""
    cut = _cut(worktree)
    if cut is None:
        return None, "its shallow list could not be read, so what the pack would carry cannot be told"
    keep = []
    for c in cut:
        parents = _recorded_parents(worktree, c)
        if parents is not None and (not parents or _held_whole(gitdir, parents, base)):
            keep.append(c)
    fd, path = tempfile.mkstemp(prefix="kontroll-cut-")
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
        fh.write("".join(c + "\n" for c in keep))
    env = dict(_RECORDED, GIT_SHALLOW_FILE=path)
    argv = _NO_TRANSPORT + ["rev-list", "--objects", "--quiet", oid] + (["^" + base] if base else [])
    try:
        p = _wt(worktree, argv, timeout=_LAND_SECONDS, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        os.unlink(path)
        return None, "git could not finish judging what the pack would carry (%s)" % type(e).__name__
    if p.returncode != 0:
        os.unlink(path)
        said = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, ("HEAD needs objects that the clone does not hold and the canonical %s (git: %s)"
                      % ("does not either" if base else "cannot, having no main yet",
                         said[0][:200] if said else "rc %d" % p.returncode))
    return env, None


def _holds(worktree, oid):
    """Whether the worktree holds the commit `oid` (`cat-file -e`, no replace ref standing in for it, no lazy
    fetch). False when git cannot say."""
    try:
        return _wt(worktree, _NO_TRANSPORT + ["cat-file", "-e", oid + "^{commit}"], env=_RECORDED).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _after_the_cut(worktree, main_id, commit_id):
    """In a shallow worktree whose own view says `main_id` is not under `commit_id`, ask again with its stops lifted
    (each commit's parents as it records them, `_RECORDED`): True when `main_id` is under `commit_id` after all (a
    `--depth` clone whose HEAD sits on `main`, or a tip that a `fetch --depth` listed as a stop). False when it is
    not, or cannot be told (the walk reaches a parent the clone does not hold)."""
    try:
        p = _wt(worktree, _NO_TRANSPORT + ["merge-base", "--is-ancestor", main_id, commit_id],
                env=dict(_RECORDED, GIT_SHALLOW_FILE=os.devnull))
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def _pack(worktree, oid, old, view=None):
    """Start `git pack-objects --revs --stdout` in the worktree: a pack of every object reachable from `oid` and
    not from `old` (every object, when `old` is None — a first seed), on its standard output. No `--thin`: a thin
    pack would be made of deltas against objects the packer ASSUMES the canonical holds, and nothing here reads
    the canonical's store to find out. The canonical holds `old` whole (it is its own `main`), so the pack holds
    exactly what it lacks. `view`, a shallow worktree's (`_pack_view`), is the environment the pack is made in:
    the one its check walked."""
    revs = (oid + "\n" + ("^" + old + "\n" if old else "")).encode("ascii")
    p = subprocess.Popen(["git", "-C", worktree] + (_NO_TRANSPORT if view else []) +
                         ["pack-objects", "--revs", "--stdout", "-q"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=dict(os.environ, **view) if view else None)
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
    branch, not a fast-forward, a shallow clone whose pack would not be whole in the canonical, or that cannot
    show `main` under HEAD, the ref moved meanwhile, git failed); 2 REFUSED by the gate, or the gate could
    not run — `skip_gate` is the human's override and nothing automatic passes it. After any rc but 0 the
    worktree keeps its commit, the canonical its refs, and the running stack is as it was.

    THE RULE (SECURITY.md C10): a commit id, judged by the gate, brought across as a pack on a pipe,
    compare-and-swapped. In order: C = HEAD's commit (`rev-parse`, in the worktree); M = the canonical's
    `refs/heads/main`, read once by its full name in the seam (`canon.read_ref`; None on a first seed; a
    symbolic ref or a non-commit refuses); M must be an ancestor of C IN THE WORKTREE (`merge-base
    --is-ancestor`: rc 1 — a real divergence — and rc 128 — M never fetched — both refuse, with the sentence the
    play printed before; in a shallow worktree (`_shallow`) M may be under C below where the clone stops, so it
    is asked again with the stops lifted (`_after_the_cut`), and where M is not in the clone, or still not seen
    under C, refused with `SHALLOW`); in a shallow worktree the pack's own view is judged (`_pack_view`: a stop is
    kept only where the canonical holds the parents it records, and the canonical's read-back is made in the
    worktree in that view), or the pack would not be whole in the canonical — refused with `SHALLOW`, whose remedy
    is `git fetch --unshallow`; `gate(C, <the worktree's git dir>, M)` judges C's tree
    as the promote would judge a proposal's — read object by object from the CALLER'S repository, re-hashed, this
    checkout's generator over the files — before anything touches the canonical; `check` stops here. Then
    `_land_in_canonical`.

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
    shallow = _shallow(worktree)
    if main_id:
        if main_id == commit_id:
            if target == canon.MAIN:
                return 0, "up to date — refs/heads/main is %s already" % commit_id
            return 1, "HEAD is the canonical's main already (%s): there is nothing to propose" % commit_id
        try:
            behind = _wt(worktree, ["merge-base", "--is-ancestor", main_id, commit_id]).returncode
        except (OSError, subprocess.SubprocessError):
            behind = -1
        if behind != 0 and shallow:                        # main may be under HEAD, below where the clone stops
            if not _holds(worktree, main_id):
                return 1, SHALLOW % (worktree, "whether the canonical's main (%s) is under HEAD cannot be told: it "
                                     "is not in the clone (if a promote moved it, `python3 -B "
                                     "scripts/kontroll-land.py --sync --worktree %s --repo %s` brings the worktree "
                                     "forward instead)" % (main_id, shlex.quote(worktree), shlex.quote(gitdir)),
                                     worktree)
            if not _after_the_cut(worktree, main_id, commit_id):
                return 1, SHALLOW % (worktree, "whether the canonical's main (%s) is under HEAD cannot be told: the "
                                     "clone stops before the history that would tell (if a promote moved it, `python3 "
                                     "-B scripts/kontroll-land.py --sync --worktree %s --repo %s` brings the worktree "
                                     "forward instead, or says what stops it)"
                                     % (main_id, shlex.quote(worktree), shlex.quote(gitdir)), worktree)
        elif behind != 0:
            return 1, NOT_A_FAST_FORWARD % (gitdir, worktree, shlex.quote(worktree), shlex.quote(gitdir))
    view = None
    if shallow:
        view, why = _pack_view(worktree, gitdir, commit_id, main_id)
        if view is None:
            return 1, SHALLOW % (worktree, why, worktree)
    try:
        return _judge_and_land(worktree, gitdir, target, commit_id, main_id, wt_gitdir, skip_gate, check, gate, view)
    finally:
        if view:
            os.unlink(view["GIT_SHALLOW_FILE"])


def _judge_and_land(worktree, gitdir, target, commit_id, main_id, wt_gitdir, skip_gate, check, gate, view):
    """`land_commit` from the gate on: the gate, `check`'s answer, then the pack (in `view`, a shallow worktree's,
    so the pack carries what the check walked) and `_land_in_canonical`."""
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
        packer = _pack(worktree, commit_id, main_id, view)
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



# ---- the sync: the worktree brought forward to what a promote put on `main` (T25) ------------------------------ #
# A promote moves the canonical's `main` to a commit the operator's worktree has never seen, and from then on every
# landing from that worktree is refused (NOT_A_FAST_FORWARD) until the worktree holds that commit and descends from
# it. The remedy was `git fetch local && git rebase local/main`, by hand — and a fetch runs the canonical's serving
# side (`upload-pack`, under the canonical's config, as the fetcher), which the canonical's doors removed everywhere
# else. The sync is the landing turned round: the canonical's objects come out as a pack on a pipe (its git in the
# seam), go into the worktree's store, and the worktree's own commits are replayed onto `main`.

_SYNC_OFFERED = 1000          # the newest commits of HEAD the canonical is asked about, so its pack leaves them out
_SYNC_MAX_PACK = 1 << 30      # the most a sync reads from the canonical: far above the gate's MAX_TREE_BYTES, which
                              # a promote's tree passed; a bound on what a writer of `main` the gate never saw sends
# What git leaves in the worktree's git directory while an operation is in progress: a rebase, a merge, a cherry-pick
# or a revert (of one commit, or of a sequence: `sequencer/`), a bisect (HEAD detached on an older commit). The
# deploy's play checks the same names before its state commit (local-canonical.yml's `sync_busy`).
_SYNC_BUSY = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
              "sequencer")
# The mark a sync writes in the worktree's own git directory before its replay (a sync killed in the replay, or whose
# undo did not put the worktree back, leaves it): HEAD before it, a line per sync. A deploy stops on it before it runs
# any script of the worktree (ansible/playbooks/tasks/stop-after-sync.yml): it says what came in since the first line,
# removes the mark (a real run, and none beside a rebase in progress) and stops — so a sync by hand, and a run killed
# between its sync and its stop, are stopped on too. Only the run that syncs, and a sync by hand (which prints what came
# in itself), are the old tree's code: a later run's stop is the tree that came in, and holds only while that tree keeps
# it (SECURITY.md C10).
SYNC_MARK = "KONTROLL_SYNCED"
# How to check a tracked file git's status does not compare (skip-worktree, assume-unchanged) after a replay that
# stopped: compare it with main's, and where it holds main's bytes put HEAD's back — `--ignore-skip-worktree-bits`,
# since a plain checkout or restore refuses a skip-worktree path and `reset --hard` leaves it (read A-f7, S1; read B-f7,
# N4). Main's file is read as git's checkout writes it (`cat-file --filters`: the operator's own eol attribute or smudge
# filter converts it), and a missing one is put back too: a file main deleted is gone after the replay (read A-f8, N2
# and N3). A link is compared by its text, read without the newline `readlink` ends it with, in one command (`cat-file
# -p … | cmp - <(readlink -nv -- …)`; `-v`, so that a missing path says so and is not read as another file, read A-r21,
# N2): `cmp` reads the file a link points to, so a flagged link main retargeted compared
# as "not main's" and was kept (read A-f9, N4), and with that newline every link compared so (read A-f10, N4). Said by
# the undo's sentence and the sync's refusal of a rebase in progress; `{wt}` is quoted.
_PUT_HEADS_BACK = ("Compare each with main's (`git -C {wt} cat-file --filters refs/remotes/local/main:<path> | cmp - "
                   "{wt}/<path>`; for a link, whose target `cmp` would read, its text: `git -C {wt} cat-file -p "
                   "refs/remotes/local/main:<path> | cmp - <(readlink -nv -- {wt}/<path>)`); where one holds main's "
                   "bytes or is missing, put HEAD's back (`git -C {wt} checkout --ignore-skip-worktree-bits HEAD -- "
                   "<path>`)")
# The listing of the tracked files git's status does not compare: every `ls-files -v` tag but `H `, each path as git
# quotes it with `core.quotePath` true, its default, which the listing sets since the operator's own config may say
# false — a name of printable ASCII but `"` and `\` as it is, any other in double quotes, C-escaped — and how to give
# such a name back to the shell (`_QUOTED_NAME`: inside `$'…'` a bare `'` ends the string, so each is written `\'`).
# Printed as it is (`core.quotePath=false`), a name that is not UTF-8 made grep call the listing binary and drop it, and
# a TAB, a newline, `"` or `\` stayed C-quoted (read A-f9, N2); the operator's own `core.quotePath = false` dropped it
# again, and a `'` in a quoted name ended the `$'…'` (read A-f10, N2 and N3; read B-f10, N2). `{wt}` is quoted.
_LIST_FLAGGED = "git -C {wt} -c core.quotePath=true ls-files -v | grep -v '^H '"
# Said beside the listing wherever it is printed: a C-quoted name given back as `$'…'` and the link compare's
# `<(…)` are bash's, and dash, Debian's `sh`, reads both otherwise, loudly (read A-f11, N4).
_BASHS = " (this and the commands after it are bash's: `$'…'` and `<(…)`)"
_QUOTED_NAME = ("a name the listing prints in double quotes is quoted the C way: give it to the shell as `$'…'`, with "
                "what is between the quotes, each `'` in it written `\\'`")
# What a rebase in progress may be: a sync's replay cut short (a run killed in it). `--continue` then lands on `main`
# without the commit the replay was on (read A-f5, N1); the abort puts HEAD back, unless an untracked file the cut
# replay wrote is in its way or the lock a git killed part-way leaves is there (read B-f6, N2), and it leaves untracked
# any file of main's git had written, which the next deploy would commit as instance state (read A-f6, N1). Said by
# the sync's refusal and by the deploy's (local-canonical.yml's busy refusal); `{wt}` and `{lock}` are quoted. The
# status is case for case: with `core.ignoreCase` true, git's status hides a file main brought whose name differs from
# a tracked one's only in case (read A-f8, S1).
_CUT_SHORT = (". If it is a sync's replay cut short, `git -C {wt} rebase --abort` puts HEAD back where it was before "
              "the replay (if git says {lock} exists and no git is running, remove that file first; move aside any "
              "untracked file the abort names); then `git -C {wt} -c core.ignoreCase=false status "
              "--untracked-files=all --ignored` names, among your own, any file of main's the replay left: remove "
              "those before running again. `" + _LIST_FLAGGED + "`" + _BASHS + " names any tracked file git's status "
              "does not "
              "compare (skip-worktree, assume-unchanged; under `core.ignoreStat`, every file; " + _QUOTED_NAME
              + "): the replay may have "
              "left main's bytes in one, and that status does not see them. " + _PUT_HEADS_BACK
              + ". `git rebase --continue` can drop the commit it was replaying")
_CAME_IN_COMMITS = 20         # the newest commits `came_in` names one by one; it counts the older ones
_SUMMARY = re.compile(r" (mode change|(create|delete) mode 1[26]0000) ")   # a mode change, a symlink, a gitlink
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")   # what a terminal acts on, C1 too (TAB kept)


def _held_in_canonical(gitdir, oids):
    """Which of `oids` — commits of the worktree's history — the canonical holds as commits: `git cat-file
    --batch-check` in the seam, fed the ids. In the order given; [] when git cannot say (the pack is then every
    object `main` reaches: larger, and as correct). Reads only."""
    if not oids:
        return []
    try:
        p = subprocess.run(["git", "cat-file", "--batch-check"], input=("\n".join(oids) + "\n").encode("ascii"),
                           capture_output=True, timeout=60, **canon.git_kwargs(gitdir, fed=True))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        return []
    if p.returncode != 0:
        return []
    held = set()
    for line in p.stdout.split(b"\n"):
        fields = line.split(b" ")
        if len(fields) == 3 and fields[1] == b"commit":
            held.add(fields[0].decode("ascii", "replace"))
    return [o for o in oids if o in held]


def _pack_in_canonical(gitdir, want, held):
    """Start `git pack-objects --revs --stdout` in the canonical, in the seam: a pack of every object `want` (the
    canonical's `main`) reaches and no commit of `held` reaches — every object `want` reaches when `held` is empty —
    on its standard output, for the worktree's `index-pack`. Its revisions are written to its standard input, which
    is then closed. No `--thin` (see `_pack`): `held` is the worktree's own history, which it holds whole, so the
    pack holds what it lacks. Raises canon.Unreadable (the seam refused the canonical) or OSError (git did not
    start, or did not take its revisions)."""
    revs = (want + "\n" + "".join("^%s\n" % h for h in held)).encode("ascii")
    p = subprocess.Popen(["git", "pack-objects", "--revs", "--stdout", "-q"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **canon.git_kwargs(gitdir, fed=True))
    try:
        p.stdin.write(revs)
        p.stdin.close()
    except OSError:
        p.kill()
        p.stdout.close()
        p.wait()
        raise
    return p


def _receive_in_worktree(worktree, packer):
    """The worktree's side: `git index-pack --strict --stdin` reads the pack `packer` writes into the worktree's
    object store, refusing a pack whose checksum or whose objects are not what their names say, one with an object
    that names an object neither the pack nor the worktree holds, and one larger than `_SYNC_MAX_PACK`. Plain git,
    as the caller, in the caller's own repository (`_wt`). Returns None, or the sentence of why nothing usable came
    across. A packer killed by SIGPIPE stopped because `index-pack` stopped reading first: that refusal is the
    worktree's, not the canonical's."""
    try:
        got = _wt(worktree, ["index-pack", "--strict", "--max-input-size=%d" % _SYNC_MAX_PACK, "--stdin"],
                  pipe=packer.stdout, timeout=_LAND_SECONDS)
    except (OSError, subprocess.SubprocessError):
        packer.kill()
        got = None
    finally:
        packer.stdout.close()
        packer.wait()
    if got is None:
        return "git could not read the canonical's pack into the worktree (it did not start, or did not finish)"
    cut_off = got.returncode != 0 and packer.returncode == -getattr(signal, "SIGPIPE", 13)
    if packer.returncode != 0 and not cut_off:
        return "the canonical could not pack its main"
    if got.returncode != 0:
        return ("the worktree refused the canonical's pack (an object in it is not what its name says, the pack is "
                "cut short, or it is larger than %d bytes)" % _SYNC_MAX_PACK)
    return None


def _tmp_packs(pack_dir):
    """The names of the temporary packs an `index-pack` is writing, or left, in `pack_dir`."""
    try:
        return {n for n in os.listdir(pack_dir) if n.startswith("tmp_pack_")}
    except OSError:
        return set()


def _bring_main(worktree, gitdir, main_id, head_id, common_dir=None):
    """`main_id` and everything it names that the worktree lacks, out of the canonical and into the worktree's
    store as one pack on a pipe: the worktree offers its newest commits, the canonical says which of them it holds,
    and packs `main_id` without what those reach. Returns None when `main_id` is whole in the worktree afterwards,
    or the sentence of why not. No ref of the worktree moves here; a pack read and then not used names nothing,
    and git's own housekeeping removes it. A pack `index-pack` refused leaves its temporary file (up to
    `_SYNC_MAX_PACK`) in `<common_dir>/objects/pack`, until git's housekeeping: the ones that were not there
    before this call are removed (read A-f1, N3) — a git of the operator's writing a pack into the same store at
    that moment would lose its own, and fail."""
    try:
        listed = _wt(worktree, ["rev-list", "--max-count=%d" % _SYNC_OFFERED, head_id])
    except (OSError, subprocess.SubprocessError):
        return "git could not be run in the worktree %s" % worktree
    offered = [o for o in listed.stdout.decode("ascii", "replace").split() if canon.valid_oid(o)]
    held = _held_in_canonical(gitdir, offered if listed.returncode == 0 else [])
    pack_dir = os.path.join(common_dir, "objects", "pack") if common_dir else ""
    left_before = _tmp_packs(pack_dir) if common_dir else set()
    try:
        packer = _pack_in_canonical(gitdir, main_id, held)
    except canon.Unreadable as e:
        return "the canonical could not be read (%s)" % e
    except OSError:
        return "git could not be run on the canonical"
    why = _receive_in_worktree(worktree, packer)
    if why:
        for name in _tmp_packs(pack_dir) - left_before if common_dir else ():
            try:
                os.unlink(os.path.join(pack_dir, name))
            except OSError:
                pass
        return why
    try:
        whole = _wt(worktree, ["rev-list", "--objects", "--quiet", main_id, "--not", head_id],
                    timeout=_LAND_SECONDS).returncode
    except (OSError, subprocess.SubprocessError):
        whole = -1
    if whole != 0:
        return ("the canonical's main (%s) is not whole in the worktree after its pack was read (an object it names "
                "is missing)" % main_id)
    return None


def _not_replayable(worktree, main_id, head_id):
    """Why HEAD's commits cannot be replayed onto `main_id` — both held in the worktree — or None: the two share no
    history (or git could not say they do); a commit of HEAD's since `main_id` is a merge, which a replay would
    flatten into a line; or one is already on another remote (a remote-tracking ref other than `local`'s holds it:
    a `git pull` brought it in, or it was pushed), which a replay would give a new id, so that the branch and its
    upstream part. Each is refused, not rewritten: the human brings such a history forward with a merge."""
    try:
        base = _wt(worktree, ["merge-base", main_id, head_id]).returncode
        merges = _wt(worktree, ["rev-list", "--merges", "--count", head_id, "^" + main_id])
        own = _wt(worktree, ["rev-list", "--count", head_id, "^" + main_id])
        unpublished = _wt(worktree, ["rev-list", "--count", head_id, "^" + main_id, "--not", "--exclude=local/*",
                                     "--remotes"])
    except (OSError, subprocess.SubprocessError):
        return "git could not be run in the worktree %s; nothing was changed" % worktree
    if base != 0 and _shallow(worktree):         # HEAD may descend from main below where the clone stops (read A, S1)
        return SHALLOW_SYNC % (worktree, main_id, shlex.quote(worktree))
    if base != 0:
        return ("the canonical's main (%s) and HEAD (%s) share no history (or git could not tell that they do): the "
                "worktree is not a checkout of this canonical, and nothing is replayed; HEAD was not changed"
                % (main_id, head_id))
    if merges.returncode != 0 or merges.stdout.strip() != b"0":
        return ("HEAD's commits since the canonical's main (%s) include a merge, which a replay would flatten; "
                "nothing was replayed and HEAD was not changed. Bring the worktree forward with a merge instead — "
                "`git -C %s merge %s` — then land again" % (main_id, shlex.quote(worktree), main_id))
    counts = [p.stdout.strip() for p in (own, unpublished) if p.returncode == 0 and p.stdout.strip().isdigit()]
    if len(counts) != 2 or counts[0] != counts[1]:
        return ("HEAD's commits since the canonical's main (%s) include %s already on another remote (`git -C %s "
                "branch -r --contains <commit>` names it): a replay would give them new ids, and the branch and its "
                "upstream would part, so nothing was replayed and HEAD was not changed. Bring the worktree forward "
                "with a merge instead — `git -C %s merge %s` — then land again"
                % (main_id, int(counts[0]) - int(counts[1]) if len(counts) == 2 else "some (or git could not tell)",
                   shlex.quote(worktree), shlex.quote(worktree), main_id))
    return None


def sync_worktree(worktree, canonical, check=False):
    """Bring the worktree forward to the canonical's `main` — where a promote moved it — so that the worktree's
    next landing is a fast-forward again. Returns `(rc, message)`:

      0  in step, nothing in progress: the canonical has no `main` yet (the landing seeds it), or `main` is HEAD or an
         ancestor of HEAD (the landing fast-forwards it); or "brought forward": `main`'s commits are in the worktree,
         HEAD descends from `main`, and the worktree's own commits since are replayed on top of it;
      1  not brought forward — HEAD, the branch and the tracked files are as they were (of the files of `main` git wrote
         before it stopped, each regular file reached through no link and holding main's bytes is removed; the sentence
         names those and any it left), unless the sentence says the worktree could not be put back, that git could not
         say whether the tracked files (or some its status does not compare) are HEAD's, or that HEAD does not descend
         from `main` (it then names HEAD): no commit at HEAD; a worktree that is not the top of its repository (one
         below it, or whose top git cannot say); the canonical unreadable, or its `main` not a branch; a rebase, merge,
         cherry-pick, revert or bisect in progress; tracked files with changes not committed; a merge among HEAD's
         commits since `main`, or one already on another remote; no common history; a caller that does not own the
         worktree; a pack refused, short or too large; an ignored file in the way of main's checkout; a mark that could
         not be written; a conflict (the files are named and the replay undone);
      3  (`check` only) would bring forward. `check` changes nothing — no object comes across and no ref moves. It
         does not judge changes in the working tree, which a deploy commits before it brings the worktree forward;
         of what a real run refuses it says what it can see (a rebase in progress; a merge, or no common history,
         when the worktree holds `main` already).

    THE RULE. M = the canonical's `refs/heads/main`, read once by its full name in the seam (`canon.read_ref`). When the
    worktree lacks M, `_bring_main` brings it across as one pack on a pipe, made in the seam. Then
    `refs/remotes/local/main` names M (as a `fetch local` would have left it) and HEAD's commits since M are replayed
    onto it: `git rebase M`, committed as `kontroll` (the committer's environment too), unsigned, with autostash,
    autosquash, update-refs and rebase-merges off, no editor and no git-lfs download. A replay that stops is undone
    (`rebase --abort`), HEAD is checked back where it was, and the tracked files are checked to be HEAD's (put back with
    `reset --hard` when git stopped part-way through its checkout of `main`); a file git wrote from `main` at a path
    HEAD does not track is removed when it holds main's bytes, and named when it does not. A tracked file the status
    does not compare (flagged skip-worktree or assume-unchanged) is not checked: the undo's sentence counts them and
    says how to name them (read A-f6, S1). No hook and no fsmonitor program runs in any git of the sync, the caller's
    own included (`_wt`). A content filter or merge driver the caller's own config names does run, in the replay and in
    its undo (`rebase --abort`, `status`, `reset --hard`), on what the incoming `.gitattributes` routes to it — as in
    the caller's own checkout of that tree; one that names a program in the tree runs the version that came in, and so
    does a config file the caller's config includes from the tree (`include.path`, `includeIf`), which can define them
    (SECURITY.md, the arming row). A replay gives each replayed commit a new id. An ignored file in the way of main's
    checkout — at a path `main` tracks, under one, or where `main` has a directory — is refused before the replay: the
    checkout would overwrite it, or remove it with its directory, without a word, and no commit holds it (read A-f3,
    S1); git itself refuses an untracked file that is not ignored.

    THE MARK. A sync appends HEAD to `SYNC_MARK` in the worktree's own git directory before its replay, so that a sync
    killed in the replay leaves it too, and refuses with HEAD as it was when it cannot (read A-f4, S1); a replay put
    back — HEAD where it was, nothing in progress, the tracked files HEAD's (none of them flagged so that the status
    does not compare it) and no untracked file that was not there before it — removes a mark that sync wrote, and so
    does a write of the mark that failed (a mark there before stays; read A-f5, S1 and N2). The deploy's own sync
    (`kontroll-land.py --sync --stop-as`) is that run's last step when it brings the worktree forward: it says what came
    in (`came_in`), removes the mark (`clear_mark`) and fails the play's task, so that no Ansible worker starts after
    the tree changed (a worker can import the tree's filter plugins when it first templates: read A-f3, B1). The next
    deploy stops on a mark that is left — a sync by hand's, or a deploy's killed before it removed it — before it lands,
    deploys or runs the worktree's scripts, says what came in since the mark's first line, and removes it
    (ansible/playbooks/tasks/stop-after-sync.yml). That stop is the tree's own: what came in can drop it, so the run
    that syncs (whose play was read before the sync) and a sync by hand (which prints `came_in` itself) are the stops
    that hold (SECURITY.md C10).

    AS WHOM. The worktree's git runs as the caller, in the caller's own repository, and a caller that does not own
    the worktree's git directory is refused before anything is written there: root's objects and ref files in the
    operator's repository are what the operator's own git cannot write over afterwards. The canonical's git runs in
    the seam — none of its hooks or config-named programs, and the service uid when root calls (a deploy calls as
    the operator, so there the canonical is read as the operator). No transport: nothing runs the canonical's
    serving side."""
    worktree = os.path.abspath(worktree)
    gitdir = canon.git_dir(os.path.abspath(canonical))
    try:
        head = _wt(worktree, ["rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD^{commit}"])
        where = _wt(worktree, ["rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir"])
    except (OSError, subprocess.SubprocessError):
        return 1, "git could not be run in the worktree %s; nothing was changed" % worktree
    head_id = head.stdout.decode("ascii", "replace").strip()
    dirs = where.stdout.decode("utf-8", "surrogateescape").splitlines()
    if head.returncode != 0 or not canon.valid_oid(head_id) or where.returncode != 0 or len(dirs) != 2:
        return 1, ("the worktree %s has no commit at HEAD (is it a repository, with a commit?); nothing was changed"
                   % worktree)
    common_dir, own_dir = dirs
    # Only the top: git names the ignored files relative to the directory it runs in, and main's from the top, so in
    # a subdirectory an ignored file in main's way is never matched and the checkout overwrites it (read A-f4, S2).
    try:
        top = _wt(worktree, ["rev-parse", "--show-toplevel"]).stdout.decode("utf-8", "surrogateescape").rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        top = ""
    if not top or os.path.realpath(top) != os.path.realpath(worktree):
        return 1, ("%s is not the top of its repository (%s): name the top with --worktree; nothing was changed"
                   % (worktree, top or "git could not say where that is"))
    # Before any "in step": a replay stopped part-way has HEAD on `main`, and HEAD's own commits are not on it yet.
    busy = [n for n in _SYNC_BUSY if os.path.lexists(os.path.join(own_dir, n))]
    if busy:
        return 1, ("a rebase, merge, cherry-pick, revert or bisect is in progress in the worktree (%s holds %s): "
                   "finish it or abort it (`git bisect reset` ends a bisect), then run again; nothing was changed%s"
                   % (own_dir, busy[0], _CUT_SHORT.format(wt=shlex.quote(worktree),
                                                          lock=shlex.quote(os.path.join(own_dir, "index.lock")))
                      if busy[0].startswith("rebase-") else ""))
    try:
        main_ref = canon.read_ref(gitdir, canon.MAIN)
    except canon.Unreadable as e:
        return 1, "the canonical could not be read (%s); nothing was changed" % e
    if main_ref is None:
        return 0, "in step — the canonical has no main yet (the landing seeds it with HEAD)"
    if main_ref[2] or main_ref[1] != "commit":
        return 1, ("refs/heads/main of the canonical is a symbolic ref, or names no commit: there is nothing to "
                   "bring the worktree forward to; nothing was changed")
    main_id = main_ref[0]
    if main_id == head_id:
        return 0, "in step — HEAD is the canonical's main (%s)" % main_id
    try:
        held = _wt(worktree, ["cat-file", "-e", main_id + "^{commit}"]).returncode == 0
        ahead = held and _wt(worktree, ["merge-base", "--is-ancestor", main_id, head_id]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return 1, "git could not be run in the worktree %s; nothing was changed" % worktree
    if ahead:
        return 0, ("in step — the canonical's main (%s) is an ancestor of HEAD (%s): the landing fast-forwards it"
                   % (main_id, head_id))
    refused = _not_replayable(worktree, main_id, head_id) if held else None
    if refused:
        return 1, refused
    if check:
        return 3, ("would bring forward — the canonical's main (%s) has commit(s) the worktree %s does not; a real "
                   "run brings them in and replays HEAD's own commits on top.%s Nothing was changed."
                   % (main_id, worktree, _SHALLOW_CHECK if _shallow(worktree) else ""))
    if hasattr(os, "geteuid"):
        try:
            owner = os.stat(common_dir).st_uid
        except OSError:
            owner = None
        if owner != os.geteuid():
            return 1, ("this process runs as uid %d and the worktree's git directory (%s) is %s: what a sync writes "
                       "there would be this uid's, and the owner's git could not write over it afterwards. Run it "
                       "as the owner (a deploy does); nothing was changed"
                       % (os.geteuid(), common_dir, "uid %d's" % owner if owner is not None else "not one it can read"))
    try:
        dirty = _wt(worktree, ["status", "--porcelain", "--untracked-files=no"])
    except (OSError, subprocess.SubprocessError):
        return 1, "git could not be run in the worktree %s; nothing was changed" % worktree
    if dirty.returncode != 0 or dirty.stdout.strip():
        return 1, ("tracked files in the worktree %s have changes that are not committed: commit them (a deploy "
                   "commits the instance state before it syncs) or put them aside, then run again; nothing was "
                   "changed" % worktree)
    if not held:
        why = _bring_main(worktree, gitdir, main_id, head_id, common_dir)
        if why:
            return 1, why + "; HEAD was not changed"
    try:
        named = _wt(worktree, ["update-ref", "-m", "kontroll sync: the canonical's main", "refs/remotes/local/main",
                               main_id]).returncode
    except (OSError, subprocess.SubprocessError):
        named = -1
    if named != 0:
        return 1, "git could not set refs/remotes/local/main in the worktree %s; HEAD was not changed" % worktree
    refused = _not_replayable(worktree, main_id, head_id)
    if refused:
        return 1, refused
    # What is untracked before the replay: what is untracked after an undo, and was not, git wrote from `main`.
    try:
        untracked = _untracked(worktree)
    except (OSError, subprocess.SubprocessError):
        untracked = None
    if untracked is None:
        return 1, "git could not list the untracked files of the worktree %s; HEAD was not changed" % worktree
    try:
        clash = _in_mains_way(worktree, main_id)
    except (OSError, subprocess.SubprocessError):
        clash = None
    if clash is None:
        return 1, ("git could not list the ignored files of the worktree %s, or the files of the canonical's main "
                   "(%s); HEAD was not changed" % (worktree, main_id))
    if clash:
        return 1, ("the canonical's main (%s) tracks a path where the worktree %s holds an ignored file, which no "
                   "commit holds: %s. The replay would overwrite it, or remove it with its directory, without a word: "
                   "move it aside, then run again; HEAD was not changed" % (main_id, worktree, _names(clash)))
    # The mark goes down BEFORE the replay: a run killed in it leaves the mark (and a rebase in progress), so the next
    # deploy stops on it and says what came in before its dry-run runs any script of the tree (read A-f4, S1). Never
    # through a link — git's `--git-path` would name its target — and never waiting on a FIFO: this writes the mark
    # itself, as a regular file, or refuses and says so.
    mark = os.path.join(own_dir, SYNC_MARK)
    had_mark = os.path.lexists(mark)
    try:
        fd = os.open(mark, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), 0o644)
        with os.fdopen(fd, "a", encoding="ascii") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OSError(errno.EINVAL, "not a regular file")
            fh.write(head_id + "\n")
    except OSError as e:
        if not had_mark:
            try:
                if stat.S_ISREG(os.lstat(mark).st_mode):
                    os.unlink(mark)
            except OSError:
                pass
        return 1, ("the mark that stops the next deploy first (%s) could not be written (%s); HEAD was not changed"
                   % (mark, e.strerror or e))
    # The caller's GIT_COMMITTER_* beat `-c user.*`, so they are set too; a signing key would ask for a passphrase
    # (or fail: the deploy's git has no HOME), and git-lfs's smudge would download, so neither runs. No hook and no
    # fsmonitor program runs either: a `core.hooksPath` naming a tracked directory would run the hooks a promote put
    # there (read A-f1, N2). A content filter or merge driver the caller's own config names still runs, here and in
    # the undo, on what the incoming `.gitattributes` routes to it, as in the caller's own checkout (SECURITY.md, the
    # arming row) — and a config file the caller's config includes from the tree is the one that came in.
    env = dict(os.environ, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true", GIT_TERMINAL_PROMPT="0",
               GIT_COMMITTER_NAME="kontroll", GIT_COMMITTER_EMAIL="kontroll@localhost", GIT_LFS_SKIP_SMUDGE="1")
    try:
        replay = subprocess.run(["git", "-C", worktree, "-c", "user.name=kontroll", "-c",
                                 "user.email=kontroll@localhost", "-c", "commit.gpgSign=false",
                                 "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                 "-c", "rebase.autoStash=false",
                                 "-c", "rebase.autoSquash=false", "-c", "rebase.updateRefs=false",
                                 "-c", "rebase.rebaseMerges=false", "rebase", "--quiet", main_id],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=_LAND_SECONDS, env=env)
    except subprocess.SubprocessError:
        replay, ran = None, True
    except OSError:
        replay, ran = None, False
    else:
        ran = True
    if replay is None or replay.returncode != 0:
        said = (_undo_replay(worktree, own_dir, main_id, head_id, replay, untracked) if ran
                else "git could not be run in the worktree %s; HEAD was not changed" % worktree)
        try:
            back = _wt(worktree, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]).stdout.decode("ascii",
                                                                                                     "replace").strip()
        except (OSError, subprocess.SubprocessError):
            back = ""
        # Put back — HEAD where it was, nothing in progress, the tracked files HEAD's (and none the status does not
        # compare: `_flagged`) and no untracked file that was not there before the replay — nothing came in: the mark
        # this sync wrote goes (one already there stays). Otherwise (the undo's sentence says so) it stays, and the
        # next deploy stops on it.
        put_back = not ran
        if ran and back == head_id:
            try:
                st = _wt(worktree, ["status", "--porcelain", "--untracked-files=no"])
                now_untracked = _untracked(worktree)
                put_back = (st.returncode == 0 and not st.stdout.strip() and now_untracked is not None
                            and now_untracked <= untracked and _flagged(worktree) == 0)
            except (OSError, subprocess.SubprocessError):
                put_back = False
        if (not had_mark and put_back
                and not any(os.path.lexists(os.path.join(own_dir, n)) for n in _SYNC_BUSY)):
            try:
                if stat.S_ISREG(os.lstat(mark).st_mode):
                    os.unlink(mark)
            except OSError:
                pass
        return 1, said
    try:
        now = _wt(worktree, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]).stdout.decode("ascii",
                                                                                                "replace").strip()
        on_main = _wt(worktree, ["merge-base", "--is-ancestor", main_id, now]).returncode == 0
        mine = _wt(worktree, ["rev-list", "--count", now, "^" + main_id]).stdout.decode("ascii", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        now, on_main, mine = "", False, ""
    if not on_main:
        return 1, ("the replay onto the canonical's main (%s) ended, but HEAD (%s) does not descend from it: look at "
                   "`git -C %s log --oneline -5` before landing"
                   % (main_id, now or "unreadable", shlex.quote(worktree)))
    return 0, ("brought forward — HEAD is %s: %s commit(s) of the worktree's own on the canonical's main %s (HEAD "
               "was %s)" % (now, mine or "?", main_id, head_id))


def _mark_path(worktree):
    """Where a sync leaves its mark: `SYNC_MARK` in the worktree's own git directory, named as that directory and
    the mark's name (never `--git-path`, which would name a link's target) — or None when git cannot name it."""
    try:
        where = _wt(worktree, ["rev-parse", "--path-format=absolute", "--git-dir"])
    except (OSError, subprocess.SubprocessError):
        return None
    if where.returncode != 0:
        return None
    return os.path.join(where.stdout.decode("utf-8", "surrogateescape").rstrip("\n"), SYNC_MARK)


def head_id(worktree):
    """HEAD's commit id in the worktree, or None (no commit, or git could not say). Plain git; reads only."""
    try:
        p = _wt(worktree, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    except (OSError, subprocess.SubprocessError):
        return None
    oid = p.stdout.decode("ascii", "replace").strip()
    return oid if p.returncode == 0 and canon.valid_oid(oid) else None


def clear_mark(worktree):
    """Remove the mark a sync left — what the run that stops on it does last, so that the next run goes on. Only a
    regular file is removed, never through a link: a mark that is not one is not a file a sync wrote, and is left
    for the operator to look at. Returns None when there is no mark now, or the reason it is still there."""
    mark = _mark_path(worktree)
    if mark is None:
        return "git could not name the worktree's git directory"
    try:
        if not stat.S_ISREG(os.lstat(mark).st_mode):
            return "%s is not a file a sync writes (a link, or not a regular file)" % mark
        os.unlink(mark)
    except FileNotFoundError:
        return None
    except OSError as e:
        return "%s: %s" % (mark, e.strerror or e)
    return None


def synced_since(worktree):
    """HEAD before the first sync since a deploy last stopped on the mark — `SYNC_MARK`'s first line, in the
    worktree's own git directory — or None: no mark, one that is a link (never followed: `--git-path` would name
    its target, so the mark is named as the git directory and its name), or one whose first line is not a commit
    id."""
    mark = _mark_path(worktree)
    if mark is None:
        return None
    try:
        fd = os.open(mark, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="ascii", errors="replace") as fh:
            first = fh.readline().strip()
    except (OSError, ValueError):
        return None
    return first if canon.valid_oid(first) else None


def came_in(worktree, before):
    """What came in since `before` (HEAD before a sync), as lines to read before any of it runs: the commits the
    canonical's main gained (`before..refs/remotes/local/main` — the newest `_CAME_IN_COMMITS` named, and a count of
    the older ones), every path HEAD's tree changed since `before` (`--name-status` with rename detection off, so a
    path is named where it now is), and each mode change, symlink and gitlink (`--summary`), which a path's letter
    does not show. A writer of `main` chooses the subjects, the names and the paths, so nothing is cut short without
    a count, and a character a terminal acts on is shown as `?` (read A-f1, S2); colour is off whatever the
    operator's config says, so no escape of git's own is among them. The worktree's own commits,
    replayed on top, are not among the commits. Plain git in the caller's repository; reads only."""
    since = before + "..refs/remotes/local/main"
    try:
        count = _wt(worktree, ["rev-list", "--count", since])
        log = _wt(worktree, ["log", "--no-color", "--no-decorate", "--no-show-signature", "--format=%h %an: %s",
                             "-n", str(_CAME_IN_COMMITS), since])
        changed = _wt(worktree, ["diff", "--no-color", "--no-renames", "--no-ext-diff", "--name-status", before,
                                 "HEAD"])
        summary = _wt(worktree, ["diff", "--no-color", "--no-renames", "--no-ext-diff", "--summary", before, "HEAD"])
    except (OSError, subprocess.SubprocessError):
        return ["git could not list it"]
    if any(r.returncode != 0 for r in (count, log, changed, summary)):
        return ["git could not list it"]
    lines = log.stdout.decode("utf-8", "replace").splitlines()
    older = count.stdout.decode("ascii", "replace").strip()
    if older.isdigit() and int(older) > _CAME_IN_COMMITS:
        lines.append("and %d older commit(s) not shown" % (int(older) - _CAME_IN_COMMITS))
    lines += changed.stdout.decode("utf-8", "replace").splitlines()
    lines += [s.strip() for s in summary.stdout.decode("utf-8", "replace").splitlines() if _SUMMARY.match(s)]
    return [_CONTROL.sub("?", s) for s in lines]


def _git_reason(stderr):
    """Why git stopped, in its words: its first `error:` or `fatal:` line with the indented lines under it (the
    paths it names: five, and a count of the rest), or — when it printed neither — its first line that is not a
    `hint:` (a hint says how to go on, not why). Git's LAST line is the least of it: `error: could not detach
    HEAD` follows the line that names the file it could not write (read A-f1, N4; read B-f1, B1)."""
    lines = [s.rstrip(b"\r") for s in (stderr or b"").split(b"\n")]
    for i, line in enumerate(lines):
        if line.startswith((b"error:", b"fatal:")):
            under = []
            for s in lines[i + 1:]:
                if not s.startswith((b"\t", b" ")) or not s.strip():
                    break
                under.append(s.strip())
            more = [b"and %d more" % (len(under) - 5)] if len(under) > 5 else []
            return canon.show(b" ".join([line.strip()] + under[:5] + more))[0]
    said = [s.strip() for s in lines if s.strip() and not s.startswith(b"hint:")]
    return canon.show(said[0])[0] if said else ""


def _undo_replay(worktree, own_dir, main_id, head_id, replay, before):
    """A replay that stopped: name the files git left in conflict, undo the replay (`rebase --abort`), check that
    HEAD is back where it was, and that the tracked files are HEAD's. Git checks out `main` before the rebase
    begins, and a checkout it stops part-way (a path it cannot write: a directory of root's) has written some files
    and deleted others with no rebase to abort; the sync refused tracked changes before the replay, so HEAD's tree
    is what those files held, and `git reset --hard` HEAD puts them back (read B-f1, B1). What git wrote at a path
    HEAD does not track is untracked then: `_clear_what_main_left` removes it (`before`: the untracked files before
    the replay). Tracked files the status does not compare (`_flagged`: skip-worktree, assume-unchanged) are counted,
    not checked: the sentence says so, and how to compare each with main's and put HEAD's back (read A-f7, S1).
    Returns the sentence."""
    q = shlex.quote(worktree)   # each command the sentence prints names it so (read A-f7, N3)
    try:
        unmerged = _wt(worktree, ["diff", "--name-only", "--diff-filter=U", "-z"]).stdout
    except (OSError, subprocess.SubprocessError):
        unmerged = b""
    names = [canon.show(n)[0] for n in unmerged.split(b"\0") if n]
    undone, why = True, ""
    try:
        if any(os.path.lexists(os.path.join(own_dir, n)) for n in ("rebase-merge", "rebase-apply")):
            aborted = _wt(worktree, ["rebase", "--abort"], timeout=_LAND_SECONDS)
            undone, why = aborted.returncode == 0, _git_reason(aborted.stderr)
    except subprocess.TimeoutExpired:
        undone, why = False, "the abort did not finish in %d s" % _LAND_SECONDS
    except (OSError, subprocess.SubprocessError):
        undone = False
    # HEAD read on its own, whatever the abort did: one cut short may have put it back, or not (read B-f4, N5).
    try:
        now = _wt(worktree, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]).stdout.decode("ascii",
                                                                                                "replace").strip()
    except (OSError, subprocess.SubprocessError):
        now = ""
    if not undone or now != head_id:
        # Git's reason, and the lock a git stopped part-way leaves (a full disk, the replay's timeout), which the
        # remedy's own `rebase --abort` would fail on (read A-f3, N3).
        return ("the replay onto the canonical's main (%s) %s, and the worktree could not be put back%s: %s. If %s "
                "is there and no git is running, remove it; then run `git -C %s rebase --abort`, check that HEAD is "
                "%s (`git -C %s status`), and run again"
                % (main_id, "did not finish in %d s" % _LAND_SECONDS if replay is None else "stopped",
                   " (" + why + ")" if why else "",
                   "HEAD is %s as it was, but the replay was not undone" % head_id if now == head_id
                   else "HEAD is %s (it was %s)" % (now or "unreadable", head_id),
                   shlex.quote(os.path.join(own_dir, "index.lock")), q, head_id, q))
    try:
        left = _wt(worktree, ["status", "--porcelain", "--untracked-files=no"])
        if left.returncode == 0 and left.stdout.strip():
            _wt(worktree, ["reset", "-q", "--hard", head_id], timeout=_LAND_SECONDS)
            left = _wt(worktree, ["status", "--porcelain", "--untracked-files=no"])
    except (OSError, subprocess.SubprocessError):
        left = None
    if left is None or left.returncode != 0:
        why = _git_reason(left.stderr) if left is not None else ""
        return ("the replay onto the canonical's main (%s) stopped and was undone — HEAD is %s as it was — but git "
                "could not say whether the tracked files are HEAD's (`git status` failed%s). If `git -C %s status` "
                "fails too (a damaged index), `rm %s && git -C %s reset` rebuilds the index from HEAD and leaves the "
                "files alone; then run `git -C %s status`, keep what you need, `git -C %s reset --hard %s`, and run "
                "again" % (main_id, head_id, ": " + why if why else "", q, shlex.quote(os.path.join(own_dir, "index")),
                           q, q, q, head_id))
    if left.stdout.strip():
        return ("the replay onto the canonical's main (%s) stopped, and the worktree could not be put back: HEAD is %s "
                "as it was, but tracked files differ from it (git stopped part-way through checking out main). Run "
                "`git -C %s status`, keep what you need, `git -C %s reset --hard %s`, then run again"
                % (main_id, head_id, q, q, head_id))
    left_over = _clear_what_main_left(worktree, main_id, before)
    try:
        flagged = _flagged(worktree)
    except (OSError, subprocess.SubprocessError):
        flagged = None
    if flagged is None:
        unseen = (" Git could not list the tracked files its status does not compare (`git -C %s ls-files -v` "
                  "failed), so it cannot say those are HEAD's." % q)
    elif flagged:
        every = " (`core.ignoreStat` is set, which flags every file git writes)" if _ignore_stat(worktree) else ""
        unseen = (" Git's status does not compare %d tracked file(s) flagged skip-worktree or assume-unchanged%s, so "
                  "it cannot say those are HEAD's: `%s`%s names them (%s). %s before running again."
                  % (flagged, every, _LIST_FLAGGED.format(wt=q), _BASHS, _QUOTED_NAME,
                     _PUT_HEADS_BACK.format(wt=q)))
    else:
        unseen = ""
    if names:
        more = " and %d more" % (len(names) - 10) if len(names) > 10 else ""
        return ("HEAD's commits do not replay onto the canonical's main (%s) without a conflict, in %s%s. The replay "
                "was undone and HEAD is as it was; refs/remotes/local/main names the canonical's main. Bring the "
                "worktree forward by hand — `git -C %s rebase local/main`, resolving those files — then run again"
                % (main_id, ", ".join(names[:10]), more, q)) + ("." + left_over + unseen
                                                               if left_over or unseen else "")
    said = _git_reason(replay.stderr) if replay is not None else ""
    return ("git could not replay HEAD's commits onto the canonical's main (%s)%s; the replay was undone and %s.%s%s"
            % (main_id, ": " + said if said else " (it did not finish)" if replay is None else "",
               "HEAD is as it was" if unseen else "HEAD and the tracked files are as they were", left_over, unseen))


def _flagged(worktree):
    """How many of the worktree's index entries git's status does not compare with their files — flagged
    skip-worktree or assume-unchanged (`core.ignoreStat` flags each entry git writes): any `ls-files -v` tag but `H ` —
    or None when git could not list them. A checkout of `main` stopped part-way may leave main's bytes in such a file,
    which the status does not compare (and `reset --hard` leaves alone when it is skip-worktree), so the undo cannot
    say that file is HEAD's, and the mark stays (read A-f6, S1; read B-f7, N5)."""
    p = _wt(worktree, ["ls-files", "-v", "-z"])
    return sum(1 for e in p.stdout.split(b"\0") if e and e[:2] != b"H ") if p.returncode == 0 else None


def _ignore_stat(worktree):
    """Whether the worktree's git reads `core.ignoreStat` as true: it then flags every index entry it writes, so the
    undo's count of flagged files is every file, not ones the operator flagged (read A-f8, N3). False when git could
    not say."""
    try:
        p = _wt(worktree, ["config", "--type=bool", "--get", "core.ignoreStat"])
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and p.stdout.strip() == b"true"


def _untracked(worktree):
    """Every untracked file of the worktree, ignored ones too (`ls-files --others`), as a set of names (bytes, as git
    gives them) — or None when git could not list them. Case for case, whatever the operator's `core.ignoreCase`
    says: with it true, git does not list a file whose name differs from a tracked one's only in case, so one main
    brings beside a tracked file went unseen, and the undo judged the worktree put back (read A-f7, S2)."""
    p = _wt(worktree, ["-c", "core.ignoreCase=false", "ls-files", "-z", "--others"])
    return {n for n in p.stdout.split(b"\0") if n} if p.returncode == 0 else None


def _in_mains_way(worktree, main_id):
    """The worktree's ignored files that the checkout of `main_id` would overwrite or remove: each at a path `main`
    tracks, under a path it tracks as a file, or where it has a directory (as git gives the names, sorted) — or None
    when git could not list the ignored files, or main's. Git refuses an untracked file in a checkout's way that is
    not ignored, and overwrites or removes an ignored one without a word (read A-f3, S1)."""
    ignored = _wt(worktree, ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"])
    if ignored.returncode != 0:
        return None
    names = sorted(n.rstrip(b"/") for n in ignored.stdout.split(b"\0") if n)     # a nested repository is `dir/`
    if not names:
        return []
    listed = _wt(worktree, ["ls-tree", "-r", "-z", "--name-only", "--full-tree", main_id])
    if listed.returncode != 0:
        return None
    paths = {n for n in listed.stdout.split(b"\0") if n}
    dirs = {p[:i] for p in paths for i in range(1, len(p)) if p[i:i + 1] == b"/"}
    return [n for n in names if n in paths or n in dirs
            or any(n[:i] in paths for i in range(1, len(n)) if n[i:i + 1] == b"/")]


def _blob_id(path, algo):
    """The id git gives the bytes of the regular file at `path` as a blob, hashed here byte for byte — no filter,
    attribute or line-ending conversion, and the name taken whole (`hash-object --stdin-paths` reads a name a line
    at a time and C-unquotes one that begins with `"`: read A-f3, N4) — or None (not a regular file, a link, not
    readable, or `algo` neither sha1 nor sha256)."""
    if algo not in ("sha1", "sha256"):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.new(algo, b"blob %d\0" % info.st_size)
        for chunk in iter(lambda: os.read(fd, 1 << 20), b""):
            digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def _a_file_in_place(worktree, name):
    """True when `name` (a path git gave, relative to the worktree) is a regular file reached through no link."""
    rel = os.fsdecode(name)
    path = os.path.join(worktree, rel)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
    except OSError:
        return False
    return os.path.realpath(os.path.dirname(path)) == os.path.normpath(
        os.path.join(os.path.realpath(worktree), os.path.dirname(rel)))


def _names(names):
    """Up to ten names, each as `canon.show` shows a path, and a count of the rest."""
    more = " and %d more" % (len(names) - 10) if len(names) > 10 else ""
    return ", ".join(canon.show(n)[0] for n in names[:10]) + more


def _clear_what_main_left(worktree, main_id, before):
    """After an undone replay: a file untracked now that was not before the replay (`before`), at a path the canonical's
    `main` tracks, is one git wrote from `main` before its checkout stopped. Each that is a regular file, reached
    through no link, holding main's bytes at that path (hashed here, byte for byte: `_blob_id`) is removed: `main` holds
    it, so nothing is lost; left, it would refuse the next sync's checkout (`would be overwritten`) and, in a hooks path
    into the tree, run as a hook on the operator's next git command (read B-f2, S1 and N4). A new file at a path `main`
    does not track is not git's doing, and is kept unsaid. Returns the sentence: '' when git left none; else what was
    removed, and what is left for the operator to look at (a link, bytes that are not main's, a file that could not be
    removed)."""
    # The status is case for case, as `_CUT_SHORT`'s (read A-f8, S1), and lists the ignored files too, as `_untracked`
    # does: a file of main's at a path the operator's ignore rules name is one the replay can leave (read B-f9, N4).
    unsure = (" Git could not say which untracked files the replay left: `git -C %s -c core.ignoreCase=false status "
              "--untracked-files=all --ignored` lists them; remove those of the canonical's main before the next "
              "sync, which writes them again." % shlex.quote(worktree))
    try:
        now = _untracked(worktree)
        listed = _wt(worktree, ["ls-tree", "-r", "-z", "--full-tree", main_id]) if now is not None else None
    except (OSError, subprocess.SubprocessError):
        now = listed = None
    if now is None or listed is None or listed.returncode != 0:
        return unsure
    new = now - before
    mains = {}
    for entry in listed.stdout.split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if name in new:
            mains[name] = meta.split(b" ")
    files = [n for n in sorted(mains) if mains[n][0] in (b"100644", b"100755") and _a_file_in_place(worktree, n)]
    same = set()
    if files:
        try:
            algo = _wt(worktree, ["rev-parse", "--show-object-format"]).stdout.decode("ascii", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            algo = ""
        same = {n for n in files
                if _blob_id(os.path.join(worktree, os.fsdecode(n)), algo) == mains[n][2].decode("ascii", "replace")}
    removed, kept = [], []
    for name in sorted(mains):
        try:
            if name in same:
                os.unlink(os.path.join(worktree, os.fsdecode(name)))
                removed.append(name)
                continue
        except OSError:
            pass
        kept.append(name)
    said = ""
    if removed:
        said += (" The files of the canonical's main that git wrote before it stopped were removed (main holds them): "
                 "%s." % _names(removed))
    if kept:
        said += (" Files of the canonical's main that git wrote before it stopped are left, untracked: %s (a link, "
                 "bytes that are not main's, or a file that could not be removed). Look at them, and remove them "
                 "before the next sync, which writes them again." % _names(kept))
    return said
