#!/usr/bin/env python3
"""Capability-aware onboarding search over Ansible Galaxy + local content.

One search bar across Galaxy and what we already have locally. Each result is a
structured RECORD: top-level metadata + a `capabilities` block keyed by the
drop-in capability VECTORS (vectors/*.yml). See docs/capability-matrix.md.

This file is the THIN CLI: argparse + cmd_* formatters that call the kontroll service
layer (scripts/kontroll/ — the pure logic, probers, sources, and repo-mutators) and
render its structured return value. The planned API (docs/api-architecture.md) calls the
same service functions directly. For back-compat the package's public functions are
re-exported here, so `import galaxy; galaxy.classify(...)` still resolves; the I/O SEAMS
the tests monkeypatch now live in their home modules (kontroll.probe / kontroll.catalog /
kontroll.gitio / kontroll.paths) — patch there.

Commands:
  search <keywords...> [--origin both|local|galaxy] [--limit N]
                       [--vector backup ...]   (filter: must HAVE these; implies --deep)
                       [--deep] [--json]        (--deep: full probe; default is fast shallow)
  probe  <namespace.collection> [--json]       deep-probe one collection
  classify <ns.collection>                     which execution backend fits
  scaffold/onboard ...                         emit a device-class / stand a host up
  openapi <url|file> [--name N] [--emit]       derive an `api` backend recipe from
                                                an OpenAPI/Swagger spec (the API tail)
  refresh                                       deep-probe every installed
                                                collection -> capability-matrix.generated.json
  audit                                         declared (enabled modules) vs installed
  capture-exception add --subject S --match G   declare a misbehaving capture in the sparse
                       [--commit] [--push]       matrix (config/capture-exceptions.yml)
  verify-parity <keywords...> [--limit N]       check the fast SHALLOW search against the full
                       [--json]                  DEEP probe (shallow ⊑ deep; unsound must be 0)

Probe depth: local is SHALLOW by default (files only, no ansible-doc — fast); --deep (or a
--vector filter) re-probes matches DEEPLY (ansible-doc -j, full signals). Galaxy is always
SHALLOW (the `contents` list). A vector unconfirmable at the current depth shows `?` ('maybe')
— resolve it with `galaxy.py probe <ns.coll>`. `overrides/` can correct/annotate any record.
"""
import argparse
import json
import os
import sys

from kontroll import catalog, gitio, paths
from kontroll.paths import CROSS, MARKS, QMARK, TICK
# Re-exports for call-style importers (back-compat). The monkeypatch SEAMS are NOT here —
# patch them in their home modules (kontroll.probe / kontroll.catalog / kontroll.gitio).
from kontroll.predicate import classify, eval_pred, eval_vector, pred_evidence  # noqa: F401
from kontroll.record import build_record  # noqa: F401
from kontroll.catalog import load_backends, load_overrides, load_vectors  # noqa: F401
from kontroll.probe import _facts  # noqa: F401
from kontroll.service.audit import service_audit
from kontroll.service.capture_exception import add_capture_exception
from kontroll.service.classify import service_classify
from kontroll.service.onboard import (  # noqa: F401
    apply_onboard_plan, build_onboard_plan, enable_in_fleet, no_backend_message)
from kontroll.service.openapi import (  # noqa: F401
    build_openapi_recipe, classify_endpoints, derive_auth, derive_server, fetch_spec)
from kontroll.service.probe import service_probe
from kontroll.service.refresh import service_refresh
from kontroll.service.scaffold import build_scaffold
from kontroll.service.search import service_search, service_search_parity


# --------------------------------------------------------------------------- #
# Rendering (a thin view over records) — presentation stays in the CLI
# --------------------------------------------------------------------------- #
def cap_cells(rec, vectors):
    cells = []
    for v in vectors:
        c = rec["capabilities"][v["name"]]
        conf = "(%s)" % c["confidence"] if c.get("confidence") else ""
        star = "*" if c.get("provenance") == "override" else ""
        cells.append("%s:%s%s%s" % (v["symbol"], MARKS[c["state"]], conf, star))
    return "  ".join(cells)


def render_record(rec, vectors):
    badge = " certified" if rec["meta"].get("certified") else ""
    if rec.get("suggested_backend"):
        badge += " →%s" % rec["suggested_backend"]
    print("  %-34s %-16s v%-10s %s%s" % (
        rec["collection"], "[%s/%s]" % (rec["origin"], rec["depth"]),
        rec["version"] or "?", cap_cells(rec, vectors), badge))
    note = rec["meta"].get("note")
    desc = note or (rec["meta"].get("description") or "")[:54]
    if desc:
        print("  %-34s %s%s" % ("", "↳ " if note else "", desc))


# --------------------------------------------------------------------------- #
# Commands — thin formatters over the service layer
# --------------------------------------------------------------------------- #
def cmd_search(args):
    vectors = catalog.load_vectors()
    records = service_search(args.keywords, args.origin, args.limit, args.vector,
                             deep=args.deep, vectors=vectors)
    if args.json:
        print(json.dumps(records, indent=2))
        return
    hint = "deep ✓" if (args.deep or args.vector) else "shallow — add --deep or `probe <ns.coll>` for detail"
    print("\n%d result(s)  [%s yes / %s no / %s unknown-at-depth | * = override | local probe: %s]\n" % (
        len(records), TICK, CROSS, QMARK, hint))
    for r in records:
        render_record(r, vectors)
    print()


def cmd_verify_parity(args):
    """Verify the fast SHALLOW search against the full DEEP probe over the same matched collections —
    the repeatable 'is fast search accurate vs the lengthy one' check. Prints each disagreeing cell and a
    summary; exits non-zero if any cell is UNSOUND (a yes↔no flip — shallow must only ever defer to '?',
    never contradict deep). Run live where ansible-doc + the collections are present."""
    res = service_search_parity(args.keywords, args.limit)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        s = res["summary"]
        print("\nparity: %d collection(s), %d cell(s) — %d agree, %d deferred (shallow '?'→deep), %d UNSOUND\n"
              % (s["checked"], s["cells"], s["agree"], s["deferred"], s["unsound"]))
        for row in res["collections"]:
            if row["verdict"] != "agree":
                print("  %-34s %-10s shallow=%-6s deep=%-6s  %s"
                      % (row["collection"], row["vector"], row["shallow"], row["deep"],
                         "DEFERRED" if row["verdict"] == "deferred" else "!! UNSOUND !!"))
        print("\n  %s (shallow ⊑ deep)\n" % ("OK — no contradictions" if s["unsound"] == 0
                                             else "FAIL — shallow contradicts deep"))
    sys.exit(1 if res["summary"]["unsound"] else 0)


def cmd_probe(args):
    vectors = catalog.load_vectors()
    result = service_probe(args.collection, vectors=vectors)
    if result is None:
        sys.exit("'%s' not installed locally. Install: ansible-galaxy collection install %s"
                 % (args.collection, args.collection))
    f, rec = result["facts"], result["record"]
    if args.json:
        print(json.dumps(rec, indent=2))
        return
    print()
    render_record(rec, vectors)
    print("\n  modules: %s" % ", ".join(sorted(f["modules"])[:20]))
    print("  plugins: %s" % (", ".join("%s=%s" % (k, v) for k, v in f["plugins"].items()) or "none"))
    for name, cap in rec["capabilities"].items():
        if cap.get("evidence"):
            print("  %s evidence: %s" % (name, cap["evidence"]))
    print()


def cmd_refresh(args):
    result = service_refresh()
    print("wrote %d records -> %s" % (result["count"], os.path.relpath(result["path"], paths.ROOT)))


def cmd_audit(args):
    result = service_audit()
    installed = result["installed"]
    print("\nenabled modules: %s" % ", ".join(result["enabled"]))
    print("declared collections (from enabled modules): %d" % len(result["declared"]))
    for c in result["declared"]:
        state = "installed %s" % installed[c] if c in installed else "MISSING"
        print("  %-28s %s" % (c, state))
    print()


def cmd_classify(args):
    result = service_classify(args.collection)
    if result is None:
        sys.exit("'%s' not installed locally." % args.collection)
    matches = result["matches"]
    print("\n  %s  ->  backend(s): %s" % (
        args.collection,
        " > ".join(matches) or "none — no backend fits; pass --backend raw_ssh only if the target is an SSH host"))
    if matches:
        print("  best: %-14s requires: %s" % (
            result["best"] or "none it can run (no match declares a role)", ", ".join(result["requires"]) or "nothing"))
    print()


# What a `no_backend` answer tells a CLI caller to do (S24: raw_ssh is never chosen for them).
_NO_BACKEND_REMEDY = "pass --backend raw_ssh only if the target is an SSH host, or add a backend"


def cmd_scaffold(args):
    res = build_scaffold(args.collection, args.key, args.group, args.secrets or None, args.backend)
    if res["error"] == "not_installed":
        sys.exit("install the collection first: ansible-galaxy collection install %s" % args.collection)
    if res["error"] == "no_backend":
        sys.exit(no_backend_message(res, args.collection, _NO_BACKEND_REMEDY))
    rel, text = res["path"], res["text"]
    abspath = paths.module_write_path(args.key)   # under write_root(), confined — where every other module write goes
    existing = paths.module_file(args.key)        # the READER's view: the clone's copy, else the shipped one —
    if args.dry_run:                              # a stub written beside a shipped class would shadow it (#124)
        print("\n# --- would write %s ---\n" % rel + text)
        if os.path.exists(existing):
            print("# (a real run refuses: the %s device class already exists)" % args.key)
    else:
        if os.path.exists(existing):
            sys.exit("refusing to overwrite %s" % existing)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote %s" % rel)
    print("Next: enable '%s' in instance/fleet.yml, add host(s) to inventory with the "
          "backend params, then run ping.yml / backup-configs.yml against it once." % args.key)


def cmd_onboard(args):
    """The operator's path: collection + host -> a managed device. Dry-run prints
    the plan; --apply performs the repo mutations (module + drop-in inventory host
    + fleet-enable); --commit commits them; --bootstrap installs the collection and
    live-verifies the host. Creds + the final push stay deliberate operator steps
    (the security gate — nothing plaintext or canonical happens automatically)."""
    apply = args.apply or args.commit or args.bootstrap or args.push
    if args.push and not args.commit:
        args.commit = True              # push needs a commit to push
    plan = build_onboard_plan(args.collection, args.key, args.group, args.host,
                              host_name=args.host_name, secrets=args.secrets or None, backend=args.backend,
                              username=args.username, password=args.password, api_token=args.api_token)
    if plan["error"] == "not_installed":
        sys.exit("install the collection first: ansible-galaxy collection install %s" % args.collection)
    if plan["error"] == "no_backend":
        sys.exit(no_backend_message(plan, args.collection, _NO_BACKEND_REMEDY))
    backend, host_name = plan["backend"], plan["host_name"]
    creds_to_set = plan["creds_to_set"]
    mod_path, inv_path = plan["mod_path"], plan["inv_path"]

    print("\n=== ONBOARD %s as device class '%s' via backend '%s' ===" % (args.collection, args.key, backend))
    print("(%s)\n" % ("--apply: mutating the repo" if apply else "dry-run — writes nothing; re-run with --apply"))
    if plan.get("module_reuse"):
        print("[1] Device-class '%s' ALREADY EXISTS for %s — REUSING it (curated module kept; adding host only).\n"
              "    %s\n" % (args.key, args.collection, mod_path))
    else:
        print("[1] Device-class declaration -> %s\n%s" % (mod_path, plan["module_text"]))
    print("[2] Drop-in inventory host -> %s\n%s" % (inv_path, plan["host_text"]))
    print("[3] Enable the class -> instance/fleet.yml enabled_modules += %s\n" % args.key)
    if creds_to_set:
        print("[4] Credentials -> encrypted into instance/secrets/%s.sops.yml as %s" % (
            plan["secrets"], ", ".join(sorted(creds_to_set))))   # the plan's domain: a reuse ADOPTS the class's
        print("    (host carries inline SOPS lookups — self-contained, no group_vars needed)\n")
    else:
        print("[4] Credentials: none passed. The host has connection vars but no login —")
        print("    pass --username/--password (SSH) or --api-token, or add them later with sops.\n")

    if not apply:
        print("[5] Then: --apply [--commit] [--push] [--bootstrap] does 1-4 + commit/push/verify.")
        print("    Once applied + landed + bootstrapped, %s is live in ping/backup + Semaphore.\n" % host_name)
        return

    print("--- applying ---")
    result = apply_onboard_plan(plan)
    changed, plan_paths = result["changed"], result["paths"]

    if args.commit and changed:
        gitio._run(["git", "add"] + plan_paths)
        crc = gitio._run(["git", "commit"] + gitio.commit_message_args([   # + the one configurable trailer
            "feat(onboard): %s -> %s via %s backend" % (args.collection, args.key, backend),
            "Machine-onboarded by scripts/galaxy.py onboard. Host %s in group %s; "
            "run ping.yml --limit %s to verify." % (host_name, plan["group"], host_name)]))
        if crc != 0:                                # never push a stale HEAD on a failed commit
            sys.exit("  ! commit failed (rc=%d) — not pushing; nothing took effect." % crc)
        # Land on the LOCAL CANONICAL (the instance source of truth) so Semaphore sees it — offline, no GitHub
        # needed (docs/local-source-of-truth.md). A LANDING, not a push: judged by the promote gate first (a
        # fleet file this onboard just wrote is judged as a proposal's would be), then a pack on a pipe and one
        # compare-and-swap (SECURITY.md C10). rc 2 = refused, the gate's sentence was printed above it.
        lrc = gitio.land_main()
        print("  landed on the local canonical — Semaphore will see %s next job." % host_name if lrc == 0
              else "  ! the landing on the local canonical did not happen (rc=%d) — read the line above; a refusal "
                   "(rc 2) names what to correct in the checkout, rc 1 what is missing (playbooks/local-canonical.yml "
                   "sets the 'local' remote)." % lrc)
        if args.push:                       # OPTIONAL offsite backup, not required to operate
            grc = gitio._run(["git", "push", "origin", "HEAD"])
            print("  also pushed to origin (offsite backup)." if grc == 0
                  else "  ! origin push failed (rc=%d) — offsite backup skipped; the instance is unaffected." % grc)
    elif args.commit:
        print("  nothing changed; nothing to commit.")

    if args.bootstrap:
        print("--- bootstrap (install the new collection on the control node) ---")
        rc = gitio._run(["ansible-playbook", "playbooks/bootstrap.yml"], cwd=os.path.join(paths.ROOT, "ansible"))
        if rc == 0:
            print("--- verify (ping the onboarded host; needs its creds present) ---")
            vrc = gitio._run(["ansible-playbook", "playbooks/ping.yml", "--limit", host_name],
                             cwd=os.path.join(paths.ROOT, "ansible"))
            print("\n%s %s — %s" % (
                "OK" if vrc == 0 else "PENDING", host_name,
                "reachable & managed." if vrc == 0
                else "not yet reachable — provide creds in the SOPS domain, then re-run ping --limit %s." % host_name))
        else:
            print("  bootstrap failed (rc=%d) — fix before verifying." % rc)

    done = [s for s, on in [("applied", True), ("creds-encrypted", bool(creds_to_set)),
            ("committed→local-canonical", args.commit), ("offsite-backup", args.push),
            ("bootstrapped", args.bootstrap)] if on]
    todo = []
    if not creds_to_set:
        todo.append("creds (--username/--password or --api-token, or sops by hand)")
    if args.commit and not args.push:
        todo.append("optional: --push for an offsite GitHub backup (not needed to operate)")
    print("\n%s done: %s.%s\n%s is live in ping/backup + the nightly Semaphore schedule.\n" % (
        host_name, ", ".join(done), ("  Remaining: " + "; ".join(todo) + "." if todo else " Fully onboarded."),
        host_name))


def cmd_openapi(args):
    spec = fetch_spec(args.spec)
    result = build_openapi_recipe(spec, args.name, args.port)
    name, text, s = result["name"], result["text"], result["summary"]

    print("\n=== OpenAPI -> api recipe: %s ===" % name)
    print("  title    : %s  v%s" % (s["title"] or "?", s["version"] or "?"))
    print("  server   : port %s  base '%s'" % (s["port"], s["base_path"] or "/"))
    print("  auth     : %s%s" % (s["auth"]["type"],
          "  [!] %s" % s["auth"]["_unverified"] if "_unverified" in s["auth"] else ""))
    print("  actuate  : %s  (write endpoints present)" % (TICK if s["actuate"] else CROSS))
    print("  backup   : %s  %s" % (TICK if s["backup"] else CROSS,
          "%s %s" % (s["backup"]["method"], s["backup"]["path"]) if s["backup"]
          else "no backup/export endpoint matched — actuate-only, or add via overrides/"))
    print("  check    : %s  %s" % (TICK if s["check"] else QMARK,
          s["check"]["path"] if s["check"] else "none matched — recipe will skip liveness"))

    path = os.path.join(paths.RECIPES_DIR, "%s.yml" % name)
    if args.emit:
        if os.path.exists(path):
            sys.exit("refusing to overwrite %s" % os.path.relpath(path, paths.ROOT))
        os.makedirs(paths.RECIPES_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("\n  + wrote %s" % os.path.relpath(path, paths.ROOT))
        print("  Next: onboard a host with --backend api, wire %s in the relevant SOPS"
              % s["auth"].get("token_var", "the cred var"))
        print("  domain + group_vars, then live-verify (ping/backup).")
    else:
        print("\n# --- would write %s (re-run with --emit) ---\n%s"
              % (os.path.relpath(path, paths.ROOT), text))


def cmd_capture_exception_add(args):
    """Declare a misbehaving capture in the sparse matrix. Dry-run prints the plan;
    --commit appends + commits + LANDS on the LOCAL CANONICAL (judged by the promote gate; never
    a push) so it takes effect on the next backup; --push also backs it up to origin. Mirrors onboard's commit/land gate —
    this is the CLI foundation the GUI calls to save an exception to git."""
    if args.push and not args.commit:
        args.commit = True                                  # push needs a commit to push
    print("\n=== capture-exception add: %s (match %r) ===" % (args.subject, args.match))
    print("(%s)\n" % ("--commit: applying" if args.commit
                      else "dry-run — writes nothing; re-run with --commit"))
    print("[1] entry -> config/capture-exceptions.yml")
    print("    subject=%s  match=%s  behavior=%s  disposition=%s  source=%s"
          % (args.subject, args.match, args.behavior, args.disposition, args.source))
    if args.observed:
        print("    observed=%s" % (args.observed[:70] + ("…" if len(args.observed) > 70 else "")))
    if not args.commit:
        print("\n[2] Then: --commit appends + commits + lands on the local canonical (judged by the promote gate) "
              "(--push also backs up to origin). Effective on the next backup run.\n")
        return
    print("\n--- applying ---")
    if not add_capture_exception(args.subject, args.match, args.behavior,
                                 args.disposition, args.observed, args.source):
        print("  nothing changed; nothing to commit.\n")
        return
    gitio._run(["git", "add", os.path.join("config", "capture-exceptions.yml")])
    crc = gitio._run(["git", "commit"] + gitio.commit_message_args([       # + the one configurable trailer
        "feat(logging): capture-exception += %s" % args.match,
        "Operator-added capture-exception for %s (match %s; behavior %s; disposition %s). "
        "Excluded captures stay on disk but out of the git history." % (
            args.subject, args.match, args.behavior, args.disposition)]))
    if crc != 0:                                    # never push a stale HEAD on a failed commit
        print("  ! commit failed (rc=%d) — not pushing; nothing took effect." % crc)
        return
    lrc = gitio.land_main()                             # a landing, judged first — never a push (SECURITY.md C10)
    print("  landed on the local canonical — effective on the next backup run." if lrc == 0
          else "  ! the landing on the local canonical did not happen (rc=%d) — read the line above (rc 2: the gate "
               "refused the tree; rc 1: playbooks/local-canonical.yml sets the 'local' remote)." % lrc)
    if args.push:
        grc = gitio._run(["git", "push", "origin", "HEAD"])
        print("  also pushed to origin (offsite backup)." if grc == 0
              else "  ! origin push failed (rc=%d) — the instance is unaffected." % grc)
    print("\n%s done.\n" % args.match)


def main():
    ap = argparse.ArgumentParser(description="Capability-aware Galaxy + local search")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("keywords", nargs="+")
    s.add_argument("--origin", choices=["both", "local", "galaxy"], default="both")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--vector", action="append", default=[])
    s.add_argument("--deep", action="store_true",
                   help="deep-probe local matches (slower; resolves '?' cells). Implied by --vector.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)
    vp = sub.add_parser("verify-parity")
    vp.add_argument("keywords", nargs="+")
    vp.add_argument("--limit", type=int, default=8)
    vp.add_argument("--json", action="store_true")
    vp.set_defaults(func=cmd_verify_parity)
    p = sub.add_parser("probe")
    p.add_argument("collection")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_probe)
    sub.add_parser("refresh").set_defaults(func=cmd_refresh)
    sub.add_parser("audit").set_defaults(func=cmd_audit)
    c = sub.add_parser("classify")
    c.add_argument("collection")
    c.set_defaults(func=cmd_classify)
    sc = sub.add_parser("scaffold")
    sc.add_argument("collection")
    sc.add_argument("--key", required=True, help="device-class key (modules/<key>/)")
    sc.add_argument("--group", required=True, help="inventory group it feeds")
    sc.add_argument("--secrets", help="secrets domain (default: the backend's default_secrets_domain)")
    sc.add_argument("--backend", help="override the auto-chosen backend")
    sc.add_argument("--dry-run", action="store_true")
    sc.set_defaults(func=cmd_scaffold)
    ob = sub.add_parser("onboard")
    ob.add_argument("collection")
    ob.add_argument("--key", required=True, help="device-class key")
    ob.add_argument("--group", required=True, help="inventory group")
    ob.add_argument("--host", required=True, help="the host's mgmt IP/name")
    ob.add_argument("--host-name", help="inventory hostname (default: <key>-1)")
    ob.add_argument("--secrets", help="secrets domain for a new class (default: the backend's "
                    "default_secrets_domain); a second device of a class stores in the class's own")
    ob.add_argument("--backend", help="override the auto-chosen backend")
    ob.add_argument("--username", help="login user (SSH) — encrypted into the secrets domain, inline on the host")
    ob.add_argument("--password", help="login password (SSH) — encrypted into the secrets domain")
    ob.add_argument("--api-token", dest="api_token", help="API token — encrypted into the secrets domain")
    ob.add_argument("--apply", action="store_true", help="write module + drop-in inventory host + fleet-enable + creds")
    ob.add_argument("--commit", action="store_true", help="git-commit the applied edits (implies --apply)")
    ob.add_argument("--push", action="store_true",
                    help="also push to origin (OPTIONAL offsite backup; the instance is already canonical via the local repo)")
    ob.add_argument("--bootstrap", action="store_true",
                    help="install the collection + live-verify the host (implies --apply)")
    ob.set_defaults(func=cmd_onboard)
    oa = sub.add_parser("openapi", help="derive an api backend recipe from an OpenAPI/Swagger spec")
    oa.add_argument("spec", help="URL (live /openapi.json, apis.guru, vendor) or local file")
    oa.add_argument("--name", help="recipe name (default: derived from spec title)")
    oa.add_argument("--port", type=int, help="override the API port")
    oa.add_argument("--emit", action="store_true", help="write the recipe (else dry-run)")
    oa.set_defaults(func=cmd_openapi)
    ce = sub.add_parser("capture-exception", help="manage the capture-exception matrix")
    ce_sub = ce.add_subparsers(dest="ce_cmd", required=True)
    cea = ce_sub.add_parser("add", help="declare a misbehaving capture (sparse, member-only)")
    cea.add_argument("--subject", required=True, help="device class/module the anomaly concerns")
    cea.add_argument("--match", required=True, help="capture-filename glob, e.g. 'FortiGate_*'")
    cea.add_argument("--behavior", default="non_deterministic", help="observed anomaly (free text)")
    cea.add_argument("--disposition", default="exclude_from_history",
                     help="how backup-configs treats it (default: exclude_from_history)")
    cea.add_argument("--observed", default="", help="when + what you saw")
    cea.add_argument("--source", default="user", help="user | derived (default: user)")
    cea.add_argument("--commit", action="store_true", help="apply + commit + land on the local canonical (judged by the promote gate)")
    cea.add_argument("--push", action="store_true",
                     help="also push to origin (OPTIONAL offsite backup; implies --commit)")
    cea.set_defaults(func=cmd_capture_exception_add)
    args = ap.parse_args()
    try:
        args.func(args)
    except (gitio.WriteConflict, paths.RequestBoundaryError, paths.RepositoryDataError) as e:
        # one clean line: a write conflict or a file of the repository's own that stopped the command (the GUI/API's
        # 409), or a refused argument (their 422)
        sys.exit(str(e))


if __name__ == "__main__":
    main()
