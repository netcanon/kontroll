#!/usr/bin/env bash
# Stage 0 — the irreducible prerequisite. Thin shim: delegates to scripts/install-prereqs.sh, the ONE
# host-floor installer (Docker + compose + git; `--with-host-ansible` adds the legacy host toolchain:
# ansible-core, sops, age). Kept so the documented `./bootstrap.sh` keeps working; the logic lives in
# scripts/install-prereqs.sh. Idempotent. See docs/SETUP.md §1.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# Already root (`sudo ./bootstrap.sh`): run it here, so the outer sudo's SUDO_UID and SUDO_USER, the operator, reach
# it. Through a second sudo, invoked by root, they would name root (I-18).
if [ "$(id -u)" -eq 0 ]; then
  exec bash "${HERE}/scripts/install-prereqs.sh" "$@"
fi
exec sudo bash "${HERE}/scripts/install-prereqs.sh" "$@"
