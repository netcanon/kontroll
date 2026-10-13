#!/usr/bin/env python3
"""Promote a staged proposal to `main` — the trusted PROMOTE half of C10 (propose-then-promote).

The deployed API/GUI runs as a STAGING network service (`KONTROLL_STAGE_PUSHES=1`): it can only push a
`proposed/<run_id>` ref to the canonical — it CANNOT advance `main`. This operator-run command fast-forwards
`main` into that proposal (**fast-forward ONLY** — it refuses a non-FF, so a proposal can never rewrite
history), then deletes the proposal. Run it on the control node against the canonical (a bare repo works —
`--repo /srv/kontroll.git`), or against a clone that has fetched the proposal. A Semaphore "approve" task
wrapping this is the in-GUI follow-on. Decision record: docs/privileged-mutation-enablement.md (SECURITY.md C10).

Run it as ROOT (`sudo`) — or as a member of the canonical's owning group (gid 1001). It advances `main` and
then deletes the staged proposal; the proposal ref is created by the container (uid 1001), so deleting it needs
write to the group-1001 canonical (the FF of `main` itself works as the repo owner, but the proposal cleanup
does not). Verified live 2026-06-15 (docs/privileged-mutation-enablement.md §5).

Usage:  sudo python3 scripts/kontroll-promote.py <run_id> --repo /srv/kontroll.git
"""
import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `from kontroll import gitio` resolves
from kontroll import gitio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The ONLY environment the gate's generator subprocess inherits. A promote runs as root on the `sudo` CLI and with
# SOPS_AGE_KEY in scope on the Semaphore path; the generator needs neither, so it gets neither (finding 3). Python's
# OWN path configuration passes through: a runner (or a venv-less box) may locate the interpreter's packages via
# PYTHONPATH / PYTHONUSERBASE / VIRTUAL_ENV, and a relocated interpreter via LD_LIBRARY_PATH (actions/setup-python on
# a self-hosted runner); without them the child cannot even `import yaml` — the first private sync found exactly
# that. Paths, not secrets; the whitelist stays closed to everything else.
_GATE_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "LANG", "LC_ALL", "PYTHONUTF8", "PYTHONIOENCODING",
             "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "VIRTUAL_ENV", "PYTHONNOUSERSITE",
             "LD_LIBRARY_PATH", "pythonLocation")


def _would_brick_generate(run_id, repo):
    """FIX-M9 — the promote-time resolve gate. Extract the PROSPECTIVE post-merge tree (`proposed/<run_id>`,
    which a fast-forward makes `main`) and run THIS tree's `gen-requirements.py --conflict-check --root <extract>`
    over it: report anything the collection generator would REFUSE there — two conflicting `==` pins for one
    collection, a bad `signature_policy`, a collection entry or a trust statement it cannot render, a fleet whose
    `enabled_modules` is not a list of class keys. It judges the whole tree, so a fault already on `main` refuses
    every proposal until one that removes it. Returns the message to refuse on, or None to proceed.

    Why at promote: two units each fine against today's `main` can conflict with EACH OTHER once both are active;
    only the FF moment (against the live `main`) is authoritative, so checking here means two conflicting `==` units
    can NEVER both become active and brick a LATER deploy's generate (which halts pre-image-build, far from the
    cause). Read-only: extracts to a temp dir and never touches the canonical or `main`.

    TRUSTED CODE, PROPOSAL DATA. The gate used to run the proposal's OWN copy of gen-requirements.py — i.e. execute
    unreviewed code from the very ref it was judging, as root on the `sudo` CLI or with SOPS_AGE_KEY in scope on the
    Semaphore path (2026-10-08 review, finding 3). Now the generator is the one THIS checkout ships (ROOT — canonical
    `main`, or the baked image), the extract is handed to it as a DATA root (`--root`), and the subprocess sees only
    the whitelisted environment above. Fails OPEN on our own read error (git/tar missing, an unreadable ref, a tree
    with no modules/): the generate-time fail-closed remains the backstop, and the gate must never itself block a
    clean promote."""
    # The FULL name — the one promote_ref fast-forwards. The short `proposed/<id>` is, to git, a tag
    # (refs/tags/proposed/<id>) or a ref of its own (refs/proposed/<id>) before it is a branch: a ref of either
    # kind beside the proposal made this gate judge one tree while the fast-forward took another.
    return _tree_refusal("refs/heads/proposed/%s" % run_id, repo)


def _tree_refusal(ref, repo):
    """The gate's answer for ONE tree — `ref` in `repo`: the collection generator's refusal of it, or None, exactly as
    `_would_brick_generate` describes. `main()` asks it of `refs/heads/main` as well, to say WHERE a fault is."""
    gen = os.path.join(ROOT, "scripts", "gen-requirements.py")    # THIS tree's generator — never the proposal's copy
    try:
        arc = subprocess.run(["git", "archive", "--format=tar", ref], cwd=repo or ROOT, capture_output=True)
        if arc.returncode != 0:
            return None                                  # can't read the ref here — promote_ref fails loudly if it's truly absent
        with tempfile.TemporaryDirectory(prefix="kontroll-promote-") as tmp:
            untar = subprocess.run(["tar", "-x", "-C", tmp], input=arc.stdout)
            if untar.returncode != 0 or not os.path.isdir(os.path.join(tmp, "modules")):
                return None                              # extraction failed / no modules registry in the tree — nothing to check
            env = {k: os.environ[k] for k in _GATE_ENV if k in os.environ}
            env.setdefault("PYTHONIOENCODING", "utf-8")
            # -B: a promote run with sudo from the operator's checkout must not leave a root-owned __pycache__ there
            p = subprocess.run([sys.executable, "-B", gen, "--conflict-check", "--root", tmp], cwd=ROOT, env=env,
                               capture_output=True, text=True)
            return None if p.returncode == 0 else (p.stderr.strip() or "generation would fail closed")
    except OSError:
        return None                                      # git/tar unavailable → fail open (backstop at generate)


def main(argv):
    ap = argparse.ArgumentParser(
        description="Promote a staged proposed/<run_id> ref to main (fast-forward only) — C10.")
    ap.add_argument("run_id", help="the proposal's run_id (the proposed/<run_id> ref the service created)")
    ap.add_argument("--repo", default=None,
                    help="the canonical (bare) or clone git dir (default: this repo root)")
    ap.add_argument("--skip-conflict-check", action="store_true",
                    help="skip the promote-time resolve gate (FIX-M9) — two conflicting == pins, a fleet line that "
                         "is not a class key, anything else the collection generator refuses (emergency override)")
    args = ap.parse_args(argv)
    if not args.skip_conflict_check:                      # FIX-M9: refuse a promote that would brick the next generate
        msg = _would_brick_generate(args.run_id, args.repo)
        if msg:
            # The gate judges the whole tree, so ask the same of `main` as it stands: when `main` is refused too, the
            # fault is there already, whatever this proposal changes — say so, or the operator looks for it in a
            # proposal that does not hold it. (Not "does this proposal touch the fleet file": an onboard of a new
            # class does, and a pin conflict of the proposal's own does not.) A read error answers None: no hint.
            where = ("(`main` as it stands is refused as well: the fault is on `main` already, and every proposal is "
                     "refused until it is corrected there — on the node, correct it in the checkout and deploy; or "
                     "promote a proposal that corrects it.)\n"
                     if _tree_refusal("refs/heads/main", args.repo) else "")
            sys.stderr.write(
                "REFUSED: promoting proposed/%s — the proposal's tree does not resolve, so the next bootstrap (and, "
                "for a fleet line, the next deploy) would stop:\n  %s\n"
                "The indented line says what to correct (--skip-conflict-check overrides this gate).\n%s"
                % (args.run_id, msg, where))
            return 2
    return 0 if gitio.promote_ref(args.run_id, cwd=args.repo) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
