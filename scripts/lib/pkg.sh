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
# svc_enable_now / compose_ensure / arch_deb.

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

# compose_ensure — make `docker compose` answer, since the stack and the installer run in compose (I-21). Asked
# first, so a host whose compose already answers installs nothing. A host with the distribution's Docker (docker.io)
# has no docker-compose-plugin candidate, since that package is in Docker's own repository; installing it
# unconditionally printed apt's "has no installation candidate", and its `|| true` hid the case where compose was
# missing. On apt it installs whichever compose v2 package the sources offer: Docker's docker-compose-plugin, else
# the distribution's docker-compose-v2 (Ubuntu's name). It asks compose again after each install. Returns 1, saying
# so and naming both packages, when compose still does not answer; the caller decides what that costs.
compose_ensure() {
  docker compose version >/dev/null 2>&1 && return 0
  [ -n "${PKG_MGR:-}" ] || PKG_MGR="$(pkg_detect)"
  if [ "$PKG_MGR" = apt ]; then
    for _pkg in docker-compose-plugin docker-compose-v2; do
      # `Candidate: (none)` or no entry at all: the sources do not carry it, so apt is not asked (it would print E:).
      # No pipe into `grep -q`: under the caller's pipefail, a SIGPIPE would read as no candidate.
      _policy="$(apt-cache policy "$_pkg" 2>/dev/null || true)"
      case "$_policy" in *"Candidate: (none)"*|"") continue ;; *"Candidate: "*) ;; *) continue ;; esac
      echo "    'docker compose' does not answer: installing $_pkg…"
      pkg_install "$_pkg" && docker compose version >/dev/null 2>&1 && return 0
    done
  fi
  echo "    ERROR: Docker is installed but 'docker compose' does not answer, and no compose v2 package could be" >&2
  echo "    installed from this host's package sources. Install the distribution's compose v2 package" >&2
  echo "    (docker-compose-v2 on Ubuntu) or docker-compose-plugin from Docker's own repository" >&2
  echo "    (https://docs.docker.com/engine/install/), then re-run this script." >&2
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
