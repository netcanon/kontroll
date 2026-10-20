#!/usr/bin/env bash
# kontroll-installer.sh — the compose-native installer LAUNCHER (Phase 1). The single entry point a fresh control
# node uses after install-prereqs.sh: it derives the invoking operator's identity (MF-1 — the playbooks chown the
# canonical + render docker/.env to THIS user, not the in-container root), ensures the host bind targets exist, and
# runs the ephemeral installer service. Host floor = Docker + git only; all ansible/sops/age live in the image.
# Design-of-record: docs/reviews/2026-06-29-compose-native-install/99-synthesis.md (Phase A).
#
# Usage:
#   scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --domain <domain> --trust-mode separated   # scaffold instance/
#   $EDITOR instance/fleet.yml instance/inventory/hosts.yml instance/instance.yml
#   scripts/kontroll-installer.sh check                                         # the mandated --check --diff dry-run
#   scripts/kontroll-installer.sh                                               # bootstrap → deploy-stack (verb 'init')
#   scripts/kontroll-installer.sh configure-semaphore                          # wire a running Semaphore (:3001)
#   scripts/kontroll-installer.sh init -e api_privileged=true \
#       -e '{"stack_services":["semaphore","homepage","prometheus","grafana","api","onboard-gui"]}'
#
set -euo pipefail

# Repo root (this script lives in scripts/). Bound into the container as /repo; the installer service binds it by
# this ABSOLUTE path so it never depends on compose's relative project-dir resolution.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

command -v docker >/dev/null 2>&1 || { echo "docker not found — run scripts/install-prereqs.sh first (the host floor is Docker + git)." >&2; exit 1; }

# MF-1: the host operator's identity, passed into the container.
export KONTROLL_REPO_ROOT="$REPO_ROOT"
export KONTROLL_OPERATOR_USER; KONTROLL_OPERATOR_USER="$(id -un)"
export KONTROLL_OPERATOR_UID;  KONTROLL_OPERATOR_UID="$(id -u)"
export KONTROLL_OPERATOR_GID;  KONTROLL_OPERATOR_GID="$(id -g)"
export KONTROLL_OPERATOR_HOME="$HOME"

# The :rw host binds must EXIST as operator-owned dirs before docker mounts them (a missing bind source is
# auto-created root-owned, which would then block the operator). age + ssh = where bootstrap/fresh-init mint keys.
mkdir -p "$HOME/.config/sops/age" "$HOME/.ssh" "$HOME/.config/semaphore"
chmod 0700 "$HOME/.config/sops/age" "$HOME/.ssh" 2>/dev/null || true

# The C10 canonical group (gid 1001): the in-container deploy (KONTROLL_IN_CONTAINER=1) SKIPS provisioning it, so
# without this a later landing of the operator's own on the canonical (scripts/update.sh, an onboard from the CLI)
# fails 'Permission denied' beside what uid 1001 wrote there (F-CANON, task_51334deb). A promote is not the
# reason any more: `sudo kontroll-promote` runs its git as the service uid. Ensure it here (sudo if available) or
# print the exact one-liner; never fatal.
if [ -f "$REPO_ROOT/scripts/lib/canonical-group.sh" ]; then
  # shellcheck source=scripts/lib/canonical-group.sh
  . "$REPO_ROOT/scripts/lib/canonical-group.sh"
  canonical_group_ensure_or_warn "$KONTROLL_OPERATOR_USER"
fi

# --build so a bundle update that changes the Dockerfile (new ansible-core, new collections, a new runtime dep) is
# reflected — a plain `run` reuses the cached image and would silently run stale glue. Near-noop when nothing changed
# (compose re-evaluates layers, almost all cached). --rm: ephemeral, no standing container (synthesis §5).
exec docker compose -f docker/services/installer.yaml run --build --rm init "$@"
