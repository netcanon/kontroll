"""_validate — the server-side knob VALIDATOR registry (P0a, #133), the M3/S-1 prerequisite for Phase 3's
widened knobs.

These pin `service/_validate.validate_value` per knob `type`. WHY (the failure they guard): Phase 3 widens the
GUI's widget vocabulary from `<select>`-only to int/text/bool/ipv4/hostname inputs. The widget is convenience;
the SERVER is the guard. If the server only re-checked `allowed`-list membership (as it did before P0a), a
widened free-form widget — or a hand-crafted POST bypassing the widget entirely — could smuggle a
metacharacter-bearing value into a rendered `module.yml` block (the config-injection hole the closed allow-list
closed). So every type here is tested with its INJECTION vector (a trailing `;`, a trailing newline, a wrong
type, an out-of-range bound) and must REJECT it. The fail-closed defaults (unknown type rejected; `text` with no
`pattern` rejected) are the load-bearing posture — a descriptor must opt INTO validation, never out of it.
"""
import pytest

from kontroll.service._validate import infer_type, plain_scalar, validate_value

pytestmark = pytest.mark.unit


# --- type inference: legacy descriptors participate with zero edits (design 21 §2.3) -----------------------

def test_infer_type_allowed_list_is_enum_and_bare_is_text():
    """A knob with `allowed` and no explicit `type` infers `enum` (every legacy method param); a bare knob with
    no allowed/type infers `text` (which, with no pattern, rejects). Guards that legacy `{allowed, required}`
    descriptors keep validating as enums through the new seam without a descriptor edit."""
    assert infer_type({"allowed": ["a", "b"]}) == "enum"
    assert infer_type({}) == "text"
    assert infer_type({"type": "int", "allowed": ["x"]}) == "int"   # explicit type wins over inference


# --- enum: the original closed-allow-list guard, unchanged -------------------------------------------------

def test_enum_accepts_allowed_rejects_outside_and_injection():
    """An enum value must be in `allowed`; anything else — including a metacharacter payload — is rejected.
    Guards the unchanged config-injection guarantee the legacy path gave."""
    k = {"key": "transport", "allowed": ["tcp", "udp"]}
    assert validate_value(k, "tcp") is None
    assert "allow-list" in validate_value(k, "rsh")
    assert "allow-list" in validate_value(k, "tcp; rm -rf /")   # injection never matches a closed list


# --- bool: a real boolean, not a truthy string/int ---------------------------------------------------------

def test_bool_accepts_only_true_false():
    """A `bool` knob accepts Python True/False only; a `"true"` string or `1` is rejected. Guards a checkbox
    value being spoofed by a string/int that would render as an unexpected scalar."""
    k = {"key": "enabled", "type": "bool"}
    assert validate_value(k, True) is None and validate_value(k, False) is None
    assert "boolean" in validate_value(k, "true")
    assert "boolean" in validate_value(k, 1)


# --- int: a real integer within server-enforced bounds -----------------------------------------------------

def test_int_enforces_type_and_range():
    """An `int` knob accepts an in-range integer and rejects a below-min, above-max, string, float, or boolean
    value (booleans are int subclasses — must be excluded). Guards an unbounded/typed-wrong number reaching a
    rendered block (e.g. a port, an interval)."""
    k = {"key": "port", "type": "int", "range": {"min": 1, "max": 65535}}
    assert validate_value(k, 443) is None
    assert "below the minimum" in validate_value(k, 0)
    assert "above the maximum" in validate_value(k, 70000)
    assert "must be an integer" in validate_value(k, "443")     # a string number is NOT an int
    assert "must be an integer" in validate_value(k, 4.5)       # a float is not an int
    assert "must be an integer" in validate_value(k, True)      # bool is excluded despite being an int subclass


def test_int_with_no_range_accepts_any_integer():
    """An `int` knob with no `range` accepts any integer (bounds are optional). Guards over-restricting a knob
    that legitimately wants the full integer domain."""
    assert validate_value({"key": "n", "type": "int"}, -5) is None


# --- ipv4: a dotted quad, nothing else ---------------------------------------------------------------------

def test_ipv4_accepts_valid_and_rejects_garbage_and_injection():
    """An `ipv4` knob accepts a valid dotted quad and rejects an out-of-range octet, trailing text, a
    metacharacter payload, and a non-string. Guards an address knob carrying a shell/YAML payload."""
    k = {"key": "addr", "type": "ipv4"}
    assert validate_value(k, "192.0.2.10") is None
    assert "IPv4" in validate_value(k, "999.0.0.1")
    assert "IPv4" in validate_value(k, "192.0.2.10; rm -rf /")
    assert "IPv4" in validate_value(k, "192.0.2.10\n")          # trailing newline rejected
    assert "IPv4" in validate_value(k, 3232235530)              # an int is not a dotted quad here


def test_ipv4_rejects_unusable_addresses_but_allows_private_and_public_unicast():
    """An `ipv4` knob additionally rejects the addresses that are never a usable host/management target —
    loopback / unspecified / multicast / reserved — while ordinary private OR public unicast still validates.
    Guards a danger knob (mgmt_ip/ansible_host) staging 0.0.0.0 / 127.0.0.1 / 224.x (review R2): a sanity bound
    behind the human type-to-confirm + C10, not a substitute for them."""
    k = {"key": "addr", "type": "ipv4"}
    assert validate_value(k, "10.0.0.1") is None and validate_value(k, "203.0.113.5") is None  # private + public; pii-guard: allow private-unicast acceptance case; pii-guard: allow private-unicast acceptance case; pii-guard: allow private-unicast acceptance case
    for bad in ("0.0.0.0", "127.0.0.1", "224.0.0.1", "240.0.0.1"):                  # unspec/loopback/multicast/reserved
        assert "usable" in validate_value(k, bad), bad


# --- ipv6 / ip: the dual-stack discovery read guard (IPv6 discovery rung) -----------------------------------

def test_ip_is_dual_stack_but_ipv4_stays_v4_only():
    """THE load-bearing guarantee of the IPv6 discovery rung: the NEW `ip` (dual-stack) type accepts BOTH a v4 and a
    v6 host, but the existing `ipv4` type stays v4-ONLY — so widening discovery's read key (service/discovery._clean_ip
    now asks for `ip`) does NOT weaken the config-plane danger knobs (mgmt_ip / ansible_host, both `type: ipv4`), which
    keep rejecting a v6 string. A design that widened `_v_ipv4` in place would silently let a v6 past the onboard guard."""
    assert validate_value({"type": "ip"}, "192.0.2.50") is None            # dual-stack accepts v4
    assert validate_value({"type": "ip"}, "2001:db8::1") is None           # dual-stack accepts v6
    assert "IPv4 or IPv6" in validate_value({"type": "ip"}, "nope")        # neither -> the dual-stack error
    assert validate_value({"type": "ipv4"}, "2001:db8::1") is not None     # the config-plane guard is NOT widened
    assert "IPv4" in validate_value({"type": "ipv4"}, "2001:db8::1")


def test_ipv6_rejects_non_host_bands_and_injection():
    """An `ipv6` knob accepts ordinary global/ULA unicast and rejects a metacharacter payload, non-address text, and
    the addresses that are never a usable host/management target — loopback (::1), unspecified (::), multicast
    (ff00::/8), reserved, AND link-local (fe80::/10, interface-scoped). The v6 twin of _v_ipv4's guard."""
    k = {"key": "addr", "type": "ipv6"}
    assert validate_value(k, "2001:db8::1") is None
    assert "IPv6" in validate_value(k, "2001:db8::1; rm -rf /")            # injection rejected
    assert "IPv6" in validate_value(k, "not:an:address")
    for bad in ("::1", "::", "ff02::1", "fe80::1"):                        # loopback/unspecified/multicast/link-local
        assert "usable" in validate_value(k, bad), bad


@pytest.mark.parametrize("value", ["2001:db8::1%eth0", "2001:db8::1%x\n  a: b", "2001:db8::1%<img src=x>",
                                   "fe80::1%1"])
def test_ipv6_and_ip_refuse_a_zone_id(value):
    """The `ipv6` and dual-stack `ip` knob types refuse an address with a zone id, whatever follows the `%`. The
    standard library accepts ANY text there — a newline, markup, braces — so `ipaddress` alone is not the charset.
    Guards the discovery read side passing device-reported text after a `%` to the page and on into the onboard
    form's host field (SECURITY.md C22)."""
    assert "IPv6" in validate_value({"key": "addr", "type": "ipv6"}, value)
    assert "IPv4 or IPv6" in validate_value({"key": "addr", "type": "ip"}, value)


def test_hostname_accepts_labels_and_rejects_injection():
    """A `hostname` knob accepts RFC-1123-ish dotted labels and rejects whitespace, a metacharacter, a leading
    hyphen, a trailing newline, and an over-long value. Guards a hostname knob smuggling an injection. The
    trailing-newline case is the key one — a `$`-anchored regex would PASS it; `fullmatch` does not."""
    k = {"key": "host", "type": "hostname"}
    assert validate_value(k, "switch01") is None
    assert validate_value(k, "switch01.lab.example.test") is None
    assert "hostname" in validate_value(k, "host name")         # space
    assert "hostname" in validate_value(k, "host;rm")           # metacharacter
    assert "hostname" in validate_value(k, "-host")             # leading hyphen
    assert "hostname" in validate_value(k, "host\n")            # trailing newline (the fullmatch case)
    assert "hostname" in validate_value(k, "a" * 254)           # over-long


# --- text: a REQUIRED pattern, fullmatch'd -----------------------------------------------------------------

def test_text_requires_a_pattern_and_fullmatches_it():
    """A `text` knob with NO `pattern` is REJECTED (an unvalidated free field is not allowed — design 21 §10).
    With a pattern, `re.fullmatch` requires the WHOLE value to match, so a trailing metacharacter or newline
    can't ride past a loosely-written pattern. Guards the DO-NOT-BUILD line: no unvalidated free text."""
    assert "no validation pattern" in validate_value({"key": "label", "type": "text"}, "anything")
    k = {"key": "label", "type": "text", "pattern": r"[a-z0-9_]+"}
    assert validate_value(k, "edge_fw") is None
    assert "does not match" in validate_value(k, "edge fw")     # space not in class
    assert "does not match" in validate_value(k, "edge_fw; rm") # trailing metacharacters
    assert "does not match" in validate_value(k, "edge_fw\n")   # trailing newline — fullmatch rejects (match would not)
    assert "must be a string" in validate_value(k, 123)         # non-string rejected


# --- fail-closed posture: unknown type rejected, never silently accepted -----------------------------------

def test_unknown_type_is_rejected_fail_closed():
    """A knob with a `type` not in the registry is REJECTED, not silently accepted — the fail-closed posture so
    a typo'd/forged descriptor can never wave a value through unvalidated. Guards the registry being the closed
    set of known knob types."""
    assert "unknown knob type" in validate_value({"key": "x", "type": "wat"}, "anything")


def test_validate_value_never_raises_on_odd_input():
    """validate_value degrades to an error string (never raises) on odd input — None value, missing key — so a
    malformed request can't crash the in-process worker (the service-never-sys.exit posture)."""
    assert validate_value({"type": "ipv4"}, None) is not None   # no `key`, None value → an error string, no raise
    assert validate_value({"type": "int"}, None) is not None


@pytest.mark.parametrize("value", ["cisco_ios", "node", "7d", "192.0.2.10", "example.test", "a-b", "v1"])
def test_plain_scalar_accepts_words_yaml_reads_as_strings(value):
    """Ordinary names, a duration preset, an address and a domain read back from YAML as themselves. Guards the
    check refusing the values the hand-formatting writers exist to write."""
    assert plain_scalar(value)


@pytest.mark.parametrize("value", ["null", "Null", "~", "true", "False", "yes", "no", "on", "off", "123", "0x10",
                                   "1.5", "2026-10-12", "", " a", "a ", "a: b", "x #y", "[a]", "{a}", "- a",
                                   "*a", "&a", "!a", "a\nb", None, 5, True, ["a"]])
def test_plain_scalar_refuses_what_yaml_reads_as_something_else(value):
    """A YAML keyword, a number, a date, an empty or padded word, structure, an anchor or tag, a line break and
    every non-string are refused. Guards a value that is inside a closed CHARSET and still lands in a
    hand-formatted file as None, a boolean, a number, a date — or as structure."""
    assert not plain_scalar(value)


@pytest.mark.parametrize("value", ["null", "true", "yes", "no", "on"])
def test_a_text_knob_refuses_a_word_yaml_reads_as_another_type(value):
    """A `text` knob whose pattern is a lowercase slug — the identity knobs `role`, `inventory_group`, `backend` —
    refuses `null`, `true`, `yes`: each matches the pattern and would be written unquoted as `role: null`, which
    YAML reads as no role at all. A real slug still passes. Guards a class losing its role or its inventory group
    to one well-formed-looking word."""
    knob = {"key": "role", "type": "text", "pattern": "[a-z][a-z0-9_]*"}
    assert "another type" in validate_value(knob, value)
    assert validate_value(knob, "core_switch") is None


@pytest.mark.parametrize("value", ["null", "Null", "true", "no", "on", "off", "yes", "123", "1.5", "0x10",
                                   "2026-10-12", "2026-02-30", "0000-00-00", "9999-99-99"])
def test_a_hostname_knob_refuses_a_word_yaml_reads_as_another_type(value):
    """A `hostname` knob — the instance `domain` — refuses a name whose labels are all inside the charset and
    which YAML reads as a keyword, a number or a date; a real domain still passes. The knob is written unquoted
    into instance.yml, so `domain: null` was no domain at all; and once the scalar writers refused such a value
    themselves, this validator answering "valid" turned the request into an uncaught error. Guards both: the wrong
    write, and the 500 that replaced it."""
    knob = {"key": "domain", "type": "hostname"}
    assert "another type" in validate_value(knob, value)
    assert validate_value(knob, "example.test") is None
    assert validate_value(knob, "sw-01.lab.example.test") is None


def test_the_validators_and_the_writers_share_one_plain_scalar():
    """`service/_validate.plain_scalar` IS `kontroll.yamlscalar.plain_scalar` — re-exported, not copied. Guards
    the request-side rule and the generators' rule drifting into two definitions of "a plain word"."""
    from kontroll import yamlscalar
    assert plain_scalar is yamlscalar.plain_scalar


@pytest.mark.parametrize("field", ["secrets_domain", "inventory_group", "backend", "role"])
def test_identity_names_are_bounded_like_the_join(field):
    """Each identity knob that holds a name accepts a sixty-four-character one and refuses a sixty-five-character
    one — and `paths.is_component`, the rule of the join the name ends up in, agrees on both.

    Guards a request storing a name the join then refuses: the knob's pattern had no length bound, so an
    eighty-letter secrets domain was accepted, promoted into the module file, and the next onboard of that class
    was refused at apply — for a field nobody had sent, after the inventory drop-in had been written (and the
    fleet line, where the class was not enabled yet)."""
    from kontroll import catalog, paths
    doc = next(a for a in catalog.load_knob_descriptors() if a.get("area") == "module-identity")
    knob = next(k for k in doc["knobs"] if k["key"] == field)
    longest, too_long = "a" * 64, "a" * 65
    assert validate_value(knob, longest) is None and paths.is_component(longest)
    assert validate_value(knob, too_long) is not None and not paths.is_component(too_long)
