"""The promote instruction a staged write hands back exists once: `kontroll.service.pending.promote_hint` (T19).

WHY: the routes and the docs typed the instruction by hand and drifted into two forms — a `kontroll promote <run_id>`
command that no deploy ships (the launch kit's `./kontroll promote` runs `ansible-playbook promote` and fails), and
"promote run_id X in Semaphore", which stopped saying what to enter when the promote-proposal task began to take
`<run id>@<commit id>` and refuse a bare run id. The operator reads that line at the moment a change waits to land.
These pin the structure (every `next` a committing route answers, and every `next` taken when a write was staged, is
built from the helper, in each spelling the tests read) and the text (no tracked surface or document spells the dead
command or a bare run id to enter, in each spelling the scan reads); the helper's own words are pinned in
test_pending.py.
"""
import ast
import glob
import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTE_FILES = ["gui/app.py"] + sorted(
    os.path.relpath(p, ROOT).replace(os.sep, "/") for p in glob.glob(os.path.join(ROOT, "api", "routes", "*.py")))
# the route modules that stage a write and answer a `next` — each must build it from the helper
STAGING_ROUTES = {"gui/app.py", "api/routes/actuation.py", "api/routes/capability.py", "api/routes/onboard.py",
                  "api/routes/secrets.py"}


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _called(node):
    """The names `node` calls anywhere inside it (a bare name or an attribute's last part)."""
    return {getattr(n.func, "id", getattr(n.func, "attr", None)) for n in ast.walk(node) if isinstance(n, ast.Call)}


def _next_values(fn, scope=None):
    """Every value function `fn` gives a `next` key — `{"next": v}`, a `next=v` keyword to any call (`dict`, `jsonify`,
    a response model), `body["next"] = v`, `body.setdefault("next", v)` — with a bare name resolved to every value
    `fn` assigns that name or, when it assigns none and a `scope` is given (a branch read inside its function), every
    value `scope` assigns it. A conditional `next` is therefore read as an expression: an `if` statement that assigns
    the name in each branch yields both values (the committing routes' check holds each to the helper). A value that
    comes from elsewhere (a parameter, a module constant, an unpacking, `+=`) resolves to nothing."""
    values = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            values += [v for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant) and k.value == "next"]
        elif isinstance(n, ast.Call):
            values += [kw.value for kw in n.keywords if kw.arg == "next"]
            if (getattr(n.func, "attr", None) == "setdefault" and len(n.args) == 2
                    and isinstance(n.args[0], ast.Constant) and n.args[0].value == "next"):
                values.append(n.args[1])
        elif isinstance(n, ast.Assign):
            values += [n.value for t in n.targets if isinstance(t, ast.Subscript)
                       and isinstance(t.slice, ast.Constant) and t.slice.value == "next"]
    resolved = []
    for v in values:
        if isinstance(v, ast.Name):
            resolved += _assigned(fn, v.id) or (_assigned(scope, v.id) if scope is not None else [])
        else:
            resolved.append(v)
    return resolved


def _assigned(node, name):
    """Every value `node` assigns to the bare name `name` (`=` or an annotated assignment)."""
    return [n.value for n in ast.walk(node) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value
            and any(isinstance(t, ast.Name) and t.id == name
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]


def _staging_routes(rel):
    """Every function in a route module that commits a write (calls `commit_and_push`) and answers a `next`, as
    (name, line, [its `next` values])."""
    out = []
    for fn in ast.walk(ast.parse(_read(rel))):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and "commit_and_push" in _called(fn):
            values = _next_values(fn)
            if values:
                out.append((fn.name, fn.lineno, values))
    return out


def _staged_polarity(test):
    """1 when `test` is the staging outcome itself — a local `staged`, `x["staged"]`, `x.get("staged")`, `x.staged`,
    or one of them compared `is`/`==` to True — -1 when it is that outcome negated (`not`, `is False`, `is not True`,
    `!= True`), else None."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _staged_polarity(test.operand)
        return -inner if inner else None
    if (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq))
            and isinstance(test.comparators[0], ast.Constant) and isinstance(test.comparators[0].value, bool)):
        inner = _staged_polarity(test.left)
        if inner and isinstance(test.ops[0], (ast.IsNot, ast.NotEq)):
            inner = -inner
        return (inner if test.comparators[0].value else -inner) if inner else None
    flag = ((isinstance(test, ast.Name) and test.id == "staged")
            or (isinstance(test, ast.Attribute) and test.attr == "staged")
            or (isinstance(test, ast.Subscript) and isinstance(test.slice, ast.Constant)
                and test.slice.value == "staged")
            or (isinstance(test, ast.Call) and getattr(test.func, "attr", None) == "get" and test.args
                and isinstance(test.args[0], ast.Constant) and test.args[0].value == "staged"))
    return 1 if flag else None


def _staged_side(test):
    """Which branch of a conditional on `test` runs only when the write was staged: "body" for the outcome itself, or
    an `and` with it as an operand; "orelse" for its negation, or an `or` with the negation as an operand; else None
    (`a and not staged` makes the body an unstaged path and says nothing of the else)."""
    polarity = _staged_polarity(test)
    if polarity:
        return "body" if polarity > 0 else "orelse"
    if isinstance(test, ast.BoolOp):
        polarities = {_staged_polarity(v) for v in test.values}
        if isinstance(test.op, ast.And) and 1 in polarities:
            return "body"
        if isinstance(test.op, ast.Or) and -1 in polarities:
            return "orelse"
    return None


def _staged_branch(value):
    """For a `next` that is a conditional expression on whether the write was staged (`_staged_side`), the branch
    taken when it was; else None."""
    side = _staged_side(value.test) if isinstance(value, ast.IfExp) else None
    return getattr(value, side) if side else None


def _staged_nexts(fn):
    """Every `next` value function `fn` answers when the write was staged: the staged branch of a conditional
    expression on it; each `next` set in the staged branch of an `if` statement on it (`_staged_side`), a name there
    that the branch does not assign resolved in the whole function; and each value that branch assigns to a name the
    function answers as its `next`. Not read: a staged path reached past an early return on the unstaged one, the
    outcome under another name (`was_staged`), and `+=`."""
    answered = _next_values(fn)
    out = [b for b in (_staged_branch(v) for v in answered) if b is not None]
    for n in ast.walk(fn):
        side = _staged_side(n.test) if isinstance(n, ast.If) else None
        if side:
            branch = ast.Module(body=getattr(n, side), type_ignores=[])
            out += _next_values(branch, fn)
            assigned = {id(a.value) for a in ast.walk(branch) if isinstance(a, (ast.Assign, ast.AnnAssign))}
            out += [v for v in answered if id(v) in assigned]
    return out


def test_every_staged_next_is_built_from_the_helper():
    """Verifies: in the GUI app and every API route module, each function that commits a write and answers a `next`
    builds that `next` with `promote_hint` — in the branch taken when the write was staged, where the `next` is a
    conditional expression on that — in each spelling `_next_values` reads (a dict display, any call's `next=` keyword,
    an item assignment, `.setdefault`, a local variable); that in every function of those modules, committing or not,
    each `next` answered when the write was staged (a conditional on it, a `next` set or a name assigned in the branch
    of an `if` that only the staged path takes) calls the helper — a reply built by a helper the committing route calls,
    or a route that wraps a committing helper and sets the `next` itself; and each module that stages a write has a
    committing function. Guards against: a route (a new one, or one edited later) re-typing the instruction — that is
    how two of the forms above came to coexist, and how a response can show one instruction while the page shows
    another."""
    seen = set()
    for rel in ROUTE_FILES:
        for name, line, values in _staging_routes(rel):
            for value in values:
                built = _staged_branch(value) or value
                assert "promote_hint" in _called(built), (
                    "%s:%d %s builds its staged `next` by hand — use pending.promote_hint, in a conditional "
                    "expression on the staging outcome" % (rel, line, name))
            seen.add(rel)
        for fn in ast.walk(ast.parse(_read(rel))):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for value in _staged_nexts(fn):
                    assert "promote_hint" in _called(value), (
                        "%s:%d %s answers a staged `next` by hand — use pending.promote_hint" % (
                            rel, value.lineno, fn.name))
    assert STAGING_ROUTES <= seen, "a staging route lost its staged `next` (or the scan stopped finding it): %s" % (
        sorted(STAGING_ROUTES - seen))


@pytest.mark.parametrize("source, by_hand", [
    ('def f(git, body):\n    body.setdefault("next", "land it" if git["staged"] else "done")', True),
    ('def f(git, body):\n    if git.get("staged"):\n        body["next"] = "land it"', True),
    ('def f(body):\n    if body and body.get("staged"):\n        body["next"] = "land it"', True),
    ('def f(git, body):\n    if not git["staged"] or body is None:\n        return body\n'
     '    else:\n        body["next"] = "land it"', True),
    ('def f(git, body):\n    nxt = "land it"\n    if git["staged"]:\n        body["next"] = nxt', True),
    ('def f(git):\n    if git["staged"]:\n        nxt = "land it"\n    else:\n        nxt = "done"\n'
     '    return dict(next=nxt)', True),
    ('def f(g):\n    return dict(next="land it" if g.staged else "done")', True),
    ('def f(git):\n    return dict(next="done" if not git["staged"] else "land it")', True),
    ('def f(git, body):\n    if git["staged"] is True:\n        body["next"] = "land it"', True),
    ('def f(git, body):\n    if git["staged"] is False:\n        body["next"] = "done"\n'
     '    else:\n        body["next"] = "land it"', True),
    ('def f(git, run_id):\n    return dict(next=promote_hint(run_id) if git["staged"] else "done")', False),
    ('def f(git, body):\n    body["next"] = promote_hint(1)\n    if git["committed"] and not git["staged"]:\n'
     '        body["next"] = "done"', False),
    ('def f(git, body):\n    body["next"] = promote_hint(1)\n    if git["staged"] is False:\n'
     '        body["next"] = "done"', False),
    ('def f(git, body):\n    nxt = "done"\n    if git["staged"]:\n        nxt = promote_hint(1)\n'
     '        body["next"] = nxt', False),
])
def test_the_structural_reader_finds_each_staged_next(source, by_hand):
    """Verifies: `_staged_nexts` finds a `next` typed by hand on the staged path in each shape sampled here — a
    `.setdefault`, an `if` on the outcome (`.get`, an `and` with it, the `else` of an `or` with its negation, `is
    True`, the `else` of `is False`), a name assigned before that `if`, a name an `if`/`else` statement assigns, an
    attribute flag, a negated conditional expression — and refuses none of the helper's own shapes, an `if` that
    only the unstaged path takes (`committed and not staged`, `is False`), or a name the staged branch assigns from
    the helper. Guards against: the reader of the routes going blind, or refusing correct code, with every test green
    — the routes in the tree use few of these shapes, so only samples can show that it still reads the rest."""
    fn = ast.parse(source).body[0]
    assert any("promote_hint" not in _called(value) for value in _staged_nexts(fn)) == by_hand


def test_no_next_in_a_route_spells_a_promote_by_hand():
    """Verifies: no `next` any route answers — committing or not — holds a string that mentions promoting unless it
    also calls `promote_hint`. Guards against: a hand-typed promote instruction in a route the structural test
    above does not reach (one that answers a `next` without committing itself)."""
    for rel in ROUTE_FILES:
        for fn in ast.walk(ast.parse(_read(rel))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for value in _next_values(fn):
                # a word test: a route with no commit has no structural signal, so a neutral wording passes, and
                # a `next` naming the GUI's staging button ("press Promote") is refused — use the helper or reword
                says = any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "promot" in n.value.lower()
                           for n in ast.walk(value))
                assert not says or "promote_hint" in _called(value), (
                    "%s:%d %s answers a promote instruction typed by hand" % (rel, value.lineno, fn.name))


# Each a regex over text in `_plain` form, so "enter the **`run_id`**" is read as "enter the run_id". A run id is
# `run id`, `run_id`, `run-id` or `runid`; one followed by `@` (`<run id>@<commit id>`) is the pinned form, never a
# bare one; a possessive (`run id's proposal`) names nothing to enter. A run id that ends a string, or is followed by a
# colon in prose, is still read; a key in code (`"run_id": …`, `{{ run_id }}`) never is, since no pattern reaches a run
# id across a quote, a colon or a brace.
_BARE = r"\brun[ _-]?id\b(?!>?@)(?!'s\b)"
_DEAD = [r"\bkontroll promote\b(?!-)",                       # the command no deploy ships (not `promote-proposal`)
         r"\bpromote (a |the |each |its )?<?" + _BARE,         # a bare run id as what is promoted
         r"\b(enter|paste|type)\b[^.@\"':={}]{0,30}" + _BARE,  # ... or entered (never across a quote, `:`, `=`, `{`)
         r"\brun[ _-]?id survey field\b"]                     # the field takes <run id>@<commit id>
# history and private material: the review dossiers and the CHANGELOG may quote the old text; local/ and instance/
# are an operator's own (not shipped)
_NOT_SHIPPED = ("docs/reviews/", "CHANGELOG.md", "local/", "instance/")
# this file: its samples quote the stale forms on purpose
_SELF = "tests/unit/test_promote_hint_single_source.py"
# the text a tracked file can carry an operator reads: documents, code, the page, compose and Semaphore YAML, scripts,
# the dashboards' panel text
_TEXT = (".md", ".py", ".html", ".js", ".json", ".yml", ".yaml", ".sh", ".ps1", ".j2", ".txt", ".cfg", ".toml",
         ".template", ".example", "Dockerfile")
# directories a walk does not enter (only outside a git tree): the git store, caches, and what a checkout grows
_WALK_SKIPS = (".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
               "node_modules", "local", "ansible_collections")


def _listed():
    """The tracked files (`git ls-files`) — or, outside a git tree (an archive or export, which holds only shipped
    files), every file a walk finds outside the skips above. A git tree git cannot list (a worktree copied away from
    its repository, a checkout owned by another user) fails the test, as tests/check-doc-links.py does: a walk there
    would read the operator's untracked notes and skip nothing they hold."""
    if os.path.exists(os.path.join(ROOT, ".git")):
        try:
            listing = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True)
        except OSError as exc:
            pytest.fail("git could not be started to list this tree (%s)" % exc.__class__.__name__)
        said = listing.stderr.decode("utf-8", "replace").strip().splitlines()
        assert listing.returncode == 0, "git could not list this tree: %s" % (said[0] if said else listing.returncode)
        return [p for p in listing.stdout.decode("utf-8").split("\0") if p]
    found = []
    for directory, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIPS]
        found += [os.path.relpath(os.path.join(directory, n), ROOT).replace(os.sep, "/") for n in names]
    return found


def _shipped_text_files():
    """Every tracked text file but the history, an operator's own, and this test (whose samples quote the stale
    forms on purpose)."""
    return sorted(f for f in _listed() if f.endswith(_TEXT) and not f.startswith(_NOT_SHIPPED) and f != _SELF
                  and os.path.isfile(os.path.join(ROOT, f)))


def _plain(text):
    """`text` lower-cased, with the entities `&nbsp;`, `&#160;`, `&lt;` and `&gt;` decoded and these removed: `*`, a
    backtick, `__`, and an HTML tag with no attributes or with `name=value` ones (`<b>`, `<code class="x">`, never
    `<run id>`). Single-underscore emphasis is not removed — `_` is part of `run_id`."""
    text = text.lower()
    for entity, char in (("&nbsp;", " "), ("&#160;", " "), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    return re.sub(r"</?[a-z][a-z0-9]*(?:\s+[a-z-]+=[^<>]*)?>|[*`]|__", "", text)


def _hits(pattern, text):
    """The numbers of the lines of `text` where `pattern` matches in `_plain` form, whitespace collapsed — on the
    line alone, or across the break into the next one with that line's own comment, quote or list marker (`#`, `//`,
    `>`, `-`, `|`) dropped, so a wrapped comment reads as one sentence — each counted once."""
    lines = [re.sub(r"\s+", " ", _plain(line)).strip() for line in text.splitlines()] + [""]
    hits = []
    for i, (line, after) in enumerate(zip(lines, lines[1:]), 1):
        cont = re.sub(r"^(#+|//|>|-|\|)\s*", "", after)
        if re.search(pattern, line) or (re.search(pattern, line + " " + cont) and not re.search(pattern, cont)):
            hits.append(i)
    return hits


# the patterns of _DEAD, by index: 0 the dead command, 1 a bare run id promoted, 2 one entered, 3 the survey field
@pytest.mark.parametrize("text, expected", [
    ("run `kontroll promote <run_id>` on the control node", {0, 1}),
    ("a trusted `kontroll promote` lands it", {0}),
    ("Promote the **`run_id`** in Semaphore.", {1}),
    ("Promote `<run_id>` in Semaphore.", {1}),
    ("then promote &lt;run_id&gt; in Semaphore", {1}),
    ("promote the runid", {1}),
    ("To promote a `run_id`:", {1}),
    ("msg = 'staged; promote the run_id';", {1}),
    ("Enter `<run_id>` in the promote-proposal survey.", {2}),
    ("enter the run id from the onboard response", {2}),
    ('return "then enter the run_id"', {2}),
    ("paste the\n`run_id` into the survey", {2}),
    ("type the run-id here", {2}),
    ("# then promote the\n# run_id in Semaphore", {1}),
    ("// then promote the\n// run_id in Semaphore", {1}),
    ("> then promote the\n> run_id in Semaphore", {1}),
    ("- then promote the\n- run_id in Semaphore", {1}),
    ("| promote the\n| run_id |", {1}),
    ("promote the  \nrun_id in Semaphore", {1}),
    ("promote  the run_id", {1}),
    ("promote __run_id__", {1}),
    ("promote the <code class=\"x\">run_id</code>", {1}),
    ("promote&nbsp;run_id", {1}),
    ("promote&#160;run_id", {1}),
    ("the <b>run_id</b> survey field", {3}),
    ("the run id survey field", {3}),
    ("paste `<run id>@<commit id>` into the *Proposal run_id@commit* field", set()),
    ("enter the &lt;run id&gt;@&lt;commit id&gt; its row copies", set()),
    ("the run id the response carries", set()),
    ("open the `promote-proposal` template", set()),
    ("`scripts/kontroll-promote.py <run_id>@<commit_id>`", set()),
    ("then promote proposed/<run_id>", set()),
    ("kontroll promotes nothing on its own", set()),
    ("promote each run id's proposal", set()),
    ('return {"type": "keygen", "run_id": run_id}', set()),
    ('<input type="text" name="run_id">', set()),
    ("Type {{ run_id }}@{{ commit }}", set()),
    ("type = run_id or None", set()),
])
def test_the_scan_catches_each_stale_form(text, expected):
    """Verifies: each pattern of the scan below finds the stale form it is for, and no other pattern stands in for it,
    in each disguise sampled here — the dead command, a bare run id to promote or to enter, paste or type (a
    placeholder `<run_id>` included, in markup too), the run id survey field; emphasised, tagged, entity-joined,
    hyphenated, run together, ending a string or before a colon, or wrapped across a line break, a comment's, quote's,
    list item's or table row's included — and that the scan passes the pinned form, a run id that is not an
    instruction, the `promote-proposal` task and the CLI by name, and a run id that is a key in code. Guards against:
    the scan going blind with every test green — dropping the join, a pattern, a marker or the emphasis stripping, or
    widening an exemption, to silence a false positive — since the shipped tree, being clean, can never show that it
    stopped seeing anything."""
    assert {i for i, pattern in enumerate(_DEAD) if _hits(pattern, text)} == expected


@pytest.mark.parametrize("pattern", _DEAD)
def test_no_surface_or_document_spells_the_promote_instruction_by_hand(pattern):
    """Verifies: no code surface (GUI app, API routes, the service package, the page) and no shipped document, compose
    comment, Semaphore template, playbook or script says `kontroll promote` (a command no deploy ships) or tells the
    operator to promote, enter, paste or type a bare run id (the promote-proposal task refuses one) — with `*`,
    backtick or `__` emphasis, in HTML tags or entities, and across one line break (each line is read with the next
    one joined to it, that line's comment, quote or list marker dropped). Not read: single-underscore emphasis, a
    three-line wrap, a string concatenated in code, a markdown link's text. A run id that is no promote instruction
    and still matches (the log-correlation key: "enter a run id to filter the audit log") is reworded, not exempted.
    Guards against: the dead command, or the stale form, coming back by hand — the operator pastes it and is refused
    at the moment a change is waiting. Reviews and the CHANGELOG are history and may quote the old text; this file
    quotes it as samples."""
    files = _shipped_text_files()
    assert {"docs/install-from-scratch.md", "config/semaphore/templates/promote-proposal.yml", "START-HERE.md",
            "SECURITY.md", "tests/testid_reference.md", "gui/templates/index.html", "gui/app.py",
            "scripts/kontroll/service/pending.py", "ansible/playbooks/promote.yml",
            "scripts/kontroll-promote.py", "docker/services/api.yaml", "instance.example/OPERATIONS.md",
            "key-roles/README.md", "scripts/update-from-bundle.sh", "dashboards/grafana/dashboards/logs.json",
            "tests/validate.sh", ".github/workflows/ci.yml"} <= set(files)
    hits = ["%s:%d" % (rel, i) for rel in files for i in _hits(pattern, _read(rel))]
    assert hits == [], "%r matches at %s" % (pattern, hits)
