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
from kontroll import canon, gitio

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


_GATE_SECONDS = 300      # the generator judges a tree in well under a second; this is the bound, not the budget


class GateCouldNotRun(Exception):
    """The gate did not judge the tree: git could not read the repository, this checkout has no generator, the
    generator could not start or did not finish. NOT a verdict on the proposal — and not a pass either: the promote
    is refused with this sentence, and `--skip-conflict-check` is the human's way past a gate that cannot run."""


def _git_dir(repo):
    """The git DIRECTORY `--repo` names: the path itself for a bare repository (the canonical), `<path>/.git` for a
    clone. The gate names the repository to git outright (`kontroll.canon`), so nothing is discovered from a cwd."""
    repo = repo or ROOT
    dot = os.path.join(repo, ".git")
    return dot if os.path.isdir(dot) else repo


def _would_brick_generate(run_id, repo):
    """FIX-M9 — the promote-time resolve gate. Read the PROSPECTIVE post-merge tree (`proposed/<run_id>`, which a
    fast-forward makes `main`) and run THIS tree's `gen-requirements.py --conflict-check --root <copy>` over it:
    report anything the collection generator would REFUSE there — two conflicting `==` pins for one collection, a
    bad `signature_policy`, a collection entry or a trust statement it cannot render, a fleet whose
    `enabled_modules` is not a list of class keys. It judges the whole tree, so a fault already on `main` refuses
    every proposal until one that removes it. Returns the message to refuse on, or None to proceed; raises
    `GateCouldNotRun` when nothing could be judged.

    Why at promote: two units each fine against today's `main` can conflict with EACH OTHER once both are active;
    only the FF moment (against the live `main`) is authoritative, so checking here means two conflicting `==` units
    can NEVER both become active and brick a LATER deploy's generate (which halts pre-image-build, far from the
    cause). Read-only: copies to a temp dir and never touches the canonical or `main`.

    TRUSTED CODE, PROPOSAL DATA. The gate used to run the proposal's OWN copy of gen-requirements.py — i.e. execute
    unreviewed code from the very ref it was judging, as root on the `sudo` CLI or with SOPS_AGE_KEY in scope on the
    Semaphore path (2026-10-08 review, finding 3). Now the generator is the one THIS checkout ships (ROOT — canonical
    `main`, or the baked image), the tree is handed to it as a DATA root (`--root`), and the subprocess sees only
    the whitelisted environment above.

    THE COMMIT ITSELF. The proposal's ref is read ONCE, by its full name (`canon.read_ref`), and what is judged is
    the commit it names: its tree read object by object, each object re-hashed against its id
    (`canon.read_tree_verified`). The gate used to judge `git archive`'s EXPORT of the ref — and an export is the
    commit as its own `.gitattributes` make it (`export-ignore` hides a path from the judge, `export-subst`
    rewrites a file for it), with the repository's filters run over it. A ref of that name that is a symbolic ref,
    or names no commit, is not a proposal and is refused.

    IT FAILS CLOSED. Every way the old gate answered "nothing to check" on a tree it had not read — the export
    failed, the unpack failed, `modules/` was not in what came out, git or tar was missing — was a way for the
    proposal (or the repository) to switch the gate off. Now: a tree the reader refuses is a refusal; a proposal
    with no `modules/` where `main` has one is a refusal; and a gate that cannot run says so and refuses
    (`GateCouldNotRun`). "Nothing to check" is left for one case, decided on trees that WERE read: neither the
    proposal nor `main` has a device-class registry. A proposal that does not exist is not judged at all — the
    fast-forward that follows refuses it (it reports "not a fast-forward") and the exit is non-zero."""
    gitdir = _git_dir(repo)
    try:
        proposal = canon.read_ref(gitdir, canon.proposal_ref(run_id))
        main = canon.read_ref(gitdir, canon.MAIN)
    except canon.Unreadable as e:
        raise GateCouldNotRun(str(e)) from None
    if proposal is None:
        return None                                      # no such proposal: the fast-forward says so and refuses
    if proposal[2] or proposal[1] != "commit":
        return "the ref of that name is not a proposal: it is a symbolic ref, or it names no commit"
    if main is not None and (main[2] or main[1] != "commit"):
        # Not "there is no `main` to compare with": the fast-forward below would move whatever this pointer leads to,
        # and the one comparison the gate makes with `main` (does the proposal remove the registry) would be skipped.
        return ("`main` is not a branch that names a commit — it is a symbolic ref, or it names another kind of "
                "object — so nothing is promoted onto it")
    return _tree_refusal(proposal[0], gitdir, main[0] if main else None)


_SAID_LINES, _SAID_CHARS = 12, 600


def _said(stderr):
    """What the generator wrote, bounded: its last lines, each cut to a length. Its refusals are one sentence; a
    crash on a file of the proposal is a traceback as long as the proposal makes it (a deeply nested YAML file is
    three thousand lines), and one refusal repeats a value of the fleet file through `repr`."""
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    cut = ["  (%d earlier lines of the generator's output are not shown)" % (len(lines) - _SAID_LINES)] if len(lines) > _SAID_LINES else []
    return "\n  ".join(cut + [ln if len(ln) <= _SAID_CHARS else ln[:_SAID_CHARS] + " …" for ln in lines[-_SAID_LINES:]])


def _has_registry(files):
    return any(path.startswith(b"modules/") for path in files)


def _reads_the_example(files):
    """Would the generator read THIS checkout's shipped example fleet in place of the tree's own? It does for a
    tree with no `instance/` directory at all and no legacy `config/fleet.yml` (`kontroll.paths.resolve`, the
    third tier) — a file that is not in the commit being judged."""
    return b"config/fleet.yml" not in files and not any(path.startswith(b"instance/") for path in files)


def _tree_refusal(commit_id, gitdir, main_id=None):
    """The gate's answer for ONE commit of the repository at `gitdir`: why its tree is refused, or None — exactly
    as `_would_brick_generate` describes. `main_id` is the commit `main` names (None when there is none to compare
    with); it decides one thing only — whether a tree without `modules/` is a proposal that REMOVES the registry.
    `main()` asks this of `main`'s own commit as well, to say WHERE a fault is."""
    gen = os.path.join(ROOT, "scripts", "gen-requirements.py")    # THIS tree's generator — never the proposal's copy
    try:
        files = canon.read_tree_verified(gitdir, commit_id)
        if not _has_registry(files):
            if main_id and main_id != commit_id and _has_registry(canon.read_tree_verified(gitdir, main_id)):
                return ("the tree has no `modules/` directory and `main` has one: promoting it would remove the "
                        "device-class registry")
            return None                                  # neither side has a registry: nothing for the generator
        if (_reads_the_example(files) and main_id and main_id != commit_id
                and not _reads_the_example(canon.read_tree_verified(gitdir, main_id))):
            return ("the tree has no fleet file (no `instance/` and no `config/fleet.yml`) and `main` has one: the "
                    "generator would judge this checkout's example fleet in its place")
        if not os.path.isfile(gen):
            raise GateCouldNotRun("this checkout has no scripts/gen-requirements.py to judge the tree with")
        with tempfile.TemporaryDirectory(prefix="kontroll-promote-") as tmp:
            canon.write_files(files, tmp)
            env = {k: os.environ[k] for k in _GATE_ENV if k in os.environ}
            env.setdefault("PYTHONIOENCODING", "utf-8")
            # -B: a promote run with sudo from the operator's checkout must not leave a root-owned __pycache__ there
            try:
                p = subprocess.run([sys.executable, "-B", gen, "--conflict-check", "--root", tmp], cwd=ROOT, env=env,
                                   capture_output=True, encoding="utf-8", errors="replace", timeout=_GATE_SECONDS)
            except subprocess.TimeoutExpired:
                raise GateCouldNotRun("the collection generator did not finish in %d seconds" % _GATE_SECONDS) from None
            except OSError:
                raise GateCouldNotRun("the collection generator could not be started") from None
            return None if p.returncode == 0 else (_said(p.stderr) or "generation would fail closed")
    except canon.TreeRefused as e:
        return "the tree cannot be read as it was committed: %s" % e
    except canon.Unreadable as e:
        raise GateCouldNotRun(str(e)) from None


def _main_is_refused(repo):
    """Is `main` as it stands refused by the gate? For the hint only: any failure to tell is a no."""
    try:
        gitdir = _git_dir(repo)
        main = canon.read_ref(gitdir, canon.MAIN)
        return bool(main and main[1] == "commit" and not main[2] and _tree_refusal(main[0], gitdir))
    except (canon.Unreadable, GateCouldNotRun):
        return False


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
    if not canon.valid_run_id(args.run_id):
        # The only strings that name a proposal are the ids the service mints. Anything else never reaches a ref
        # name — and, since a leading hyphen is not one, never reaches git as an option.
        sys.stderr.write("REFUSED: that is not a run id (twelve hex digits, or <UTC stamp>-<six hex digits>). The "
                         "Pending panel and the propose response show the id of each staged proposal.\n")
        return 1
    if not args.skip_conflict_check:                      # FIX-M9: refuse a promote that would brick the next generate
        try:
            msg = _would_brick_generate(args.run_id, args.repo)
        except GateCouldNotRun as e:
            sys.stderr.write(
                "REFUSED: promoting proposed/%s — the gate could not run, so the proposal's tree was not judged:\n"
                "  %s\n"
                "Nothing was promoted. Correct what the indented line names and run the promote again "
                "(--skip-conflict-check promotes WITHOUT the gate — the human's override, for a node that must "
                "move while the gate cannot run).\n" % (args.run_id, e))
            return 2
        if msg:
            # The gate judges the whole tree, so ask the same of `main` as it stands: when `main` is refused too, the
            # fault is there already, whatever this proposal changes — say so, or the operator looks for it in a
            # proposal that does not hold it. (Not "does this proposal touch the fleet file": an onboard of a new
            # class does, and a pin conflict of the proposal's own does not.) A read error answers None: no hint.
            where = ("(`main` as it stands is refused as well: the fault is on `main` already, and every proposal is "
                     "refused until it is corrected there — on the node, correct it in the checkout and deploy; or "
                     "promote a proposal that corrects it.)\n"
                     if _main_is_refused(args.repo) else "")
            sys.stderr.write(
                "REFUSED: promoting proposed/%s — the proposal's tree does not resolve, so the next bootstrap (and, "
                "for a fleet line, the next deploy) would stop:\n  %s\n"
                "The indented line says what to correct (--skip-conflict-check overrides this gate).\n%s"
                % (args.run_id, msg, where))
            return 2
    return 0 if gitio.promote_ref(args.run_id, cwd=args.repo) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
