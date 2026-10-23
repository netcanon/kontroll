#!/usr/bin/env bash
# entrypoint.sh — the kontroll installer's THIN verb dispatcher. NO provisioning logic lives here: every verb
# shells the EXISTING playbook / script UNCHANGED (synthesis §4.2 — docs/reviews/2026-06-29-compose-native-install/).
# The image + this dispatcher are the entire Phase-1 install glue; the provisioning IS
# ansible/playbooks/{bootstrap,local-canonical,deploy-stack}.yml + scripts/. Run THROUGH A LAUNCHER —
# scripts/kontroll-installer.sh, or ./kontroll in a launch kit — which passes the repository path and the
# operator's identity that docker/services/installer.yaml requires. These are the verbs it forwards:
#
#   fresh-init --mgmt-ip <ip> --domain <domain> --trust-mode separated \
#              --break-glass-recipient <age1…>                           # scaffold instance/ + mint control key
#   check                                                                # the mandated --check --diff dry-run
#   init   (the default when no verb is given)                           # bootstrap → deploy-stack
#   configure-semaphore                                                  # wire a running Semaphore (:3001)
#
set -euo pipefail
# The repo is bound at the SAME path as on the host (installer.yaml working_dir + the repo bind), so the
# in-container `docker compose up` resolves the stack's relative bind sources to real HOST paths. Fall back to the
# baked /repo only if the launcher didn't pass it (e.g. a bare `docker run` for debugging).
REPO="${KONTROLL_REPO_ROOT:-/repo}"
ANSIBLE_DIR="$REPO/ansible"

verb="${1:-init}"
shift || true

case "$verb" in
  init)
    # bootstrap (collections + age/ssh keys, fleet-driven) → deploy-stack, which IMPORTS local-canonical (the C10
    # canonical) then renders docker/.env from the SOPS domains and brings up the compose stack. Extra args
    # (e.g. -e api_privileged=true, -e stack_services='["semaphore","onboard-gui",…]') forward to deploy-stack,
    # the sole consumer; bootstrap takes none.
    cd "$ANSIBLE_DIR"
    ansible-playbook playbooks/bootstrap.yml
    ansible-playbook playbooks/deploy-stack.yml "$@"
    ;;
  check)
    # The CLAUDE.md-mandated --check --diff dry-run before any state-changing apply. The control-plane collections
    # are baked into the image, so this runs STANDALONE (before any bootstrap). deploy-stack imports local-canonical,
    # so the whole control-plane apply is previewed in one pass.
    cd "$ANSIBLE_DIR"
    ansible-playbook playbooks/deploy-stack.yml --check --diff "$@"
    ;;
  fresh-init)
    # Scaffold instance/ from instance.example/ + mint the control age key with show-once bootstrap secrets. Writes
    # instance/ THROUGH the /repo bind and the key THROUGH the host age-dir bind to SOPS_AGE_KEY_FILE
    # (/root/.config/sops/age/keys.txt → host ~/.config/sops/age/keys.txt). No docker socket needed.
    #
    # NOT `exec`: we run as root in here, so everything written through those binds lands on the HOST owned by
    # root — and `instance/` is the operator's own config, the very files the next step tells them to edit. On a
    # fresh VM that meant `$EDITOR instance/fleet.yml` failed with permission denied until they worked out it
    # needed sudo (live-caught, 2026-07-28). MF-1 already injects the invoking operator's identity for exactly
    # this class of problem; it just never covered the fresh-init artifacts. Same numeric-uid discipline as
    # local-canonical.yml: the host operator has no /etc/passwd entry in here, so a NAME cannot resolve.
    python3 "$REPO/scripts/kontroll-init.py" --fresh "$@"
    rc=$?
    if [ -n "${KONTROLL_OPERATOR_UID:-}" ]; then
      gid="${KONTROLL_OPERATOR_GID:-$KONTROLL_OPERATOR_UID}"
      # Best-effort: a chown failure must not fail a scaffold that otherwise succeeded — the operator can still
      # fix ownership by hand, and losing the exit code would hide whether the scaffold itself worked.
      [ -e "$REPO/instance" ] && chown -R "$KONTROLL_OPERATOR_UID:$gid" "$REPO/instance" || true
      key="${SOPS_AGE_KEY_FILE:-/root/.config/sops/age/keys.txt}"
      [ -e "$key" ] && chown "$KONTROLL_OPERATOR_UID:$gid" "$key" || true
      # The key DIRECTORY too: minted 0700 root-owned, it blocks the operator from listing or adding keys later.
      [ -d "$(dirname "$key")" ] && chown "$KONTROLL_OPERATOR_UID:$gid" "$(dirname "$key")" || true
    fi
    exit $rc
    ;;
  configure-semaphore)
    # Wire a RUNNING Semaphore (localhost:3001 via network_mode: host) — run AFTER init. Self-decrypts the admin
    # password from instance/secrets/semaphore.sops.yml (PR #87); the value is never logged.
    exec python3 "$REPO/scripts/configure-semaphore.py" "$@"
    ;;
  shell)
    # Break-glass: an interactive shell in the installer image to debug a failed apply. Ephemeral (run --rm).
    exec /bin/bash "$@"
    ;;
  *)
    # Passthrough — run one play / arbitrary ansible-playbook args (the run-one-play debuggability the synthesis
    # values, e.g. `docker compose run --rm init playbooks/local-canonical.yml`). The verb is the playbook arg.
    cd "$ANSIBLE_DIR"
    exec ansible-playbook "$verb" "$@"
    ;;
esac
