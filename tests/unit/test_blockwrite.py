"""service/_blockwrite — the shared comment-preserving block writer + param config-injection guard, lifted
out of observe.py so observe (telemetry) and logsvc (logging) write the same way and validate against the same
CLOSED allow-list. These pin the shared helpers directly so a future refactor can't silently re-diverge the two
instances (V1 must-fix #13).
"""
import pytest

from kontroll.service._blockwrite import (disable_in_fleet, insert_into_block, set_list_block,
                                          set_nested_scalar, upsert_into_block, upsert_top_level_key,
                                          validate_params)

pytestmark = pytest.mark.unit

_METHOD = {"name": "m", "params": {"transport": {"allowed": ["tcp", "udp"], "required": False},
                                   "fmt": {"allowed": ["json"], "required": True}}}


def test_insert_appends_to_an_existing_block_preserving_comments():
    """insert_into_block appends a list item to an existing top-level block and leaves the rest (comments,
    other blocks) untouched — the comment-preserving line-surgery a hand-written module.yml depends on; guards
    a regression to a safe_dump round-trip that would flatten comments."""
    text = "key: x\n# a comment\nlogs:\n  - {method: a}\nrole: r\n"
    out = insert_into_block(text, "logs", ["  - {method: b}"])
    assert "# a comment" in out and "- {method: a}" in out and "- {method: b}" in out
    assert out.index("- {method: b}") < out.index("role: r")   # inserted INSIDE the block, before the next key


def test_insert_creates_the_block_when_absent():
    """When the block doesn't exist, insert_into_block appends a fresh `block:` at EOF — guards a missing-block
    add silently dropping the new entry."""
    out = insert_into_block("key: x\n", "logs", ["  - {method: a}"])
    assert out.rstrip().endswith("logs:\n  - {method: a}".rstrip()) or "logs:" in out


def test_upsert_replaces_a_list_item_in_place_preserving_comments_and_siblings():
    """upsert_into_block finds the `metrics:` item whose method matches and REPLACES its line-span in place,
    leaving the comment + the sibling host_node entry intact — the reconfigure G1 writer that lifts the
    `already_declared` wall without flattening a hand-written module (the line-span swap, never a safe_dump)."""
    text = ("metrics:\n"
            "  # the snmp poller\n"
            "  - {method: snmp, params: {module: if_mib}}\n"
            "  - {method: host_node}\n"
            "role: r\n")
    out = upsert_into_block(text, "metrics", "snmp", ["  - {method: snmp, params: {module: system}}"])
    assert "module: system" in out and "module: if_mib" not in out      # the value was replaced in place
    assert "# the snmp poller" in out and "- {method: host_node}" in out  # comment + sibling survived
    assert out.index("- {method: snmp") < out.index("- {method: host_node}")  # order preserved


def test_upsert_handles_a_block_style_entry_with_a_nested_list_value():
    """upsert replaces a block-STYLE entry whose param value is a nested YAML list (the deeper `- ` lines are
    NOT item boundaries) — the span covers the whole entry, the nested list stays intact, and the following
    sibling survives. Guards the corruption the adversarial review found: a naive 'any `- ` is an item' detector
    truncates the span and orphans the nested lines into invalid YAML."""
    text = ("metrics:\n"
            "  - method: snmp\n"
            "    params:\n"
            "      modules:\n"
            "        - if_mib\n"
            "        - extra\n"
            "  - {method: host_node}\n")
    out = upsert_into_block(text, "metrics", "snmp", ["  - {method: snmp, params: {module: system}}"])
    assert "module: system" in out and "- if_mib" not in out and "- extra" not in out   # the whole entry replaced
    assert "- {method: host_node}" in out                                                # the sibling survived
    import yaml as _y
    assert _y.safe_load(out)["metrics"][-1] == {"method": "host_node"}                    # still valid YAML


def test_upsert_appends_when_the_entry_is_not_yet_declared():
    """An entry whose method is NOT in the block is appended (delegates to insert_into_block) — the add path,
    so upsert is a superset of insert. Guards the add branch of the reconfigure write."""
    text = "metrics:\n  - {method: host_node}\n"
    out = upsert_into_block(text, "metrics", "snmp", ["  - {method: snmp, params: {module: if_mib}}"])
    assert "- {method: host_node}" in out and "- {method: snmp" in out


def test_upsert_mapping_block_replaces_the_whole_block_in_place():
    """For a MAPPING block (backup, list_block=False) upsert replaces the `block_key:` + body with the new full
    block, comment- and trailing-content-preserving. Guards the backup reconfigure (its `already_declared` wall
    was the sharpest) replacing the mapping, not duplicating it."""
    text = ("key: x\n"
            "backup:\n"
            "  capable: true\n"
            "  schedule: \"0 2 * * *\"\n"
            "  retention: keep-all\n"
            "  destination: local\n")
    new_block = ["backup:", "  capable: true", "  schedule: \"0 3 * * 0\"", "  retention: 90d",
                 "  destination: local"]
    out = upsert_into_block(text, "backup", "backup", new_block, list_block=False)
    assert "retention: 90d" in out and "retention: keep-all" not in out
    assert out.count("backup:") == 1 and "key: x" in out                 # replaced in place, not duplicated


def test_upsert_top_level_key_replaces_in_place_preserving_comments_and_siblings():
    """upsert_top_level_key swaps a top-level scalar's value IN PLACE, preserving the line's inline comment, the
    other keys, and the rest of the file — the IDENTITY-key line-surgery (design 22 §5.4 G7) a hand-written
    module.yml depends on; guards an identity re-classify flattening the module to a safe_dump (losing comments).
    It round-trips through yaml.safe_load (still valid YAML)."""
    text = ("key: demo_sw\n"
            "status: active\n"
            "inventory_group: core_switch   # the dispatch seam\n"
            "metrics:\n  - {method: snmp}\n")
    out = upsert_top_level_key(text, "inventory_group", "edge_firewall")
    assert "inventory_group: edge_firewall" in out and "core_switch" not in out   # value replaced
    assert "# the dispatch seam" in out                                            # inline comment survived
    assert "status: active" in out and "- {method: snmp}" in out                   # siblings + block intact
    import yaml as _y
    assert _y.safe_load(out)["inventory_group"] == "edge_firewall"


def test_upsert_top_level_key_refuses_a_multiline_value():
    """upsert_top_level_key FAIL-CLOSES on a value containing a newline — a top-level scalar must be single-line;
    a `\\n` would write a SECOND top-level key (a config injection). Guards the hole behind the validator: the
    writer is a SHARED primitive, so it must stay safe even if a future descriptor adds a looser-pattern knob
    (review SHOULD-FIX 3). The live path validates first, so this never fires there."""
    with pytest.raises(ValueError):
        upsert_top_level_key("key: x\nrole: r\n", "role", "backend\ninjected: pwned")


def test_upsert_top_level_key_does_not_match_a_prefix_key_and_appends_when_absent():
    """The target is matched only when the char after the key is `:` (so `role:` never matches `roles:`), and an
    ABSENT key is appended at EOF (YAML is order-free). Guards a re-classify of `role` corrupting a `roles:` key,
    and a first-set silently dropping the new value."""
    text = "roles: [a, b]\nrole: backend_ssh\n"
    out = upsert_top_level_key(text, "role", "backend_network_cli")
    assert "role: backend_network_cli" in out and "roles: [a, b]" in out           # prefix key untouched
    appended = upsert_top_level_key("key: x\n", "status", "staged")
    assert appended.rstrip().endswith("status: staged")                            # absent → appended at EOF


def test_set_nested_scalar_replaces_in_place_preserving_siblings_and_comments(tmp_repo=None):
    """set_nested_scalar swaps `frontend.tls_mode` IN PLACE, keeping the parent's other children, the inline
    comment, and the rest of the file — the Phase-5 tls_mode writer over a hand-commented instance.yml. Guards a
    nested-key edit flattening the file or clobbering a sibling under the same parent."""
    text = ("mgmt_ip: 192.0.2.10\nfrontend:\n  tls_mode: self_signed   # the mode\n  other: keep\nx: y\n")
    out = set_nested_scalar(text, "frontend", "tls_mode", "byo_proxy")
    assert "tls_mode: byo_proxy" in out and "self_signed" not in out and "# the mode" in out
    assert "other: keep" in out and "mgmt_ip: 192.0.2.10" in out and "x: y" in out
    import yaml as _y
    assert _y.safe_load(out)["frontend"] == {"tls_mode": "byo_proxy", "other": "keep"}


def test_set_nested_scalar_inserts_an_absent_child_and_appends_an_absent_parent():
    """An absent child is inserted under the existing parent; an absent parent is appended with the child. Guards
    a first-set silently dropping the value."""
    assert "tls_mode: acme" in set_nested_scalar("frontend:\n  other: x\n", "frontend", "tls_mode", "acme")
    assert "frontend:\n  tls_mode: acme" in set_nested_scalar("mgmt_ip: x\n", "frontend", "tls_mode", "acme")


def test_set_list_block_replaces_inline_and_block_forms_preserving_following_content():
    """set_list_block replaces a top-level list (the Phase-5 backup_remotes writer) — both the inline `key: []`
    form and the `key:` + `  - a` block form — and leaves the following comment/key intact; an empty list renders
    `key: []`. Guards a list edit consuming a following blank/comment or duplicating the block."""
    inline = "backup_remotes: []\n# next\nfrontend:\n  tls_mode: self_signed\n"
    out = set_list_block(inline, "backup_remotes", ["backup", "offsite"])
    assert "backup_remotes:\n  - backup\n  - offsite" in out and "# next" in out and "tls_mode" in out
    block = "backup_remotes:\n  - a\n  - b\nfrontend: {}\n"
    assert set_list_block(block, "backup_remotes", []).startswith("backup_remotes: []")   # back to empty inline
    import yaml as _y
    assert _y.safe_load(out)["backup_remotes"] == ["backup", "offsite"]


def test_set_list_block_does_not_orphan_items_split_by_a_blank_or_comment(tmp_repo=None):
    """set_list_block replaces the WHOLE list body even when a blank line or a comment sits BETWEEN two items —
    so an operator's new list is exactly what lands, with no orphaned trailing item. Guards the silent-clobber
    bug the adversarial review found (B2): a between-items blank/comment used to truncate the span, leaving
    `['new']` written as `['new', orphan]` — invisible to the no-drop (lists aren't descended) and the token."""
    import yaml as _y
    blank = "backup_remotes:\n  - keep1\n\n  - keep2\nmgmt_ip: 1.1.1.1\n"
    out = set_list_block(blank, "backup_remotes", ["new"])
    assert _y.safe_load(out)["backup_remotes"] == ["new"] and "keep2" not in out   # no orphan
    assert "mgmt_ip: 1.1.1.1" in out                                               # the next key survives
    comment = "backup_remotes:\n  - a\n  # note\n  - b\nfrontend: {}\n"
    assert _y.safe_load(set_list_block(comment, "backup_remotes", ["new"]))["backup_remotes"] == ["new"]
    # a col-0 comment AFTER the list belongs to the next section and is preserved + the separator blank survives
    sep = "backup_remotes: []\n\n# next section\nfrontend: {}\n"
    out2 = set_list_block(sep, "backup_remotes", ["x"])
    assert "# next section" in out2 and "\n\n# next section" in out2 and _y.safe_load(out2)["backup_remotes"] == ["x"]


def test_set_nested_scalar_and_set_list_block_refuse_a_multiline_value():
    """set_nested_scalar and set_list_block FAIL-CLOSE on a `\\n` value (the multi-line guard) — a newline would
    inject a sibling/second key. Guards the config-injection backstop behind the validator on BOTH writers (only
    upsert_top_level_key's guard was directly pinned; the review flagged these two as untested)."""
    with pytest.raises(ValueError):
        set_nested_scalar("frontend:\n  tls_mode: x\n", "frontend", "tls_mode", "byo_proxy\ninjected: y")
    with pytest.raises(ValueError):
        set_list_block("backup_remotes: []\n", "backup_remotes", ["ok", "bad\ninjected: y"])


def test_disable_in_fleet_removes_one_item_and_raises_when_absent():
    """disable_in_fleet drops exactly the `  - <module>` line (comment + sibling modules survive) and RAISES
    (never sys.exit) when the module isn't active or enabled_modules: is missing. Guards the fleet-removal writer
    duplicating/flattening the list or killing the worker on a bad module (the G9 fix)."""
    text = "enabled_modules:\n  - proxmox\n  - cisco_ios   # the switch\n  # - opnsense (commented)\n"
    out = disable_in_fleet(text, "proxmox")
    assert "- proxmox" not in out and "- cisco_ios" in out and "# the switch" in out and "opnsense" in out
    with pytest.raises(ValueError):
        disable_in_fleet(text, "not_enabled")
    with pytest.raises(ValueError):
        disable_in_fleet("other: x\n", "proxmox")


def test_validate_params_accepts_allowed_and_rejects_outside_the_allowlist():
    """validate_params returns None for allow-listed values and an error string for a value outside the
    allow-list — the config-injection guard shared with the generator; guards a metacharacter-bearing value
    reaching a rendered entry (C12)."""
    assert validate_params(_METHOD, {"transport": "tcp", "fmt": "json"}) is None
    assert "allow-list" in validate_params(_METHOD, {"transport": "rsh", "fmt": "json"})


def test_validate_params_flags_missing_required_and_unknown_param():
    """validate_params rejects a missing REQUIRED param and an unknown param name — guards a method silently
    rendering without a required value, or accepting a param the method doesn't define."""
    assert "requires param 'fmt'" in validate_params(_METHOD, {"transport": "tcp"})
    assert "takes no param" in validate_params(_METHOD, {"fmt": "json", "bogus": "x"})


def test_validate_params_enforces_widened_knob_types_through_the_method_loop():
    """validate_params dispatches every value through service/_validate, so a method whose param carries a
    widened `type` (here an `int` with a `range`) is range-checked server-side — not just allow-list-checked.
    Guards the P0a wiring: the widened validator must reach the method-param loop the GUI capability writes use,
    so Phase 3's widened widgets are re-validated on the server (the widget is convenience; the server is the
    guard). A required bool that's missing is still flagged."""
    method = {"name": "m", "params": {"interval": {"type": "int", "range": {"min": 5, "max": 300},
                                                    "required": True}}}
    assert validate_params(method, {"interval": 60}) is None
    assert "above the maximum" in validate_params(method, {"interval": 9999})
    assert "must be an integer" in validate_params(method, {"interval": "60; rm"})   # injection can't pass as int
    assert "requires param 'interval'" in validate_params(method, {})


def test_the_scalar_writers_refuse_a_value_that_would_not_read_back():
    """The three hand-formatting scalar writers fail closed on a value YAML would read back as something else — a
    string that is a keyword, a number or structure, a string holding ANY line-break character (the Unicode
    separators included), and a value that is a list or a mapping — and still write an int, a bool and an ordinary
    word. They are the last check: a caller validates first, but a new knob type with a looser validator must not
    be able to turn `role: null` or `mode: [a]` into a file, or add a key. Guards the primitive itself, whatever
    its callers do."""
    text = "key: demo\nrole: backend_api\nfrontend:\n  tls_mode: self_signed\nbackup_remotes: []\n"
    # U+2028 / U+2029: PyYAML keeps them verbatim in a bare scalar, so the word round-trips as a document — and
    # it is a line break where the writer puts it: `a<U+2028>? injected` added a top-level key `injected`
    separators = ["a" + chr(0x2028) + "? injected", "a" + chr(0x2029) + "b", "a\x0bb", "a\x85b"]
    for bad in ["null", "true", "123", "a: b", "x #y", "[a]"] + separators:
        with pytest.raises(ValueError):
            upsert_top_level_key(text, "role", bad)
        with pytest.raises(ValueError):
            set_nested_scalar(text, "frontend", "tls_mode", bad)
        with pytest.raises(ValueError):
            set_list_block(text, "backup_remotes", [bad])
    # a value with ANY line break is refused by the single-line rule, with the message that says why — the rule
    # used to look for a newline and a carriage return only, while the same writers split the file with
    # splitlines(), which breaks on all of these
    for broken in ["a\nb"] + separators:
        for call in (lambda v: upsert_top_level_key(text, "role", v),
                     lambda v: set_nested_scalar(text, "frontend", "tls_mode", v),
                     lambda v: set_list_block(text, "backup_remotes", [v])):
            with pytest.raises(ValueError, match="single-line"):
                call(broken)
    # a value that is not a scalar at all: Python's text for a list or a dict IS YAML flow syntax
    for structure in (["x"], {"a": 1}, [], (1, 2)):
        with pytest.raises(ValueError):
            upsert_top_level_key(text, "role", structure)
        with pytest.raises(ValueError):
            set_nested_scalar(text, "frontend", "tls_mode", structure)
    assert "role: opnsense\n" in upsert_top_level_key(text, "role", "opnsense")
    assert "retries: 5\n" in upsert_top_level_key(text, "retries", 5)
    # a float is written only when its text reads back as that float: 1.5 does, 1e+20 (no dot) is a string to YAML
    assert "ratio: 1.5\n" in upsert_top_level_key(text, "ratio", 1.5)
    for unreadable in (1e20, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            upsert_top_level_key(text, "ratio", unreadable)
    assert "enabled: True\n" in upsert_top_level_key(text, "enabled", True)
    assert "  - offsite\n" in set_list_block(text, "backup_remotes", ["offsite"])
