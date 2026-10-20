#!/usr/bin/env python3
"""Promote a staged proposal to `main` — the trusted PROMOTE half of C10 (propose-then-promote).

The deployed API/GUI runs as a STAGING network service (`KONTROLL_STAGE_PUSHES=1`): it can only push a
`proposed/<run_id>` ref to the canonical — it CANNOT advance `main`. This operator-run command fast-forwards
`main` into that proposal (**fast-forward ONLY** — it refuses a non-FF, so a proposal can never rewrite
history), then deletes the proposal. Run it on the control node against the canonical (a bare repo works —
`--repo /srv/kontroll.git`), or against a clone that has fetched the proposal. The Semaphore `promote-proposal`
task wraps it. Decision record: docs/privileged-mutation-enablement.md (SECURITY.md C10).

ONE ID. The proposal's ref is read once, by its full name; the commit that read returned is the commit whose
landing is previewed, the commit the gate judges, and the commit one ref transaction makes `main` — provided the
proposal is STILL that commit when the transaction runs (`gitio.promote_commit`). A person says which commit they
looked at by giving `<run id>@<commit id>` (the Pending panel's copy control and `--list` print that form); a
proposal that has become another commit since is refused. `--require-pin` (the Semaphore job passes it) refuses a
bare run id. The lines this command prints before it promotes — the run id, the commit, and the commits being
landed — are the record of what was approved.

AND, FOR A CALLER WHOSE JUDGEMENT DEPENDED ON `main`: `--onto <commit id>` promotes only if `refs/heads/main` IS
that commit. The gate here judges the proposal's TREE, which is the same whatever `main` is; a caller that judged
the proposal's DIFF against a `main` it read itself (which paths change — a classifier, a reviewer of a change
list) approved a change RELATIVE to that commit, and this command reads `main` for itself, a moment later. Named,
the commit it was judged against is the commit `main` is moved FROM, or nothing moves.

AS WHOM. Run it with `sudo`. As root it does not run git as root: every git it starts in a repository the
service can write runs as the SERVICE uid (`canon.git_kwargs`, rule 5 of `kontroll.canon`). When a deploy is armed
the canonical's own files — a config line, a ref, a pack — are written by a network service, and whatever they
can make a git do they then make it do as that service and no more. The Semaphore job and the containers already
are the service uid, and run this as they are. Anybody else — a member of the canonical's group who is neither —
is told to use `sudo`: run as themselves, git would open that same store with their account. (That last rule is
a maintainer's call; letting group members through is the one refusal in `main` that says so, turned into a
note.) What the gate then does with the commit's files — the collection generator over a copy of them — runs as
the caller, on files the caller wrote: it starts no git.

Usage:  sudo python3 scripts/kontroll-promote.py <run_id>@<commit_id> --repo /srv/kontroll.git
        sudo python3 scripts/kontroll-promote.py --list --repo /srv/kontroll.git

Exit 0: promoted (or listed). Exit 1: nothing to promote as asked — not a run id, no such proposal, not the commit
that was named, `main` not the commit named with `--onto`, not a fast-forward, the canonical unreadable, a caller
who is neither root nor the service uid, a root that cannot become the service uid, or `main` moved meanwhile.
Exit 2: the gate refused the proposal's tree, or could not run (`--skip-conflict-check` is the override).
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
_PREVIEW = 20            # how many of the commits a promote lands are printed, newest first
_SUBJECT = 200           # how much of one subject is printed; the rest is counted, not shown


def _subject(b):
    """A commit subject as one line of the preview or of `--list`: escaped where it is not text (`canon.show`),
    and cut — a subject is as long as its author made it, and a line of spaces as wide as the terminal reads as
    the end of one line and the start of the next."""
    text = canon.show(b)[0]
    return text if len(text) <= _SUBJECT else "%s [+%d characters]" % (text[:_SUBJECT], len(text) - _SUBJECT)


def _streams_never_raise():
    """A subject is whatever was committed and the terminal's encoding is whatever the caller's locale says: a
    character the encoding lacks used to end the preview — or `--list`, part-way down — in a traceback. It is
    written as its backslash escape instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


class GateCouldNotRun(gitio.GateCouldNotRun):
    """The gate did not judge the tree: git could not read the repository, this checkout has no generator, the
    generator could not start or did not finish. NOT a verdict on the proposal — and not a pass either: the promote
    is refused with this sentence, and `--skip-conflict-check` is the human's way past a gate that cannot run.
    A subclass of `gitio.GateCouldNotRun`: a landing (`gitio.land_commit`) runs this same gate over the commit
    it lands and catches the base."""


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
    """FIX-M9 — the promote-time resolve gate, for ONE commit of the repository at `gitdir`: why its tree is
    refused, or None. The commit is the one the promote will land (a fast-forward makes it `main`) — or the one a
    LANDING brings in (`gitio.land_commit`, which loads this function and judges the worktree's commit, read from
    the worktree's own repository, before anything touches the canonical: every way into `main` passes this gate).
    The gate runs
    THIS tree's `gen-requirements.py --conflict-check --root <copy>` over it and reports anything the collection
    generator would REFUSE there — two conflicting `==` pins for one collection, a bad `signature_policy`, a
    collection entry or a trust statement it cannot render, a fleet whose `enabled_modules` is not a list of class
    keys. It judges the whole tree, so a fault already on `main` refuses every proposal until one that removes it
    (`main()` asks this of `main`'s own commit as well, to say WHERE a fault is). Raises `GateCouldNotRun` when
    nothing could be judged. `main_id` is the commit `main` names; it decides one thing only — whether a tree
    without `modules/` is a proposal that REMOVES the registry.

    Why at promote: two units each fine against today's `main` can conflict with EACH OTHER once both are active;
    only the FF moment (against the live `main`) is authoritative, so checking here means two conflicting `==` units
    can NEVER both become active and brick a LATER deploy's generate (which halts pre-image-build, far from the
    cause). Read-only: copies to a temp dir and never touches the canonical or `main`.

    TRUSTED CODE, PROPOSAL DATA. The gate used to run the proposal's OWN copy of gen-requirements.py — i.e. execute
    unreviewed code from the very ref it was judging, as root on the `sudo` CLI or with SOPS_AGE_KEY in scope on the
    Semaphore path (2026-10-08 review, finding 3). Now the generator is the one THIS checkout ships (ROOT — canonical
    `main`, or the baked image), the tree is handed to it as a DATA root (`--root`), and the subprocess sees only
    the whitelisted environment above.

    THE COMMIT ITSELF. What is judged is the commit `commit_id` — an id, which the caller read from the proposal's
    ref once — and its tree as it is: read object by object, each object re-hashed against its id
    (`canon.read_tree_verified`). The gate used to judge `git archive`'s EXPORT of a ref name — and an export is
    the commit as its own `.gitattributes` make it (`export-ignore` hides a path from the judge, `export-subst`
    rewrites a file for it), with the repository's filters run over it.

    IT FAILS CLOSED. Every way the old gate answered "nothing to check" on a tree it had not read — the export
    failed, the unpack failed, `modules/` was not in what came out, git or tar was missing — was a way for the
    proposal (or the repository) to switch the gate off. Now: a tree the reader refuses is a refusal; a proposal
    with no `modules/` where `main` has one is a refusal; and a gate that cannot run says so and refuses
    (`GateCouldNotRun`). "Nothing to check" is left for one case, decided on trees that WERE read: neither the
    proposal nor `main` has a device-class registry."""
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


def _main_is_refused(gitdir, main_id):
    """Is `main` as it stands refused by the gate? For the hint only: any failure to tell is a no."""
    try:
        return bool(_tree_refusal(main_id, gitdir))
    except GateCouldNotRun:
        return False


def _refuse(code, sentence):
    sys.stderr.write("REFUSED: %s\n" % sentence)
    return code


def _list(gitdir):
    """`--list`: every staged proposal as `<run id>@<commit id>  <subject>`, newest first — the form the promote
    takes, read the way the promote reads (by full ref name, in the seam). What a person uses instead of running
    git in the canonical by hand."""
    try:
        found, apart, left_out = canon.proposals(gitdir)
    except canon.Unreadable as e:
        sys.stderr.write("kontroll-promote: the canonical could not be read (%s)\n" % e)
        return 1
    for f in sorted(found, key=lambda f: f["when"], reverse=True):
        print("%s@%s  %s" % (f["run_id"], f["oid"], _subject(f["subject"])))
    if not found:
        sys.stderr.write("kontroll-promote: no proposals are staged\n")
    if apart:
        sys.stderr.write("kontroll-promote: refs that are not proposals sit under refs/heads/proposed/ (the "
                         "Pending panel lists them); none of them can be promoted\n")
    if left_out:
        sys.stderr.write("kontroll-promote: git left out at least one ref under refs/heads/proposed/ that it "
                         "could not read\n")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        description="Promote a staged proposal to main (fast-forward only) — C10. The proposal is named by its "
                    "run id, or by <run id>@<commit id> to say which commit you were shown.")
    ap.add_argument("target", nargs="?", metavar="<run_id>[@<commit_id>]",
                    help="the proposal's run id; with @<commit id>, promote only if the proposal is still that "
                         "commit (the Pending panel's copy control and --list print this form)")
    ap.add_argument("--repo", default=None,
                    help="the canonical (bare) or clone git dir (default: this repo root)")
    ap.add_argument("--list", action="store_true",
                    help="print the staged proposals as <run id>@<commit id> and exit")
    ap.add_argument("--require-pin", action="store_true",
                    help="refuse a bare run id — the commit must be named (the Semaphore job passes this)")
    ap.add_argument("--onto", default=None, metavar="<commit_id>",
                    help="promote only if refs/heads/main is this commit — for a caller that judged the proposal "
                         "against the main it read (a change list, a classifier); refused when main is another")
    ap.add_argument("--skip-conflict-check", action="store_true",
                    help="skip the promote-time resolve gate (FIX-M9) — two conflicting == pins, a fleet line that "
                         "is not a class key, anything else the collection generator refuses (emergency override)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:                               # argparse printed its own sentence (or the help text)
        if e.code in (0, None):
            raise
        return 1
    _streams_never_raise()
    gitdir = canon.git_dir(args.repo or ROOT)
    if hasattr(os, "geteuid") and os.geteuid() not in (0, canon.SERVICE_UID) and canon.service_writable(gitdir):
        # Root is made the service uid for every git (the seam); the service uid is it already. Anybody else
        # would run git AS THEMSELVES in a store a network service writes — the listing too, which is why this
        # stands before it. A repository of which nothing is the service's (the caller's own clone, every test
        # repository) is not asked about. Maintainer's call (the module docstring): to let a member of the
        # canonical's group through, print this sentence as a note and go on.
        return _refuse(1, "run this with sudo. The repository is one the network service can write, and you are "
                          "neither root nor that service: as root this command runs git as the service — not as "
                          "you, and not as root. Nothing was read or changed.")
    if hasattr(os, "geteuid") and os.geteuid() == 0 and canon.service_writable(gitdir):
        try:
            canon.git_kwargs(gitdir)        # what every git below would be refused for is said here, once, first
        except canon.Unreadable as e:
            return _refuse(1, "the canonical could not be read (%s)." % e)
        sys.stderr.write("kontroll-promote: git runs as the service uid (%d) in this repository, not as root\n"
                         % canon.SERVICE_UID)
    if args.list:
        return _list(gitdir)
    if not args.target:
        return _refuse(1, "name the proposal: <run id>, or <run id>@<commit id> (--list prints what is staged)")
    rid, at, pin = args.target.partition("@")
    if not canon.valid_run_id(rid) or (at and not canon.valid_oid(pin)):
        # The only strings that name a proposal are the ids the service mints, and the only pin is a full commit
        # id. Anything else never reaches a ref name — and, since a leading hyphen is neither, never reaches git
        # as an option.
        return _refuse(1, "that is not a run id (twelve hex digits, or <UTC stamp>-<six hex digits>), or what "
                          "follows the @ is not a full commit id. The Pending panel and --list show each staged "
                          "proposal in the form to use.")
    if args.require_pin and not pin:
        return _refuse(1, "say which commit: <run id>@<commit id>. The Pending panel's copy control and "
                          "`kontroll-promote.py --list` print it.")
    if args.onto is not None and not canon.valid_oid(args.onto):
        return _refuse(1, "--onto takes a full commit id: the commit refs/heads/main was when the proposal was "
                          "judged (never a ref name — a name is what this is here to rule out).")
    # ONE read of each ref, by its full name. Everything after this is asked of the two ids.
    try:
        main_ref = canon.read_ref(gitdir, canon.MAIN)
        proposal = canon.read_ref(gitdir, canon.proposal_ref(rid))
    except canon.Unreadable as e:
        return _refuse(1, "the canonical could not be read (%s). Nothing was promoted." % e)
    if proposal is None:
        return _refuse(1, "no staged proposal proposed/%s on the canonical (--list prints what is staged)." % rid)
    if proposal[2] or proposal[1] != "commit":
        return _refuse(1, "the ref proposed/%s is not a proposal: it is a symbolic ref, or it names no commit. "
                          "Nothing was promoted." % rid)
    if main_ref is None or main_ref[2] or main_ref[1] != "commit":
        return _refuse(1, "refs/heads/main is missing, or is not a branch that names a commit — there is nothing "
                          "to fast-forward. Nothing was promoted.")
    main_id, commit_id = main_ref[0], proposal[0]
    if args.onto is not None and args.onto != main_id:
        # The caller's judgement was of this proposal RELATIVE to another `main`. Whatever is a fast-forward of
        # both would land a change, measured from where `main` is now, that nobody judged.
        return _refuse(1, "main is not the commit this promote was to land on: it is %s now (it was to be %s). "
                          "Nothing was promoted — judge proposed/%s again, against main as it is."
                          % (main_id, args.onto, rid))
    if pin and pin != commit_id:
        return _refuse(1, "proposed/%s is not the commit you were shown: it is %s now. Look at it again (the "
                          "Pending panel, or --list) before promoting." % (rid, commit_id))
    if commit_id == main_id:
        return _refuse(1, "proposed/%s is main's commit already: there is nothing to promote. Its ref was left "
                          "behind by a promote that could not delete it, or was staged at main — discard it (the "
                          "Pending panel)." % rid)
    if canon.is_ancestor(gitdir, main_id, commit_id) is not True:
        return _refuse(1, "proposed/%s is not a fast-forward of main (main moved past it, or the canonical could "
                          "not say) — stage the change again against current main." % rid)
    # What this approval lands — printed by the promoter itself, from the ids it will use. This, not a panel, is
    # the record of what was approved.
    try:
        count, listed = canon.commits_between(gitdir, main_id, commit_id, _PREVIEW)
    except canon.Unreadable as e:
        return _refuse(1, "the commits this promote would land could not be listed (%s). Nothing was promoted." % e)
    print("kontroll_run_id=%s promote proposed/%s commit=%s -> main (was %s); commits=%d"
          % (rid, rid, commit_id, main_id, count))
    for oid, subject in listed:
        print("  %s  %s" % (oid[:12], _subject(subject)))
    if count > len(listed):
        print("  … and %d older" % (count - len(listed)))
    if not args.skip_conflict_check:                      # FIX-M9: refuse a promote that would brick the next generate
        try:
            msg = _tree_refusal(commit_id, gitdir, main_id)
        except GateCouldNotRun as e:
            sys.stderr.write(
                "REFUSED: promoting proposed/%s — the gate could not run, so the proposal's tree was not judged:\n"
                "  %s\n"
                "Nothing was promoted. Correct what the indented line names and run the promote again "
                "(--skip-conflict-check promotes WITHOUT the gate — the human's override, for a node that must "
                "move while the gate cannot run).\n" % (rid, e))
            return 2
        if msg:
            # The gate judges the whole tree, so ask the same of `main` as it stands: when `main` is refused too, the
            # fault is there already, whatever this proposal changes — say so, or the operator looks for it in a
            # proposal that does not hold it. (Not "does this proposal touch the fleet file": an onboard of a new
            # class does, and a pin conflict of the proposal's own does not.) A read error answers None: no hint.
            where = ("(`main` as it stands is refused as well: the fault is on `main` already, and every proposal is "
                     "refused until it is corrected there — on the node, correct it in the checkout and deploy; or "
                     "promote a proposal that corrects it.)\n"
                     if _main_is_refused(gitdir, main_id) else "")
            sys.stderr.write(
                "REFUSED: promoting proposed/%s — the proposal's tree does not resolve, so the next bootstrap (and, "
                "for a fleet line, the next deploy) would stop:\n  %s\n"
                "The indented line says what to correct (--skip-conflict-check overrides this gate).\n%s"
                % (rid, msg, where))
            return 2
    return 0 if gitio.promote_commit(rid, commit_id, main_id, gitdir) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
