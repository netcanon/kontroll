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
  `(hash -p …` and `hash>/dev/null -p …` are read): each can stand in for the checked program, or for `set`;
- no `trap`, `exec`, `return` or `exit` shares the text, but `exit 1` to `exit 255` (`exit 256` is 0): each of the
  others can end the text with a status that is not the failure's. One is read as a word anywhere in a command, as
  written or with its quotes and backslashes removed (`\\exit 0`, `"exit" 0`, `command exit 0`, `eval 'exit 0'`),
  after an `&`, `|` or redirect and before a redirect too (`true&exit 0`, `exit>/dev/null`);
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
`command`, written through quotes, backslashes or a locale string's `$""`), by a `set` given a variable, a backquoted
command, a glob, a brace expansion or an extended glob (`set $o`, `` set `cat o` ``, `set +[e]`, `set +{e,}`, `set
+@(e)`), and by a `shopt` naming errexit or given any of those (`shopt -ou $x`). A `set -e` the reader cannot place
switches nothing on, and a `set` it cannot rule out switches errexit off, so each misreading of these that it knows of
refuses. The reader refuses what it knows it cannot read; what it still cannot see is listed in
tests/unit/test_image_pins.py's docstring ("What the checks still cannot see").
"""
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


def _plain(command):
    """`command` with its quotes and backslashes removed, and the `$` of a locale string (bash expands `$""` to
    nothing), so `"set" +e`, `e\\xit 0` and `exit$""` read as written plainly."""
    return re.sub(r"""\$(?=["'])|["'\\]""", "", command)


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


def _left_open(text):
    """What `text` leaves open once each quoted string, then each construct closed in it, is removed: 'text',
    'compound' or ''."""
    if "$'" in text:
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
    """[whether errexit is on when cmds[k] runs]: `on` at the start, then switched off by any `set +e`/`set +o
    errexit` anywhere (after a keyword, in a brace group, behind an assignment or `command`), and on by a `set -e`/
    `set -o errexit` that is a whole command at the top of the text and runs whenever the text gets there (not
    behind `&&` or `||`, not piped, nothing open)."""
    out = []
    for k, (command, before, after) in enumerate(cmds):
        out.append(on)
        if _SET_OFF.search(command) or _SET_OFF.search(_plain(command)):
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
    an opening `(`, `{` or `!`, a case pattern, `command`, `eval`, an assignment, an `&`, `|` or redirect; or before
    a redirect, `exit>/dev/null`), as written or with its quotes and backslashes removed; or a `set -n`, `set -t`,
    `set -o noexec` or `set -o onecmd` (a flag cluster holding `n` or `t`), a `shopt` naming noexec or onecmd, or a
    `set` or `shopt` that can be one (the module docstring's last rule: the shell then runs none of the rest of the
    text, or stops after one command); or None."""
    for command, _, _ in cmds:
        for text in (command, _plain(command)):
            m = _ENDER.search(text)
            if _NOEXEC.search(text):
                return command
            if m and not (m.group(1) == "exit" and re.fullmatch(r"[1-9]\d?|1\d\d|2[0-4]\d|25[0-5]",
                                                                (m.group(2) or "").strip())):
                return command
    return None


def gate_faults(what, cmds, i, on=False, unit="RUN"):
    """["<what> …: <command>"] if a failure of cmds[i] would not fail the text (the module docstring's rules), else [].
    `on`: whether errexit is on at the start of the text."""
    command = cmds[i][0]
    if any(_FUNCTION.search(t) or _STAND_IN.search(t)
           for c, _, _ in cmds for t in (_without_quotes(c), _plain(c))):
        return ["%s shares the %s with a function or an alias, or an entry in bash's command tables (`hash`, "
                "`enable`, `BASH_CMDS`, `BASH_ALIASES`), which can stand in for it: %s"
                % (what, unit, command)]
    if conditions(cmds)[i]:
        return ["%s is a condition or negated (its failure would not fail the %s): %s" % (what, unit, command)]
    end = i
    while end + 1 < len(cmds) and cmds[end][2] in ("&&", "|"):
        end += 1
    faults, open_ = [], "text" if any("$'" in c for c, _, _ in cmds[i:end + 1]) else opened(cmds, i)
    if open_ == "text":
        faults.append("%s runs inside a `$(…)`, backquotes or a quoted string, or after an ANSI-C `$'…'` or in a "
                      "list holding one (its failure would not fail the %s): %s" % (what, unit, command))
    if ender(cmds):
        faults.append("%s shares the %s with `%s`, which can end it with another status or run none of it: %s"
                      % (what, unit, ender(cmds), command))
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
