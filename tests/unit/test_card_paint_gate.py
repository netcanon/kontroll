"""Only LITERAL markup may reach `innerHTML` in the GUI — SECURITY.md C19, enforced as a class.

WHAT THIS VERIFIES: every place where `gui/templates/index.html` turns a string into markup — the markup argument
of each `E(...)` call (`E` assigns it to `innerHTML`) and the right-hand side of each `.innerHTML =` — is a
concatenation of string literals written in the template and nothing else: no variable, no call, no ternary, no
template interpolation. Around that rule it closes the ways past it that need no markup at all: the sink may not be
spelled any other way (an alias, a literal bracket key, another HTML-parsing API), a link's `href` is assigned in
exactly one helper and only from `safeHref()`, no attribute or property that executes or navigates is set from
script, every element is created from one literal, inert tag name, every request is a direct `fetch()` whose path
carries each dynamic segment encoded (and the few names that stand in a path unencoded are pinned to where their
value comes from), and the parts of the file the script scan does not read (a second script, a handler attribute, a
template expression anywhere) are pinned to what they are today.

WHY (the failure it guards against): the GUI paints values it did not author — a staged proposal's ref name, commit
subject and path names; an API error string, which can quote request input or a remote response; a third-party
Galaxy record; a device byte. Painted through `innerHTML`, any of them that carries `<img src=x onerror=…>` runs
script in the operator's authenticated session, with every `POST` route the GUI exposes in reach. The first version
of this gate was a DENYLIST: it searched `E()` arguments for seven substrings (`rec.`, `data.error`, …). It could
not see a value under any other name — the Pending panel painted `p.run_id`, `p.subject` and `p.paths` straight
into markup — and it never read a direct `.innerHTML =` at all, where the dialog headers and the panels' error
lines interpolated server strings. An allow-list has no such blind spot: a new sink fails here whatever the value is
called, and the fix is always the same — paint it with `ET()`/`T()`, or leave an empty element in the literal and
fill its `textContent`.

Scope note: this is a lint-grade source gate, not the security control. The control is the paint itself; the
browser-level proof is tests/e2e/test_paint_rule.py. The scanner is small on purpose. It understands string
literals, comments and regular-expression literals, not the whole language, and it reads tokens:

  * a string or a regex that does not close raises, and the script is reported as unreadable;
  * "regex or division" is decided from the token before the `/`, not by a parser. A span the mask takes for a
    string or a regex is invisible to every rule, and two constructs are known to be misread: a division written
    right after a `}` that ends an expression, and a regex written right after `if (…)`;
  * a property reached through a COMPUTED name (`x[k] = v`, `x['inner' + 'HTML'] = v`) is not a token it can see.

Those are shapes somebody writes on purpose. What this gate stops is the sink coming back through an ordinary edit,
under whatever name the value has. It reads source, so it binds what is spelled in this file; it is not a claim
about every program.
"""
import os
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX = os.path.join(ROOT, "gui", "templates", "index.html")

# ---- the helpers, pinned by their exact definitions. A change to what a helper does must be made here too. ----
E_DEF = ("const E = (t, c, h, tid) => { const e=document.createElement(t); if(c)e.className=c; "
         "if(h!=null)e.innerHTML=h; if(tid)e.dataset.testid=tid; return e; };")
ET_DEF = "const ET = (t, c, text, tid) => { const e=E(t, c, null, tid); if(text!=null)e.textContent=text; return e; };"
T_DEF = "const T = (text) => document.createTextNode(text == null ? '' : String(text));"
SAFEHREF_DEF = "\n".join([
    "const safeHref = (url) => { if (typeof url !== 'string' || !url.trim()) return null;",
    "  try { const p = new URL(url, document.baseURI).protocol; return (p === 'http:' || p === 'https:') ? url : null; }",
    "  catch (e) { return null; } };",
])
LINK_DEF = "\n".join([
    "const LINK = (url, text, tid) => { const href = safeHref(url); if (!href) return null;",
    "  const a = ET('a', null, text, tid); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; };",
])
HELPERS = {"E": E_DEF, "ET": ET_DEF, "T": T_DEF, "safeHref": SAFEHREF_DEF, "LINK": LINK_DEF}

# Characters a literal-only markup expression may consist of once string BODIES are blanked: the quotes themselves,
# the concatenation operator, grouping and whitespace. Anything else — a letter, a digit, `?`, `:`, `.`, `[`, `$` —
# is a non-literal part.
_LITERAL_CHARS = frozenset("'\"`+() \t\n")

# Tag names that execute, load or restyle by merely existing: never created from script.
_ACTIVE_TAGS = frozenset({"script", "iframe", "object", "embed", "link", "style", "base", "meta", "frame", "frameset"})

# Attributes whose VALUE is code or a navigation target: never set with setAttribute (a link is built by LINK(), a
# handler by assigning a function to `.onclick`). Event-handler attributes are matched by shape (`on` + letters).
# `data-route` is here because the reconfigure dialog reads its request path back from `dataset.route`.
_ACTIVE_ATTRS = frozenset({"href", "src", "srcdoc", "action", "formaction", "style", "xlink:href", "data-route"})

# Names that parse a string as HTML, run a string as code, navigate, send a request that is not `fetch()`, or reach
# a property or an attribute behind the scanner's back. None is used; none may appear as a token in code (prose and
# string bodies are masked first).
_FORBIDDEN_TOKENS = ("insertAdjacentHTML", "outerHTML", "srcdoc", "DOMParser", "createContextualFragment",
                     "setHTMLUnsafe", "parseHTMLUnsafe", "setHTML", "setAttributeNS", "createElementNS",
                     "getOwnPropertyDescriptor", "Reflect", "eval", "Function", "setInterval", "location",
                     "importScripts", "execScript", "write", "writeln",
                     "createAttribute", "setAttributeNode", "setNamedItem", "attributes",
                     "defineProperty", "defineProperties",
                     "Worker", "SharedWorker", "serviceWorker", "createObjectURL", "Blob",
                     "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource")

# Characters that end a line for JavaScript but not for this scanner (so code could hide behind a `//` comment), or
# that reorder how the source is DISPLAYED to a reviewer. Written as escapes: this file must not contain one.
_HIDDEN_CHARS = re.compile("[\u2028\u2029\u202a-\u202e\u2066-\u2069]|\r(?!\n)")

# Properties whose assigned VALUE loads, navigates or restyles. `href` is handled by the LINK pin instead.
_ACTIVE_PROPERTIES = ("src", "action", "formAction", "srcdoc", "data", "codeBase", "ping", "cssText", "style")

# A string literal that IS one of these names is a sink reached by bracket access (`x['innerHTML'] = v`).
_SINK_NAMES = frozenset({"innerHTML", "outerHTML", "insertAdjacentHTML", "srcdoc", "href", "setAttribute",
                         "setAttributeNS", "write", "writeln", "action", "formAction", "src", "cssText"})

# The names that may stand in a request path outside encodeURIComponent(). A name proves nothing by itself
# (`const host = j; fetch('/api/keygen/' + host)` carries a raw value under an innocent name), so each one is pinned
# to where its value comes from, and every other occurrence of the identifier in the script is a breach:
#   deep               one declaration, a ternary of two literals (DEEP_DEF);
#   credFieldsUrl      one `const`, assembled from literals and encodeURIComponent() parts only;
#   dlg.dataset.route  one assignment, from openReconfigureDialog's `route` parameter (ROUTE_FN, ROUTE_SET) — and
#                      the third argument of every openReconfigureDialog() call is literals and
#                      encodeURIComponent() parts only.
_FETCH_ALLOWED = frozenset({"deep", "credFieldsUrl", "dlg.dataset.route"})
DEEP_DEF = "const deep = document.getElementById('deep').checked ? '&deep=true' : '';"
ROUTE_FN = "async function openReconfigureDialog(kind, id, route, titleLabel){"
ROUTE_SET = "dlg.dataset.route = route;"
_PATH_PINS = {"deep": (DEEP_DEF,), "route": (ROUTE_FN, ROUTE_SET), "openReconfigureDialog": (ROUTE_FN,)}

# testid -> what it paints. The allow-list above needs no list: it refuses the sink whatever the value is called.
# This is a second, named pin for the few elements whose bytes nobody has reviewed when they are painted — a
# third-party record, a staged and unpromoted proposal, a freshly minted private key — so that a rewrite cannot move
# one of them back to E() behind a relaxed gate.
MUST_USE_ET = {
    "result-title": "the collection name (Galaxy-authored)",
    "result-meta": "the collection description/note (Galaxy-authored)",
    "search-api-error": "api_search's exception string (can embed a remote response)",
    "suggested-backend-badge": "a rec.* field (locally derived, but the rule is mechanical)",
    "pending-head": "a staged proposal's run id (a git ref name), staged-at and sha",
    "pending-subject": "a staged proposal's commit subject",
    "pending-change": "one change of a staged proposal: its status letter and a path name",
    "pending-unrecognised-row": "a ref beside the proposals that is not one — its name, id and the reason",
    "keygen-private": "a freshly minted private key (text, shown once)",
}

_REGEX_PREV = frozenset("(,=:[!&|?{};+-*%<>~^")       # a '/' right after one of these opens a regex, not a division
_REGEX_WORDS = frozenset({"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
                          "do", "else", "yield", "await"})


def _mask(js):
    """Blank comments and the BODIES of string and regular-expression literals, keeping the delimiters and every
    offset.

    After masking, markup text can no longer be mistaken for code and code can no longer hide inside prose, so the
    rules work on plain characters. A template literal's `${` is left as a visible `$`, so an interpolated template
    can never look like a literal. "Regex or division" is decided from the token before the `/`, which is right for
    every construct this template uses but is not a JavaScript parser. Raises ValueError on a string or a regex that
    does not close — the loud failure for input this scanner cannot follow.
    """
    out, i, n, prev, word = list(js), 0, len(js), "", ""
    while i < n:
        ch = js[i]
        if ch in "'\"`":
            quote, i = ch, i + 1
            while i < n and js[i] != quote:
                if js[i] == "\\":                               # an escape: blank it and the escaped character
                    out[i] = " "
                    if i + 1 < n and js[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if quote == "`" and js.startswith("${", i):     # an interpolation: keep the `$`, blank the rest
                    depth, i = 0, i + 1
                    while i < n:
                        if js[i] == "{":
                            depth += 1
                        elif js[i] == "}":
                            depth -= 1
                        if js[i] != "\n":
                            out[i] = " "
                        i += 1
                        if depth == 0:
                            break
                    continue
                if js[i] != "\n":                               # keep newlines so line numbers survive
                    out[i] = " "
                i += 1
            if i >= n:
                raise ValueError("unterminated %s string" % quote)
            i, prev, word = i + 1, quote, ""
            continue
        if js.startswith("//", i):
            while i < n and js[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if js.startswith("/*", i):
            end = js.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for k in range(i, end):
                if js[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if ch == "/" and (prev == "" or prev in _REGEX_PREV or word in _REGEX_WORDS):
            k, in_class = i + 1, False                          # a regex literal: blank its body up to the closing '/'
            while k < n and js[k] != "\n" and (in_class or js[k] != "/"):
                if js[k] == "\\":                               # an escape: blank it and step over the escaped char
                    out[k] = " "
                    k += 1
                elif js[k] == "[":
                    in_class = True
                elif js[k] == "]":
                    in_class = False
                if k < n and js[k] != "\n":
                    out[k] = " "
                k += 1
            if k >= n or js[k] == "\n":
                raise ValueError("unterminated regex literal")
            i, prev, word = k + 1, "/", ""
            continue
        if ch.isalnum() or ch in "_$":
            word = word + ch if i and (js[i - 1].isalnum() or js[i - 1] in "_$") else ch
        elif not ch.isspace():
            word = ""
        if not ch.isspace():
            prev = ch
        i += 1
    return "".join(out)


def _call_span(masked, open_paren):
    """[(start, end)] offsets of the top-level arguments of the call whose '(' is at `open_paren`; None if the call
    never closes. Nesting counts (/[/{ so a comma inside a sub-call, an array or an arrow body does not split."""
    depth, i, spans, start = 0, open_paren, [], open_paren + 1
    while i < len(masked):
        ch = masked[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                spans.append((start, i))
                return spans
        elif ch == "," and depth == 1:
            spans.append((start, i))
            start = i + 1
        i += 1
    return None


def _calls(masked, name, methods=False):
    """[(offset, [(start, end), ...])] for every call of the bare function `name` (not a longer name). With
    `methods`, a call through any object (`window.fetch(`) counts too."""
    found = []
    for m in re.finditer(r"(?<![A-Za-z0-9_$%s])%s\(" % ("" if methods else ".", re.escape(name)), masked):
        spans = _call_span(masked, m.end() - 1)
        if spans is not None:
            found.append((m.start(), spans))
    return found


def _is_literal(masked_expr):
    """True if a masked expression is a concatenation of string literals and nothing else."""
    return bool(masked_expr.strip()) and set(masked_expr) <= _LITERAL_CHARS and any(q in masked_expr for q in "'\"`")


def _one_literal(js, masked, span):
    """The body of the ONE string literal a span consists of, else None. A concatenation is not one literal:
    `'scr'+'ipt'` must not pass as a tag name."""
    a, b = span
    text = js[a:b].strip()
    if not re.fullmatch(r"\s*(['\"`]) *\1\s*", masked[a:b]):
        return None
    return text[1:-1]


def _markup_sinks(js, masked):
    """[(offset, kind, expression, masked expression)] for every expression assigned to innerHTML: the third
    argument of each `E(` call that has one, and the right-hand side of each `.innerHTML =` / `.innerHTML +=`."""
    sinks = []
    for off, spans in _calls(masked, "E"):
        if len(spans) >= 3:
            a, b = spans[2]
            sinks.append((off, "E() html argument", js[a:b], masked[a:b]))
    for m in re.finditer(r"\.innerHTML\s*\+?=(?!=)", masked):
        i, depth = m.end(), 0
        while i < len(masked):
            ch = masked[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break                                       # the enclosing block closed: end of the statement
                depth -= 1
            elif ch == ";" and depth == 0:
                break
            elif ch == "\n" and depth == 0:                     # no semicolon: a finished literal ends at the line,
                so_far, rest = masked[m.end():i], masked[i:].lstrip()        # unless the next line continues it
                closed = all(so_far.count(q) % 2 == 0 for q in "'\"`")       # (not inside a multi-line template)
                if closed and _is_literal(so_far) and not so_far.rstrip().endswith("+") and not rest.startswith("+"):
                    break
            i += 1
        sinks.append((m.start(), "innerHTML assignment", js[m.end():i], masked[m.end():i]))
    return sinks


def _strip_calls(masked_expr, name):
    """`masked_expr` with every `name( … )` call blanked (balanced), so what is left is what the call did not wrap."""
    out = list(masked_expr)
    for m in re.finditer(r"(?<![A-Za-z0-9_$.])%s\(" % re.escape(name), masked_expr):
        spans = _call_span(masked_expr, m.end() - 1)
        end = (spans[-1][1] + 1) if spans else len(masked_expr)
        for k in range(m.start(), end):
            out[k] = " "
    return "".join(out)


def _unencoded_names(masked_expr):
    """The names that carry a VALUE into a URL expression: outside encodeURIComponent(), outside string literals,
    and not merely the condition of a ternary (`host ? '?host=' + encodeURIComponent(host) : ''` uses `host` raw
    only to choose between two branches). `x ?? y` and `x?.y` are not conditions — there the name is the value."""
    rest = _strip_calls(masked_expr, "encodeURIComponent")
    rest = re.sub(r"[A-Za-z_$][\w$.]*\s*\?(?![?.])", lambda m: " " * len(m.group(0)), rest)
    return set(re.findall(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", rest))


def _tokens(masked, name):
    """Offsets of every occurrence of the identifier `name` in code — as a variable, a parameter or a property."""
    return [m.start() for m in re.finditer(r"(?<![A-Za-z0-9_$])%s(?![A-Za-z0-9_$])" % re.escape(name), masked)]


def _script(html):
    """(script body, offset of its first character in the file) of the template's one inline script, or raise."""
    # An element starts a line here; `<script>` inside a line is prose in a comment (a second element written
    # mid-line still needs its own closing tag, which the count below sees).
    opens = list(re.finditer(r"^[ \t]*<script\b[^>]*>", html, re.M | re.I))
    if len(opens) != 1 or opens[0].group(0) != "<script>" or html.lower().count("</script") != 1:
        raise ValueError("expected exactly one bare inline <script> element")
    start = opens[0].end()
    return html[start:html.lower().index("</script")], start


def violations(html):
    """Every breach of the paint rule in a template's text, as `index.html:<line>  <rule>  ->  <detail>` strings.
    An empty list is the pass. This is the whole gate; the tests below run it on the real template and on planted
    variants of it."""
    def at(offset):                                             # a file line number for an offset in the file
        return html.count("\n", 0, offset) + 1

    found = []
    # -- a character that ends a line for the browser but not for this scan, or reorders what a reviewer sees --
    for m in _HIDDEN_CHARS.finditer(html):
        found.append("index.html:%d  hidden-character  ->  U+%04X (a line break or a display-reordering control "
                     "that neither this scan nor a reader sees)" % (at(m.start()), ord(m.group(0))))
    try:
        js, base = _script(html)
    except ValueError as exc:
        # a second script element, a module, a `src`: code this scan does not read
        return found + ["index.html  script-element  ->  %s (found: %s; %d closing tag(s))" % (
            exc, ", ".join(m.group(0).strip() for m in re.finditer(r"^[ \t]*<script\b[^>]*>", html, re.M | re.I)) or "none",
            html.lower().count("</script"))]

    def bad(offset, rule, detail):                              # offset is within the script
        found.append("index.html:%d  %s  ->  %s" % (at(base + offset), rule, " ".join(str(detail).split())[:150]))

    # -- outside the script: handler attributes are a second place code lives; the template engine is a third --
    outside = html[:base] + " " * len(js) + html[base + len(js):]
    for m in re.finditer(r"""[\s/"']on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", outside, re.I):
        if not re.fullmatch(r'"[A-Za-z]+\(\)"', m.group(1)):
            found.append("index.html:%d  handler-attribute  ->  %s" % (at(m.start()), m.group(0).strip()[:120]))
    for m in re.finditer(r"\{\{|\{%", outside):
        found.append("index.html:%d  template-expression  ->  a template expression in the static markup (hand the "
                     "value to the script as `{{ x | tojson }}` and paint it with ET())" % at(m.start()))

    try:
        masked = _mask(js)
    except ValueError as exc:
        bad(0, "unreadable-script", "%s — the scanner cannot follow this script, so it is refused" % exc)
        return found

    # -- the template engine writes into the script BEFORE the browser reads it --
    if "{%" in js:
        bad(js.index("{%"), "template-statement", "a template statement inside the script")
    if js.count("{{") != masked.count("{{"):
        bad(0, "template-expression", "a template expression inside a string, a regex or a comment of the script")
    for m in re.finditer(r"\{\{.*?\}\}", js, re.S):
        if not re.search(r"\|\s*tojson\s*\}\}$", m.group(0)):
            bad(m.start(), "template-expression", "%s is not `| tojson`" % m.group(0))

    # -- an interpolated template literal is code inside a string: the mask blanks it, so it may not exist --
    for m in re.finditer(r"\$\{", js):
        bad(m.start(), "template-interpolation", "`${` in the script (even in a comment): concatenate, and paint "
                                                 "values with ET()/T()")

    # -- nothing the scanner had to guess at --
    for m in re.finditer(r"\\", masked):
        bad(m.start(), "escape-outside-a-literal", "a backslash in code (an escaped identifier hides a name)")

    # -- the helpers are the pinned ones, each defined once --
    helper_spans = []
    for name, text in HELPERS.items():
        if js.count(text) != 1:
            bad(0, "helper-definition", "`%s` is not defined exactly once as pinned in this gate" % name)
        else:
            helper_spans.append((js.index(text), js.index(text) + len(text)))

    def in_helper(offset):
        return any(a <= offset < b for a, b in helper_spans)

    # -- the sink has two spellings and no others --
    for name in ("E", "ET"):
        for m in re.finditer(r"(?<![A-Za-z0-9_$])%s(?![A-Za-z0-9_$])" % name, masked):
            pre = masked[max(0, m.start() - 6):m.start()]
            is_def = pre.endswith("const ") and masked[m.end():m.end() + 2] == " ="
            if not is_def and not (masked[m.end():m.end() + 1] == "(" and not pre.rstrip().endswith(".")):
                bad(m.start(), "helper-alias", "`%s` used other than as a direct call" % name)
    sinks = _markup_sinks(js, masked)
    assigned = {m.start() + 1 for m in re.finditer(r"\.innerHTML\s*\+?=(?!=)", masked)}
    for m in re.finditer(r"\binnerHTML\b", masked):
        if m.start() not in assigned:
            bad(m.start(), "innerHTML-other-use", "`innerHTML` other than as `.innerHTML =` (read, bracket, alias, `||=`)")

    # -- the rule itself: only literal markup --
    for off, kind, expr, masked_expr in sinks:
        text = expr.strip()
        if _is_literal(masked_expr) or (kind.startswith("E()") and text in ("null", "undefined")):
            continue
        if kind == "innerHTML assignment" and text == "h" and in_helper(off):
            continue                                            # the one assignment that must exist: E's own
        bad(off, "non-literal " + kind, text)

    # -- other ways to parse markup, run a string, navigate --
    for token in _FORBIDDEN_TOKENS:
        for m in re.finditer(r"(?<![A-Za-z0-9_$])%s(?![A-Za-z0-9_$])" % re.escape(token), masked):
            bad(m.start(), "forbidden-api", token)
    for m in re.finditer(r"(?<![A-Za-z0-9_$])open\s*\(|"       # any call named open; a reference to the global one
                         r"(?<![A-Za-z0-9_$.])(?:window|self|top|parent|frames|globalThis|document)\s*\.\s*open"
                         r"(?![A-Za-z0-9_$])", masked):
        bad(m.start(), "forbidden-api", "open — a window, a document or a request opened from script")
    for m in re.finditer(r"(?<![A-Za-z0-9_$])import\s*\(", masked):
        bad(m.start(), "forbidden-api", "import() — code loaded from a string")
    for off in _tokens(masked, "assign"):                       # Object.assign(node, data): DATA names the property
        spans = _call_span(masked, off + 6) if masked[off + 6:off + 7] == "(" else None
        if not spans or not masked[spans[0][0]:spans[0][1]].lstrip().startswith("{"):
            bad(off, "assign-onto-an-object", "assign() whose first argument is not a new `{…}` object (or `assign` "
                                              "not called directly)")
    for m in re.finditer(r"\.\s*(%s)\s*[-+*/%%&|^?]{0,3}=(?!=)" % "|".join(_ACTIVE_PROPERTIES), masked):
        bad(m.start(), "active-property", "assignment to .%s" % m.group(1))
    for off, spans in _calls(masked, "setTimeout", methods=True):
        if "=>" not in masked[spans[0][0]:spans[0][1]]:
            bad(off, "string-timer", "setTimeout's first argument is not an arrow function")
    for m in re.finditer(r"(['\"`])( *)\1", masked):
        if js[m.start() + 1:m.end() - 1] in _SINK_NAMES:
            bad(m.start(), "sink-name-in-a-string", js[m.start():m.end()])

    # -- a link's target is assigned in one place, from safeHref() --
    href_sites = [m.start() for m in re.finditer(r"\bhref\b", masked) if not in_helper(m.start())]
    for off in href_sites:
        bad(off, "href-outside-LINK", "`href` outside the LINK() helper — build the anchor with LINK(url, text)")

    # -- attributes and tag names --
    for m in re.finditer(r"\.setAttribute\(", masked):
        spans = _call_span(masked, m.end() - 1)
        name = _one_literal(js, masked, spans[0]) if spans else None
        if name is None or name.lower() in _ACTIVE_ATTRS or re.fullmatch(r"on[a-z]+", name.lower()):
            bad(m.start(), "active-attribute", "setAttribute(%s, …)" % (js[spans[0][0]:spans[0][1]].strip() if spans else "?"))
    for helper in ("E", "ET"):
        for off, spans in _calls(masked, helper):
            if in_helper(off):
                continue                                        # ET's own E(t, …): the tag is its parameter
            tag = _one_literal(js, masked, spans[0])
            if tag is None or tag.lower() in _ACTIVE_TAGS:
                bad(off, "tag-name", "%s(%s, …): the tag must be one inert string literal" % (
                    helper, js[spans[0][0]:spans[0][1]].strip()))
    for m in re.finditer(r"document\.createElement\(", masked):
        if in_helper(m.start()):
            continue
        spans = _call_span(masked, m.end() - 1)
        tag = _one_literal(js, masked, spans[0]) if spans else None
        if tag is None or tag.lower() in _ACTIVE_TAGS:
            bad(m.start(), "tag-name", "document.createElement(%s)" % (js[spans[0][0]:spans[0][1]].strip() if spans else "?"))

    # -- a request: a direct fetch(), every dynamic segment of its path encoded --
    path_spans = []                                             # where a pinned name may legitimately be READ
    for off in _tokens(masked, "fetch"):
        if masked[off + 5:off + 6] != "(":
            bad(off, "request-alias", "`fetch` used other than as a direct call")
    for off, spans in _calls(masked, "fetch", methods=True):
        path_spans.append(spans[0])
        extra = _unencoded_names(masked[spans[0][0]:spans[0][1]]) - _FETCH_ALLOWED
        if extra:
            bad(off, "unencoded-request-path", "fetch(…) carries %s outside encodeURIComponent()" % ", ".join(sorted(extra)))

    # -- the names a path may carry unencoded get their value in ONE pinned place, and are named nowhere else --
    for name, texts in _PATH_PINS.items():
        pinned = []
        for text in texts:
            if js.count(text) != 1:
                bad(0, "unencoded-request-path", "the pinned source of `%s` is not present exactly once: %s" % (name, text))
            else:
                pinned.append((js.index(text), js.index(text) + len(text)))
        for off in _tokens(masked, name):
            if any(a <= off < b for a, b in pinned):
                continue
            if name == "openReconfigureDialog":                 # a call site: its route argument is checked below
                if masked[off + len(name):off + len(name) + 1] != "(":
                    bad(off, "unencoded-request-path", "`openReconfigureDialog` used other than as a direct call")
            elif not any(a <= off < b for a, b in path_spans):
                bad(off, "unencoded-request-path", "`%s` named outside its one pinned source and outside a fetch() "
                                                   "path — a second binding would carry a raw value" % name)
    for off, spans in _calls(masked, "openReconfigureDialog"):
        if js.startswith(ROUTE_FN, max(0, off - len("async function "))):
            continue                                            # the definition itself
        extra = _unencoded_names(masked[spans[2][0]:spans[2][1]]) if len(spans) >= 3 else {"(no route argument)"}
        if extra:
            bad(off, "unencoded-request-path", "openReconfigureDialog(…) route carries %s outside "
                                               "encodeURIComponent()" % ", ".join(sorted(extra)))
    declared = list(re.finditer(r"(?<![A-Za-z0-9_$.])const\s+credFieldsUrl\s*=(?!=)", masked))
    if len(declared) != 1:
        bad(0, "unencoded-request-path", "`credFieldsUrl` is not declared exactly once with `const`")
    for m in declared:                                          # a path assembled one statement before its fetch
        end = masked.find(";", m.end())
        extra = _unencoded_names(masked[m.end():len(masked) if end < 0 else end])
        if extra:
            bad(m.start(), "unencoded-request-path", "`credFieldsUrl` carries %s outside encodeURIComponent()" %
                ", ".join(sorted(extra)))
    declared_at = {m.start() + m.group(0).index("credFieldsUrl") for m in declared}
    for off in _tokens(masked, "credFieldsUrl"):
        if off not in declared_at and not any(a <= off < b for a, b in path_spans):
            bad(off, "unencoded-request-path", "`credFieldsUrl` named outside its one declaration and outside a "
                                               "fetch() path")
    return found


def _template():
    return open(INDEX, encoding="utf-8").read()


_ANCHOR = "document.getElementById('q').addEventListener('keydown'"


def _planted(line):
    """The real template with one extra statement in its script, just before the script's last statement."""
    html = _template()
    assert html.count(_ANCHOR) == 1, "the script's last statement moved — update _ANCHOR"
    return html.replace(_ANCHOR, line + "\n" + _ANCHOR)


def test_the_helpers_are_what_this_gate_guards():
    """Pin the premise: `E` assigns innerHTML, `ET` assigns textContent, `T` makes a text node, `safeHref` asks the
    browser's own URL parser, and `LINK` is where an href is assigned. Each is defined exactly once, as written at
    the top of this file. If someone changes what a helper does, this gate is measuring something else, and the
    failure should say so rather than pass silently."""
    js, _ = _script(_template())
    for name, text in HELPERS.items():
        assert js.count(text) == 1, "the %s helper is not defined exactly once as pinned" % name


def test_the_template_obeys_the_paint_rule():
    """The gate itself: the real template has no breach of any rule in `violations()` — every `E()` markup argument
    and every `.innerHTML` right-hand side is literal-only, the sink is spelled no other way, links, attributes, tag
    names and request paths are built as pinned. Guards the whole class the Pending panel belonged to — a staged
    proposal's subject of `<img src=x onerror=…>` executing in the operator's session the moment the panel opens —
    under whatever name the next value arrives."""
    found = violations(_template())
    assert not found, "the paint rule is broken — paint the value with ET()/T(), build links with LINK():\n" + "\n".join(
        "  " + line for line in found)


def test_the_scanner_sees_the_real_sinks():
    """A scan that silently finds nothing would also pass. Prove it reads the real template: the helper's own
    assignment, `E()` calls with literal markup and direct literal assignments are all found."""
    js, _ = _script(_template())
    sinks = _markup_sinks(js, _mask(js))
    assert {kind for _, kind, _, _ in sinks} == {"E() html argument", "innerHTML assignment"}
    assert any(expr.strip() == "h" for _, kind, expr, _ in sinks if kind == "innerHTML assignment")
    assert any("<b>" in expr for _, _, expr, _ in sinks), "no literal markup found — the scanner is not reading the script"


@pytest.mark.parametrize("line", [
    # the shapes a value took in this file before the rule was applied as a class
    "x.appendChild(E('div','meta', p.subject, 'pending-subject'));",
    "x.appendChild(E('div','meta', 'changes: '+p.paths.join(', ')));",
    "const h9 = E('h3', null, p.run_id + ' <span class=\"meta\">staged</span>');",
    "x.appendChild(E('div','meta', ok ? 'yes' : 'no'));",
    "x.appendChild(E('div','meta', `staged ${p.sha}`));",
    "x.appendChild(E('div','meta', ['a', 'b'].join('')));",
    "body.innerHTML = '<div class=\"warn\">'+data.error+'</div>';",
    "body.innerHTML += '<b>'+name+'</b>';",
    "body.innerHTML = html;",
    "f.innerHTML = `\n  <input value=\"${key}\">\n`;",
    "try { x() } catch(e){ body.innerHTML = msg }",
    # the sink under another spelling
    "x['innerHTML']=v;",
    "x.innerHTML||=v;",
    "x. innerHTML=v;",
    "[x.innerHTML]=[v];",
    "({innerHTML: x.innerHTML} = {innerHTML: v});",
    "Object.assign(x,{innerHTML:v});",
    "x.inner\\u0048TML=v;",
    "E ('div',null,v);",
    "(E)('div',null,v);",
    "E?.('div',null,v);",
    "E.call(0,'div',null,v);",
    "E.apply(0,['div',null,v]);",
    "const F=E;F('div',null,v);",
    "E(...a);",
    "Object.getOwnPropertyDescriptor(Element.prototype,'innerHTML').set.call(x,v);",
    "x.append(Document.parseHTMLUnsafe(v).body.firstChild);",
    "x['insertAdjacentHTML']('beforeend',v);",
    "p['outerHTML']=v;",
    "x.insertAdjacentHTML('beforeend', v);",
    # hidden behind a regular expression the first scanner could not read
    "s.replace(/\\//g,'');x.innerHTML=v;",
    "s.replace(/'/g,'');x.innerHTML=v//'",
    "s.replace(/[//]/g,''); x.innerHTML=v;",
    # an element or an attribute that executes
    "ET('scr'+'ipt',null,v2);",
    "ET('script', null, v2);",
    "document.createElement('ifr'+'ame');",
    "x.setAttribute('onclick', v2);",
    "x['setAttribute']('onclick',v2);",
    "x.setAttributeNS(null,'onclick',v2);",
    "x.setAttribute('href', u);",
    # a link target that did not pass safeHref()
    "const href=u;a.href=href;",
    "let href=u;a.href=href;",
    "((href)=>{a.href=href;})(u);",
    "const href=safeHref(u)||u;a.href=href;",
    "a['href']=u;",
    "a.href||=u;",
    "Object.assign(a,{href:u});",
    "a.setAttributeNS(null,'href',u);",
    # a property or an API that loads, navigates or runs a string
    "const f9=E('form');f9.action=u;f9.submit();",
    "img9.src = u;",
    "x.style = v;",
    "setTimeout(v2,0);",
    "eval(v2);",
    "new Function(v2)();",
    "window.open(u);",
    "location = u;",
    "import('data:text/javascript,' + v2);",
    "open(u);",
    "self.open(u);",
    "const o8 = self.open; o8(u);",
    "new Worker(URL.createObjectURL(new Blob([v2])));",
    # an attribute or a property whose NAME is not spelled where the scanner can read it
    "const a9 = document.createAttribute('onclick'); a9.value = v2; x.setAttributeNode(a9);",
    "l.attributes[0].value = u;",
    "Object.assign(x, JSON.parse(j));",
    "const m9 = Object.assign; m9(x, o);",
    "Object.defineProperty(x, k, {value: v});",
    # code inside an interpolated template literal, where the mask blanks it
    "const s9 = `${x.innerHTML = v}`;",
    # a request path with a raw segment
    "fetch('/api/keygen/'+role);",
    "fetch('/api/x/'+a+'/'+encodeURIComponent(b));",
    # ... under a name that is allowed to stand in a path, or was (a name is not a value)
    "const host = j; fetch('/api/keygen/' + host, {method: 'POST'});",
    "const g9 = async () => { const deep = '/' + j; return fetch('/api/search?q=x' + deep); };",
    "dlg.dataset.route = '/api/keygen/' + j;",
    "x.setAttribute('data-route', '/api/keygen/' + j);",
    "openReconfigureDialog('k', 'i', '/api/host/' + j, 't');",
    "openReconfigureDialog('k', 'i');",
    "const r9 = openReconfigureDialog; r9('k', 'i', '/api/host/' + j, 't');",
    "((credFieldsUrl) => fetch(credFieldsUrl))('/api/keygen/' + j);",
    "fetch('/api/x/' + (j ?? ''));",
    "fetch('/api/x/' + (c ? j : ''));",
    # ... or sent some other way than a direct fetch()
    "const f9 = fetch; f9('/api/keygen/' + j);",
    "navigator.sendBeacon('/api/keygen/' + j);",
    "const x9 = new XMLHttpRequest();",
    # the template engine writing into the script
    "x.innerHTML = '{{ banner | safe }}';",
    "x.appendChild(E('div', null, '<b>{{ title }}</b>'));",
    "const t9 = {{ title }};",
    "{% if x %} x.remove(); {% endif %}",
    # a line break the browser honours and this scan does not, hiding code behind a comment; a reordering control
    "// note\u2028x.innerHTML = v;",
    "// note\u2029x.innerHTML = v;",
    "// note\rx.innerHTML = v;",
    "const s8 = 'a\u202eb';",
])
def test_a_planted_breach_is_reported(line):
    """A gate that cannot fail is not a gate. Each line is planted into the REAL script and must be reported: the
    shapes a value took in this file, the sink under every other spelling a reviewer found (a literal bracket key,
    `||=`, an alias, `.call`, another parsing API), a sink hidden behind a regular expression or inside an
    interpolated template literal, an executing element or attribute, an attribute or property whose name comes
    from data, a link target that skipped `safeHref()`, a navigating property, a raw request-path segment — also
    under a name that is allowed in a path, and through a request that is not a direct `fetch()` — a template
    expression inside the script, and a hidden line break. Most of these were run in a browser by the reviews of
    this gate, where they painted, executed or retargeted a request while an earlier version of it passed; the rest
    are the same sinks one token away."""
    assert violations(_planted(line)), "the gate missed: %s" % line


@pytest.mark.parametrize("change", [
    ("a second script element", lambda h: h.replace("</script>", "</script>\n<script>document.body.innerHTML = name;</script>", 1)),
    ("a module script", lambda h: h.replace("</script>", "</script>\n<script type=\"module\">\ndocument.body.innerHTML = name;\n</script>", 1)),
    ("an external script", lambda h: h.replace("</script>", "</script>\n<script src=\"/static/x.js\"></script>", 1)),
    ("an attribute on the script element", lambda h: h.replace("\n<script>\n", "\n<script defer>\n", 1)),
    ("a handler attribute with code in it", lambda h: h.replace('onclick="search()"', 'onclick="results.innerHTML = q.value"', 1)),
    ("a handler attribute in another case", lambda h: h.replace('onclick="search()"', 'onClick="results.innerHTML = q.value"', 1)),
    ("a handler attribute after a slash", lambda h: h.replace(' onclick="search()"', '/onclick="results.innerHTML = q.value"', 1)),
    ("a template expression in the static markup", lambda h: h.replace("<header>", "<header>{{ banner | safe }}", 1)),
    ("a template expression in a static attribute", lambda h: h.replace("<header>", '<header><a href="{{ url }}">x</a>', 1)),
    ("a template statement in the static markup", lambda h: h.replace("<header>", "<header>{% include banner %}", 1)),
    ("a sink on the helper's own line", lambda h: h.replace(E_DEF, E_DEF + " const paint = (el, h) => { el.innerHTML=h; };", 1)),
    ("a redefined helper", lambda h: h.replace("e.textContent=text", "e.innerHTML=text", 1)),
    ("a raw segment in the pinned request path", lambda h: h.replace(
        "'&collection=' + encodeURIComponent(collection)", "'&collection=' + collection", 1)),
    ("a second value for a pinned path name", lambda h: h.replace(DEEP_DEF, DEEP_DEF.replace("'&deep=true'", "q"), 1)),
    ("a route set from something other than the parameter", lambda h: h.replace(ROUTE_SET, "dlg.dataset.route = id;", 1)),
], ids=lambda c: c[0] if isinstance(c, tuple) else None)
def test_a_planted_structural_bypass_is_reported(change):
    """The script scan reads one inline script. Everything it does not read must not be able to carry a sink: a
    second, module or external script element, code in a static handler attribute (in any letter case, after any
    separator), a template expression or statement in the static markup, a sink smuggled onto a helper's exempt
    line, a helper redefined to assign innerHTML. And the three places a request path gets an unencoded name from
    are pinned: a raw segment in the assembled cred-fields path, another value for `deep`, a route that is not the
    dialog's parameter. Each is applied to the real template and must be reported."""
    name, edit = change
    html = _template()
    changed = edit(html)
    assert changed != html, "the planted change did not apply: %s" % name
    assert violations(changed), "the gate missed: %s" % name


@pytest.mark.parametrize("line", [
    "x.appendChild(E('div','spin','Searching…','search-loading'));",
    "x.appendChild(E('div', 'meta', 'Reorder <b>sections</b>; '+\n  '<b>Save</b> stages a proposal.'));",
    "const row9 = E('div', 'row', null, 'unit-row');",
    "const c9 = E('div','card');",
    "const d9 = E(`div`);",
    "x.appendChild(ET('div','meta', p.subject, 'pending-subject'));",
    "body.innerHTML = '';",
    "function f8(x) { x.innerHTML = ''\n  return x; }",
    "dlg.innerHTML = '<div class=\"caphd\"><span><b></b></span>'+\n  '<button class=\"capx\">×</button></div>';",
    "f.innerHTML = `\n  <div><label>key</label><input name=\"key\"></div>`;",
    "const l9 = LINK(u, 'label ↗', 'cap-link');",
    "w.setAttribute('onboard-step', '1');",
    "fetch('/api/keygen/'+encodeURIComponent(role));",
    "setTimeout(() => done(), 5);",
    "const s1 = s.replace(/\"/g, '');",
    "const s2 = s.replace(/[\\/']/g, '-') + 1 / 2;",
    "const d1 = a / b / 2; // it's a division",
    "const r1 = (w + h) / 2 / w;",
    "function f7(s) { return /^a/.test(s) ? 'x' : typeof s; }",
    "const o9 = ET('option', null, 'x');",
    "fetch('/api/x' + (h9 ? '?host=' + encodeURIComponent(h9) : ''));",
    "fetch('/api/x/' + encodeURIComponent(a) + '/y?z=' + encodeURIComponent(b), {method: 'POST', body: JSON.stringify(c)});",
    "openReconfigureDialog('settings-knob', k9, '/api/settings/' + encodeURIComponent(k9), 'settings · ' + k9);",
    "const b9 = Object.assign({}, a, {apply: true});",
    "const g8 = () => { const open = x.classList.contains('open'); return open ? 'Collapse' : dlg9.open; };",
    "x.setAttribute('data-step', '1');",
])
def test_legitimate_code_is_not_reported(line):
    """The other half of the proof: authored markup (single literals, concatenations across lines, an
    interpolation-free template, a clear with no semicolon), the text helper, the link helper, an encoded request
    path (with a name used only as a ternary's condition, and with a request body that carries anything), a
    reconfigure dialog opened on an encoded route, `Object.assign` onto a new object, a local called `open`, an
    arrow-function timer and ordinary regular expressions and divisions all pass when planted into the real script
    — so the gate does not push anyone toward weakening it."""
    assert not violations(_planted(line)), "false positive on: %s\n%s" % (line, "\n".join(violations(_planted(line))))


def test_the_scanner_ignores_prose_and_refuses_what_it_cannot_follow():
    """`E(` or `.innerHTML =` inside a comment or a string literal is not a sink — without this the helper's own
    doc comment and the paint-rule comment would be noise. And input the masker cannot follow is refused, not
    skipped: a string or a regular expression that does not close raises, and `violations()` reports the script as
    unreadable."""
    for prose in ("// E(tag, class, html, testid) — the doc comment\n",
                  "// the same holds for every direct `.innerHTML =` assignment\n",
                  "const u = 'E(rec.description, x, y)'; const v = 'http://example.invalid/';\n",
                  "/* body.innerHTML = data.error; */\n"):
        assert not _markup_sinks(prose, _mask(prose)), prose
    code = "E('a','b', 'literal', 'tid');\n"
    assert _markup_sinks(code, _mask(code)), "real call sites must still be seen"
    with pytest.raises(ValueError):
        _mask("const s = 'never closed;\n")
    with pytest.raises(ValueError):
        _mask("const r = x.replace(/never closed\n, '');\n")
    unreadable = "<html><body>\n<script>\nconst s = 'never closed;\n</script>\n</body></html>\n"
    assert any("unreadable-script" in v for v in violations(unreadable))


def test_unreviewed_values_are_painted_with_the_text_helper():
    """Belt to the allow-list's braces: the elements that carry bytes nobody has reviewed when they are painted — a
    third-party record, a staged proposal, a minted private key — are named and pinned to ET( by testid, so a
    rewrite cannot quietly move one back to E() behind a relaxed gate."""
    html = _template()
    for tid, what in MUST_USE_ET.items():
        assert re.search(r"ET\([^\n]*'%s'" % re.escape(tid), html), \
            "%s (%s) must be painted with ET(), not E()" % (tid, what)


def test_security_headers_are_declared():
    """The defence-in-depth layer ships and stays: object-src/base-uri/frame-ancestors plus nosniff. NOT
    script-src — index.html carries a large inline <script> and inline handler attributes on its header buttons, so
    asserting it here would pin a header that breaks the page; a per-response nonce with those handlers rebound is
    the named follow-up (SECURITY.md C19)."""
    app_src = open(os.path.join(ROOT, "gui", "app.py"), encoding="utf-8").read()
    assert "@app.after_request" in app_src
    m = re.search(r'"Content-Security-Policy",\s*\n?\s*"([^"]*)"', app_src)
    assert m, "no Content-Security-Policy header value found in gui/app.py"
    policy = m.group(1)          # assert on the POLICY, not the file — prose about script-src is not a directive
    for directive in ("object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'"):
        assert directive in policy, "missing CSP directive: %s (policy is %r)" % (directive, policy)
    assert "X-Content-Type-Options" in app_src
    assert "script-src" not in policy, "script-src cannot ship while index.html has an inline <script>"
