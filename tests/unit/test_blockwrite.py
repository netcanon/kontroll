"""service/_blockwrite — the shared comment-preserving block writer + param config-injection guard, lifted
out of observe.py so observe (telemetry) and logsvc (logging) write the same way and validate against the same
CLOSED allow-list. These pin the shared helpers directly so a future refactor can't silently re-diverge the two
instances (V1 must-fix #13).
"""
import glob
import os

import pytest
import yaml

from kontroll import paths
from kontroll.service._blockwrite import (disable_in_fleet, insert_into_block, require_reads_back, set_list_block,
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


# --- the line that opens a block: with a comment, with a value on it, written twice ----------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SHIPPED_MODULES = sorted(glob.glob(os.path.join(ROOT, "modules", "*", "module.yml")))

# The shape of one shipped class's file: the list's key line carries a comment.
_KEY_LINE_WITH_A_COMMENT = """key: demo
metrics:                       # ORDER = [blackbox, host_node]
  - {method: blackbox, params: {probe: icmp}}    # reachability
  - {method: host_node}                          # host metrics
dashboards:
  - {gnet: 1, name: a}
"""


def _key_lines(text, key):
    """The top-level lines of `text` that open `key` — one, in a file YAML reads as written."""
    return [ln for ln in text.splitlines() if ln.startswith(key + ":")]


def test_a_key_line_that_carries_a_comment_opens_the_block():
    """A list whose key line has a trailing comment (`metrics:   # ...`) is found by both list writers: an upsert
    replaces the named entry in place and an insert lands inside the block, the key line stands once and unchanged,
    and YAML reads every entry that was there.

    Guards the loss this fixes: the writers looked for the bare `key:` line, did not find a commented one, and
    appended a SECOND top-level block at the end of the file — YAML reads the last of two, so the class's other
    methods were gone from every generated file while the module file still showed them."""
    out = upsert_into_block(_KEY_LINE_WITH_A_COMMENT, "metrics", "blackbox",
                            ["  - {method: blackbox, params: {probe: tcp_connect}}"])
    assert _key_lines(out, "metrics") == ["metrics:                       # ORDER = [blackbox, host_node]"]
    read = yaml.safe_load(out)
    assert [m["method"] for m in read["metrics"]] == ["blackbox", "host_node"]
    assert read["metrics"][0]["params"] == {"probe": "tcp_connect"} and read["dashboards"] == [{"gnet": 1, "name": "a"}]
    assert "# host metrics" in out                                    # the sibling's comment survives
    out = insert_into_block(_KEY_LINE_WITH_A_COMMENT, "metrics", ["  - {method: snmp}"])
    assert len(_key_lines(out, "metrics")) == 1
    assert [m["method"] for m in yaml.safe_load(out)["metrics"]] == ["blackbox", "host_node", "snmp"]
    assert out.index("method: snmp") < out.index("dashboards:")       # inside the block, not at the end of the file


def test_a_mapping_block_under_a_commented_key_line_is_replaced_in_place_and_the_comment_stays():
    """The mapping writer (`list_block=False`, the backup block) replaces a block whose key line carries a comment
    where it stands, and keeps that key line — comment included — in front of the new body.

    Guards the same second block for the mapping form, and the writer dropping an operator's note from the one
    line it would otherwise have rewritten: "comment-preserving" is what this module promises."""
    text = "key: demo\nbackup:      # nightly; see the runbook\n  method: old\n  keep: 3\nrole: r\n"
    out = upsert_into_block(text, "backup", "x", ["backup:", "  method: new"], list_block=False)
    assert _key_lines(out, "backup") == ["backup:      # nightly; see the runbook"]
    assert yaml.safe_load(out) == {"key": "demo", "backup": {"method": "new"}, "role": "r"}


@pytest.mark.parametrize("write", [
    lambda text: insert_into_block(text, "metrics", ["  - {method: c}"]),
    lambda text: upsert_into_block(text, "metrics", "a", ["  - {method: a}"]),
    lambda text: upsert_into_block(text, "metrics", "x", ["metrics:", "  m: 1"], list_block=False),
], ids=["insert", "upsert-list", "upsert-mapping"])
@pytest.mark.parametrize("text,says", [
    ("key: demo\nmetrics: []\n", "own line"),
    ("key: demo\nmetrics: [{method: a}]   # inline\n", "own line"),
    ("key: demo\nmetrics:\n  - {method: a}\nrole: r\nmetrics:\n  - {method: b}\n", "twice"),
    ("key: demo\nmetrics:   # one\n  - {method: a}\nrole: r\nmetrics:   # two\n  - {method: b}\n", "twice"),
], ids=["an-empty-flow-list", "a-flow-list", "the-key-twice", "the-key-twice-with-comments"])
def test_a_value_on_the_key_line_and_a_key_written_twice_are_refused_not_written_beside(text, says, write):
    """A key whose value is on its own line (`metrics: []`, a flow list) and a file that holds the key twice are
    refused by every block writer as the repository's own data (409 `repository_data`), with a sentence that
    names the key and the rule and carries nothing of the file.

    Guards the two shapes for which "append a block" is still a second top-level key: an inline list beside a new
    block, and a third block beside the two a file damaged by the old behaviour already holds — where YAML reads
    only the last, and a human has to merge them."""
    with pytest.raises(paths.RepositoryDataError) as refused:
        write(text)
    message = str(refused.value)
    assert refused.value.status == 409 and refused.value.code == "repository_data"
    assert "`metrics`" in message and says in message and "nothing was written" in message
    assert "demo" not in message and "{" not in message and "method" not in message


@pytest.mark.parametrize("path", _SHIPPED_MODULES, ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_every_shipped_module_keeps_every_entry_through_a_reconfigure(path):
    """For each shipped class file: every `metrics` and `logs` entry rewritten in place, an entry added to each
    list block the file declares (`metrics`, `logs`, `dashboards` — an empty one included), and the `backup`
    mapping replaced, leave the key line standing once and everything else as YAML read it before.

    Guards the writers against the files this repository actually ships — a key line with a comment was in one of
    them for as long as the writers could not see it — and tells whoever adds a class file in a shape the writers
    refuse (a value on the key's line) at the gate, not at an operator's first reconfigure."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    doc = yaml.safe_load(text) or {}
    for block, ident in (("metrics", "method"), ("logs", "method"), ("dashboards", "name")):
        if block not in doc:
            continue
        entries = doc[block] or []                                  # a key with nothing under it is still a key
        ids = [e[ident] for e in entries]
        if ident == "method":
            for method in ids:
                out = upsert_into_block(text, block, method, ["  - {method: %s}" % method])
                assert len(_key_lines(out, block)) == 1, (block, method)
                assert [e["method"] for e in yaml.safe_load(out)[block]] == ids, (block, method)
        out = insert_into_block(text, block, ["  - {%s: zz_new}" % ident])
        assert len(_key_lines(out, block)) == 1, block
        assert [e[ident] for e in yaml.safe_load(out)[block]] == ids + ["zz_new"], block
    if "backup" in doc:
        out = upsert_into_block(text, "backup", "backup", ["backup:", "  capable: true"], list_block=False)
        read = yaml.safe_load(out)
        assert len(_key_lines(out, "backup")) == 1 and read["backup"] == {"capable": True}
        assert {k: v for k, v in read.items() if k != "backup"} == {k: v for k, v in doc.items() if k != "backup"}


def test_only_the_key_itself_at_column_zero_opens_the_block():
    """A nested key of the same name, a longer key that begins with it and a commented-out line are not the
    block: with only those in the file a fresh block is appended and they stand untouched; with the real key
    beside them, the real one is edited.

    Guards the match from loosening in either direction — an indented `metrics:` taken for the top-level key puts
    the entry inside another mapping, and `metrics_extra:` taken for `metrics:` edits or refuses the wrong key."""
    others = "key: demo\nouter:\n  metrics:\n    - {method: q}\nmetrics_extra: [x]\n# metrics: was here\nrole: r\n"
    kept = {"key": "demo", "outer": {"metrics": [{"method": "q"}]}, "metrics_extra": ["x"], "role": "r"}
    out = insert_into_block(others, "metrics", ["  - {method: a}"])
    assert yaml.safe_load(out) == dict(kept, metrics=[{"method": "a"}])
    assert out.startswith(others)                                   # appended at the end; nothing above it moved
    both = others + "metrics:   # the real one\n  - {method: a}\n"
    out = upsert_into_block(both, "metrics", "a", ["  - {method: a, params: {p: 1}}"])
    assert yaml.safe_load(out) == dict(kept, metrics=[{"method": "a", "params": {"p": 1}}])


_AS_AN_ONBOARD_WRITES_IT = yaml.safe_dump(
    {"key": "demo", "role": "r",
     "metrics": [{"method": "blackbox", "params": {"probe": "icmp"}, "derived": True},
                 {"method": "host_node", "derived": True}],
     "logs": [{"method": "journald", "derived": True}], "derived": True}, sort_keys=False)


@pytest.mark.parametrize("text,indent", [
    (_AS_AN_ONBOARD_WRITES_IT, ""),
    ("key: demo\nmetrics:\n    - {method: blackbox, params: {probe: icmp}}\n    - method: host_node\n"
     "logs:\n    - {method: journald}\n", "    "),
], ids=["items-at-the-keys-column", "items-four-spaces-in"])
def test_a_list_whose_items_are_not_two_spaces_in_is_edited_at_its_own_indent(text, indent):
    """A list written at the key's own column — what yaml.safe_dump writes, so what an onboard writes — or four
    spaces in is edited at THAT indent: an upsert replaces the entry, an insert lands after the last item and
    before the next key, and YAML reads every entry of this block and of the next.

    Guards the writers against the tool's own module files: the block was taken to end at the first column-0
    item, the new two-space item was put above the list, and the planner returned a text that is not YAML."""
    assert ("\nmetrics:\n%s- " % indent) in text                    # the shape under test is the one written
    out = upsert_into_block(text, "metrics", "blackbox", ["  - {method: blackbox, params: {probe: tcp_connect}}"])
    read = yaml.safe_load(out)
    assert [m["method"] for m in read["metrics"]] == ["blackbox", "host_node"]
    assert read["metrics"][0]["params"] == {"probe": "tcp_connect"}
    assert [m["method"] for m in read["logs"]] == ["journald"]
    assert ("\n%s- {method: blackbox, params: {probe: tcp_connect}}\n" % indent) in out
    out = upsert_into_block(text, "metrics", "host_node", ["  - {method: host_node}"])
    assert [m["method"] for m in yaml.safe_load(out)["metrics"]] == ["blackbox", "host_node"]
    out = insert_into_block(text, "metrics", ["  - {method: snmp}"])
    read = yaml.safe_load(out)
    assert [m["method"] for m in read["metrics"]] == ["blackbox", "host_node", "snmp"]
    assert [m["method"] for m in read["logs"]] == ["journald"] and out.index("method: snmp") < out.index("logs:")


def test_set_list_block_replaces_a_list_written_at_the_keys_column():
    """`set_list_block` replaces a list whose items sit at the key's column, whole.

    Guards the shared block bound: the column-0 items were left behind under the new list, which is not YAML."""
    out = set_list_block("a: 1\nbackup_remotes:\n- x\n- y\nb: 2\n", "backup_remotes", ["new"])
    assert yaml.safe_load(out) == {"a": 1, "backup_remotes": ["new"], "b": 2}


# --- the post-condition: what a writer returns must READ BACK as the change that was planned ---------------------- #
def _plan(text, block, entry_id, new_lines):
    """What a planner does: replace the entry when YAML reads one of that id, append when it reads none."""
    declared = [e.get("method") for e in (yaml.safe_load(text) or {}).get(block) or [] if isinstance(e, dict)]
    return (upsert_into_block if entry_id in declared else insert_into_block)(
        *((text, block, entry_id, new_lines) if entry_id in declared else (text, block, new_lines)))


@pytest.mark.parametrize("path", _SHIPPED_MODULES, ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_what_the_writers_write_for_every_shipped_module_reads_back(path):
    """For each shipped class file, every edit the capability dialogs can make — an entry of `metrics` or `logs`
    rewritten, an entry added to `metrics`, `logs` and `dashboards`, the `backup` block replaced — passes the
    read-back, whether the file declares the block or not: a block the file does not hold is appended at the end
    of the file, which is what a first declare from a dialog is.

    Guards the post-condition against the tool's own files: it is asked on every plan, so a shape of a shipped
    class file it refused would turn that class's dialogs off."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    doc = yaml.safe_load(text) or {}
    for block, ident in (("metrics", "method"), ("logs", "method"), ("dashboards", "name")):
        for entry in (doc.get(block) or []) if ident == "method" else []:
            line = ["  - {method: %s, params: {zz: 1}}" % entry["method"]]
            out = upsert_into_block(text, block, entry["method"], line)
            require_reads_back(text, out, block, line, entry_id=entry["method"])
        line = ["  - {%s: zz_new}" % ident]
        require_reads_back(text, insert_into_block(text, block, line), block, line,
                           entry_id="zz_new" if ident == "method" else None, ident=ident)
    block_lines = ["backup:", "  capable: true", '  schedule: "0 2 * * *"']
    out = upsert_into_block(text, "backup", "backup", block_lines, list_block=False)
    require_reads_back(text, out, "backup", block_lines, mapping=True)


def test_what_the_writers_write_for_a_file_an_onboard_wrote_reads_back():
    """Over a class file as an onboard writes it (yaml.safe_dump: lists at the key's own column, `derived` marks):
    a `metrics` entry rewritten and one added, a `logs` entry rewritten and one added, a first `dashboards` block,
    a first `backup` block and that block replaced again — each passes the read-back.

    Guards the commonest file an operator has: one the tool wrote itself."""
    text = _AS_AN_ONBOARD_WRITES_IT
    for block, method, line in (("metrics", "blackbox", ["  - {method: blackbox, params: {probe: tcp_connect}}"]),
                                ("metrics", "snmp", ["  - {method: snmp}"]),
                                ("logs", "journald", ["  - {method: journald, params: {retention: 7d}}"]),
                                ("logs", "file_tail", ["  - {method: file_tail}"])):
        require_reads_back(text, _plan(text, block, method, line), block, line, entry_id=method)
    line = ["  - {gnet: 1860, name: node}"]
    require_reads_back(text, insert_into_block(text, "dashboards", line), "dashboards", line, ident="name")
    first = ["backup:", "  capable: true", '  schedule: "0 2 * * *"']
    with_backup = upsert_into_block(text, "backup", "backup", first, list_block=False)
    require_reads_back(text, with_backup, "backup", first, mapping=True)
    second = ["backup:", "  capable: true", '  schedule: "0 3 * * *"']
    require_reads_back(with_backup, upsert_into_block(with_backup, "backup", "backup", second, list_block=False),
                       "backup", second, mapping=True)


@pytest.mark.parametrize("text,entry_id", [
    ("key: demo\nmetrics:\n  - {method: a}\n# a note at the left margin\n  - {method: b, params: {p: 1}}\nrole: r\n", "b"),
    ("key: demo\nmetrics:\n# a note at the left margin\n  - {method: a}\n  - {method: b, params: {p: 1}}\n", "b"),
    ("key: demo\nmetrics:\n  - {method: 'b', params: {p: 1}}\nrole: r\n", "b"),
    ('key: demo\n"metrics":\n  - {method: a}\nrole: r\n', "c"),
    ("key: demo\nmetrics :\n  - {method: a}\nrole: r\n", "c"),
    ("\ufeffmetrics:\n  - {method: a}\nrole: r\n", "c"),
], ids=["a-comment-at-the-margin-between-two-entries", "a-comment-at-the-margin-under-the-key", "a-quoted-method",
        "a-quoted-key", "a-space-before-the-colon", "a-byte-order-mark-before-the-key"])
def test_a_shape_the_writers_do_not_model_is_refused_not_written(text, entry_id):
    """For each shape of a hand-written list the line writers still get wrong — a comment line at the left margin
    inside the list, a quoted method or key, a space before the colon, a byte-order mark in front of a first-line
    key — the text a writer returns does NOT read back as the planned change (an entry is there twice, or the key
    is), and the read-back refuses it as the repository's own data, naming the block and nothing of the file.

    Guards the quiet losses that are left after the two the writers were taught: without the read-back each of
    these plans answered "applied" for a file that held the entry twice or the block twice — YAML reads the last
    block only — while the structural no-drop check, which compares the plan, saw nothing."""
    line = ["  - {method: %s, params: {p: 2}}" % entry_id]
    out = _plan(text, "metrics", entry_id, line)
    with pytest.raises(paths.RepositoryDataError) as refused:
        require_reads_back(text, out, "metrics", line, entry_id=entry_id)
    message = str(refused.value)
    assert refused.value.status == 409 and "`metrics`" in message and "nothing was written" in message
    assert "demo" not in message and "{" not in message


def test_the_read_back_holds_the_rest_of_the_file_and_the_block_once():
    """Asked directly: an unchanged text passes; a text in which another key reads differently, in which the
    block's key is there twice although YAML's last-one-wins reading IS the plan, whose mapping block is not the
    rendered one, or that YAML cannot read at all, is refused.

    Guards each clause of the post-condition on its own — "changes nothing else", "the key once", "the block as
    rendered", "readable" — so that loosening one of them fails a row here and not only in a planner."""
    before = "key: demo\nrole: r\nbackup:\n  capable: true\n  schedule: old\n"
    block = ["backup:", "  capable: true", "  schedule: new"]
    good = "key: demo\nrole: r\nbackup:\n  capable: true\n  schedule: new\n"
    require_reads_back(before, before, "backup", block, mapping=True)            # nothing changed: nothing to ask
    require_reads_back(before, good, "backup", block, mapping=True)
    for bad in (good.replace("role: r", "role: other"),                           # another key reads differently
                before + "backup:\n  capable: true\n  schedule: new\n",          # the key twice; the last IS the plan
                good.replace("schedule: new", "schedule: newer"),                 # not the rendered block
                good + "  - not: [yaml\n"):                                       # not YAML
        with pytest.raises(paths.RepositoryDataError):
            require_reads_back(before, bad, "backup", block, mapping=True)


def test_the_read_back_compares_what_yaml_reads_by_type_and_a_list_in_its_order():
    """A file that holds `.nan` — in another key and inside the edited list — passes the read-back for a correct
    edit; a text in which another key's `1` has become `true` is refused, and so is one whose list holds the
    planned entries in another ORDER.

    Guards the comparison itself: Python holds `1`, `1.0` and `True` equal, so a plain `==` would pass a writer
    that changed a number into a keyword; and the order of a list is part of its reading (the generators render
    the entries in it), so a comparison that sorted it away would pass a writer that moved an entry."""
    before = "key: demo\nweight: .nan\nport: 1\nmetrics:\n  - {method: a, params: {p: .nan}}\n  - {method: b}\n"
    line = ["  - {method: b, params: {p: 2}}"]
    good = upsert_into_block(before, "metrics", "b", line)
    require_reads_back(before, good, "metrics", line, entry_id="b")
    moved = good.replace("  - {method: a, params: {p: .nan}}\n  - {method: b, params: {p: 2}}\n",
                         "  - {method: b, params: {p: 2}}\n  - {method: a, params: {p: .nan}}\n")
    assert moved != good
    for bad in (good.replace("port: 1", "port: true"), moved):
        with pytest.raises(paths.RepositoryDataError):
            require_reads_back(before, bad, "metrics", line, entry_id="b")


def test_a_module_file_with_no_document_reads_back_as_no_keys():
    """An empty module file, and one that holds only a comment, pass the read-back for a first `metrics` entry and
    a first `backup` block: what YAML reads from them is nothing, and the planners take that for a class with no
    keys.

    Guards a false refusal: the read-back asked for a mapping BEFORE the edit, so a plan the writers handle — the
    block appended to a file with nothing in it — was answered 409 with a sentence about how the block is
    written."""
    for before in ("", "# a class with nothing in it yet\n"):
        line = ["  - {method: a}"]
        require_reads_back(before, insert_into_block(before, "metrics", line), "metrics", line, entry_id="a")
        block = ["backup:", "  capable: true"]
        require_reads_back(before, upsert_into_block(before, "backup", "backup", block, list_block=False),
                           "backup", block, mapping=True)


def test_an_entry_the_list_holds_twice_is_refused_not_half_edited():
    """A `metrics` list that holds one method twice: rewriting that method is refused, and adding another method
    to the same file still passes.

    Guards the plan against the writer's choice of entry: the dialog shows the LAST entry of a method (the
    planner's reading is a mapping by method) and the line writer replaces the FIRST, so the plan said one value
    changed while the file kept it — and the same change was offered again on the next propose."""
    before = ("key: demo\nmetrics:\n  - {method: a, params: {p: 1}}\n  - {method: b}\n"
              "  - {method: a, params: {p: 2}}\n")
    line = ["  - {method: a, params: {p: 3}}"]
    with pytest.raises(paths.RepositoryDataError):
        require_reads_back(before, upsert_into_block(before, "metrics", "a", line), "metrics", line, entry_id="a")
    other = ["  - {method: c}"]
    require_reads_back(before, insert_into_block(before, "metrics", other), "metrics", other, entry_id="c")
