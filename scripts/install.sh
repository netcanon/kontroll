#!/usr/bin/env bash
# Git-FREE install of a kontroll instance from a release bundle (scripts/make-bundle.sh).
# NO GitHub clone — the bundle IS the code. git is used only LOCALLY (the working tree +
# the local canonical the instance runs from), so the operator needs no git/GitHub
# account. This replaces the "git clone" step in docs/SETUP.md §2; every later step is
# unchanged. See docs/local-source-of-truth.md.
#
# Prereq: git on the host. (scripts/install-prereqs.sh installs it, with Docker; both scripts are INSIDE the
# bundle, so extract it once to reach them — docs/install-from-scratch.md, the quickstart's first step.)
# Usage:  scripts/install.sh <bundle.tar.gz> [<install-dir>]      # default dir: ./kontroll
set -euo pipefail
BUNDLE="${1:?usage: install.sh <bundle.tar.gz> [install-dir]}"
DEST="${2:-$PWD/kontroll}"
[ -f "$BUNDLE" ] || { echo "no such bundle: $BUNDLE" >&2; exit 1; }
[ -e "$DEST" ] && { echo "install dir $DEST already exists — refusing to overwrite" >&2; exit 1; }
BUNDLE="$(cd "$(dirname "$BUNDLE")" && pwd)/$(basename "$BUNDLE")"   # absolutise before cd

echo "==> Extracting bundle into $DEST…"
mkdir -p "$DEST" && tar xzf "$BUNDLE" -C "$DEST"

echo "==> Initialising the LOCAL git working tree (no remote — git is internal only)…"
git -C "$DEST" init -q -b main
# A blanket add with NO plaintext gate, on purpose: $DEST did not exist a moment ago and holds exactly what the
# bundle held, and a bundle carries no instance/ (tests/integration/test_make_bundle.py). The blanket adds that
# CAN take an operator's files are gated: scripts/kontroll.sh and ansible/playbooks/local-canonical.yml.
git -C "$DEST" add -A
git -C "$DEST" -c user.name=kontroll -c user.email=kontroll@localhost \
    commit -q -m "kontroll: installed from bundle $(basename "$BUNDLE")"

# The bundle ships NO instance/ overlay (only instance.example/) — do NOT hand-scaffold one
# here. `kontroll-init --fresh` (the scaffold step of the written sequence) is the single, tested scaffolder: it copies
# instance.example/ → instance/, mints the key, seeds instance/.sops.yaml, and fills instance.yml.
# A partial copy here (e.g. only fleet.yml) would leave a half-built overlay with no .sops.yaml.

cat <<EOF

==> Installed (git-free) at $DEST. No GitHub involved; no remote configured.
  Next:
    cd $DEST && sudo bash scripts/install-prereqs.sh
  The whole sequence: docs/install-from-scratch.md, the quickstart at the top.
EOF
