# scripts/lib/canonical-group.sh — provision/verify the C10 canonical's owning group (gid 1001).
#
# WHY this is a shared HOST gesture: the canonical bare repo /srv/kontroll.git is group-gid-1001 (the uid:gid the
# privileged api/onboard-gui write proposals as). The operator WRITES it too — scripts/update.sh,
# scripts/update-from-bundle.sh and an onboard from the CLI LAND `main` there as the operator (a pack and a ref
# transaction through scripts/kontroll-land.py, judged by the promote gate; never a push), beside files uid
# 1001 wrote (its proposals; what a promote leaves) — and can only do that as a member of that group, else the
# write fails "Permission denied" (live-caught 2026-06-29 on the .52 baked prod box, F-CANON; task_51334deb). A
# DEPLOY does not depend on it (the play makes the canonical the operator's before it lands), and the PROMOTE is
# no longer the reason: `sudo kontroll-promote` runs its git as the service uid itself (SECURITY.md C10), and
# without `sudo` an account that is not the service is refused whatever its groups.
# ansible/playbooks/local-canonical.yml provisions the named group + the operator's membership on a
# HOST-DIRECT run, but SKIPS them inside the installer container (KONTROLL_IN_CONTAINER=1 — the published-kit and
# local-build launchers both run the deploy in-container) because a container-local group dies with the ephemeral
# installer and the operator name isn't in the container's /etc/passwd. So the HOST gesture lives here, shared by the
# root host-floor installer (install-prereqs.sh — creates it) and the operator launchers (kontroll.sh /
# kontroll-installer.sh — ensure-or-warn). POSIX sh (no bashisms; sourced under sh or bash); every function is
# idempotent. SECURITY.md C10.

KONTROLL_CANONICAL_GID="${KONTROLL_CANONICAL_GID:-1001}"
KONTROLL_CANONICAL_GROUP="${KONTROLL_CANONICAL_GROUP:-kontroll}"

# canonical_group_satisfied <operator> — 0 iff a group at the canonical gid EXISTS and <operator> is an EFFECTIVE
# member of it (primary or supplementary). Conservative: a usermod'd-but-not-relogged-in member reads as unsatisfied
# (id reflects only the current session), so the launcher's ensure path is idempotently re-tried — harmless.
canonical_group_satisfied() {
  _op="$1"
  getent group "$KONTROLL_CANONICAL_GID" >/dev/null 2>&1 || return 1
  _grp="$(getent group "$KONTROLL_CANONICAL_GID" | cut -d: -f1)"
  id -nG "$_op" 2>/dev/null | tr ' ' '\n' | grep -qx "$_grp"
}

# canonical_group_ensure <operator> [noninteractive] — create the gid-1001 group (named 'kontroll' unless that gid
# already exists under another name) and add <operator> to it. Runs the privileged commands directly when root, else
# via sudo (`sudo -n`, never prompting, when arg 2 is "noninteractive" — for an unattended launcher). Idempotent: the
# groupadd is skipped if a group already holds the gid, the usermod if <operator> is already a member or is root.
# Returns non-zero if it could not complete (no root + no usable sudo, or the command failed) so the caller can warn.
canonical_group_ensure() {
  _op="$1"; _mode="${2:-}"
  if [ "$(id -u)" -eq 0 ]; then
    _sudo=""
  elif command -v sudo >/dev/null 2>&1; then
    if [ "$_mode" = noninteractive ]; then _sudo="sudo -n"; else _sudo="sudo"; fi
  else
    return 1                                   # not root and no sudo — caller warns with the manual one-liner
  fi
  if ! getent group "$KONTROLL_CANONICAL_GID" >/dev/null 2>&1; then
    # shellcheck disable=SC2086  # $_sudo is an intentional 0-or-2-word prefix; must word-split
    $_sudo groupadd -g "$KONTROLL_CANONICAL_GID" "$KONTROLL_CANONICAL_GROUP" >/dev/null 2>&1 || return 1
    echo "    created the gid-$KONTROLL_CANONICAL_GID $KONTROLL_CANONICAL_GROUP group (the C10 canonical's owning group)."
  fi
  _grp="$(getent group "$KONTROLL_CANONICAL_GID" | cut -d: -f1)"
  if [ -n "$_op" ] && [ "$_op" != root ] \
     && ! id -nG "$_op" 2>/dev/null | tr ' ' '\n' | grep -qx "$_grp"; then
    # shellcheck disable=SC2086
    $_sudo usermod -aG "$_grp" "$_op" >/dev/null 2>&1 || return 1
    echo "    added $_op to the $_grp group (effective next login; the canonical is also operator-owned, so the first deploy can write it now)."
  fi
  return 0
}

# canonical_group_ensure_or_warn <operator> — launcher convenience: if already satisfied, silent; else try a
# non-interactive ensure; if that can't run (no root/sudo), print a LOUD, actionable warning with the exact manual
# one-liner so the gap can't silently bite at the operator's next push. NEVER fatal (returns 0) — the install
# proceeds; only a later push of the operator's own beside what uid 1001 wrote needs the group.
canonical_group_ensure_or_warn() {
  _op="$1"
  canonical_group_satisfied "$_op" && return 0
  if canonical_group_ensure "$_op" noninteractive; then return 0; fi
  echo "kontroll: WARNING — the C10 canonical group (gid $KONTROLL_CANONICAL_GID) is missing or '$_op' is not a member." >&2
  echo "          The stack will install, but a later push of yours into the canonical — scripts/update.sh," >&2
  echo "          update-from-bundle.sh, an onboard from the CLI — can fail 'Permission denied' beside what the" >&2
  echo "          service wrote there, until you run this one-time gesture:" >&2
  echo "            sudo groupadd -g $KONTROLL_CANONICAL_GID $KONTROLL_CANONICAL_GROUP; sudo usermod -aG $KONTROLL_CANONICAL_GROUP $_op" >&2
  echo "          then log out/in. (A promote does not need it: run it with sudo.) SECURITY.md C10." >&2
  return 0
}
