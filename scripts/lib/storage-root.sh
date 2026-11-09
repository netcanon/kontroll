# scripts/lib/storage-root.sh — make a missing storage root root's 0711 on the host, before any bind can make it.
#
# WHY this is a host gesture: deploy-stack.yml makes a missing KONTROLL_STORAGE_ROOT root's 0711 (passable, listed and
# written by nobody else; SECURITY.md C10), but only when it runs where the root is missing. On the installer path the
# installer container binds the root (docker/services/installer.yaml), so Docker has already made it root:root 0755 by
# the time the play looks, and the play leaves a root that exists as it is (I-24, 2026-11-08). install-prereqs.sh runs
# as root on the host before the first installer `up`, so it makes the root here, the way the play would have.
# POSIX sh (sourced under sh or bash); idempotent.

KONTROLL_STORAGE_ROOT_DEFAULT=/var/lib/kontroll

# storage_root_ensure [<root>] — <root> (default /var/lib/kontroll) must be an absolute path other than `/`, as
# deploy-stack's own assert requires; else return 2. A root that exists — a directory, a mount, a link, anything — is
# left exactly as it is (the deploy then judges it and prints the fix). A missing one is made, with each missing
# parent, owner root, group root, mode 0711, as Ansible's `file` would make it. Needs root; returns non-zero, with the
# reason, if it could not make the root.
storage_root_ensure() {
  _root="${1:-$KONTROLL_STORAGE_ROOT_DEFAULT}"
  case "$_root" in
    /*) ;;
    *) echo "storage root '$_root' is not an absolute path; nothing made" >&2; return 2 ;;
  esac
  if [ -z "$(printf '%s' "$_root" | tr -d '/')" ]; then
    echo "storage root '$_root' is the filesystem root; nothing made" >&2; return 2
  fi
  if [ -e "$_root" ] || [ -L "$_root" ]; then
    echo "    storage root $_root is there already: left as it is"
    return 0
  fi
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
