"""One flattener for every audit writer — the API's TSV log (api/audit.py) and the GUI's (gui/app.py).

An audit line is one row of tab-separated cells. Several routes audit request text BEFORE they validate it (a
class key, a method name, a free-text subject, a URL path variable), so a field can hold anything a JSON string
or a URL can: a tab would add a column, and a line break ends the ROW early — the rest of the genuine entry is
hidden from the reader, and, were tabs not flattened too, the remainder would read as an entry of the caller's
choosing. The reader opens the file with universal newlines, so a lone carriage return is a line break too; the
API's writer flattened tab and newline only until 2026-10-12, while the GUI's own copy of this function already
handled all three. One function, so the two cannot drift again.
"""

# Every character that some line-oriented consumer treats as the end of a line (the file iterator's \n, \r; and
# str.splitlines()'s wider set, which a log shipper or a later reader may use), plus the cell separator.
_BREAKS = dict.fromkeys(
    [ord(c) for c in "\t\n\r\x0b\x0c\x1c\x1d\x1e\x85"] + [0x2028, 0x2029], " ")


def flatten_field(value):
    """`value` as ONE cell on ONE line of a tab-separated audit row: a tab and every line-breaking character become
    a space. Applied to every field by both writers, so no caller has to remember it and no request value can add
    a column or end the row early."""
    # backslashreplace: a lone surrogate (a JSON string can carry one) cannot be encoded, and would make the API's
    # writer raise and the GUI's logging handler drop the whole line — it is written as its escape instead
    return str(value).translate(_BREAKS).encode("utf-8", "backslashreplace").decode("utf-8")
