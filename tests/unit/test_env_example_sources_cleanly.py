"""docker/.env.example sources cleanly as a shell file, and compose reads the same values from it (I-58).

WHY. tests/validate.sh's vector steps load the shipped placeholder env with `set -a; . docker/.env.example; set +a`.
KONTROLL_API_UVICORN_EXTRA's value holds spaces and was unquoted, so the shell set the variable to `--ssl-keyfile` for
one command, `/tls/api.key`, which it then tried to run: every gate printed `…: /tls/api.key: No such file or
directory`, and anything else that sourced the template got the wrong value. Vector's config reads no such variable,
so no gate went red, which is how it stayed. Compose reads an env file by its own rules (an unquoted value is the
rest of the line, a quoted one loses its quotes), so the stack was right either way. The value is quoted now, and
these rows hold the file to one rule (a value with whitespace is quoted; none holds a backslash inside double quotes,
which the shell keeps and compose reads as an escape) and its readers to it: the shell's value for every name,
sourced under `set -eu` with nothing on stderr, and compose's for the api's command as it resolves. The rule refuses
the shapes on which the two readers part, so a value it accepts reads the same in both. The real docker/.env, which
the deploy renders, is read by compose alone (`--env-file`), so it is not held here.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE = os.path.join(ROOT, "docker", ".env.example")
UVICORN = "--ssl-keyfile /tls/api.key --ssl-certfile /tls/api.crt"
_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _assignments(text=None):
    """[(line number, name, value as a shell gives it, the raw text after `=`)] for every assignment line of the
    template (or of `text`); a quoted value loses its quotes, an inline comment (whitespace, then `#`) is dropped.
    Fails on a line that is neither an assignment, a comment nor blank, and on a value the rule refuses: unquoted
    whitespace, text after a closing quote other than a comment, or a backslash inside double quotes (the shell keeps
    it; compose reads `\\n`, `\\t` and the like as escapes, so the two readers would part)."""
    out, bad = [], []
    if text is None:
        with open(EXAMPLE, encoding="utf-8") as fh:
            text = fh.read()
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            bad.append((n, "not an assignment", line))
            continue
        name, raw = m.groups()
        if raw[:1] in ("'", '"'):
            end = raw.find(raw[0], 1)
            rest = raw[end + 1:] if end > 0 else None
            if rest is None or (rest.strip() and not re.match(r"\s+#", rest)):
                bad.append((n, "text after the closing quote, or none", line))
                continue
            value = raw[1:end]
            if raw[0] == '"' and "\\" in value:
                bad.append((n, "a backslash inside double quotes", line))
                continue
        else:
            value, rest = re.match(r"(\S*)(.*)$", raw).groups()
            if rest.strip() and not re.match(r"\s+#", rest):
                bad.append((n, "unquoted whitespace in the value", line))
                continue
        out.append((n, name, value, raw))
    assert not bad, "docker/.env.example: %s" % bad
    return out


@pytest.mark.parametrize("line, refused", [
    ('X="a\\nb"', "a backslash inside double quotes"),
    ('X="a\\tb"', "a backslash inside double quotes"),
    ("X=a b", "unquoted whitespace in the value"),
    ('X="a b"c', "text after the closing quote, or none"),
    ('X="a b', "text after the closing quote, or none"),
    ("export X=a", "not an assignment"),
])
def test_the_rule_refuses_each_shape_the_two_readers_read_apart(line, refused):
    """The rule the template is held to refuses, with its reason, each shape it names: a backslash inside double
    quotes (bash keeps `\\n` as two characters, compose reads a newline), unquoted whitespace (the shell ends the
    value there and runs the rest), text after the closing quote or no closing quote, and a line that is not a plain
    assignment. Guards the rule losing a refusal while the template holds no such line, so a later value of that shape
    passes every row: the reads of I-58 added `KONTROLL_Z="a\\nb"` and all three rows passed, the shell reading
    `a\\nb` and compose a newline."""
    with pytest.raises(AssertionError) as e:
        _assignments(line + "\n")
    assert refused in str(e.value), str(e.value)


@pytest.mark.parametrize("line, value", [("X='a\\nb'", "a\\nb"), ('X="a b"  # note', "a b"), ("X=a  # note", "a")])
def test_the_rule_reads_a_shape_both_readers_agree_on(line, value):
    """The rule accepts, and reads as both readers do, a backslash inside single quotes (literal to each), a quoted
    value with a comment after it, and an unquoted one with a comment after it. Guards the refusal above reaching a
    shape the readers agree on, which would fail the template for a value that reads the same in both."""
    assert [(name, v) for _n, name, v, _raw in _assignments(line + "\n")] == [("X", value)]


def test_every_value_with_whitespace_is_quoted():
    """Each line of the template is blank, a comment, or `NAME=value`, and a value that holds whitespace is quoted,
    since the gate sources the file with a shell, which ends an unquoted value at the first space and runs the rest
    as a command (I-58). An inline comment after the value is allowed: both readers drop it. The api's uvicorn
    arguments, the one spaced value today, are read whole."""
    found = {name: value for _n, name, value, _raw in _assignments()}
    assert found["KONTROLL_API_UVICORN_EXTRA"] == UVICORN, found.get("KONTROLL_API_UVICORN_EXTRA")


@pytest.mark.skipif(os.name != "posix" or not shutil.which("bash"), reason="the gate sources it with a POSIX bash")
def test_the_template_sources_cleanly_and_the_shell_reads_every_value_whole():
    """`set -eu; set -a; . docker/.env.example; set +a`, as tests/validate.sh's vector steps run it (with `-eu` added,
    so an error stops it): it exits 0, prints nothing on stderr, and every name holds the value the rule above reads
    (before I-58 the shell printed `/tls/api.key: No such file or directory` and the api's arguments were cut at the
    first space). The environment it starts from holds no KONTROLL_ variable, so each value is the file's."""
    rows = _assignments()
    names = " ".join(name for _n, name, _v, _r in rows)
    script = ("set -eu; set -a; . docker/.env.example; set +a; "
              "for n in %s; do printf '%%s=%%s\\0' \"$n\" \"${!n}\"; done" % names)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KONTROLL_", "BASH_ENV", "ENV"))}
    p = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0 and p.stderr == "", (p.returncode, p.stderr)
    shell = dict(item.split("=", 1) for item in p.stdout.split("\0") if item)
    assert shell == {name: value for _n, name, value, _r in rows}, sorted(
        (k, shell.get(k), v) for _n, k, v, _r in rows if shell.get(k) != v)


def _compose_works():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _compose_works(), reason="needs the docker compose CLI (the gate image has it)")
def test_compose_reads_the_quoted_value_without_its_quotes():
    """`docker compose --env-file docker/.env.example config`, as validate's compose-config step runs it: the api's
    command ends `--port 8444` and the uvicorn arguments, with no quote character. Compose drops a quoted value's
    quotes; had it kept them, the arguments would reach the container's `sh -c` as one quoted word, and uvicorn
    would refuse it. Compose resolves the config without a daemon."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KONTROLL_")}
    p = subprocess.run(["docker", "compose", "--env-file", EXAMPLE, "-f", os.path.join(ROOT, "docker", "compose.yaml"),
                        "config", "--format", "json"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    command = json.loads(p.stdout)["services"]["api"]["command"]
    text = " ".join(command) if isinstance(command, list) else command
    assert text.rstrip().endswith("--port 8444 " + UVICORN), text
    assert '"' not in text, text
