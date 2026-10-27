#!/usr/bin/env bash
# make-launch-kit.sh — build the C1 BUNDLE-AS-COMPOSE launch kit: the tree a fresh control node needs to install from
# PUBLISHED, digest-pinned images (no git clone, no `docker build`). Distinct from make-bundle.sh, which ships the FULL
# source tree for a LOCAL build; this kit is the SAME instance-stripped tree PLUS the rendered digest pins (images.env)
# PLUS the `kontroll` launcher, so install = `./kontroll fresh-init …` → `./kontroll init` against the ghcr images.
# Design-of-record: docs/reviews/2026-06-29-compose-native-install/22-distribution-images.md §6 (distribution format).
#
# PINNING: the digests come from docker/images.lock.yml via gen-image-digests.py --env. A real kit MUST be pinned —
# run AFTER a tagged publish + `gen-image-digests.py --refresh <tag>` + commit. `--allow-unpinned` builds an UNPINNED
# skeleton (empty images.env; the launcher refuses to run until refreshed) for assembly testing / air-gap staging.
#
# Usage:  scripts/make-launch-kit.sh [<version>] [<out-dir>] [--allow-unpinned]
set -euo pipefail
cd "$(dirname "$0")/.."

ALLOW_UNPINNED=0
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-unpinned) ALLOW_UNPINNED=1 ;;
    *) POS+=("$1") ;;
  esac
  shift
done

VERSION="${POS[0]:-$(git describe --tags --always 2>/dev/null || git rev-parse --short HEAD)}"
OUT_DIR="${POS[1]:-dist}"; mkdir -p "$OUT_DIR"
KIT="kontroll-launch-${VERSION}"
ARCHIVE="${OUT_DIR%/}/${KIT}.tar.gz"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
dst="$tmp/$KIT"; mkdir -p "$dst"

# The instance-stripped tracked tree at HEAD (same strip as make-bundle.sh: NO instance/ overlay = no recipients /
# secrets / inventory IPs / fleet, and NO internal review snapshots). A fresh node scaffolds its own instance/.
git archive --format=tar HEAD | tar -x -C "$dst"
# Phase B Rung 3 — STRIP the BAKED code. api/ + gui/ run from the PUBLISHED, digest-pinned control image at
# /opt/kontroll (NOT from the kit-seeded canonical), so the kit needs no api/gui source; tests/ + the dev trees are
# never read at install/promote/deploy-stack runtime (adversarially verified — docs/reviews/2026-06-29-phase-b-baked-
# code/99-synthesis.md §1). The closure that DOES run from the canonical/kit STAYS WHOLE: scripts/ (the deploy-stack
# generators + the C10 promote slice + FIX-M9's gen-requirements) + ansible/ + the data registries + config/docker/
# prometheus + instance.example. Because a minimized kit has NO source to local-build, the `kontroll` launcher forces
# use_published_images=true + bake_code=true (the services run baked). Pinned by tests/unit/test_launch_kit.py.
rm -rf "$dst"/instance "$dst"/docs/reviews \
       "$dst"/api "$dst"/gui "$dst"/tests "$dst"/.github "$dst"/.claude

# Ship the WORKING-TREE docker/images.lock.yml (the same source gen-image-digests reads for images.env below), NOT
# git-archive HEAD's — so a `gen-image-digests.py --refresh <tag>` that hasn't been committed yet still produces a kit
# whose lock + images.env AGREE. deploy-stack reads the LOCK (not images.env) for its digest assert, so a kit with a
# pinned images.env but a null HEAD lock fails 'no recorded digest' at install (dogfood-caught 2026-06-29). For a
# committed lock these are identical; this just removes the must-commit-the-lock-first footgun.
cp docker/images.lock.yml "$dst/docker/images.lock.yml"

# The digest pins, rendered to a shell-sourceable images.env so the HOST launcher needs no python (host floor =
# Docker + git only). Each line is VAR=ref@sha256:… (KONTROLL_{CONTROL,VECTOR,INSTALLER}_IMAGE) — Docker verifies on pull.
# PYTHON is the build-host interpreter (default python3); a test/CI may override it to the exact pyyaml-bearing venv.
# `tr -d '\r'`: a Windows build host's python emits \r\n, which would leave a trailing CR in each ref → "invalid
# reference format" when the launcher pulls it (dogfood-caught 2026-06-29). Normalize to LF so the kit is host-agnostic.
PYTHON="${PYTHON:-python3}"
images_env="$("$PYTHON" scripts/gen-image-digests.py --env | tr -d '\r')"
if [ -z "$images_env" ]; then
  if [ "$ALLOW_UNPINNED" -eq 0 ]; then
    echo "make-launch-kit: docker/images.lock.yml has no recorded digests — publish, then" >&2
    echo "                 gen-image-digests.py --refresh <tag> + commit the lock first," >&2
    echo "                 or pass --allow-unpinned to build an (unrunnable) skeleton." >&2
    exit 1
  fi
  echo "make-launch-kit: WARNING — no digests recorded; building an UNPINNED skeleton (the launcher refuses to run)." >&2
fi
printf '%s\n' "$images_env" > "$dst/images.env"

# The launcher at the kit ROOT, named `kontroll` (it lives at scripts/kontroll.sh in the repo because scripts/kontroll/
# is the Python service package — the bare name would collide).
cp scripts/kontroll.sh "$dst/kontroll"
chmod 0755 "$dst/kontroll"

tar czf "$ARCHIVE" -C "$tmp" "$KIT"
echo "Wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo "Launch kit = instance-stripped tree + images.env (digest pins) + the kontroll launcher; NO instance/, NO secrets."
echo "Install on a fresh node (host floor = Docker + git):"
echo "  tar xzf $(basename "$ARCHIVE") && cd $KIT && ./kontroll fresh-init --mgmt-ip <ip> --domain <domain> --trust-mode separated --break-glass-recipient <age1…>"
echo "  (<age1…>: the public half of a break-glass key made on another machine — docs/break-glass.md)"
echo "  (the whole sequence: docs/install-from-scratch.md — write ./kontroll where it writes scripts/kontroll-installer.sh)"
