"""homepage domain — the read+stage "arrange the portal" surface for the GUI Homepage editor (#136, Phase 2).

The `:3000` portal (gethomepage) only READS YAML — it has no write API — so "GUI-configurable homepage" means the
authenticated `:8443` GUI rewrites the overlay files `instance/dashboards/homepage/{services,settings}.yaml`. This
module is the read+plan+apply seam: `read_board()` parses the two files into ONE normalized board (sections[] +
items[] + order); `build_homepage_plan(board)` re-serializes the edited board back COMMENT- and
FLEET-MARKER-preserving, computes a diff vs current, and a `plan_token` over the exact bytes (PURE — writes
nothing); `apply_homepage_plan(plan)`/`promote_board(board, token)` token-gate + write. The write STAGES the usual
C10 `proposed/<run_id>` (the route calls `gitio.commit_and_push(run_id=…)`); the GUI never promotes.

MVP scope: **select which tiles appear (membership) + reorder tiles within a section + reorder sections.** The
serializer is a LINE-SPAN rewrite (never `yaml.safe_dump`, which strips a hand-written file's comments) — it only
RELOCATES or OMITS spans it parsed from the current file, NEVER synthesizes a new tile body (so no href/token can be
injected — C11 holds by construction). The generated `gen-homepage` fleet block (between its `# >>> kontroll fleet`
markers) is an OPAQUE, operator-immovable-contents span: the operator may reposition the whole block but never edit
its generated tiles (the #124 banner-keyed ownership idea applied to tiles). The homepage files are NOT in
`paths._OVERLAY_MAP`; they resolve at the literal path under `paths.ROOT` (read dynamically — tmp-repo testable),
exactly like `kontroll-init._personalize_homepage`. Reads degrade to a safe empty board; nothing here ever
`sys.exit`s (it would kill the in-process Flask worker).
"""
import difflib
import logging
import os

import yaml

from kontroll import paths
from kontroll.service.promote import plan_token, verify_token

log = logging.getLogger("kontroll.service.homepage")

FLEET_BEGIN = "# >>> kontroll fleet"        # gen-homepage's generated-block begin marker (prefix match)
FLEET_END = "# <<< kontroll fleet"          # …and its end marker


def _hp_path(name):
    """The literal overlay path of a homepage config file (NOT _OVERLAY_MAP). The homepage board is operator-mutable
    instance/ state (the GUI editor writes it + the propose path stages it), so it resolves under the WRITE root —
    the propose clone, never the read-only baked image. Read dynamically (tmp-repo testable); WRITE_ROOT==ROOT today."""
    return os.path.join(paths.write_root(), "instance", "dashboards", "homepage", name)


def _hp_read_path(name):
    """The path a READ of a homepage file resolves to: the overlay file whenever the instance/ overlay exists (or
    the file does), else the SHIPPED instance.example/ board — a public clone, a CI checkout, a node before
    `kontroll-init --fresh`. The same narrow READ-only tier `paths.resolve()` applies to the mapped instance paths
    (public split, 2026-10-08): the editor shows the example board instead of an empty panel, and the tier is off
    the moment an instance/ dir exists. Writes never come here — `_hp_path()` stays the overlay path, so the shipped
    example can never become a write target (apply_homepage_plan fails closed on an unconfigured tree)."""
    p = _hp_path(name)
    if os.path.exists(p) or os.path.isdir(os.path.join(paths.write_root(), paths.INSTANCE_DIR_NAME)):
        return p
    return os.path.join(paths.ROOT, paths.EXAMPLE_DIR_NAME, "dashboards", "homepage", name)


def _read(name):
    """The raw text of a homepage file, or '' if absent/unreadable (degrade, never raise). READ seam: falls through
    to the shipped example on an unconfigured tree (_hp_read_path)."""
    try:
        with open(_hp_read_path(name), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _safe_yaml(text):
    """yaml.safe_load(text) or None on any error (the MODEL read only — the WRITE never re-dumps)."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _name_of(line):
    """The key NAME from a `- <Name>:` list-map header line ('  - Foo bar:' -> 'Foo bar')."""
    s = line.strip()
    if s.startswith("- "):
        s = s[2:]
    return s[:-1].rstrip() if s.endswith(":") else s.rstrip()


# --- the LINE-SPAN tokenizer (round-trip-identity: same-order concat reproduces the file byte-for-byte) --------

def _split_services(text):
    """Tokenize services.yaml into (preamble, blocks). `preamble` = the file-header lines before the first group
    (never reordered). Each block is {kind: 'group'|'fleet', name, text}: a top-level `- <Group>:` block (its
    header + tile lines + trailing blanks, up to the next boundary) or the OPAQUE generated fleet block
    (`# >>> kontroll fleet` … to the next boundary). Boundaries are top-level `- ` group headers + the fleet-begin
    marker — never a `- ` INSIDE the fleet block. `"".join([preamble] + [b.text …])` == text (every line in one
    span)."""
    lines = text.splitlines(keepends=True)
    n = len(lines)
    fb_begin = fb_end = None
    for i, ln in enumerate(lines):
        st = ln.lstrip()
        if fb_begin is None and st.startswith(FLEET_BEGIN):
            fb_begin = i
        elif fb_begin is not None and fb_end is None and st.startswith(FLEET_END):
            fb_end = i
            break
    fleet = set(range(fb_begin, (fb_end if fb_end is not None else fb_begin) + 1)) if fb_begin is not None else set()
    heads = [i for i, ln in enumerate(lines) if ln.startswith("- ") and i not in fleet]
    bounds = sorted(heads + ([fb_begin] if fb_begin is not None else []))
    first = bounds[0] if bounds else n
    preamble = "".join(lines[:first])
    blocks = []
    for idx, b in enumerate(bounds):
        end = bounds[idx + 1] if idx + 1 < len(bounds) else n
        text_b = "".join(lines[b:end])
        if b == fb_begin:
            blocks.append({"kind": "fleet", "name": None, "text": text_b})
        else:
            blocks.append({"kind": "group", "name": _name_of(lines[b]), "text": text_b})
    return preamble, blocks


def _split_tiles(group_text):
    """Tokenize a group block into (header, lead, tiles). `header` = the `- <Group>:` line; `lead` = any lines
    between the header and the first tile; `tiles` = [{name, text}] one span per `<indent>- <Tile>:` item (its
    body + trailing lines, up to the next tile). `header + lead + "".join(t.text)` == group_text."""
    lines = group_text.splitlines(keepends=True)
    header = lines[0] if lines else ""
    tile_heads = [i for i, ln in enumerate(lines)
                  if i > 0 and ln.lstrip().startswith("- ") and (len(ln) - len(ln.lstrip())) > 0]
    lead = "".join(lines[1:tile_heads[0]]) if tile_heads else "".join(lines[1:])
    tiles = []
    for idx, t in enumerate(tile_heads):
        end = tile_heads[idx + 1] if idx + 1 < len(tile_heads) else len(lines)
        tiles.append({"name": _name_of(lines[t]), "text": "".join(lines[t:end])})
    return header, lead, tiles


def _fleet_group_names(services_text):
    """The group NAME(s) inside the generated fleet block (so read_board can flag those sections generated:true)."""
    _, blocks = _split_services(services_text)
    names = set()
    for b in blocks:
        if b["kind"] == "fleet":
            for ln in b["text"].splitlines():
                if ln.startswith("- "):
                    names.add(_name_of(ln))
    return names


def splice_fleet_block(current_text, block_text):
    """Insert or replace the generated fleet block in services.yaml text, comment- and operator-tile-preserving
    (PURE — no I/O; the generator-side inverse of the editor's reorder). The block boundary is _split_services'
    own, so gen-homepage (the PRODUCER) and the GUI editor (the CONSUMER) share ONE definition of "the fleet span"
    and can never drift on it. If a fleet block already exists its span is REPLACED in place (preserving its board
    position + all operator tiles outside the markers); else `block_text` is APPENDED after the last operator
    group, normalized to exactly one blank-line separator so a re-run is byte-identical (the generator's
    idempotency rests on this). `block_text` MUST begin with FLEET_BEGIN and end with FLEET_END + a trailing
    newline — `scripts/gen-homepage.py:render_fleet_block` produces it."""
    preamble, blocks = _split_services(current_text)
    had_fleet = any(b["kind"] == "fleet" for b in blocks)
    out = [preamble] + [block_text if b["kind"] == "fleet" else b["text"] for b in blocks]
    joined = "".join(out)
    if had_fleet:
        return joined
    sep = "" if (not joined or joined.endswith("\n\n")) else ("\n" if joined.endswith("\n") else "\n\n")
    return joined + sep + block_text


def _layout_order(settings_text):
    """The group key-order under settings.yaml `layout:` (the authoritative section order), [] if none."""
    doc = _safe_yaml(settings_text) or {}
    layout = doc.get("layout") if isinstance(doc, dict) else None
    return list(layout.keys()) if isinstance(layout, dict) else []


# --- read_board: the normalized board model -------------------------------------------------------------------

def read_board():
    """The normalized homepage board (title/theme + ordered sections[] each with ordered items[]) parsed from the
    two overlay files. Section order follows settings.yaml `layout:` key-order (services-only groups appended);
    tile order follows each services.yaml inner list. Per-tile `present:true`; a tile carrying a `widget:` block
    is flagged `has_widget`/`widget_type` (read-only — the widget's url/token is NEVER surfaced, C11). The
    generated fleet block's group is flagged `generated:true` (its items are operator-immovable). A
    missing/malformed file degrades to a safe partial board — NEVER raises (service-never-sys.exit)."""
    services_text, settings_text = _read("services.yaml"), _read("settings.yaml")
    sdoc = _safe_yaml(services_text) or []
    sett = _safe_yaml(settings_text) or {}
    layout = sett.get("layout") if isinstance(sett, dict) else {}
    layout = layout if isinstance(layout, dict) else {}
    generated = _fleet_group_names(services_text)
    sections = []
    for grp in sdoc if isinstance(sdoc, list) else []:
        if not isinstance(grp, dict) or len(grp) != 1:
            continue
        gname, tiles = next(iter(grp.items()))
        items = []
        for t in tiles or []:
            if not isinstance(t, dict) or len(t) != 1:
                continue
            tname, body = next(iter(t.items()))
            body = body if isinstance(body, dict) else {}
            widget = body.get("widget") if isinstance(body.get("widget"), dict) else None
            items.append({"name": tname, "href": body.get("href"), "description": body.get("description"),
                          "has_widget": "widget" in body, "widget_type": (widget or {}).get("type"),
                          "present": True})
        lay = layout.get(gname) if isinstance(layout.get(gname), dict) else {}
        sections.append({"name": gname, "generated": gname in generated,
                         "style": lay.get("style"), "columns": lay.get("columns"), "items": items})
    order = _layout_order(settings_text)
    sections.sort(key=lambda s: order.index(s["name"]) if s["name"] in order else len(order))
    return {"title": (sett.get("title") if isinstance(sett, dict) else None),
            "theme": (sett.get("theme") if isinstance(sett, dict) else None), "sections": sections}


# --- validate + serialize + plan + apply ----------------------------------------------------------------------

def _validate_board(board, current):
    """None if `board` is a legal reordering/selection of `current`, else an error string (the route turns it into
    422). GUARDS (config-injection + integrity): every section name in `board` EXISTS in `current` (no new
    sections); every item name EXISTS in its current section (no invented tiles / smuggled href); a generated
    section's items are UNCHANGED in membership (the operator may move the block, never edit its generated
    contents — #124 ownership). This is what makes the line-span serializer safe: it only ever relocates/omits
    spans the current file already contained."""
    cur_secs = {s["name"]: s for s in current.get("sections", [])}
    for sec in board.get("sections") or []:
        name = sec.get("name")
        if name not in cur_secs:
            return "validation: unknown section '%s'" % name
        cur_items = {it["name"] for it in cur_secs[name]["items"]}
        for it in sec.get("items") or []:
            if it.get("name") not in cur_items:
                return "validation: unknown tile '%s' in section '%s'" % (it.get("name"), name)
        if cur_secs[name]["generated"]:
            present = [it["name"] for it in (sec.get("items") or []) if it.get("present", True)]
            if present != [it["name"] for it in cur_secs[name]["items"]]:
                return "validation: the generated section '%s' is owned by gen-homepage — its tiles can't be edited" % name
    return None


def _reserialize_services(current_text, board):
    """Re-emit services.yaml in the board's section + tile order, OMITTING present:false tiles, comment- and
    fleet-marker-preserving. Operator tiles are relocated/dropped spans (never synthesized); the fleet block is
    re-emitted verbatim at its board position. Returns the new file text."""
    preamble, blocks = _split_services(current_text)
    by_name = {b["name"]: b for b in blocks if b["kind"] == "group"}
    fleet = next((b for b in blocks if b["kind"] == "fleet"), None)
    out = [preamble]
    for sec in board.get("sections") or []:
        if sec.get("generated") and fleet is not None:
            out.append(fleet["text"])
            continue
        blk = by_name.get(sec["name"])
        if blk is None:
            continue
        header, lead, tiles = _split_tiles(blk["text"])
        tile_by_name = {t["name"]: t for t in tiles}
        keep = [it["name"] for it in (sec.get("items") or []) if it.get("present", True)]
        body = "".join(tile_by_name[n]["text"] for n in keep if n in tile_by_name)
        out.append(header + lead + body)
    return "".join(out)


def _reserialize_layout(current_text, board):
    """Re-emit settings.yaml with the `layout:` group keys in the board's section order; every other line
    (title/theme/headerStyle, the `layout:` header, each group's style/columns sub-block, trailing comments)
    preserved verbatim. Round-trip-identity when the order is unchanged."""
    lines = current_text.splitlines(keepends=True)
    li = next((i for i, ln in enumerate(lines) if ln.rstrip().endswith("layout:") and not ln.startswith(" ")), None)
    if li is None:
        return current_text                                   # no layout: block — nothing to reorder
    base_indent = len(lines[li + 1]) - len(lines[li + 1].lstrip()) if li + 1 < len(lines) else 2
    # tokenize the layout sub-keys: each `<indent><Group>:` + its deeper-indented lines (+ trailing comments)
    subs, order, j = {}, [], li + 1
    cur_key, cur_lines = None, []
    while j < len(lines):
        ln = lines[j]
        indent = len(ln) - len(ln.lstrip())
        if ln.strip() and not ln.lstrip().startswith("#") and indent <= 0:
            break                                             # left the layout: block (a new top-level key)
        if indent == base_indent and not ln.lstrip().startswith("#") and ln.rstrip().endswith(":"):
            if cur_key is not None:
                subs[cur_key] = "".join(cur_lines)
            cur_key = _name_of(ln) if ln.lstrip().startswith("- ") else ln.strip()[:-1].rstrip()
            order.append(cur_key)
            cur_lines = [ln]
        elif cur_key is not None:
            cur_lines.append(ln)
        else:
            cur_lines.append(ln)                              # comments before the first key — keep with head
        j += 1
    head_extra = ""
    if cur_key is None:
        head_extra = "".join(cur_lines)
        cur_lines = []
    if cur_key is not None:
        subs[cur_key] = "".join(cur_lines)
    want = [s["name"] for s in (board.get("sections") or []) if s["name"] in subs]
    want += [k for k in order if k not in want]               # keep any layout keys the board didn't carry
    reordered = head_extra + "".join(subs[k] for k in want)
    return "".join(lines[:li + 1]) + reordered + "".join(lines[j:])


def build_homepage_plan(board):
    """PROPOSE (apply:false): re-serialize `board` into services.yaml + settings.yaml comment-/marker-preserving,
    compute the unified diff vs current, and return a PURE plan (writes nothing). Returns {error?, paths, diff,
    summary, text_after, token_parts, plan_token}. error ∈ {empty_board, validation:<msg>}."""
    cur = read_board()
    if not (board.get("sections")):
        return {"error": "empty_board"}
    verr = _validate_board(board, cur)
    if verr:
        return {"error": verr}
    svc_before, set_before = _read("services.yaml"), _read("settings.yaml")
    svc_after = _reserialize_services(svc_before, board)
    set_after = _reserialize_layout(set_before, board)
    diff = []
    for fname, before, after in (("services.yaml", svc_before, svc_after), ("settings.yaml", set_before, set_after)):
        ud = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                          fromfile=fname, tofile=fname))
        if ud:
            diff.append({"file": fname, "unified": ud})
    cur_present = {(s["name"], it["name"]) for s in cur["sections"] for it in s["items"]}
    new_present = {(s["name"], it["name"]) for s in board["sections"] for it in (s.get("items") or [])
                   if it.get("present", True)}
    removed = sorted(n for (sname, n) in (cur_present - new_present))
    removed_widgets = sorted(it["name"] for s in cur["sections"] for it in s["items"]
                             if it["has_widget"] and (s["name"], it["name"]) in (cur_present - new_present))
    paths_rel = ["instance/dashboards/homepage/services.yaml", "instance/dashboards/homepage/settings.yaml"]
    return {"error": None, "paths": paths_rel, "diff": diff,
            "summary": {"removed_tiles": removed, "removed_widgets": removed_widgets,
                        "changed": bool(diff)},
            "text_after": {"services": svc_after, "settings": set_after},
            "token_parts": [svc_after, set_after], "plan_token": plan_token(svc_after, set_after)}


def apply_homepage_plan(plan):
    """APPLY: write plan['text_after'] to the two overlay files, idempotent (identical content ⇒ changed:False,
    no write). Returns {error?, changed, paths}. Never raises; a write failure returns the code `write_failed`,
    never sys.exit, and never the OSError's text (an absolute path inside the container): the log line names the
    file, the exception's type and its errno, and the route answers with a fixed sentence (T20)."""
    ta = plan.get("text_after") or {}
    writes = (("services.yaml", ta.get("services")), ("settings.yaml", ta.get("settings")))
    changed = False
    name = None
    try:
        for name, after in writes:
            if after is None:
                continue
            if _read(name) != after:
                with open(_hp_path(name), "w", encoding="utf-8", newline="") as fh:
                    fh.write(after)
                changed = True
    except OSError as e:
        log.error("homepage write failed: file=%s type=%s errno=%s", name, type(e).__name__, e.errno)
        return {"error": "write_failed", "changed": changed, "paths": plan.get("paths")}
    return {"error": None, "changed": changed, "paths": plan.get("paths")}


def promote_board(board, token):
    """The token-gated apply spine (the capability.promote analogue): recompute the plan, verify_token against the
    exact bytes (drift → {"error": "drift"}, the route returns 409), else apply. NEVER promotes — the route stages
    proposed/<run_id> via gitio.commit_and_push (C10 two-key); this module never imports promote_ref."""
    plan = build_homepage_plan(board)
    if plan.get("error"):
        return {"error": plan["error"]}
    if not verify_token(token, *plan["token_parts"]):
        return {"error": "drift"}
    out = apply_homepage_plan(plan)
    out["paths"] = plan["paths"]
    return out
