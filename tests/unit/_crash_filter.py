"""The crash-storm rule's line filters, read for the two alerting test files (T32): every stage after the rule's
`line_format "{{.message}}"`, each a LogQL line filter on a double-quoted string or a backquoted one, and nothing
else. A double-quoted string may escape only a `"` or a `\\`, the two escapes whose Go reading is the character
itself; any other escape (a `\\n`, a `\\x41`, an invalid `\\.`, which LogQL refuses) and a newline inside one fail,
as does a stage this reader cannot read, such as an `or` between two strings: reading only the first `|~` stage,
and a double-quoted string's escapes raw, let a counted word through the crash-word hold (T32 fold 2 read A, S1:
G1, G2). Whitespace is LogQL's, a space, a tab, a CR or a newline: inside a string it is kept as written, and any
other character Python calls whitespace (a no-break space, a form feed, an em space) is text this reader cannot
read, as Loki refuses the query that holds one between two stages (fold 4's read 90, S1)."""
import re
import warnings

_WS = "[ \t\r\n]"
_DQ = r'"((?:[^"\\]|\\.)*)"'
_BQ = r"`([^`]*)`"
LINE_FILTER = re.compile(r"(\|~|!~|\|=|!=)" + _WS + "*(?:" + _DQ + "|" + _BQ + ")", re.S)
_TOKEN = re.compile(r'("(?:[^"\\]|\\.)*"|`[^`]*`)|' + _WS + "+", re.S)
_ESCAPE = re.compile(r"\\(.)", re.S)


def normal(expr):
    """`expr` with each run of LogQL whitespace outside a string made one space, none at either end, and every
    string kept as written (a `|` scalar's trailing newline is no part of the query)."""
    return _TOKEN.sub(lambda m: m.group(1) or " ", expr).strip(" \t\r\n")


def _unquote(quoted):
    odd = [m.group(0) for m in _ESCAPE.finditer(quoted) if m.group(1) not in '"\\'] + ["\n"] * ("\n" in quoted)
    assert not odd, "a double-quoted crash-filter string this reader does not unquote: %r" % odd
    return _ESCAPE.sub(r"\1", quoted)


def stages(expr):
    """[(operator, pattern)] for each line filter after `line_format "{{.message}}"` in the rule's `expr`, in order;
    AssertionError if any text but the range's closing `[5m]))` is left once they are removed. It reads one query
    expression: a rule's second query node is its callers' to refuse."""
    tail = normal(expr).split('line_format "{{.message}}"', 1)[1]
    left = re.sub(_WS + "+", " ", LINE_FILTER.sub(" ", tail)).strip(" ")
    assert left == "[5m]))", "a crash-filter stage this reader cannot read: %r" % left
    return [(op, raw or _unquote(quoted)) for op, quoted, raw in LINE_FILTER.findall(tail)]


# Escapes Python reads otherwise than RE2: a Perl class or word boundary (Unicode in Python, ASCII in RE2), `\Z`,
# `\N{…}`, a `\u` or `\U` code point (RE2 has none of the three) and a back-reference or what Python may read as one
# (RE2 reads `\12` as octal; a sound `\144` is refused with them).
_PY_ESCAPES = set("bBwWdDsSZNuU123456789")
# Groups RE2 has no syntax for: a lookaround, an atomic group, a conditional, a named back-reference, a comment.
_PY_GROUPS = ("(?=", "(?!", "(?<=", "(?<!", "(?>", "(?(", "(?P=", "(?#")
# A named group RE2 takes: its name ASCII letters, digits and `_` (Go's isValidCaptureName); Python takes any
# identifier.
_RE2_NAMED = re.compile(r"\(\?P<[A-Za-z0-9_]+>")
# What to write instead, in the refusal.
_ADMISSIBLE = ("write `[0-9]` for `\\d`, `[0-9A-Za-z_]` for `\\w`, `\\{` for a brace, a count spelled out "
               "(`[0-9][0-9][0-9]` for `[0-9]{3}`, `xx?` for `x{1,2}`) and a flag group only at the start; a `$`, a "
               "`\\b` and a lookaround have no spelling both read alike, so a filter matches a word, not its edges")


def python_only(pattern):
    """The constructs in `pattern` that Python reads otherwise than RE2, or that RE2 refuses, of those listed here:
    the escapes above, and a `\\` before a character that is not ASCII (RE2 refuses it); the groups above, and a named
    group whose name is not ASCII (RE2 refuses it); a possessive quantifier (`*+`, `++`, `?+`; a counted one, `{2}+`,
    falls with its `{`, and a literal `}` before a `+` both read alike); a flag but `i`, `m` or `s`; a `{` outside a
    set, so every counted repeat (RE2 reads a count with a leading zero, `{01}`, as the text and refuses a count, or a
    nested product, over 1000, where Python reads a repeat), a literal brace being written `\\{`; a `$` (Python's also
    matches before a final newline); and a `[:` inside a set (RE2 reads `[:name:]` as a POSIX class, and refuses one
    that names none, where Python reads the characters; refused even where no `:]` follows, which both read as the
    characters). A set's members are read as members, so `[*+$]`, `[{]` and `[(?=]` are sound; `[=` and `[.` are no
    class in RE2 and read alike. `(?i)` is let through though Python folds three letters that are not ASCII (a dotless
    `ı`, a long `ſ`, the Kelvin sign) that Loki's case-insensitive literal match does not: Python then keeps more lines,
    which only makes the crash-word hold stricter, and the lines the crash rule's test keeps are ASCII (fold 4's read
    100, S1; fold 5's reads 110, S1, N1, N2 and 111, N2, N3; fold 6's reads 120, N2 and 121, N6)."""
    found, i, in_set, n = [], 0, False, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            if pattern[i + 1] in _PY_ESCAPES or not pattern[i + 1].isascii():
                found.append(pattern[i:i + 2])
            i += 2
            continue
        if in_set:
            if c == "[" and pattern[i + 1:i + 2] == ":":
                found.append("[:")
            elif c == "]":
                in_set = False
            i += 1
            continue
        if c == "[":
            in_set, i = True, i + 1
            i += pattern[i:i + 1] == "^"
            i += pattern[i:i + 1] == "]"               # a `]` first in a set is a member
            continue
        group = next((g for g in _PY_GROUPS if pattern.startswith(g, i)), None)
        if group:
            found.append(group)
        elif pattern.startswith("(?P<", i):
            if not _RE2_NAMED.match(pattern, i):
                found.append(re.match(r"\(\?P<[^>]*>?", pattern[i:]).group(0))
        elif pattern.startswith("(?", i) and re.match(r"[A-Za-z-]", pattern[i + 2:i + 3]):
            flags = re.match(r"[A-Za-z-]*", pattern[i + 2:]).group(0)
            if set(flags) - set("ims-"):
                found.append("(?" + flags)
        elif c in "*+?" and pattern[i + 1:i + 2] == "+" and not pattern.startswith("(?", i - 1):
            found.append(c + "+")
        elif c in "${":
            found.append(c)
        i += 1
    return found


def compile_re2(pattern):
    """`pattern` compiled as Python reads it, refusing (ValueError, which says what to write instead) one that holds
    a construct `python_only` lists, or that Python compiles only with a warning (a set operation such as `[a&&b]`, a
    `[` first in a set). Python's cache is cleared first, so a pattern compiled before without the warning filter is
    compiled again under it (fold 4's read 90, N2)."""
    found = python_only(pattern)
    if found:
        raise ValueError("a crash-filter pattern Python reads otherwise than RE2: %r (%s)" % (found, _ADMISSIBLE))
    re.purge()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            return re.compile(pattern)
        except FutureWarning as w:
            raise ValueError("a crash-filter pattern Python compiles only with a warning: %s" % w) from w
