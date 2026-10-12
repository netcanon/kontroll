#!/usr/bin/env python3
"""Resolve the collection set (modules/_core.yml + each enabled module's module.yml,
driven by instance/fleet.yml) into an ansible-galaxy requirements file.

Standalone — no ansible (the one kontroll import, paths, is os-only: the instance-overlay
resolver) — so CI (and anyone) can install the collections without running the full bootstrap. Mirrors ansible/playbooks/bootstrap.yml's resolution
against the SAME source of truth (modules/); it is never a hand-maintained list (a hard
rule — see CLAUDE.md). Output path defaults to the generated lockfile bootstrap also uses.

Usage:  python3 scripts/gen-requirements.py [<out-path>]
        python3 scripts/gen-requirements.py --all-modules                    # every module's set: the published runner
        python3 scripts/gen-requirements.py --core                           # modules/_core.yml alone: the installer
        python3 scripts/gen-requirements.py --check                          # is THIS node's lockfile fresh (fleet mode)
        python3 scripts/gen-requirements.py --conflict-check --root <tree>   # FIX-M9: judge ANOTHER tree's data
"""
import os
import sys

import yaml

from kontroll import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOCKFILE_DEST = "ansible/collections/requirements.generated.yml"
SIDECAR_DEST = "ansible/collections/trust.generated.yml"   # the never-brick trust policy (gitignored, same rule)
DEFAULT_KEYRING = "instance/trust/galaxy-pubkeys.gpg"      # the wrapper's keyring; absent at install => skip-L3 (NB-1)
SIG_POLICIES = ("adaptive", "required", "none")           # adaptive = the brick-proof default-on-absence (NB-5)


def _load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _is_exact(v):
    return isinstance(v, str) and v.strip().startswith("==")


def resolve_pins(colls):
    """Dedup the unioned collection pins by name with the app-store policy (report 21 §4.3; MVP per §11.1):
    a unit's EXACT '==' pin WINS over a module FLOOR ('>=') for the same collection (an app-store install pins a
    REVIEWED version; a floor is weaker and must never silently override it); TWO DIFFERENT exact pins for one
    collection FAIL CLOSED (no single install satisfies both — re-pin one). String-equality only, no SemVer floor
    math (deferred per report 32 §5.4) — this closes the silent-downgrade hole without a dependency. Floors-only
    names keep the FIRST (the pre-actuation behaviour, so a fleet with no units derives a byte-identical lockfile).
    Raises SystemExit on an irreconcilable conflict."""
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
         "keyring: %s\n" % (keyring or "null") +
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
    return out, _actuation_provenance(adir, enabled)


def all_module_keys():
    """Every device-class key in the PUBLIC modules/ catalog (the dirs, not _core.yml) — the superset's enabled set."""
    md = os.path.join(ROOT, "modules")
    return sorted(d for d in os.listdir(md) if os.path.isdir(os.path.join(md, d)))


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
    check = "--check" in args
    conflict_check = "--conflict-check" in args                # FIX-M9: resolve-only (the promote-time pin-conflict gate)
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
        fleet = _load(paths.resolve("config/fleet.yml"))
        enabled = fleet.get("enabled_modules") or []
        default_policy = fleet.get("signature_policy") or "adaptive"   # the instance-wide NB-5 ratchet (default adaptive)
        if default_policy not in SIG_POLICIES:
            raise SystemExit("instance signature_policy %r must be one of %s (fail closed)"
                             % (default_policy, list(SIG_POLICIES)))
        if default_policy == "required":               # FIX-RATCHET: a fleet-wide strict on an unsigned source bricks
            sys.stderr.write("WARNING: signature_policy: required is set fleet-wide — on a source that serves NO "
                             "signatures (public Galaxy, every current fleet collection) this FAILS every install. Use "
                             "only with a signing-enabled source (Automation Hub / a self-signed mirror).\n")
        keyring = fleet.get("keyring") or DEFAULT_KEYRING

    out, prov = compute(enabled, paths.resolve("actuation"))  # FIX-M9: resolve_pins fails closed on conflicting `==`
    if conflict_check:                                        # the promote-time gate — resolve only, no staleness/no write
        print("gen-requirements: no pin conflict — generation would succeed (--conflict-check)")
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
