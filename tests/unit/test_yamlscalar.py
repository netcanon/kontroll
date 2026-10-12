"""kontroll.yamlscalar — may a string stand UNQUOTED in a hand-formatted YAML line, and how to write one that may not.

WHY: several writers format YAML by hand (to keep a file's comments, or because a generated file is rendered line by
line). A closed charset is not enough for a value that goes into such a line: `null`, `true` and `123` are made of
harmless characters and YAML reads each as another type; and a first version of this check tested the bare word
only — so a string holding U+2028 (which PyYAML keeps verbatim inside a bare scalar, and which ends a line) passed
it and added a top-level key through every scalar writer, and a string with a `,` passed it and was two entries
inside a flow mapping. The check is positional now. These tests pin the three functions against EVERY position a
writer uses, exhaustively over the indicator alphabet for short strings.
"""
import itertools

import pytest
import yaml

from kontroll import yamlscalar
from kontroll.yamlscalar import plain_scalar, quoted, word

pytestmark = pytest.mark.unit

LS, PS, NEL, BOM = chr(0x2028), chr(0x2029), chr(0x85), chr(0xFEFF)

# Every position a hand-formatting writer puts a value in: (template, what the document must parse to).
BLOCK = [
    ("%s", lambda v: v),                                                   # the bare word
    ("k: %s\n", lambda v: {"k": v}),                                       # upsert_top_level_key
    ("k: %s  # c\n", lambda v: {"k": v}),                                  # ...keeping the line's comment
    ("p:\n  c: %s  # c\n", lambda v: {"p": {"c": v}}),                     # set_nested_scalar
    ("l:\n  - %s\n", lambda v: {"l": [v]}),                                # set_list_block
    ("l:\n  - %s  # onboarded via scripts/galaxy.py\nz: 1\n", lambda v: {"l": [v], "z": 1}),   # enable_in_fleet
    ("a: 1\nk: %s\nz: 1\n", lambda v: {"a": 1, "k": v, "z": 1}),           # between two other keys
]
FLOW = [
    ("- {gnet: 5, name: %s}\n", lambda v: [{"gnet": 5, "name": v}]),       # observe._render_dashboard_entry
    ("- {method: m, params: {k: %s}}\n", lambda v: [{"method": "m", "params": {"k": v}}]),   # _render_metrics_entry
]
# YAML's indicators, the quote and escape characters, every kind of white space and line break, and a few ordinary
# characters that form keywords and numbers together.
ALPHABET = list("a1-:#,[]{}?&*!|>'\"%@`~=. \t\n\r0exyno_b+\\") + ["\x0b", "\x0c", "\x1c", NEL, "\xa0", LS, PS, BOM, "é"]


def _parse(template, value):
    try:
        return yaml.safe_load(template % value)
    except yaml.YAMLError as e:
        return ("unparseable", type(e).__name__)


def _short_strings():
    for n in (1, 2):
        for chars in itertools.product(ALPHABET, repeat=n):
            yield "".join(chars)


@pytest.mark.parametrize("value", ["cisco_ios", "node", "7d", "192.0.2.10", "example.test", "a-b", "v1", "keep-all",
                                   "self_signed", "2001:db8::1", "a b", "instance/trust/galaxy-pubkeys.gpg",
                                   "backup-cisco_ios", "if_mib"])
def test_plain_scalar_accepts_the_values_the_writers_exist_to_write(value):
    """Ordinary names, a duration preset, an address, a domain and a repo-relative path are plain in every
    position. Guards the check refusing — or a generator needlessly quoting — the values the shipped tree holds;
    a quoted value there would change a committed generated file."""
    assert plain_scalar(value) and plain_scalar(value, flow=True)
    assert word(value) == value


@pytest.mark.parametrize("value", ["null", "Null", "~", "true", "False", "yes", "no", "on", "off", "123", "0x10",
                                   "1.5", "017", "1_000", "+1", ".inf", "1:30", "20:1:2:3:4:5:6:7", "2026-10-12",
                                   "", " a", "a ", "a: b", "x #y", "[a]", "{a}", "- a", "*a", "&a", "!a", "?",
                                   "a\nb", "a\rb", "a" + NEL + "b", "a" + LS + "b", "a" + PS + "b",
                                   "a" + LS + "? injected", "a\x0bb", "a\x0cb", "a\x1cb", "a\tb", BOM + "a",
                                   "a" + BOM + "b", "2026-02-30", "0000-00-00", "2026-13-45", "9999-99-99", "0x_",
                                   "0b_", "0x__", "!!bool x", "!!int x", "!!timestamp x", "[" * 3000, "{" * 3000,
                                   None, 5, True, ["a"]], ids=lambda v: None if len(repr(v)) < 30 else "long")
def test_plain_scalar_refuses_what_yaml_reads_as_something_else(value):
    """A YAML keyword, a number in any of its spellings, a date, an empty or padded word, structure, an anchor or
    a tag, EVERY line-break character (the two Unicode separators included — PyYAML keeps those verbatim in a bare
    scalar, so a whole-document round trip is exact while the file has gained a line), a tab and a byte-order
    mark anywhere in the word, every non-string — and a word PyYAML resolves to a type and then cannot BUILD (a
    date that does not exist, `0x_`, a tag with a value it cannot convert, a nesting too deep to parse): those are
    refused, never raised. Guards a value that is inside a closed charset and still lands in a
    hand-formatted file as None, a boolean, a number, a date, or as a second key."""
    assert not plain_scalar(value)
    assert not plain_scalar(value, flow=True)


@pytest.mark.parametrize("value", ["a,b", "a]", "a}", "1,", "a[", "x{y"])
def test_a_flow_indicator_is_structure_only_inside_a_flow_mapping(value):
    """`a,b` is one plain word after `key: ` and two entries inside `{name: a,b}`; with `flow=True` it is refused.
    Guards the trap the first version of the check left: a caller that renders into a flow mapping and believes
    that passing the block-position check is enough."""
    assert plain_scalar(value)
    assert not plain_scalar(value, flow=True)


@pytest.mark.parametrize("value", ["y", "n", "Y", "N", "1e3", "0o17", "08", "None", "nil", "inf", "NaN"])
def test_words_pyyaml_reads_as_strings_are_accepted_and_this_table_is_the_tripwire(value):
    """PyYAML — the reader of every file these writers produce — takes each of these for a string, so they are
    accepted, although the YAML 1.1 text calls `y`/`n` booleans and a YAML 1.2 reader calls `1e3` and `0o17`
    numbers. PyYAML is a floor in the requirements files, not a pin: this table fails if a release changes what
    it resolves, which is the day the boundary would move without a diff in this repository."""
    assert plain_scalar(value)
    assert yaml.safe_load(value) == value


def test_every_accepted_short_string_reads_back_in_every_position_a_writer_uses():
    """Exhaustive over all strings of one and two characters from the indicator alphabet: each string
    `plain_scalar` accepts parses back as itself in all seven block positions, and each it accepts with `flow=True`
    also inside the two flow mappings. Guards the check being right about the bare word and wrong about the place
    the word is written — the defect of its first version. (The same run over three characters was made by
    hand when the check was written: no mismatch.)"""
    accepted = accepted_flow = 0
    for value in _short_strings():
        if plain_scalar(value):
            accepted += 1
            for template, want in BLOCK:
                assert _parse(template, value) == want(value), (value, template)
        if plain_scalar(value, flow=True):
            accepted_flow += 1
            for template, want in BLOCK + FLOW:
                assert _parse(template, value) == want(value), (value, template)
    assert accepted > accepted_flow > 0            # the flow rule is strictly narrower, and neither is vacuous


def test_quoted_reads_back_as_the_value_in_every_position_whatever_it_holds():
    """`quoted()` of EVERY short string over the indicator alphabet — quotes, backslashes, every line break, the
    Unicode separators, a byte-order mark — and of some longer hostile ones parses back as exactly that string, in
    block and in flow position, and the rendered form is one ASCII line. Guards the generators' fallback: a value
    they merely render (an inventory group, a host name) must not be able to end its line or its scalar."""
    hostile = ["null", "true", "123", 'a"b', "a\\b", "a\nb: c", "x" + LS + "? k", "é" + chr(0x1F600) + "z",
               "a: b #c", "- {x: y}", "\x00\x1f\x7f", " lead", "trail ", ""]
    for value in itertools.chain(_short_strings(), hostile):
        rendered = quoted(value)
        assert rendered.isascii() and not (yamlscalar.LINE_BREAKS & set(rendered)), value
        for template, want in BLOCK[1:] + FLOW:
            assert _parse(template, rendered) == want(value), (value, rendered, template)


def test_word_quotes_only_what_is_not_plain_and_never_refuses():
    """`word()` returns the bare text for a plain string, the quoted form for a string that is not, and the usual
    text for a value that is not a string (an int stays a bare number). In every case the line parses back to the
    value. Guards two things at once: a generated file changing by a byte for the values it already holds, and a
    generator — which runs at deploy time — refusing where it can simply quote."""
    assert word("core_switch") == "core_switch"
    assert word("null") == '"null"' and word("123") == '"123"' and word("true") == '"true"'
    assert word("a,b") == "a,b" and word("a,b", flow=True) == '"a,b"'
    assert word(9100) == "9100" and word(True) == "True"
    # ...including the words the reader resolves and cannot build: a host really can be named like an impossible date
    unbuildable = ("2026-02-30", "0000-00-00", "0x_", "0b_", "!!bool x", "[" * 3000)
    for value in ("core_switch", "null", "123", "true", "a b", "x" + LS + "y", 'q"q', 9100, True) + unbuildable:
        assert yaml.safe_load("k: %s  # c\n" % word(value)) == {"k": value}
    for value in unbuildable:
        assert word(value) == quoted(value) and word(value, flow=True) == quoted(value)


def test_plain_scalar_asks_the_reader_about_every_position_not_only_the_bare_word(monkeypatch):
    """With a reader that agrees about the bare word and disagrees after `key: ` — or after `- ` — `plain_scalar`
    refuses the word. On today's PyYAML no VALUE can show this: the line-break set refuses every string the
    positional parse refuses in block position, and each is the other's second layer. So the mechanism is shown
    directly. Guards the positional parse being dropped as redundant; it is what holds if a PyYAML release starts
    treating some other character as a line break or an indicator."""
    real = yaml.safe_load
    monkeypatch.setattr(yamlscalar.yaml, "safe_load",
                        lambda text: {"k": "something else"} if text.startswith("k: ") else real(text))
    assert not plain_scalar("word")
    monkeypatch.setattr(yamlscalar.yaml, "safe_load",
                        lambda text: ["something else"] if text.startswith("- word") else real(text))
    assert not plain_scalar("word")
    monkeypatch.setattr(yamlscalar.yaml, "safe_load", real)
    assert plain_scalar("word")


def test_plain_scalar_answers_every_string_and_never_raises():
    """For EVERY string of one to four characters over the characters numbers, dates and tags are made of,
    `plain_scalar` returns True or False — it never raises — and `word()` of the string reads back as the string.
    Guards the defect of the check's second version: it caught the YAML library's own error class only, while the
    library's value builders raise what Python raises (`0x_` is shaped like a hex number and has no digits; a
    well-shaped date need not exist). The check runs inside the deploy-time generators and inside a validator that
    is applied to names a DHCP client chose, so one such name stopped a deploy, or blanked the discovery panel."""
    for n in (1, 2, 3, 4):
        for chars in itertools.product("0xb_-+1.:eo", repeat=n):
            value = "".join(chars)
            answer = plain_scalar(value)                      # an exception here fails the test, with the value
            assert answer is True or answer is False, value
            assert yaml.safe_load("k: %s  # c\n" % word(value)) == {"k": value}, value
    for value in ("2026-02-30", "2026-13-45", "0000-00-00", "9999-99-99", "!!bool x", "!!int x", "!!float x",
                  "!!timestamp x", "[" * 3000, "{" * 3000, "- " * 3000, "9" * 5000):
        assert plain_scalar(value) is False and plain_scalar(value, flow=True) is False, value[:20]
