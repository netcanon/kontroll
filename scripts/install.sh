#!/usr/bin/env bash
# Git-FREE install of a kontroll instance from a release bundle (scripts/make-bundle.sh).
# NO GitHub clone — the bundle IS the code. git is used only LOCALLY (the working tree +
# the local canonical the instance runs from), so the operator needs no git/GitHub
# account. This replaces the "git clone" step in docs/SETUP.md §2; every later step is
# unchanged. See docs/local-source-of-truth.md.
#
# Prereq: bootstrap.sh has installed ansible + git (Stage 0).
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
# here. `kontroll-init --fresh` (epilogue step 1) is the single, tested scaffolder: it copies
# instance.example/ → instance/, mints the key, seeds instance/.sops.yaml, and fills instance.yml.
# A partial copy here (e.g. only fleet.yml) would leave a half-built overlay with no .sops.yaml.

cat <<EOF

==> Installed (git-free) at $DEST. No GitHub involved; no remote configured.
Continue with the COMPOSE-NATIVE flow (host floor = Docker + git; all ansible/sops/age live in the installer image):
  1. cd $DEST
  2. scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --domain <domain>  # scaffold instance/ overlay + mint control key
  3. edit instance/fleet.yml + instance/inventory/hosts.yml + instance/instance.yml   # declare fleet, hosts, mgmt_ip/domain
  4. scripts/kontroll-installer.sh check                       # deploy-stack --check --diff (the mandated dry-run)
  5. scripts/kontroll-installer.sh                             # bootstrap → deploy-stack (the stack comes up; Semaphore :3001)
  6. scripts/kontroll-installer.sh configure-semaphore         # wire Semaphore — RUN AFTER deploy
  7. wire remaining secrets: the GUI "Secrets"/"Keys" dialogs, or sops instance/secrets/<domain>.sops.yml
  (the C10 canonical group, gid 1001, is provisioned by install-prereqs.sh and re-ensured by the launcher — SECURITY.md C10)
  GUI: https://<mgmt-ip>:8443 (onboard-gui) · Semaphore :3001 · Grafana :3002 · API :8444
  (Prereq: scripts/install-prereqs.sh installed Docker + git. See docker/installer/README.md. The legacy
   host-ansible flow — cd ansible && ansible-playbook … — still works after install-prereqs.sh --with-host-ansible.)
Updates: install a newer bundle the same way (state persists), OR — if you later add an
optional git remote — scripts/update.sh (merge) + scripts/backup.sh (offsite). State
(secrets/inventory/fleet/dashboards) is never touched by a code update.
EOF
