"""The audit line format: one request value is one cell of one row, whatever characters it holds.

WHY: several routes audit request text BEFORE they validate it — a class key, a method name, a free-text subject,
a URL path variable. The API's writer flattened a tab and a newline but not a carriage return, and its reader
opens the log with universal newlines, where a lone carriage return ends a line. One such byte in a field CUT THE
ROW SHORT: everything the route recorded after that field (on a promote: the method, the token prefix, the push
flag) vanished from the audit query, and the remainder sat in the file as a line of its own. (It could not forge a
whole row — tabs were flattened, so the remainder never had enough cells to be read as one.) The GUI's writer had
its own copy of the flattener, which did handle it: two copies of one rule had drifted. Both now call
`kontroll.auditfmt.flatten_field`; these tests pin the function, the API writer through a real file, and that the
GUI agrees with it.
"""
import pytest

from api.audit import read_audit, write_audit
from kontroll import auditfmt

pytestmark = pytest.mark.unit

LINE_BREAKS = {"LF": "\n", "CR": "\r", "CRLF": "\r\n", "VT": "\x0b", "FF": "\x0c", "FS": "\x1c", "GS": "\x1d",
               "RS": "\x1e", "NEL": "\x85", "LS": chr(0x2028), "PS": chr(0x2029)}
SEPARATORS = dict(LINE_BREAKS, TAB="\t")


@pytest.mark.parametrize("name", sorted(SEPARATORS))
def test_flatten_field_leaves_no_separator_of_any_kind(name):
    """`flatten_field` turns a tab and every character a line-oriented reader treats as the end of a line into a
    space, and leaves the rest of the text alone. Guards a field adding a column (a tab) or ending the row early (a
    line break) in a tab-separated audit line — including the breaks that only `str.splitlines()` honours, which a
    log shipper or a later reader may use."""
    out = auditfmt.flatten_field("key=a" + SEPARATORS[name] + "REST")
    assert out.splitlines() == [out] and "\t" not in out
    assert out.startswith("key=a") and out.endswith("REST")
    assert auditfmt.flatten_field(12) == "12" and auditfmt.flatten_field(None) == "None"


def test_flatten_field_writes_an_unencodable_character_as_its_escape():
    """A lone surrogate — which a JSON string can carry — comes out as a backslash escape, so the field can be
    encoded. Guards the audit line being lost: the API's writer raised on it (a 500 before any mutation) and the
    GUI's logging handler dropped the whole line without a word."""
    out = auditfmt.flatten_field("key=a" + chr(0xD800) + "b")
    assert out.encode("utf-8") and "d800" in out.lower() and out.startswith("key=a") and out.endswith("b")


def test_a_carriage_return_in_a_field_cannot_cut_the_audit_row_short(tmp_path):
    """`write_audit` with a detail that holds a carriage return in the middle, read back with `read_audit`, gives
    exactly ONE row whose detail still holds everything after the carriage return. Guards the reader's
    universal-newline mode ending the row at that byte and hiding the rest of the entry from the audit query — the
    defect is only visible through the real file, which is why this writes one."""
    log = str(tmp_path / "audit" / "api-audit.log")
    write_audit(log, "api-token", "192.0.2.9", "capability-promote", "RUN-1",
                "cap=telemetry key=x\rmethod=snmp token=abc push=True")
    rows = read_audit(log)
    assert len(rows) == 1, rows
    assert rows[0]["user"] == "api-token" and rows[0]["run_id"] == "RUN-1"
    assert rows[0]["detail"].endswith("method=snmp token=abc push=True"), "the rest of the entry was cut off"


def test_the_gui_writer_uses_the_same_flattener():
    """The GUI's audit writer agrees with `kontroll.auditfmt.flatten_field` on every separator: it calls that
    function instead of carrying a copy. Guards the drift that caused the defect — two implementations of one rule,
    one of which was missing a character."""
    import app as gui          # gui/ is on sys.path (conftest)
    for sep in SEPARATORS.values():
        text = "a" + sep + "b"
        assert gui._flatten_audit_field(text) == auditfmt.flatten_field(text)
    assert gui._flatten_audit_field("a\rb") == "a b"
