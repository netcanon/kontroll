#!/usr/bin/env python3
"""Land the worktree's HEAD on the canonical's `main` — judged by the promote gate first, never a push (C10).

The canonical (`/srv/kontroll.git`) is what the instance runs. Four doors used to put a commit on its `main` with
`git push … HEAD:refs/heads/main`: `scripts/update.sh` and `scripts/update-from-bundle.sh` after a code update,
the operator CLI's onboard (`scripts/galaxy.py … --commit`), and the deploy's mirror task
(`ansible/playbooks/local-canonical.yml`). A push runs the canonical's `update` hook and whatever its config
names, as the pusher — the operator, or root in the installer container — and passed no gate: a fleet line or a
collection pin the promote refuses was on `main` the moment a deploy ended, and from then on every proposal was
refused. Now `refs/heads/main` moves only through `gitio.land_commit`: a commit id, judged by the same gate the
promote runs (this checkout's `gen-requirements.py --conflict-check` over the commit's files, read object by
object from YOUR repository and re-hashed), brought across as a pack on a pipe (`pack-objects --stdout` here,
`index-pack --stdin` there, in the seam of `kontroll.canon`), and compare-and-swapped in one ref transaction.
The canonical's hook refuses a push of `main` from everyone; this command is the door.

Usage:  python3 scripts/kontroll-land.py [--worktree <dir>] [--repo <canonical>] [--check]
                                         [--skip-conflict-check] [--propose <run id>]
        python3 scripts/kontroll-land.py --sync [--worktree <dir>] [--repo <canonical>] [--check | --stop-as <play>]

  --worktree    the repository whose HEAD is landed, or brought forward with --sync (default: the current
                directory, which must be the top of its repository for --sync)
  --repo        the canonical, a bare repository on this machine (default: what the worktree's `local` remote
                names — the remote `local-canonical.yml` adds)
  --check       judge and say what would land; change nothing (the deploy's dry-run runs this first)
  --skip-conflict-check
                land WITHOUT the gate — the human's override, for a node that must move while the gate cannot
                run or refuses a tree you have read yourself; nothing automatic passes it
  --propose <run id>
                create `refs/heads/proposed/<run id>` instead of moving `main` — the hand-made proposal (a
                minted run id: twelve lower-case hex digits, or <UTC stamp>-<six hex digits>); refused when
                that proposal exists
  --sync        bring the worktree FORWARD to the canonical's `main` instead of landing (what a promote put
                there comes across as a pack on a pipe, made in the seam, and the worktree's own commits are
                replayed on top — `gitio.sync_worktree`); a deploy runs it before it lands. With --check it says
                whether there is anything to bring forward and changes nothing. Not with --propose or
                --skip-conflict-check
  --stop-as <play>
                with --sync (not --check): the sync is a play's own (ansible/playbooks/local-canonical.yml), and
                when it brings the worktree forward it is that run's LAST step — it says what came in as the
                play's stop, removes the mark it wrote and exits 4, which fails the play's task, so that nothing
                of the tree that came in runs in that run (SECURITY.md C10); a refusal is said as the play's,
                on standard output, with exit 1; in step it exits 0

Exit 0: landed, or `main` is this commit already ("up to date"), or — with --check — it would land. Exit 1:
nothing to land as asked — not a repository, no commit at HEAD, no `local` remote, the canonical unreadable or its
`main` not a branch, not a fast-forward (the canonical has commits the worktree does not: bring the worktree
forward and run again), a shallow clone (`git clone --depth`) whose pack would not be whole in the canonical, or
that cannot show the canonical's `main` under HEAD (`git fetch --unshallow`, and run again), the ref moved
meanwhile, git failed. Exit 2: REFUSED — the gate
refused the tree, or could not run; nothing was packed and nothing moved. After any exit but 0 the worktree
keeps its commit, the canonical its refs, the running stack is as it was.

With --sync — exit 0: in step (the landing fast-forwards), or brought forward; exit 1: not brought forward, and HEAD,
the branch and the tracked files are as they were unless the sentence says the worktree could not be put back, that git
could not say whether the tracked files (or those its status does not compare) are HEAD's, or that HEAD does not descend
from `main` (it then names HEAD) — of the files of `main` that git wrote before it stopped, each regular file reached
through no link and holding main's bytes is removed, and the sentence names those and any it left; the sentence says
why: a conflict names its files, a merge in the worktree's own history or a commit already on another remote names the
merge that brings it forward instead, an ignored file in the way of main's checkout is named, and so are a worktree
named below the top of its repository and a mark that could not be written; exit 3: with --check, there is something to
bring forward; exit 4: with --stop-as, brought forward, and the play stops here.

WHAT CAME IN. A sync that brought the worktree forward prints what came in since HEAD before it: the commits the
canonical's `main` gained, every path that changed, and each mode change, symlink and gitlink. Read it then: what
came in is the code the next deploy runs — its playbooks, its scripts, the installer image built from it — as root,
and whether that deploy stops first is that tree's own play's to say. The sync also leaves a mark in the worktree's
git directory (`gitio.SYNC_MARK`), before its replay, so a sync killed in the replay leaves it too; a deploy whose
play keeps the shipped stop says the same, removes the mark (not beside a rebase in progress, where it stays until
the abort) and stops before it lands, deploys or runs the tree's scripts (SECURITY.md C10). A play's own sync
(--stop-as) removes the mark itself, after saying what came in: that run is the one that stops.

AS WHOM. Run it as yourself from your checkout (the deploy's host-direct mode): the worktree's git runs as you,
and so does the canonical's — in the seam, which runs none of the canonical's hooks and config-named programs.
Run as root (the installer container; `sudo`), every git of the canonical's side is the SERVICE uid, as the
promote's is (rule 5 of `kontroll.canon`). The one line it prints first — `kontroll_run_id=… land …` — is the
record of what was landed (the deploy's log, or your terminal); no credential is in it. A sync writes the
WORKTREE, so it is refused to anyone but the worktree's owner (the deploy runs it as the operator, and so reads
the canonical as the operator); its line is `kontroll_run_id=… sync …`.
"""
import argparse
import os
import re
import shlex
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `from kontroll import gitio` resolves
from kontroll import canon, gitio


def _mint():
    """A run id of the stamped shape (`api.audit.mint_run_id`'s), for the landing's own log line when the landing
    is not a proposal: a landing is not a request, and has no id of its own to carry."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()


def _refuse(code, sentence):
    sys.stdout.flush()                                    # the log line above it comes first, on a terminal too
    sys.stderr.write("REFUSED: %s\n" % sentence if code == 2 else "kontroll-land: %s\n" % sentence)
    return code


def _stop_as(play, worktree, rc, said, head_before):
    """A play's own sync (`--stop-as <play>`), said as that play's, on standard output: the play's task fails on the
    exit and prints it. In step: exit 0, and the play goes on. Not brought forward: the play's refusal, exit 1. Brought
    forward: the run's LAST step — what came in since the mark's first line (or `head_before`, HEAD as read before the
    sync, when the mark cannot be read, or git cannot list from its first line: read A-f4, N4), then the mark removed
    (this run is the one that stops on it, so the next one goes on), then exit 4, which fails the play's task: no
    Ansible worker starts after the tree changed, and a worker can import the tree's filter plugins when it first
    templates, as root (read A-f3, B1). The sync wrote the mark before its replay, so a run killed in the replay, or
    here, is stopped on by the next."""
    if rc == 0 and not said.startswith("brought forward"):
        print("kontroll-land: %s" % said)
        return 0
    if rc != 0:
        print("%s: REFUSING to land — the sync of the worktree to the canonical's main exited %d: %s%s Nothing was "
              "landed: the canonical is untouched, and the worktree is as this run's state commit left it, unless "
              "the sentence says otherwise. Do what the sentence says in %s, then run again."
              % (play, rc, said, "" if said.endswith(".") else ".", worktree))
        return 1
    print("kontroll-land: %s" % said)
    before = gitio.synced_since(worktree) or head_before
    listing = gitio.came_in(worktree, before) if before else ["git could not list it"]
    if listing == ["git could not list it"] and head_before and before != head_before:
        # The mark's first line is a commit git cannot list from here (one the worktree lacks): what this sync
        # brought is still listed, since HEAD as read before it.
        before, listing = head_before, gitio.came_in(worktree, head_before)
    print("%s: STOPPED after bringing the worktree forward to the canonical's main. What came in%s:"
          % (play, " since %s" % before if before else ""))
    for line in listing:
        print("  %s" % line)
    sys.stdout.flush()
    left = gitio.clear_mark(worktree)
    print("%s Nothing was landed or deployed from the tree the worktree now holds. Read what came in now: the next "
          "run, its dry-run too, runs that tree's own playbooks and scripts as root (the installer image is built "
          "from it), and whether that run stops first is that tree's own play's to say (SECURITY.md C10). Then run "
          "the dry-run (`--check --diff`) and deploy — in step, unless another promote lands first (it then stops "
          "again, naming what that brought). HEAD before the sync: %s; and in `git -C %s reflog`."
          % ("The mark is removed, so the next run goes on." if left is None else "The mark was NOT removed (%s): "
             "remove it by hand, as yourself, or the next run stops here again." % left, before or "not known",
             shlex.quote(worktree)))
    return 4


def main(argv):
    ap = argparse.ArgumentParser(
        description="Land the worktree's HEAD on the canonical's main — judged by the promote gate, never a push (C10).")
    ap.add_argument("--worktree", default=".",
                    help="the repository whose HEAD is landed, or brought forward with --sync (default: the current "
                         "directory, which must be the top of its repository for --sync)")
    ap.add_argument("--repo", default=None,
                    help="the canonical, a bare repository on this machine (default: the worktree's `local` remote)")
    ap.add_argument("--check", action="store_true", help="judge and say what would land; change nothing")
    ap.add_argument("--skip-conflict-check", action="store_true",
                    help="land without the promote gate (the human's override; nothing automatic passes it)")
    ap.add_argument("--propose", default=None, metavar="<run id>",
                    help="create refs/heads/proposed/<run id> instead of moving main (a minted run id)")
    ap.add_argument("--sync", action="store_true",
                    help="bring the worktree forward to the canonical's main instead of landing (with --check: say "
                         "whether there is anything to; exit 3 when there is)")
    ap.add_argument("--stop-as", default=None, metavar="<play>",
                    help="with --sync, a play's own sync: brought forward, it says what came in as the play's stop, "
                         "removes the mark and exits 4")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:                               # argparse's usage error is exit 2, and 2 is "refused" here
        if e.code in (0, None):
            raise
        return 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
    worktree = os.path.abspath(args.worktree)
    if args.sync and (args.propose is not None or args.skip_conflict_check):
        return _refuse(1, "--sync brings the worktree forward and lands nothing, so it takes neither --propose nor "
                          "--skip-conflict-check; nothing was changed.")
    if args.stop_as is not None and (not args.sync or args.check
                                     or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.stop_as)):
        return _refuse(1, "--stop-as names the play whose own sync this is (lower-case letters, digits and dashes) "
                          "and takes --sync, not --check; nothing was changed.")
    if args.propose is not None and not canon.valid_run_id(args.propose):
        return _refuse(1, "--propose takes a minted run id (twelve hex digits, or <UTC stamp>-<six hex digits>); "
                          "nothing was landed.")
    target = canon.proposal_ref(args.propose) if args.propose else canon.MAIN
    nothing = "Nothing was changed." if args.sync else "Nothing was landed."
    canonical = args.repo
    if canonical is None:
        canonical = gitio._canonical_of(worktree)
        if canonical is None:
            return _refuse(1, "the worktree %s has no `local` remote naming a canonical on this machine; say which "
                              "with --repo, or run playbooks/local-canonical.yml (it sets the 'local' remote). %s"
                              % (worktree, nothing))
    elif canonical.startswith("file://"):
        canonical = canonical[len("file://"):]
    if "://" in canonical or not os.path.isdir(canonical):
        return _refuse(1, "--repo must name a bare repository on this machine (a directory; a `file://` URL is "
                          "taken as its path). A landing, and a sync, use no transport. %s" % nothing)
    if args.sync:
        print("kontroll_run_id=%s sync %s%s <- refs/heads/main of %s" % (
            _mint(), "--check " if args.check else "", worktree, os.path.abspath(canonical)))
        head_before = gitio.head_id(worktree) if args.stop_as else None
        rc, said = gitio.sync_worktree(worktree, canonical, check=args.check)
        if args.stop_as:
            return _stop_as(args.stop_as, worktree, rc, said, head_before)
        if rc in (0, 3):
            print("kontroll-land: %s" % said)
            if rc == 0 and said.startswith("brought forward"):
                before = gitio.synced_since(worktree)
                print("kontroll-land: what came in%s:" % (" since %s" % before if before else ""))
                for line in gitio.came_in(worktree, before) if before else ["git could not list it"]:
                    print("  %s" % line)
                print("kontroll-land: read it now — the next deploy runs this tree's own playbooks and scripts, as "
                      "root, and only this tree's own play decides whether that deploy stops first (SECURITY.md C10)")
            return rc
        return _refuse(rc, said)
    run_id = args.propose or _mint()
    print("kontroll_run_id=%s land %s HEAD of %s -> %s of %s%s" % (
        run_id, "--check" if args.check else "", worktree, target, os.path.abspath(canonical),
        " (--skip-conflict-check: the gate is NOT asked)" if args.skip_conflict_check else ""))
    rc, said = gitio.land_commit(worktree, canonical, target=target, skip_gate=args.skip_conflict_check,
                                 check=args.check)
    if rc == 0:
        print("kontroll-land: %s" % said)
        return 0
    return _refuse(rc, said)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
