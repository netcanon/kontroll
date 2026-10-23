#!/usr/bin/env bash
# Restore instance STATE from a non-git snapshot (scripts/state-snapshot.sh) into the
# current tree — the DR / migrate path with no git involved. Lay this over a fresh base
# (a clean checkout, a release bundle, or the runner image's code) to reconstitute an
# instance: base code ⊕ restored state.
#
# After restoring you still need: an age key that opens the secrets in place (the control key's
# off-box copy, or the offline break-glass key — docs/break-glass.md), then
# re-seed the local canonical (ansible/playbooks/local-canonical.yml) and run
# ansible/playbooks/deploy-stack.yml. See docs/local-source-of-truth.md.
#
# Usage:  scripts/state-restore.sh <snapshot.tar.gz>
set -euo pipefail
cd "$(dirname "$0")/.."
# Layer 0: tee this run to a persistent, 30-day-pruned log (docs/logging-architecture.md §3).
if [ -f scripts/lib/run-log.sh ]; then . scripts/lib/run-log.sh; run_log_init "state-restore"; fi
ARCHIVE="${1:?usage: state-restore.sh <snapshot.tar.gz>}"
[ -f "$ARCHIVE" ] || { echo "no such snapshot: $ARCHIVE" >&2; exit 1; }

echo "Restoring instance state from $ARCHIVE into $(pwd)…"
echo "Paths it will (over)write:"
tar tzf "$ARCHIVE" | sed 's/^/  /'
tar xzf "$ARCHIVE"
echo "Done. Next: ensure the age key is present, re-seed the canonical"
echo "(ansible-playbook ansible/playbooks/local-canonical.yml), then deploy-stack."
