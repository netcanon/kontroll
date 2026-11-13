"""The deploy's walk (T32): the plays, tasks, handlers, vars files and templates `ansible-playbook
deploy-stack.yml` loads from the places listed here, for the two pins that must see all of it: the alert URL's no_log
pin (test_alerting_metrics.py) and the platform-core drift pin (test_gen_secret_env.py). Each read a subset of its own
before: a role's files under `.yml` only, `include_vars` as one file, no imported playbook, and the drift pin
deploy-stack.yml alone; each form Ansible loads beyond those let a leak or a third platform-core domain through (fold
4's reads 90, S2, N6, N7 and 100, N3, N4). It read neither the inventory nor any template, so a template the mandated
`--check --diff` prints carried the URL or a third domain past both pins (fold 5's reads 110, S2 and 111, S2).

Where Ansible looks for a file in more than one place, the walk reads the file in each place it lists (`Walk`), taken
from Ansible's search orders, not only the one a run takes. Those orders differ by plugin and by role (a template
lookup's include searches the looked-up template's own directory first, the template action the top template's last;
a role's include_tasks its `tasks/` first; a meta dependency its parents' directories too), and each order the walk
modelled was one a reader found Ansible leaving, the URL then sitting in the file the walk did not read (folds 5 to 8;
fold 6's reads 120, S1, S2 and 121, S2; fold 7's reads 130, S2 and 131, S6: the working directory, an include's
parents' directories, a role handler's `handlers/`, a role's `tasks/` below it, a nested playbook's own
`playbook_dir`; fold 8's read 140, S2: the parent of a role's nested `tasks/`). A pin that reads a file Ansible skips
fails loudly or reads too much; one that skips the file Ansible loads passes in silence. So the walk takes the
places' union, and some it reads Ansible does not (a nested playbook's role in the top
playbook's `roles/`, its vars file in the top playbook's directory: forms Ansible fails on, fold 7's read 130, N2). A
name's spelling (`main.yml` before a bare `main`) is still taken as Ansible's role loader takes it. The places are the
ones listed (`Walk`), not every place Ansible could search: the next a reader finds is a row and a place, as each
fold's were.

It reads Jinja with Jinja's own parser, not a pattern: each string as a template, and as one expression where the whole
of it is one (a condition, a debug `var:`), the expression's own top node too (`_all`: a debug `var:` that was a
template lookup alone was read as none, fold 10's own find), then each literal and raw block's text in it again, and
each string Jinja computes from literals alone, whole (`_computed`: `'{{ lookup(' ~ …` by its operators and its own
filters, not each half), as 2.18 renders a fact's value again where it is read (a pattern took a quoted `'{#'` for a
comment and the include behind it for text that never runs: fold 8's read 140, S1; two joined literals passed: fold 9's
read 151, S1). Text a play builds at run time from a variable, an Ansible filter or a method call it does not compute,
and on 2.18 that text runs too (SECURITY.md R-ALERT-1). A playbook's string is lexed as Ansible lexes it, each backslash
in a `{{ }}` literal kept (`_escaped`), and a template file as Jinja lexes it: Jinja alone decodes a backslash escape
Ansible keeps, so the walk read a literal Ansible never renders and opened a decoy where Ansible opens the name as
written (fold 9's read 150, S-B). A file the walk cannot name, or a form it does not model, fails it (`Unfollowed`): a
path templated by anything but `{{ playbook_dir }}`; an include, import, extends or from that is not one literal in a
template, or any at all in a task's string or a var's (fold 7's read 131, S3), wherever the parser finds it; a lookup
whose plugin is not one literal, or whose name is read other than to call it (in an expression that calls it through a
subscript or a conditional, `[lookup][0](…)`, too: 151, S1; 150, S-C), the names `lookup`, `query` and `q` being
reserved (a var of one of them read in a template fails it: 150, N-D); a template lookup's term that is not one literal,
or one handed delimiters or unpacked arguments; a `#jinja2:` header that sets anything but whitespace (131, S3, N2); a
string that does not parse as Jinja, or a literal in one that does not and names a lookup or a load (150, S-B); a
condition that holds a delimiter or a `#jinja2:` header, quoted or not, as written or as Jinja decodes a literal of it
(`when: "{{ … }}"`, which 2.18 renders and runs again and 2.19 refuses: 150, S-A; `'{#' != '{{ lookup(…) }}'`, whose
literal 2.19 renders: fold 11's read 170, S-1), under `loop_control.break_when` and an include's `apply:` too (171, S4;
170, S-2), or one that does not parse as one expression, whose head 2.18 runs (fold 12's reads 181, S3 and 180, S-A);
an `action:` or `local_action:` whose module is templated (181, S2); an import_playbook entry with a key but its name,
its `tags:` and its `when:`,
whose `vars:` hold for the plays it imports (131, S4); an inventory file that is not YAML or JSON (an INI one: 131, S5),
and a vars or inventory file that is not a mapping (140, S1); and a role's `meta/argument_specs`, which the walk does
not read (140, N3).
tests/unit/test_deploy_walk.py holds the walk to each form on a tree of its own.

It does not read a `copy` src or a `lookup('file', …)`, which Ansible hands on as written, never templated, nor the
operator's own instance/: the inventory is read as shipped (instance.example/inventory/, which kontroll-init copies
to the instance/inventory ansible.cfg names), each file at any depth Ansible's directory loader does not skip, and
the group_vars and host_vars at its top."""
import collections
import functools
import glob
import json
import os
import re

import jinja2
import yaml
from jinja2 import nodes
from jinja2.parser import Parser

# The names Ansible tries for a role's file, in order (its role loader, _load_role_yaml): for the main file `.yml`,
# `.yaml`, `.json`, then the bare name; for a file a `<x>_from` names, the bare name first. It loads the first found.
MAIN_EXTENSIONS = (".yml", ".yaml", ".json", "")
FROM_EXTENSIONS = ("", ".yml", ".yaml", ".json")
_PREFIXES = ("", "ansible.builtin.", "ansible.legacy.")
# ansible.cfg's `roles_path = roles` and `inventory = ../instance/inventory`, relative to ansible/ (test_deploy_walk.py
# holds both), the inventory as the tool ships it.
ROLES_PATH = ("ansible", "roles")
INVENTORY = ("instance.example", "inventory")
# The directory every deploy runs `ansible-playbook playbooks/deploy-stack.yml` from (test_deploy_walk.py holds the
# commands), which a relative name in a task include's search resolves against (DataLoader.path_dwim_relative).
CWD = ("ansible",)
# What Ansible skips in an inventory directory, at each level, on both cores (InventoryManager's IGNORED): a hidden
# name, a group_vars, host_vars or vars_plugins directory, and INVENTORY_IGNORE_EXTS as 2.19 has it (2.18 skips
# `.ini` too).
_INVENTORY_SKIP = re.compile(r"^\.|^(?:host_vars|group_vars|vars_plugins)$"
                             r"|(?:\.pyc|\.pyo|\.swp|\.bak|~|\.rpm|\.md|\.txt|\.rst|\.orig|\.cfg|\.retry)$")
# The one template expression the walk reads in a path, `{{ playbook_dir }}`; any other fails it.
_PLAYBOOK_DIR = re.compile(r"\{\{\s*playbook_dir\s*\}\}")
# Jinja's syntax as both cores take it: its default delimiters and no extension (ansible.cfg sets no
# `jinja2_extensions`: test_deploy_walk.py holds it). The walk reads Jinja with Jinja's own parser, so a quoted
# delimiter (`'{#'`, `'}}'`, `'{% raw %}'`) is a string as Jinja reads it, not the comment or the close a pattern
# took it for (fold 8's read 140, S1); a playbook's string is lexed first as Ansible lexes it (`_escaped`).
_JINJA = jinja2.Environment()
# What Jinja computes from literals alone under that environment (`as_const`: its operators and its own filters,
# not Ansible's, which it does not hold).
_EVAL = nodes.EvalContext(_JINJA)
# A lookup by any of its names, and a template lookup's plugin under any prefix (`ansible.builtin.`, …). A
# `with_template` loop under any prefix.
_LOOKUPS = ("lookup", "query", "q")
_TEMPLATE_PLUGIN = re.compile(r"(?:[\w.]*\.)?template")
_WITH_TEMPLATE = re.compile(r"with_(?:[\w.]*\.)?template")
# Jinja's own delimiters, set otherwise: a template task's or lookup's argument.
_DELIMITERS = re.compile(r"\b(?:variable|block|comment)_(?:start|end)_string\b")
# The `#jinja2:` header keys that leave Jinja's delimiters as they are; any other (a delimiter, a line statement or
# line comment prefix) can make text the walk reads as plain run as Jinja (both cores read a header on any templated
# string: 2.19's TemplateOverrides, 2.18's _create_overlay).
_HEADER = "#jinja2:"
_WHITESPACE = frozenset({"trim_blocks", "lstrip_blocks", "newline_sequence", "keep_trailing_newline"})
# A template's include, import, from-import and extends, each of which loads a file.
_LOADS = (nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends)
# A word that runs a lookup or loads a file: a literal that does not parse but holds one fails the walk, where one
# that holds neither runs nothing a pin reads (fold 9's read 150, S-B).
_RUNS = re.compile(r"\b(?:lookup|query|q|include|import|from|extends)\b")
# The keys a task, a role's entry and an import_playbook entry run as a condition, without delimiters (an assert's
# `that:`, a loop's `loop_control.break_when` and an include's `apply:` `when:` too), and what a condition may not
# hold: a delimiter's start, or a `#jinja2:` header, which 2.19 honours past the whitespace it strips from a condition
# (fold 11's read 170, S-3).
_CONDITIONS = ("when", "changed_when", "failed_when", "until")
_DELIMITED = re.compile(r"\{[{%#]|#jinja2:")


class Unfollowed(FileNotFoundError):
    """A file the walk cannot name, or a form it does not read: a path templated by anything but `{{ playbook_dir
    }}`, an include, a lookup's plugin or a template lookup's term that is not one literal, a `#jinja2:` header that
    sets anything but whitespace, a string that does not parse as Jinja (or a literal in one that names a lookup or a
    load), a condition that holds a delimiter or a header or does not parse as one expression (`_conditions`), a
    module named by a template (`_templated_module`), a task's or a var's string that includes, imports or extends a
    file of its own, an import_playbook entry with a key but its name, tags and when, an
    inventory file that is not YAML or JSON or a vars file that is not a mapping, a role's argument_specs."""


def module(task, name):
    """The task's argument to module `name` under any spelling Ansible takes (bare, `ansible.builtin.`,
    `ansible.legacy.`), or None."""
    for prefix in _PREFIXES:
        if prefix + name in task:
            return task[prefix + name]
    return None


def kv(s):
    """A k=v string as Ansible's parse_kv reads it: words split on whitespace outside quotes and outside
    `{{ }}`, `{% %}` and `{# #}`; each `key=value` word an argument, one pair of outer quotes taken off its value; the
    other words joined as `_raw_params`."""
    words, word, quote, depth, i = [], "", None, 0, 0
    while i < len(s):
        c, two = s[i], s[i:i + 2]
        if quote:
            if c == "\\" and i + 1 < len(s):
                word, i = word + two, i + 2
                continue
            if c == quote:
                quote = None
        elif two in ("{{", "{%", "{#"):
            depth, word, i = depth + 1, word + two, i + 2
            continue
        elif two in ("}}", "%}", "#}") and depth:
            depth, word, i = depth - 1, word + two, i + 2
            continue
        elif c in "'\"":
            quote = c
        elif c.isspace() and not depth:
            words, word, i = words + [word] if word else words, "", i + 1
            continue
        word, i = word + c, i + 1
    words += [word] if word else []
    out, raw = {}, []
    for w in words:
        m = re.match(r"(\w+)=(.*)\Z", w, re.S)
        if m is None:
            raw.append(w)
            continue
        v = m.group(2)
        out[m.group(1)] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v
    if raw:
        out["_raw_params"] = " ".join(raw)
    return out


def args(task, name):
    """The arguments the task hands module `name`, as a mapping (a k=v string read by `kv`), under each form Ansible
    takes: the module's key, bare, `ansible.builtin.` or `ansible.legacy.`, or `action:` / `local_action:` naming it
    (a string's first word, or a mapping's `module:`'s, whose other words are k=v arguments over the mapping's other
    keys: `module_words`), with the task's `args:` mapping under it; None when the task runs no such module. A module
    word that holds a delimiter, which names no module until Ansible renders it, the walk refuses first
    (`_templated_module`)."""
    names = {p + name for p in _PREFIXES}
    keys = [k for k in task if k in names]
    if keys:
        value = task[keys[0]]
    else:
        for key in ("action", "local_action"):
            a = task.get(key)
            if isinstance(a, str) and a.split() and a.split()[0] in names:
                value = a.strip()[len(a.split()[0]):]
                break
            word, rest = module_words(a.get("module")) if isinstance(a, dict) else (None, "")
            if word in names:
                value = dict({k: v for k, v in a.items() if k != "module"}, **kv(rest))
                break
        else:
            return None
    value = kv(value) if isinstance(value, str) else {} if value is None else value
    if isinstance(value, dict) and isinstance(task.get("args"), dict):
        value = dict(task["args"], **value)
    return value


def module_words(module):
    """A mapping `action:`'s `module:` split as Ansible splits it (`ModuleArgsParser._split_module_string`): its first
    word, the module, and the rest, which Ansible reads as k=v arguments over the mapping's other keys; (None, "") for
    none. Why: a `module: "ansible.builtin.template src=a.j2 dest=/x"` ran the template module, which the walk, taking
    the whole string for the module's name, read no template of (fold 13's reads 191, S-b and 190, N-1)."""
    words = str(module).split(None, 1) if module is not None else []
    return (words[0], words[1] if len(words) > 1 else "") if words else (None, "")


def load(path):
    """A YAML file parsed, or a `.json` one as JSON (which Ansible reads and YAML refuses when it is tab-indented)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh) if path.endswith(".json") else yaml.safe_load(fh)


def strings(value):
    """Every key and string value in a parsed document, a YAML comment being none and a block scalar's `#` line
    part of its string, as Ansible reads them."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)
    elif isinstance(value, str):
        yield value


def _header(s):
    """`s` past its `#jinja2:` header, which may set whitespace alone (`_WHITESPACE`); a header with any other key, or
    one 2.19 refuses, or whose line 2.18 renders whole (no newline after it), or a pair without a `:`, fails the walk
    (fold 9's read 150, N-B)."""
    if not s.startswith(_HEADER):
        return s
    eol = s.find("\n")
    pairs = s[len(_HEADER):eol].split(",") if eol >= 0 else [""]
    if any(":" not in p or p.split(":", 1)[0].strip() not in _WHITESPACE for p in pairs):
        raise Unfollowed("a `#jinja2:` header that can set Jinja's delimiters: %s" % s.split("\n", 1)[0])
    return s[eol + 1:]


def _expression(s):
    """`s` parsed as one Jinja expression, a tuple too, as Ansible runs a condition: 2.19 compiles it as one expression
    (and refuses a tuple), 2.18 splices it into a `{% set %}`, which builds each of a tuple's items (`when: "true,
    lookup(…)"`; fold 9's read 150, S-A, N-B)."""
    parser = Parser(_JINJA, s, state="variable")
    tree = parser.parse_tuple()
    if not parser.stream.eos:
        raise jinja2.TemplateSyntaxError("not one expression", parser.stream.current.lineno)
    return tree



def _escaped(s):
    """`s` as Ansible's templar lexes a playbook's string: each backslash in a literal inside `{{ }}` doubled, so
    that Jinja keeps it as written. 2.18's `_escape_backslashes` doubles them and lexes the doubled text again; 2.19
    doubles each string token in place, so it takes a literal with an escaped quote (`{{ 'it\\'s' }}`) that 2.18 and
    the walk refuse as not parsing (fold 10's read 161, N2). Jinja alone decodes a backslash escape there (a brace,
    or a dot, written as its code), so the walk read a literal Ansible never renders and opened a decoy where Ansible
    opens the name as written (fold 9's read 150, S-B). Inside `{% %}` both cores decode, as Jinja does, and a
    template file is lexed as Jinja lexes it. A debug `var:` is a playbook's string on 2.18 alone, which wraps it in
    `{{ }}`: 2.19 compiles it as an expression and decodes it, and `_trees` reads both (fold 10's read 160, N-1)."""
    if "\\" not in s or "{{" not in s:
        return s
    out, inside = [], False
    try:
        for _, kind, text in _JINJA.lex(s):
            inside = (inside or kind == "variable_begin") and kind != "variable_end"
            out.append(text.replace("\\", "\\\\") if inside and kind == "string" else text)
    except jinja2.TemplateSyntaxError:
        return s
    return "".join(out)


def _all(tree, types):
    """Each node of `tree` of `types`, `tree` itself first where it is one: Jinja's `find_all` leaves out the node it
    starts from, which, for a string parsed as one expression, is the whole of it (a debug `var:` of `lookup('template',
    'x.j2')` alone was read as no lookup at all, and its template as none: fold 10's own find)."""
    return ([tree] if isinstance(tree, types) else []) + list(tree.find_all(types))



def _strings_of(value):
    """Each string in a value Jinja computes: itself, or each of a list's, a tuple's or a mapping's, as Ansible renders
    each again."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [x for kv in value.items() for v in kv for x in _strings_of(v)]
    if isinstance(value, (list, tuple, set)):
        return [x for v in value for x in _strings_of(v)]
    return []


def _computed(node):
    """Each string Jinja computes from literals alone in `node` (`as_const`: its operators and its own filters), the
    outermost only: a node it computes is read whole, not its literals one by one (`'{{ lookup(' ~ "'template', …"`
    is one lookup, where each half alone does not parse and names one); a node it cannot compute (a variable, an
    Ansible filter, a call) is read through its children, each literal of it alone."""
    if isinstance(node, nodes.Expr):
        try:
            value = node.as_const(_EVAL)
        except Exception:   # nodes.Impossible, or a filter that fails on its literal: nothing Jinja computes
            pass
        else:
            yield from _strings_of(value)
            return
    for child in node.iter_child_nodes():
        yield from _computed(child)


@functools.lru_cache(maxsize=None)
def _trees(s, nested=False, file=False):
    """The Jinja `s` runs, parsed: `s` as a template (past its `_header`), and, at the top, as one expression where
    the whole of it is one (a `when:`, `that:` or `until:` condition, a debug `var:`, which Ansible runs without
    delimiters), its literals kept and decoded both (2.18 wraps a debug `var:` in `{{ }}` and lexes it as a
    playbook's string, keeping a backslash; 2.19 compiles it as an expression and decodes it: fold 10's read 160,
    N-1; a condition is lexed as a `{% %}` block's); then each string either holds, as a template: a literal's or a
    raw block's text as written, and each string Jinja computes from literals alone, whole (`_computed`: `'{{
    lookup(' ~ "'template', …"`, `'{' * 2`, a `join` of literals, not each half). Both cores render such text again:
    2.18 a set_fact's value where the fact is read, both a lookup's terms when it runs, 2.19 a condition's literal at
    once (fold 9's own probe; two joined literals passed: fold 9's read 151, S1; 150, N-B). Text built from a
    variable, an Ansible filter or a method call is not computed (SECURITY.md R-ALERT-1). A playbook's string, and
    each string taken from one, is lexed as Ansible lexes it (`_escaped`); a template `file`'s text as Jinja lexes
    it, and as a template alone, which is all Ansible renders it as (fold 11's read 170, N-2). A string that does not
    parse as a template fails the walk: a condition that holds a delimiter fails it before (`_conditions`), and 2.19
    fails a task on any other such string (fold 11 read one whose first delimiter does not close, `'{{' in banner`, as
    its expression alone, where neither core runs it to its end and 2.19 renders its literal: fold 11's reads 170, S-1,
    S-4 and 171, S1). A nested one that does not parse is text that never runs, unless it names a lookup or a load
    (`_RUNS`: there the walk's text and Ansible's may differ; 150, S-B)."""
    try:
        trees = [_JINJA.parse(_header(s if file else _escaped(s)))]
    except jinja2.TemplateSyntaxError as e:
        if nested and not _RUNS.search(s):
            return ()
        raise Unfollowed("a string that does not parse as Jinja (%s): %.200s" % (e.message, s)) from None
    for x in () if nested or file else dict.fromkeys([s, _escaped("{{ %s }}" % s)[3:-3]]):
        try:
            trees.append(_expression(x))
        except jinja2.TemplateSyntaxError:
            pass
    for tree in list(trees):
        for text in _computed(tree):
            if text != s:
                trees += _trees(text, True)
    return tuple(trees)


def template_terms(s, file=False):
    """Each template a template lookup in the Jinja of `s` (`_trees`) renders: its terms up to its keyword arguments,
    each one literal. A lookup whose plugin is not one literal, a template lookup whose term is not one or that is
    handed delimiters of its own or unpacked arguments (`*`, `**`), and a lookup's name read other than to call it, in a
    template (`{% set l = lookup %}`) or in an expression that calls anything (`[lookup][0]('template', …)`, which
    Ansible runs: fold 9's read 151, S1), fail the walk. An expression that calls nothing (`q`, `-q`: plain text Jinja
    reads as a name) runs no lookup, and passes. So the names `lookup`, `query` and `q` are reserved: a var, a loop
    variable or a set of one of them, read in a template, fails the walk, though Ansible reads it as the var (fold 9's
    read 150, N-D; no shipped file binds one). `file`: `s` is a template file's text (`_trees`)."""
    names = []
    for tree in _trees(s, file=file):
        calls = [c for c in _all(tree, nodes.Call) if isinstance(c.node, nodes.Name) and c.node.name in _LOOKUPS]
        called = {id(c.node) for c in calls}
        if (isinstance(tree, nodes.Template) or _all(tree, nodes.Call)) and any(
                n.name in _LOOKUPS and id(n) not in called for n in _all(tree, nodes.Name)):
            raise Unfollowed("a lookup's name read other than to call it: %.200s" % s)
        for c in calls:
            plugin = c.args[0] if c.args else None
            if not (isinstance(plugin, nodes.Const) and isinstance(plugin.value, str)):
                raise Unfollowed("a lookup whose plugin is not one literal: %.200s" % s)
            if not _TEMPLATE_PLUGIN.fullmatch(plugin.value):
                continue
            if c.dyn_args or c.dyn_kwargs or any(_DELIMITERS.fullmatch(k.key) for k in c.kwargs) or not all(
                    isinstance(t, nodes.Const) and isinstance(t.value, str) for t in c.args[1:]):
                raise Unfollowed("a template lookup the walk cannot follow: %.200s" % s)
            names += [t.value for t in c.args[1:]]
    return names


def _includes(text, path, file=False):
    """Each file the Jinja of `text` (`_trees`) includes, imports, imports from or extends: one literal name each; any
    other (a var, a list, a concatenation) fails the walk."""
    names = []
    for tree in _trees(text, file=file):
        for n in tree.find_all(_LOADS):
            if not (isinstance(n.template, nodes.Const) and isinstance(n.template.value, str)):
                raise Unfollowed("an include the walk cannot follow, in %s, line %d" % (path, n.lineno))
            names.append(n.template.value)
    return names


def _inline(value, where):
    """Refuse a string of `value` whose Jinja the walk does not read: one that includes, imports, imports from or
    extends a file, which Ansible's templar loads from the playbook's directory, in any of its Jinja (`_trees`: behind
    a quoted delimiter too, or in a literal rendered again); one with a `#jinja2:` header that can set delimiters
    (`_header`); one that does not parse."""
    for s in strings(value):
        if any(True for tree in _trees(s) for _ in tree.find_all(_LOADS)):
            raise Unfollowed("a string with Jinja of its own the walk does not read, in %s: %.200s" % (where, s))



def _delimited(s):
    """Whether condition `s` holds `_DELIMITED` as written, or in a string literal as Jinja decodes it (a brace
    written as its code), `s` parsed as one expression: both cores decode a condition's literals, 2.18 in the
    `{% set %}` it splices it into, 2.19 in the expression it compiles. (A literal lexed as a playbook's string,
    `_escaped`, has each backslash doubled, which decodes to no character `s` does not hold as written.) A condition
    that does not parse as one expression is refused too: 2.19 fails it, and 2.18 splices it into a `{% set __cres =
    … %}` (conditional.py:125 in 2.18.19; the `{% if %}` of its line 122 runs only under the internal
    `_DISABLE_BACKPORTED_INSPECTIONS` switch) and runs its head where it closes that tag itself (`lookup(…) %}x`,
    under each of the condition places, ran its lookup on 2.18.19: fold 12's reads 181, S3 and 180, S-A; the tag as
    fold 13's read 190, S-1 found it)."""
    if _DELIMITED.search(s):
        return True
    try:
        tree = _expression(s)
    except jinja2.TemplateSyntaxError:
        return True
    return any(isinstance(c.value, str) and _DELIMITED.search(c.value) for c in _all(tree, nodes.Const))


def _conditions(value, where):
    """Refuse a condition of `value` (a task, a role's entry or an import_playbook entry: `_CONDITIONS`, an assert's
    `that:`, a loop's `loop_control.break_when`, an include's `apply:` `when:`; a string, or a list's) that holds a
    delimiter or a `#jinja2:` header (`_delimited`), quoted or not. 2.18 renders a condition that is a template and
    runs what it renders, which the walk does not compute (`when: "{{ 'look' ~ 'up(…) is string' }}"`, or spliced
    past its `{% set %}`: fold 9's read 150, S-A; under `break_when` and an `apply:` `when:` too: fold 11's reads 171,
    S4 and 170, S-2); 2.19 refuses one, renders each literal of a condition that holds a delimiter as a template, a
    lookup in it too, where the expression folds to a value the walk reads no literal of (`'{#' != '{{ lookup(…) }}'`,
    and a literal whose braces are written as their codes, which Jinja decodes: 170, S-1 and fold 12's probe), and
    honours a header past the whitespace it strips (170, S-3). A condition that holds a delimiter in a literal alone
    (`'{{' in banner`) 2.19 fails, rendering the literal; 2.18 may run it to its end; fold 11 read it as its expression
    and fold 12 refuses it again, as fold 10 did, to keep the cores' behaviour in step (170, S-4; 171, S1; fold 12's
    read 180, N-E). A literal delimiter or header is written in pieces (`'{' ~ '{'`, `'#' ~ 'jinja2:'`), which the
    refusal names (fold 12's read 181, N1)."""
    if not isinstance(value, dict):
        return
    a, lc = args(value, "assert"), value.get("loop_control")
    applied = [ref["apply"] for key in ("include_tasks", "include_role") for ref in [args(value, key)]
               if isinstance(ref, dict) and isinstance(ref.get("apply"), dict)]
    for v in ([value.get(k) for k in _CONDITIONS] + ([a.get("that")] if isinstance(a, dict) else [])
              + ([lc.get("break_when")] if isinstance(lc, dict) else []) + [x.get("when") for x in applied]):
        for s in v if isinstance(v, list) else [v]:
            if isinstance(s, str) and _delimited(s):
                raise Unfollowed("a condition that holds a delimiter's start or a `#jinja2:` header, or that does not "
                                 "parse as one expression, in %s: %.200s (a literal one is written in pieces: "
                                 "`'{' ~ '{'`, `'#' ~ 'jinja2:'`)" % (where, s))


def _templated_module(task, where):
    """Refuse an `action:` or `local_action:` whose module (a string's first word, or a mapping's `module:`'s:
    `module_words`, as Ansible splits it, so a mapping's `copy content={{ _s }} …` names the copy module: fold 13's
    read 190, N-1) holds a delimiter's start or a header (`_DELIMITED`): Ansible renders the word and runs the
    module it names, which `args` and the pins' reads of a task's modules never see (`action: "{{ _m }}"`, `_m:
    template`, rendered a template the URL is in and printed it under `--check --diff` on both cores: fold 12's read
    181, S2). A templated module key fails at parse, and a templated include on both cores, so neither is read
    here."""
    for key in ("action", "local_action"):
        a = task.get(key)
        word = a.split()[0] if isinstance(a, str) and a.split() else \
            module_words(a.get("module"))[0] if isinstance(a, dict) else None
        if word is not None and _DELIMITED.search(str(word)):
            raise Unfollowed("a module named by a template, in %s: %s: %.200s" % (where, key, a))


def _files_under(path):
    return sorted(f for f in glob.glob(os.path.join(path, "**", "*"), recursive=True) if os.path.isfile(f))


def _inventory_files(path):
    """Every file Ansible reads as an inventory source under the inventory directory `path`, at any depth, but what it
    skips (`_INVENTORY_SKIP`); a file that is not YAML or JSON (an INI file, one with no extension) fails the walk."""
    out = []
    for name in sorted(os.listdir(path)) if os.path.isdir(path) else []:
        p = os.path.join(path, name)
        if _INVENTORY_SKIP.search(name):
            continue
        if os.path.isdir(p):
            out += _inventory_files(p)
        elif name.endswith((".yml", ".yaml", ".json")):
            out.append(p)
        else:
            raise Unfollowed("an inventory file the walk does not parse: %s" % p)
    return out


def _cumulative(parents):
    """Each path Ansible's include loaders try a nested include's name under, from `parents`, the directory each
    include above it was written in (a role's root for an include_role), the nearest first: walking up, the dynamic
    loader (included_file.py) and the static one (helpers.py) join each onto the path so far; once the path is
    absolute, the dynamic one starts again from the next, the static one keeps it."""
    out = []
    for dynamic in (True, False):
        path = None
        for d in parents:
            path = d if path is None or (dynamic and os.path.isabs(path)) else \
                path if os.path.isabs(path) else os.path.join(d, path)
            out.append(path)
    return list(dict.fromkeys(out))


# Where a task's file stands: `base`, the directory of the file that holds it; `chain`, the roots of the role it runs
# in and of each role that brought that one in (a meta dependency's dependents, an include_role's includer), its own
# first; `pbdir`, the directory of the playbook its play is in; `parents`, for `_cumulative`; `handler`, whether it
# runs as a handler (a role's handler include searches `handlers/`).
Ctx = collections.namedtuple("Ctx", "base chain pbdir parents handler")


class Walk:
    """`plays`: every play of the playbook and of each playbook it imports, in order. `tasks`: every task and
    handler they run (pre_tasks, tasks, post_tasks and handlers; inside a block, rescue or always; of every task file a
    task imports or includes; of every role a play lists or a task imports or includes, its meta dependencies first,
    its `tasks_from` and `handlers_from` included, a role named in the k=v string form too). `via`: each task's id to
    the id of the import or include that brought it in. `deps`: (meta file, dependency) for each meta dependency written
    as a mapping, whose params and `vars:` the role sees. `var_files`: every vars file they load (each inventory file
    and its group_vars and host_vars; a role's defaults and vars, every file of a `main/` directory or `<x>_from`;
    include_vars, a `dir:` included, under its `extensions:`; each play's vars_files, a first-found list's every
    entry; the group_vars and host_vars beside each playbook, whatever the extension). `templates`: {task id: every
    template it renders: a `template` task's `src` (under `args:`, `action:` or `local_action:` too), each term of a
    template lookup in any of its strings, each of a `with_template` under any prefix; and every file each includes,
    imports, imports from, extends or looks up}. `opened`: every YAML or JSON file read.

    A task file, a vars file, a template or an include_vars `dir:` is read in each place listed here, taken from
    Ansible's search orders (its DataLoader's path_dwim_relative and path_dwim_relative_stack, IncludedFile's and the
    static loader's walk up an include's parents, the template action's and lookup's Jinja search paths):
    `<d>/<sub>/<name>` and `<d>/<name>` (`sub` `tasks`, `handlers` for a handler's include, `vars` or `templates`) for
    `d` each role of the chain, the task's directory, its playbook's and the top playbook's, and for a task file the
    working directory (`CWD`) too; under the playbook's directory, the working directory and the role's `tasks/` (or
    `handlers/`), a task file's name joined to its parents' written directories;
    for each such place named `tasks`, its parent too (DataLoader's RE_TASKS); for a lookup from a task file directly
    below a `tasks/` directory (a role's own, or the working directory's that holds an include the role lacks), that
    `tasks/` and its parent too; and for a template's own include, lookup or import also the top template's directory
    and the including one's. `{{ playbook_dir }}` is the directory of the playbook the task is in. A role is found in
    each playbook's `roles/` and directory, the roles path and beside each role of the chain; an imported playbook in
    its importer's directory alone (Ansible's PlaybookInclude joins the name to it). A template lookup in a play var, a
    vars file or a dependency's param, which Ansible renders where a task reads the var, is found from every directory
    any task's search held, and the var's own. A file it cannot find, or cannot name (`Unfollowed`), fails it loudly."""

    def __init__(self, root, playbook="deploy-stack.yml"):
        self.root = root
        self.playbooks = os.path.join(root, "ansible", "playbooks")      # the top playbook's directory
        self.cwd = os.path.join(root, *CWD)
        self.plays, self.tasks, self.via, self.deps, self.var_files, self.opened = [], [], {}, [], [], []
        self.imports = []
        self.templates, self._texts, self._looked_up, self._dirs, self._held = {}, {}, {}, {}, []
        inventory = os.path.join(root, *INVENTORY)
        for f in _inventory_files(inventory):        # each inventory file: `all: vars:`, a host's
            self._var_file(f)
        for kind in ("group_vars", "host_vars"):
            for f in _files_under(os.path.join(inventory, kind)):
                self._var_file(f)
        self._playbook(os.path.join(self.playbooks, playbook))
        everywhere = list(self._dirs)
        for s, own in self._held:
            here = list(dict.fromkeys([own] + everywhere))
            for name in template_terms(s):
                for p in self._lookup(here, ("templates",), name, None):
                    self._template(self._looked_up.setdefault(s, []), p, here, p)

    def text(self, task):
        """The task as the pins read it: its own keys and strings, then the text of each template it renders."""
        return str(task) + "".join(self._texts[p] for p in self.templates.get(id(task), []))

    def expand(self, value):
        """A var's value as the pins read it: its text, then the text of each template a lookup in one of its strings
        renders (`rendered`)."""
        return str(value) + self.rendered(value)

    def rendered(self, value):
        """The text of each template a lookup in one of `value`'s strings renders."""
        return "".join(self._texts[p] for s in strings(value) for p in self._looked_up.get(s, []))

    def template_text(self, path):
        return self._texts[path]

    @property
    def template_files(self):
        """Every template the walk read, a task's or a var's."""
        return list(self._texts)

    def _read(self, path):
        self.opened.append(path)
        return load(path)

    def _hold(self, value, own):
        """Keep each string of a var's value that holds a template lookup, to render once every task's search is
        known; a string with Jinja of its own fails the walk (`_inline`)."""
        _inline(value, own)
        self._held += [(s, own) for s in strings(value) if template_terms(s)]

    def _var_file(self, path):
        """Read a vars or inventory file, which must be a mapping or empty: a `.yml` inventory file the yaml plugin
        refuses as no mapping, the ini plugin reads as INI (fold 8's read 140, S1)."""
        if path not in self.var_files:
            self.var_files.append(path)
            doc = self._read(path)
            if doc is not None and not isinstance(doc, dict):
                raise Unfollowed("a vars or inventory file that is not a mapping: %s" % path)
            self._hold(doc, os.path.dirname(path))

    def _search(self, ctx):
        """Where a template, a vars file or an include_vars `dir:` is looked up from a task: each role of the chain,
        the task's directory, its playbook's and the top playbook's (below the top an over-read), and for a task file
        directly below a `tasks/` directory that directory and its parent (a lookup's path_dwim_relative_stack takes
        such a file for a role's)."""
        up = os.path.dirname(ctx.base)
        below = [up, os.path.dirname(up)] if os.path.basename(up) == "tasks" else []
        dirs = list(dict.fromkeys(list(ctx.chain) + [ctx.base] + below + [ctx.pbdir, self.playbooks]))
        self._dirs.update(dict.fromkeys(dirs))
        return dirs

    def _task_places(self, ctx):
        """Where a task file an import or include names is looked up (DataLoader.path_dwim_relative, from the include
        loaders): `_search`'s directories, the deploy's working directory, and under each `_cumulative` path the
        playbook's directory, the working directory and the role's `tasks/` (or `handlers/`); a union of the dynamic
        and the static loaders' orders, a role's and a play's, so some are over-reads."""
        subs = ("tasks", "handlers") if ctx.handler else ("tasks",)
        under = [ctx.pbdir, self.cwd] + [os.path.join(r, s) for r in ctx.chain[:1] for s in subs]
        places = self._search(ctx) + [self.cwd] + [os.path.join(d, c) for c in _cumulative(ctx.parents) for d in under]
        # a place named `tasks` stands for its parent too: path_dwim_relative takes a role's so (its RE_TASKS), and a
        # role's include under `x/tasks/` loads `<role>/tasks/x/<name>` (fold 8's read 140, S2)
        places += [os.path.dirname(os.path.normpath(p)) for p in places
                   if os.path.basename(os.path.normpath(p)) == "tasks"]
        return list(dict.fromkeys(places)), subs

    def _lookup(self, dirs, subs, ref, pbdir, want=os.path.isfile):
        """Every `<d>/<sub>/<ref>` and `<d>/<ref>` that `want` holds (a file, or a directory for an include_vars
        `dir:`), for `d` each of `dirs` and `sub` each of `subs`; `{{ playbook_dir }}` read as `pbdir`, the directory of
        the playbook the play is in, so `ref` may be absolute. None found fails the walk."""
        if not isinstance(ref, str):
            raise Unfollowed(repr(ref))
        if pbdir is not None:
            ref = _PLAYBOOK_DIR.sub(lambda m: pbdir, ref)
        if "{{" in ref or "{%" in ref:
            raise Unfollowed(ref)
        candidates = [ref] if os.path.isabs(ref) else [os.path.join(d, *s, *ref.split("/"))
                                                        for d in dirs for s in [[sub] for sub in subs] + [[]]]
        found = list(dict.fromkeys(os.path.normpath(p) for p in candidates if want(p)))
        if not found:
            raise FileNotFoundError(ref)
        return found

    def _playbook(self, path):
        pbdir = os.path.dirname(path)
        for kind in ("group_vars", "host_vars"):
            for f in _files_under(os.path.join(pbdir, kind)):
                self._var_file(f)
        for play in self._read(path) or []:
            ref = module(play, "import_playbook")
            if ref is not None:                         # the importer's directory alone, as Ansible joins it
                keys = set(play) - {"name", "tags", "when"} - {p + "import_playbook" for p in _PREFIXES}
                if keys:                                # an entry's `vars:` reach every play it imports
                    raise Unfollowed("an import_playbook entry's %s, in %s" % (", ".join(sorted(keys)), path))
                self.imports.append(play)               # its tags and condition, read as a play's keys (140, N1)
                self._hold({k: v for k, v in play.items() if k in ("tags", "when")}, pbdir)
                _conditions(play, path)
                for p in self._lookup([pbdir], (), ref, pbdir):
                    self._playbook(p)
                continue
            self.plays.append(play)
            self._hold({k: v for k, v in play.items() if k not in ("pre_tasks", "tasks", "post_tasks", "handlers")},
                       pbdir)
            ctx = Ctx(pbdir, (), pbdir, (), False)
            for ref in play.get("vars_files") or []:
                if isinstance(ref, list):                   # a first-found list: every entry that is there
                    files = [f for name in ref for f in self._found(self._search(ctx), ("vars",), name, pbdir)]
                    if not files:
                        raise FileNotFoundError(repr(ref))
                else:
                    files = self._lookup(self._search(ctx), ("vars",), ref, pbdir)
                for f in files:
                    self._var_file(f)
            self._walk(play.get("pre_tasks"), None, ctx)
            for ref in play.get("roles") or []:
                self._role(ref, None, ctx)
            for key in ("tasks", "post_tasks", "handlers"):
                self._walk(play.get(key), None, ctx._replace(handler=key == "handlers"))

    def _found(self, dirs, subs, ref, pbdir):
        try:
            return self._lookup(dirs, subs, ref, pbdir)
        except Unfollowed:
            raise
        except FileNotFoundError:
            return []

    def _role_files(self, base, sub, name, allow_dir):
        """What Ansible loads for `<sub>/<name>` of a role (`name` None for the main file): the first spelling that
        exists, or, for defaults and vars, every file under the directory of that name."""
        for ext in FROM_EXTENSIONS if name else MAIN_EXTENSIONS:
            p = os.path.join(base, sub, (name or "main") + ext)
            if os.path.isdir(p):
                if allow_dir:
                    return _files_under(p)
            elif os.path.exists(p):
                return [p]
        return []

    def _role(self, ref, by, ctx):
        ref = ref if isinstance(ref, dict) else {"name": ref}
        _conditions(ref, ctx.base)
        name = ref.get("name") or ref.get("role")
        if not isinstance(name, str) or "{{" in name or "{%" in name:
            raise Unfollowed(repr(name))
        pbdirs = list(dict.fromkeys([ctx.pbdir, self.playbooks]))
        places = [os.path.join(d, "roles", name) for d in pbdirs] + [os.path.join(self.root, *ROLES_PATH, name)] + \
            [os.path.join(d, name) for d in pbdirs] + [os.path.join(os.path.dirname(r), name) for r in ctx.chain]
        found = list(dict.fromkeys(os.path.normpath(p) for p in places if os.path.isdir(p)))
        if not found:
            raise FileNotFoundError(name)
        for base in found:
            if base in ctx.chain:                       # a role that brings itself in: Ansible refuses the loop
                continue
            # its root an include above its tasks (an include_role's; a play's role's and a dependency's an over-read)
            inner = ctx._replace(chain=(base,) + ctx.chain, parents=(base,) + ctx.parents)
            # a role's argument_specs, which the walk does not read, fails it (SECURITY.md R-ALERT-1; 140, N3)
            metas = [(meta, self._read(meta) or {}) for meta in self._role_files(base, "meta", None, False)]
            if any(os.path.exists(os.path.join(base, "meta", "argument_specs" + e)) for e in MAIN_EXTENSIONS) or \
                    any("argument_specs" in doc for _, doc in metas):
                raise Unfollowed("a role's argument_specs, which the walk does not read: %s" % base)
            for meta, doc in metas:
                for dep in doc.get("dependencies") or []:
                    if isinstance(dep, dict) and (meta, dep) not in self.deps:   # once, however often the role runs
                        self.deps.append((meta, dep))
                        self._hold(dep, base)
                    self._role(dep, by, inner._replace(base=base))
            for sub in ("defaults", "vars", "tasks", "handlers"):
                named = ref.get(sub + "_from")
                files = self._role_files(base, sub, named, sub in ("defaults", "vars"))
                if named and not files:
                    raise FileNotFoundError(os.path.join(base, sub, named))   # Ansible fails on a named file it lacks
                for f in files:
                    if sub in ("defaults", "vars"):
                        self._var_file(f)
                    else:
                        self._walk(self._read(f), by, inner._replace(base=os.path.dirname(f),
                                                                     handler=ctx.handler or sub == "handlers"))

    def _template(self, seen, path, dirs, top):
        """Record template `path` in `seen`, and each file it includes, imports, imports from, extends or looks up,
        found in `dirs` (the task's search), the top template's directory or this one's; a template with a `#jinja2:`
        header that can set delimiters of its own (`_header`), or one that does not parse, fails the walk."""
        if path in seen:
            return
        seen.append(path)
        if path not in self._texts:
            with open(path, encoding="utf-8") as fh:
                self._texts[path] = fh.read()
        text = self._texts[path]
        here = list(dict.fromkeys(dirs + [os.path.dirname(top), os.path.dirname(path)]))
        try:
            names = _includes(text, path, True) + template_terms(text, True)
        except Unfollowed as e:
            raise Unfollowed("%s, in the template %s" % (e, path)) from None
        for name in names:
            try:
                found = self._lookup(here, ("templates",), name, None)
            except Unfollowed:
                raise
            except FileNotFoundError as e:
                # a raw block's text is followed as a lookup's or a fact's may be rendered again: for a template task
                # that is an over-read, so the error says how to spell it (fold 9's read 151, N1)
                raise FileNotFoundError(
                    "%s, which the template %s includes or looks up, in its own Jinja or in a raw block's or a "
                    "literal's text (followed, as a lookup's or a fact's text may be rendered again); in a template "
                    "task's raw block, write the delimiters as `{{ '{%%' }}` and `{{ '%%}' }}`" % (e, path)) from None
            for p in found:
                self._template(seen, p, dirs, top)

    def _render(self, t, s, name, dirs, pbdir):
        """Template `name` as task `t` renders it, recorded under the task and, for a lookup, under its string `s`
        (which `expand` reads)."""
        for p in self._lookup(dirs, ("templates",), name, pbdir):
            got = []
            self._template(got, p, dirs, p)
            for have in [self.templates.setdefault(id(t), [])] + ([self._looked_up.setdefault(s, [])] if s else []):
                have += [g for g in got if g not in have]

    @staticmethod
    def _args(t, key):
        a = args(t, key)
        if a is not None and not isinstance(a, dict):
            raise Unfollowed("%s: %r" % (key, a))
        return a

    def _walk(self, tasks, by, ctx):
        dirs = self._search(ctx)
        for t in tasks or []:
            self.via[id(t)] = by
            self.tasks.append(t)
            _inline(t, ctx.base)
            _conditions(t, ctx.base)
            _templated_module(t, ctx.base)
            for part in ("block", "rescue", "always"):
                if isinstance(t.get(part), list):
                    self._walk(t[part], by, ctx)
            for key in ("import_tasks", "include_tasks"):
                a = self._args(t, key)
                if a is not None:
                    ref = a.get("file", a.get("_raw_params"))
                    for p in self._lookup(*self._task_places(ctx), ref, ctx.pbdir):
                        written = os.path.dirname(_PLAYBOOK_DIR.sub(lambda m: ctx.pbdir, ref))
                        self._walk(self._read(p), id(t), ctx._replace(base=os.path.dirname(p),
                                                                      parents=(written,) + ctx.parents))
            a = self._args(t, "include_vars")
            if a is not None and a.get("dir") is not None:
                extensions = a.get("extensions", ["yaml", "yml", "json"])
                if not isinstance(extensions, list):
                    raise Unfollowed(repr(extensions))
                for d in self._lookup(dirs, ("vars",), a["dir"], ctx.pbdir, os.path.isdir):
                    for f in _files_under(d):
                        if os.path.splitext(f)[1][1:] in extensions:
                            self._var_file(f)
            elif a is not None:
                for f in self._lookup(dirs, ("vars",), a.get("file", a.get("_raw_params")), ctx.pbdir):
                    self._var_file(f)
            a = self._args(t, "template")
            if a is not None:
                if any(_DELIMITERS.search(str(k)) for k in a):
                    raise Unfollowed("a template task under delimiters of its own: %s" % t)
                self._render(t, None, a.get("src"), dirs, ctx.pbdir)
            for key in t:
                if _WITH_TEMPLATE.fullmatch(str(key)):
                    loop = t[key]
                    for name in loop if isinstance(loop, list) else [] if loop is None else [loop]:
                        self._render(t, None, name, dirs, ctx.pbdir)
            for s in strings(t):
                for name in template_terms(s):
                    self._render(t, s, name, dirs, ctx.pbdir)
            for key in ("import_role", "include_role"):
                a = self._args(t, key)
                if a is not None:
                    self._role(a, id(t), ctx)
