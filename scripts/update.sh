#!/usr/bin/env bash
# Non-destructive code update for a kontroll INSTANCE.
#
# The instance is the source of truth (docs/local-source-of-truth.md). This pulls
# PRODUCT-CODE updates from an upstream remote and MERGES them, PRESERVING instance
# state — onboarded hosts, encrypted secrets, the fleet selection, dashboards. That is
# the whole point of Phase B: never `git reset --hard` (which would clobber the
# operator's local state commits). Code and state live in DISJOINT files
# (config/state-manifest.yml), so the merge is normally conflict-free; a genuine
# conflict means a *code* file was edited locally — surfaced, never silently lost.
#
# Then it re-seeds the local canonical the running instance (Semaphore) reads, so the
# new code takes effect with no remote git at run time.
#
# This is the DEVELOPER-managed update path (a git-connected instance). The git-FREE
# operator update — code delivered via the runner image / a release bundle — is Phase C.
#
# Usage:  scripts/update.sh [<remote> [<branch>]]      # default: origin main
set -euo pipefail
REMOTE="${1:-origin}"; BRANCH="${2:-main}"
cd "$(dirname "$0")/.."
# Layer 0: tee this run to a persistent, 30-day-pruned log (docs/logging-architecture.md §3).
if [ -f scripts/lib/run-log.sh ]; then . scripts/lib/run-log.sh; run_log_init "update"; fi

# A sync, a fetch and a merge write the checkout's git directory. Run as anyone but its owner (root, under sudo),
# they would leave files there that the owner's git cannot write over afterwards, so refuse before anything is.
owner="$(stat -c %u "$(git rev-parse --git-common-dir 2>/dev/null || echo .git)" 2>/dev/null || echo unknown)"
if [ "$owner" != "$(id -u)" ]; then
  echo "!! REFUSING to update — this runs as uid $(id -u), and the checkout's git directory is uid $owner's." >&2
  echo "   Run it as the checkout's owner (not under sudo). Nothing was fetched or merged." >&2
  exit 1
fi

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "No '$REMOTE' remote — this instance has no upstream. Code updates come via the" >&2
  echo "runner image / release bundle (Phase C), not git. Nothing to do." >&2
  exit 0
fi

# A promote moves the canonical's `main` past this checkout. Merging an update on top of that and landing would be
# refused at the end (not a fast-forward) with the merge already made; so ask first, and change nothing here when
# the canonical is ahead. `--sync --check` is read-only: exit 3 = the canonical has commits this checkout does
# not; 1 = it could not tell, or a real sync would refuse (the sentence says why).
if git remote get-url local >/dev/null 2>&1; then
  rc=0; python3 -B scripts/kontroll-land.py --sync --check --repo "$(git remote get-url local)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "" >&2
    echo "!! REFUSING to update — the local canonical's main has moved past this checkout (a promote), or" >&2
    echo "   could not be compared with it (the sentence above). Nothing was fetched or merged. Bring the" >&2
    echo "   checkout forward first: python3 -B scripts/kontroll-land.py --sync (a deploy does this too)," >&2
    echo "   read what it says came in (the next deploy runs that tree, as root), then re-run." >&2
    exit "$rc"
  fi
fi

echo "==> Fetching ${REMOTE}/${BRANCH} (product code)…"
git fetch --quiet "$REMOTE" "$BRANCH"

before="$(git rev-parse HEAD)"
echo "==> Merging code updates, preserving instance state…"
if ! git merge --no-edit "${REMOTE}/${BRANCH}"; then
  echo "" >&2
  echo "!! Merge conflict — a CODE file was edited on this instance. Resolve it, then" >&2
  echo "   re-run. Instance STATE (inventory/secrets/fleet/dashboards) is never the" >&2
  echo "   cause: it lives in files upstream doesn't touch (config/state-manifest.yml)." >&2
  exit 1
fi

if [ "$(git rev-parse HEAD)" = "$before" ]; then
  echo "==> Already up to date."
fi

echo "==> Landing HEAD on the local canonical (what the instance runs)…"
# Not a push: a landing is judged by the promote gate first (a fleet line or a collection pin the promote would
# refuse stops HERE, with the generator's sentence, instead of reaching `main` and refusing every later promote),
# then brought across as a pack on a pipe and compare-and-swapped (SECURITY.md C10). The canonical's hook refuses a
# push of `main` from everyone. Exit 2 = refused (nothing moved; fix the tree and re-run, or --skip-conflict-check
# as the human's override); exit 1 = nothing to land (no `local` remote, not a fast-forward: the sentence says).
if git remote get-url local >/dev/null 2>&1; then
  if python3 -B scripts/kontroll-land.py --repo "$(git remote get-url local)"; then
    echo "    Local canonical updated — Semaphore's jobs use it from their next run. The API and GUI services"
    echo "    run the code of the last deploy until the next one."
  else
    rc=$?
    echo "!! The landing did not happen (exit $rc) — the canonical is untouched; the merged code is in this" >&2
    echo "   checkout only. Read the sentence above, correct it, and re-run (docs/troubleshooting.md §10)." >&2
    exit "$rc"
  fi
else
  echo "    No 'local' remote yet — run: ansible-playbook ansible/playbooks/local-canonical.yml"
fi

echo "==> Done. If the enabled modules/collections changed, re-run bootstrap + rebuild the runner."
