#!/usr/bin/env bash
# Non-destructive code update for a REMOTELESS kontroll instance (no upstream git remote).
#
# scripts/update.sh is the git-connected update path (fetch origin -> merge -> re-seed). A
# production box in the local-as-truth posture (docs/local-source-of-truth.md) has NO GitHub
# remote, so it can't `fetch origin`; instead the developer ships a `git bundle` of the new
# PRODUCT code (built on a connected machine) and applies it here. Everything past the fetch is
# IDENTICAL to update.sh: a state-preserving `git merge` (code & state live in DISJOINT files —
# config/state-manifest.yml — so the merge is normally conflict-free; a real conflict means a
# *code* file was edited on this instance, surfaced never silently lost), then re-seed the local
# canonical the running instance (Semaphore) reads, so the new code takes effect with no remote
# git at run time. This codifies the #145 bundle-merge dance (previously hand-run).
#
# Build the bundle on a git-connected machine (thin — the box already has its current HEAD as basis):
#     git bundle create kontroll.bundle <box-HEAD-or-merge-base>..main      # or, full: git bundle create … --all
#     git bundle verify kontroll.bundle
# scp it to the box, then:
#     scripts/update-from-bundle.sh kontroll.bundle [<branch>]
#
# Usage:  scripts/update-from-bundle.sh <bundle-file> [<branch>]      # default branch: main
set -euo pipefail
BUNDLE="${1:?usage: update-from-bundle.sh <bundle-file> [<branch>]}"
BRANCH="${2:-main}"
cd "$(dirname "$0")/.."
# Layer 0: tee this run to a persistent, 30-day-pruned log (docs/logging-architecture.md §3).
if [ -f scripts/lib/run-log.sh ]; then . scripts/lib/run-log.sh; run_log_init "update-from-bundle"; fi

[ -f "$BUNDLE" ] || { echo "Bundle not found: $BUNDLE" >&2; exit 1; }

echo "==> Verifying the bundle (prerequisites must already be in this repo)…"
git bundle verify "$BUNDLE" >&2

echo "==> Fetching ${BRANCH} from the bundle (product code)…"
git fetch --quiet "$BUNDLE" "$BRANCH"

before="$(git rev-parse HEAD)"
echo "==> Merging code updates, preserving instance state…"
if ! git merge --no-edit FETCH_HEAD; then
  echo "" >&2
  echo "!! Merge conflict — a CODE file was edited on this instance. Resolve it, then" >&2
  echo "   finish with 'git commit' (or re-run after 'git merge --abort'). Instance STATE" >&2
  echo "   (inventory/secrets/fleet/dashboards) is never the cause — it lives in files the" >&2
  echo "   bundle doesn't touch (config/state-manifest.yml)." >&2
  exit 1
fi

if [ "$(git rev-parse HEAD)" = "$before" ]; then
  echo "==> Already up to date."
fi

echo "==> Landing HEAD on the local canonical (what the instance runs)…"
# Not a push — the same landing as scripts/update.sh: judged by the promote gate first, a pack on a pipe, one
# compare-and-swap (SECURITY.md C10). The canonical's hook refuses a push of `main` from everyone. Exit 2 =
# refused (nothing moved; fix the tree and re-run, or --skip-conflict-check as the human's override); exit 1 =
# nothing to land (no `local` remote, not a fast-forward: the sentence says).
if git remote get-url local >/dev/null 2>&1; then
  if python3 -B scripts/kontroll-land.py --repo "$(git remote get-url local)"; then
    echo "    Local canonical updated — Semaphore runs the new code on its next job."
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
