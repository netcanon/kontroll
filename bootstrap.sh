#!/usr/bin/env bash
# Stage 0 — the irreducible prerequisite. Thin shim: delegates to scripts/install-prereqs.sh, the ONE
# host-floor installer (Docker + compose + git; `--with-host-ansible` adds the legacy host toolchain:
# ansible-core, sops, age). Kept so the documented `./bootstrap.sh` keeps working; the logic lives in
# scripts/install-prereqs.sh. Idempotent. See docs/SETUP.md §1.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec sudo bash "${HERE}/scripts/install-prereqs.sh" "$@"
