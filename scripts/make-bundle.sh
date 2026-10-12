#!/usr/bin/env bash
# Build a git-FREE INSTALL BUNDLE: the product code at HEAD (tracked tree — no
# gitignored files, no local/) MINUS this instance's PRIVATE overlay. A fresh
# instance installs from this tarball with NO GitHub clone (scripts/install.sh) —
# the operator needs no git/GitHub account; distribute via GitHub Releases or any
# file transfer. Pairs with the QA-plan §6 dissemination.
#
# The bundle ships the TOOL ONLY: it strips the entire `instance/` overlay (this
# instance's .sops.yaml recipients, encrypted secrets, inventory IPs, fleet,
# instance.yml, dashboards) AND the internal `docs/reviews/` snapshots, and ships the
# public `instance.example/` stub instead. A fresh node scaffolds its OWN `instance/`
# with `scripts/kontroll-init.py --fresh` (its own keys — never the author's), so the
# bundle carries no secret, no recipient, no IP/domain, and no onboarded host.
#
# Usage:  scripts/make-bundle.sh [<version>] [<out-dir>]    # defaults: git-describe, dist/
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:-$(git describe --tags --always 2>/dev/null || git rev-parse --short HEAD)}"
OUT_DIR="${2:-dist}"; mkdir -p "$OUT_DIR"
ARCHIVE="${OUT_DIR%/}/kontroll-${VERSION}.tar.gz"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

git archive --format=tar HEAD | tar -x -C "$tmp"        # tracked files at HEAD only
# Ship NO instance overlay (recipients, encrypted secrets, inventory IPs, fleet, dashboards)
# and no internal review snapshots — a fresh node scaffolds its own via `kontroll-init --fresh`.
rm -rf "$tmp"/instance "$tmp"/docs/reviews
tar czf "$ARCHIVE" -C "$tmp" .

echo "Wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo "Bundle = the TOOL + instance.example/ stub; NO instance/ overlay, NO secrets, NO recipients,"
echo "         NO inventory/IPs, NO review snapshots, NO git history. A fresh node scaffolds its own overlay."
echo "Install on a fresh node: docs/install-from-scratch.md, the quickstart's first step (\"a release bundle\")."
