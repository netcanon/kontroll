#!/usr/bin/env bash
# install-prereqs.sh — the host floor for a fresh Debian/Ubuntu control node.
#
# COMPOSE-NATIVE (Phase 1, the default): the host floor is just **Docker + git** (+ tar). All of ansible-core,
# the device collections, sops and age now live in the EPHEMERAL installer image (docker/installer/Dockerfile),
# so there is no host ansible/sops/age to version-skew — which is the entire PR #87 fresh-Debian-12 bug class,
# removed structurally. After this, run `scripts/kontroll-installer.sh fresh-init …` (NOT a host ansible-playbook).
# Design-of-record: docs/reviews/2026-06-29-compose-native-install/99-synthesis.md (host floor = "Docker + git",
# MF-5). qemu-guest-agent + the host-systemd residual: scripts/host-systemd.sh.
#
# --with-host-ansible (ROLLBACK / legacy): ALSO install the full host toolchain (ansible-core via pip + sops +
# age + python) so the OLD `cd ansible && ansible-playbook …` flow still works on the host. Keeps a clean exit if
# the container path is ever unavailable. Idempotent: every step is a no-op on a node that already has the package.
#
# Usage:  sudo bash scripts/install-prereqs.sh [--with-host-ansible]
set -euo pipefail

WITH_HOST_ANSIBLE=0
for arg in "$@"; do
  case "$arg" in
    --with-host-ansible) WITH_HOST_ANSIBLE=1 ;;
    *) echo "unknown arg: $arg (usage: install-prereqs.sh [--with-host-ansible])" >&2; exit 2 ;;
  esac
done

# It sources helpers from lib/ beside itself, so it runs from a kontroll checkout or the unpacked launch kit. Piped
# or copied alone (`cat install-prereqs.sh | ssh host sudo bash`) it would die on a bare "No such file": say what is
# missing and where to run it, before anything else (no root needed to learn that).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
for lib in pkg.sh canonical-group.sh; do
  if [ ! -r "$SCRIPT_DIR/lib/$lib" ]; then
    echo "install-prereqs.sh: $SCRIPT_DIR/lib/$lib not found. It needs the scripts/lib/ beside it:" >&2
    echo "run it from a kontroll checkout or the unpacked launch kit (sudo bash scripts/install-prereqs.sh)," >&2
    echo "not piped or copied alone." >&2
    exit 1
  fi
done

if [ "$(id -u)" -ne 0 ]; then
  echo "install-prereqs.sh needs root (it installs system packages). Re-run: sudo bash $0 $*" >&2
  exit 1
fi

# Package-manager + init-system dispatch (nixflavor-agnostic) — apt/dnf/zypper/pacman. The compose-native floor is
# the SAME everywhere ("Docker + git"); only HOW it's installed differs by distro. See scripts/lib/pkg.sh.
# shellcheck source=scripts/lib/pkg.sh
. "$SCRIPT_DIR/lib/pkg.sh"
PKG_MGR="$(pkg_detect)"
if [ "$PKG_MGR" = unknown ]; then
  echo "install-prereqs.sh: no supported package manager (apt/dnf/zypper/pacman) found. Install 'git' + Docker for" >&2
  echo "your distro by hand, then run scripts/kontroll-installer.sh (everything else runs in the installer container)." >&2
  exit 1
fi
. /etc/os-release 2>/dev/null || true
CODENAME="${VERSION_CODENAME:-stable}"
DOCKER_REPO_OS="$([ "${ID:-debian}" = "ubuntu" ] && echo ubuntu || echo debian)"

export DEBIAN_FRONTEND=noninteractive
# apt-only: a freshly-booted cloud node is still running cloud-init's first-boot apt, which holds the dpkg/apt lock
# for a minute or two. Without this the first `apt-get update` fails "Could not get lock" and aborts under `set -e`.
# Wait for the lock instead of racing it (found on a fresh-VM smoke install, 2026-06-19). No-op on other PMs.
[ "$PKG_MGR" = apt ] && echo 'DPkg::Lock::Timeout "600";' > /etc/apt/apt.conf.d/99kontroll-lock-timeout
pkg_refresh

# The COMPOSE-NATIVE host floor: git+tar (scripts/install.sh unpacks the release bundle + git-inits the working
# tree BEFORE any container can bind-mount it — the one-time host gesture, MF-5). curl pulls the Docker installer.
echo "==> $PKG_MGR: the host floor (git, tar, curl)…"
pkg_install git tar curl

echo "==> Docker CE + the compose plugin (the stack — and the installer — run in compose)…"
if command -v docker >/dev/null 2>&1; then
  # Docker present — ensure the apt compose plugin too (older apt installs predate it; the other PMs' Docker ships it).
  { [ "$PKG_MGR" = apt ] && pkg_install docker-compose-plugin; } || true
elif [ "$PKG_MGR" = apt ]; then
  # The apt deb822 path — keyring + repo file MUST match docker/installer/Dockerfile + deploy-stack.yml EXACTLY
  # (docker.asc + a deb822 .sources) — apt errors "Conflicting values for Signed-By" if two seams disagree. The
  # gpg URL is ASCII-armored, usable directly as a .asc keyring (no dearmor).
  pkg_install ca-certificates gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${DOCKER_REPO_OS}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  rm -f /etc/apt/sources.list.d/docker.list      # drop any stale .list an older version of this script wrote
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/${DOCKER_REPO_OS}
Suites: ${CODENAME}
Components: stable
Architectures: $(arch_deb)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -qq
  pkg_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  # dnf/zypper/pacman: Docker's official cross-distro convenience installer (RHEL/Fedora/Rocky/SUSE/Arch). It adds
  # the distro-correct Docker repo + installs docker-ce + the compose plugin in one shot — the universal path.
  echo "    via https://get.docker.com (the official cross-distro Docker installer)…"
  curl -fsSL https://get.docker.com | sh
fi

echo "==> Enabling + starting the Docker service…"
svc_enable_now docker
if ! docker info >/dev/null 2>&1; then
  echo "    WARNING: 'docker info' failed — the Docker daemon isn't running yet. Start it via your init system" >&2
  echo "    (e.g. 'sudo systemctl start docker'), then re-run scripts/kontroll-installer.sh." >&2
fi

# Let the invoking (non-root) operator run docker without sudo — the compose-native installer launcher
# (scripts/kontroll-installer.sh) calls `docker compose` as the operator, so a docker-group membership avoids sudo.
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  usermod -aG docker "${SUDO_USER}" 2>/dev/null \
    && echo "    added ${SUDO_USER} to the docker group (log out/in to take effect)."
fi

# The C10 canonical group (gid 1001): the operator must be a member to WRITE the canonical beside what uid 1001
# wrote there (scripts/update.sh and an onboard from the CLI push `main` as the operator) — else that write fails
# 'Permission denied' (F-CANON, task_51334deb). A promote is not the reason any more: `sudo kontroll-promote` runs
# its git as the service uid.
# local-canonical.yml SKIPS this inside the installer container, so the host floor (here, as root)
# provisions it. Was a printed manual "step 1"; now an idempotent action. See scripts/lib/canonical-group.sh.
echo "==> The C10 canonical group (gid 1001) + the operator's membership…"
# shellcheck source=scripts/lib/canonical-group.sh
. "$SCRIPT_DIR/lib/canonical-group.sh"
canonical_group_ensure "${SUDO_USER:-root}" || \
  echo "    (could not provision the gid-1001 group automatically — see SECURITY.md C10 / docs/install-from-scratch.md)" >&2

# ── OPTIONAL: the legacy host toolchain (rollback) ───────────────────────────────────────────────────────────
if [ "$WITH_HOST_ANSIBLE" -eq 1 ]; then
  echo "==> --with-host-ansible: installing the legacy host toolchain (ansible-core + sops + age + python)…"
  # age: age-keygen (Stage-0 key mint). acl: ansible's unprivileged become path. openssl/jq: token/JSON work.
  # Package names mostly match across distros; a few may differ (note any that fail to install by hand).
  pkg_install openssl acl jq age python3 python3-pip python3-venv \
    || echo "    (a legacy package name may differ on $PKG_MGR — install any missing one by hand)" >&2
  # ansible-core via pip, NOT the distro package: Debian's `ansible` metapackage pins core 2.14 (too old for
  # deploy-stack's ansible.builtin.deb822_repository, core >=2.15) AND preseeds stale community.* collections that
  # SHADOW the project lockfile pins (the PR #87 bug). pip core lands ahead on PATH with NO bundled collections.
  # Pinned to a range; `pip install` (no -U) is a no-op when an in-range core is present, so re-running is idempotent.
  echo "    ansible-core (>=2.16,<2.19) via pip…"
  # PEP-668: Debian's python is externally-managed (needs --break-system-packages); other distros' pip may not have
  # the flag. Try the plain install first, fall back to the override only if it is rejected.
  python3 -m pip install -q "ansible-core>=2.16,<2.19" 2>/dev/null \
    || python3 -m pip install --break-system-packages -q "ansible-core>=2.16,<2.19"
  SOPS_VERSION="v3.13.1"            # keep in sync with bootstrap.yml + docker/installer/Dockerfile
  if ! command -v sops >/dev/null 2>&1; then
    echo "    sops ${SOPS_VERSION} (GitHub release; not in every distro repo)…"
    curl -fsSL "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.$(arch_deb)" -o /usr/local/bin/sops
    chmod 0755 /usr/local/bin/sops
  fi
fi

echo
echo "==> Host floor installed:"
printf '    %s\n' \
  "docker  : $(docker --version 2>/dev/null || echo MISSING)" \
  "compose : $(docker compose version 2>/dev/null || echo MISSING)" \
  "git     : $(git --version 2>/dev/null || echo MISSING)"
if [ "$WITH_HOST_ANSIBLE" -eq 1 ]; then
  printf '    %s\n' \
    "ansible : $(ansible --version 2>/dev/null | head -1 || echo MISSING)" \
    "sops    : $(sops --version 2>/dev/null | head -1 || echo MISSING)" \
    "age     : $(age --version 2>/dev/null || echo MISSING)"
fi
cat <<'NEXT'

==> Done (idempotent — safe to re-run). The C10 canonical group (gid 1001) was provisioned above.
  Next, from your kontroll/ directory — after logging out and back in, if this run added you to the docker group:
    scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --domain <domain> --trust-mode separated --break-glass-recipient <age1…>
  (from a launch kit: ./kontroll in place of scripts/kontroll-installer.sh; <age1…> is the public half of a key you
  make on another machine — docs/break-glass.md)
  The whole sequence, and what --trust-mode decides: docs/install-from-scratch.md, the quickstart at the top.
  (optional) sudo bash scripts/host-systemd.sh            # qemu-guest-agent + the host-systemd residual
NEXT
