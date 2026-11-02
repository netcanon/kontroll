#!/usr/bin/env bash
# make-bundle-airgap.sh — build the C1 AIR-GAPPED bundle: a launch kit + a `docker save` tar of every kontroll image
# (pulled BY DIGEST), so an air-gapped control node installs with ZERO registry reachability. Producer side runs on a
# CONNECTED machine; the consumer does `docker load` + `./kontroll …`. Design-of-record: report 22 §6.3.
#
# The digest-pin property survives the save/load round-trip (`docker load` preserves the digest), so the loaded images
# are the exact verified bytes — the air-gap node needs no Galaxy, no ghcr, no `docker build`. The kontroll launcher is
# air-gap-aware: it skips the registry pull when the installer image is already present (the loaded one).
#
# REQUIRES a populated docker/images.lock.yml — run AFTER a publish + `gen-image-digests.py --refresh <tag>` + commit.
#
# Usage:  scripts/make-bundle-airgap.sh [<version>] [<out-dir>] [<arch>]
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

VERSION="${1:-$(git describe --tags --always 2>/dev/null || git rev-parse --short HEAD)}"
OUT_DIR="${2:-dist}"; mkdir -p "$OUT_DIR"
ARCH="${3:-$(docker version -f '{{.Server.Arch}}' 2>/dev/null || echo amd64)}"

# The pinned image refs (ref@sha256) from the lock — air-gap REQUIRES every kontroll image pinned (no local build on
# the disconnected node). gen-image-digests.py --env emits `VAR=ref@digest`; strip the VAR= to get the refs.
mapfile -t REFS < <("$PYTHON" scripts/gen-image-digests.py --env | sed 's/^[^=]*=//')
if [ "${#REFS[@]}" -eq 0 ]; then
  echo "make-bundle-airgap: docker/images.lock.yml has no recorded digests — publish, then" >&2
  echo "                    gen-image-digests.py --refresh <tag> + commit the lock first." >&2
  exit 1
fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
kit="kontroll-airgap-${VERSION}-${ARCH}"
stage="$tmp/$kit"; mkdir -p "$stage"

# 1) pull each image by digest for the target arch; 2) docker save them all into one tar (digest-addressed).
echo "Pulling ${#REFS[@]} image(s) by digest for linux/${ARCH}…"
for ref in "${REFS[@]}"; do docker pull --platform "linux/${ARCH}" "$ref"; done
docker save -o "$stage/kontroll-images-${VERSION}-${ARCH}.tar" "${REFS[@]}"

# 3) the launch kit (instance-stripped tree + images.env pins + the kontroll launcher) alongside the images tar,
# unpacked so the consumer just `cd`s in and runs ./kontroll (no second extract step).
PYTHON="$PYTHON" bash scripts/make-launch-kit.sh "$VERSION" "$stage"
tar xzf "$stage/kontroll-launch-${VERSION}.tar.gz" -C "$stage"
rm -f "$stage/kontroll-launch-${VERSION}.tar.gz"

ARCHIVE="${OUT_DIR%/}/${kit}.tar.gz"
tar czf "$ARCHIVE" -C "$tmp" "$kit"
echo "Wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
cat <<MSG
Air-gap install (consumer side — ZERO registry reachability, host floor = Docker + git):
  tar xzf $(basename "$ARCHIVE") && cd ${kit}
  docker load -i kontroll-images-${VERSION}-${ARCH}.tar          # images now local, digest-addressed
  cd kontroll-launch-${VERSION} && ./kontroll fresh-init --mgmt-ip <ip> --domain <domain> --trust-mode separated --break-glass-recipient <age1…>
  (<age1…>: the public half of a break-glass key made on another machine — docs/break-glass.md)
  (the whole sequence: docs/install-from-scratch.md — write ./kontroll where it writes scripts/kontroll-installer.sh)
MSG
