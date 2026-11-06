"""How a build's shell text is read: a Dockerfile RUN (tests/unit/test_image_pins.py) and a workflow step's `run:`
(tests/unit/test_ci_workflows.py). Both hold that a check the build runs (a `pip check`, a `sha256sum -c`, a
`pip-audit`) can fail it, so both read the same way when a failing command fails the text it is in.

The text is split into commands on `&&`, `||`, `;`, `|` and a lone `&` (one outside quotes that is not part of
`&&`, `>&`, `<&` or `|&`), with the operator on each side kept ('' at either end). Quotes are not parsed for the
other operators, so one inside a quoted string only adds a boundary. Newlines are not read: a RUN's continuation
lines are already joined (as BuildKit joins them, with no space), and a step's reader joins its lines with `;`, or
with a space after a line that ends in `&&`, `||` or `|`.

Under /bin/sh (POSIX 2.9.3, and `set -e` in 2.14), a failing command fails the whole text only if all of these hold:
- nothing is piped after it: a pipeline's status is its last command's (no pipefail is assumed);
- no `||` follows it in its AND-OR list: a later `||` runs on its failure, and the list takes that command's status;
- its AND-OR list is the last of the text and not sent to the background by a lone `&` (the shell goes on at once,
  with status 0), or errexit is on and it starts its AND-OR list and is the whole of it (errexit is ignored for every
  command of a list but the last, so `set -e; a && b; c` runs `c` after `a` fails, and a guard before it,
  `set -e; [ "$X" = y ] && it; c`, skips it and goes on);
- it is not part of an `if`, `elif`, `while` or `until` condition, or of a pipeline that opens with `!` (errexit is
  ignored there, and a failure picks a branch);
- its pipeline does not run only when the command before it fails (`a || it`);
- it does not run inside a `$(…)`, backquotes or a quoted string (an `sh -c '…'`, an `eval '…'`): its status is
  then not this shell's. An ANSI-C `$'…'` (dash and bash read it differently) leaves the rest of the text open, and
  one later in the check's own AND-OR list or pipeline hides where that list ends;
- it does not run in a compound behind `||` or `!`: the shell skips it, or drops its status (one after a `|` or a
  lone `&` runs, and gives its status as any command does);
- no function or alias is defined in the text, and no entry made in bash's command tables (`hash`, `enable`,
  `BASH_CMDS`, `BASH_ALIASES`; a name read after a space or a `(` and before a space or a redirect, so
  `(hash -p …` and `hash>/dev/null -p …` are read, as written and in the next rule's views, `{hash,-p,…}`, `hash$@ -p
  …`): each can stand in for the checked program, or for `set`;
- no `trap`, `exec`, `return` or `exit` shares the text, but `exit 1` to `exit 255` (`exit 256` is 0): each of the
  others can end the text with a status that is not the failure's. One is read as a word anywhere in a command, as
  written or in a view of it: its brace expansions expanded, then the special parameters a shell may start with empty
  removed (`$@`, `$*`, `$!`, a positional parameter, bare or braced, `$1`, `${10}`, `${010}`, and `$-` and `$_`, which
  dash starts `sh -c` with empty and unset), and any parameter, named, indirect or subscripted too, given a `-`, `:-`,
  `+`, `:+`, `=` or `:=` operator or a pattern replacement read as its word, innermost first, then its quotes,
  backslashes and a locale string's `$` removed (`\\exit 0`, `"exit" 0`, `exit$""`, `exit$@`, `exit${10}`, `exit$_ 0`,
  `${1-exit} 0`, `${1-${2-exit}} 0`, `${#:+exit} 0`, `${x:-exit} 0`, `${x:=exit} 0`, `${0/*/exit} 0`, `${!x:-exit} 0`,
  `{exit,0}`, `command exit 0`, `eval 'exit 0'`); or in that view with every expansion left in it removed (each
  backquoted command first), read once as empty and once as a space, as most can come out empty and some split a word in
  two (`exit${0##*}`, `exit$x 0`, `` exit`:` 0 ``, `e$(:)xit 0`, `exit${IFS}0`, `exit$(echo " ")0`, and, read as a space
  first, a special parameter `_plain` removes: `exit$*0` after `set -- ' '`, `exit$_"0"` after `: ' '`): all but `$#`,
  `$?` and `$$`, which never come out empty; or with each parameter given an operator or a pattern replacement read once
  as its word and once as empty, in every combination, and every other expansion as empty, as each parameter may be
  unset or set empty, so the empty branch of an alternative or a replacement is read, and one beside another's word
  (`exit${x:+foo} 0`, `exit${x/a/b} 0`, `x=; exit${x-foo} 0`, `${y-ex}${x:+foo}it 0`; a command read more ways than
  `_CHOICES` is read as `exit`), each gap it leaves where it removed an expansion glued to a word on both sides read as
  empty and as a space apart from the others (`ex${x}it${IFS}0`), and the command read again with every `'` removed, as
  POSIX sh reads one inside a double-quoted `${…}` as a character (`ex"${x:+y'}"''it 0`); or with each operator's word
  read once an expansion inside it is gone (`${x:-${y}exit} 0`, `${1-exit${y}} 0`, `${0/\\/*/exit} 0`). These last two
  views read a quoted or escaped `}`, `)` or `/` as text, and a case pattern's `)`, its words read with their quotes and
  escapes, as closing nothing (`exit${x:+"}"} 0`, `exit$(echo "(" >/dev/null) 0`, `exit$(case a in b) 1;; esac) 0`,
  `ex$(case a in ")") 1;; esac)it 0`), and read an expansion that holds the opening of another after it, so a `}` inside
  a `$(…)` closes no `${…}` around it (`e${x:+$(echo })}xit 0`), and a bare `{` inside a `${…}` as a character, as sh
  pairs `${x+{}` at its first `}` (`ex${x+{}it 0`). A word is read after an `&`, `|` or redirect and before a redirect
  too (`true&exit 0`, `exit>/dev/null`). A command that opens a `$(…)`, `${…}` or backquoted command that `commands` cut
  at an operator inside it is read with the pieces cut from it, rejoined (`exit$(:|:) 0`, `exit${x+;} 0`, `` exit`:;:` 0
  ``, `exit$(: ")1"; :) 0`), the opening read both blind to quotes and quote-aware. A `$-` or `${-…}` anywhere is read
  as one: it holds the shell's option letters, `e` once a `set -e` runs and `ehB` in a step's bash, which can build
  `exit` or `set` (`set -e; $-xit 0`, `${-%hB}xit 0`, `s$-t -o noexec`);
- no brace expansion the reader cannot read shares the text: a `{` outside quotes, not after a `$`, with a comma or a
  `..` after it in its word, that the view neither expands nor keeps, because an item holds a piece read only one
  level deep (a `${…}` or `$(…)` nested in another, a `<(…)`, a `$((…))`, a `case` inside a `$(…)`) or is cut by an
  operator inside a quote or a substitution (`{exec,true,"a;b"}`): bash may expand it into any command word;
- no `set -n`, `set -t`, `set -o noexec` or `set -o onecmd` shares the text (a flag cluster holding `n` or `t`), nor
  a `shopt` naming noexec or onecmd (`shopt -so noexec`), nor a `set` or a `shopt` given a variable, a backquoted
  command, a glob, a brace expansion or an extended glob (`set -$x`, `` set `cat o` ``, `set +[e]`, `set -{n,n}`,
  `set +@(e)`: each can be one): the shell reads the rest of the text and runs none of it, or stops after one
  command.
Inside a compound command (a brace group, a subshell, a function body, an `if`, `case`, `for`, `while` or `until`
body) only `&&` to the end of the text counts: errexit is ignored inside a compound that is not the last of its
AND-OR list (`{ a; b; } || true` runs `b` after `a` fails), so the `;` rule is not trusted there. A compound closed
before the command leaves nothing open; a `}`, `fi`, `done` or `esac` closes one only as a command of its own (after
a `;`, `&` or `|`, so `echo done` closes nothing) and before a delimiter (so `done=1` and `}x` close nothing), and a
`${…}` is removed first.

Errexit is switched on by a `set -e` (a flag cluster holding `e`, or `-o errexit`) that is a whole command at the top of
the text and runs whenever the text reaches it (not behind `&&` or `||`, not piped, not sent to the background, nothing
open). It is switched off by a `set +e` or `set +o errexit` anywhere in the text (inside a compound, behind `eval` or
`command`, written through quotes, backslashes, a locale string's `$""`, a special parameter empty at the start (`set$@
+e`) or any expansion glued to it (`set$x +e`), or built by a brace expansion, `{set,+e}`), by a `set` given a variable,
a backquoted command, a glob, a brace expansion or an extended glob (`set $o`, `` set `cat o` ``, `set +[e]`, `set
+{e,}`, `set +@(e)`), and by a `shopt` naming errexit or given any of those (`shopt -ou $x`). A `set -e` the reader
cannot place switches nothing on, and a `set` it cannot rule out switches errexit off, so each misreading of these that
it knows of refuses. The reader refuses what it knows it cannot read; what it still cannot see is listed in
tests/unit/test_image_pins.py's docstring ("What the checks still cannot see"). """
import re

_OPERATORS = re.compile(r"(&&|\|\||;|\||\x02)")
_LONE_AMP = re.compile(r"(?<![<>|&])&(?!&)")
_CONDITION = re.compile(r"(?:if|elif|while|until)(?:\s|$)")
_BODY = re.compile(r"(?:then|do)(?:\s|$)")
_SET_OFF = re.compile(r"""\bset\s+(?:\S+\s+)*?(?:["'\\]*\+["'\\]*[a-zA-Z]*e|\+o\s+["']?errexit|\S*[$`[*?{(])"""
                      r"|\bshopt\b.*(?:\berrexit\b|[$`[*?{(])")
_ENDER = re.compile(r"""(?:^|[\s;&|<>'"\\(){}!=])(trap|exec|exit|return)(?:["']?(?:\s+(.*))?$|["'\s;)<>&|])""")
_NOEXEC = re.compile(r"\bset\s+(?:\S+\s+)*?(?:-[a-zA-Z]*[nt][a-zA-Z]*|-o\s+(?:noexec|onecmd)|\S*[$`[*?{(])"
                     r"|\bshopt\b.*(?:\b(?:noexec|onecmd)\b|[$`[*?{(])")
_STAND_IN = re.compile(r"(?:^|[\s(])(?:alias|hash|enable)(?=[\s<>])|\bBASH_(?:ALIASES|CMDS)\b")
# `$-` or `${-…}`: the shell's option letters, which a `set` changes (`e` once a `set -e` runs) and bash runs a step's
# script with (`ehB`), so it can build `exit` or `set` (`set -e; $-xit 0`, `s$-t -o noexec`, `${-%hB}xit 0`: fold 15's
# fuzz); `ender` reads it as one wherever it is, quoted or not.
_OPTIONS = re.compile(r"\$\{?-")


# A brace expansion bash expands (none after a `$`): a list, `{a,b}`, or a sequence of single letters or of integers,
# with an optional step, `{a..e}`, `{1..9..2}` (bash's braces.c). A list's item is made of pieces, as bash's
# brace_gobbler skips each: a backslash and the character after it, a quoted string (a `\"` inside `"…"` too), a `${…}`
# holding no brace and a `$(…)` no parenthesis of its own, or a backquoted command (a space and a comma each may hold),
# or one other character but a space, a comma or a brace, each read whole where it can be and never cut again (an atomic
# group: `$(a,b)` is one piece, never `$(a` and `b)`, and no word makes the reader try each way of cutting it); a word
# (`_WORD`) is made of the same pieces. A piece is read one level deep only: a brace whose item holds a `${…}` or
# `$(…)` nested in another, a `<(…)` or a `$((…))` is neither expanded nor kept, and `unread_brace` refuses it.
# `_SPECIAL`: a special parameter a step's script or a RUN may start with empty (`_EMPTY`: `$@`, `$*`, `$!`, a
# positional parameter, bare or braced, `${10}`, `${010}` the same one, and `$-` and `$_`, which dash starts `sh -c`
# with empty and unset), removed under any operator (bare, `$_` only where no name character follows it: `$_x` is the
# variable `_x`; braced, any `${…}` that opens with one of them, so the variables `${_x}` and `${_1}` and the `${-x}` of
# `$-` are removed too, as `_bare` removes them: stricter, never less read; `ender` refuses any `$-` before a view reads
# it, `_OPTIONS`); and any parameter, special, positional, named, indirect or subscripted (`$0`, `${00}`, `$#`, `$?`,
# `$$`, `${x}`, `${!x}`, `${a[0]}`), given a `-`, `:-`, `+`, `:+`, `=` or `:=` operator or a pattern replacement (`/`,
# `//`, `/#`, `/%`), read as its word (`${1-exit}`, `${_:-exit}`, `${#:+exit}`, `${x:-exit}`, `${x:=exit}`,
# `${0/*/exit}` and `${!x:-exit}` read `exit`). Which of a default, an alternative and a replacement bash takes depends
# on the value; the view reads the word either way, which only adds a refusal, as the text is read as written too.
# `_plain` reads them innermost first, until none is left (`${1-${2-exit}}` and `${1-$@exit}` read `exit`).
_WHOLE = r"""\\.|"(?:[^"\\]|\\.)*"|'[^']*'|\$\{[^{}]*\}|\$\([^()]*\)|`(?:[^`\\]|\\.)*`"""
_PIECE = r"""(?>%s|[^{}\s,"'\\`])""" % _WHOLE
_BRACE = re.compile(r"(?<!\$)\{(?:(%s*(?:,%s*)+)" % (_PIECE, _PIECE)
                    + r"|([a-zA-Z]\.\.[a-zA-Z]|[-+]?\d+\.\.[-+]?\d+)(?:\.\.([-+]?\d+))?)\}")
_WORD = re.compile(r"""(?:%s|[^\s"'\\`])+""" % _WHOLE)
_EMPTY = r"[@*!_-]|0*[1-9]\d*"
_SPECIAL = re.compile(r"\$(?:[@*!1-9-]|_(?!\w)"
                      r"|\{(?:[@*!#?$-]|\d+|!?[A-Za-z_]\w*(?:\[[^]{}]*\])?)(?::?[-+=]|/[/#%%]?[^/{}]*/)([^{}]*)\}"
                      r"|\{(?:%s)[^{}]*\})" % _EMPTY)

def _braced(text, marked=False):
    """`text` with each brace expansion (`_BRACE`) expanded, in any word, the command's own too: a list (`{exit,0}` ->
    `exit 0`, `a{b,c}d` -> `abd acd`, `{exec,true,"a b"}` -> `exec true "a b"`) or a sequence (`{e..e}xit` -> `exit`,
    its step read, at most 64 items), innermost first, until none is left; a brace that holds no comma outside its
    pieces (`{}`, `{x}`, `{${x}}`, `{"a,b"}`, `{$(echo a)}`, nested to any depth) is kept as text once nothing else
    expands, so a list around it expands. It reads more than bash does: a quoted `{a,b}`, which bash leaves, is expanded
    too, a nested list innermost first (`{a,{b,c}}` -> `a b a c`, where bash gives `a b c`), and /bin/sh (a Dockerfile
    RUN) expands none. The gate rules read `_plain`'s view (and `_bare`'s, which starts from it) beside the text as
    written, where each of these only adds a refusal; the digest-name scan (tests/unit/test_image_pins.py) does not read
    it. `marked`: each kept brace is left as `\\x05…\\x06`, so `unread_brace` tells a kept brace from one neither
    expanded nor kept."""
    def items(b):
        if b.group(1) is not None:
            out = [""]
            for piece in re.finditer(_PIECE + "|,", b.group(1)):
                if piece.group(0) == ",":
                    out.append("")
                else:
                    out[-1] += piece.group(0)
            return out
        a, z = b.group(2).split("..")
        num = not a.isalpha()
        lo, hi = (int(a), int(z)) if num else (ord(a), ord(z))
        step = (abs(int(b.group(3) or 1)) or 1) * (1 if hi >= lo else -1)
        return [str(n) if num else chr(n) for n in range(lo, hi + (1 if step > 0 else -1), step)[:64]]

    def word(m):
        w, b = m.group(0), _BRACE.search(m.group(0))
        return " ".join(w[:b.start()] + item + w[b.end():] for item in items(b)) if b else w
    prev = None
    while prev != text:
        prev, text = text, _WORD.sub(word, text)
        if prev == text:
            text = re.sub(r"(?<!\$)\{(%s*)\}" % _PIECE, "\x05\\1\x06", text)
    return text if marked else text.replace("\x05", "{").replace("\x06", "}")


def unread_brace(command):
    """The rest of the word, from its first `{` that is not after a `$`, of a word of `command` that `_braced` leaves
    a brace in, neither expanded nor kept, with a comma or a `..` after it (in `_braced`'s view, its kept braces marked
    and its quoted strings removed), or None: a brace whose items the view cannot read (see `_PIECE`),
    `{exec,true,${x:-${y}}}`, `{exec,true,$(echo $(echo a))}`, `{exec,true,<(echo a)}`, or one `commands` cut at an
    operator inside a quote or a substitution (`{exec,true,"a` once it has cut `{exec,true,"a;b"}`). Bash may expand it
    into any command word. A brace that holds no comma outside its pieces (`{x$(:,y)}`, `find`'s `{}`) and one inside
    quotes are not read as one. Each word is scanned once from its first such `{`, which a later one never precedes; a
    quoted string in it is shown as `…`, and a kept brace as written."""
    for word in _without_quotes(_braced(command, marked=True)).split():
        m = re.search(r"(?<!\$)\{", word)
        if m and ("," in word[m.start():] or ".." in word[m.start():]):
            return word[m.start():].replace("\x01", "…").replace("\x05", "{").replace("\x06", "}")
    return None


def _plain(command, braces=True):
    """`command` with its brace expansions expanded (`_braced`), then the special parameters a step's script and a RUN
    may start with empty removed (`_SPECIAL`: `$@`, `$*` and a positional parameter, bare or braced, `$1`, `${10}`:
    neither is given arguments; `$!`: no job is in the background yet; `$-` and `$_`: dash starts `sh -c` with them
    empty and unset), and any parameter, named, indirect or subscripted too, given a `-`, `:-`, `+`, `:+`, `=` or `:=`
    operator or a pattern replacement read as its word, innermost first until none is left, as bash expands a brace
    before a parameter, then its quotes, backslashes and the `$` of a locale string (bash expands `$""` to nothing), so
    `{exit,0}`, `"set" +e`, `e\\xit 0`, `exit$""`, `exit$@`, `exit${10}`, `${1-exit} 0`, `${1-${2-exit}} 0`, `${#:+exit}
    0`, `${x:-exit} 0`, `${x:=exit} 0`, `${0/*/exit} 0` and `${a[0]:-exit} 0` read as written plainly. One of these that
    is not empty (after a `set --` gives it a word, or a job sent to the background) is removed all the same: beside the
    text as written that only adds a refusal; `_bare`'s space view reads it as a space too (`set -- ' '; exit$*0`); and
    a command word it builds is one built from what a parameter holds, which the checks cannot see (`set -- ex; ${1}it
    0`)."""
    command, prev = (_braced(command) if braces else command), None
    while prev != command:
        prev, command = command, _SPECIAL.sub(lambda m: m.group(1) or "", command)
    return re.sub(r"""\$(?=["'])|["'\\]""", "", command)


# An expansion `_bare` removes: a `${…}`, a `$(…)`, a `(…)` or a backquoted command holding none of its own (so a
# `$((…))` and a `$( (…) )` go innermost first), a `$name`, `$@`, `$*`, `$!`, `$-`, `$_` or a positional parameter.
# `$#`, `$?` and `$$` never come out empty and are kept (`exit$# 0` runs `exit0 0`, a command not found: read 131,
# N1). An arithmetic expansion never comes out empty either, nor do `${#}`, `${#x}` and `$0`. Each is removed all the
# same: an arithmetic expansion so that a substitution holding one is read (`e$(: $((1)))xit 0`), `${#}` and `${#x}` as
# every `${…}` is, and `$0` as a `$name` is (`\w` holds a digit). That refuses `exit$((0))`, `exit${#} 0` and `exit$0
# 0`, each a command not found, although sound.
_EXPANSION = re.compile(r"\$\{[^{}]*\}|\$?\([^()]*\)|`[^`]*`|\$(?:\w+|[@*!_-])")


def _bare(command, sep="", braces=True):
    """`_plain`'s view of `command` with every expansion left in it removed (`_EXPANSION`), each backquoted command
    first (an escaped backquote inside it too: `` exit`:\\`:\\`` 0 ``), then innermost first until none is left: most of
    them can come out empty, a variable never set, a `$(true)`, `${0##*}`, `${#%?}`, and a word glued to it is then read
    alone (`exit${0##*}`, `e$(true)xit 0`, `` `true`exit 0 ``, `set$x +e`, `hash${x} -p …`). `sep` stands in for each:
    `_views` reads it as empty and as a space, as an expansion can also split a word in two (`exit${IFS}0`, `exit$(echo
    " ")0`, `set${IFS}+e`: read 130, S3). Where `sep` is a space, the special parameters `_plain` removes as empty
    (`_SPECIAL`'s bare and removal arms: `$@`, `$*`, a positional parameter, `$_`, bare or braced) are read as a space
    first, innermost first, and one given an operator or a pattern replacement is left for `_plain` to read its word, as
    a `set --` can give one a space (`set -- ' '; exit$*0`) and a command can leave `$_` one where sh is bash (`: ' ';
    exit$_"0"`; `exit$_0` reads the variable `_0`). Like `_plain`'s view, it only adds a refusal to the gate rules,
    which read it beside the text as written."""
    text, prev = re.sub(r"`(?:[^`\\]|\\.)*`", sep, command), None
    while sep and prev != text:
        prev, text = text, _SPECIAL.sub(lambda m: m.group(0) if m.group(1) is not None else sep, text)
    text, prev = _plain(text, braces), None
    while prev != text:
        prev, text = text, _EXPANSION.sub(sep, text)
    return text


# The quote-aware pieces of `_choices`' and `_word`'s views, which read a quoted or escaped `}`, `)` or `/` as text, as
# sh does (`exit${x:+"}"} 0`, `exit$(echo "(" >/dev/null) 0`, `${0/\/*/exit} 0`: read 140, S3, S4). `_SPECIAL_Q`: a
# parameter given an operator or a pattern replacement, as `_SPECIAL` reads one, its pattern and word made of these
# pieces, and `_REPLACE_Q` the start of one that is a pattern replacement (`${x/a/b}`, `${x//a/b}`, `${x/#a/b}`,
# `${x/%a/b}`: the name, then a `/`); `_EXPANSION_Q`: an expansion, as `_EXPANSION` reads one, made of them, holding the
# opening of no other (a `$(`, a `${` or a backquote: the inner one is read first, so a `}` inside a `$(…)` closes no
# `${…}` around it, `e${x:+$(echo })}xit 0`: fold 15's fuzz; a bare `{` is a character of a `${…}`, as sh pairs `${x+{}`
# at its first `}`, `ex${x+{}it 0`: read 151, S3), a name as sh reads it (a positional parameter is one digit:
# `exit$1xit` is `exit`, `$1` and `xit`, which `_EXPANSION`'s `\w+` reads as one name, `$1xit`; `_bare` meets none
# there, as `_plain` removes a `$1` first); `_CASE_PAT`: a case pattern's `)` (`in b)`, `;; b)`, `in (b|c)`), its words
# read quote-aware (`_CASE_PIECE`: `in b\))`, `in ")")`: read 150, S2; read 151, S2), which closes nothing, and one
# after a lone `;`, as `commands` drops the empty command between the two of a `;;` and `_spans` rejoins the rest with
# one (`set$(case a in b) +x;; c) +x;; esac) -n`; stricter only elsewhere). A pattern it cannot read (`$(echo ')')`,
# `${x:-)}`) keeps its `)`, which `_case_unread` finds (fold 17's settle read).
_QPIECE = r"""\\.|"(?:[^"\\]|\\.)*"|'[^']*'"""
_SPECIAL_Q = re.compile(r"\$\{(?:[@*!#?$-]|\d+|!?[A-Za-z_]\w*(?:\[[^]{}]*\])?)(?::?[-+=]|/[/#%%]?(?:%s|[^/{}\\\"'])*/)"
                        r"((?:%s|[^{}\\\"'])*)\}" % (_QPIECE, _QPIECE))
_REPLACE_Q = re.compile(r"\$\{(?:[@*!#?$-]|\d+|!?[A-Za-z_]\w*(?:\[[^]{}]*\])?)/")
_EXPANSION_Q = re.compile(r"\$\{(?:%s|\$(?![({])|[^}\\\"'`$])*\}"
                          r"|\$?\((?:%s|\$(?![({])|[^()\\\"'`$])*\)|`(?:[^`\\]|\\.)*`"
                          r"|\$(?:[A-Za-z_]\w*|\d|[@*!-])" % (_QPIECE, _QPIECE))
_CASE_PIECE = r"""(?:%s|\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`|[^()\s;|\\"'`])+""" % _QPIECE
_CASE_PAT = re.compile(r"((?:\bin|;\s*;?&?)\s+)\(?((?!esac\b)%s(?:\s*\|\s*%s)*)\s*\)" % (_CASE_PIECE, _CASE_PIECE))


def _decase(text):
    """`text` with each case pattern's closing `)` removed (`_CASE_PAT`; `esac)` is kept, as it closes the `$(…)`
    around it), where `text` holds a `case`: the pattern's `)` closes no `$(…)`, so `exit$(case a in b) 1;; esac) 0`
    is one expansion glued to `exit` (read 140, S4). A pattern `_CASE_PIECE` cannot read keeps its `)`
    (`$(echo ')')`, `${x:-)}`); `_case_unread` finds it, and the readers fail closed (fold 17's settle read)."""
    return _CASE_PAT.sub(r"\1\2 ", text) if re.search(r"\bcase\s", text) else text


_CASE_WORD = re.compile(r"(?<![^\s;&|(`])case(?=\s)")
# An `esac` where a command starts: after `;`, or after an `&` that is no redirect's (`>&`, `<&`) and no `&&`'s or
# `|&`'s, after which an `esac` fails to parse or, inside `[[ … ]]`, is a word (read 170, B1; read 180, B1).
_ESAC_WORD = re.compile(r"(?:;|(?<![<>&|])&)[ \t]*esac(?=[\s;&|)`]|$)")


def _case_unread(text):
    """True if `text` (as `_decase` leaves it) holds a case pattern `_decase` did not read: its `)` is left, and closes
    the `$(…)` or the paren around the case before the `esac`, so every reading ends the substitution there and glues
    nothing (`e$(case a in $(echo ')')) :;; esac)xit 0` runs `exit 0`; so do a pattern `${x:-)}`, one in backquotes
    holding an escaped backquote, and one a `$(…)` nesting two parens: the settle read of fold 17). Read with
    `_scan_open`'s stack, each frame keeping the text of its own level (each nested quote, backquote, `${…}`, `$(…)`,
    bash `$[…]` or paren one mark there): a `$(…)`, or a paren inside one, whose own level holds more `case` words than
    `esac` ones closed early, an `esac` counted only where a command starts (`_ESAC_WORD`: after `;`, as the readers
    join lines with `;`, or after a lone `&`), so a decoy `esac` in an argument, a pattern, a `${…}`, an inner `$(…)` or
    `$[…]`, a redirect's word or a `[[ … ]]` evens nothing (`e$(: esac; case a in $(echo ')')) :;; esac)xit 0`, `e$(case
    a in $(echo ')')|esac) :;; esac)xit 0`, `e$(: ${x:-;esac }; case …`: read 170, B1; `e$(: >&esac; case …`, `e$(: $[ ;
    esac ]; case …`, `e$([[ a && esac ]]; case …`: read 180, B1). A bash `$[…]` is a frame in one reading and plain text
    in the other (dash and POSIX sh read no `$[`), and the text is refused if either reading closes early, so a `$[` no
    `]` closes, or one whose `]` sits in a quote, hides no `)` from the dash reading (`e$(: $[ ; case …`, `se$(: $[ "]"
    ; case …`: read 190, B1); a quote inside a `$[…]` is a frame of its own, so a `]` it holds closes nothing (`e$(: $[
    "]" ; esac ]; case …`: read 190, S1). It fails closed: `_open_subst` reads such a text as open until the case's
    `esac` is joined (so `_spans` joins the commands up to it, and the audit after it stays its own command) and
    `_choices` reads it as `exit`, so a pattern `_CASE_PIECE` cannot read is refused in each of these shapes. The walk
    is not sh's parser: a decoy `esac` it reads where a command starts and sh reads none would even the count (a limit
    test_image_pins.py's docstring names). Refused although sound: an unquoted `case` word followed by a blank inside a
    `$(…)` that opens no case (`$(echo case x)`), after the check too (`$(echo case)` is passed there, as no blank
    follows the word; before it, `_left_open` refuses a `$(…)` holding any `case` word, as it did before), and a case
    whose last branch is empty or that has none (`$(case a in x) esac)`, `$(case a in esac)`), whose `esac` follows no
    `;` or lone `&` (read 170, B1)."""
    return _case_walk(text, True) or _case_walk(text, False)


def _case_walk(text, brackets):
    """`_case_unread`'s walk, a bash `$[…]` read as a frame where `brackets` is set and as plain text where not."""
    stack, k = [["", []]], 0  # each frame: its kind and the text of its own level
    while k < len(text):
        ch, top, own = text[k], stack[-1][0], stack[-1][1]
        if top == "'":
            if ch == "'":
                stack.pop()
                stack[-1][1].append("\x01")
        elif ch == "\\":
            k += 1
            own.append("\x01")
        elif top == "`":
            if ch == "`":
                stack.pop()
                stack[-1][1].append("\x01")
        elif ch == "$" and text[k + 1:k + 2] in (("{", "(", "[") if brackets else ("{", "(")):
            stack.append([text[k + 1], []])
            k += 1
        elif top == "[" and ch in "[]":
            if ch == "[":
                stack.append(["[", []])
            elif ch == "]":
                stack.pop()
                stack[-1][1].append("\x01")
        elif top == '"':
            if ch == '"':
                stack.pop()
                stack[-1][1].append("\x01")
            elif ch == "`":
                stack.append([ch, []])
        elif ch == "'" and next((s[0] for s in reversed(stack) if s[0] in ('"', "(", "p")), None) == '"':
            pass
        elif ch in "'\"`":
            stack.append([ch, []])
        elif ch == "}" and top == "{":
            stack.pop()
            stack[-1][1].append("\x01")
        elif ch == "(" and top in ("(", "p"):
            stack.append(["p", []])
        elif ch == ")" and top in ("(", "p"):
            inner = "".join(stack.pop()[1])
            if len(_CASE_WORD.findall(inner)) > len(_ESAC_WORD.findall(inner)):
                return True
            stack[-1][1].append("\x01")
        else:
            own.append(ch)
        k += 1
    return False


# The most readings `_choices` makes of one command, and the most ways `_fills` fills one text's gaps: each parameter
# given an operator doubles the readings, a replacement with a word makes five of each, and each gap glued to a word on
# both sides doubles the fills. A command it would read more ways is read as `exit`, which refuses it.
_CHOICES = 64
_GAP = chr(7)  # where `_choices` removed an expansion, read at the end as empty and as a space (`_fills`)
_GAP_AT = chr(8)  # where it removed a `$@`-like expansion, which splits a word inside double quotes too
_AT = re.compile(r"\$(?:@|\{!?(?:@|[A-Za-z_]\w*(?:\[@\]|@)))")  # `$@`, `${@…}`, `${!p@}`, `${a[@]…}`


def _choices(command):
    """`command` read each way sh may expand it where each parameter is unset, set empty or, as IFS is, set to a space:
    its brace expansions expanded and a case pattern's `)` removed (`_decase`), then, leftmost first, each expansion
    read quote-aware that holds none of its own, nor the opening of one (`_EXPANSION_Q`: `e${x:+$(echo })}xit 0`, fold
    15's fuzz), a parameter given an operator or a pattern replacement (`_SPECIAL_Q`) read once as its word and once as
    empty, a replacement with a word (`_REPLACE_Q`) also as that word with a gap after it, before it and on each side,
    as the value it does not replace is kept and can split the word (sh sets IFS to a space, a tab and a newline, so
    `se${IFS/#/t}-n` runs `set -n`: fold 15's fuzz), and any other as empty, until none is left; then `_bare`'s view of
    each, the expansions it removed read as empty and as a space, each gap glued to a word on both sides apart from the
    others (`_fills`: `ex${x}it${IFS}0`, read 150, S1). Which branch an operator takes turns on its parameter:
    `${x-foo}` and `${x=foo}` give `foo` where x is unset and nothing where it is set empty, `${x+foo}` the reverse,
    `${x:-foo}` and `${x:=foo}` give `foo` either way, `${x:+foo}` and a replacement nothing; and each parameter is one
    or the other, so every combination is read. `_plain` reads every operator's word and `_bare` starts from it, so
    neither reads the empty branch of an alternative (`exit${x:+foo} 0`), of a replacement (`exit${x/a/b} 0`) or of a
    default of a parameter set empty (`x=; exit${x-foo} 0`: read 140, S2), nor one beside another's word
    (`${y-ex}${x:+foo}it 0`, `${1-s}${x/a/b}e''t +e`: fold 15's fuzz, which a view that read each default or assignment
    one way alone passed). A branch no state gives (`${x:-foo}` read as empty) only adds a refusal, as each view does.
    Its quote-aware pairing reads a quoted or escaped `}` or `)` inside an expansion as text (`exit${x:+"}"} 0`,
    `exit$(echo "(" >/dev/null) 0`, `exit$(: ")1"; :) 0`: read 140, S4). It reads the command twice, as written and with
    every `'` removed: POSIX sh reads a `'` inside a double-quoted `${…}` as a character, so `ex"${x:+y'}"''it 0` runs
    `exit 0` where x is unset, and the pairing reads that `'` as opening a quote (read 150, S3). A bare `{` inside a
    `${…}` is a character, as sh pairs `${x+{}` at its first `}` (`ex${x+{}it 0`: read 151, S3). A command read more
    than `_CHOICES` ways, either time, is read as `exit`, so the ender refuses it; so is one holding a case pattern
    `_decase` cannot read (`_case_unread`: `e$(case a in $(echo ')')) :;; esac)xit 0`, fold 17's settle read)."""
    first, out = _decase(_braced(command)), set()
    if _case_unread(first):
        return ("exit",)
    for start in (first, first.replace("'", ""), _scan_gaps(first)):
        got = _readings(start)
        if got is None:
            return ("exit",)
        out |= got
    return tuple(sorted(out))


def _scan_gaps(text):
    """`text` with each outermost `$(…)`, `${…}` and backquoted command, as a stack pairs them left to right (quotes
    nest inside each; `_scan_open`), replaced by a gap; `text` itself if one is left open: a third reading for
    `_choices`, which a quoted `(` or `}` the regex pairing starts inside cannot move (read 160)."""
    spans, stack, k, start = [], [], 0, None
    while k < len(text):
        ch, top = text[k], (stack[-1] if stack else None)
        opener, step = None, 1
        if top == "'":
            if ch == "'":
                stack.pop()
        elif ch == "\\":
            step = 2
        elif top == "`":
            if ch == "`":
                stack.pop()
        elif ch == "$" and text[k + 1:k + 2] in ("{", "("):
            opener, step = text[k + 1], 2
        elif top == '"':
            if ch == '"':
                stack.pop()
            elif ch == "`":
                opener = "`"
        elif ch == "'" and next((s for s in reversed(stack) if s in ('"', "(", "p")), None) == '"':
            pass
        elif ch in "'\"":
            stack.append(ch)
        elif ch == "`":
            opener = "`"
        elif ch == "}" and top == "{":
            stack.pop()
        elif ch == "(" and top in ("(", "p"):
            stack.append("p")
        elif ch == ")" and top in ("(", "p"):
            stack.pop()
        if opener:
            if start is None:
                start = k
            stack.append(opener)
        k += step
        if start is not None and not any(s in ("(", "{", "`", "p") for s in stack):
            spans.append((start, k))
            start = None
    if start is not None:
        return text
    for a, b in reversed(spans):
        text = text[:a] + _GAP + text[b:]
    return text


def _readings(start):
    """{`_bare`'s view of each way `_choices` reads `start`}, or None if it reads it more than `_CHOICES` ways."""
    texts, out = {start}, set()
    while texts:
        later = set()
        for text in texts:
            m = _EXPANSION_Q.search(text)
            if m is None:
                fills = _fills(text)
                if fills is None:
                    return None
                out.update(_bare(f, sep, braces=False) for f in fills for sep in ("", " "))
                continue
            word = _SPECIAL_Q.fullmatch(m.group(0))
            gap = _GAP_AT if _AT.match(m.group(0)) else _GAP
            later.update(text[:m.start()] + x + text[m.end():] for x in ([word.group(1)] if word else []) + [gap])
            if word and word.group(1) and _REPLACE_Q.match(m.group(0)):
                w, head, tail = word.group(1), text[:m.start()], text[m.end():]
                later.update(head + x + tail for x in (w + _GAP, _GAP + w, _GAP + w + _GAP))
        if len(later) > _CHOICES:
            return None
        texts = later
    return out


def _fills(text):
    """{`text` with its gaps filled each way they can split a word}, or None past `_CHOICES` ways: every gap empty, and
    every gap a space, and each gap glued to a word on both sides (neither neighbour a blank or one of `;&|()<>`, and
    not inside double quotes, where an expansion other than a `$@`'s never splits a word: `"e${x}xit$@0"` runs `exit
    0` after `set -- '' ''`, read 161, S1) empty and a space in every combination, the others all empty or all a space.
    A run of gaps is one: empty, or a space somewhere in it, and a `$@`'s if it holds one. One value for every gap
    never reads `ex${x}it${IFS}0`, which runs `exit 0` where x is unset, nor `"ex"${x}"it"${IFS}0` (read 150, S1). A
    command holding the word `eval` with a gap inside double quotes, but for one that is a whole quoted word, is read
    past `_CHOICES` ways: eval reads the string again, so the gap can be a space or a newline (`eval "ex${x}it${IFS}0"`,
    read 160, S3). The word is read with the text's quotes and backslashes removed, as the shell reads a command word
    (`\\eval`, `"eval"`, `e\\val`: read 170, S2), and with a gap allowed before it, between its letters and after it, so
    each gap reads as empty or as a space in any mix (`ev${x}al`, `${x}eval`, `command${IFS}eval`: read 180, S1;
    `ev${x}al${IFS}…`, `command${IFS}ev${x}al …`: read 190, B2)."""
    text = re.sub("[" + _GAP + _GAP_AT + "]+", lambda r: _GAP_AT if _GAP_AT in r.group(0) else _GAP, text)
    parts, quoted, q, k = re.split("[" + _GAP + _GAP_AT + "]", text), [], None, 0
    plain = re.sub(r"""["'\\]""", "", text)
    g = "[" + _GAP + _GAP_AT + "]"
    evals = re.search(r"(?:^|[\s;&|()`]|" + g + ")" + (g + "*").join("eval") + r"(?=\s|$|" + g + ")", plain)
    while k < len(text):  # whether each gap sits inside double quotes; a backslash before a gap leaves it a gap
        ch = text[k]
        if ch in (_GAP, _GAP_AT):
            quoted.append(q == '"' and ch == _GAP)
            if q == '"' and evals and not (text[k - 1:k] == '"' and text[k + 1:k + 2] == '"'):
                return None
        elif ch == "\\" and q != "'" and text[k + 1:k + 2] not in (_GAP, _GAP_AT):
            k += 1
        elif ch == '"' and q != "'":
            q = None if q else '"'
        elif ch == "'" and q != '"':
            q = None if q else "'"
        k += 1
    edge = r"[^\s;&|()<>]"
    glued = [g for g in range(len(parts) - 1)
             if not quoted[g] and re.match(edge, parts[g][-1:]) and re.match(edge, parts[g + 1][:1])]
    if 2 ** len(glued) > _CHOICES:
        return None
    out = set()
    for other in ("", " "):
        for mask in range(2 ** len(glued)):
            seps = [other] * (len(parts) - 1)
            for b, g in enumerate(glued):
                seps[g] = " " if mask >> b & 1 else ""
            out.add(parts[0] + "".join(sep + part for sep, part in zip(seps, parts[1:])))
    return out


def _word(command):
    """`command` with each parameter given an operator or a pattern replacement read as its word, quote-aware
    (`_SPECIAL_Q`), then `_plain`'s view and every expansion left removed as empty, repeated until nothing changes: an
    operator whose word or pattern holds an expansion, or a quoted or escaped `/`, which `_SPECIAL` does not read, is
    read once that expansion is gone (`${x:-${y}exit} 0`, `${1-exit${y}} 0`, `${x:-${y%z}exit} 0`, `${a[${i}]:-exit}
    0`, `${0/*/${y}exit} 0`, `${0/\\/*/exit} 0`, `${0/"/"*/exit} 0`: read 140, S3). Its brace expansions are expanded
    once, on the text as written, as in `_plain`. `_choices` reads each of these too (an operator's word once an
    expansion inside it is a gap), so no row tells `_word` apart from it: a mutant that drops it survives (read 151,
    N3)."""
    text, prev = _braced(command), None
    while prev != text:
        prev, q = text, None
        while q != text:
            q, text = text, _SPECIAL_Q.sub(lambda m: m.group(1), text)
        text = _EXPANSION.sub("", _plain(text, False))
    return text


def _views(command):
    """`command` as written, in `_plain`'s view, in `_bare`'s with its expansions read as empty and as a space, in
    `_word`'s and in each of `_choices`': the gate rules read a word in any of them, so each view only adds a
    refusal."""
    return (command, _plain(command), _bare(command), _bare(command, " "), _word(command)) + _choices(command)


def _open_subst(text):
    """True if `text` opens a `$(…)`, `${…}` or backquoted command it does not close, read two ways, and True if either
    says so: with each escaped character removed and quotes not read (an escaped backquote opens nothing: `echo "see
    \\`x\\`"`, read 140, N4), and quote-aware (`_EXPANSION_Q`), a case pattern's `)` removed first (`_decase`), so a `)`
    or `}` a quote, an escape or a case pattern holds closes nothing (`exit$(echo ")" ; :) 0`, `exit$(case a in b) 1;;
    esac) 0`: read 140, S4; `ex$(case a in \\)) 1;; esac)it 0`: read 150, S2). Reading it both ways joins more of the
    commands `commands` cut, never fewer, so a span that either reading leaves open is read whole. And True while a
    case pattern `_decase` cannot read has closed the `$(…)` around it early (`_case_unread`) and the text holds more
    unquoted `case` words than `esac` ones, so the commands are joined up to the case's `esac` and no further (fold
    17's settle read)."""
    blind, prev = re.sub(r"\\.", "", text, flags=re.S), None
    while prev != blind:
        prev, blind = blind, re.sub(r"\$\{[^{}]*\}|\$?\([^()]*\)|`(?:[^`\\]|\\.)*`", "", blind)
    quoted, prev, bare = _decase(text), None, _without_quotes(_decase(text))
    while prev != quoted:
        prev, quoted = quoted, _EXPANSION_Q.sub("", quoted)
    return bool(re.search(r"\$[({]|`", blind) or re.search(r"\$[({]|`", re.sub(r"\\.", "", quoted, flags=re.S))
                or _scan_open(_decase(text))
                or _case_unread(_decase(text)) and len(_CASE_WORD.findall(bare)) > len(_ESAC_WORD.findall(bare)))


def _scan_open(text):
    """True if `text` leaves a `$(…)`, `${…}` or backquote open, read left to right with a stack as sh nests them:
    quotes nest inside each, a `(` inside a `$(…)` is paired, a `}` or `)` a quote holds closes nothing (read 160)."""
    stack, k = [], 0
    while k < len(text):
        ch, top = text[k], (stack[-1] if stack else None)
        if top == "'":
            if ch == "'":
                stack.pop()
        elif ch == "\\":
            k += 1
        elif top == "`":
            if ch == "`":
                stack.pop()
        elif ch == "$" and text[k + 1:k + 2] in ("{", "("):
            stack.append(text[k + 1])
            k += 1
        elif top == '"':
            if ch == '"':
                stack.pop()
            elif ch == "`":
                stack.append(ch)
        elif ch == "'" and next((s for s in reversed(stack) if s in ('"', "(", "p")), None) == '"':
            pass
        elif ch in "'\"`":
            stack.append(ch)
        elif ch == "}" and top == "{":
            stack.pop()
        elif ch == "(" and top in ("(", "p"):
            stack.append("p")
        elif ch == ")" and top in ("(", "p"):
            stack.pop()
        k += 1
    return any(s in ("(", "{", "`") for s in stack)


def _spans(cmds):
    """[cmds[k] with the commands a `$(…)`, `${…}` or backquoted command it opens runs on into (`commands` cut it at an
    operator inside: `exit$(:|:) 0`, `exit${x+;} 0`), rejoined with their operators]."""
    out = []
    for k in range(len(cmds)):
        text, m = cmds[k][0], k
        while _open_subst(text) and m + 1 < len(cmds):
            odd = (len(text) - len(text.rstrip("\\"))) % 2
            text, m = text + ("" if odd else " ") + cmds[m][2] + ("" if odd else " ") + cmds[m + 1][0], m + 1
        out.append(text)
    return out


def unquote(token):
    """A word without the quotes around it (`"$sha` -> `$sha`)."""
    return token.strip("\"'")


def image_reference(value):
    """The image a compose `image:` value pulls: the default of a `${VAR:-default}`, else the value itself."""
    m = re.fullmatch(r"\$\{\w+:?-(.+)\}", value.strip())
    return m.group(1) if m else value.strip()


def commands(text):
    """[(command, the operator before it, the operator after it)] of shell text; the operator is '' at either end."""
    mask, prev = text, None
    while prev != mask:
        prev, mask = mask, _QUOTED.sub(lambda m: "\x01" * len(m.group(0)), mask)
    amps = {m.start() for m in _LONE_AMP.finditer(mask)}
    text = "".join("\x02" if k in amps else ch for k, ch in enumerate(text))
    parts = ["&" if p == "\x02" else p for p in _OPERATORS.split(text)]
    return [(parts[i].strip(), parts[i - 1] if i else "", parts[i + 1] if i + 1 < len(parts) else "")
            for i in range(0, len(parts), 2) if parts[i].strip()]


# What a command may open a compound with before its program: `(`, `{`, a keyword, `!`.
_PREFIX = re.compile(r"(?:\s*(?:[({]|(?:if|then|else|elif|do|while|until|!)(?=\s|$)))*")
# The constructs a command may close, each matched only when nothing inside it is a quote: the closed quoted strings
# are removed first, so a quote still there is an open one, which swallows the closer (`( echo " ); X` closes nothing).
_CLOSED = (re.compile(r"\$\{[^{}\"'`]*\}"),
           re.compile(r"\bcase\b(?:(?!\bcase\b|\besac\b)[^\"'`])*[;&|]\s*esac(?=[\s;&|()<>]|$)"),
           re.compile(r"\((?:(?!\bcase\b)[^()\"'`])*\)"),
           re.compile(r"\{[^{}\"'`]*[;&|]\s*\}(?=[\s;&|()<>]|$)"),
           re.compile(r"\bif\b(?:(?!\bif\b|\bfi\b)[^\"'`])*[;&|]\s*fi(?=[\s;&|()<>]|$)"),
           re.compile(r"\b(?:for|while|until)\b"
                      r"(?:(?!\b(?:for|while|until|done)\b)[^\"'`])*[;&|]\s*done(?=[\s;&|()<>]|$)"))


_QUOTED = re.compile(r"""'[^']*'|"(?:[^"\\`]|\\.|`(?:[^`\\]|\\.)*`)*"|`(?:[^`\\]|\\.)*`|\\.""")


def _without_quotes(text):
    """`text` with each closed quoted string, backquoted command and backslash-escaped character replaced by a mark that
    is neither a space nor a delimiter (so `}''` and `done""` close nothing: sh reads no quoted word as a reserved
    word), each read as sh reads it: nothing is special inside `'…'`, elsewhere a backslash escapes the next character
    (`\\"` opens no quote), and a `'` inside `"…"` is a character. A quote left open stays. A `$(…)` is not tracked: a
    quote inside one reads as closing the quote around it. Removal repeats until nothing changes, so a quoted string
    that a quote inside a `"$(…)"` hides from one pass goes in the next (`"$(echo '`')"`: the `'`'`, then the rest)."""
    prev = None
    while prev != text:
        prev, text = text, _QUOTED.sub("\x01", text)
    return text


def _ansi_c(text):
    """True if `text` holds a `$'` that is not inside a single-quoted string nor escaped: `grep -q '^ok$' f` opens no
    ANSI-C string (read 160); one inside double quotes still counts."""
    q, k = None, 0
    while k < len(text):
        ch = text[k]
        if q == "'":
            q = None if ch == "'" else q
        elif ch == "\\":
            k += 1
        elif ch == "$" and text[k + 1:k + 2] == "'":
            return True
        elif q == '"':
            q = None if ch == '"' else q
        elif ch in "'\"":
            q = ch
        k += 1
    return False


def _left_open(text):
    """What `text` leaves open once each quoted string, then each construct closed in it, is removed: 'text',
    'compound' or ''."""
    if _ansi_c(text):
        return "text"
    text, prev = _without_quotes(text), None
    while prev != text:
        prev = text
        for pattern in _CLOSED:
            text = pattern.sub(" ", text)
    if "$(" in text or re.search("[`'\"]", text):
        return "text"
    if re.search(r"[({]|\b(?:if|then|else|elif|do|case|for|while|until)\b", text):
        return "compound"
    return ""


_FUNCTION = re.compile(r"\bfunction\s+[^\s;&|()<>]+(?:\s*\(\s*\))?|[^\s;&|()<>$]+\s*\(\s*\)")


def _skipped(text):
    """True if `text` leaves open a compound whose status it does not take: one after `||` or `!` (a function body
    never gets here: gate_faults refuses a function first)."""
    text, prev = _without_quotes(text), None
    while prev != text:
        prev = text
        for pattern in _CLOSED:
            text = pattern.sub(" ", text)
    return any(re.search(r"(?:\|\||(?:^|\s)!)\s*$", text[:m.start()])
               for m in re.finditer(r"[({]|\b(?:if|case|for|while|until)\b", text))


def opened(cmds, i):
    """What cmds[i] runs inside: 'text' for a `$(…)`, backquotes or a quoted string (an `sh -c '…'`, an `eval '…'`:
    its failure is not this shell's) that the text before it leaves open, or that the command itself opens or closes
    unpaired (`echo "$(curl …`, `sha256sum -c -)"`); else 'compound' for a brace group, a subshell, a function body
    or an if/case/for/while/until that the text before it, or the command's own opening `(`, `{` or keyword, leaves
    open; else ''. A construct closed before the command is removed first, so it leaves nothing open."""
    before = _left_open(" ".join([c + " " + a for c, _, a in cmds[:i]] + [_PREFIX.match(cmds[i][0]).group(0)]))
    return "text" if _left_open(cmds[i][0]) == "text" else before


def errexit(cmds, on=False):
    """[whether errexit is on when cmds[k] runs]: `on` at the start, then switched off by any `set +e`/`set +o errexit`
    anywhere (after a keyword, in a brace group, behind an assignment or `command`), and on by a `set -e`/`set -o
    errexit` that is a whole command at the top of the text and runs whenever the text gets there (not behind `&&` or
    `||`, not piped, nothing open). A `set +e` is read in each view of the command (`_views`), with the pieces
    `commands` cut from an expansion it opens (`_spans`: `set$(:|:) +e`)."""
    out, spans = [], _spans(cmds)
    for k, (command, before, after) in enumerate(cmds):
        out.append(on)
        if any(_SET_OFF.search(text) for text in _views(spans[k])):
            on = False
            continue
        words = command.split()
        if not words or words[0] != "set" or before not in ("", ";", "&") or after in ("|", "&") \
                or opened(cmds, k):
            continue
        for n, word in enumerate(words[1:], 1):
            if word == "--":
                break
            if (word == "-o" and words[n + 1:n + 2] == ["errexit"]) or re.fullmatch(r"-[a-zA-Z]*e[a-zA-Z]*", word):
                on = True
    return out


def _pipeline_start(cmds, i):
    """The index of the first command of the pipeline that holds cmds[i]."""
    while i > 0 and cmds[i][1] == "|":
        i -= 1
    return i


def conditions(cmds):
    """[whether cmds[k] runs as a condition]: inside an `if`/`elif`/`while`/`until` condition (from the keyword to its
    `then` or `do`), or in a pipeline that opens with `!`."""
    out, inside = [], False
    for k, (command, _, _) in enumerate(cmds):
        if _BODY.match(command):
            inside = False
        if _CONDITION.match(re.sub(r"^(?:(?:then|do|else)\s+|[({]\s*)*", "", command)):
            inside = True
        start = re.sub(r"^(?:(?:then|do|else)\s+|[({]\s*)*", "", cmds[_pipeline_start(cmds, k)][0])
        out.append(inside or bool(re.match(r"!(?:\s|$)", start)))
    return out


def ender(cmds):
    """The first command of the text holding a `trap`, `exec`, `return` or `exit` that can end it with a status other
    than a failure's (each but `exit 1` to `exit 255`), as a word of its own anywhere in the command (after a keyword,
    an opening `(`, `{` or `!`, a case pattern, `command`, `eval`, an assignment, an `&`, `|` or redirect; or before a
    redirect, `exit>/dev/null`), as written or in a view of it (`_views`: `_plain`'s, its brace expansions expanded, its
    quotes, backslashes, a locale string's `$` and the special parameters empty at the start removed, and a parameter's
    word read; `_bare`'s, every expansion left but `$#`, `$?` and `$$` removed, read as empty and as a space; `_word`'s,
    each operator's word read once an expansion inside it is gone; and `_choices`', each parameter given an operator
    read as its word and as empty, in every combination); or a `set -n`, `set -t`, `set -o noexec` or `set -o onecmd` (a
    flag cluster holding `n` or `t`), a `shopt` naming noexec or onecmd, or a `set` or `shopt` that can be one (the
    module docstring's last rule: the shell then runs none of the rest of the text, or stops after one command); or a
    `$-` anywhere (`_OPTIONS`: its option letters can build `exit` or `set`); or None. A command that opens an expansion
    `commands` cut at an operator inside it is read, and named, with the pieces cut from it (`_spans`: `exit$(: | :)
    0`)."""
    for span in _spans(cmds):
        if _OPTIONS.search(span):
            return span
        for text in _views(span):
            m = _ENDER.search(text)
            if _NOEXEC.search(text):
                return span
            if m and not (m.group(1) == "exit" and re.fullmatch(r"[1-9]\d?|1\d\d|2[0-4]\d|25[0-5]",
                                                                (m.group(2) or "").strip())):
                return span
    return None


def gate_faults(what, cmds, i, on=False, unit="RUN"):
    """["<what> …: <command>"] if a failure of cmds[i] would not fail the text (the module docstring's rules), else [].
    `on`: whether errexit is on at the start of the text."""
    command = cmds[i][0]
    if any(_FUNCTION.search(t) or _STAND_IN.search(t)
           for c in _spans(cmds) for t in (_without_quotes(c),) + _views(c)[1:]):
        return ["%s shares the %s with a function or an alias, or an entry in bash's command tables (`hash`, "
                "`enable`, `BASH_CMDS`, `BASH_ALIASES`), which can stand in for it: %s"
                % (what, unit, command)]
    if conditions(cmds)[i]:
        return ["%s is a condition or negated (its failure would not fail the %s): %s" % (what, unit, command)]
    end = i
    while end + 1 < len(cmds) and cmds[end][2] in ("&&", "|"):
        end += 1
    faults, open_ = [], "text" if any(_ansi_c(c) for c, _, _ in cmds[i:end + 1]) else opened(cmds, i)
    if open_ == "text":
        faults.append("%s runs inside a `$(…)`, backquotes or a quoted string, or after an ANSI-C `$'…'` or in a "
                      "list holding one (its failure would not fail the %s): %s" % (what, unit, command))
    if ender(cmds):
        faults.append("%s shares the %s with `%s`, which can end it with another status or run none of it: %s"
                      % (what, unit, ender(cmds), command))
    unread = next((u for u in (unread_brace(c) for c, _, _ in cmds) if u), None)
    if unread:
        faults.append("%s shares the %s with a brace expansion the check cannot read (`%s`: an item holding a nested "
                      "substitution or parameter, or a quote or operator cut from it), which can build any command "
                      "word: %s" % (what, unit, unread, command))
    if cmds[_pipeline_start(cmds, i)][1] == "||":
        faults.append("%s runs only when the command before it fails: %s" % (what, command))
    if open_ == "compound" and _skipped(" ".join([c + " " + a for c, _, a in cmds[:i]]
                                                 + [_PREFIX.match(command).group(0)])):
        faults.append("%s runs in a compound behind `||` or `!` (its failure would not fail the %s): %s"
                      % (what, unit, command))
    j = i
    while j + 1 < len(cmds) and (cmds[j][2] == "&&" or (cmds[j][2] == "|" and j > i and open_ != "compound")):
        j += 1
    after = cmds[j][2]
    if not (after == "" or (after == ";" and j == i and errexit(cmds, on)[i] and not open_
                            and cmds[_pipeline_start(cmds, i)][1] in ("", ";", "&"))):
        faults.append("%s not followed by && (its failure would not fail the %s): %s" % (what, unit, command))
    return faults
