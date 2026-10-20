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

  --worktree    the repository whose HEAD is landed (default: the current directory)
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

Exit 0: landed, or `main` is this commit already ("up to date"), or — with --check — it would land. Exit 1:
nothing to land as asked — not a repository, no commit at HEAD, no `local` remote, the canonical unreadable or its
`main` not a branch, not a fast-forward (the canonical has commits the worktree does not: bring the worktree
forward and run again), the ref moved meanwhile, git failed. Exit 2: REFUSED — the gate refused the tree, or
could not run; nothing was packed and nothing moved. After any exit but 0 the worktree keeps its commit, the
canonical its refs, the running stack is as it was.

AS WHOM. Run it as yourself from your checkout (the deploy's host-direct mode): the worktree's git runs as you,
and so does the canonical's — in the seam, which runs none of the canonical's hooks and config-named programs.
Run as root (the installer container; `sudo`), every git of the canonical's side is the SERVICE uid, as the
promote's is (rule 5 of `kontroll.canon`). The one line it prints first — `kontroll_run_id=… land …` — is the
record of what was landed (the deploy's log, or your terminal); no credential is in it.
"""
import argparse
import os
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


def main(argv):
    ap = argparse.ArgumentParser(
        description="Land the worktree's HEAD on the canonical's main — judged by the promote gate, never a push (C10).")
    ap.add_argument("--worktree", default=".", help="the repository whose HEAD is landed (default: the current directory)")
    ap.add_argument("--repo", default=None,
                    help="the canonical, a bare repository on this machine (default: the worktree's `local` remote)")
    ap.add_argument("--check", action="store_true", help="judge and say what would land; change nothing")
    ap.add_argument("--skip-conflict-check", action="store_true",
                    help="land without the promote gate (the human's override; nothing automatic passes it)")
    ap.add_argument("--propose", default=None, metavar="<run id>",
                    help="create refs/heads/proposed/<run id> instead of moving main (a minted run id)")
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
    if args.propose is not None and not canon.valid_run_id(args.propose):
        return _refuse(1, "--propose takes a minted run id (twelve hex digits, or <UTC stamp>-<six hex digits>); "
                          "nothing was landed.")
    target = canon.proposal_ref(args.propose) if args.propose else canon.MAIN
    canonical = args.repo
    if canonical is None:
        canonical = gitio._canonical_of(worktree)
        if canonical is None:
            return _refuse(1, "the worktree %s has no `local` remote naming a canonical on this machine; say which "
                              "with --repo, or run playbooks/local-canonical.yml (it sets the 'local' remote). "
                              "Nothing was landed." % worktree)
    elif canonical.startswith("file://"):
        canonical = canonical[len("file://"):]
    if "://" in canonical or not os.path.isdir(canonical):
        return _refuse(1, "--repo must name a bare repository on this machine (a directory; a `file://` URL is "
                          "taken as its path). A landing uses no transport. Nothing was landed.")
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
