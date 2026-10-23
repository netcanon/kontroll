#!/usr/bin/env bash
# Non-git DR SNAPSHOT of instance STATE → a portable tarball you can restore ANYWHERE
# (USB, S3, another box) with no git at all. This is the disaster-recovery owner now
# that the *instance* (not GitHub) is the source of truth (docs/local-source-of-truth.md).
#
# Snapshots exactly the config/state-manifest.yml paths — the single boundary, so this
# can never drift from what "state" means. Contents mirror the repo: SOPS-ENCRYPTED
# secrets (safe to store) + inventory (holds lab IPs — keep the archive on trusted
# media). No age key is included by design; a restore brings one separately (the control
# key's off-box copy, or the offline break-glass key — docs/break-glass.md), so a leaked
# snapshot alone can't decrypt anything.
#
# Usage:  scripts/state-snapshot.sh [<output-dir>]     # default: current dir
# Restore: scripts/state-restore.sh <snapshot.tar.gz>
set -euo pipefail
cd "$(dirname "$0")/.."
# Layer 0: tee this run to a persistent, 30-day-pruned log (docs/logging-architecture.md §3).
if [ -f scripts/lib/run-log.sh ]; then . scripts/lib/run-log.sh; run_log_init "state-snapshot"; fi
OUT_DIR="${1:-.}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${OUT_DIR%/}/kontroll-state-${TS}.tar.gz"

# Resolve the manifest's state paths (globs, dirs, plain files) to what actually exists.
mapfile -t PATHS < <(python3 - <<'PY'
import glob, os, yaml
m = yaml.safe_load(open("config/state-manifest.yml"))
out = []
for pat in m.get("state_paths", []):
    hits = sorted(glob.glob(pat))
    if hits:
        out += hits
    elif os.path.exists(pat):
        out.append(pat)
print("\n".join(p for p in out if os.path.exists(p)))
PY
)

if [ "${#PATHS[@]}" -eq 0 ]; then
  echo "No state paths matched config/state-manifest.yml — nothing to snapshot." >&2
  exit 1
fi

tar czf "$ARCHIVE" "${PATHS[@]}"
echo "Wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo "Captured ${#PATHS[@]} state path(s):"
printf '  - %s\n' "${PATHS[@]}"
echo "NOTE: SOPS-encrypted secrets + inventory IPs inside; no age key is — a restore needs"
echo "      the control key's off-box copy or the break-glass key (docs/break-glass.md)."
echo "      Store this archive on trusted/offsite media."
