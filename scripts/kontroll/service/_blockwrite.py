"""_blockwrite — the comment-preserving module.yml block writer + the param config-injection guard, SHARED by
every secondary-capability instance (observe/telemetry, logsvc/logging, …).

Lifted verbatim out of observe.py so the two instances (and any future one) write the DECLARED block the same
way and validate params against the same guard — one config-injection guard, one source of truth, so a refactor
can't silently re-diverge the copies. Behaviour-preserving: the existing telemetry tests exercise this via
observe's public API; test_blockwrite.py pins it directly. The per-VALUE checks live in service/_validate so the
guard widens (int/text/bool/ipv4/hostname, P0a) without re-diverging — the server-side twin of Phase-3's client
type→widget table.
"""
import os
import re

import yaml

from kontroll import paths
from kontroll.service._validate import plain_scalar, validate_value
from kontroll.yamlscalar import LINE_BREAKS


def _reads_back(value):
    """True when `value`, rendered unquoted by a writer below, is read back by YAML as the value given. The
    writers' last check, and a closed list of types: None (an empty value), a bool, an int, a float whose text
    YAML reads as that float, and a string that is plain where a writer puts it (`plain_scalar`). Anything else —
    a list, a mapping, a date — is refused: Python's text for a list or a dict IS YAML flow syntax, so it would
    land in the file as structure."""
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, str):
        return plain_scalar(value)
    if isinstance(value, float):
        try:
            return yaml.safe_load(str(value)) == value
        except Exception:          # noqa: BLE001 — the reader's refusal is whatever it raises (see kontroll.yamlscalar)
            return False
    return False


def write_module(plan):
    """Write a reconfigure plan's module text to its WRITE target and say whether anything was written. The one
    place the module reconfigures (telemetry, logging, backup, identity) write `modules/<key>/module.yml`.

    A plan reads the class through paths.module_file() — the clone's copy, or the baked one when the clone has
    none — and names its write target with paths.module_write_path(), which is always in the write root. The two
    are the same file except on a baked install for a shipped class the clone does not hold. Hence the first test:
    a plan that changes nothing writes nothing. Comparing against the write target alone would copy a shipped
    file into the clone on a no-op, and a second apply would stop reporting "no change". Nothing is written
    either when the target already holds the text; and the class directory is created when it exists only in the
    code root."""
    if plan["text_after"] == plan["text_before"]:
        return False
    target = plan["mod_path"]
    try:
        with open(target, encoding="utf-8") as fh:
            if fh.read() == plan["text_after"]:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as out:
        out.write(plan["text_after"])
    return True


def require_fleet_list():
    """Refuse — BEFORE a module reconfigure writes anything — when the fleet file holds no list of classes (the
    file is not a mapping, or `enabled_modules` is there and is not a list). Each generator renders nothing and
    prunes nothing for such a file and RETURNS non-zero; a caller that then lists the files on disk would stage
    the module change beside its old generated files and answer "applied". The fault is the repository's, not the
    request's (RepositoryDataError: 409 `repository_data`, an audit line of its own), and the sentence names the
    file and the rule, never a value. A fleet file that cannot be opened or parsed is not this guard's: the
    generator stops on that, loudly, as it always did."""
    try:
        with open(paths.resolve("config/fleet.yml"), encoding="utf-8") as fh:
            fleet = yaml.safe_load(fh) or {}
    except (OSError, ValueError, LookupError, AttributeError, TypeError, RecursionError, yaml.YAMLError):
        return
    if paths.fleet_list_unreadable(fleet):
        raise paths.RepositoryDataError(
            "fleet file", "instance/fleet.yml holds no list of classes (the file is not a mapping, or "
                          "enabled_modules is not a list), so nothing can be regenerated from it — correct the "
                          "file first; nothing was written")


def staged_paths(plan, regen):
    """The repo-relative paths a module reconfigure hands to the scoped commit: the regenerated files, and the
    module itself WHEN it is in the write root. An apply lists its paths whether or not it wrote the module, and a
    regenerator returns its files whether or not they changed — so a plan that changes nothing can still reach
    the commit. For a class only the image tree holds, the module is then a path that is not in the clone, and
    `git add` fails the whole stage for it. Leaving it out does not make that stage succeed — the commit then has
    nothing to commit — it only keeps a path that is not there out of the add."""
    module = [plan["mod_rel"]] if os.path.exists(plan["mod_path"]) else []
    return sorted(set(module + list(regen)))


def validate_params(method, params):
    """None if `params` satisfy the method's knob descriptors, else an error string. A required param missing, an
    unknown param name, or a value failing its knob's type/allow-list/range/pattern check (delegated to
    service/_validate.validate_value) is rejected — so a metacharacter-bearing value never reaches the rendered
    entry. Returns a message (the route turns it into a 422); never raises/sys.exits (a service must degrade,
    not crash the worker — unlike a generator's fail-closed sys.exit at deploy time). Backward-compatible: a
    legacy `{allowed, required}` param infers `type: enum` and validates exactly as before (P0a)."""
    given = params or {}
    spec = method.get("params") or {}
    for pname, pdef in spec.items():
        val = given.get(pname)
        if val is None:
            if pdef.get("required"):
                return "method %r requires param %r" % (method["name"], pname)
            continue
        bad = validate_value(dict(pdef, key=pname), val)   # key=pname so the message names the param
        if bad:
            return bad
    for pname in given:
        if pname not in spec:
            return "method %r takes no param %r" % (method["name"], pname)
    return None


def _split_inline_comment(rest):
    """Split a key's value-portion `rest` into (value, comment) where `comment` is a trailing ` # …` (a `#`
    preceded by whitespace) — so the upsert preserves an operator's inline annotation. A `#` NOT preceded by
    whitespace is part of the value, not a comment. Returns ("", "") gracefully for an empty rest."""
    for j in range(1, len(rest)):
        if rest[j] == "#" and rest[j - 1] in " \t":
            return rest[:j].rstrip(), rest[j:]
    return rest.strip(), ""


def upsert_top_level_key(text, key, value):
    """Return `text` with the TOP-LEVEL scalar `key`'s value replaced by `value` IN PLACE (comment- and
    sibling-preserving line-surgery), or `key: value` appended at EOF if the key is absent. The IDENTITY-key
    analogue of `upsert_into_block` (design 22 §5.4 G7): a `modules/<key>/module.yml` is hand-written with
    comments, so an identity re-classify must be a line-span swap, NEVER a `yaml.safe_dump` round-trip (which
    flattens the comments). A line is the target only when it is at column 0 AND the char after `key` is `:`
    (so `role:` is not matched by `roles:`); any inline trailing comment on that line survives. `value` MUST be
    pre-validated against its knob descriptor (service/_validate) before it reaches here — only allow-list/
    pattern-checked identity slugs render, so the upsert can't inject (the config-injection guard, like the
    capability path). PURE — returns text, writes nothing. FAIL-CLOSED: a multi-line `value` is REFUSED (a
    top-level scalar must be single-line — a `\n` would write a SECOND top-level key, a config injection). The
    caller validates first, so this never fires on the live path; it keeps the primitive safe even if a future
    descriptor adds a knob with a looser pattern (the no-bespoke-config tenet means a knob is a drop-in)."""
    rendered = "" if value is None else str(value)
    if LINE_BREAKS & set(rendered):                          # the set splitlines() below breaks on — not only \n, \r
        raise ValueError("upsert_top_level_key: a top-level scalar value must be single-line — refusing %r "
                         "(it would inject a second key)" % value)
    if not _reads_back(value):
        raise ValueError("upsert_top_level_key: the value for %s would not read back from YAML as the value given "
                         "(a keyword, a number or structure) — refusing" % key)
    lines = text.rstrip("\n").splitlines()
    for i, ln in enumerate(lines):
        if ln[:len(key)] == key and ln[len(key):len(key) + 1] == ":":   # top-level `key:` (col 0, exact)
            _, comment = _split_inline_comment(ln[len(key) + 1:])
            lines[i] = "%s: %s%s" % (key, rendered, ("  " + comment) if comment else "")
            return "\n".join(lines) + "\n"
    return "\n".join(lines + ["%s: %s" % (key, rendered)]) + "\n"      # absent → append at EOF (YAML order-free)


def set_nested_scalar(text, parent, child, value):
    """Return `text` with the nested scalar `parent.child` set to `value` IN PLACE (comment-/sibling-preserving),
    or inserting `child` under an existing `parent`, or appending `parent:`+`child` if absent. For
    `instance.yml frontend.tls_mode` (Phase 5). Like `upsert_top_level_key` but one level down: the `parent:` is a
    top-level key, the `child:` an indented key under it. FAIL-CLOSED on a multi-line value (a `\n` would inject a
    sibling key). PURE — returns text, writes nothing; the value MUST be pre-validated (service/_validate)."""
    rendered = "" if value is None else str(value)
    if LINE_BREAKS & set(rendered):
        raise ValueError("set_nested_scalar: a scalar value must be single-line — refusing %r" % value)
    if not _reads_back(value):
        raise ValueError("set_nested_scalar: the value for %s.%s would not read back from YAML as the value given "
                         "(a keyword, a number or structure) — refusing" % (parent, child))
    lines = text.rstrip("\n").splitlines()
    pstart = next((i for i, ln in enumerate(lines)
                   if ln[:len(parent)] == parent and ln[len(parent):len(parent) + 1] == ":"), None)
    if pstart is None:                                       # parent absent — append parent + child at EOF
        return "\n".join(lines + ["%s:" % parent, "  %s: %s" % (child, rendered)]) + "\n"
    end = _block_end(lines, pstart)
    for i in range(pstart + 1, end):
        ln = lines[i]
        stripped = ln.lstrip()
        if ln.startswith((" ", "\t")) and stripped[:len(child)] == child and stripped[len(child):len(child) + 1] == ":":
            indent = ln[:len(ln) - len(stripped)]
            _, comment = _split_inline_comment(stripped[len(child) + 1:])
            lines[i] = "%s%s: %s%s" % (indent, child, rendered, ("  " + comment) if comment else "")
            return "\n".join(lines) + "\n"
    lines.insert(pstart + 1, "  %s: %s" % (child, rendered))   # child absent — insert as the first line under parent
    return "\n".join(lines) + "\n"


def set_list_block(text, key, items):
    """Return `text` with the top-level list `key:` replaced by `items` IN PLACE (sibling-preserving). Replaces the
    `key:` line + its WHOLE block body (every indented line + any blank/comment line interleaved BETWEEN items),
    bounded by `_block_end` (the next dedented line) with a trailing-blank trim so the separator blank before the
    next key survives; an empty `items` renders the inline `key: []` form. For `instance.yml backup_remotes`
    (Phase 5). Handles both the inline (`key: []`) and block (`key:` + `  - a`) forms. NB: the list body is
    REGENERATED, so a per-item INLINE comment is not preserved (the items are name-only); a col-0 comment AFTER
    the list belongs to the next section and survives. Each item MUST be pre-validated. PURE — writes nothing.

    Bounding by the full block body (not just the leading run of `- ` lines) is the fix for the silent-orphan bug
    the adversarial review found (B2): a blank/comment line BETWEEN two items used to truncate the span, leaving
    the trailing items behind — so the operator's `['new']` landed as `['new', orphaned]`, invisible to the
    no-drop (lists are not descended) and to the token (computed over the same buggy bytes)."""
    for it in items:
        if not isinstance(it, str) or LINE_BREAKS & set(it):
            raise ValueError("set_list_block: list items must be single-line strings — refusing %r" % (it,))
        if not plain_scalar(it):
            raise ValueError("set_list_block: an item of %s would not read back from YAML as the string given "
                             "(a keyword, a number or structure) — refusing" % key)
    lines = text.rstrip("\n").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln[:len(key)] == key and ln[len(key):len(key) + 1] == ":"), None)
    new = ["%s:" % key] + ["  - %s" % it for it in items] if items else ["%s: []" % key]
    if start is None:                                       # absent — append the block at EOF
        return "\n".join(lines + new) + "\n"
    end = _block_end(lines, start)                          # the WHOLE block body (incl. interleaved blanks/comments)
    while end - 1 > start and not lines[end - 1].strip():   # …but keep the blank separator before the next key
        end -= 1
    return "\n".join(lines[:start] + new + lines[end:]) + "\n"


def disable_in_fleet(text, module):
    """Return `text` with `  - <module>` REMOVED from the `enabled_modules:` list (comment-preserving — only the
    one active item line is dropped; surrounding comments + sibling modules stay). The remove-half of
    onboard.enable_in_fleet (Phase 5). FAIL-CLOSED: RAISES (never sys.exit — a service must not kill the worker)
    if there is no `enabled_modules:` key or the module is not active. PURE — returns text, writes nothing; the
    caller (settings.apply_plan) does the I/O."""
    lines = text.rstrip("\n").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "enabled_modules:"), None)
    if start is None:
        raise ValueError("fleet.yml: no enabled_modules: key")
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith((" ", "\t")):   # dedented -> block ended
            break
        item = ln.strip()
        if item.startswith("- ") and item[2:].split("#")[0].strip() == module:
            return "\n".join(lines[:i] + lines[i + 1:]) + "\n"
    raise ValueError("fleet.yml: %r is not an active enabled_modules entry" % module)


def _block_start(lines, block_key):
    """The index of the top-level line that OPENS the block `block_key` — the key at column 0, its colon, and then
    nothing but blanks or a comment — or None when the file has no such key.

    A key line may carry a comment (`metrics:      # ORDER = …`, as one shipped class's does). The writers used to
    look for the bare `key:` line only: such a line was not found, the new entry was appended as a SECOND top-level
    block at the end of the file, and YAML reads the last of two — so every other entry of the class was gone from
    what the generators render, while the file still showed them. Two shapes are refused instead of written
    beside, as the repository's own data (409 `repository_data`, never the request's fault), naming the key and
    the rule: a key whose value is on its own line (`metrics: []`, a flow list — not a block this writer can
    edit), and a file that already holds the key twice (what the old behaviour left behind: YAML reads only the
    last block there, and a human has to merge the two)."""
    head = "%s:" % block_key
    found = None
    for i, ln in enumerate(lines):
        if not ln.startswith(head):
            continue
        value, _comment = _split_inline_comment(ln[len(head):])
        if value:
            raise paths.RepositoryDataError(
                "module file", "this class's module.yml writes `%s` with a value on the key's own line; the "
                               "writer edits a block — the key alone on its line, its entries on the lines below. "
                               "Rewrite it as a block first; nothing was written" % block_key)
        if found is not None:
            raise paths.RepositoryDataError(
                "module file", "this class's module.yml holds the top-level key `%s` twice; YAML reads only the "
                               "last, so the entries of the first are not in effect. Merge the two blocks by hand "
                               "first; nothing was written" % block_key)
        found = i
    return found


def insert_into_block(text, block_key, new_lines):
    """Return `text` with `new_lines` appended to the top-level `block_key:` list (preserving comments + the
    rest of the file). If the block exists, insert after its last (indented/blank) line; else append a fresh
    `block_key:` block at EOF. The comment-preserving analogue of onboard.enable_in_fleet — never a safe_dump
    round-trip (which would strip a hand-written module's comments)."""
    lines = text.rstrip("\n").splitlines()
    start = _block_start(lines, block_key)                   # a key line with a comment IS the block's start
    if start is None:
        return "\n".join(lines + ["%s:" % block_key] + new_lines) + "\n"
    end = _block_end(lines, start)
    return "\n".join(lines[:end] + _at_item_indent(lines, start, end, new_lines) + lines[end:]) + "\n"


def _block_end(lines, start):
    """The index ONE PAST the last line of the block that opens at `lines[start]` (`block_key:`) — the first
    dedented non-blank line after it, or EOF. Shared by insert/upsert so both bound a block identically. A list
    item at the key's own column is part of the block: `metrics:` / `- a` is how yaml.safe_dump writes a list, and
    so how an onboard writes one."""
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith((" ", "\t")):   # a dedented non-blank line — block ended…
            if ln == "-" or ln.startswith("- "):             # …unless it is an item at the key's own column
                continue
            return i
    return len(lines)


def _at_item_indent(lines, start, end, new_lines):
    """`new_lines` — rendered by the planners with a two-space item indent — moved to the indent the block's own
    items have. YAML reads one sequence only when its items share a column: a list written at the key's column
    (`metrics:` / `- a`, what yaml.safe_dump writes and so what an onboard writes) or four spaces in would
    otherwise be given an item at a column of its own, and the file would stop being YAML."""
    first = next((lines[i] for i in range(start + 1, end) if lines[i].lstrip().startswith("- ")), None)
    given = [ln for ln in new_lines if ln.strip()]
    if first is None or not given:
        return list(new_lines)
    have = len(first) - len(first.lstrip())
    lead = min(len(ln) - len(ln.lstrip()) for ln in given)
    if have == lead:
        return list(new_lines)
    return [(" " * have + ln[lead:]) if ln.strip() else ln for ln in new_lines]


_METHOD_RE = re.compile(r"\bmethod:\s*([A-Za-z0-9_.-]+)")


def _entry_method(span_lines):
    """The `method:` value of a list-item span (the first `method:` token across its lines) — matches both the
    flow item `- {method: snmp, params: {...}}` and the block item `- method: snmp`. None if absent."""
    m = _METHOD_RE.search("\n".join(span_lines))
    return m.group(1) if m else None


def upsert_into_block(text, block_key, entry_id, new_lines, list_block=True):
    """Like `insert_into_block`, but if an entry matching `entry_id` already exists in `block_key`, REPLACE its
    line-span IN PLACE (comment- and sibling-preserving); else append (delegates to `insert_into_block`). For a
    LIST block (telemetry `metrics:`, logging `logs:`) `entry_id` matches the list item whose `method:` ==
    entry_id; `new_lines` is the replacement item. For a MAPPING block (backup) pass `list_block=False` and
    `new_lines` is the FULL block (incl. the `block_key:` line), which replaces the existing block in place. The
    replace is a line-span swap — NEVER a `yaml.safe_dump` round-trip (which would flatten a hand-written
    module's comments, the reason `insert_into_block` exists). Pairs with `validate_params`: only allow-listed
    values reach `new_lines`, so the upsert can't inject (the config-injection guard survives the reconfigure
    path). The reconfigure G1 writer (design 22 §5.1)."""
    lines = text.rstrip("\n").splitlines()
    start = _block_start(lines, block_key)                   # a key line with a comment IS the block's start
    if not list_block:                                       # MAPPING block — replace `block_key:` + body, or append
        if start is None:
            return "\n".join(lines + new_lines) + "\n"
        if new_lines and new_lines[0].rstrip() == "%s:" % block_key:
            new_lines = [lines[start]] + list(new_lines[1:])  # the key line as it stands: its comment stays
        return "\n".join(lines[:start] + new_lines + lines[_block_end(lines, start):]) + "\n"
    if start is None:
        return insert_into_block(text, block_key, new_lines)
    end = _block_end(lines, start)
    item_lines = [i for i in range(start + 1, end) if lines[i].lstrip().startswith("- ")]
    if not item_lines:
        return insert_into_block(text, block_key, new_lines)
    # An item boundary is a `- ` at the BLOCK'S item indent (the first item's). A deeper `- ` is a nested LIST
    # VALUE inside an entry (e.g. a `modules:` sub-list), NOT a new item — including it would truncate the span
    # and orphan the nested lines into invalid YAML (adversarial-review SHOULD-FIX).
    item_indent = len(lines[item_lines[0]]) - len(lines[item_lines[0]].lstrip())
    item_starts = [i for i in item_lines if (len(lines[i]) - len(lines[i].lstrip())) == item_indent]
    for idx, i in enumerate(item_starts):
        span_end = item_starts[idx + 1] if idx + 1 < len(item_starts) else end
        if _entry_method(lines[i:span_end]) == entry_id:     # found the declared entry — swap its full span in place
            return "\n".join(lines[:i] + _at_item_indent(lines, start, end, new_lines) + lines[span_end:]) + "\n"
    return insert_into_block(text, block_key, new_lines)      # not declared — append (the add path)
