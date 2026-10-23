"""How a build's shell text is read: a Dockerfile RUN (tests/unit/test_image_pins.py) and a workflow step's `run:`
(tests/unit/test_ci_workflows.py). Both hold that a check the build runs (a `pip check`, a `sha256sum -c`, a
`pip-audit`) can fail it, so both read the same way when a failing command fails the text it is in.

The text is split into commands on `&&`, `||`, `;` and `|`, with the operator on each side kept ('' at either end).
Quotes are not parsed, so a separator inside a quoted string only adds a boundary. Newlines are not read: a RUN's
continuation lines are already joined, and a step's reader joins its lines with `;`.

Under /bin/sh (POSIX 2.9.3, and `set -e` in 2.14), a failing command fails the whole text only if all of these hold:
- nothing is piped after it: a pipeline's status is its last command's (no pipefail is assumed);
- no `||` follows it in its AND-OR list: a later `||` runs on its failure, and the list takes that command's status;
- its AND-OR list is the last of the text, or errexit is on and it is the last command of its list (errexit is
  ignored for every command of an AND-OR list but the last, so `set -e; a && b; c` runs `c` after `a` fails);
- it is not part of an `if`, `elif`, `while` or `until` condition, or of a pipeline that opens with `!` (errexit is
  ignored there, and a failure picks a branch);
- its pipeline does not run only when the command before it fails (`a || it`).
Errexit is on from a `set -e` (a flag cluster holding `e`, or `-o errexit`) that is a whole command, until a `set +e`
or `set +o errexit`. A `set` inside a subshell or after a keyword is not read as one, which only refuses more.
"""
import re

_OPERATORS = re.compile(r"(&&|\|\||;|\|)")
_CONDITION = re.compile(r"(?:if|elif|while|until)(?:\s|$)")
_BODY = re.compile(r"(?:then|do)(?:\s|$)")


def unquote(token):
    """A word without the quotes around it (`"$sha` -> `$sha`)."""
    return token.strip("\"'")


def image_reference(value):
    """The image a compose `image:` value pulls: the default of a `${VAR:-default}`, else the value itself."""
    m = re.fullmatch(r"\$\{\w+:?-(.+)\}", value.strip())
    return m.group(1) if m else value.strip()


def commands(text):
    """[(command, the operator before it, the operator after it)] of shell text; the operator is '' at either end."""
    parts = _OPERATORS.split(text)
    return [(parts[i].strip(), parts[i - 1] if i else "", parts[i + 1] if i + 1 < len(parts) else "")
            for i in range(0, len(parts), 2) if parts[i].strip()]


def errexit(cmds, on=False):
    """[whether errexit is on when cmds[k] runs]: `on` at the start, then switched by each `set` that is a whole
    command (`set -eu`, `set -o errexit`, `set +e`, `set +o errexit`)."""
    out = []
    for command, _, _ in cmds:
        out.append(on)
        words = command.split()
        if not words or words[0] != "set":
            continue
        for k, word in enumerate(words[1:], 1):
            if word == "--":
                break
            if word in ("-o", "+o"):
                if k + 1 < len(words) and words[k + 1] == "errexit":
                    on = word == "-o"
            elif re.fullmatch(r"[-+][a-zA-Z]+", word) and "e" in word[1:]:
                on = word[0] == "-"
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
        out.append(inside or bool(re.match(r"!(?:\s|$)", cmds[_pipeline_start(cmds, k)][0])))
    return out


def gate_faults(what, cmds, i, on=False, unit="RUN"):
    """["<what> …: <command>"] if a failure of cmds[i] would not fail the text (the module docstring's rules), else [].
    `on`: whether errexit is on at the start of the text."""
    command = cmds[i][0]
    if conditions(cmds)[i]:
        return ["%s is a condition or negated (its failure would not fail the %s): %s" % (what, unit, command)]
    faults = []
    if cmds[_pipeline_start(cmds, i)][1] == "||":
        faults.append("%s runs only when the command before it fails: %s" % (what, command))
    j = i
    while j + 1 < len(cmds) and (cmds[j][2] == "&&" or (cmds[j][2] == "|" and j > i)):
        j += 1
    after = cmds[j][2]
    if not (after == "" or (after == ";" and j == i and errexit(cmds, on)[i])):
        faults.append("%s not followed by && (its failure would not fail the %s): %s" % (what, unit, command))
    return faults
