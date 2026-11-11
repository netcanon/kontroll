#!/usr/bin/env sh
# scripts/lib/pkg.sh — package-manager + init-system dispatch for the HOST FLOOR (nixflavor-agnostic).
#
# WHY: the compose-native architecture pushes ansible/sops/age/collections into the EPHEMERAL installer IMAGE, so
# the host only needs "Docker + git". The image is intentionally Debian internally — but the HOST an operator runs
# the docs on can be ANY mainstream Linux. The two host-floor scripts (install-prereqs.sh, host-systemd.sh) were
# apt-only, which blocked a Fedora/RHEL/Rocky/Arch/openSUSE operator at step one. This sourced helper detects the
# package manager + init system and dispatches, so the floor installs everywhere with one code path.
#
# NOT install-gating: this runs BEFORE the never-brick generators (gen-requirements/install-collections) and is
# allowed to reach the network (it bootstraps Docker). It never touches the offline/fail-closed install seam, so
# adding network-using, distro-branching logic here does not weaken the BRICK-1 invariant.
#
# POSIX sh (no bashisms) so it sources cleanly under sh or bash. Sets PKG_MGR; provides pkg_refresh / pkg_install /
# svc_enable_now / compose_version / compose_ensure / arch_deb.

# Detect the system package manager → PKG_MGR (apt|dnf|zypper|pacman|unknown). dnf is preferred over its yum alias.
pkg_detect() {
  if command -v apt-get >/dev/null 2>&1; then PKG_MGR=apt
  elif command -v dnf >/dev/null 2>&1; then PKG_MGR=dnf
  elif command -v zypper >/dev/null 2>&1; then PKG_MGR=zypper
  elif command -v pacman >/dev/null 2>&1; then PKG_MGR=pacman
  else PKG_MGR=unknown
  fi
  echo "$PKG_MGR"
}

# Refresh the package index (best-effort; a stale index is not fatal for our floor packages).
pkg_refresh() {
  [ -n "${PKG_MGR:-}" ] || PKG_MGR="$(pkg_detect)"
  case "$PKG_MGR" in
    apt)    DEBIAN_FRONTEND=noninteractive apt-get update -qq ;;
    dnf)    dnf -q makecache >/dev/null 2>&1 || true ;;
    zypper) zypper --non-interactive --gpg-auto-import-keys refresh >/dev/null 2>&1 || true ;;
    pacman) pacman -Sy --noconfirm >/dev/null 2>&1 || true ;;
  esac
}

# pkg_install <pkg>… — install packages non-interactively via the detected PM. Returns non-zero (with a clear
# message) when no supported PM exists, so the caller can fail closed with a manual-install hint.
pkg_install() {
  [ "$#" -gt 0 ] || return 0
  [ -n "${PKG_MGR:-}" ] || PKG_MGR="$(pkg_detect)"
  case "$PKG_MGR" in
    apt)    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf)    dnf install -y -q "$@" ;;
    zypper) zypper --non-interactive install -y "$@" ;;
    pacman) pacman -S --needed --noconfirm "$@" ;;
    *) echo "no supported package manager (apt/dnf/zypper/pacman) — install manually: $*" >&2; return 1 ;;
  esac
}

# svc_enable_now <service> — enable+start a service across init systems, degrading gracefully on a non-systemd /
# non-OpenRC host instead of leaving the operator with a silently-unstarted daemon. Never aborts the caller.
svc_enable_now() {
  _svc="$1"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now "$_svc" >/dev/null 2>&1 || true
  elif command -v rc-update >/dev/null 2>&1; then
    rc-update add "$_svc" default >/dev/null 2>&1 || true
    rc-service "$_svc" start >/dev/null 2>&1 || true
  else
    echo "    no systemctl/rc-update found — start '$_svc' via your init system." >&2
  fi
}

# compose_version [user] — print `docker compose version` and return 0 when compose answers: as root, else, given a
# user other than root, as that user. The launcher (scripts/kontroll-installer.sh) runs compose as the operator, and
# a plugin the operator put in their own ~/.docker/cli-plugins (Docker's per-user install) answers for them but not
# for root, whose docker looks in root's home. runuser runs that plugin as its owner, never as root. Returns 1 when
# neither answers.
compose_version() {
  docker compose version 2>/dev/null && return 0
  [ -n "${1:-}" ] && [ "$1" != root ] && command -v runuser >/dev/null 2>&1 || return 1
  _cv="$(runuser -u "$1" -- docker compose version 2>/dev/null)" || return 1
  echo "$_cv (as $1; root's does not answer)"
}

# compose_ensure [user] — make `docker compose` answer, since the stack and the installer run in compose (I-21). Asked
# first (compose_version: as root, else as <user>), so a host whose compose already answers installs nothing. A host
# with the distribution's Docker (docker.io) has no docker-compose-plugin candidate, since that package is in Docker's
# own repository; installing it unconditionally printed apt's "has no installation candidate", and its `|| true` hid
# the case where compose was missing. On apt it installs the first compose v2 package the sources offer: Docker's
# docker-compose-plugin, Ubuntu's docker-compose-v2, or Debian's docker-compose where its candidate is 2 or later
# (Debian 13 on; before that it is the v1 tool, whose `docker-compose` is not `docker compose`). It asks compose again
# after each install. On dnf, zypper and pacman it installs nothing: where Docker was absent, install-prereqs.sh's
# Docker install brought the plugin with the engine (on dnf; Docker's script refuses openSUSE and Arch). Returns 1,
# saying so and naming the packages, when compose still does not answer; the
# caller decides what that costs.
compose_ensure() {
  _as="${1:-}"
  compose_version "$_as" >/dev/null && return 0
  [ -n "${PKG_MGR:-}" ] || PKG_MGR="$(pkg_detect)"
  if [ "$PKG_MGR" != apt ]; then
    echo "    ERROR: Docker is installed but 'docker compose' does not answer, and this script installs a compose" >&2
    echo "    package only on apt. Install docker-compose-plugin from Docker's own repository" >&2
    echo "    (https://docs.docker.com/engine/install/) with $PKG_MGR, then re-run this script." >&2
    return 1
  fi
  for _pkg in docker-compose-plugin docker-compose-v2 docker-compose; do
    # The policy is read in the C locale: apt translates "Candidate:", so where apt speaks another language the word
    # would never match and every package would be skipped. Plain C, as Ansible's apt module sets: gettext ignores
    # LANGUAGE under the C locale. No pipe into `grep -q`: under the caller's pipefail, a SIGPIPE would read as no
    # candidate (sed reads its input to the end).
    _policy="$(LC_ALL=C apt-cache policy "$_pkg" 2>/dev/null || true)"
    _cand="$(printf '%s\n' "$_policy" | sed -n 's/^ *Candidate: *//p')"
    # `Candidate: (none)` or no entry at all: the sources do not carry it, so apt is not asked (it would print E:).
    case "$_cand" in ""|"(none)") continue ;; esac
    # Debian's docker-compose before Debian 13 is v1. The epoch, if any, is dropped before the major is read.
    if [ "$_pkg" = docker-compose ]; then case "${_cand#*:}" in 0.*|1.*) continue ;; esac; fi
    echo "    'docker compose' does not answer: installing $_pkg…"
    # --no-remove: apt aborts rather than remove a package to make room (an engine that Conflicts with the one this
    # package Depends on), so no engine is swapped under a host whose Docker was already there.
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-remove "$_pkg" \
      && compose_version "$_as" >/dev/null && return 0
  done
  echo "    ERROR: Docker is installed but 'docker compose' does not answer, and no compose v2 package could be" >&2
  echo "    installed from this host's package sources. Install docker-compose-plugin from Docker's own repository" >&2
  echo "    (https://docs.docker.com/engine/install/), or the distribution's compose v2 package (docker-compose-v2 on" >&2
  echo "    Ubuntu; docker-compose on Debian 13 and later), then re-run this script." >&2
  return 1
}

# arch_deb — the Docker/sops release naming for this machine's architecture, derived from `uname -m` (portable;
# `dpkg --print-architecture` only exists on Debian). amd64/arm64/armhf cover the realistic control-node set.
arch_deb() {
  case "$(uname -m)" in
    x86_64|amd64)   echo amd64 ;;
    aarch64|arm64)  echo arm64 ;;
    armv7l|armhf)   echo armhf ;;
    *)              uname -m ;;
  esac
}
