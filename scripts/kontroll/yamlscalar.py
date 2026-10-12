"""kontroll/yamlscalar — may a string stand UNQUOTED in a hand-formatted YAML line, and how to write one that may not.

WHY: several writers format YAML by hand. Some do it to keep a file's comments (the scalar and block writers of
`service/_blockwrite.py`, `service/onboard.enable_in_fleet`); the generators do it because their output is rendered
line by line (the Prometheus targets, the backup schedules, the two collection lockfiles). A closed CHARSET is not
enough for a value that goes into such a line. `null`, `true`, `no`, `123`, `0x10` and `2026-10-12` are made of
harmless characters and YAML reads each as another type; and a string can be plain as a whole document and still be
structure where a writer puts it — before a ` # comment`, after `- `, inside `{a: 1, k: …}` — or contain a
character that the YAML reader and `str.splitlines()` both treat as the end of a line.

Two rules, one home:

* a value the tool is ASKED to store (a class key, an inventory group, a dashboard name, a knob) must pass
  `plain_scalar()`, or the request is refused;
* a value a generator merely RENDERS (it was read back from a module file, the inventory, a unit descriptor) goes
  through `word()`: the bare word when it is plain, a double-quoted scalar when it is not. No NAME is refused at
  deploy time — a generator quotes what it cannot write bare — and none can add a line. Every value the shipped
  tree holds is plain, so no generated file changes by a byte.

Both rest on one property: `plain_scalar()` ANSWERS every string. PyYAML resolves a bare word to a type by its
shape and then builds the value, and the builders raise what Python raises — `2026-02-30` is shaped like a date
and is no date, `0x_` is shaped like a number and has no digits. Such a word is "not plain"; an exception
escaping here would be a deploy that dies on a host's name, or a discovery panel that dies on a name some DHCP
client chose.

"Reads back" means: by PyYAML, the reader of every file these writers produce (Ansible loads YAML with it too).
A YAML 1.2 reader differs on a few words (`1e3`, `0o17` are numbers there, `yes` and `no` are strings); the one
non-Python reader of a file these helpers write into is Prometheus, whose labels decode into strings whatever
the scalar looks like — except `null`, which is why a group named `null` must be quoted there. (The Homepage
tiles, read by a JavaScript YAML parser, are formatted by gen-homepage.py, which quotes every field itself.)
"""
import yaml

# Every character that ends a line for the YAML reader or for str.splitlines() — the writers split a file's text
# with splitlines(), so their idea of a line and the check's must be the same set. PyYAML FOLDS \n, \r and NEL
# inside a bare multi-line scalar (the round trip changes, so those were always refused), but it keeps U+2028 and
# U+2029 verbatim: `a<U+2028>? k` round-trips exactly as a document and is two lines, the second a new key.
LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85" + chr(0x2028) + chr(0x2029))
# ...plus the two a name never legitimately holds: a tab (separation in some positions, content in others) and
# the byte-order mark (PyYAML strips one at the start of a document and keeps one elsewhere).
_NEVER_PLAIN = LINE_BREAKS | frozenset("\t" + chr(0xFEFF))


def plain_scalar(value, flow=False):
    """True when the STRING `value`, written unquoted, reads back as the same string WHERE A WRITER PUTS IT: as a
    whole document, after `key: `, after `- `, each with a trailing ` # comment` — and, with `flow=True`, as a
    value inside a flow mapping (`{a: 1, k: <value>}`), where `,` `[` `]` `{` `}` are structure too.

    Refused: a word YAML resolves to another type (`null`, `~`, `true`, `no`, `on`, `123`, `1.5`, `0x10`,
    `2026-10-12`), and one it resolves and then cannot build (`2026-02-30`, `0x_`); anything holding `: `, ` #`, a
    leading indicator or surrounding space; the empty string; and any string with a line-break character, a tab
    or a byte-order mark. Accepted although a YAML 1.2 reader would disagree: `y`, `n`, `1e3`, `0o17` (strings to
    PyYAML — see the module docstring). Never raises: whatever the reader raises is a "no"."""
    if not isinstance(value, str) or not value or _NEVER_PLAIN & set(value):
        return False
    try:
        return (yaml.safe_load(value) == value
                and yaml.safe_load("k: %s  # c\n" % value) == {"k": value}
                and yaml.safe_load("- %s  # c\n" % value) == [value]
                and (not flow or yaml.safe_load("- {a: 1, k: %s}\n" % value) == [{"a": 1, "k": value}]))
    except Exception:              # noqa: BLE001 — not only yaml.YAMLError: PyYAML's constructors raise ValueError,
        return False               # KeyError, AttributeError, RecursionError for a word they resolve and cannot build


def quoted(value):
    """The string `value` as ONE double-quoted YAML scalar, in ASCII: `"` and `\\` escaped, every character outside
    printable ASCII written as `\\xNN`, `\\uNNNN` or `\\UNNNNNNNN`. It reads back as exactly `value` in every
    position (block or flow), whatever the value holds — no character of it can end the line or the scalar. (One
    value has no faithful form: a lone surrogate is written as its escape, which PyYAML reads back and a stricter
    reader refuses. No request can carry one — every name charset is ASCII.)"""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch in '"\\':
            out.append("\\" + ch)
        elif 0x20 <= code < 0x7F:
            out.append(ch)
        elif code <= 0xFF:
            out.append("\\x%02x" % code)
        elif code <= 0xFFFF:
            out.append("\\u%04x" % code)
        else:
            out.append("\\U%08x" % code)
    out.append('"')
    return "".join(out)


def word(value, flow=False):
    """`value` as one YAML scalar for a hand-formatted line a GENERATOR renders: the bare word when it is a plain
    string (`plain_scalar`), a double-quoted scalar (`quoted`) when it is a string that is not. A value that is not
    a string is formatted as it always was (an int stays a bare number, a bool a bare boolean) — its type is the
    declared one. Never refuses and never raises: a generator runs at deploy time, where either is an outage."""
    if not isinstance(value, str):
        return "%s" % (value,)
    return value if plain_scalar(value, flow=flow) else quoted(value)
