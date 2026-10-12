#!/usr/bin/env sh
# kontroll — the bundle-as-compose launcher (C1). The single host gesture a fresh control node uses when installing
# from PUBLISHED, digest-pinned images (the launch kit) instead of a local build. Host floor = Docker + git ONLY —
# no host python/ansible/sops/age (those live in the pulled installer image). This is the published-path sibling of
# scripts/kontroll-installer.sh (which BUILDS the image locally from the repo); the only difference is:
#   local-build:  docker compose … run --build  init      # builds docker/installer/Dockerfile
#   published:    docker pull <each image, by its ghcr @sha256 digest> ; docker compose … run init   # nothing is built:
#                 the image is present (`--no-build` is not a valid `run` flag on every compose version)
# Design-of-record: docs/reviews/2026-06-29-compose-native-install/22-distribution-images.md §6 (bundle-as-compose).
# scripts/make-launch-kit.sh copies this to the launch-kit ROOT as `kontroll` (it lives at scripts/kontroll.sh in the
# repo because scripts/kontroll/ is the Python service package — the name would collide).
#
# Usage (verbs mirror docker/installer/entrypoint.sh):
#   ./kontroll fresh-init --mgmt-ip <ip> --domain <domain> --trust-mode separated   # scaffold instance/ + the control key
#   $EDITOR instance/fleet.yml instance/inventory/hosts.yml instance/instance.yml
#   ./kontroll check                                         # the mandated --check --diff dry-run
#   ./kontroll init                                          # bootstrap → deploy-stack (pulls runner/vector by digest)
#   ./kontroll configure-semaphore                          # wire a running Semaphore (:3001)
set -eu

# Launch-kit / repo root (this script sits at the kit root in a published kit, or scripts/ in the repo). Resolve to
# the tree that holds docker/services/installer.yaml + docker/images.lock.yml.
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/docker/services/installer.yaml" ]; then
  ROOT="$HERE"                              # launch-kit layout: kontroll sits at the tree root
elif [ -f "$HERE/../docker/services/installer.yaml" ]; then
  ROOT="$(cd "$HERE/.." && pwd)"            # repo layout: scripts/kontroll.sh
else
  echo "kontroll: cannot locate docker/services/installer.yaml (run from a launch kit or the repo root)." >&2
  exit 1
fi
cd "$ROOT"

command -v docker >/dev/null 2>&1 || {
  echo "kontroll: docker not found — install Docker (the host floor is Docker + git)." >&2; exit 1; }

# The digest pins. images.env is rendered at kit-build time by scripts/make-launch-kit.sh (gen-image-digests.py
# --env), so the host needs NO python to read them. Each line is VAR=ref@sha256:… — Docker verifies the digest on pull.
IMAGES_ENV="$ROOT/images.env"
if [ ! -f "$IMAGES_ENV" ]; then
  echo "kontroll: $IMAGES_ENV not found — this tree is not pinned to published images." >&2
  echo "          Build a pinned launch kit with scripts/make-launch-kit.sh AFTER a tagged publish, or use" >&2
  echo "          scripts/kontroll-installer.sh to build the installer image locally from the repo." >&2
  exit 1
fi
# Source the digest pins, tolerant of a CRLF images.env (a Windows build host's gen-image-digests.py emits \r\n;
# make-launch-kit strips it now, but be defensive — a trailing \r yields "invalid reference format" on pull). Export
# every KONTROLL_*_IMAGE for compose interpolation + the host pre-pull below.
while IFS='=' read -r _k _v; do
  case "$_k" in
    KONTROLL_*_IMAGE) export "$_k=$(printf '%s' "$_v" | tr -d '\r')" ;;
  esac
done < "$IMAGES_ENV"
[ -n "${KONTROLL_INSTALLER_IMAGE:-}" ] || {
  echo "kontroll: KONTROLL_INSTALLER_IMAGE unset in images.env (the installer image is not published)." >&2; exit 1; }

# MF-1: the host operator's identity, passed into the container — the playbooks chown the canonical + render
# docker/.env to THIS uid/gid, not the in-container root. Identical to scripts/kontroll-installer.sh.
export KONTROLL_REPO_ROOT="$ROOT"
KONTROLL_OPERATOR_USER="$(id -un)"; export KONTROLL_OPERATOR_USER
KONTROLL_OPERATOR_UID="$(id -u)";   export KONTROLL_OPERATOR_UID
KONTROLL_OPERATOR_GID="$(id -g)";   export KONTROLL_OPERATOR_GID
export KONTROLL_OPERATOR_HOME="$HOME"

# The :rw host binds must EXIST as operator-owned dirs before docker mounts them (a missing source is auto-created
# root-owned, which then blocks the operator). age + ssh = where bootstrap/fresh-init mint keys.
mkdir -p "$HOME/.config/sops/age" "$HOME/.ssh" "$HOME/.config/semaphore"
chmod 0700 "$HOME/.config/sops/age" "$HOME/.ssh" 2>/dev/null || true

# The C10 canonical group (gid 1001): the in-container deploy (KONTROLL_IN_CONTAINER=1) SKIPS provisioning it, so
# without this the operator can't later manage uid-1001-pushed proposal refs and promote fails 'Permission denied'
# (F-CANON, task_51334deb). Ensure it here (sudo if available) or print the exact one-liner; never fatal.
if [ -f "$ROOT/scripts/lib/canonical-group.sh" ]; then
  . "$ROOT/scripts/lib/canonical-group.sh"
  canonical_group_ensure_or_warn "$KONTROLL_OPERATOR_USER"
fi

# The verb, read here because the capture below is not for every one of them.
verb="${1:-init}"

# The C10 canonical is seeded from THIS working tree's git (local-canonical adds a `local` remote to it + pushes). A
# launch kit is a tarball extract with NO .git, so git-init it (the step scripts/install.sh does for the bundle path)
# and capture the current tree — including a freshly-scaffolded instance/ — so the canonical seed has a HEAD to push.
# Operator-owned, local-only, no remote. Idempotent: re-commits only when the tree changed (e.g. after fresh-init).
# `shell` deploys nothing and captures nothing. It is the break-glass verb, and the only way to the image's sops on a
# host that has none — so a refusal of the gate below must not lock it out.
if [ "$verb" != shell ]; then
  # A GIT_DIR exported in the operator's shell (some dotfile set-ups do) would send every git call below to THAT
  # repository instead of this directory's; the others would put this one's index or objects somewhere else.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR
  # Its OWN repository. A kit extracted somewhere inside another repository (a home directory kept under git) is
  # "inside a work tree" too, and `git add -A` there stages THAT repository. So the question is whether this
  # directory is the top of a repository, not whether it is inside one.
  if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
     || [ -n "$(git -C "$ROOT" rev-parse --show-cdup 2>/dev/null)" ]; then
    git -C "$ROOT" init -q
    git -C "$ROOT" config user.email "kontroll@localhost"
    git -C "$ROOT" config user.name "kontroll"
    git -C "$ROOT" config commit.gpgsign false
  fi
  if ! git -C "$ROOT" rev-parse HEAD >/dev/null 2>&1 || [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    # This is the FIRST capture of a scaffolded instance/ on a launch-kit box (the deploy's own commit then finds a
    # clean tree and never runs), so the plaintext gate has to stand here too. It needs sh, git and the POSIX base
    # tools every distribution ships (find, awk, tr, wc, cat) — still no python and no sops on the host. No existence
    # test on purpose: this is a control, and a kit without it must refuse, not skip.
    sh "$ROOT/scripts/lib/plaintext-guard.sh" "$ROOT" || {
      echo "kontroll: refusing to capture the tree — see the line(s) above. This run committed nothing." >&2
      echo "kontroll: a file named there is moved out of this directory, or fixed as docs/install-from-scratch.md" >&2
      echo "          (\"REFUSING to commit\") says for its rule. With no file named, nothing was checked: the same" >&2
      echo "          section says why. \`./kontroll shell\` still runs, and has sops." >&2
      exit 1
    }
    git -C "$ROOT" add -A
    git -C "$ROOT" -c core.fileMode=false commit -qm "kontroll launch-kit tree" >/dev/null 2>&1 || true
  fi
fi

# init/check run deploy-stack — force use_published_images=true so it pulls the runner/vector by digest (the kit ships
# a digest-pinned docker/images.lock.yml) AND bake_code=true so the api/onboard-gui services run the BAKED code from
# the published control image at /opt/kontroll (Phase B Rung 3 — a minimized launch kit has NO api/gui source to run
# from the /repo clone; make-launch-kit.sh strips it). Both appended AFTER any operator -e so an explicit override
# still wins. (The repo-based local-build path, scripts/kontroll-installer.sh, defaults both off — byte-identical.)
if [ $# -gt 0 ]; then shift; fi
case "$verb" in
  init|check) set -- "$verb" "$@" -e use_published_images=true -e bake_code=true ;;
  *)          set -- "$verb" "$@" ;;
esac

# Pre-pull the published images on the HOST so deploy-stack's in-container `docker compose up` finds them already
# present (compose pulls only-if-missing). The packages are public, so the pull is anonymous — no `docker login` on
# the host (one is needed only while a package is private, when the credential-less installer container could not
# pull for itself). The installer is always pulled (needed to RUN any verb); init/check also pre-pull the
# runner/vector (they deploy the stack). Skips images already present (idempotent; air-gap: `docker load`ed images
# are present, no registry reached). Docker verifies each @sha256: digest on pull (L2b).
_imgs="$KONTROLL_INSTALLER_IMAGE"
case "$verb" in init|check) _imgs="$_imgs ${KONTROLL_CONTROL_IMAGE:-} ${KONTROLL_VECTOR_IMAGE:-}" ;; esac
for _img in $_imgs; do
  if docker image inspect "$_img" >/dev/null 2>&1; then
    echo "kontroll: $_img already present — not pulling." >&2
  else
    docker pull "$_img"
  fi
done

# Run the installer with a plain `run` — the image is present from the pull above, so the build: block never fires
# (`compose run` builds only when the image is ABSENT; `--no-build` is NOT a valid `run` flag on all compose versions).
exec docker compose -f docker/services/installer.yaml run --rm init "$@"
