"""service/_validate — the server-side knob VALIDATOR registry: one validator per knob `type`, the server-side
twin of Phase-3's client type→widget table. THE descriptor is the contract; the widget is convenience; THIS is
the guard. Every widened/free-form knob value (a bounded int, a pattern-checked text, an ipv4, a hostname) is
re-checked here against its descriptor before it can reach a rendered module.yml block — so widening the GUI's
widget vocabulary (Phase 3) never weakens the closed-allow-list config-injection guarantee the original
`allowed`-only check gave (design `docs/reviews/2026-06-19-gui-paradigm/21-design-actionable-config-plane.md`
§3/§4/§10; the M3/S-1 prerequisite that gates the widened knobs).

Shared by `_blockwrite.validate_params` (telemetry/logging Stage-1 params) and — once Phase 3 lands — any
capability `build_plan` that re-validates a widened knob. Validators return an error STRING (the route turns it
into a 422) or None, and NEVER raise / sys.exit — a service degrades, it does not crash the in-process Flask
worker. They are FAIL-CLOSED: an unknown `type`, or a `text` knob with no `pattern`, is REJECTED, never silently
accepted (an unvalidated free field would re-open the very injection hole the closed allow-list closed —
DO-NOT-BUILD, design 21 §10). The per-type checks live HERE (not inlined per caller) so the server-side guard is
one source of truth that the client type→widget table mirrors key-for-key.
"""
import ipaddress
import re

# The "may this string stand unquoted in a hand-formatted YAML line" rule lives in kontroll/yamlscalar.py, because
# the generators need it as well as the validators here; it is re-exported so the service modules keep one import.
from kontroll.yamlscalar import plain_scalar  # noqa: F401  (re-exported)

# A conservative DNS label (RFC-1123-ish): 1..63 of [A-Za-z0-9-], no leading/trailing hyphen. `fullmatch`'d per
# dot-separated label so a hostname can carry no whitespace, metacharacter, or trailing newline.
_HOSTNAME_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)\Z")


def infer_type(knob):
    """A knob's `type`, inferred when absent (design 21 §2.3): an `allowed` list with no explicit type ⇒ `enum`
    (every legacy method param); otherwise `text` (the conservative default — which, with no `pattern`, REJECTS,
    so a descriptor must opt into free text explicitly). Lets legacy `{allowed, required}` descriptors validate
    through this seam with zero edits."""
    t = knob.get("type")
    if t:
        return t
    return "enum" if knob.get("allowed") is not None else "text"


def _v_enum(knob, value, name):
    """A CLOSED allow-list: the value must be one of `allowed` (the original config-injection guard)."""
    allowed = knob.get("allowed") or []
    if value in allowed:
        return None
    return "param %s=%r is not in the allow-list %r (rejected)" % (name, value, allowed)


def _v_bool(knob, value, name):
    """A true boolean — NOT a truthy string/int (so a `"true"` or `1` can't smuggle past a checkbox knob)."""
    if isinstance(value, bool):
        return None
    return "param %s=%r must be a boolean (true/false)" % (name, value)


def _v_int(knob, value, name):
    """A bounded integer. JSON booleans are Python `bool` (a subclass of `int`) — excluded, so a checkbox value
    can't pass as an int. `range.{min,max}` are server-enforced (inclusive)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "param %s=%r must be an integer" % (name, value)
    rng = knob.get("range") or {}
    lo, hi = rng.get("min"), rng.get("max")
    if lo is not None and value < lo:
        return "param %s=%d is below the minimum %d" % (name, value, lo)
    if hi is not None and value > hi:
        return "param %s=%d is above the maximum %d" % (name, value, hi)
    return None


def _v_ipv4(knob, value, name):
    """A dotted-quad IPv4 address (`ipaddress.IPv4Address`) — rejects any out-of-range octet, any extra text,
    and any metacharacter (`192.0.2.10; rm` is not a valid address), so no shell/YAML payload can ride here.
    Additionally rejects the addresses that are never a usable host/management target — loopback (127/8),
    unspecified (0.0.0.0), multicast (224/4), and reserved (240/4) — a sanity bound on the danger knobs
    (mgmt_ip / ansible_host); ordinary private OR public unicast still validates (review R2)."""
    if not isinstance(value, str):
        return "param %s=%r is not a valid IPv4 address" % (name, value)
    try:
        addr = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError):
        return "param %s=%r is not a valid IPv4 address" % (name, value)
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_reserved:
        return ("param %s=%r is not a usable host/management IPv4 (loopback/unspecified/multicast/reserved)"
                % (name, value))
    return None


def _v_ipv6(knob, value, name):
    """A textual IPv6 address (`ipaddress.IPv6Address`) — the v6 twin of `_v_ipv4`'s config-injection guard: rejects
    any non-hex-colon text, any embedded metacharacter, and any trailing junk (`2001:db8::1; rm` is not a valid
    address). Additionally rejects the addresses that are never a usable host/management target — loopback (::1),
    unspecified (::), multicast (ff00::/8), reserved, AND link-local (fe80::/10, interface-scoped: real on the wire but
    not a routable/onboardable mgmt address without a zone id); ordinary global or ULA unicast still validates.
    A ZONE ID is refused outright: `ipaddress` accepts `2001:db8::1%<anything>` and takes any text after the `%` —
    a newline, braces, markup — so the parser alone is not the charset (SECURITY.md C22)."""
    if not isinstance(value, str) or "%" in value:
        return "param %s=%r is not a valid IPv6 address" % (name, value)
    try:
        addr = ipaddress.IPv6Address(value)
    except (ipaddress.AddressValueError, ValueError):
        return "param %s=%r is not a valid IPv6 address" % (name, value)
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_reserved or addr.is_link_local:
        return ("param %s=%r is not a usable host/management IPv6 (loopback/unspecified/multicast/reserved/link-local)"
                % (name, value))
    return None


def _v_ip(knob, value, name):
    """A dual-stack host address: valid as EITHER `_v_ipv4` OR `_v_ipv6` (each with its own never-a-host + injection
    rejections). This is the guard the discovery read side (`_clean_ip`) uses so a v6 neighbour becomes a candidate
    WITHOUT widening the v4-only `ipv4` type the config-plane danger knobs (mgmt_ip / ansible_host) still validate
    through. Returns None (valid) if either leg passes; else the generic dual-stack error."""
    if _v_ipv4(knob, value, name) is None or _v_ipv6(knob, value, name) is None:
        return None
    return "param %s=%r is not a valid IPv4 or IPv6 host address" % (name, value)


def _v_hostname(knob, value, name):
    """A dotted hostname of RFC-1123-ish labels (`_HOSTNAME_LABEL` per label, ≤253 total). `fullmatch` per label
    rejects whitespace, metacharacters, and a trailing newline — so a hostname knob can carry no injection. A name
    whose labels are all in the charset can still be a YAML keyword or number (`null`, `on`, `123`, `1.5`); a
    hostname knob is written unquoted into instance.yml, so such a name is refused as well (`plain_scalar`)."""
    if not isinstance(value, str) or not value or len(value) > 253:
        return "param %s=%r is not a valid hostname" % (name, value)
    if not all(_HOSTNAME_LABEL.fullmatch(lbl) for lbl in value.split(".")):
        return "param %s=%r is not a valid hostname" % (name, value)
    if not plain_scalar(value):
        # `null`, `true`, `no`, `123`, `1.5`, a date: every label is in the charset, and the scalar writers would
        # store the word as another type. Refused HERE so the caller gets a clean message — the writers refuse it
        # too, but with a plain ValueError that a route without a `try` answers as a 500.
        return ("param %s is a word YAML reads as another type (null, a boolean, a number, a date) — it cannot be "
                "stored as a hostname" % name)
    return None


def _v_text(knob, value, name):
    """A short free string constrained by a REQUIRED `pattern`. A `text` knob with NO pattern is REJECTED (an
    unvalidated free field is not allowed; design 21 §10). `re.fullmatch` (not `match`) requires the WHOLE value
    to match, so a loosely-written pattern can't be bypassed by a trailing metacharacter or newline — the exact
    config-injection vector the guard exists to stop."""
    pattern = knob.get("pattern")
    if not pattern:
        return ("param %s is type 'text' but its descriptor carries no validation pattern — rejected "
                "(an unvalidated free field is not allowed; design 21 §10)" % name)
    if not isinstance(value, str):
        return "param %s=%r must be a string" % (name, value)
    if re.fullmatch(pattern, value) is None:
        return "param %s=%r does not match the required pattern %r" % (name, value, pattern)
    if not plain_scalar(value):
        return ("param %s is a word YAML reads as another type (null, a boolean, a number, a date) — it cannot be "
                "stored as text" % name)
    return None


# The validator registry — the server-side dispatch seam, twin of Phase-3's client type→widget table. Adding a
# knob type is ONE entry here + one widget there; the descriptor's `type` selects both. Keep them in lockstep.
_VALIDATORS = {
    "enum": _v_enum,
    "bool": _v_bool,
    "int": _v_int,
    "ipv4": _v_ipv4,
    "ipv6": _v_ipv6,   # v6-only host guard (discovery v6 neighbours; NOT the config-plane ipv4 knobs)
    "ip": _v_ip,       # dual-stack (ipv4 OR ipv6) — the discovery read-side key guard
    "hostname": _v_hostname,
    "text": _v_text,
}


def validate_value(knob, value):
    """None if `value` satisfies the knob descriptor, else an error string. Dispatches on the knob `type`
    (inferred when absent). The reusable single-knob guard; `_blockwrite.validate_params` loops it over a
    method's params, and Phase-3's `build_plan` re-validation calls it per widened knob. FAIL-CLOSED: an
    unknown `type` is rejected, never accepted. Never raises (a malformed descriptor degrades to an error
    string, not a crashed worker)."""
    name = knob.get("key") or knob.get("name") or "value"
    fn = _VALIDATORS.get(infer_type(knob))
    if fn is None:
        return "param %s has unknown knob type %r (rejected)" % (name, knob.get("type"))
    return fn(knob, value, name)
