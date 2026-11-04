"""promote — the capability-neutral propose-then-promote anti-drift gate.

The shared spine of every secondary-capability dialog (telemetry, backup, …): a dialog PROPOSES a plan
(apply:false — a pure read; the diff the operator reviews) and later PROMOTES it (apply:true — the audited
data write). `plan_token` hashes the plan's full write-set at propose time; the dialog echoes the token back
at promote time; `verify_token` re-hashes the CURRENT plan and refuses (a 409) if it has drifted — so a
promote can only land the EXACT bytes the operator was shown.

This is a CONSISTENCY / anti-drift gate, NOT an authorization control (docs/observability-onboarding-flow.md
§8): authorization is `require_token` + the fail-closed audit. The token therefore needs no secret/salt/clock
— it is pure (same parts in, same hex out), which is what makes it testable offline and identical across the
propose and promote requests. If adversarial (unforgeable) properties are ever wanted, key it with the server
`api_token` via HMAC; today an operator on the mgmt VLAN behind the bearer token is the trust model.

Capability-neutral by construction: `plan_token(*parts)` hashes arbitrary text, so telemetry's parts (the
rendered `metrics:` block + the regenerated targets) and backup's parts (the `backup:` mapping + the schedule
spec) flow through the IDENTICAL function — the seam's "promote.py verbatim" guarantee.
"""
import hashlib
import hmac


def plan_token(*parts):
    """Stable content hash (64-hex SHA-256) over an ordered set of plan parts — the propose→promote
    anti-drift token. Pure: no salt, no secret, no clock; the same parts always yield the same hex, so the
    token computed when the plan is PROPOSED equals the one recomputed at PROMOTE iff nothing drifted. Each
    part is length-framed before hashing, so no regrouping of one part-set can collide with another
    (plan_token("ab", "c") != plan_token("a", "bc")) — a part boundary is part of the identity. A None part
    hashes as empty (never raises), so an absent optional section is well-defined, not a crash."""
    h = hashlib.sha256()
    for p in parts:
        b = ("" if p is None else str(p)).encode("utf-8")
        h.update(("%d:" % len(b)).encode("ascii"))   # length frame -> unambiguous part boundaries
        h.update(b)
    return h.hexdigest()


def verify_token(token, *parts):
    """True iff `token` is the plan_token of `parts` — i.e. the plan has NOT drifted since it was proposed.
    The promote route calls this and returns 409 on False (the data changed under the operator; re-propose).
    Constant-time compare (defensive hygiene, though this is a consistency gate, not a secret), of UTF-8 bytes. A
    missing, empty, non-string or non-ASCII token is simply a mismatch (False), never an exception — a malformed
    promote is refused, never crashes the route (`hmac.compare_digest` raises on a non-ASCII str)."""
    if not isinstance(token, str) or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), plan_token(*parts).encode("utf-8"))
