#!/usr/bin/env python3
"""Resolve the collection set (modules/_core.yml + each enabled module's module.yml, driven by
instance/fleet.yml) into an ansible-galaxy requirements file. This is the reader that decides what is INSTALLED: a
fleet whose `enabled_modules` is not a list of device-class keys is REFUSED here — in a generate, under
`--conflict-check` (the promote gate) and in `--fleet-check` (the plays) — with the entry's place and never the
entry (SECURITY.md C22).

Standalone — no ansible (its kontroll imports, paths and yamlscalar, need neither a collection nor a play) — so CI
(and anyone) can install the collections without running the full bootstrap. It is the ONE place the collection
set is resolved (bootstrap.yml and deploy-stack.yml both call it); it is never a hand-maintained list (a hard
rule — see CLAUDE.md). Output path defaults to the generated lockfile.

Usage:  python3 scripts/gen-requirements.py [<out-path>]
        python3 scripts/gen-requirements.py --all-modules                    # every module's set: the published runner
        python3 scripts/gen-requirements.py --core                           # modules/_core.yml alone: the installer
        python3 scripts/gen-requirements.py --check                          # is THIS node's lockfile fresh (fleet mode)
        python3 scripts/gen-requirements.py --conflict-check --root <tree>   # FIX-M9: judge ANOTHER tree's data
        python3 scripts/gen-requirements.py --fleet-check                    # is every fleet line a class key? (plays)
"""
import os
import re
import sys

import yaml

from kontroll import paths, yamlscalar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOCKFILE_DEST = "ansible/collections/requirements.generated.yml"
SIDECAR_DEST = "ansible/collections/trust.generated.yml"   # the never-brick trust policy (gitignored, same rule)
DEFAULT_KEYRING = "instance/trust/galaxy-pubkeys.gpg"      # the wrapper's keyring; absent at install => skip-L3 (NB-1)
SIG_POLICIES = ("adaptive", "required", "none")           # adaptive = the brick-proof default-on-absence (NB-5)


def _load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_fleet():
    """The fleet file, parsed — or ONE sentence when it cannot be read: where it stops parsing when the parser
    says, and no piece of the file. A traceback's last line quotes the file: a tag or an alias name (the parser's
    own errors), and the scalar itself when a constructor fails on it — `!!int x`, `!!bool x`, `!!float x`, a date
    that does not exist — which are a ValueError, a KeyError, an AttributeError, never a YAMLError. Bytes that are
    not UTF-8 and a nesting too deep for the parser end here too; a file that cannot be opened says only why."""
    try:
        return _load(paths.resolve("config/fleet.yml"))
    except OSError as exc:
        raise SystemExit("gen-requirements: instance/fleet.yml cannot be opened (%s). Every generator stops on it."
                         % (exc.strerror or type(exc).__name__))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = " (line %d, column %d)" % (mark.line + 1, mark.column + 1) if mark else ""
    except (ValueError, LookupError, AttributeError, TypeError, RecursionError):
        where = ""
    raise SystemExit("gen-requirements: instance/fleet.yml is not YAML this tool can read%s. Correct the file: every "
                     "generator stops on it." % where)


def _is_exact(v):
    return isinstance(v, str) and v.strip().startswith("==")


# What may stand in the two hand-formatted lockfiles. The lockfile and the trust sidecar are rendered by hand (they
# have to be yamllint-clean), so a value outside these sets made a file unparseable, or parse to something else —
# and module files and unit descriptors are data somebody proposed.
#   NAME      `namespace.name`, each half starting with a letter: it stands UNQUOTED, and `1.5` is a float there.
#   VERSION   a pin, a floor or a range, inside double quotes: never a quote, a backslash, a colon, a `#`, a break.
#   POLICY    one of SIG_POLICIES (unquoted).         DIGEST   sixty-four lowercase hex digits (inside quotes).
# The instance keyring path is not refused: it is written through yamlscalar.word(), bare when it is a plain word
# (the default is) and quoted when it is not.
_LOCK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*\Z")
_LOCK_VERSION_RE = re.compile(r"[0-9A-Za-z.+\-<>=!~^*, ]{1,256}\Z")
_LOCK_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


def _assert_renderable(colls):
    """Fail closed (offline, before anything is rendered or written) on a collection entry whose name or version
    could not be written into the lockfile as itself. An absent or empty version is "no constraint", as it always
    was; a version YAML read as a number (`version: 8` unquoted in a module file) is taken by its text, as it
    always was — the text of a number is inside the charset."""
    for c in colls:
        name = c.get("name") if isinstance(c, dict) else None
        version = c.get("version") if isinstance(c, dict) else None
        if not isinstance(name, str) or not _LOCK_NAME_RE.fullmatch(name):
            raise SystemExit("gen-requirements: a collection entry has a name that is not `namespace.name` — "
                             "refusing to render the lockfile (fail closed)")
        if version is None or version == "":
            continue
        if isinstance(version, (int, float)) and not isinstance(version, bool):
            version = str(version)
        if not isinstance(version, str) or not _LOCK_VERSION_RE.fullmatch(version):
            raise SystemExit("gen-requirements: collection %s has a version that is not a plain pin, floor or "
                             "range — refusing to render the lockfile (fail closed)" % name)


def _assert_trust_renderable(prov_by_name):
    """Fail closed on a unit's trust statement that could not be written into the sidecar as itself: a signature
    policy outside SIG_POLICIES (it stands unquoted — one with a line break added an entry for ANOTHER collection,
    with a policy of its choice), or a digest that is not sixty-four hex digits (inside quotes — one with a quote
    in it rewrote its own entry's policy; one with a trailing newline read back as a different digest, so a correct
    install failed its comparison). A malformed trust statement is never quietly taken for the default."""
    for name in sorted(prov_by_name):
        p = prov_by_name[name] or {}
        if p.get("signature_policy") not in SIG_POLICIES:
            raise SystemExit("gen-requirements: the unit that installs %s declares a signature policy that is not "
                             "one of %s — refusing to render the trust sidecar (fail closed)"
                             % (name, list(SIG_POLICIES)))
        sha = p.get("sha256")
        if sha is not None and sha != "" and not (isinstance(sha, str) and _LOCK_DIGEST_RE.fullmatch(sha)):
            raise SystemExit("gen-requirements: the unit that installs %s declares a sha256 that is not sixty-four "
                             "lowercase hex digits — refusing to render the trust sidecar (fail closed)" % name)


def resolve_pins(colls):
    """Dedup the unioned collection pins by name with the app-store policy (report 21 §4.3; MVP per §11.1):
    a unit's EXACT '==' pin WINS over a module FLOOR ('>=') for the same collection (an app-store install pins a
    REVIEWED version; a floor is weaker and must never silently override it); TWO DIFFERENT exact pins for one
    collection FAIL CLOSED (no single install satisfies both — re-pin one). String-equality only, no SemVer floor
    math (deferred per report 32 §5.4) — this closes the silent-downgrade hole without a dependency. Floors-only
    names keep the FIRST (the pre-actuation behaviour, so a fleet with no units derives a byte-identical lockfile).
    Raises SystemExit on an irreconcilable conflict, and on an entry that could not be rendered as itself."""
    _assert_renderable(colls)
    chosen, order = {}, []
    for c in colls:
        name = c["name"]
        if name not in chosen:
            chosen[name] = c
            order.append(name)
            continue
        cv, nv = chosen[name].get("version"), c.get("version")
        if _is_exact(cv) and _is_exact(nv):
            if cv.strip() != nv.strip():
                raise SystemExit("requirements pin conflict: collection %s is pinned to both %r and %r by different "
                                 "actuation units — re-pin one (gen-requirements fails closed)." % (name, cv, nv))
        elif _is_exact(nv) and not _is_exact(cv):
            chosen[name] = c                         # an exact app-store pin WINS over a module floor
        # else: keep current (floor-vs-floor keep-first; exact-vs-later-floor keeps the exact)
    return [chosen[n] for n in order]


def _actuation_collections(adir, enabled_modules):
    """Union the install.collections of every ACTIVE actuation unit under `adir` whose target class is enabled
    (report 21 §3 net composition rule). `adir` is the overlay-resolved actuation dir (instance/actuation/ when
    present). Returns {name, version} entries in the SAME shape modules emit, so they flow through resolve_pins +
    the emit loop unchanged. A malformed / disabled / non-enabled-target unit contributes nothing (gen-actuation.py
    is the LOUD fail-closed validator; this reader is defensively quiet)."""
    out, enabled = [], set(enabled_modules or [])
    if not os.path.isdir(adir):
        return out
    for key in sorted(os.listdir(adir)):
        up = os.path.join(adir, key, "unit.yml")
        if not os.path.isfile(up):
            continue
        try:
            u = _load(up) or {}
        except (OSError, yaml.YAMLError):
            continue
        if u.get("status", "active") != "active":
            continue
        if (u.get("target") or {}).get("device_class") not in enabled:
            continue
        out += (u.get("install") or {}).get("collections") or []
    return out


def _actuation_provenance(adir, enabled_modules):
    """Return {collection_name: {signature_policy, keyring, sha256}} for every ACTIVE, enabled-target unit's
    install.collections — the never-brick trust policy the sidecar materializes (R5 PR-B). A unit that omits
    `signature` defaults to 'adaptive' (the brick-proof NB-5 default); gen-actuation.py is the LOUD validator, this
    reader is defensively quiet. Module-derived collections are NOT in the map, so they fall to the sidecar default.
    OFFLINE by construction (synthesis Decision R1): the sha256 is the descriptor's value or None — NEVER fetched;
    the install WRAPPER records the real digest at first install, so no network call enters this install-gating
    generator (closes BRICK-1)."""
    prov, enabled = {}, set(enabled_modules or [])
    if not os.path.isdir(adir):
        return prov
    for key in sorted(os.listdir(adir)):
        up = os.path.join(adir, key, "unit.yml")
        if not os.path.isfile(up):
            continue
        try:
            u = _load(up) or {}
        except (OSError, yaml.YAMLError):
            continue
        if u.get("status", "active") != "active":
            continue
        if (u.get("target") or {}).get("device_class") not in enabled:
            continue
        p = (u.get("install") or {}).get("provenance") or {}
        for c in (u.get("install") or {}).get("collections") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            prov[c["name"]] = {"signature_policy": p.get("signature") or "adaptive",
                               "keyring": p.get("keyring"), "sha256": p.get("sha256")}
    return prov


def render_lockfile(out):
    """The byte-exact lockfile content — UNCHANGED from R2 (a unit-free fleet must derive the same
    requirements.generated.yml, the no-blast invariant)."""
    s = ("---\n"
         "# GENERATED by scripts/gen-requirements.py from instance/fleet.yml + modules/.\n"
         "# Do not edit by hand — change modules/ or instance/fleet.yml and re-run.\n"
         "collections:\n")
    for c in out:                                  # hand-written so it's yamllint-clean (indented seq)
        s += "  - name: %s\n" % c["name"]
        if c.get("version"):
            s += "    version: \"%s\"\n" % c["version"]
    return s


def render_sidecar(out, prov_by_name, default_policy, keyring):
    """The never-brick trust sidecar (R5 PR-B): per-collection signature policy + the L2b sha256 anchor, read by
    the (future) install wrapper. GENERATED + gitignored like the lockfile. A module-derived collection isn't in
    `prov_by_name` -> the brick-proof default policy (NB-5). sha256 is the descriptor value or null — recorded by
    the wrapper at install, never fetched here (Decision R1); a `>=` floor therefore always carries `sha256: null`
    (BRICK-3 — a digest re-verifies only against an exact `==` install)."""
    s = ("---\n"
         "# GENERATED by scripts/gen-requirements.py — DO NOT EDIT (derives like requirements.generated.yml).\n"
         "# The never-brick trust policy the install wrapper reads. Offline: sha256 is the descriptor value or\n"
         "# null; the wrapper records the real digest at install (docs/reviews/2026-06-25-never-brick-supply-chain).\n"
         "schema: 1\n"
         "default_signature_policy: %s\n" % default_policy +
         "keyring: %s\n" % (yamlscalar.word(keyring) if keyring else "null") +
         "collections:\n")
    for c in out:
        p = prov_by_name.get(c["name"], {})
        ver, sha = c.get("version"), p.get("sha256")
        s += "  - name: %s\n" % c["name"]
        s += "    version: %s\n" % (("\"%s\"" % ver) if ver else "null")
        s += "    signature_policy: %s\n" % (p.get("signature_policy") or default_policy)
        s += "    sha256: %s\n" % (("\"%s\"" % sha) if sha else "null")
    return s


def compute(enabled, adir):
    """Resolve the unioned, deduped collection set + the per-collection provenance map. Pure given the on-disk
    modules/units — no network (the BRICK-1 invariant; tests pin it)."""
    colls = list(_load("modules/_core.yml").get("collections") or [])
    for key in enabled:
        mp = os.path.join("modules", key, "module.yml")
        if os.path.exists(os.path.join(ROOT, mp)):
            colls += _load(mp).get("collections") or []
    colls += _actuation_collections(adir, enabled)     # R2: the app-store units' pins union in
    out = resolve_pins(colls)                          # exact-wins-over-floor; fail-closed on conflicting exacts
    prov = _actuation_provenance(adir, enabled)
    _assert_trust_renderable(prov)                     # here, not at the render: the promote gate stops after compute
    return out, prov


def _fleet_message(problems, refusing=True):
    """The one message for a fleet whose `enabled_modules` is not a list of class keys: positions and the rule,
    never an entry, and what ends the condition. `refusing` — a generate or the promote gate's resolve, which
    stop because a skipped class would be missing from what is installed; the plays' `--fleet-check` resolves
    nothing, so it says only what it found (each play stops on it, before anything walks the list)."""
    found = ("instance/fleet.yml: %s. Correct the file — remove or fix each line named; a proposal that does "
             "promotes." % "; ".join(problems))
    if not refusing:
        return "gen-requirements: " + found
    return ("gen-requirements: refusing to resolve the collection set — %s Fail closed: a class that is skipped "
            "here would install less than the fleet declares, and say nothing." % found)


def all_module_keys():
    """Every device-class key in the PUBLIC modules/ catalog (the dirs, not _core.yml) — the superset's enabled set."""
    md = os.path.join(ROOT, "modules")
    return sorted(d for d in os.listdir(md) if paths.is_component(d) and os.path.isdir(os.path.join(md, d)))


def main():
    global ROOT
    args = sys.argv[1:]
    if "--root" in args:
        # FIX-M9's promote gate runs THIS (trusted) generator over ANOTHER tree's DATA — the extracted
        # proposed/<run_id> — instead of executing the proposal's own copy of this script, which would run
        # unreviewed code as root on the `sudo` CLI or with SOPS_AGE_KEY in scope on the Semaphore path
        # (2026-10-08 review, finding 3). ROOT becomes the data root for modules/, and KONTROLL_WRITE_ROOT points
        # paths.resolve() (fleet, actuation) at the same tree. The CODE — this file and kontroll.paths — stays the
        # caller's. Meant for --conflict-check: a generate with --root would write into that tree.
        ROOT = os.path.abspath(args[args.index("--root") + 1])
        os.environ["KONTROLL_WRITE_ROOT"] = ROOT
    if "--fleet-check" in args:
        # The ONE check of the fleet list, for the plays: it runs before anything joins a fleet entry into a path
        # (bootstrap.yml's own lookup; every generator deploy-stack.yml calls), with the rule every reader uses
        # (kontroll.paths) and not a second copy of its charset in Jinja. Reads one file; resolves nothing; offline.
        others = sorted(a for a in args if a in ("--check", "--conflict-check", "--core", "--all-modules"))
        if others:                                     # it would pass for a mode that never looked at what it names
            raise SystemExit("--fleet-check resolves nothing, so it cannot be combined with %s — run them separately"
                             % ", ".join(others))
        problems = paths.fleet_problems(_load_fleet())
        if problems:
            raise SystemExit(_fleet_message(problems, refusing=False))
        print("gen-requirements: every enabled_modules entry is a device-class key (--fleet-check)")
        return
    check = "--check" in args
    conflict_check = "--conflict-check" in args                # FIX-M9: resolve-only — the promote gate (a pin
    #                                                            conflict, a fleet line that is not a key, an entry
    #                                                            it cannot render)
    all_modules = "--all-modules" in args                     # the PUBLISHED-runner SUPERSET (report 22 §2.3)
    core_only = "--core" in args                              # the INSTALLER's base: modules/_core.yml alone
    if all_modules and core_only:
        raise SystemExit("--all-modules and --core are two different sets — pass one")
    if core_only and conflict_check:
        raise SystemExit("--core resolves no actuation unit, so --conflict-check could never refuse — the promote "
                         "gate runs without --core")
    if core_only:
        # The installer image carries what the INSTALL playbooks import and nothing of any device fleet, and it is
        # built before an instance exists. So: no fleet read at all. (With no flag and no instance/, paths.resolve
        # falls through to the shipped EXAMPLE fleet — which would bake device collections into the installer.)
        enabled, default_policy, keyring = [], "adaptive", DEFAULT_KEYRING
    elif all_modules:
        # The published kontroll-control (runner) image bakes the union of EVERY module's collections — CI has no
        # instance/fleet.yml, and a prebuilt image can't know a node's fleet. Derived from the PUBLIC modules/
        # catalog (+ the public actuation/ dir, empty in the shipped tree); the per-node fleet then only SELECTS
        # which baked collections a play uses at runtime. OFFLINE (BRICK-1) — pure data over modules/. No fleet read.
        enabled, default_policy, keyring = all_module_keys(), "adaptive", DEFAULT_KEYRING
    else:
        fleet = _load_fleet()
        problems = paths.fleet_problems(fleet)
        if problems:                                   # the reader that decides what is INSTALLED does not skip
            raise SystemExit(_fleet_message(problems))
        enabled = paths.enabled_keys(fleet)
        default_policy = fleet.get("signature_policy") or "adaptive"   # the instance-wide default the sidecar records
        if default_policy not in SIG_POLICIES:
            raise SystemExit("instance signature_policy %r must be one of %s (fail closed)"
                             % (default_policy, list(SIG_POLICIES)))
        if default_policy == "required":               # say what the word does: it is recorded, not enforced
            sys.stderr.write("WARNING: signature_policy: required is set fleet-wide, and no install step enforces it "
                             "yet: every collection installs with the command line an `adaptive` one gets, and no "
                             "install fails for a missing signature. The word is recorded in the trust sidecar, and "
                             "the provenance view shows a collection as `signed` once a keyring file exists — "
                             "without a signature having been checked. SECURITY.md C15-a.\n")
        keyring = fleet.get("keyring") or DEFAULT_KEYRING

    out, prov = compute(enabled, paths.resolve("actuation"))  # FIX-M9: resolve_pins fails closed on conflicting `==`
    if conflict_check:                                        # the promote-time gate — resolve only, no staleness/no write
        print("gen-requirements: the tree resolves — generation would succeed (--conflict-check)")
        return
    lock, side = render_lockfile(out), render_sidecar(out, prov, default_policy, keyring)
    dest = sys.argv[1] if (len(sys.argv) > 1 and not sys.argv[1].startswith("--")) else LOCKFILE_DEST

    if check:                                          # staleness gate (the output is gitignored — diff on-disk if
        stale = []                                     # present, else a clean generation is the floor; never a fetch)
        for path, content in ((dest, lock), (SIDECAR_DEST, side)):
            full = os.path.join(ROOT, path)
            if os.path.exists(full):
                with open(full, encoding="utf-8") as fh:
                    if fh.read() != content:
                        stale.append(path)
        if stale:
            for p in stale:
                sys.stderr.write("STALE: %s differs from a fresh generation — re-run scripts/gen-requirements.py\n" % p)
            sys.exit(1)
        print("gen-requirements: lockfile + trust sidecar fresh (--check)")
        return

    for path, content in ((dest, lock), (SIDECAR_DEST, side)):
        full = os.path.join(ROOT, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    print("wrote %s + %s (%d collections: %s)" % (dest, SIDECAR_DEST, len(out), ", ".join(c["name"] for c in out)))


if __name__ == "__main__":
    main()
