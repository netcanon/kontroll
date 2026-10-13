#!/bin/sh
# C10 — the canonical's `update` hook: an ALLOW-LIST. Installed at <canonical>/hooks/update by
# ansible/playbooks/local-canonical.yml (root:root 0755). git runs it once per pushed ref, as the PUSHER:
#   $1 = the ref's full name, $2 = the id it has (all zeros: it does not exist), $3 = the id pushed
#   (all zeros: a delete). A non-zero exit refuses that ref and no other.
#
# What it allows:
#   uid 1001 (the network service)   CREATE refs/heads/proposed/<run id>     — and nothing else: not main, not a
#                                    tag, not refs/replace/*, not a move or a delete of a proposal that exists.
#   any other uid (the operator's    refs/heads/main, and refs/heads/proposed/<run id>     — and nothing else.
#   mirror, root in the installer)
# A push can therefore leave two kinds of ref in the canonical. A release tag, or a tag named like a branch, is
# refused from everyone: every reader names its ref in full, and this keeps the short names unambiguous for a
# person at a shell.
#
# What it does NOT stop: a write that is not a push. `git update-ref`, or a file written under refs/heads/ through
# the :rw mount, runs no hook (the promote is such a write, on purpose). That is SECURITY.md's accepted-risk row
# for an armed box.
#
# Keyed on the uid `id -u` prints; the hook reads no variable of its own, so there is no switch that turns it off.
# It runs as the pusher and in the pusher's environment: it binds a pusher that does not lie about itself (the
# tool's own code, a person's `git push`). A pusher that arranges its own environment is one that can write the ref
# without a push — the paragraph above. If `id` cannot be run, nothing is allowed. A run id is one of the two minted
# shapes (kontroll.canon.valid_run_id is the same rule in Python, and one test feeds both the same samples).
ref="$1"; old="$2"; new="$3"
uid="$(id -u 2>/dev/null)" || uid=""
is_zero() { case "$1" in ''|*[!0]*) return 1 ;; *) return 0 ;; esac; }
is_id() { case "$1" in ''|*[!0-9a-f]*) return 1 ;; esac; [ "${#1}" -eq 40 ] || [ "${#1}" -eq 64 ]; }
H='[0-9a-f]'; D='[0-9]'
is_proposal() {
    case "$1" in
        refs/heads/proposed/$H$H$H$H$H$H$H$H$H$H$H$H) return 0 ;;
        refs/heads/proposed/$D$D$D$D$D$D$D${D}T$D$D$D$D$D${D}Z-$H$H$H$H$H$H) return 0 ;;
    esac
    return 1
}
refuse() { echo "C10: refused $ref — $1" >&2; exit 1; }
[ -n "$uid" ] || refuse "the pusher's uid could not be read (nothing is allowed without it)."
is_id "$old" && is_id "$new" || refuse "the hook was not handed two object ids (nothing is allowed without them)."
is_zero "$new" && refuse "a push never deletes a ref on the canonical."
if [ "$uid" = "1001" ]; then
    is_proposal "$ref" || refuse "uid 1001 may only create refs/heads/proposed/<run id>."
    is_zero "$old"     || refuse "a proposal is created once; uid 1001 may not move one that exists (stage a new run id)."
    exit 0
fi
[ "$ref" = "refs/heads/main" ] && exit 0
is_proposal "$ref" && exit 0
refuse "the canonical holds refs/heads/main and refs/heads/proposed/<run id> and nothing else."
