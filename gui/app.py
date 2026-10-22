#!/usr/bin/env python3
"""kontroll onboarding GUI — a THIN web layer over the kontroll service layer + galaxy.py.

The whole point: Joe searches, sees what a collection can do, supplies the
connection details, and ends up with a managed device + a backup — with **no**
manual step other than (1) the search selection and (2) entering connection
details. Every other step (module declaration, drop-in inventory host, fleet
enable, credential encryption into SOPS, commit, push, collection install,
live-verify) is driven by `galaxy.py onboard` behind this UI. See gui/README.md.

**Search, classify, AND onboard all call the `kontroll` service layer in-process** (no subprocess —
the same service functions the API uses). Onboard mirrors `api/routes/onboard.py`: build the plan,
apply (module + drop-in host + fleet-enable + creds→SOPS), then `gitio.commit_and_push(run_id=…)` —
which STAGES `proposed/<run_id>` when the deploy armed `KONTROLL_STAGE_PUSHES` (C10), never `main`.

This is a PRIVILEGED surface (it can write the repo, encrypt secrets, push, and run
Ansible against the lab). It is therefore guarded: HTTP Basic **auth** (fail-closed —
won't start without a password), **TLS** (set GUI_TLS_CERT/GUI_TLS_KEY), and an
**audit log** of every action. Still: bind it to the mgmt network only, never expose
it publicly. See gui/README.md + SECURITY.md.
"""
import functools
import hmac
import logging
import os
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler

from flask import Flask, Response, jsonify, render_template, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # the kontroll service layer (search/classify in-process)
from kontroll import auditfmt  # noqa: E402  (the one audit-field flattener, shared with the API's writer)
from kontroll import authspec  # noqa: E402  (F1: derive the onboard form's per-backend credential fields)
from kontroll import catalog as _catalog  # noqa: E402  (after sys.path is set up)
from kontroll import probe  # noqa: E402  (F1 TIER-B: deep-probe an installed collection for candidate auth modules)
from kontroll import gitio  # noqa: E402  (onboard + capability promote commit the scoped paths in-process)
from kontroll import paths  # noqa: E402  (the request-boundary refusal, mapped once for the app — C22)
from kontroll.envguard import positive_int  # noqa: E402  (fail-closed audit-rotation knobs — M-5)
from kontroll.service import capability  # noqa: E402  (the secondary-capability dialog backend)
from kontroll.service import identity as identity_service  # noqa: E402  (the module-identity reconfigure backend, Phase 4b)
from kontroll.service import hostvars as hostvars_service  # noqa: E402  (the inventory host-var reconfigure backend, Phase 4b)
from kontroll.service import settings as settings_service  # noqa: E402  (the platform-settings EDIT backend, Phase 5)
from kontroll.service import _reconfig  # noqa: E402  (the shared single-object promote: token → no_drop → apply)
from kontroll.service import keygen as keygen_service  # noqa: E402  (the guided age key-generation backend)
from kontroll.service import secrets as secrets_service  # noqa: E402  (the secret-onboarding dialog backend)
from kontroll.service.classify import service_classify  # noqa: E402
from kontroll.service.onboard import (  # noqa: E402  (in-process onboard)
    apply_onboard_plan, build_onboard_plan, no_backend_message)
from kontroll.service.search import service_search  # noqa: E402

app = Flask(__name__)


@app.after_request
def _security_headers(resp):
    """Defence-in-depth headers on every response. The REAL fix for third-party bytes on the page is painting them
    with ET()/textContent (see index.html's card()) — this is the second layer, not the first. `script-src` is
    deliberately ABSENT: index.html carries one large inline <script>, so a `script-src 'self'` would break the page,
    and relocating that script is a separate change. Shipping the directives that DO hold rather than none of them."""
    resp.headers.setdefault("Content-Security-Policy",
                            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# The drop-in catalog (vectors/overrides/backends), loaded once + cached; the per-request I/O
# (deep_probe / local_installed / galaxy_search) stays in the service layer.
_CATALOG = None


def _catalog_triple():
    """(vectors, overrides, backends) loaded once from the real tree (the read-only registries)."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = (_catalog.load_vectors(), _catalog.load_overrides(), _catalog.load_backends())
    return _CATALOG

# --- Auth (fail-closed): a password MUST be set, else the privileged surface refuses
# to run. Sourced from the SOPS-backed env at deploy (GUI_PASSWORD). ----------------
GUI_USER = os.environ.get("GUI_USER", "admin")
GUI_PASSWORD = os.environ.get("GUI_PASSWORD")
AUDIT_LOG = os.environ.get("GUI_AUDIT_LOG", os.path.join(ROOT, "local", "onboard-gui-audit.log"))


_audit_logger = None


def _get_audit_logger():
    """Lazily build the rotating audit logger (size-bounded so it can't grow forever —
    Layer 0 logging foundation, docs/logging-architecture.md §3). The handler writes the
    raw TSV line as-is (no logging prefix) to preserve the exact audit format."""
    global _audit_logger
    if _audit_logger is None:
        lg = logging.getLogger("kontroll.onboard-gui.audit")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        lg.handlers.clear()            # idempotent on rebuild (no duplicate handlers)
        try:
            os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
            # FAIL-CLOSED rotation knobs (M-5): an empty/garbage env value falls to 5 MB x 5, NEVER to 0
            # (maxBytes=0 = no rotation = an unbounded GUI audit file, a C12 fail-open).
            h = RotatingFileHandler(AUDIT_LOG, maxBytes=positive_int("KONTROLL_AUDIT_MAX_MB", 5) * 1024 * 1024,
                                    backupCount=positive_int("KONTROLL_AUDIT_MAX_FILES", 5), encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(h)
        except OSError:
            pass
        _audit_logger = lg
    return _audit_logger


def _flatten_audit_field(v):
    """Flatten a tab and every line-breaking character in an audit field to a space so a caller-influenced value
    (e.g. a URL path variable like a discard run_id) can NEVER forge an extra TSV line or corrupt the tab-delimited
    columns — audit-log injection. The SAME function the API's writer uses (kontroll.auditfmt), not a copy of it;
    applied to action + detail so every _audit caller is protected, not just the one that surfaced it (the discard
    route, which audits the raw run_id before validation)."""
    return auditfmt.flatten_field(v)


def _audit(action, detail=""):
    """Append an action to the audit log — who (client IP), when, what. Never creds. Fields are flattened so a
    caller-controlled value can't inject a line/column into the TSV. Rotates at 5 MB × 5
    (engineering-standards §3b) so the log stays bounded."""
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "-")
        _get_audit_logger().info("%s\t%s\t%s\t%s" % (
            ts, _flatten_audit_field(ip), _flatten_audit_field(action), _flatten_audit_field(detail)))
    except OSError:
        pass


@app.errorhandler(paths.RequestBoundaryError)
def _boundary_refused(e):
    """A request field failed its closed-charset check somewhere below a route (SECURITY.md C22): answer 422 with
    the field's message and code — never the value — and leave an audit line naming the route and the FIELD. Mapped
    ONCE for the app, so a route with no `try`, or a new one, cannot turn a refusal into a bare 500 or an exception
    string. The routes that hand a request value to a service inside a broad `except` re-raise this error, and the
    repository's (below), first."""
    _audit("request-refuse", "reason=request-boundary field=%s route=%s" % (e.field, request.endpoint or "-"))
    return jsonify({"error": str(e), "code": e.code}), 422


@app.errorhandler(paths.RepositoryDataError)
def _repository_refused(e):
    """The repository's own data or layout stopped a request (SECURITY.md C22) — a module file whose adopted name
    is not one, a credential field declared badly, a backend with no role and a name none can be built from (or
    a registry role that is not a name), a write target outside the write root: 409 with a sentence that says
    where to look and `repository_data`, and an audit line of its own ACTION (every GUI route is authenticated,
    so every such refusal leaves one) — never a refused field, never a bare 500. Mapped ONCE for the app, like
    the field refusal above."""
    _audit("repository-refuse", "what=%s route=%s" % (e.what, request.endpoint or "-"))
    return jsonify({"error": str(e), "code": e.code}), e.status


def require_auth(fn):
    """HTTP Basic auth on every route; constant-time compare; fail-closed."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        auth = request.authorization
        ok = (auth and auth.username == GUI_USER
              and hmac.compare_digest(auth.password or "", GUI_PASSWORD))
        if not ok:
            _audit("auth-denied", request.path)
            return Response("authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="kontroll onboard"'})
        return fn(*a, **kw)
    return wrapper


@app.route("/")
@require_auth
def index():
    # has_remote gates the onboard form's "also push origin (offsite backup)" checkbox — hidden on a fresh node
    # with no offsite remote (the push would just no-op/fail there).
    return render_template("index.html", has_remote=gitio.offsite_remote_exists())


@app.route("/api/search")
@require_auth
def api_search():
    """Capability search over the kontroll service layer, in-process (no subprocess). Returns the
    same record list shape galaxy.py search --json produced, so the browser is unchanged."""
    q = (request.args.get("q") or "").split()
    if not q:
        return jsonify([])
    # Local hits are SHALLOW by default (fast — '?' cells need a deep probe); ?deep=true re-probes them.
    deep = (request.args.get("deep") or "").lower() in ("1", "true", "yes", "on")
    vectors, overrides, backends = _catalog_triple()
    try:
        records = service_search(q, deep=deep, vectors=vectors, overrides=overrides, backends=backends)
    except Exception as e:               # an external probe/search failure → a clean 500, not a crash
        return jsonify({"error": "search failed: %s" % e}), 500
    return jsonify(records)


@app.route("/api/fleet")
@require_auth
def api_fleet():
    """The onboarded fleet, class-centric (read-only) — what the operator has onboarded, each class's declared
    capabilities, and its inventory hosts. Powers the Fleet panel; the per-class capability badges reuse the
    same capability dialog the search cards do. Reads only (instance/fleet.yml + modules + inventory)."""
    from kontroll.service.fleet import list_fleet  # local import — read-only view, no per-request catalog needed
    try:
        return jsonify(list_fleet())
    except Exception as e:               # a malformed inventory/module → a clean 500, not a crash
        return jsonify({"error": "fleet read failed: %s" % e}), 500


@app.route("/api/services")
@require_auth
def api_services():
    """The control-plane services kontroll stands up (read-only) — powers the Index panel's services section:
    onboard-GUI/Semaphore/Homepage/Grafana/Prometheus/API with their mgmt URLs, so the operator jumps to each
    from 'what's running'. Reads only config/services.yml + the instance mgmt-IP/domain env; serves non-secret
    control-plane URLs (no audit, like api_fleet); a read-only surface never gates onboarding (INVARIANT D*)."""
    from kontroll.service.fleet import list_services  # local import — read-only view, no per-request catalog
    try:
        return jsonify(list_services())
    except Exception as e:               # a malformed config/services.yml → a clean 500, not a crash
        return jsonify({"error": "services read failed: %s" % e}), 500


@app.route("/api/pending")
@require_auth
def api_pending():
    """The staged proposals awaiting promotion (C10) — read-only. Powers the Pending panel: lists the canonical's
    proposed/<run_id> refs + what each changes (one status and one name per file, a moved file at both ends, a
    name that is not text in an escaped spelling marked as such), so the operator can copy a run_id into the
    Semaphore promote-proposal task; and, apart, any ref beside them that is not a proposal. Reads only the
    canonical bare repo, by full ref name and in the one environment `kontroll.canon` gives its git; NEVER
    promotes (that is the separate, admin-authed Semaphore app — the GUI cannot advance main; C10 two-key). A
    read-only surface never gates onboarding (D*)."""
    from kontroll.service.pending import list_pending  # local import — read-only view, no per-request catalog
    try:
        return jsonify(list_pending())
    except Exception as e:               # a git/read failure → a clean 500, not a worker crash
        return jsonify({"error": "pending read failed: %s" % e}), 500


@app.route("/api/pending/<run_id>/discard", methods=["POST"])
@require_auth
def api_pending_discard(run_id):
    """Discard a STALE (non-fast-forwardable) staged proposal — delete its `proposed/<run_id>` ref from the
    canonical (FF-race Move 2). C10-SAFE: un-stage ≠ promote — it removes Key-1's OWN output; it cannot advance
    `main`, enact anything, or grow the promotable set (the GUI still has NO promote path). Availability-only: an
    authed user can delete a legit pending proposal — the same trust tier the propose surface already grants — so
    the front-end gates the control to STALE rows + a two-step confirm; loss is recoverable from the audited sha
    (`git update-ref refs/heads/proposed/<id> <sha>` before gc), not the reflog (a bare canonical keeps none for a
    deleted branch). Compare-and-delete on the current tip: a concurrent promote/rebase that moved the ref → 409, a
    vanished/never-staged ref → 404, an invalid run_id → 400 — never a 500. Audited (action run_id + target run_id +
    sha; names/sha only, no secret)."""
    from kontroll.service import discard as _discard   # local import — the ONE pending write verb, kept out of the read view
    action_run_id = uuid.uuid4().hex[:12]   # correlates the two audit lines; distinct from the TARGET proposal's run_id
    _audit("pending-discard", "target_run_id=%s run_id=%s" % (run_id, action_run_id))
    out = _discard.discard_proposal(run_id)
    _audit("pending-discard-result", "target_run_id=%s ok=%s sha=%s run_id=%s" % (
        run_id, out.get("ok"), out.get("sha") or "-", action_run_id))
    if not out.get("ok"):
        return jsonify({"error": out.get("error"), "run_id": run_id}), out.get("code", 400)
    return jsonify({"discarded": True, "run_id": run_id, "ref": out.get("ref"), "sha": out.get("sha"),
                    "run_id_audit": action_run_id})


@app.route("/api/discovery")
@require_auth
def api_discovery():
    """The discovery INBOX (read-only) — un-onboarded hosts an onboarded device's OWN lease view sees but the
    onboarded inventory doesn't, so the operator can onboard them. Powers the Discovery panel; a row's 'Onboard
    this' pre-fills the discovered host into the EXISTING onboard form (the human still picks the class + reviews +
    onboards + promotes — C10 two-key; there is NO auto-onboard). Reads only a CACHED sweep artifact + the onboarded
    inventory; reaches NO device in-process (the sweep is the separate on-demand actor scripts/kontroll-discover.py
    — SECURITY C18). Writes nothing, stages nothing, never promotes; a read surface never gates onboarding (D*)."""
    from kontroll.service.discovery import read_inbox   # local import — read-only view, no per-request catalog
    try:
        return jsonify(read_inbox())
    except Exception as e:               # a malformed artifact/inventory → a clean 500, not a worker crash
        return jsonify({"error": "discovery read failed: %s" % e}), 500


@app.route("/api/provenance")
@require_auth
def api_provenance():
    """The supply-chain install PROVENANCE (read-only, MF-5 — no verification theatre): per collection the pin +
    signature policy + whether it is verified by a signature (green) or by pin + checksum ONLY (amber
    `unsigned-pinned`, the current 0-signature fleet). Powers the Index 'Supply chain' section so a green
    'installed' never implies a signature was checked. In-process over service.provenance.fleet_provenance — reads
    only the generated trust sidecar + the L2b digest lock (no value/secret; SEC-2); `available:false` (HTTP 200)
    when the sidecar isn't generated yet. Audited counts-by-class (names/counts only). A read surface never gates
    onboarding (INVARIANT D*); pinned assert_read_only."""
    from kontroll.service.provenance import fleet_provenance  # local import — read-only, no per-request catalog
    try:
        out = fleet_provenance()
    except Exception as e:               # a malformed sidecar → a clean 500, not a worker crash
        return jsonify({"error": "provenance read failed: %s" % e}), 500
    s = out.get("summary") or {}
    _audit("provenance-index", "collections=%d unsigned_pinned=%d signed=%d digests=%d" % (
        s.get("total", 0), s.get("unsigned_pinned", 0), s.get("signed", 0), s.get("digests_recorded", 0)))
    return jsonify(out)


@app.route("/api/image-provenance")
@require_auth
def api_image_provenance():
    """The published-IMAGE install provenance (read-only, report 22 §7.3 — the MF-5 honesty extended to the image
    supply chain): per kontroll image its class — `digest-pinned` (amber: a recorded `@sha256:` Docker verifies on
    pull, NOT a signature), `local-build` (no digest ⇒ built on the node), or `signed` (green, cosign — the deferred
    rung, never true today). Powers the Index 'Supply chain' images sub-panel so a digest-pin is never mistaken for a
    verified signature. In-process over service.provenance.image_provenance — reads ONLY docker/images.lock.yml
    (image names + public digests; no secret, SEC-2); `available:false` (HTTP 200) when the lock is absent. Audited
    counts-by-class. A read surface never gates onboarding (INVARIANT D*); pinned assert_read_only."""
    from kontroll.service.provenance import image_provenance  # local import — read-only, no per-request catalog
    try:
        out = image_provenance()
    except Exception as e:               # a malformed lock → a clean 500, not a worker crash
        return jsonify({"error": "image provenance read failed: %s" % e}), 500
    s = out.get("summary") or {}
    _audit("image-provenance-index", "images=%d digest_pinned=%d local_build=%d signed=%d" % (
        s.get("total", 0), s.get("digest_pinned", 0), s.get("local_build", 0), s.get("signed", 0)))
    return jsonify(out)


@app.route("/api/backups")
@require_auth
def api_backups():
    """The capture-bearing devices (read-only, C14) — powers the backup viewer index. Reads ONLY the `:ro` captures
    store; a missing store → {available:false} (HTTP 200, never a crash). Optional ?host=<dotted ansible_host>
    filters to that host's capture file(s) (the per-host drill-in). Audited NAMES/counts only — reading a
    secret-bearing capture over HTTP is the auditable event (stricter than the non-secret fleet/services reads). A
    read-only surface never gates onboarding (INVARIANT D*)."""
    from kontroll.service.backups import for_host, list_devices, BackupStoreError
    host = request.args.get("host") or None
    try:
        out = for_host(host) if host else list_devices()
    except BackupStoreError as e:
        return jsonify({"error": "backup store read failed: %s" % e}), 500
    _audit("backup-index", "host=%s devices=%d" % (host or "*", len(out.get("devices") or [])))  # counts only
    return jsonify(out)


@app.route("/api/backups/<path:file>/revisions")
@require_auth
def api_backup_revisions(file):
    """The VERSION LIST for one capture file (read-only, C14). `file` is validated by allow-list membership in the
    service (a name not in the store is rejected); `<path:>` is tolerated only so a dotted name routes. Audited
    NAMES/counts only."""
    from kontroll.service.backups import list_versions, BackupStoreError
    try:
        out = list_versions(file)
    except BackupStoreError as e:
        return jsonify({"error": "revisions read failed: %s" % e}), 400   # bad/unknown file → 400, not 500
    _audit("backup-revisions", "device=%s count=%d" % (file, len(out.get("revisions") or [])))
    return jsonify(out)


@app.route("/api/backups/<path:file>/view")
@require_auth
def api_backup_view(file):
    """A single rev's REDACTED text (read-only, C14). ?rev=<7-40 hex>. The capture value is masked server-side
    (config/capture-redactions.yml, redact-then-serve) before it leaves the process; the audit carries the file +
    rev + redacted flag, NEVER a capture line (G4). An invalid rev/file is a clean 400 (rejected before any
    `git show` — M2)."""
    from kontroll.service.backups import get_version, BackupStoreError
    rev = request.args.get("rev") or ""
    try:
        out = get_version(file, rev)
    except BackupStoreError as e:
        return jsonify({"error": "view failed: %s" % e}), 400
    _audit("backup-view", "device=%s rev=%s redacted=%s" % (file, rev, out.get("redacted")))
    return jsonify(out)


@app.route("/api/backups/<path:file>/diff")
@require_auth
def api_backup_diff(file):
    """The REDACTED structured-hunk DIFF between two revs (read-only, C14). ?base=<hex>&compare=<hex>. Redact-then-
    diff (the diff runs on masked text, so a pure-secret change shows masked-vs-masked). Audited file + revs only,
    never a hunk line (G4)."""
    from kontroll.service.backups import diff_versions, BackupStoreError
    base, compare = request.args.get("base") or "", request.args.get("compare") or ""
    try:
        out = diff_versions(file, base, compare)
    except BackupStoreError as e:
        return jsonify({"error": "diff failed: %s" % e}), 400
    _audit("backup-diff", "device=%s a=%s b=%s" % (file, base, compare))
    return jsonify(out)


@app.route("/api/settings")
@require_auth
def api_settings():
    """The platform-settings snapshot (read-only) — powers the Settings panel: instance identity (mgmt_ip/domain/
    tls_mode), offsite backup remotes, fleet enablement, and a status group (privileged-mutation arming, running
    services, the secret-domain NAMES roster). Reads only the INPUTS (instance.yml/fleet.yml/the GUI env/the
    secret-form NAMES); serves NO secret value and NO docker/.env line (C11/C12); stages nothing. A read-only
    surface never gates onboarding (INVARIANT D*)."""
    from kontroll.service.settings import read_view  # local import — read-only view, no per-request catalog
    try:
        return jsonify(read_view())
    except Exception as e:               # a malformed source → a clean 500, not a worker crash
        return jsonify({"error": "settings read failed: %s" % e}), 500


@app.route("/api/homepage")
@require_auth
def api_homepage():
    """The normalized homepage board (sections[]+items[]+order) for the Homepage editor — read-only. Degrades to a
    safe partial board on a missing/malformed file (never 500s the worker; service-never-sys.exit)."""
    from kontroll.service.homepage import read_board  # local import — read-only view, no per-request catalog
    try:
        return jsonify(read_board())
    except Exception as e:               # belt-and-braces; read_board already degrades
        return jsonify({"error": "homepage read failed: %s" % e}), 500


@app.route("/api/homepage", methods=["POST"])
@require_auth
def api_homepage_apply():
    """PROPOSE (apply:false → the diff + a plan_token, writes nothing) or APPLY (token-gated write of the two
    overlay files, audited NAMES-only, committed + STAGED proposed/<run_id>, C10). One handler; the board is data.
    The GUI NEVER promotes (no promote button — C10 two-key). 422 on a malformed/forged board; 409 on drift."""
    from kontroll.service import homepage as hp  # local import — read+plan+stage seam
    d = request.get_json(force=True) or {}
    board = d.get("board") or {}
    plan = hp.build_homepage_plan(board)
    if plan.get("error"):
        return jsonify({"error": plan["error"], "detail": plan.get("detail")}), 422
    if not d.get("apply"):
        return jsonify({"applied": False, "paths": plan["paths"], "diff": plan["diff"],
                        "summary": plan["summary"], "token": plan["plan_token"]})
    run_id = uuid.uuid4().hex[:12]   # correlates the audit line, the commit, and the staged proposal (C10)
    s = plan["summary"]
    _audit("homepage-edit", "sections=%d removed=%s run_id=%s" % (
        len(board.get("sections") or []), ",".join(s.get("removed_tiles") or []) or "none", run_id))
    out = hp.promote_board(board, d.get("token"))
    if out.get("error") == "drift":
        return jsonify({"error": "homepage config drifted since propose; re-propose"}), 409
    if out.get("error"):
        return jsonify({"error": out["error"], "detail": out.get("detail")}), 422
    if not out["changed"]:
        _audit("homepage-result", "changed=no run_id=%s" % run_id)
        return jsonify({"applied": True, "changed": False, "note": "no change; nothing to commit"})
    git = gitio.commit_and_push(out["paths"], [
        "feat(homepage): rearrange the portal via the Homepage editor",
        "Reordered/selected control-plane tiles via the kontroll GUI (run_id %s). Data-only edit of the tracked "
        "homepage overlay; the generated fleet block is untouched." % run_id],
        run_id=run_id)               # staging GUI ⇒ proposed/<run_id> (C10), never main
    _audit("homepage-result", "committed=%s staged=%s run_id=%s" % (git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing staged (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": True, "paths": out["paths"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "next": ("promote run_id %s in Semaphore, then deploy-stack reloads :3000" % run_id)
                            if git["staged"] else "committed to main"})


@app.route("/api/classify")
@require_auth
def api_classify():
    """Backend classification over the service layer, in-process. Structured result
    {collection, matches, best, requires}; 404 if the collection isn't installed locally."""
    coll = request.args.get("collection") or ""
    _, _, backends = _catalog_triple()
    try:
        result = service_classify(coll, backends=backends)
    except (paths.RequestBoundaryError, paths.RepositoryDataError):
        raise                                # the app-wide handlers answer these (422, 409), not a "failed" 500
    except Exception as e:
        return jsonify({"error": "classify failed: %s" % e}), 500
    if result is None:
        return jsonify({"error": "%s not installed locally" % coll}), 404
    return jsonify(result)


def _onboard_plan_view(plan):
    """The client-safe onboard plan: the module + drop-in host blocks, the rel paths, and the cred VAR
    NAMES — never the credential values (those live only in the in-memory plan's creds_to_set). Mirrors
    api/routes/onboard.py._plan_view so the GUI and API expose the same shape."""
    return {"collection": plan["collection"], "key": plan["key"], "group": plan["group"],
            "backend": plan["backend"], "role": plan["role"], "host_name": plan["host_name"],
            "module_reuse": plan.get("module_reuse", False),   # reusing a curated class — add host only (#124)
            "module": plan["module"], "host_block": plan["host_block"],
            "paths": {"module": plan["mod_path"], "inventory": plan["inv_path"]},
            "creds": sorted(plan["creds_to_set"].keys()),
            # F1: the DERIVED credential-field descriptors (names/kinds/labels/flags — NEVER values, wire-projected
            # by authspec.public_descriptor); cred_source = shallow (backend auth: block) | fallback (generic union).
            "cred_fields": [authspec.public_descriptor(cf) for cf in (plan.get("cred_fields") or [])],
            "cred_source": plan.get("cred_source"),
            # F2: the honest capability GAP — vendor/credentialed rungs this blind class can't auto-derive (names +
            # why only, non-secret); advisory + NON-GATING (INVARIANT D*). Empty for a full-parity / reused class.
            "capability_gap": plan.get("capability_gap") or [],
            "provisioning": plan.get("provisioning") or []}   # advisory cred-prereqs (role NAMES only, non-gating)


@app.route("/api/onboard/cred-fields", methods=["GET"])
@require_auth
def api_onboard_cred_fields():
    """The DERIVED credential-field descriptors (names/kinds/labels/flags — NEVER values) for a backend, so the
    onboard form renders the RIGHT per-backend inputs (SSH login for a switch, a token for a REST device) the
    instant a result is picked. When `collection` resolves to a curated device-class (the reuse path), that class's
    OWN auth: block wins — a proxmox result shows its coherent api-token auth_set (grouped, names lined up with the
    pve-exporter), not the generic single token box (F1 seam S1). Mirrors api/routes/onboard.onboard_cred_fields.
    Read-only, pure schema — no creds, no write; a read surface never gates onboarding (INVARIANT D*)."""
    backend = request.args.get("backend") or ""
    secrets = request.args.get("secrets") or "network"
    collection = request.args.get("collection") or None
    deep = (request.args.get("deep") or "").lower() in ("1", "true", "yes", "on")
    paths.component(secrets, "secrets domain")     # it is written into every descriptor this answers with
    _vectors, _overrides, backends = _catalog_triple()
    bdef = next((b for b in backends if b.get("name") == backend), {})
    module = _catalog.module_for_collection(collection) if collection else None
    # F1 TIER-B (Rung 3): SHARPEN to the EXACT argument_spec fields when the collection is installed (install-
    # confined — MF-S1 also fail-closes inside authspec). Mirrors api/routes/onboard.onboard_cred_fields.
    modules = None
    if deep and collection and collection in _catalog.local_installed():
        modules = authspec.auth_candidate_modules(probe.deep_probe(collection))
    out = authspec.derive_auth(bdef, secrets, module=module, deep=deep, coll=collection, modules=modules)
    return jsonify({"backend": backend, "secrets": secrets,
                    "cred_fields": [authspec.public_descriptor(cf) for cf in out["fields"]],
                    "cred_source": out["source"]})


@app.route("/api/onboard", methods=["POST"])
@require_auth
def api_onboard():
    """Onboard a device, IN-PROCESS over the service layer (mirrors api/routes/onboard.py — no subprocess,
    no creds in argv). Dry-run (apply omitted) returns the plan with cred VAR NAMES only; apply writes the
    module + drop-in host + fleet-enable + encrypts creds to SOPS, then commits + pushes the canonical —
    STAGED as `proposed/<run_id>` when the deploy armed `KONTROLL_STAGE_PUSHES` (C10, propose-then-promote),
    else direct to `main`. Bootstrap (collection install + live-verify) is DEFERRED to a post-promote
    Semaphore enact — never run inline (the network surface runs no Ansible, and bootstrapping a host that
    lives only in an un-promoted proposal would be premature). Creds are NEVER returned or logged."""
    d = request.get_json(force=True) or {}
    for req in ("collection", "key", "group", "host"):
        if not d.get(req):
            return jsonify({"error": "missing required field: %s" % req}), 400
    vectors, _overrides, backends = _catalog_triple()
    creds = d.get("creds")
    if creds is None:   # legacy scalar caller — fold the four named creds into the F1 map (back-compat)
        creds = {f: d.get(f) for f in ("username", "password", "api_token", "ssh_private_key") if d.get(f)}
    try:
        plan = build_onboard_plan(
            d["collection"], d["key"], d["group"], d["host"],
            host_name=d.get("host_name"), secrets=d.get("secrets") or "network", backend=d.get("backend"),
            creds=creds, backends=backends, vectors=vectors, deep=bool(d.get("deep")))
    except ValueError as e:
        # A catchable planner guard fired pre-write (MF-S2 domain-confinement, or a partially-filled required
        # auth_set — more reachable via F1 TIER-B). A clean 422 client error, never a 500 (mirrors the API route).
        # The `code` is what lets the browser render per-refusal next-steps copy instead of a bare error (the
        # permissive edition keys its install-offer copy on `invalid_collection`). A request-boundary refusal derives
        # it from the refused FIELD — `collection` → invalid_collection, `device-class key` → invalid_device_class_key —
        # never from the value, which is also kept out of the audit line.
        field = getattr(e, "field", None)
        code = e.code if isinstance(e, paths.RequestBoundaryError) else "invalid_request"
        _audit("onboard-refuse", "reason=%s field=%s" % (
            "request-boundary" if isinstance(e, paths.RequestBoundaryError) else "planner-guard", field or "input"))
        return jsonify({"error": str(e), "code": code}), 422
    if plan["error"] == "not_installed":
        return jsonify({"error": "install the collection first: %s" % d["collection"]}), 404
    if plan["error"] == "no_backend":
        # the API's answer and audit line (api/routes/onboard.py): what the collection showed; the reason alone logged
        _audit("onboard-refuse", "reason=no-backend")
        return jsonify({"error": no_backend_message(plan, d["collection"], (
            "if it is a plain SSH host, onboard it from the Generic SSH host search result (search: ssh), "
            "or pass an explicit backend")), "code": "no_backend"}), 422
    run_id = uuid.uuid4().hex[:12]   # correlates the audit line, the commit, and the staged proposal (C10)
    bootstrap = bool(d.get("bootstrap"))
    if not d.get("apply"):
        # Audit the action (NOT the creds) — the privileged path is logged who/when/what.
        _audit("onboard-dryrun", "collection=%s key=%s group=%s host=%s run_id=%s" % (
            d["collection"], d["key"], d["group"], d["host"], run_id))
        return jsonify({"applied": False, "plan": _onboard_plan_view(plan),
                        "bootstrap_requested": bootstrap, "run_id": run_id})
    _audit("onboard-apply", "collection=%s key=%s group=%s host=%s creds=%s push=%s run_id=%s" % (
        d["collection"], d["key"], d["group"], d["host"],
        ",".join(sorted(plan["creds_to_set"])) or "none", bool(d.get("push")), run_id))
    try:
        applied = apply_onboard_plan(plan, overwrite=bool(d.get("overwrite")))
    except gitio.WriteConflict as e:
        # A drop-in (module/inventory) already exists divergently — return a clean 409, NOT a SystemExit that
        # would kill the worker and surface as a bare "request failed" (live-caught onboarding a shipped key).
        # `conflict: true` tells the GUI to offer an Overwrite button (re-submits with overwrite=true).
        _audit("onboard-conflict", "key=%s run_id=%s" % (d["key"], run_id))
        return jsonify({"error": str(e), "conflict": True, "run_id": run_id}), 409
    if not applied["changed"]:
        _audit("onboard-result", "key=%s changed=no run_id=%s" % (d["key"], run_id))
        return jsonify({"applied": True, "changed": False, "committed": False,
                        "host_name": plan["host_name"], "run_id": run_id,
                        "note": "nothing changed (already onboarded); nothing to commit"})
    git = gitio.commit_and_push(
        applied["paths"],
        ["feat(onboard): %s -> %s via %s backend" % (d["collection"], d["key"], plan["backend"]),
         "Onboarded via the kontroll onboarding GUI (run_id %s). Host %s in group %s; run ping.yml --limit HOST to verify." % (
             run_id, plan["host_name"], plan["group"])],   # the plan's group: a reuse ADOPTS the class's
        push_origin=bool(d.get("push")), run_id=run_id)   # staging GUI ⇒ proposed/<run_id> (C10)
    _audit("onboard-result", "key=%s committed=%s staged=%s run_id=%s" % (
        d["key"], git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing pushed (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": True,
                    "pushed_canonical": git["pushed_canonical"], "pushed_origin": git["pushed_origin"],
                    "target_ref": git["target_ref"], "staged": git["staged"],
                    "host_name": plan["host_name"], "run_id": run_id, "bootstrap_requested": bootstrap,
                    "next": ("promote the proposal, then bootstrap: `kontroll promote %s`" % run_id)
                            if git["staged"] else "bootstrap (collection install + live-verify) is a Semaphore task"})


@app.route("/api/configurable")
@require_auth
def api_configurable():
    """The uniform read-only KNOB-GROUP projection behind the ONE knob renderer (Phase 3, #137): `?kind=&id=` →
    service/configurable.configurable_view. A `secret-domain` returns its `fields` group (NAMES only, never a
    value); a `capability:<cap>` wraps the read-only `suggest_view` + adds the projected param/Stage-3 groups.
    Read-only — projects, never stages/decrypts (pinned by assert_read_only). A read surface never gates
    onboarding (INVARIANT D*); a missing kind/capability/form is a 404, a malformed source a clean 500."""
    from kontroll.service import configurable  # local import — read-only projection, no per-request catalog
    kind = request.args.get("kind") or ""
    obj_id = request.args.get("id") or ""
    try:
        view = configurable.configurable_view(kind, obj_id)
    except (paths.RequestBoundaryError, paths.RepositoryDataError):
        raise                                # the app-wide handlers answer these (422, 409), not a "failed" 500
    except Exception as e:               # a probe/registry failure → a clean 500, not a worker crash
        return jsonify({"error": "configurable read failed: %s" % e}), 500
    if view.get("error") in ("no_kind", "no_capability", "no_form", "not_onboarded", "no_knob", "not_enabled",
                             "no_unit"):
        return jsonify(view), 404
    return jsonify(view)


@app.route("/api/capability/<cap>/suggest")
@require_auth
def api_capability_suggest(cap):
    """Read-only Stage-0 detection for opening `cap`'s standalone dialog on an onboarded class — in-process
    over the service layer (like search/classify). 404 for an unknown capability or an un-onboarded key:
    the capability surface is strictly AFTER onboarding and never gates it (INVARIANT D*)."""
    key = request.args.get("key") or ""
    vectors, _, _ = _catalog_triple()
    try:
        view = capability.suggest_view(cap, key, vectors=vectors)
    except (paths.RequestBoundaryError, paths.RepositoryDataError):
        raise                                # the app-wide handlers answer these (422, 409), not a "failed" 500
    except Exception as e:               # a probe/registry failure → a clean 500, not a crash
        return jsonify({"error": "suggest failed: %s" % e}), 500
    if view.get("error") == "no_capability":
        return jsonify({"error": "no such capability: %s" % cap}), 404
    if view.get("error") == "not_onboarded":
        return jsonify({"error": "%s is not onboarded yet — onboard it first" % key}), 404
    return jsonify(view)


@app.route("/api/capability/<cap>", methods=["POST"])
@require_auth
def api_capability(cap):
    """PROPOSE (apply:false — a pure plan + an anti-drift token) or PROMOTE (apply:true — the token-gated,
    audited write + a commit scoped to the plan's paths) `cap` for an onboarded class. The actuation half
    (deploy/reload) is the operator's: the response returns the enact commands; nothing is run here. One
    handler for every capability — `cap` is a path segment, not a branch (INVARIANT D*: strictly-after-onboard)."""
    d = request.get_json(force=True) or {}
    key, selection = d.get("key") or "", d.get("selection") or {}
    if capability.get_descriptor(cap) is None:
        return jsonify({"error": "no such capability: %s" % cap}), 404
    if not d.get("apply"):
        plan = capability.propose(cap, key, selection)
        if plan.get("error"):
            return jsonify({"error": plan["error"], "detail": plan.get("detail")}), 422
        # Phase 4a: surface the reconfigure DIFF (changes/will_overwrite/severity) so the dialog renders the
        # reconfig-diff pane + gates Promote behind the right-weight confirm. The change PATHS are non-secret
        # (telemetry/backup param paths) — no value leaks (these capabilities carry no secret value).
        return jsonify({"applied": False, "cap": cap, "key": key, "method": plan.get("method"),
                        "params": plan.get("params"), "paths": plan["paths"], "enact": plan["enact"],
                        "links": plan.get("links") or [],   # capability links (telemetry → Grafana, #122) — generic
                        "changes": plan.get("changes") or [], "will_overwrite": plan.get("will_overwrite") or [],
                        "severity": plan.get("severity"),
                        "token": plan["plan_token"]})
    # Promote — audit who/what (method NAME + change paths/severity, NEVER a value), then the token-gated +
    # no-drop-gated write + commit.
    run_id = uuid.uuid4().hex[:12]   # correlates the audit line, the commit, and the staged proposal (C10)
    _audit("capability-promote", "cap=%s key=%s method=%s params=%d run_id=%s" % (
        cap, key, selection.get("method"), len(selection.get("params") or {}), run_id))
    out = capability.promote(cap, key, selection, d.get("token"))
    if out.get("error") == "drift":   # anti-drift 409 — now WITH the diff (G6) so the UI shows what moved under the operator
        return jsonify({"error": "plan drifted since propose; re-propose",
                        "changes": out.get("changes") or []}), 409
    if out.get("error") == "would_drop":   # anti-clobber machine gate refused (verify_no_drop) — 409 + the diff
        return jsonify({"error": "this change would drop a method another dialog declared on the class — re-propose",
                        "changes": out.get("changes") or []}), 409
    if out.get("error"):
        return jsonify({"error": out["error"], "detail": out.get("detail")}), 422
    if not out["changed"]:
        _audit("capability-result", "cap=%s key=%s changed=no run_id=%s" % (cap, key, run_id))
        return jsonify({"applied": True, "changed": False, "note": "already declared; nothing to commit"})
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(%s): enable on %s via the capability dialog" % (cap, key),
         "Promoted via the kontroll onboarding GUI (run_id %s). Data-only write; nothing actuated until "
         "the enact commands run." % run_id],
        push_origin=bool(d.get("push")), run_id=run_id)   # staging GUI ⇒ proposed/<run_id> (C10)
    # severity is re-derived from the SERVER's recomputed plan (out["plan"]), never a client-sent field — the
    # reconfigure audit carries the change severity + paths (non-secret), correlated by run_id (M1).
    _audit("capability-result", "cap=%s key=%s severity=%s changed=%s paths=%d committed=%s staged=%s run_id=%s" % (
        cap, key, (out["plan"].get("severity") or "add"), True, len(out.get("paths") or []),
        git["committed"], git["staged"], run_id))
    if not git["committed"]:        # a refused add or a failed commit: nothing was staged — never answer as if it were
        return jsonify({"error": "commit failed; nothing staged (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": True,
                    "pushed_canonical": git["pushed_canonical"], "paths": out["paths"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "links": out["plan"].get("links") or [],   # Grafana deep-links to surface post-promote (#122)
                    "enact": out["plan"]["enact"]})


# --- app-store UNIT dialog: search a collection's runnable units, then AUTHOR an actuation unit -------------
# The GUI mounts the create→configure→preview→stage substrate (service/actuation.py + actuation/<key>/unit.yml).
# api_units is the read-only Pick stage (a collection's runnable units); api_actuation_create AUTHORS the unit
# descriptor — mirrors api/routes/actuation.py's create IN-PROCESS (the same propose→stage spine the rest of the
# GUI uses): apply:false = a pure plan + token (writes nothing), apply:true = stage instance/actuation/<key>/
# unit.yml to proposed/<run_id> via commit_and_push(run_id) (C10, never main). The descriptor is validated through
# the SAME fail-closed schema before it stages; a high-blast target is refused (the create-time Tier-cap, 403).
_UNIT_CREATE_STATUS = {"invalid": 422, "no_module": 422, "not_enabled": 409, "exists": 409, "tier_cap": 403,
                       "drift": 409}


def _unit_create_detail(out):
    """The human message for a create-flow error — the per-field errors for 'invalid', a pointed line for the
    Tier-cap / collision / unresolved-class refusals, a re-propose hint for drift. Never echoes a credential
    (no create input is a secret)."""
    err = out.get("error")
    if err == "invalid":
        return out.get("errors") or {}
    if err == "drift":
        return "plan drifted since propose; re-propose"
    if err == "tier_cap":
        return ("the %s device-class targets %s, the highest-blast tier — an app-store install there needs a "
                "signed source (public Galaxy is unsigned). Refused (never-brick + blast-radius)."
                % (out.get("device_class"), out.get("inventory_group")))
    if err == "exists":
        return "a unit '%s' already exists — configure or disable it, don't overwrite" % out.get("key")
    if err == "not_enabled":
        return "the device-class '%s' is not an enabled module (instance/fleet.yml)" % out.get("device_class")
    if err == "no_module":
        return "no device-class module declares collection '%s'" % out.get("collection")
    return "create failed"


@app.route("/api/units/<collection>")
@require_auth
def api_units(collection):
    """List a collection's runnable units (the app-store Pick stage) — read-only, in-process over service_units
    (collection-shipped playbooks + roles; empty if it ships only modules). 404 if the collection isn't installed
    locally. The SEC-3 traversal guard lives in the service (`collection` resolves against the installed set,
    never a raw path-join). A read surface never gates onboarding (INVARIANT D*); pinned `assert_read_only`."""
    from kontroll.service.units import service_units  # local import — read-only, no per-request catalog
    try:
        result = service_units(collection)
    except (paths.RequestBoundaryError, paths.RepositoryDataError):
        raise                                # the app-wide handlers answer these (422, 409), not a "failed" 500
    except Exception as e:               # a probe failure → a clean 500, not a worker crash
        return jsonify({"error": "units read failed: %s" % e}), 500
    if result is None:
        return jsonify({"error": "%s not installed locally" % collection}), 404
    _audit("unit-list", "collection=%s units=%d" % (collection, len(result.get("units") or [])))
    return jsonify(result)


@app.route("/api/actuation/create", methods=["POST"])
@require_auth
def api_actuation_create():
    """AUTHOR an actuation unit from a searched collection — propose (apply:false → the derived key + the unit.yml
    path + a token, writes nothing) or create (apply:true → render+validate the descriptor, then STAGE only
    instance/actuation/<key>/unit.yml to proposed/<run_id> via commit_and_push, C10). Mirrors
    api/routes/actuation.py's create IN-PROCESS. device_class/inventory_group are DERIVED from the declaring
    module; the descriptor is validated through the SAME fail-closed schema before it stages; a high-blast target
    is refused (the create-time Tier-cap, 403); an existing unit is never clobbered (409). No field is a secret;
    the audit carries NAMES/counts/run_id only. The GUI NEVER promotes (C10 two-key — no promote button)."""
    from kontroll.service.actuation import build_create_unit_plan, stage_create_unit  # local import — write seam
    d = request.get_json(force=True) or {}
    inputs = {"collection": d.get("collection"), "kind": d.get("kind"), "name": d.get("name"),
              "version": d.get("version"), "blast_radius": d.get("blast_radius")}
    if not d.get("apply"):
        plan = build_create_unit_plan(inputs)
        if plan.get("error"):
            return jsonify({"error": plan["error"], "detail": _unit_create_detail(plan)}), \
                _UNIT_CREATE_STATUS.get(plan["error"], 422)
        return jsonify({"applied": False, "key": plan["key"], "paths": plan["paths"], "token": plan["plan_token"],
                        "device_class": plan["device_class"], "inventory_group": plan["inventory_group"],
                        "blast_radius": plan["blast_radius"], "version": plan["version"]})
    run_id = uuid.uuid4().hex[:12]   # correlates the audit line, the commit, and the staged proposal (C10)
    _audit("unit-create", "collection=%s kind=%s name=%s blast=%s run_id=%s" % (
        d.get("collection"), d.get("kind"), d.get("name"), d.get("blast_radius"), run_id))
    out = stage_create_unit(inputs, d.get("token"))
    if out.get("error"):
        return jsonify({"error": out["error"], "detail": _unit_create_detail(out)}), \
            _UNIT_CREATE_STATUS.get(out["error"], 422)
    key = out["plan"]["key"]
    if not out["changed"]:
        _audit("unit-result", "key=%s changed=no run_id=%s" % (key, run_id))
        return jsonify({"applied": True, "changed": False, "committed": False, "key": key, "run_id": run_id,
                        "note": "descriptor unchanged; nothing to stage"})
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(actuation): create app-store unit %s via the GUI" % key,
         "Authored the actuation unit descriptor for %s via the kontroll onboarding GUI (run_id %s). Config-only "
         "write (no secret); nothing installed/actuated until the operator promotes the proposal, then "
         "configures + runs the unit in Semaphore." % (key, run_id)],
        push_origin=bool(d.get("push")), run_id=run_id)   # staging GUI ⇒ proposed/<run_id> (C10)
    _audit("unit-result", "key=%s committed=%s staged=%s run_id=%s" % (key, git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing staged (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": True, "key": key, "paths": out["paths"],
                    "device_class": out["plan"]["device_class"], "inventory_group": out["plan"]["inventory_group"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "next": (("promote run_id %s in Semaphore, then configure + run the unit" % run_id)
                             if git["staged"] else "committed to main — configure + run the unit in Semaphore")})


# --- configure an EXISTING registered unit: list the registry (the Automations index entry point), then PREVIEW +
# STAGE the unit's curated configure VALUES on the SAME C10 spine. api_actuation lists the registered units
# (read-only, the Index panel's Automations section); api_actuation_stage mirrors api/routes/actuation.py::stage
# IN-PROCESS — apply:false = the would-run PLAY + an anti-drift token (writes nothing), apply:true = stage
# instance/actuation/<key>/vars.yml to proposed/<run_id> via commit_and_push(run_id) (C10, never main). The knob
# group itself is projected read-only by api_configurable (kind=actuation-unit). SEC-2 keeps every staged value
# non-secret, so the audit carries the knob COUNT + run_id only — never a value.
_UNIT_STAGE_STATUS = {"no_unit": 404, "invalid": 422, "drift": 409}


def _unit_stage_detail(out, key):
    """The human message for a configure-stage error — the per-knob errors for 'invalid', a re-propose hint for
    drift, else a clean not-found. Never echoes a submitted value (values are non-secret config; the detail +
    audit carry knob NAMES/counts only)."""
    err = out.get("error")
    if err == "invalid":
        return out.get("errors") or {}
    if err == "drift":
        return "plan drifted since propose; re-propose"
    return "no actuation unit '%s'" % key


@app.route("/api/actuation")
@require_auth
def api_actuation():
    """List the REGISTERED actuation units (the Automations index — what the operator has authored) — read-only,
    in-process over service.units.registered_units_view: per unit the derived target / `==` pin / knob-count /
    configured flag, NEVER a value (SEC-2). Powers the Index panel's Automations section; each row's key opens the
    configure dialog (api_configurable knob group → api_actuation_stage preview/stage). A read surface never gates
    onboarding (INVARIANT D*); pinned assert_read_only."""
    from kontroll.service.units import registered_units_view  # local import — read-only, no per-request catalog
    try:
        units = registered_units_view()
    except Exception as e:               # a malformed registry → a clean 500, not a worker crash
        return jsonify({"error": "actuation registry read failed: %s" % e}), 500
    _audit("unit-registry", "units=%d" % len(units))
    return jsonify({"units": units})


@app.route("/api/actuation/<key>", methods=["POST"])
@require_auth
def api_actuation_stage(key):
    """PREVIEW (apply:false → the would-run play + the resolved non-secret vars + an anti-drift token, writes
    nothing) or STAGE (apply:true → re-validate + write instance/actuation/<key>/vars.yml, then stage ONLY that
    path to proposed/<run_id> via commit_and_push, C10) the configure VALUES of registered unit `key`. Mirrors
    api/routes/actuation.py::stage IN-PROCESS. 404 no unit; 422 invalid values; 409 drift. SEC-2: no value is a
    secret; the audit carries the knob COUNT + run_id only. The GUI NEVER promotes (C10 two-key — no promote
    button); /api/actuation/create wins the static path, so `key` never serves 'create'."""
    from kontroll.service.actuation import build_actuation_plan, stage_plan  # local import — write seam
    d = request.get_json(force=True) or {}
    values = d.get("values") or {}
    if not d.get("apply"):
        plan = build_actuation_plan(key, values)
        if plan.get("error"):
            return jsonify({"error": plan["error"], "detail": _unit_stage_detail(plan, key)}), \
                _UNIT_STAGE_STATUS.get(plan["error"], 422)
        return jsonify({"applied": False, "key": key, "paths": plan["paths"], "token": plan["plan_token"],
                        "play": plan["play"], "target": plan["target"], "vars": plan["vars"],
                        "check_first": plan["check_first"]})
    run_id = uuid.uuid4().hex[:12]   # correlates the audit line, the commit, and the staged proposal (C10)
    _audit("unit-config-stage", "key=%s vars=%d run_id=%s" % (key, len(values), run_id))
    out = stage_plan(key, values, d.get("token"))
    if out.get("error"):
        return jsonify({"error": out["error"], "detail": _unit_stage_detail(out, key)}), \
            _UNIT_STAGE_STATUS.get(out["error"], 422)
    if not out["changed"]:
        _audit("unit-config-result", "key=%s changed=no run_id=%s" % (key, run_id))
        return jsonify({"applied": True, "changed": False, "committed": False, "key": key, "run_id": run_id,
                        "note": "values unchanged; nothing to stage"})
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(actuation): stage configure values for %s via the GUI" % key,
         "Staged the configure values for actuation unit %s via the kontroll onboarding GUI (run_id %s). "
         "Config-only write (no secret); nothing actuated until the operator promotes the proposal + runs the "
         "unit in Semaphore." % (key, run_id)],
        push_origin=bool(d.get("push")), run_id=run_id)   # staging GUI ⇒ proposed/<run_id> (C10)
    _audit("unit-config-result", "key=%s committed=%s staged=%s run_id=%s" % (
        key, git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing staged (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": True, "key": key, "paths": out["paths"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "next": (("promote run_id %s in Semaphore, then run the unit" % run_id)
                             if git["staged"] else "committed to main — run the unit in Semaphore")})


# --- module-IDENTITY reconfigure (Phase 4b, #139): re-classify a class's secrets_domain/inventory_group/backend/
# role. The heaviest reconfigure short of platform settings — an identity change re-homes hosts / orphans
# creds — so it rides the SAME propose→token→promote→stage spine + the type-to-confirm identity gate, and STAGES
# proposed/<run_id> (C10, never main). The diff PATHS + values are non-secret config (auditable; no value leak).
@app.route("/api/identity/<key>", methods=["POST"])
@require_auth
def api_identity(key):
    """PROPOSE (apply:false — a pure plan + an anti-drift token + the field DIFF) or PROMOTE (apply:true — the
    token-gated + no-drop-gated identity write, scoped commit, C10 stage) the identity keys of onboarded class
    `key`. Mirrors api_capability's shape; the reconfigure half is shared (`_reconfig.run_promote`). The write
    stages module.yml; the operator regenerates targets + re-runs bootstrap/deploy-stack (the enact hand-off)."""
    d = request.get_json(force=True) or {}
    selection = d.get("selection") or {}
    if not d.get("apply"):
        plan = identity_service.build_plan(key, selection)
        if plan.get("error"):
            return jsonify({"error": plan["error"], "detail": plan.get("detail")}), 422
        return jsonify({"applied": False, "key": key, "paths": plan["paths"], "enact": plan["enact"],
                        "changes": plan.get("changes") or [], "will_overwrite": plan.get("will_overwrite") or [],
                        "severity": plan.get("severity"), "token": plan["plan_token"]})
    # Promote — audit the changed FIELDS (NAMES; identity values are non-secret) + run_id, NO client severity.
    run_id = uuid.uuid4().hex[:12]
    fields = ",".join(sorted((selection.get("values") or {}).keys())) or "none"
    _audit("identity-promote", "key=%s fields=%s run_id=%s" % (key, fields, run_id))
    out = _reconfig.run_promote(identity_service.build_plan, identity_service.apply_plan,
                                d.get("token"), key, selection)
    if out.get("error") == "drift":
        return jsonify({"error": "plan drifted since propose; re-propose",
                        "changes": out.get("changes") or []}), 409
    if out.get("error") == "would_drop":
        return jsonify({"error": "this change would drop an identity key another edit set on the class — re-propose",
                        "changes": out.get("changes") or []}), 409
    if out.get("error"):
        return jsonify({"error": out["error"], "detail": out.get("detail")}), 422
    if not out["changed"]:
        _audit("identity-result", "key=%s changed=no run_id=%s" % (key, run_id))
        return jsonify({"applied": True, "changed": False, "note": "no change; nothing to commit"})
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(identity): reconfigure %s identity via the GUI" % key,
         "Re-classified module identity (run_id %s). Data-only write; the operator regenerates targets + "
         "re-runs bootstrap/deploy-stack to make it live (the enact hints)." % run_id],
        push_origin=bool(d.get("push")), run_id=run_id)   # staging GUI ⇒ proposed/<run_id> (C10)
    # severity re-derived from the SERVER's recomputed plan (out["plan"]), never a client field (M1).
    _audit("identity-result", "key=%s severity=%s changed=%s paths=%d committed=%s staged=%s run_id=%s" % (
        key, (out["plan"].get("severity") or "modify"), True, len(out.get("paths") or []),
        git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing staged (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": git["committed"], "paths": out["paths"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "enact": out["plan"]["enact"]})


# --- inventory HOST-VAR reconfigure (Phase 4b, #139): change an onboarded host's editable connection vars
# (MVP: ansible_host — "the device moved"). The silent key-level .update lifted into a diff via gitio.owned_merge;
# severity `modify` (a single-host re-IP), STAGES proposed/<run_id> (C10). Non-secret connection var (auditable).
@app.route("/api/host/<key>/<host>", methods=["POST"])
@require_auth
def api_host(key, host):
    """PROPOSE (apply:false — a pure plan + token + the var DIFF) or PROMOTE (apply:true — the token-gated +
    no-drop-gated host-var write, scoped commit, C10 stage) of `host` in onboarded class `key`. Mirrors
    api_identity; the reconfigure half is the shared `_reconfig.run_promote`. The write re-serializes the
    banner-owned drop-in (machine-generated, no comment-surgery); the operator confirms reachability at enact."""
    d = request.get_json(force=True) or {}
    selection = d.get("selection") or {}
    if not d.get("apply"):
        plan = hostvars_service.build_plan(key, host, selection)
        if plan.get("error"):
            return jsonify({"error": plan["error"], "detail": plan.get("detail")}), 422
        return jsonify({"applied": False, "key": key, "host": host, "paths": plan["paths"], "enact": plan["enact"],
                        "changes": plan.get("changes") or [], "will_overwrite": plan.get("will_overwrite") or [],
                        "severity": plan.get("severity"), "token": plan["plan_token"]})
    run_id = uuid.uuid4().hex[:12]
    fields = ",".join(sorted((selection.get("values") or {}).keys())) or "none"
    _audit("host-promote", "key=%s host=%s fields=%s run_id=%s" % (key, host, fields, run_id))
    out = _reconfig.run_promote(hostvars_service.build_plan, hostvars_service.apply_plan,
                                d.get("token"), key, host, selection)
    if out.get("error") == "drift":
        return jsonify({"error": "plan drifted since propose; re-propose",
                        "changes": out.get("changes") or []}), 409
    if out.get("error") == "would_drop":
        return jsonify({"error": "this change would drop a host var another edit set — re-propose",
                        "changes": out.get("changes") or []}), 409
    if out.get("error"):
        return jsonify({"error": out["error"], "detail": out.get("detail")}), 422
    if not out["changed"]:
        _audit("host-result", "key=%s host=%s changed=no run_id=%s" % (key, host, run_id))
        return jsonify({"applied": True, "changed": False, "note": "no change; nothing to commit"})
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(inventory): reconfigure %s host %s via the GUI" % (key, host),
         "Host-var edit (run_id %s). Data-only write of the banner-owned drop-in; the operator verifies "
         "reachability + re-runs as needed (the enact hints)." % run_id],
        push_origin=bool(d.get("push")), run_id=run_id)   # staging GUI ⇒ proposed/<run_id> (C10)
    _audit("host-result", "key=%s host=%s severity=%s changed=%s paths=%d committed=%s staged=%s run_id=%s" % (
        key, host, (out["plan"].get("severity") or "modify"), True, len(out.get("paths") or []),
        git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing staged (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": git["committed"], "paths": out["paths"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "enact": out["plan"]["enact"]})


# --- platform-settings EDIT (Phase 5, #140): STAGE a change to a tracked instance.yml / fleet.yml knob. The
# highest-blast GUI write (a mgmt_ip re-IP severs this very listener) — so it rides the SAME spine + the
# severity gate (mgmt_ip type-to-confirm / tls_mode+domain redeploy / fleet removal remove-confirm) and STAGES
# proposed/<run_id> (C10, never main). api_privileged + docker/.env are NOT here — categorically FORBID.
def _settings_promote(build_fn, audit_label, audit_detail, run_id, *build_args):
    """Shared promote tail for the two settings routes: audit (NAMES + run_id, server-derived severity), run the
    token→no_drop→apply gate, map drift/would_drop to 409+diff, and STAGE proposed/<run_id>. Returns a Flask
    (json, status) tuple. The change PATHS/values here are non-secret platform config (mgmt_ip/domain/module),
    auditable; no secret value is read or written (C11)."""
    _audit("%s-promote" % audit_label, "%s run_id=%s" % (audit_detail, run_id))
    body = request.get_json(silent=True) or {}              # parity with the rest of app.py — never .get on None
    out = _reconfig.run_promote(build_fn, settings_service.apply_plan, body.get("token"), *build_args)
    if out.get("error") == "drift":
        return jsonify({"error": "plan drifted since propose; re-propose", "changes": out.get("changes") or []}), 409
    if out.get("error") == "would_drop":
        return jsonify({"error": "this change would drop a co-owner key — re-propose",
                        "changes": out.get("changes") or []}), 409
    if out.get("error"):
        return jsonify({"error": out["error"], "detail": out.get("detail")}), 422
    if not out["changed"]:
        _audit("%s-result" % audit_label, "%s changed=no run_id=%s" % (audit_detail, run_id))
        return jsonify({"applied": True, "changed": False, "note": "no change; nothing to commit"})
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(settings): %s via the GUI" % audit_detail,
         "Platform-settings edit (run_id %s). Data-only stage of a tracked instance/fleet knob; the operator "
         "promotes + re-runs deploy-stack to take effect (the enact hints)." % run_id],
        push_origin=bool(body.get("push")), run_id=run_id)
    _audit("%s-result" % audit_label, "%s severity=%s changed=%s committed=%s staged=%s run_id=%s" % (
        audit_detail, (out["plan"].get("severity") or "modify"), True, git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing staged (repo may be dirty)", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": git["committed"], "paths": out["paths"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "enact": out["plan"]["enact"]})


@app.route("/api/settings/<knob>", methods=["POST"])
@require_auth
def api_settings_edit(knob):
    """PROPOSE (apply:false — the pure plan + token + diff + consequence) or PROMOTE (apply:true — the token-gated
    + no-drop-gated instance.yml write, C10 stage) of a platform knob (mgmt_ip/domain/tls_mode/backup_remotes).
    The value rides the standard `selection.values[<knob>]` shape so the shared reconfigure dialog drives it."""
    d = request.get_json(force=True) or {}
    value = ((d.get("selection") or {}).get("values") or {}).get(knob)
    if not d.get("apply"):
        plan = settings_service.build_plan(knob, value)
        if plan.get("error"):
            return jsonify({"error": plan["error"], "detail": plan.get("detail")}), \
                (404 if plan["error"] in ("unknown_knob", "no_instance") else 422)
        return jsonify({"applied": False, "knob": knob, "paths": plan["paths"], "enact": plan["enact"],
                        "changes": plan.get("changes") or [], "will_overwrite": plan.get("will_overwrite") or [],
                        "severity": plan.get("severity"), "consequence": plan.get("consequence") or "",
                        "token": plan["plan_token"]})
    run_id = uuid.uuid4().hex[:12]
    return _settings_promote(settings_service.build_plan, "settings",
                             "knob=%s" % knob, run_id, knob, value)


@app.route("/api/settings/fleet/<module>", methods=["POST"])
@require_auth
def api_settings_fleet_disable(module):
    """PROPOSE / PROMOTE a fleet REMOVAL (disable `module` in fleet.yml) — severity `remove` (drops monitoring/log
    pipelines for live hosts). The removal is computed server-side from the URL; the selection is ignored."""
    d = request.get_json(force=True) or {}
    if not d.get("apply"):
        plan = settings_service.build_fleet_disable_plan(module)
        if plan.get("error"):
            return jsonify({"error": plan["error"], "detail": plan.get("detail")}), \
                (404 if plan["error"] in ("unknown_knob", "no_fleet", "not_enabled") else 422)
        return jsonify({"applied": False, "module": module, "paths": plan["paths"], "enact": plan["enact"],
                        "changes": plan.get("changes") or [], "will_overwrite": plan.get("will_overwrite") or [],
                        "severity": plan.get("severity"), "consequence": plan.get("consequence") or "",
                        "token": plan["plan_token"]})
    run_id = uuid.uuid4().hex[:12]
    return _settings_promote(settings_service.build_fleet_disable_plan, "settings-fleet",
                             "module=%s" % module, run_id, module)


# --- secret-onboarding dialog (D): guided entry of a SOPS domain's service/infra secrets ----------------
# ONE shared dialog for every domain — `domain` is data, not a branch (the secret analogue of the capability
# dialog). The values are encrypted into SOPS in-process (the GUI holds the age key) + the write STAGES
# proposed/<run_id> (C10). Values are NEVER returned, logged, or committed in plaintext — the audit + the
# dialog only ever carry field NAMES. See secret-forms/README.md + SECURITY.md.
@app.route("/api/secrets")
@require_auth
def api_secrets_list():
    """The registered secret-onboarding domains (label + description) for the picker — read-only, NAMES only."""
    return jsonify([{"domain": f["domain"], "label": f.get("label", f["domain"]),
                     "description": f.get("description", "")} for f in _catalog.load_secret_forms()])


@app.route("/api/secrets/<domain>/fields")
@require_auth
def api_secret_fields(domain):
    """A domain's form fields + already-set flags (NAMES only, never a value). 404 if no descriptor exists."""
    out = secrets_service.offerable_fields(domain)
    if out["error"] == "no_form":
        return jsonify({"error": "no secret form for domain: %s" % domain}), 404
    return jsonify(out)


@app.route("/api/secrets/<domain>", methods=["POST"])
@require_auth
def api_secret_apply(domain):
    """Propose (apply:false → the view, no values) or apply (encrypt the values into SOPS, commit + STAGE
    proposed/<run_id>). In-process — the GUI holds the age key (mirrors api/routes/secrets.py). The action +
    field NAMES are audited; the values NEVER are."""
    if _catalog.secret_form(domain) is None:
        return jsonify({"error": "no secret form for domain: %s" % domain}), 404
    d = request.get_json(force=True) or {}
    plan = secrets_service.build_secret_plan(domain, d.get("values") or {}, d.get("regenerate") or [])
    if (plan.get("error") or "").startswith("missing:"):
        return jsonify({"error": "required field not provided: %s" % plan["error"].split(":", 1)[1]}), 422
    if plan["error"]:
        return jsonify({"error": plan["error"]}), 422
    run_id = uuid.uuid4().hex[:12]
    overwrite = secrets_service.overwrite_set(domain, plan)   # NAMES of already-set fields this would clobber (C9)
    if not d.get("apply"):
        return jsonify({"applied": False, "domain": domain, "view": plan["view"], "overwrite": overwrite,
                        "run_id": run_id})
    if overwrite and not d.get("overwrite"):                  # fail CLOSED: clobbering a live secret needs an ack
        return jsonify({"error": "overwrite confirm required for: %s" % ",".join(overwrite),
                        "overwrite": overwrite, "run_id": run_id}), 409
    names = [v["key"] for v in plan["view"] if v["source"] in ("provided", "generated")]
    _audit("secret-apply", "domain=%s fields=%s overwrite=%s run_id=%s" % (
        domain, ",".join(names) or "none", ",".join(overwrite) or "none", run_id))
    applied = secrets_service.apply_secret_plan(plan)
    if applied["error"]:
        _audit("secret-result", "domain=%s error=%s run_id=%s" % (domain, applied["error"], run_id))
        return jsonify({"error": "secret write failed: %s (is an age key present to encrypt?)" % applied["error"],
                        "run_id": run_id}), 500
    if not applied["changed"]:
        _audit("secret-result", "domain=%s changed=no run_id=%s" % (domain, run_id))
        return jsonify({"applied": True, "changed": False, "committed": False, "domain": domain,
                        "view": plan["view"], "run_id": run_id, "note": "nothing changed; nothing to commit"})
    git = gitio.commit_and_push(
        applied["paths"],
        ["feat(secrets): set %s via the secret-onboarding dialog" % domain,
         "Set %d field(s) on %s via the kontroll onboarding GUI (run_id %s). SOPS ciphertext only; values "
         "never logged." % (len(names), domain, run_id)],
        push_origin=bool(d.get("push")), run_id=run_id)   # staging GUI ⇒ proposed/<run_id> (C10)
    _audit("secret-result", "domain=%s committed=%s staged=%s run_id=%s" % (
        domain, git["committed"], git["staged"], run_id))
    if not git["committed"]:
        return jsonify({"error": "commit failed; nothing pushed", "run_id": run_id}), 500
    return jsonify({"applied": True, "changed": True, "committed": True, "domain": domain, "view": plan["view"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "enact": secrets_service.actuation_enact_commands(domain, names),   # post-promote hand-off
                    "next": ("promote the proposal: `kontroll promote %s`" % run_id) if git["staged"]
                            else "committed to main"})


# --- guided age key-generation (D): mint break-glass / scoped-Semaphore / control-rotation keys ----------
# ONE shared dialog for every role — `role` is data, not a branch (the keygen analogue of the secret dialog).
# The crown jewel: the minted PRIVATE key is returned EXACTLY ONCE (this surface's POST response) and is NEVER
# logged, audited, or committed — the audit + commit carry only the PUBLIC recipient. The only repo write is
# the additive /.sops.yaml recipient add, which STAGES proposed/<run_id> (C10). The decrypt-needing re-wrap
# (`sops updatekeys`) is a deferred operator step, never run here. See key-roles/README.md + SECURITY.md (C11).
@app.route("/api/keygen")
@require_auth
def api_keygen_list():
    """The registered keygen roles (label + scope) for the picker — read-only, no key material."""
    return jsonify([{"role": r["role"], "label": r.get("label", r["role"]),
                     "description": r.get("description", ""), "scope_label": r.get("scope_label", "")}
                    for r in _catalog.load_key_roles()])


@app.route("/api/keygen/<role>")
@require_auth
def api_keygen_plan(role):
    """A role's plan (scope, placement, the domains the public key will join, the deferred re-wrap step) —
    read-only, generates NOTHING. 404 if no descriptor exists."""
    plan = keygen_service.build_keygen_plan(role)
    if plan["error"] == "no_role":
        return jsonify({"error": "no such key role: %s" % role}), 404
    return jsonify(plan)


@app.route("/api/keygen/<role>", methods=["POST"])
@require_auth
def api_keygen_apply(role):
    """Mint `role`'s keypair, add the PUBLIC recipient to /.sops.yaml (additive, parse-verified), commit +
    STAGE proposed/<run_id>. Returns the show-once `private_key` ONCE; the audit + commit record only the
    role + PUBLIC key. 404 unknown role; 500 if keygen/verify fails (nothing written)."""
    if _catalog.key_role(role) is None:
        return jsonify({"error": "no such key role: %s" % role}), 404
    run_id = uuid.uuid4().hex[:12]
    res = keygen_service.apply_keygen_plan(role)
    if res["error"] == "no_role":
        return jsonify({"error": "no such key role: %s" % role}), 404
    if res["error"]:
        # NEVER include the private key in an error path; record the failure (no key) for audit.
        _audit("keygen-result", "role=%s error=%s run_id=%s" % (role, res["error"], run_id))
        return jsonify({"error": "keygen failed: %s" % res["error"], "run_id": run_id}), 500
    # Audit + commit carry the PUBLIC key only — the private key is never written anywhere but the response.
    _audit("keygen-apply", "role=%s public=%s run_id=%s" % (role, res["public_key"], run_id))
    git = {"committed": False, "staged": False, "target_ref": None, "pushed_canonical": False}
    if res["changed"]:
        git = gitio.commit_and_push(
            res["paths"],
            ["feat(keygen): add the %s key as a /.sops.yaml recipient" % role,
             "Minted via the kontroll onboarding GUI (run_id %s). Public recipient %s added to %s; the private "
             "key is shown once and never stored. Re-wrap with `%s` after promoting." % (
                 run_id, res["public_key"], ", ".join(res["recipient_domains"]) or "the configured groups",
                 res["sops_updatekeys"])],
            push_origin=bool((request.get_json(silent=True) or {}).get("push")), run_id=run_id)
        _audit("keygen-result", "role=%s committed=%s staged=%s run_id=%s" % (
            role, git["committed"], git["staged"], run_id))
        if not git["committed"]:
            return jsonify({"error": "recipient added but commit failed; nothing pushed", "run_id": run_id,
                            "private_key": res["private_key"]}), 500
    # The ONE place the private key appears — the operator must save it now (it is never recoverable).
    return jsonify({"role": role, "changed": res["changed"], "committed": git["committed"],
                    "private_key": res["private_key"], "public_key": res["public_key"],
                    "placement": res["placement"], "private_key_path": res["private_key_path"],
                    "recipient_domains": res["recipient_domains"], "high_blast": res["high_blast"],
                    "sops_updatekeys": res["sops_updatekeys"], "next_steps": res["next_steps"],
                    "target_ref": git["target_ref"], "staged": git["staged"], "run_id": run_id,
                    "note": None if res["changed"] else "public key was already a recipient; nothing staged"})


if __name__ == "__main__":
    import sys
    # Fail-closed: a privileged surface never runs unauthenticated.
    if not GUI_PASSWORD:
        sys.exit("refusing to start: GUI_PASSWORD is unset (this surface can write the "
                 "repo, encrypt secrets, push, and run Ansible). Set GUI_USER/GUI_PASSWORD "
                 "(sourced from SOPS at deploy).")
    # TLS: serve HTTPS when a cert+key are provided (recommended — the surface is
    # privileged). Falls back to HTTP with a loud warning, for local dev only.
    cert, key = os.environ.get("GUI_TLS_CERT"), os.environ.get("GUI_TLS_KEY")
    ssl_context = (cert, key) if cert and key else None
    if ssl_context is None:
        print("WARNING: no GUI_TLS_CERT/GUI_TLS_KEY set — serving HTTP (dev only). "
              "Provide a cert+key (self-signed is fine on the mgmt network).", file=sys.stderr)
    app.run(host=os.environ.get("GUI_BIND", "127.0.0.1"),
            port=int(os.environ.get("GUI_PORT", "8080")),
            ssl_context=ssl_context)
