# scripts/lib/storage-root.sh — make a missing storage root root's 0711 on the host, before any bind can make it.
#
# WHY this is a host gesture: deploy-stack.yml makes a missing storage root (its `kontroll_storage_root`) root's 0711
# (passable, listed and written by nobody else; SECURITY.md C10), but only when it runs where the root is missing. On
# the installer path the installer container binds the root (docker/services/installer.yaml interpolates
# KONTROLL_STORAGE_ROOT from the launcher's environment), so Docker has already made it root:root 0755 by the time the
# play looks, and the play leaves a root that exists as it is (I-24, 2026-11-08). install-prereqs.sh, run as root on
# the host before the first installer `up`, makes the root here, the way the play would have; where it was not run
# first, Docker's 0755 root is what there is, and docs/SETUP.md "Pre-provisioning storage" closes it.
# POSIX sh (sourced under sh or bash); idempotent.

KONTROLL_STORAGE_ROOT_DEFAULT=/var/lib/kontroll

# storage_root_ensure [<root>] — <root> (default /var/lib/kontroll) must be an absolute path other than `/`, as
# deploy-stack's own assert requires; else return 2. A root that exists — a directory, a mount, a link, anything — is
# left exactly as it is, and what it is is said: a deploy that brings up `api` or `onboard-gui` refuses one that is
# not root's alone or that others may not pass through, and prints the fix, but it passes a root:root 0755 one (what
# Docker makes), so for that one the line that closes it is printed here. A missing one is made, with each missing
# parent, owner root, group root, mode 0711, as Ansible's `file` would make it; but not under a directory that is not
# root's, or that others can write without the sticky bit, each judged where its links lead: there a directory made
# here could be swapped for a link before the `chown` and `chmod` below, which follow one. That returns 3 and makes
# nothing.
# Needs root; returns 1, with the reason, if it could not make the root.
storage_root_ensure() {
  _root="${1:-$KONTROLL_STORAGE_ROOT_DEFAULT}"
  case "$_root" in
    /*) ;;
    *) echo "storage root '$_root' is not an absolute path; nothing made" >&2; return 2 ;;
  esac
  if [ -z "$(printf '%s' "$_root" | tr -d '/')" ]; then
    echo "storage root '$_root' is the filesystem root; nothing made" >&2; return 2
  fi
  if [ -L "$_root" ]; then
    echo "    storage root $_root is there already (a link): left as it is"
    return 0
  fi
  if [ -e "$_root" ]; then
    _is="$(stat -c '%u:%g %a' -- "$_root" 2>/dev/null)" || _is='?'
    echo "    storage root $_root is there already ($_is): left as it is"
    _mode="${_is##* }"
    case "$_is" in
      '?') ;;
      0:*) if [ -d "$_root" ] && [ $(( 0$_mode & 022 )) -eq 0 ] && [ $(( 0$_mode & 044 )) -ne 0 ]; then
             echo "    others can list it; the deploy passes it as it is, and root's 0711 closes it:" \
                  "sudo chmod 0711 $_root"
           fi ;;
    esac
    return 0
  fi
  # The directories on the way that are there come first; none is changed. Each, and every directory it lies in once
  # its links are followed (where `cd -P` lands), must be root's and writable by nobody else, or sticky (like /tmp),
  # so that what is made below stays where it was made: an account that owns one could rename what is made in it, and
  # put a link in its place, as could one that may write one that is not sticky.
  _path=''
  _rest="${_root#/}"
  while [ -n "$_rest" ]; do
    _part="${_rest%%/*}"
    case "$_rest" in */*) _rest="${_rest#*/}" ;; *) _rest='' ;; esac
    [ -n "$_part" ] || continue
    _path="$_path/$_part"
    { [ -e "$_path" ] || [ -L "$_path" ]; } || break
    [ -d "$_path" ] || continue   # not a directory: the mkdir below fails on it and says so
    _phys="$(cd -P -- "$_path" 2>/dev/null && pwd -P)" || {
      echo "storage root: could not follow $_path to where it leads; nothing made" >&2; return 3; }
    _dir=''
    _left="${_phys#/}"
    while :; do
      _st="$(stat -c '%u %a' -- "${_dir:-/}" 2>/dev/null)" || {
        echo "storage root: could not look at ${_dir:-/}; nothing made" >&2; return 3; }
      _u="${_st% *}"
      _m="${_st#* }"
      if [ "$_u" != 0 ]; then
        echo "storage root: ${_dir:-/} is uid $_u's, so what is made under it could be swapped before it is made" \
             "root's; nothing made" >&2
        return 3
      fi
      if [ $(( 0$_m & 01000 )) -eq 0 ] && [ $(( 0$_m & 022 )) -ne 0 ]; then
        echo "storage root: ${_dir:-/} is writable by others (mode $_m, not sticky), so what is made in it could be" \
             "swapped before it is made root's; nothing made" >&2
        return 3
      fi
      [ -n "$_left" ] || break
      _dir="$_dir/${_left%%/*}"
      case "$_left" in */*) _left="${_left#*/}" ;; *) _left='' ;; esac
    done
  done
  _made=''
  _path=''
  _rest="${_root#/}"
  while [ -n "$_rest" ]; do
    _part="${_rest%%/*}"
    case "$_rest" in */*) _rest="${_rest#*/}" ;; *) _rest='' ;; esac
    [ -n "$_part" ] || continue
    _path="$_path/$_part"
    if [ ! -e "$_path" ] && [ ! -L "$_path" ]; then
      # chown before chmod, and `g-s` after it: a directory made under a setgid parent inherits the setgid bit, which
      # a numeric chmod leaves on a directory; Ansible's `file` would leave exactly 0711.
      { mkdir -m 0711 -- "$_path" && chown 0:0 -- "$_path" && chmod 0711 -- "$_path" && chmod g-s -- "$_path"; } || {
        echo "storage root: could not make $_path root's 0711" >&2; return 1; }
      _made="$_made $_path"
    fi
  done
  echo "    made the storage root, root's 0711:$_made"
}
