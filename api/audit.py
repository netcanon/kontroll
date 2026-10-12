"""API audit log + run_id minting — Layer 0 logging (docs/logging-architecture.md §3, §7).

Every privileged call writes one append-only TSV line — `ts, user, ip, action, run_id, detail` —
who/when/what, **never** a credential. Size-rotated (5 MB × 5, the GUI's RotatingFileHandler
semantics, done by hand so a write failure RAISES). `write_audit` raising is the point: a caller can
then fail-closed — an audited mutation that cannot be audited must not proceed. `read_audit` parses
the same TSV back for the query route.
"""
import os
from datetime import datetime, timezone

from kontroll.auditfmt import flatten_field
from kontroll.envguard import positive_int

# Rotation knobs (storage paradigm). FAIL-CLOSED via positive_int: an empty/unset/garbage env value falls to the
# 5 MB x 5 default, NEVER to 0 (= no rotation = an unbounded audit file, a C12 fail-open) — V1 must-fix M-5.
_MAX_BYTES = positive_int("KONTROLL_AUDIT_MAX_MB", 5) * 1024 * 1024
_BACKUPS = positive_int("KONTROLL_AUDIT_MAX_FILES", 5)
_FIELDS = ("ts", "user", "ip", "action", "run_id", "detail")


def mint_run_id() -> str:
    """A correlation id for one privileged request: `<UTC-stamp>-<6hex>`. Co-appears in the audit
    line and (when a route triggers Ansible) the `-e kontroll_run_id=…` it passes down."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()


def _rotate_if_needed(path: str) -> None:
    """Size-based rotation matching RotatingFileHandler (path → path.1 → … → path.N, oldest dropped)."""
    if os.path.exists(path) and os.path.getsize(path) >= _MAX_BYTES:
        for i in range(_BACKUPS - 1, 0, -1):
            src = "%s.%d" % (path, i)
            if os.path.exists(src):
                os.replace(src, "%s.%d" % (path, i + 1))
        os.replace(path, path + ".1")


def write_audit(path: str, user: str, ip: str, action: str, run_id: str, detail: str = "") -> None:
    """Append one append-only TSV audit line. A tab and every line-breaking character in a field are
    flattened (kontroll.auditfmt — the one flattener the GUI's writer shares), so a request value that
    is audited before it is validated cannot add a column or forge a row. RAISES (OSError) on write
    failure — callers fail-closed on it. NEVER pass a credential as any field."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe = [flatten_field(x) for x in (ts, user, ip, action, run_id, detail)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _rotate_if_needed(path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\t".join(safe) + "\n")


def read_audit(path: str, action=None, user=None, run_id=None, since=None, limit: int = 200) -> list:
    """Parse the TSV audit log into dicts (most recent first), filtered by action/user/run_id and a
    `since` ISO-timestamp (lexicographic on the UTC stamp). Returns [] if the log doesn't exist yet."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(_FIELDS):
                continue
            row = dict(zip(_FIELDS, parts[:len(_FIELDS)]))
            if action and row["action"] != action:
                continue
            if user and row["user"] != user:
                continue
            if run_id and row["run_id"] != run_id:
                continue
            if since and row["ts"] < since:
                continue
            rows.append(row)
    rows.reverse()
    return rows[:limit]
