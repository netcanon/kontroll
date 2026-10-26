"""Every input an image build or the composed stack pulls is pinned: a base by tag and digest, a compose image by an
exact tag (or a digest), a Python package by an exact version from a committed file, an apk package by its minor, a
downloaded file by its checksum.

WHY (the failure this guards). A rebuild is the project's recovery move (docs/floating-prod-rebuild.md), and a
rebuild from `FROM semaphoreui/semaphore:latest` plus a bare `pip install paramiko flask fastapi uvicorn` landed on
whatever upstream published that day: a different Semaphore, a different library in Semaphore's ansible venv, a
base no reviewer saw (L11 F11-4, L17 F17-12). The published images were pinned by digest (docker/images.lock.yml);
the bases they were built from, and every local build, were not.

These checks read the committed text only. They fail when a pin is MISSING or its check is switched off, never
because a pin is old — nothing here compares a version with upstream; Dependabot (.github/dependabot.yml) and the
refresh procedure in docker/README.md move the pins. The host's legacy ansible-core range is held by
test_fresh_install_compat.py.

How a Dockerfile is read. A Dockerfile is any file whose name Dependabot's docker fetcher reads as one; every one lives
under docker/. Its lines are read as BuildKit's parser splits and joins them: split on LF only, a byte-order mark
dropped, continuation lines joined with no space (a line ending in two backslashes continues nothing), a blank line or a
comment line inside a continued instruction skipped, an instruction's keyword ended by a space, a tab, VT, FF or CR. A
whitespace character other than a space, a tab or a line feed (a carriage return inside a line, VT, FF, U+001C to
U+001F, NEL, a no-break space, LS, PS, any other Unicode space) is refused: sh splits words on none of them, while these
checks (and, for VT, FF and CR, BuildKit's keyword split) do. A RUN is read in shell form by tests/unit/_build_read.py
(its docstring gives the rules): split into commands on `&&`, `||`, `;`, `|` and a lone `&`, a command found by its
words anywhere in it (`pip … install`, `apk … add`, `curl`, `wget`, `sha256sum … -c`), so a path before the program, an
option before the subcommand, a shell keyword (`then`) or a `$(…)` does not hide it from being found; a `curl` or `wget`
that an `apt-get install` or `apk add` names is a package, not a download. A command that must stop the build (a `pip
install`, a `pip check`, an `apk add`, a download, a `sha256sum -c`) must fail the RUN when it fails: nothing is piped
after it, no `||` follows it, it is no condition, it does not run inside a `$(…)` or a quoted string (`sh -c '…'`),
after an ANSI-C `$'…'` or in a list holding one, nor in a compound behind `||` or `!`, no function or alias is defined
in the RUN (nor a `hash`, `enable`, `BASH_CMDS` or `BASH_ALIASES` entry), no `trap`, `exec`, `return` or `exit` (but
`exit 1` to `exit 255`) and no `set -n` or `set -t` (or a `shopt` naming noexec or onecmd, or a `set` or `shopt` given a
variable, a backquoted command, a glob or a brace expansion, which can be one) shares the RUN, and every operator after
it to the end of the RUN is `&&` (a lone `&` sends the list to the background) — or errexit is on (`set -e`, `set -o
errexit`), it is inside no compound (a brace group, a subshell, an `if`, a loop), it starts its AND-OR list, and `;`
ends that list right after it. A download is checked by `echo "<digest>  <file>" | sha256sum -c -` in the same
RUN (`sha256sum`, `/bin/sha256sum` or `/usr/bin/sha256sum` the first word of its command), its file written by name (one
URL, one output), the digest an ARG or ENV whose every value in the file is 64 hex, or a shell variable the RUN sets
only from one; a checksum file next to the download is not a check, since whoever swaps the file swaps it too. A SHELL
other than Docker's default, an exec-form RUN (`RUN [...]` that parses as JSON), a heredoc (a `<<` that opens a word
inside a `$((…))` is one to BuildKit: `$((1 << 2))` alone fails the build, unterminated heredoc), a comment inside a RUN
(Docker joins its lines first, so the shell drops the rest), and an `# escape=` directive are refused, since these
checks cannot read them; so is a build arg that names a digest ARG. What the checks still cannot see: a command built at
run time (`$TOOL install`, `eval "$cmd"`), a package manager or downloader they do not name (`npm`, `gem`, `go install`,
`git clone`, `python3 -c 'urllib…'`), the URLs and outputs a curl config file adds (`curl -K`, or with no option at all
a `~/.curlrc` or one under `$CURL_HOME`) or a wget input file (`wget -i`), a second URL written without its scheme and
sent to standard output, a later command that writes the checked file again, a file the RUN sources (`. /tmp/env`, which
can set a digest name), a checked program that an earlier PATH entry shadows (a `sha256sum` an earlier step put in
/usr/local/bin), a quote inside a `$(…)` that is itself inside double quotes (the reader pairs it with the outer one), a
digest ARG one stage declares and another reads, and the apt packages, which float inside the pinned base (SECURITY.md
R-IMG-2). Refused although sound (each fails loudly): a `pip check` in a later RUN than its install, a `pip install`
option that takes a value and is not named here (its value reads as a package), a check written to a file and read by
`sha256sum -c <file>`, a check line written with `printf`, a `;` inside a compound (under `set -e`, or the one before a
brace group's `}`: `{ a && check; }`), a check right before a subshell's `)` (`( install && check )`, behind `&&` or
not: the `)` joins the check's command, which then reads as no `pip check`), a `;` inside a quoted string after a check
(`&& python3 -c "import yaml; …"`), an `exit 0`, `exec 2>&1` or `(exit 0)` anywhere in the RUN, the word `exit`,
`return`, `trap` or `exec` in a message (`echo "will exit on error"`), a function or an alias anywhere in the RUN (the
word `alias`, `hash` or `enable`, or a `name()`, in a quoted message too), the word `set` or `shopt` in a message before
a word holding `$`, a backquote, a glob character, `{` or `(` (`echo "set up (once)"`), an ANSI-C `$'…'` before the
check or later in its own list, a `sha256sum` that is not the first word of its command (`| env sha256sum -c -`), a `#`
right after a `$(…)` (`echo $(true)#x`: one word to sh, a comment inside a RUN to these checks), a `#` after a space
inside quotes (`echo "see #1"`), a SHELL such as hadolint's `["/bin/bash", "-o", "pipefail", "-c"]`, a curl or wget
naming two URLs or two outputs, a trailing comment on a RUN's last line, and a digest ARG redeclared without its value
in a later stage.
"""
import json
import os
import re

import pytest
import yaml
from _build_read import commands as _commands
from _build_read import _plain, gate_faults, image_reference
from _build_read import unquote as _unquote

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Local-build names: tags the deploy itself creates from a Dockerfile in this tree, never pulled from a registry.
_LOCAL_BUILDS = {"kontroll/semaphore:latest", "kontroll/installer:latest"}
# Packages the runner's base already ships in its ansible venv, with the module the runner Dockerfile imports to
# prove it at build time (the closure file installs `--no-deps`, so it never re-installs a base library).
_FROM_THE_BASE = {"paramiko": "paramiko", "pyyaml": "yaml"}
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
_EXACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!_-]*(?:\s+--hash=sha256:[0-9a-f]{64})*$")
# The file names Dependabot's docker entry reads (dependabot-core docker/lib/dependabot/docker/file_fetcher.rb,
# DOCKER_REGEXP, matched anywhere in the name): each is a file these checks read.
_DOCKERFILE_NAME = re.compile(r"dockerfile|containerfile", re.I)
_PIP_INSTALL = re.compile(r"\bpip[\d.]*\"?\s+(?:\S+\s+)*?install\b(.*)$")
_PIP_CHECK = re.compile(r"\bpip[\d.]*\"?(?:\s+-\S+)*\s+check$")
# pip's index options (`pip install --help`); optparse also takes any unambiguous prefix of a long one.
_PIP_INDEX_LONG = ("--index-url", "--extra-index-url", "--find-links", "--trusted-host")
# The value-taking options a `pip install` is read with: the short ones (a cluster's first such letter takes the rest
# of the word, or the next word) and the long ones. Another option's value reads as a package: a loud refusal.
_PIP_SHORT_VALUED = set("rcteifC")
_PIP_LONG_VALUED = ("--requirement", "--constraint", "--target", "--prefix", "--root", "--editable") + _PIP_INDEX_LONG
# pip's own environment and config: an index named there reaches every install.
_PIP_SETTINGS = re.compile(r"\bPIP_(?:INDEX_URL|EXTRA_INDEX_URL|FIND_LINKS|TRUSTED_HOST|CONFIG_FILE)\b"
                           r"|\bpip\.(?:conf|ini)\b|\bpip[\d.]*\"?\s+(?:-\S+\s+)*config\b")
_APK = re.compile(r"\bapk\b(?:\s+-\S+)*\s+(add|upgrade)\b(.*)$")
_APT_INSTALL = re.compile(r"\b(?:apt-get|apt)\s+(?:\S+\s+)*?install\b")
_DOWNLOAD = re.compile(r"\b(curl|wget)\b")
_SHA_CHECK = re.compile(r"\bsha256sum\b.*\s(?:-[a-z]*c[a-z]*|--check)(?:\s|$)")


def _read(rel, root=ROOT):
    with open(os.path.join(root, rel), encoding="utf-8", newline="") as fh:
        return fh.read()


def dockerfiles(root=ROOT):
    """Every Dockerfile under docker/, repo-relative: each file whose name Dependabot's docker fetcher reads as one
    (`Dockerfile`, `Dockerfile.debug`, `build.containerfile`, any case)."""
    out = []
    for dirpath, _, names in os.walk(os.path.join(root, "docker")):
        out += [os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")
                for n in names if _DOCKERFILE_NAME.search(n)]
    return sorted(out)


# Directories that ship nothing: version control, the private overlay and the operator's local files, caches.
_NOT_SHIPPED = {".git", "local", "instance", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                ".ruff_cache", ".venv", "venv"}


def dockerfiles_outside_docker(root=ROOT):
    """Every file outside docker/ whose name Dependabot's docker fetcher reads as a Dockerfile, repo-relative."""
    out = []
    for dirpath, dirnames, names in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if d not in _NOT_SHIPPED and not (rel == "." and d == "docker")]
        out += [(n if rel == "." else rel + "/" + n) for n in names if _DOCKERFILE_NAME.search(n)]
    return sorted(out)


def _lines(text):
    """A Dockerfile's lines as BuildKit splits them: a byte-order mark dropped, split on LF only, a CR before it
    dropped."""
    text = text[1:] if text.startswith("\ufeff") else text
    return [line.rstrip("\r") for line in text.split("\n")]


# Every whitespace character but a space, a tab and a line feed (a CR before a line feed or at the end is dropped).
_ODD_SPACE = re.compile(r"(?!\r\n|\r$)[^\S \t\n]")


def instructions(text):
    """[(KEYWORD, arguments)] of a Dockerfile: continuation lines joined, comment and blank lines dropped. As in
    BuildKit, a blank or comment line inside a continued instruction neither ends nor joins it."""
    out, buf = [], ""
    for line in _lines(text):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.search(r"(?:^|[^\\])(\\[ \t]*)$", line)
        if m:
            buf += line[:m.start(1)]
            continue
        buf += line.rstrip()
        keyword, args = (re.split(r"[\t\v\f\r ]+", buf.strip(), maxsplit=1) + [""])[:2]
        out.append((keyword.upper(), args.strip()))
        buf = ""
    return out


def _runs(text):
    """[the commands of each RUN] of a Dockerfile, as tests/unit/_build_read.py splits them."""
    return [_commands(args) for keyword, args in instructions(text) if keyword == "RUN"]


def _tag(ref):
    """The tag of an image reference (after the last `:` of its last path segment), without a digest; '' if none."""
    name = ref.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else ""


def from_pin_faults(text):
    """["<reason>: <args>"] for each FROM, each COPY or ADD `--from=`, and each RUN `--mount=…,from=…`, that pulls an
    image not pinned as `<image>:<tag>@sha256:<64 hex>` with a tag other than `latest`. An earlier stage's name
    (`FROM base`, `--from=build`, a stage index) and `scratch` pull nothing. A `${NAME}` or `$NAME` in the reference is
    read through the default of an ARG declared before the first FROM; with no default it stays unread, so
    unpinned."""
    faults, stages, defaults, seen_from = [], set(), {}, False
    for keyword, args in instructions(text):
        if keyword == "ARG" and not seen_from:
            for token in args.split():
                name, eq, value = token.partition("=")
                defaults[name] = _unquote(value) if eq else None
            continue
        tokens = [t for t in args.split() if not t.startswith("--")]
        if keyword == "FROM":
            seen_from = True
            refs = tokens[:1] or [""]
        elif keyword in ("COPY", "ADD"):
            refs = re.findall(r"--from=(\S+)", args)
        elif keyword == "RUN":
            refs = [value for mount in re.findall(r"--mount=(\S+)", args)
                    for key, _, value in (pair.partition("=") for pair in mount.split(",")) if key == "from"]
        else:
            continue
        for ref in refs:
            ref = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: defaults.get(m.group(1) or m.group(2)) or m.group(0), ref)
            if ref.lower() in stages or ref == "scratch" or (keyword != "FROM" and ref.isdigit()):
                continue
            if not _DIGEST.search(ref):
                faults.append("no digest: " + args)
            if _tag(ref) in ("", "latest"):
                faults.append("no tag: " + args)
        if keyword == "FROM" and len(tokens) >= 3 and tokens[1].upper() == "AS":
            stages.add(tokens[2].lower())
    return faults


def _json_array(args):
    """The list `args` parses as when it is a JSON array, else None."""
    try:
        value = json.loads(args)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _exec_form(args):
    """True if a RUN's arguments (after its `--` flags) parse as a JSON array, which Docker runs in exec form."""
    return _json_array(re.sub(r"^(?:--\S+\s+)*", "", args)) is not None


def form_faults(text):
    """["<reason>: <line>"] for what these checks cannot read: a SHELL other than Docker's default `["/bin/sh", "-c"]`
    (it changes how every later RUN runs), an exec-form RUN, a heredoc (a `<<` outside a `$((…))`, or one that opens a
    word anywhere: BuildKit reads that as a heredoc inside a `$((…))` too), a `#` that opens a word inside a RUN (after
    a space or an operator character: Docker joins a RUN's lines before the shell reads it, so a comment on one line
    drops every later line; only a whole-line comment is removed first), an `# escape=` directive (it moves which lines
    continue a RUN), and a `# syntax=` parser directive not pinned by digest (BuildKit pulls that frontend image on
    every build)."""
    faults = []
    if _ODD_SPACE.search(text):
        faults.append("a whitespace character other than a space, a tab or a line feed (sh splits words on none of "
                      "them; these checks, and BuildKit's keyword split for VT, FF and CR, do)")
    for line in _lines(text):
        m = re.match(r"\s*#\s*syntax\s*=\s*(\S+)", line, re.I)
        if m and not _DIGEST.search(m.group(1)):
            faults.append("a syntax directive with no digest: " + line.strip())
        if re.match(r"\s*#\s*escape\s*=", line, re.I):
            faults.append("an escape directive: " + line.strip())
    for keyword, args in instructions(text):
        if keyword == "SHELL" and _json_array(args) != ["/bin/sh", "-c"]:
            faults.append("a SHELL instruction other than Docker's default (the RUN rules read /bin/sh -c): " + args)
        if keyword == "RUN" and _exec_form(args):
            faults.append("an exec-form RUN: " + args)
        if keyword == "RUN" and re.search(r"(?:^|[\s;&|()])#", args):
            faults.append("a comment inside a RUN (Docker joins its lines, so the shell drops the rest): " + args)
        if "<<" in re.sub(r"\$\(\(.*?\)\)", "", args) or re.search(r"(?:^|\s)\d*<<", args):
            faults.append("a heredoc: " + args)
    return faults


def compose_images(root=ROOT):
    """{image reference: the file naming it} for every `image:` in docker/services/ and telemetry/ (`.yaml`, `.yml`)."""
    found = {}

    def walk(node, rel):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "image" and isinstance(value, str):
                    found[image_reference(value)] = rel
                walk(value, rel)
        elif isinstance(node, list):
            for item in node:
                walk(item, rel)

    for directory in ("docker/services", "telemetry"):
        for name in sorted(os.listdir(os.path.join(root, directory))):
            if name.endswith((".yaml", ".yml")):
                walk(yaml.safe_load(_read(directory + "/" + name, root)), directory + "/" + name)
    return found


def _pip(command):
    """The pip a command runs: its text up to and including the `pip` word (`"$VENV/pip"`, `python3 -m pip`), with a
    shell keyword (`if`, `then`, `!`) or an opening `{` or `(` before it dropped."""
    m = re.search(r"\bpip[\d.]*\"?", command)
    pip = command[:m.end()].strip() if m else ""
    return re.sub(r"^(?:(?:if|elif|while|until|then|do|else|!)\s+|[({]\s*)*", "", pip)


def pip_arguments(args):
    """(its -r files, the arguments it installs by name, its flags) of the words after a `pip install`. A value-taking
    option takes the rest of its word (`-r/tmp/r.txt`, `--target=/t`) or the next word; an `-e`/`--editable` project
    installs by name; an index option and its value are kept as a flag (`-i=<url>`)."""
    tokens = [_unquote(t) for t in args.split()]
    files, bare, flags, j = [], [], [], 0
    while j < len(tokens):
        token, j = tokens[j], j + 1
        if token.startswith("--") and token.partition("=")[0] in _PIP_LONG_VALUED:
            option, eq, value = token.partition("=")
        elif token.startswith("--") or not token.startswith("-") or token == "-":
            (flags if token.startswith("--") else bare).append(token)
            continue
        else:
            k = next((k for k in range(1, len(token)) if token[k] in _PIP_SHORT_VALUED), len(token))
            flags += ["-" + c for c in token[1:k]]
            if k == len(token):
                continue
            option, eq, value = "-" + token[k], token[k + 1:], token[k + 1:]
        if not eq:
            value, j = (tokens[j] if j < len(tokens) else ""), j + 1
        if option in ("-r", "--requirement"):
            files.append(value)
        elif option in ("-e", "--editable"):
            bare.append(value)
        else:
            flags.append(option + "=" + value)
    return files, bare, flags


def _names_an_index(flag):
    """True for pip's `-i`, `-f` and each long index option, or an unambiguous prefix of one (`--extra-index`)."""
    name = flag.partition("=")[0]
    return name in ("-i", "-f") or (len(name) > 2 and any(o.startswith(name) for o in _PIP_INDEX_LONG))


def pip_installs(text):
    """[(the pip command, its -r targets, the arguments it installs by name, its flags, the commands of its RUN, its
    index there)] for every `pip install` in a Dockerfile."""
    out = []
    for commands in _runs(text):
        for i, (command, _, _) in enumerate(commands):
            m = _PIP_INSTALL.search(command)
            if m:
                out.append((command,) + pip_arguments(m.group(1)) + (commands, i))
    return out


def pip_install_faults(text):
    """["<reason>: <command>"] for each `pip install` in a Dockerfile that names a package itself, names an index,
    lacks `--no-deps`, does not stop the build, or is not followed in the same RUN by a `pip check` of the same pip
    that stops the build; and for an index named in pip's environment or config (`PIP_INDEX_URL`, a `pip.conf`,
    `pip config`) by a Dockerfile that runs one."""
    faults = []
    installs = pip_installs(text)
    for command, files, bare, flags, commands, i in installs:
        if not files or bare:
            faults.append("installs a package by name, not from -r <file>: " + command)
        if "--no-deps" not in flags:
            faults.append("no --no-deps: " + command)
        if any(map(_names_an_index, flags)):
            faults.append("names an index: " + command)
        faults += gate_faults("pip install", commands, i)
        checks = [j for j in range(i + 1, len(commands))
                  if _PIP_CHECK.search(commands[j][0]) and _pip(commands[j][0]) == _pip(command)]
        if not checks:
            faults.append("no `pip check` of the same pip after it: " + command)
        else:
            faults += gate_faults("pip check", commands, checks[0])
    if installs:
        faults += ["names an index in pip's environment or config: " + args
                   for _, args in instructions(text) if _PIP_SETTINGS.search(args)]
    return faults


def apk_faults(text):
    """["<reason>: <command>"] for each `apk add` whose packages do not all carry `~=` or `=`, or that does not stop
    the build, and each `apk upgrade` (it moves every base package past the digest). The name a `--virtual` or `-t`
    gives the added group is not a package."""
    faults = []
    for commands in _runs(text):
        for i, (command, _, _) in enumerate(commands):
            m = _APK.search(command)
            if not m:
                continue
            if m.group(1) == "upgrade":
                faults.append("apk upgrade moves the base's packages: " + command)
                continue
            packages = [_unquote(t) for t in re.sub(r"(?:^|\s)(?:--virtual|-t)\s+\S+", " ", m.group(2)).split()
                        if not t.startswith("-")]
            if not packages or not all(re.fullmatch(r"[\w.+-]+~?=[\w.+-]+", p) for p in packages):
                faults.append("an apk package with no version constraint: " + command)
            faults += gate_faults("apk add", commands, i)
    return faults


def _download_target(command):
    """The file a curl or wget command writes (curl `-o`/`--output`, wget `-O`/`--output-document`, either at the end
    of a cluster of short flags), or None: standard output, curl's remote name, or no output option."""
    tokens = [_unquote(t) for t in command.split()]
    wget = re.search(r"\bwget\b", command) and not re.search(r"\bcurl\b", command)
    short, long_ = (r"-[a-zA-Z]*O", "--output-document") if wget else (r"-[a-zA-Z]*o", "--output")
    for j, token in enumerate(tokens):
        value = None
        if token.startswith(long_ + "="):
            value = token.split("=", 1)[1]
        elif token == long_ or re.fullmatch(short, token):
            value = tokens[j + 1] if j + 1 < len(tokens) else None
        if value:
            return None if value == "-" else value
    return None


def _downloads_one(command):
    """True if a curl or wget command names at most one URL (a word holding `://`) and at most one output option
    (curl `-o`, `--output`, `-O`, `--remote-name`, `--remote-name-all`; wget `-O`, `--output-document`). With more,
    it writes a file that `_download_target` does not name, and nothing checks that file."""
    tokens = [_unquote(t) for t in command.split()]
    wget = re.search(r"\bwget\b", command) and not re.search(r"\bcurl\b", command)
    output = (r"-[a-zA-Z]*O|--output-document(?:=.*)?" if wget
              else r"-[a-zA-Z]*o|-[a-zA-Z]*O[a-zA-Z]*|--output(?:=.*)?|--output-dir(?:=.*)?|--remote-name(?:-all)?")
    return sum("://" in t for t in tokens) <= 1 and sum(bool(re.fullmatch(output, t)) for t in tokens) <= 1


def _is_digest(word, names):
    """True for 64 hex, or a `$name`/`${name}` of one of `names`."""
    m = re.fullmatch(r"\$\{?(\w+)\}?", word)
    return bool(re.fullmatch(r"[0-9a-f]{64}", word) or (m and m.group(1) in names))


def tree_digests(text):
    """{the name of each ARG or ENV of a Dockerfile whose every value in the file is 64 hex}: a digest written in the
    tree. A name an ARG or ENV also sets to anything else (`ENV TOOL_SHA256=$X`), or declares with no value, is not
    one: its value is then not the tree's."""
    values = {}
    for keyword, args in instructions(text):
        if keyword not in ("ARG", "ENV"):
            continue
        tokens = args.split()
        pairs = [t.partition("=")[::2] for t in tokens]
        if keyword == "ENV" and len(tokens) >= 2 and "=" not in tokens[0]:
            pairs = [(tokens[0], " ".join(tokens[1:]))]           # the legacy `ENV NAME value`
        for name, value in pairs:
            values.setdefault(name, []).append(_unquote(value))
    return {name for name, seen in values.items() if all(re.fullmatch(r"[0-9a-f]{64}", v) for v in seen)}


def _digest_names(commands, digests):
    """The names a RUN reads a digest from the tree through: each of `digests` the RUN does not assign, and each shell
    variable whose every `name=value` assignment in the RUN is 64 hex or one of these names. A name `read`, `for`,
    `getopts` or `unset` sets in the RUN is assigned too, with a value that is no digest. Either is read wherever a
    word can start (after a space, `(`, `)`, `;`, `&`, `|` or a backquote), and an assignment also with its quotes
    and backslashes removed (`export TOOL_'SHA256'=…`)."""
    assigned = {}
    for command, _, _ in commands:
        for name, value in re.findall(r"""(?:^|[\s();&|`])["']?(\w+)=(\S*)""", command + " " + _plain(command)):
            assigned.setdefault(name, []).append(_unquote(value))
        for m in re.finditer(r"""(?:^|[\s();&|`])(read|for|getopts|unset)((?:\s+[^\s;&|<>()]+)+)""", command):
            for name in re.findall(r"""\s["']?(\w+)""", m.group(2)):
                assigned.setdefault(name, []).append("<set by %s>" % m.group(1))
    names, grown = {n for n in digests if n not in assigned}, True
    while grown:
        more = {n for n, values in assigned.items() if n not in names and all(_is_digest(v, names) for v in values)}
        names, grown = names | more, bool(more)
    return names


def _checked_words(commands, j, names):
    """The words of the line a `sha256sum -c` at commands[j] checks, when it reads that line from the pipeline feeding
    it (no file operand but `-`, no `<`, no `--ignore-missing`) and the line is exactly `echo <digest> <file>`, piped
    straight into the check, its digest one from the tree; else None. Another command in the pipe (`| cat sidecar |`)
    can replace the line, and a `printf` format can add one that GNU sha256sum skips as malformed."""
    words = commands[j][0].split()
    at = 0 if words and re.fullmatch(r"(?:(?:/usr)?/bin/)?sha256sum", words[0]) else None
    args = words[at + 1:] if at is not None else ["<"]
    if "--ignore-missing" in args or any("<" in a for a in args):
        return None
    operands, skip = [], False
    for a in args:
        if skip or re.match(r"\d*>", a):
            skip = not skip and bool(re.fullmatch(r"\d*>>?", a))
            continue
        if a == "-" or not a.startswith("-"):
            operands.append(a)
    start = j
    while start > 0 and commands[start][1] == "|":
        start -= 1
    line = [_unquote(w).lstrip("*") for c, _, _ in commands[start:j] for w in c.split()]
    if any(o != "-" for o in operands) or start != j - 1 or len(line) != 3 or line[0] != "echo" \
            or not _is_digest(line[1], names):
        return None
    return line


def download_faults(text):
    """["<reason>: <args>"] for each ADD from a URL without `--checksum=sha256:`, and each curl or wget that writes no
    named file or one named through a variable, does not stop the build, or whose file no later `sha256sum -c` of
    the same RUN checks against a digest from the tree (`echo "<digest>  <file>" | sha256sum -c -`); a
    `sha256sum -c` that does not stop the build is one too."""
    faults, digests = [], tree_digests(text)
    for keyword, args in instructions(text):
        if keyword == "ADD":
            sources = [t for t in args.split() if not t.startswith("--")][:-1]
            if any(re.match(r"(?:https?|git|ssh)://|git@", s) for s in sources) and not re.search(
                    r"--checksum=sha256:[0-9a-f]{64}(?:\s|$)", args):
                faults.append("ADD from a URL with no --checksum=sha256: " + args)
    for commands in _runs(text):
        names = _digest_names(commands, digests)
        checked = [_checked_words(commands, j, names) if _SHA_CHECK.search(c) else None
                   for j, (c, _, _) in enumerate(commands)]
        for i, (command, _, _) in enumerate(commands):
            if _SHA_CHECK.search(command):
                faults += gate_faults("sha256sum -c", commands, i)
            if not _DOWNLOAD.search(command) or _APT_INSTALL.search(command) or _APK.search(command):
                continue
            faults += gate_faults("a download", commands, i)
            target = _download_target(command)
            if not _downloads_one(command):
                faults.append("a download naming more than one URL or output (only one file is read as checked): "
                              + command)
            if not target:
                faults.append("a download to no named file: " + command)
            elif "$" in target:
                faults.append("a download to a file named through a variable: " + command)
            elif not any(target in (words or ()) for words in checked[i + 1:]):
                faults.append("a download no later sha256sum -c checks against a digest in the tree: " + command)
    return faults


def copied_from(text, dest):
    """The repo path a Dockerfile COPYs to `dest` (a single-source COPY), or None."""
    for keyword, args in instructions(text):
        parts = [p for p in args.split() if not p.startswith("--")]
        if keyword == "COPY" and len(parts) == 2 and parts[1] == dest:
            return parts[0]
    return None


def image_requirement_files(root=ROOT):
    """{repo path of every requirements file a Dockerfile pip-installs: the Dockerfile}."""
    out = {}
    for rel in dockerfiles(root):
        text = _read(rel, root)
        for install in pip_installs(text):
            for target in install[1]:
                out[copied_from(text, target) or "<not COPY'd: %s>" % target] = rel
    return out


def requirement_lines(text):
    """The requirements of a pip requirements file: backslash continuations joined, comments and blank lines
    dropped."""
    out, buf = [], ""
    for line in text.splitlines():
        line = re.sub(r"(?:^|\s)#.*$", "", line).rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        if (buf + line).strip():
            out.append(" ".join((buf + line).split()))
        buf = ""
    return out + ([" ".join(buf.split())] if buf.strip() else [])


def _dist(line):
    """The normalized distribution name of a requirement (PEP 503)."""
    return re.sub(r"[-_.]+", "-", re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0]).lower()


def _version(text):
    """A version's leading numbers as a tuple (`2.18.19` -> (2, 18, 19))."""
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def _found(faults, expected):
    """Each expected fragment names exactly one fault, in order, and no fault is left over."""
    return len(faults) == len(expected) and all(e in f for e, f in zip(expected, faults))


def test_dockerfiles_are_found_by_dependabots_name_rule(tmp_path):
    """dockerfiles() finds every file under docker/ whose name Dependabot's docker fetcher reads as a Dockerfile (any
    case, with a suffix or a prefix: `Dockerfile.debug`, `build.containerfile`) and nothing else. Guards a variant
    Dockerfile that Dependabot bumps but no pin check reads (T29's read, S3: F2)."""
    for rel in ("docker/a/Dockerfile", "docker/a/Dockerfile.debug", "docker/b/build.containerfile",
                "docker/b/README.md", "docker/b/requirements.txt", "Dockerfile"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("FROM scratch\n", encoding="utf-8")
    assert dockerfiles(str(tmp_path)) == ["docker/a/Dockerfile", "docker/a/Dockerfile.debug",
                                          "docker/b/build.containerfile"]


def test_no_dockerfile_lives_outside_docker():
    """No file outside docker/ is named like a Dockerfile (in Dependabot's docker fetcher's rule). Every pin check
    and the Dependabot coverage check read docker/ only, so a Dockerfile elsewhere would build from bases and
    packages no check reads (T29's fold-2 read, N4)."""
    assert dockerfiles_outside_docker() == []


def test_the_outside_walk_finds_a_dockerfile_anywhere_but_docker(tmp_path):
    """dockerfiles_outside_docker() names a Dockerfile at the root and one under another directory, and neither one
    under docker/ nor one in a directory that ships nothing (`.git`, `local`). Guards the refusal above passing
    because its walk reads nothing."""
    for rel in ("Dockerfile", "tools/x/build.containerfile", "docker/a/Dockerfile", ".git/Dockerfile",
                "local/Dockerfile", "tools/x/README.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("FROM scratch\n", encoding="utf-8")
    assert dockerfiles_outside_docker(str(tmp_path)) == ["Dockerfile", "tools/x/build.containerfile"]


def test_every_dockerfile_is_one_these_checks_read():
    """Every Dockerfile under docker/ (each file Dependabot's docker fetcher reads as one, so `Dockerfile.debug` too)
    runs its RUNs in shell form, holds no heredoc and no `# escape=` directive, and pins any `# syntax=` frontend by
    digest. Guards a build step these checks would read wrongly or not at all, and a frontend image BuildKit pulls
    unpinned on every build (T29's read, S3: F1, F2, P6, P7)."""
    files = dockerfiles()
    assert len(files) >= 3, files                         # the runner, the installer, the Vector image
    assert {rel: f for rel, f in ((rel, form_faults(_read(rel))) for rel in files) if f} == {}


@pytest.mark.parametrize("text,expected", [
    ('RUN ["pip", "install", "requests"]\n', ["exec-form"]),
    ("RUN --mount=type=cache,target=/c [\"sh\", \"-c\", \"true\"]\n", ["exec-form"]),
    ("RUN <<EOF\npip install requests\nEOF\n", ["heredoc"]),
    ("# syntax=docker/dockerfile:1\nFROM x:1.2@sha256:" + "a" * 64 + "\n", ["syntax directive"]),
    ("# syntax=docker/dockerfile:1@sha256:" + "a" * 64 + "\nFROM x:1.2@sha256:" + "a" * 64 + "\n", []),
    ("RUN echo '[x]' && true\n", []),
    ("RUN [ -d /x ] || mkdir /x\n", []),
    ("RUN echo $((1<<2)) && true\n", []),
    ("# escape=`\nFROM x:1.2@sha256:" + "a" * 64 + "\n", ["escape directive"]),
    ("RUN curl -fsSL https://x.test/k -o /k # the key \\\n && echo ok\n", ["comment inside a RUN"]),
    ("RUN VENV=/v # the venv \\\n && pip install x\n", ["comment inside a RUN"]),
    ("RUN true \\\n# a note of its own\n && echo ok\n", []),
    ("RUN echo a#b ${#x} && true\n", []),
    ("RUN curl -fsSL https://x.test/k -o /k;# the key \\\n && echo ok\n", ["comment inside a RUN"]),
    ("RUN true &&# a note \\\n echo ok\n", ["comment inside a RUN"]),
    ('RUN echo "# x" \'#y\' https://x.test/a#frag && true\n', []),
    ('RUN echo "see #1" && true\n', ["comment inside a RUN"]),
    ('SHELL ["/bin/sh", "-c", "eval \\"$0\\" || true"]\n', ["SHELL instruction"]),
    ('SHELL ["/bin/bash", "-o", "pipefail", "-c"]\n', ["SHELL instruction"]),
    ('SHELL ["/bin/sh", "-c"]\n', []),
    ('RUN true\\\\\nSHELL ["/bin/sh", "-c", "eval \\"$0\\" || true"]\n', ["SHELL instruction"]),
    ("\ufeff# syntax=docker/dockerfile:1\nFROM x:1.2@sha256:" + "a" * 64 + "\n", ["syntax directive"]),
    ("\ufeff# escape=`\nFROM x:1.2@sha256:" + "a" * 64 + "\n", ["escape directive"]),
    ("RUN true\r|| true\n", ["a whitespace character"]),
    ("RUN true\x85|| true\n", ["a whitespace character"]),
    ("RUN true\u2028|| true\n", ["a whitespace character"]),
    ("RUN true \\\n\x1f#x || true \\\n&& true\n", ["a whitespace character"]),
    ("RUN true\xa0|| true\n", ["a whitespace character"]),
    ("RUN true\x0b|| true\n", ["a whitespace character"]),
    ("RUN true \\\n\x1c&& true\n", ["a whitespace character"]),
    ("RUN true\u2029|| true\n", ["a whitespace character"]),
    ("RUN true\x0c|| true\n", ["a whitespace character"]),
    ("RUN true\u3000|| true\n", ["a whitespace character"]),
    ("RUN echo $((1\t<<x ))\nx\n", ["heredoc"]),
    ('RUN\t["pip", "install", "requests"]\n', ["exec-form"]),
    ('SHELL\t["/bin/bash", "-c"]\n', ["SHELL instruction"]),
    ("RUN echo $(( <<EOF ))\nx\nEOF\n", ["heredoc"]),
    ("RUN echo $((1 + 2<<EOF))\nx\nEOF\n", ["heredoc"]),
    ("RUN echo $((1 << 2)) && true\n", ["heredoc"]),
    ("RUN true \\\r\n && echo ok\r\n", []),
    ("FROM x:1.2@sha256:" + "a" * 64 + "\r\nRUN echo ok\r", []),
], ids=["exec-form", "exec-form-after-mount", "heredoc", "syntax-unpinned", "syntax-pinned", "shell-form",
        "test-bracket", "arithmetic-shift", "escape-directive", "comment-mid-run", "comment-after-an-assignment",
        "comment-line-in-a-run", "hash-inside-a-word", "comment-after-a-semicolon", "comment-after-and-and",
        "quoted-hash-and-a-url-fragment", "quoted-hash-after-a-space", "shell-that-swallows-every-run",
        "shell-bash-pipefail", "shell-dockers-default", "shell-after-a-double-backslash",
        "byte-order-mark-before-the-syntax", "byte-order-mark-before-the-escape", "carriage-return-inside-a-line",
        "next-line-character", "line-separator", "unit-separator-before-a-hash", "no-break-space", "vertical-tab",
        "file-separator-on-a-continuation", "paragraph-separator", "form-feed", "ideographic-space",
        "tab-before-a-heredoc-in-an-arithmetic-expansion", "exec-form-after-a-tab", "shell-after-a-tab",
        "heredoc-in-an-arithmetic-expansion", "heredoc-after-digits-in-an-arithmetic-expansion",
        "spaced-shift-in-an-arithmetic-expansion", "crlf-line-endings", "carriage-return-at-the-end"])
def test_the_form_check_refuses_what_the_checks_cannot_read(text, expected):
    """The form check flags an exec-form RUN (behind a `--mount` too), a heredoc, an `# escape=` directive, a `#
    syntax=` directive with no digest, and a `#` that opens a word inside a RUN; it passes a pinned directive, a
    shell-form RUN that holds a `[` or opens with a `[ … ]` test (not JSON, so Docker runs it in a shell), a `<<` inside
    `$((…))`, a comment line of its own inside a continued RUN (Docker drops it before the shell reads the RUN), and a
    `#` inside a word. It flags a `#` after an operator character (`;#`, `&&#`), and one after a space inside quotes
    (`"see #1"`, sound but refused); a SHELL other than Docker's default, one that swallows every RUN's status and
    hadolint's bash `-o pipefail` one (sound, but the RUN rules read /bin/sh) alike; and passes a `#` after a quote, a
    URL fragment and Docker's own `["/bin/sh", "-c"]`. It reads a SHELL after a RUN line that ends in two backslashes
    (an escaped backslash continues nothing) and a directive after a byte-order mark (BuildKit drops it), and flags a
    character BuildKit and Python split or strip differently: a carriage return inside a line, NEL, LS, and a U+001F
    before a `#` (Python strips it and skips the line as a comment; BuildKit keeps it and joins the line into the RUN).
    Fold 6's reads added: every other whitespace character but a space, a tab and a line feed (a no-break space, VT, a
    U+001C opening a continuation line, PS: sh reads each as part of a word), an exec-form RUN and a SHELL whose keyword
    a tab ends (BuildKit splits there; a reader that split on a space read no RUN and no SHELL), and a `<<` that opens a
    word inside a `$((…))` (BuildKit reads a heredoc there: `$((1 << 2))` alone fails the build, unterminated heredoc).
    Fold 7's read B added: a form feed and an ideographic space (sh reads each as part of a word), and a tab before a
    `<<` in a `$((…))` (a heredoc to BuildKit). It passes CRLF line endings and a CR at the end of the text (BuildKit
    drops each; fold 7's mutants: V7o). Guards a form check that passes everything, or refuses an ordinary RUN (T29's
    fold-2 read, N3 and N4), a comment that Docker joins into the RUN so the shell drops every later line — an unchecked
    key installed, an install never run, while the checks read the dropped lines (fold 3's read, S1: Z3, Z4; fold 4's
    read, S3: C-a, Z19b) — a SHELL that runs every RUN under a wrapper the checks never read (fold 4's read, N4: C-h,
    Z21), and lines the checks split where BuildKit does not (fold 5's read A, S4 and S6: D6, L2, L3, I1 to I3, M1; fold
    6's read A, S1, S2, S8; its read B, S1, S5, N4: BK5 to BK7; fold 7's read B, S2: OS1, OS2, HD1)."""
    assert _found(form_faults(text), expected), form_faults(text)


def test_every_dockerfile_from_is_tag_and_digest_pinned():
    """Every FROM under docker/, every COPY or ADD `--from=` image, and every RUN `--mount=…,from=` image names a tag
    other than `latest` and an `@sha256:` digest: the digest is what is pulled, the tag is the label a reader checks
    it against. Guards a rebuild silently landing on a different base than the one reviewed and published (F11-4,
    F17-12)."""
    faults = {rel: from_pin_faults(_read(rel)) for rel in dockerfiles()}
    assert {rel: f for rel, f in faults.items() if f} == {}


_PINNED = "x:1.2@sha256:" + "e" * 64


@pytest.mark.parametrize("text,faults", [
    ("FROM debian:12-slim", ["no digest: debian:12-slim"]),
    ("FROM semaphoreui/semaphore:latest@sha256:" + "a" * 64,
     ["no tag: semaphoreui/semaphore:latest@sha256:" + "a" * 64]),
    ("FROM semaphoreui/semaphore@sha256:" + "a" * 64, ["no tag: semaphoreui/semaphore@sha256:" + "a" * 64]),
    ("FROM --platform=$BUILDPLATFORM ghcr.io:443/x/y:1.2@sha256:" + "b" * 64 + " AS build", []),
    ("FROM x:1.2@sha256:" + "c" * 63, ["no digest: x:1.2@sha256:" + "c" * 63]),
    ("FROM ghcr.io:443/x/y@sha256:" + "d" * 64, ["no tag: ghcr.io:443/x/y@sha256:" + "d" * 64]),
    ("ARG BASE=" + _PINNED + "\nFROM ${BASE}", []),
    ("ARG BASE=x:latest\nFROM $BASE", ["no digest: $BASE", "no tag: $BASE"]),
    ("ARG BASE\nFROM ${BASE}", ["no digest: ${BASE}", "no tag: ${BASE}"]),
    ("FROM " + _PINNED + "\nARG BASE=" + _PINNED + "\nFROM ${BASE}", ["no digest: ${BASE}", "no tag: ${BASE}"]),
    ("FROM " + _PINNED + " AS Base\nFROM base\nCOPY --from=base /a /b", []),
    ("FROM scratch\nCOPY --from=0 /a /b", []),
    ("FROM " + _PINNED + "\nCOPY --from=alpine:3.20 /a /b", ["no digest: --from=alpine:3.20 /a /b"]),
    ("FROM " + _PINNED + "\nCOPY --from=x:1.2@sha256:" + "f" * 64 + " /a /b", []),
    ("FROM " + _PINNED + "\nRUN --mount=type=bind,from=busybox:latest,target=/b true",
     ["no digest: --mount=type=bind,from=busybox:latest,target=/b true",
      "no tag: --mount=type=bind,from=busybox:latest,target=/b true"]),
    ("FROM " + _PINNED + " AS build\nFROM " + _PINNED + "\nRUN --mount=type=cache,target=/c "
     "--mount=type=bind,from=build,target=/b true", []),
    ("FROM " + _PINNED + "\nRUN echo done\\\\\nFROM debian:12-slim", ["no digest: debian:12-slim"]),
    ("\ufeffFROM debian:12-slim\nRUN true", ["no digest: debian:12-slim"]),
    ("FROM\tdebian:12-slim", ["no digest: debian:12-slim"]),
    ("  FROM debian:12-slim", ["no digest: debian:12-slim"]),
    ("FROM " + _PINNED + "\nCOPY\t--from=alpine:3.20 /a /b", ["no digest: --from=alpine:3.20 /a /b"]),
], ids=["no-digest", "latest", "digest-only", "registry-port-and-stage", "short-digest", "registry-port-no-tag",
        "arg-default-pinned", "arg-default-latest", "arg-no-default", "arg-after-from", "earlier-stage",
        "scratch-and-index", "copy-from-image", "copy-from-pinned", "mount-from-image", "mount-from-stage",
        "from-after-a-double-backslash", "byte-order-mark-before-the-from", "from-after-a-tab", "indented-from",
        "copy-after-a-tab"])
def test_the_from_check_tells_a_pin_from_a_near_miss(text, faults):
    """The FROM check flags a tag with no digest, `latest` or no tag with a digest, a digest one hex digit short, an
    ARG-built reference whose default is unpinned or missing (or declared after the first FROM, where a FROM cannot read
    it), and a COPY `--from=` or a RUN `--mount=…,from=` an unpinned image; it passes a pinned reference behind a
    `--platform` flag, on a registry with a port, naming a stage, through a pinned ARG default, an earlier stage by name
    (any case, a mount's too), `scratch`, a stage index and a mount with no `from=`. It reads a FROM after a RUN line
    that ends in two backslashes (an escaped backslash continues nothing) and one after a byte-order mark (BuildKit
    drops it), a FROM or a COPY whose keyword a tab ends (BuildKit splits there), and an indented FROM (BuildKit trims
    the indent). Guards a check that passes everything, reads a registry port as a tag, or refuses a correct multi-stage
    or ARG-built pin (T29's read, N4: F3, F5; its fold-2 read, N4: a `--mount` pulled `busybox:latest` unread; its
    fold-5 read A, S4 and S6: D5, L1; its fold-6 read A, S1; its fold-7 read B, S2: KS1)."""
    assert from_pin_faults(text + "\n") == faults


def floating(ref):
    """True if a compose image reference is not pinned: no digest and no tag of at least major.minor, or a digest
    under no tag or `latest` (a local-build name excepted)."""
    if ref in _LOCAL_BUILDS:
        return False
    if _DIGEST.search(ref):
        return _tag(ref) in ("", "latest")
    return not re.match(r"^v?\d+\.\d+", _tag(ref))


@pytest.mark.parametrize("ref,is_floating", [
    ("postgres:16-alpine", True),
    ("postgres:16.15-alpine", False),
    ("prom/prometheus:v3.5.4", False),
    ("grafana/grafana:latest", True),
    ("grafana/grafana", True),
    ("ghcr.io:443/x/y", True),
    ("kontroll/semaphore:latest", False),
    ("postgres:16-alpine@sha256:" + "a" * 64, False),
    ("postgres@sha256:" + "a" * 64, True),
    ("postgres:latest@sha256:" + "a" * 64, True),
], ids=["bare-major", "major-minor", "v-prefix", "latest", "no-tag", "registry-port-no-tag", "local-build",
        "bare-major-with-digest", "digest-no-tag", "latest-with-digest"])
def test_the_compose_tag_check_tells_exact_from_floating(ref, is_floating):
    """The compose-tag check flags a bare major (`16-alpine`), `latest`, no tag, a registry port with no tag, and a
    digest under no tag or `latest`; it passes major.minor with or without a `v` and a `-<variant>`, the local-build
    names, and a digest under a tag (the digest is the pin, the tag its label). Guards a check that reads
    `16-alpine` as pinned — the floating tag this test was written for — or refuses a reference pinned by digest
    (T29's read, N4: K3)."""
    assert floating(ref) is is_floating


def test_compose_images_reads_both_endings_in_both_directories(tmp_path):
    """compose_images() reads the `image:` of a `.yaml` and a `.yml` file in docker/services/ and in telemetry/, and
    nothing from another file. Guards the pin check going blind to the telemetry descriptors, which end in `.yml`
    while the stack fragments end in `.yaml`."""
    for rel, ref in (("docker/services/a.yaml", "a:1.2"), ("docker/services/b.yml", "b:1.2"),
                     ("telemetry/c.yml", "c:1.2"), ("telemetry/d.yaml", "d:1.2"), ("telemetry/README.md", "e:1")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("services:\n  x:\n    image: %s\n" % ref, encoding="utf-8")
    assert compose_images(str(tmp_path)) == {"a:1.2": "docker/services/a.yaml", "b:1.2": "docker/services/b.yml",
                                             "c:1.2": "telemetry/c.yml", "d:1.2": "telemetry/d.yaml"}


def test_every_compose_image_names_an_exact_tag():
    """Every `image:` of a stack fragment and of a telemetry descriptor, `.yaml` or `.yml` (the default of a
    `${VAR:-…}` included), has a tag of at least major.minor or a digest under a tag — not `latest`, not a bare
    major like `16-alpine`. The local-build names are exempt by exact name (tags the deploy creates, never pulled),
    and each exemption must still be named. Guards a fragment shipping a floating tag: `postgres:16-alpine` moved to
    a new minor on every fresh pull."""
    images = compose_images()
    assert {ref: rel for ref, rel in images.items() if floating(ref)} == {}
    assert _LOCAL_BUILDS <= set(images), "stale _LOCAL_BUILDS rows: %r" % sorted(_LOCAL_BUILDS - set(images))


def test_image_builds_install_python_packages_only_from_a_pinned_file():
    """Every `pip install` in a Dockerfile installs from `-r <file>`, names no package and no index, carries
    `--no-deps`, stops the build, and is followed in the same RUN by a `pip check` of the same pip that stops the
    build; each file is COPY'd from the repository, and each of its requirements is `name==version` (with
    `--hash=sha256:` if any). Guards an unpinned package moving a library inside Semaphore's ansible venv on the next
    build, and the pinned file silently ceasing to be the whole truth."""
    seen = 0
    for rel in dockerfiles():
        text = _read(rel)
        assert pip_install_faults(text) == [], rel
        for install in pip_installs(text):
            seen += 1
            for target in install[1]:
                source = copied_from(text, target)
                assert source, "%s: %s is not COPY'd from the repository" % (rel, target)
                lines = requirement_lines(_read(source))
                assert lines and all(_EXACT.match(ln) for ln in lines), (source, [ln for ln in lines
                                                                                  if not _EXACT.match(ln)])
    assert seen >= 2, "the runner and the installer each install Python packages"


_INSTALL = "python3 -m pip install --no-deps -r /tmp/r.txt"


@pytest.mark.parametrize("run,expected", [
    (_INSTALL + " && python3 -m pip check", []),
    ('"$VENV/pip" install --no-cache-dir --no-deps -r /tmp/k/r.txt && "$VENV/pip" check && rm -rf /tmp/k', []),
    ("pip install requests", ["by name", "no --no-deps", "no `pip check`"]),
    ("pip --disable-pip-version-check install requests", ["by name", "no --no-deps", "no `pip check`"]),
    ("pip3.11 install --no-deps requests && pip3.11 check", ["by name"]),
    ("uv pip install --system --no-deps requests && uv pip check", ["by name"]),
    (_INSTALL + " && python3 -m pip check || true", ["pip install not followed", "pip check not followed"]),
    (_INSTALL + "; python3 -m pip check", ["pip install not followed"]),
    (_INSTALL + " && python3 -m pip check; rm -f /tmp/r.txt", ["pip install not followed", "pip check not followed"]),
    (_INSTALL + " && python3 -m pip check | tee /tmp/log", ["pip check not followed"]),
    (_INSTALL + " && pip check", ["no `pip check`"]),
    ("python3 -m pip install -r /tmp/r.txt && python3 -m pip check", ["no --no-deps"]),
    ("python3 -m pip install --no-deps --extra-index-url=https://x.test -r /tmp/r.txt && python3 -m pip check",
     ["names an index"]),
    ("false || " + _INSTALL + " && python3 -m pip check", ["pip install runs only"]),
    ("set -eu; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -eu; " + _INSTALL + "; python3 -m pip check || true", ["pip check not followed"]),
    (_INSTALL + " && python3 -m pip check && rm -f /tmp/r.txt || true",
     ["pip install not followed", "pip check not followed"]),
    (_INSTALL + " && python3 -m pip check && rm -f /tmp/r.txt; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; " + _INSTALL + " && python3 -m pip check; rm -f /tmp/r.txt",
     ["pip install not followed", "pip check not followed"]),
    ("set -o errexit; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; set +o errexit; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; if " + _INSTALL + "; then python3 -m pip check; fi",
     ["pip install is a condition", "pip check not followed"]),
    (_INSTALL + " && ! python3 -m pip check", ["pip check is a condition"]),
    (_INSTALL + " && python3 -m pip check && python3 -m pip list | head -3", []),
    ("python3 -m pip install --no-deps --extra-index=https://x.test -r /tmp/r.txt && python3 -m pip check",
     ["names an index"]),
    ("python3 -m pip install --no-deps -ihttps://x.test -r /tmp/r.txt && python3 -m pip check", ["names an index"]),
    ("python3 -m pip install --no-deps -qf https://x.test -r /tmp/r.txt && python3 -m pip check",
     ["names an index"]),
    ("python3 -m pip install --no-deps --editable=git+https://x.test/y.git -r /tmp/r.txt && python3 -m pip check",
     ["by name"]),
    ("python3 -m pip install --no-deps -e /src/y -r /tmp/r.txt && python3 -m pip check", ["by name"]),
    ("python3 -m pip install --no-deps --target /opt/t -c /tmp/c.txt -r/tmp/r.txt && python3 -m pip check", []),
    ("set -e; set +e; " + _INSTALL + "; python3 -m pip check", ["pip install not followed"]),
    ("set -- -e; " + _INSTALL + "; python3 -m pip check", ["pip install not followed"]),
    ("python3 -m pip check && " + _INSTALL, ["no `pip check`"]),
    ("set -e; { cd /tmp; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt; } || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; ( cd /tmp; " + _INSTALL + "; python3 -m pip check; true ) || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; sh -c 'cd /tmp; " + _INSTALL + "; python3 -m pip check; true'",
     ["pip install runs inside", "pip install not followed", "pip check runs inside", "pip check not followed"]),
    ('set -e; v="$(' + _INSTALL + ' && python3 -m pip check)"; echo "$v"',
     ["pip install runs inside", "pip install not followed", "no `pip check`"]),
    ("set -e; if set +e; then :; fi; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; { set +e; }; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; X=1 set +e; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("false && set -e; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e | true; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("trap 'exit 0' EXIT; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `trap", "pip check shares the RUN with `trap"]),
    ("exit 0; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `exit 0`", "pip check shares the RUN with `exit 0`"]),
    ('case "$a" in x) true ;; *) exit 1 ;; esac && ' + _INSTALL + " && python3 -m pip check", []),
    ("set -e; if false; then true; elif " + _INSTALL + "; then python3 -m pip check; fi",
     ["pip install is a condition", "pip check not followed"]),
    ("while " + _INSTALL + "; do break; done && python3 -m pip check", ["pip install is a condition"]),
    ("until " + _INSTALL + "; do sleep 1; done && python3 -m pip check", ["pip install is a condition"]),
    ("set -e; while false; do true; done; " + _INSTALL + " && python3 -m pip check", []),
    ("set -e; ( " + _INSTALL + "; python3 -m pip check ) || true", ["pip install not followed", "no `pip check`"]),
    ("( true; set -e ); " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("exec true; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `exec true`", "pip check shares the RUN with `exec true`"]),
    ("if [ -f /x ]; then exit 0; fi; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `then exit 0`", "pip check shares the RUN with `then exit 0`"]),
    ("echo $(" + _INSTALL + " && python3 -m pip check)", ["pip install runs inside", "no `pip check`"]),
    ("set -e; " + _INSTALL + '; [ -n "$NOPE" ] && python3 -m pip check; true', ["pip check not followed"]),
    (_INSTALL + " && python3 -m pip check \\\n\n || true", ["pip install not followed", "pip check not followed"]),
    (_INSTALL + " && python3 -m pip check \\\n   \n || true",
     ["pip install not followed", "pip check not followed"]),
    ('set -e; sh -c "cd /tmp; ' + _INSTALL + '; python3 -m pip check; true"',
     ["pip install runs inside", "pip install not followed", "pip check runs inside", "pip check not followed"]),
    ("set -e; set -u +e; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; eval 'set +e'; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ('set -e; set +"e"; ' + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ('set -e; "set" +e; ' + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; o=+e; set $o; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install shares the RUN with `set", "pip install not followed",
      "pip check shares the RUN with `set", "pip check not followed"]),
    ("set -e & true; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; { echo }; " + _INSTALL + "; python3 -m pip check; } || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; for f in a; do echo done; " + _INSTALL + "; python3 -m pip check; done || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; if true; then echo fi; " + _INSTALL + "; python3 -m pip check; fi || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; case a in a) echo esac; " + _INSTALL + "; python3 -m pip check;; esac || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; ( case a in a) true; " + _INSTALL + "; python3 -m pip check;; esac ) || true",
     ["pip install not followed", "pip check not followed"]),
    ('set -e; case "$a" in x) true;; esac; ' + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; if [ -f /x ]; then true; fi; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; for f in a b; do true; done; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; { cd /tmp; }; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; ( cd /tmp ); " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ('set -e; echo "${HOME}"; ' + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; echo ${HOME}; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("{ if " + _INSTALL + "; then python3 -m pip check; fi; }",
     ["pip install is a condition", "pip check not followed"]),
    ("\\exit 0; " + _INSTALL + " && python3 -m pip check", ["pip install shares the RUN", "pip check shares the RUN"]),
    ('"ex"it 0; ' + _INSTALL + " && python3 -m pip check", ["pip install shares the RUN", "pip check shares the RUN"]),
    ("eval 'exit 0'; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN", "pip check shares the RUN"]),
    ("command exit 0; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN", "pip check shares the RUN"]),
    ("return 0; " + _INSTALL + " && python3 -m pip check", ["pip install shares the RUN", "pip check shares the RUN"]),
    ("exit 256; " + _INSTALL + " && python3 -m pip check", ["pip install shares the RUN", "pip check shares the RUN"]),
    ('case "$a" in x)exit 0;; esac; ' + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN", "pip check shares the RUN"]),
    ("{ exit 0; }; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN", "pip check shares the RUN"]),
    ('set -e; echo \\"; sh -c "cd /tmp; ' + _INSTALL + '; python3 -m pip check; true"',
     ["pip install runs inside", "pip install not followed", "pip check runs inside", "pip check not followed"]),
    ('set -e; ( echo " ); ' + _INSTALL + '; python3 -m pip check; echo " ); true',
     ["pip install runs inside", "pip install not followed", "pip check runs inside", "pip check not followed"]),
    ('echo "the runner\'s venv" && ' + _INSTALL + " && python3 -m pip check", []),
    ("set -e; echo 'if {'; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ('set -e; echo "a \\" b"; ' + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    (_INSTALL + " && python3 -m pip check && true &", ["pip install not followed", "pip check not followed"]),
    ("set -e &true; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e &>/dev/null; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("{ " + _INSTALL + " && python3 -m pip check && true & }", ["pip install not followed", "pip check not followed"]),
    ("true & " + _INSTALL + " && python3 -m pip check", []),
    ("set -e; " + _INSTALL + "; true || (true && python3 -m pip check && true)", ["pip check runs in a compound"]),
    ("set -e; " + _INSTALL + "; f() (true && python3 -m pip check && true)",
     ["pip install shares the RUN with a function", "pip check shares the RUN with a function"]),
    ("set -e; " + _INSTALL + "; ! (true && python3 -m pip check && false)", ["pip check runs in a compound"]),
    ("true || (cd /tmp && " + _INSTALL + " && python3 -m pip check)",
     ["pip install runs in a compound", "no `pip check`"]),
    ("( " + _INSTALL + " && python3 -m pip check && true ) | cat",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; { true; }x 2>/dev/null || true; " + _INSTALL + "; python3 -m pip check; } || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; for f in a; do true; done=1; " + _INSTALL + "; python3 -m pip check; done || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; if true; then true; fi=1; " + _INSTALL + "; python3 -m pip check; fi || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; case a in a) true; esac=1; " + _INSTALL + "; python3 -m pip check;; esac || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; if true; then true; fi-x 2>/dev/null || true; " + _INSTALL + "; python3 -m pip check; fi || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; { true; }>/dev/null; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("e\\\nxit 0; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `exit 0`", "pip check shares the RUN with `exit 0`"]),
    ("set -e; set +\\\ne; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; ( ca\\\nse a in a) true; " + _INSTALL + "; python3 -m pip check;; esac ) || true",
     ["pip install not followed", "pip check not followed"]),
    (_INSTALL + " \\\n && python3 -m pip check", []),
    ("set -e; " + _INSTALL + "; echo \"it's `true`\"; sh -c 'echo \"; python3 -m pip check; true\"'",
     ["pip check runs inside", "pip check not followed"]),
    ("echo \"it's `uname -m`\" && " + _INSTALL + " && python3 -m pip check", []),
    ("set -e; " + _INSTALL + "; echo $'it\\'s'; sh -c 'cd /; python3 -m pip check; true'",
     ["pip check runs inside", "pip check not followed"]),
    ("exit>/dev/null; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `exit>/dev/null`", "pip check shares the RUN with `exit>/dev/null`"]),
    ("return</dev/null; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `return</dev/null`", "pip check shares the RUN with `return</dev/null`"]),
    (_INSTALL + " && python3 -m pip check && true\r|| true", ["pip install not followed", "pip check not followed"]),
    ("set() { :; }; set -e; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install shares the RUN with a function", "pip check shares the RUN with a function"]),
    (_INSTALL + " && ! ! python3 -m pip check", ["pip check is a condition"]),
    ("true&exit 0; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `exit 0`", "pip check shares the RUN with `exit 0`"]),
    ("true; exit>/dev/null; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `exit>/dev/null`", "pip check shares the RUN with `exit>/dev/null`"]),
    ("exec>/dev/null true; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `exec>", "pip check shares the RUN with `exec>"]),
    ("set -n; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `set -n`", "pip check shares the RUN with `set -n`"]),
    ("set -o noexec; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `set -o noexec`", "pip check shares the RUN with `set -o noexec`"]),
    ("set -eun; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install shares the RUN with `set -eun`", "pip check shares the RUN with `set -eun`"]),
    ("echo \"built `date`\" && " + _INSTALL + " && python3 -m pip check", []),
    ("echo \"built `date`; ok\" && " + _INSTALL + " && python3 -m pip check", []),
    ("true & set -e; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; true & " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("echo `date` && " + _INSTALL + " && python3 -m pip check", []),
    ("echo \"$(echo '`')\" && " + _INSTALL + " && python3 -m pip check", []),
    (_INSTALL + " && python3 -m pip check && echo \"a \\\" b\" & echo \"c\"",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; set `cat /tmp/o`; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install shares the RUN with `set", "pip install not followed",
      "pip check shares the RUN with `set", "pip check not followed"]),
    ("set -e; { true; }'' 2>/dev/null || true; " + _INSTALL + "; python3 -m pip check; } || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; for f in a; do true; done\"\"; " + _INSTALL + "; python3 -m pip check; done || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; if true; then true; fi\\x; " + _INSTALL + "; python3 -m pip check; fi || true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; \"python3\"() { true; }; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install shares the RUN with a function", "pip check shares the RUN with a function"]),
    ("set -e; eval 'alias python3=true'; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install shares the RUN with a function", "pip check shares the RUN with a function"]),
    ("echo \"alias x\" && " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with a function", "pip check shares the RUN with a function"]),
    (_INSTALL + " && python3 -m pip check && echo $'\\'' & echo 'x'",
     ["pip install runs inside", "pip check runs inside"]),
    (_INSTALL + " && python3 -m pip check \\ \t\n|| true", ["pip install not followed", "pip check not followed"]),
    (_INSTALL + " && python3 -m pip check | echo $'\\''",
     ["pip install runs inside", "pip check runs inside", "pip check not followed"]),
    ("true | ( " + _INSTALL + " && python3 -m pip check && true )", []),
    ("true & ( " + _INSTALL + " && python3 -m pip check && true )", []),
    ("true || (true); ( " + _INSTALL + " && python3 -m pip check && true )", []),
    ("set -e; x=n; set -$x; " + _INSTALL + " && python3 -m pip check",
     ["pip install shares the RUN with `set", "pip check shares the RUN with `set"]),
    ("set -e; set +[e]; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install shares the RUN with `set", "pip install not followed",
      "pip check shares the RUN with `set", "pip check not followed"]),
], ids=["module", "venv-pip", "bare", "option-first", "versioned-pip", "uv", "check-or-true", "install-then-semicolon",
        "check-then-semicolon", "check-piped", "check-another-pip", "no-no-deps", "extra-index", "behind-or",
        "set-e-semicolons", "set-e-or-true", "or-true-at-the-end", "semicolon-later", "set-e-not-last-of-its-list",
        "set-o-errexit", "errexit-switched-off", "install-as-a-condition", "check-negated", "pipe-in-a-later-pipeline",
        "index-prefix", "index-attached", "index-in-a-cluster", "editable-long", "editable-short", "valued-options",
        "set-plus-e", "set-double-dash", "check-before-install", "brace-group-or-true", "subshell-or-true",
        "sh-c-string", "in-a-substitution", "set-plus-e-as-a-condition", "set-plus-e-in-a-brace-group",
        "set-plus-e-behind-an-assignment", "set-e-behind-and", "set-e-piped", "trap-exit-0", "exit-0-first",
        "case-exit-1", "install-as-an-elif-condition", "install-as-a-while-condition", "install-as-an-until-condition",
        "after-a-while-loop", "subshell-opened-by-the-install", "set-e-in-a-subshell", "exec-first",
        "exit-0-in-a-then", "in-an-unquoted-substitution", "check-behind-a-guard", "blank-continuation-line",
        "blank-continuation-line-of-spaces", "sh-c-double-quoted", "set-plus-e-after-another-flag", "eval-set-plus-e",
        "set-quoted-plus-e", "quoted-set-plus-e", "set-of-a-variable", "set-e-in-the-background",
        "brace-word-closes-nothing", "done-word-closes-nothing", "fi-word-closes-nothing", "esac-word-closes-nothing",
        "case-pattern-closes-no-subshell", "after-a-closed-case", "after-a-closed-if", "after-a-closed-for",
        "after-a-closed-brace-group", "after-a-closed-subshell", "after-a-braced-expansion",
        "after-an-unquoted-braced-expansion",
        "condition-inside-a-brace-group", "backslash-exit", "exit-spelled-through-quotes", "eval-exit-0",
        "command-exit-0", "return-0", "exit-256", "exit-0-after-a-case-pattern", "exit-0-in-a-brace-group",
        "escaped-quote-before-an-sh-c-string", "quoted-paren-closes-no-subshell", "apostrophe-in-a-message",
        "keyword-and-brace-in-a-quoted-string",
        "escaped-quote-inside-a-message", "sent-to-the-background", "set-e-in-the-background-unspaced",
        "set-e-before-an-amp-redirect", "background-in-a-brace-group", "background-before-the-install",
        "check-in-a-subshell-behind-or", "check-in-a-function-body", "check-in-a-negated-subshell",
        "both-in-a-subshell-behind-or", "subshell-piped", "brace-closer-glued-to-a-word", "done-as-an-assignment",
        "fi-as-an-assignment", "esac-as-an-assignment", "fi-glued-to-a-dash-word", "brace-closer-then-a-redirect",
        "exit-split-by-a-continuation", "set-plus-e-split-by-a-continuation", "case-split-by-a-continuation",
        "continuation-after-a-space", "apostrophe-and-backquote-then-sh-c", "apostrophe-and-backquote-in-a-message",
        "ansi-c-quote-then-sh-c", "exit-with-a-redirect", "return-with-a-redirect", "carriage-return-inside-a-line",
        "set-as-a-function", "double-negation", "exit-after-a-background", "exit-redirect-after-a-command",
        "exec-with-a-redirect", "set-n", "set-o-noexec", "set-n-in-a-cluster", "backquotes-in-a-message",
        "backquotes-and-a-semicolon-in-a-message", "set-e-after-a-background", "after-a-background-under-errexit",
        "backquoted-date", "backquote-quoted-in-a-quoted-substitution", "background-after-an-escaped-quote",
        "set-of-a-backquoted-command", "brace-closer-then-a-quote", "done-closer-then-a-quote",
        "fi-closer-then-a-backslash", "function-named-through-quotes", "alias-through-eval", "alias-in-a-message",
        "ansi-c-quote-later-in-the-list", "continuation-after-trailing-blanks",
        "ansi-c-quote-after-a-pipe", "subshell-after-a-pipe",
        "subshell-after-a-background", "subshell-after-one-closed-behind-or", "set-of-a-variable-flag",
        "set-of-a-glob"])
def test_the_pip_check_tells_a_pinned_install_from_a_near_miss(run, expected):
    """The pip check flags an install that names a package (with an option before `install`, a versioned pip, `uv pip`,
    or an `-e`/`--editable` project), lacks `--no-deps`, names an index (in full, by an unambiguous prefix, attached to
    `-i`, or in a flag cluster), or runs behind `||`; a `pip check` that is missing or of another pip; and an install or
    check whose failure would not fail the RUN: a later `|| true` or `;` anywhere after it, a pipe right after it, a `;`
    under errexit when it is not the last command of its AND-OR list, a `;` after errexit is switched off (`set +e`,
    `set +o errexit`; `set -- -e` sets no option), an `if` condition, a `!`, and a `pip check` that runs before the
    install; and, from fold 3's read: a `;` under `set -e` inside a brace group or subshell that a `|| true` follows
    (errexit is ignored there), an install inside `sh -c '…'` or a `$(…)`, errexit switched off by a `set +e` that is a
    condition, in a brace group or behind an assignment, a `set -e` that never takes effect (behind `&&`, piped), a
    `trap` or an `exit 0` in the RUN, and an install that is an `elif`, `while` or `until` condition. It passes the two
    shapes the images use, `;` under `set -e` or `set -o errexit` right after each, a pipe in a later pipeline,
    `--target`, `-c` and an attached `-r<file>`, a `case` whose fallback is `exit 1`, and an install after a `while`
    loop has closed. Fold 4's mutants added: a subshell the install opens under `|| true`, a `set -e` inside a subshell
    (it does not reach the RUN's shell), an `exec` before the install (it never runs), an `exit 0` in a `then`, and an
    install in an unquoted `$(…)`. Fold 4's read added: a check behind a guard under errexit (`X; [ -n "$NOPE" ] && pip
    check;`: the guard skips it); a `|| true` after a blank continuation line, of nothing or of spaces (BuildKit joins
    across it); a double-quoted `sh -c "…"`; errexit switched off by a `+e` after another flag, `eval 'set +e'`, `set
    +"e"`, `"set" +e` or `set $o`, and a `set -e` sent to the background; a `}`, `done`, `fi` or `esac` written as a
    word (`echo done` closes nothing) and a case pattern's `)` (it closes no subshell); a condition inside a brace
    group; an `exit` written `\\exit 0`, `"ex"it 0`, `eval 'exit 0'`, `command exit 0`, after a case pattern or in a
    brace group, `return 0` and `exit 256` (status 0); an escaped quote before an `sh -c "…"` string; and a quote that
    swallows a subshell's `)`. It passes a `;` check after a closed `case`, `if`, `for`, brace group, subshell or `${…}`
    (quoted or not), an apostrophe or an escaped quote in a double-quoted message, and a keyword and a brace inside a
    quoted string. Fold 5's reads added: a list a lone `&` sends to the background (at the end, in a brace group, a `set
    -e &true` or `set -e &>/dev/null`), and it passes one before the install, before a `set -e` or before an install
    under errexit (the shell goes on in the foreground); a check in a compound behind `||` or `!`, or piped, or in a
    function body, and a function named `set`; a closer glued to a word (`}x`, `done=1`, `fi=1`, `esac=1`, `fi-x`: no
    closer to the shell), and it passes `}>/dev/null`; a word a continuation splits (`e\\` and a newline then `xit 0`,
    `set +\\` … `e`, `ca\\` … `se`: BuildKit joins with no space), and it passes a continuation after a space; an `sh -c
    '…'` after a double-quoted message holding a backquote and an apostrophe, or after an ANSI-C `$'…'`, and it passes
    such a message alone and a backquoted `date` in a message (a `;` in it too); an `exit`, `return` or `exec` with a
    redirect (after `&` or `;` too), a `! !`, and a `set -n`, `set -o noexec` or `set -eun` (the shell reads the rest
    and runs none of it); and a carriage return inside a line (BuildKit splits on LF only, so the `|| true` after it is
    read). Fold 6's mutants added: a list sent to the background after an escaped quote in a message, and it passes a
    backquoted `date` outside quotes and a quoted backquote in a `"$(…)"`. Fold 6's reads added: a `set` given a
    backquoted command (it can print `+e`); a `}`, `done` or `fi` a quote or a backslash touches (`}''`: sh reads no
    quoted word as a reserved word, so nothing closes); a function named through quotes and an alias defined through
    `eval` (and the word `alias` in a message, sound, refused); an ANSI-C `$'…'` later in the check's own list (dash and
    bash read it differently, so where the list ends is unread), after a pipe too; and a `|| true` after a continuation
    with blanks after its backslash (BuildKit continues it). It passes a subshell after a pipe or a lone `&`, whose
    status is the RUN's, and one after a subshell closed behind `||` (a closed one skips nothing that follows it; fold
    7's mutants: O3s). Fold 7's read A added: a `set` given a variable, a backquoted command or a glob (`set $o`, `set
    -$x`, `` set `cat o` ``, `set +[e]`) shares the RUN as a `set -n` would, since it can be one, and switches errexit
    off. Guards a pin check that a common install shape walks past, and a `pip check` that cannot fail the build — "make
    the cleanup tolerant" with a trailing `|| true` was one (T29's read, S2 and S3: P1, P2, P5, P8; its fold-2 read, S1,
    N3, N4; its fold-3 read, S2, N1, N2, N6: Z1, Z5, Z6, Z10, V2, V3; fold 4's mutants: S4, O7, O8, E4, E5; its read,
    S1, S2, S4, S5, N1, N2, N6: G-a, C-g, O-a to O-d, O-i, E-a to E-d, N-a to N-j, OR-a, OR-b, Z20, Z24, Z25, Z32, Z33,
    O10 to O14, O17, S5, C4; fold 5's read A, S1 to S4, S6, N1 to N4, N7: A1, A3 to A6, B1, B3, B4, B6, B8, C1 to C4,
    C6, C7, D1, D2, D4, D8, E1, E3, F1, G1, G2, I1, K1, K4; its read B, N1, N3, N8: ZB2, ZB3, ZB4, QX1, QX2; fold 6's
    mutants: Q7, QX1, QX2; its read A, S3, S4, S5, S7; its read B, S3, N1, N4: R1, SK1, SK2, BK1; fold 7's read A,
    S2)."""
    faults = pip_install_faults("RUN " + run + "\n")
    assert _found(faults, expected), faults


@pytest.mark.parametrize("text,expected", [
    ("", []),
    ("ENV PIP_INDEX_URL=https://x.test\n", ["environment or config: PIP_INDEX_URL"]),
    ("ARG PIP_EXTRA_INDEX_URL=https://x.test\n", ["environment or config: PIP_EXTRA_INDEX_URL"]),
    ("COPY docker/x/pip.conf /etc/pip.conf\n", ["environment or config: docker/x/pip.conf"]),
    ("RUN python3 -m pip config set global.index-url https://x.test\n", ["environment or config: python3 -m pip"]),
], ids=["none", "env-index", "arg-extra-index", "pip-conf", "pip-config-set"])
def test_the_pip_check_reads_pips_environment_and_config(text, expected):
    """An index named in pip's environment (`ENV`/`ARG PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, …), a copied
    `pip.conf`, or a `pip config set` is flagged in a Dockerfile that runs a `pip install`: pip reads each before its
    command line, so the install's own words name no index while one is used. Guards an index the per-command read
    cannot see (T29's fold-2 read, N4: `ENV PIP_INDEX_URL` passed)."""
    faults = pip_install_faults(text + "RUN " + _INSTALL + " && python3 -m pip check\n")
    assert _found(faults, expected), faults


def test_the_requirement_files_are_exactly_the_ones_the_images_install():
    """The requirements files under docker/ are exactly the files a Dockerfile pip-installs. Dependabot's pip entries
    and the pip-audit step (tests/unit/test_ci_workflows.py) are held to the same set, so this keeps them honest
    about what the images run. Guards a file that no image installs (audited, never shipped) and an install from
    a file that is not where the others look."""
    on_disk = {os.path.relpath(os.path.join(d, n), ROOT).replace(os.sep, "/")
               for d, _, names in os.walk(os.path.join(ROOT, "docker"))
               for n in names if re.fullmatch(r"requirements.*\.txt", n)}
    assert set(image_requirement_files()) == on_disk


@pytest.mark.parametrize("text,lines,exact", [
    ("Flask==3.1.3\n", ["Flask==3.1.3"], [True]),
    ("# header\nflask==3.1.3  # the GUI\n\n", ["flask==3.1.3"], [True]),
    ("Flask==3.1.3 \\\n    --hash=sha256:" + "a" * 64 + "\n", ["Flask==3.1.3 --hash=sha256:" + "a" * 64], [True]),
    ("flask>=3\n", ["flask>=3"], [False]),
    ("--extra-index-url https://x.test\nflask==3.1.3\n", ["--extra-index-url https://x.test", "flask==3.1.3"],
     [False, True]),
    ("-e git+https://x.test/y.git#egg=y\n", ["-e git+https://x.test/y.git#egg=y"], [False]),
], ids=["exact", "comments", "hash-continued", "floor", "index-line", "editable"])
def test_the_requirement_reader_tells_an_exact_pin_from_a_near_miss(text, lines, exact):
    """A requirements file reads as its requirements, comments dropped and a backslash continuation joined, and an
    exact pin with or without `--hash=sha256:` passes while a floor, an index line and an editable install do not.
    Guards the "next rung" docker/semaphore-runner/requirements.txt names (hash pins) failing as unpinned, and a
    check that reads a hash or an index line as a pin (T29's read, N4: Q1)."""
    assert requirement_lines(text) == lines
    assert [bool(_EXACT.match(ln)) for ln in lines] == exact


def test_image_requirements_cover_the_declared_runtime_floors():
    """Every package gui/requirements.txt and api/requirements.txt name ships in the control image: in
    docker/semaphore-runner/requirements.txt, or from the base and imported by the runner Dockerfile's build-time
    check. A floor names no extra (`uvicorn[standard]`): the image installs `--no-deps`, so an extra's packages
    would never ship. Guards the image and the declared runtime deps drifting apart (they had: PyYAML, the uvicorn
    extra)."""
    closure = {_dist(ln) for ln in requirement_lines(_read("docker/semaphore-runner/requirements.txt"))}
    runner = _read("docker/semaphore-runner/Dockerfile")
    imported = set()
    for keyword, args in instructions(runner):
        for m in re.finditer(r"-c\s+\"import ([\w, ]+)\"", args) if keyword == "RUN" else ():
            imported |= {name.strip() for name in m.group(1).split(",")}
    for floor in ("gui/requirements.txt", "api/requirements.txt"):
        for line in requirement_lines(_read(floor)):
            dist = _dist(line)
            assert "[" not in line, "%s names an extra the image does not ship: %r" % (floor, line)
            assert dist in closure or _FROM_THE_BASE.get(dist) in imported, "%s: %s ships nowhere" % (floor, dist)
    assert set(_FROM_THE_BASE.values()) <= imported, "the base packages must be import-checked at build"
    assert not closure & set(_FROM_THE_BASE), "a base package in the closure would re-install it over the base's"


def test_the_installer_ansible_core_is_inside_the_host_range():
    """The installer's `ansible-core==` pin is inside the `ansible-core>=…,<…` range scripts/install-prereqs.sh puts on
    the host. The installer Dockerfile says it is, and the refresh procedure re-derives the file by a resolve that,
    unconstrained, lands on the next series (2.19.x on the installer's Python 3.11; T29's read, N2). Guards the two
    install paths quietly running different ansible-core series."""
    host = re.search(r'"ansible-core>=([\d.]+),<([\d.]+)"', _read("scripts/install-prereqs.sh"))
    pins = [ln for ln in requirement_lines(_read("docker/installer/requirements.txt")) if _dist(ln) == "ansible-core"]
    assert host and len(pins) == 1, (host, pins)
    assert _version(host.group(1)) <= _version(pins[0].split("==", 1)[1]) < _version(host.group(2)), pins


def test_apk_packages_carry_a_version_constraint():
    """Each package an `apk add` installs carries `~=` (a minor, its patch revision free) or `=`, and the add stops
    the build; no `apk upgrade` runs. Guards a bare package name coming back, which takes whatever the Alpine branch
    serves that day, and an upgrade moving every base package past the pinned digest."""
    seen = 0
    for rel in dockerfiles():
        text = _read(rel)
        assert apk_faults(text) == [], rel
        seen += sum(1 for commands in _runs(text) for c, _, _ in commands if _APK.search(c))
    assert seen, "the runner installs apk packages"


@pytest.mark.parametrize("run,expected", [
    ('apk add --no-cache "age~=1.2" "sops=3.9.1-r5" && age --version', []),
    ("apk --no-cache add jq", ["no version constraint"]),
    ("apk upgrade --no-cache", ["apk upgrade"]),
    ("if true; then apk add jq; fi", ["no version constraint", "apk add not followed"]),
    ('apk add --no-cache "age~=1.2"; age --version', ["apk add not followed"]),
    ('apk add --no-cache "age~=1.2" || true', ["apk add not followed"]),
    ('apk add --no-cache "age~=1.2" jq && true', ["no version constraint"]),
    ('apk add --no-cache "age~=1.2" && age --version || true', ["apk add not followed"]),
    ('apk add --no-cache --virtual .build-deps "build-base~=0.5" && true', []),
    ('apk add --no-cache -t .build-deps "build-base~=0.5" && true', []),
    ("apk add --no-cache --virtual .build-deps build-base && true", ["no version constraint"]),
], ids=["pinned", "option-first", "upgrade", "in-an-if", "then-semicolon", "or-true", "one-unpinned",
        "or-true-later", "virtual-group", "virtual-group-short", "virtual-group-unpinned"])
def test_the_apk_check_tells_a_pinned_add_from_a_near_miss(run, expected):
    """The apk check flags a package with no constraint (with an option before `add`, inside an `if`, one of
    several, or after a `--virtual` group name), an add that cannot fail the build (a `;` or `|| true` right after it
    or later in the RUN), and any `apk upgrade`; it passes `~=` and `=` constraints, and does not read the name
    `--virtual`/`-t` gives the group as a package. Guards a check that only reads `apk add` written first (T29's
    read, S3: A1, A2, A3; its fold-2 read, S1), and one that refuses Alpine's build-deps idiom, the likeliest next apk
    edit (its fold-3 read, N7: F1, F2)."""
    faults = apk_faults("RUN " + run + "\n")
    assert _found(faults, expected), faults


def test_downloads_in_an_image_build_are_checksum_verified():
    """Every `curl` / `wget` in a Dockerfile RUN writes a named file that a later `sha256sum -c -` in the same RUN
    checks against a digest from the tree (an ARG or ENV holding 64 hex), and both stop the build; no ADD fetches a
    URL without `--checksum=sha256:`. Guards a swapped release asset (or apt signing key) baked into the image that
    holds the docker socket."""
    seen = 0
    for rel in dockerfiles():
        text = _read(rel)
        assert download_faults(text) == [], rel
        seen += sum(1 for commands in _runs(text) for c, _, _ in commands
                    if _DOWNLOAD.search(c) and not _APT_INSTALL.search(c))
    assert seen >= 2, "the installer fetches the apt key and the sops binary"


# The suite's files whose rows name digest ARGs as data (the build-arg walk does not read them).
_NAMES_DIGEST_ARGS_AS_DATA = {"tests/unit/test_image_pins.py"}


def build_arg_faults(root=ROOT):
    """["<file>: <name>"] for each shipped text file that names a digest ARG (an ARG of a Dockerfile under docker/
    holding 64 hex), other than Markdown, this test file (its rows name such ARGs as data) and each Dockerfile that
    declares it with its digest. A `--build-arg`, a workflow's `build-args:` or a compose `build.args:` must name the
    ARG to override it, so a name seen nowhere else is a digest no build replaces."""
    declared = {}
    for rel in dockerfiles(root):
        for keyword, args in instructions(_read(rel, root)):
            if keyword == "ARG":
                for name in tree_digests("ARG " + args):
                    declared.setdefault(name, set()).add(rel)
    faults = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOT_SHIPPED]
        for n in names:
            rel = os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")
            if rel.endswith(".md") or rel in _NAMES_DIGEST_ARGS_AS_DATA:
                continue
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            faults += ["%s: %s" % (rel, name) for name, homes in sorted(declared.items())
                       if rel not in homes and re.search(r"\b%s\b" % name, text)]
    return sorted(faults)


def test_no_build_passes_a_digest_arg():
    """No shipped file but a Dockerfile that declares it (and Markdown, and this file's rows) names a digest ARG, so no
    `--build-arg`, workflow `build-args:` or compose `build.args:` replaces the checksum the tree holds. Guards a build
    arg that swaps the digest a download is checked against, which the Dockerfile reader cannot see (T29's fold-3
    read, N4)."""
    assert build_arg_faults() == []


def test_the_build_arg_walk_finds_a_digest_arg_named_elsewhere(tmp_path):
    """build_arg_faults names a compose `build.args`, a workflow `build-args`, a script `--build-arg`, a helper under
    tests/ that passes one and another test file that names one, and passes each Dockerfile that declares the ARG
    with its digest (two images may both download one tool), a Markdown file, this file's rows and an ARG that holds
    no digest. Guards a walk that reads nothing, which would pass the tree check above whatever it held (T29's fold-3
    read, N4); one that reads this file's rows, which name such ARGs (fold 4's P6: a new digest ARG that a row also
    names failed the tree check); one that skips all of tests/, where a build helper could pass one unseen (fold 4's
    read, N7); and one that keeps a single Dockerfile per name, which fails a second image that declares the same
    digest ARG (N6)."""
    files = {"docker/x/Dockerfile": "FROM scratch\nARG TOOL_SHA256=" + "a" * 64 + "\nARG TOOL_VERSION=1.2\n",
             "docker/y/Dockerfile": "FROM scratch\nARG TOOL_SHA256=" + "a" * 64 + "\n",
             "docker/services/x.yaml": "services:\n  x:\n    build:\n      args:\n        TOOL_SHA256: abc\n",
             ".github/workflows/b.yml": "jobs: {b: {steps: [{with: {build-args: 'TOOL_SHA256=abc'}}]}}\n",
             "scripts/b.sh": "docker build --build-arg TOOL_SHA256=abc docker/x\n",
             "scripts/v.sh": "docker build --build-arg TOOL_VERSION=1.3 docker/x\n",
             "docker/README.md": "Bump TOOL_SHA256 with the release.\n",
             "tests/validate.sh": "docker build --build-arg TOOL_SHA256=abc docker/x\n",
             "tests/unit/test_x.py": '_TREE = "ARG TOOL_SHA256=" + "a" * 64\n',
             "tests/unit/test_image_pins.py": '_TREE = "ARG TOOL_SHA256=" + "a" * 64\n'}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    assert build_arg_faults(str(tmp_path)) == [".github/workflows/b.yml: TOOL_SHA256",
                                               "docker/services/x.yaml: TOOL_SHA256", "scripts/b.sh: TOOL_SHA256",
                                               "tests/unit/test_x.py: TOOL_SHA256", "tests/validate.sh: TOOL_SHA256"]


_TREE = "ARG TOOL_SHA256=" + "a" * 64 + "\n"
_SUM = 'echo "$TOOL_SHA256  /usr/local/bin/tool" | sha256sum -c -'
_GET = "curl -fsSL https://x.test/tool -o /usr/local/bin/tool"
_SIDECAR = "curl -fsSL https://x.test/tool.sha256 -o /tmp/tool.sha256"


@pytest.mark.parametrize("text,expected", [
    ("RUN " + _GET + " && " + _SUM + " && chmod 0755 /usr/local/bin/tool", []),
    ("RUN wget -qO /usr/local/bin/tool https://x.test/tool && " + _SUM, []),
    ("RUN apt-get install -y --no-install-recommends curl ca-certificates", []),
    ("ADD --checksum=sha256:" + "a" * 64 + " https://x.test/tool /usr/local/bin/tool", []),
    ("ADD https://x.test/tool /usr/local/bin/tool", ["ADD from a URL"]),
    ("RUN /usr/bin/curl -fsSL https://x.test/tool -o /usr/local/bin/tool", ["no later sha256sum"]),
    ("RUN curl -fsSL https://x.test/i.sh | sh && " + _SUM, ["a download not followed", "no named file"]),
    ("RUN " + _GET + " && " + _SUM + " || true", ["a download not followed", "sha256sum -c not followed"]),
    ("RUN if true; then wget -O /usr/local/bin/tool https://x.test/tool; fi", ["a download not followed",
                                                                                "no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /tmp/other" | sha256sum -c -', ["no later sha256sum"]),
    ("RUN curl -fsSLO https://x.test/tool && sha256sum -c sums.txt", ["no named file"]),
    ('RUN v="$(curl -fsSL https://x.test/version)" && true', ["no named file"]),
    ("RUN " + _SUM + " && " + _GET, ["no later sha256sum"]),
    ("RUN " + _GET + " && sha256sum /usr/local/bin/tool", ["no later sha256sum"]),
    ("RUN " + _GET + " && " + _SUM + " && chmod 0755 /usr/local/bin/tool || true",
     ["a download not followed", "sha256sum -c not followed"]),
    ("RUN " + _GET + " && " + _SUM + " && chmod 0755 /usr/local/bin/tool; rm -rf /tmp/k",
     ["a download not followed", "sha256sum -c not followed"]),
    ("RUN set -e; " + _GET + "; if " + _SUM + "; then chmod 0755 /usr/local/bin/tool; fi",
     ["no later sha256sum", "sha256sum -c is a condition"]),
    ("RUN " + _GET + " && " + _SUM + " && tool --version | head -1", []),
    ("RUN " + _GET + " && " + _SIDECAR + " && sha256sum -c /tmp/tool.sha256",
     ["no later sha256sum", "no later sha256sum"]),
    ('RUN ' + _GET + ' && ' + _SIDECAR + ' && echo "$(cat /tmp/tool.sha256)  /usr/local/bin/tool" | sha256sum -c -',
     ["no later sha256sum", "no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool2" | sha256sum -c -', ["no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | sha256sum -c --ignore-missing -',
     ["no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$NOT_IN_THE_TREE  /usr/local/bin/tool" | sha256sum -c -', ["no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "' + "b" * 64 + '  /usr/local/bin/tool" | sha256sum -c -', []),
    ('RUN case "$a" in x) sha="$TOOL_SHA256" ;; *) exit 1 ;; esac && ' + _GET
     + ' && echo "$sha  /usr/local/bin/tool" | sha256sum -c -', []),
    ('RUN sha="$TOOL_SHA256" && sha="$(cat /tmp/x)" && ' + _GET
     + ' && echo "$sha  /usr/local/bin/tool" | sha256sum -c -', ["no later sha256sum"]),
    ("ENV TOOL2_SHA256=" + "c" * 64 + '\nRUN ' + _GET + ' && echo "${TOOL2_SHA256}  /usr/local/bin/tool" '
     '| sha256sum -c -', []),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" > /tmp/s && sha256sum -c /tmp/s',
     ["no later sha256sum"]),
    ("RUN wget --output-document=/usr/local/bin/tool https://x.test/tool && " + _SUM, []),
    ("RUN " + _GET + " || " + _SUM, ["a download not followed", "sha256sum -c runs only when"]),
    ("RUN set -e; " + _GET + "; if true; then if " + _SUM + "; then chmod 0755 /usr/local/bin/tool; fi; fi",
     ["no later sha256sum", "sha256sum -c is a condition"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | sha256sum -c /tmp/tool.sha256',
     ["no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | sha256sum -c -</tmp/tool.sha256',
     ["no later sha256sum"]),
    ("ENV TOOL2_SHA256 " + "c" * 64 + '\nRUN ' + _GET + ' && echo "$TOOL2_SHA256  /usr/local/bin/tool" '
     '| sha256sum -c -', []),
    ('RUN TOOL_SHA256="$(cat /tmp/x)" && ' + _GET + ' && ' + _SUM, ["no later sha256sum"]),
    ("RUN set -e; if true; then " + _GET + "; " + _SUM + "; fi || true",
     ["a download not followed", "sha256sum -c not followed"]),
    ('RUN echo "$(' + _GET + " && " + _SUM + ')" && chmod 0755 /usr/local/bin/tool',
     ["a download runs inside", "sha256sum -c runs inside"]),
    ('RUN ' + _GET + ' && ' + _SIDECAR + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | cat /tmp/tool.sha256 '
     '| sha256sum -c -', ["no later sha256sum", "no later sha256sum"]),
    ("RUN " + _GET + " && printf '%s  %s\\n' \"$TOOL_SHA256\" /tmp/b /usr/local/bin/tool | sha256sum -c -",
     ["no later sha256sum"]),
    ("RUN " + _GET + " && ( ! " + _SUM + " )", ["no later sha256sum", "sha256sum -c is a condition"]),
    ('RUN f=/tmp/a && curl -fsSL https://x.test/tool -o "$f" && f=/tmp/b && echo "$TOOL_SHA256  $f" '
     '| sha256sum -c -', ["named through a variable"]),
    ("RUN read TOOL_SHA256 _ < /etc/hostname; " + _GET + " && " + _SUM, ["no later sha256sum"]),
    ("ENV TOOL2_SHA256=" + "c" * 64 + "\nARG X\nENV TOOL2_SHA256=$X\nRUN " + _GET
     + ' && echo "$TOOL2_SHA256  /usr/local/bin/tool" | sha256sum -c -', ["no later sha256sum"]),
    ("RUN trap 'exit 0' EXIT; " + _GET + " && " + _SUM,
     ["a download shares the RUN with `trap", "sha256sum -c shares the RUN with `trap"]),
    ("RUN echo `" + _GET + " && " + _SUM + "` && chmod 0755 /usr/local/bin/tool",
     ["a download runs inside", "sha256sum -c runs inside"]),
    ("RUN " + _GET + ' && printf "$TOOL_SHA256  /usr/local/bin/tool" | sha256sum -c -', ["no later sha256sum"]),
    ("RUN " + _GET + ' && echo "$TOOL_SHA256" /usr/local/bin/tool /tmp/b | sha256sum -c -', ["no later sha256sum"]),
    ("RUN " + _GET + ' && echo "$TOOL_SHA256" | /usr/local/bin/tool | sha256sum -c -', ["no later sha256sum"]),
    ("ARG TOOL_SHA256\nRUN " + _GET + " && " + _SUM, ["no later sha256sum"]),
    ("RUN set -e; " + _GET + "; false || false && " + _SUM + "; true", ["sha256sum -c not followed"]),
    ("ARG TOOL_SHA256_ARM64=" + "b" * 64 + "\nRUN set -e; " + _GET + '; [ "$TARGETARCH" = amd64 ] && ' + _SUM
     + '; [ "$TARGETARCH" = arm64 ] && echo "$TOOL_SHA256_ARM64  /usr/local/bin/tool" | sha256sum -c -; chmod 0755 '
     "/usr/local/bin/tool", ["sha256sum -c not followed", "sha256sum -c not followed"]),
    ("RUN for TOOL_SHA256 in x; do :; done; " + _GET + " && " + _SUM, ["no later sha256sum"]),
    ("RUN unset TOOL_SHA256; " + _GET + " && " + _SUM, ["no later sha256sum"]),
    ('RUN getopts "ab:" TOOL_SHA256; ' + _GET + " && " + _SUM, ["no later sha256sum"]),
    ("RUN " + _GET + ' && export "TOOL_SHA256=$(sha256sum /usr/local/bin/tool | cut -c1-64)" && ' + _SUM,
     ["no later sha256sum"]),
    ("RUN " + _GET + ' && sha256sum /usr/local/bin/tool > /tmp/s && read "TOOL_SHA256" _ < /tmp/s && ' + _SUM,
     ["no later sha256sum"]),
    ("RUN " + _GET + ' && echo "$TOOL_SHA256 */usr/local/bin/tool" | sha256sum -c -', []),
    ("RUN curl -fsSL https://x.test/tool -o /usr/local/bin/tool https://x.test/lib -o /usr/local/lib/x.so && "
     + _SUM, ["more than one URL"]),
    ("RUN curl -fsSL -o /usr/local/bin/tool https://x.test/tool -O https://x.test/lib.so && " + _SUM,
     ["more than one URL"]),
    ("RUN curl -fsSL -o /usr/local/bin/tool https://x.test/tool -O x.test/lib.so && " + _SUM, ["more than one URL"]),
    ("RUN curl -fsSL -o /usr/local/bin/tool https://x.test/tool https://x.test/lib > /usr/local/lib/x.so && " + _SUM,
     ["more than one URL"]),
    ("RUN set -e; if true; then echo fi; " + _GET + "; " + _SUM + "; fi || true",
     ["a download not followed", "sha256sum -c not followed"]),
    ("RUN " + _GET + " && " + _SUM + " && chmod 0755 /usr/local/bin/tool &",
     ["a download not followed", "sha256sum -c not followed"]),
    ("RUN set -e; " + _GET + "; chmod 0755 /usr/local/bin/tool; true || (true && " + _SUM + " && true)",
     ["sha256sum -c runs in a compound"]),
    ("RUN set -e; " + _GET + "; ! (true && " + _SUM + " && false)", ["sha256sum -c runs in a compound"]),
    ("RUN ( " + _GET + " && " + _SUM + " && chmod 0755 /usr/local/bin/tool ) | cat",
     ["a download not followed", "sha256sum -c not followed"]),
    ("RUN set -e; for f in a; do true; done=1; " + _GET + "; " + _SUM + "; done || true",
     ["a download not followed", "sha256sum -c not followed"]),
    ("RUN echo $\\\n(" + _GET + " && " + _SUM + " && true)", ["a download runs inside", "sha256sum -c runs inside"]),
    ("RUN true\\\\\nADD https://x.test/tool /usr/local/bin/tool", ["ADD from a URL"]),
    ("RUN set -e; " + _GET + "; echo \"it's `true`\"; sh -c 'echo \"; " + _SUM + "; true\"'",
     ["sha256sum -c runs inside", "sha256sum -c not followed"]),
    ("RUN sha256sum() { cat >/dev/null; }; " + _GET + " && " + _SUM + " && chmod 0755 /usr/local/bin/tool",
     ["a download shares the RUN with a function", "sha256sum -c shares the RUN with a function"]),
    ("RUN curl -fsSL --output-dir /usr/local/bin -o tool https://x.test/tool && echo \"$TOOL_SHA256  tool\" | "
     "sha256sum -c - && chmod 0755 /usr/local/bin/tool", ["more than one URL"]),
    ("RUN " + _GET + " && (TOOL_SHA256=$(sha256sum /usr/local/bin/tool | cut -c1-64) && " + _SUM + ")",
     ["no later sha256sum"]),
    ("RUN " + _GET + " && true&TOOL_SHA256=$(sha256sum /usr/local/bin/tool | cut -c1-64) && " + _SUM,
     ["a download not followed", "no later sha256sum"]),
    ("RUN " + _GET + " && sha256sum /usr/local/bin/tool > /tmp/s && (read TOOL_SHA256 _ < /tmp/s && " + _SUM + ")",
     ["no later sha256sum"]),
    ("RUN " + _GET + " && export TOOL_'SHA256'=$(sha256sum /usr/local/bin/tool | cut -c1-64) && " + _SUM,
     ["no later sha256sum"]),
    ("RUN " + _GET + ' && case "$a" in x)TOOL_SHA256=$(sha256sum /usr/local/bin/tool | cut -c1-64);; esac && ' + _SUM,
     ["a download not followed", "no later sha256sum"]),
    ("RUN curl -fsSL -o /usr/local/bin/tool --remote-name-all https://x.test/tool && " + _SUM, ["more than one URL"]),
    ("RUN wget -q -O /usr/local/bin/tool -O /tmp/x https://x.test/tool && " + _SUM, ["more than one URL"]),
    ("RUN " + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | echo sha256sum -c -', ["no later sha256sum"]),
    ("RUN " + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | : sha256sum -c -', ["no later sha256sum"]),
    ("RUN sha256sum () { cat >/dev/null; }; " + _GET + " && " + _SUM,
     ["a download shares the RUN with a function", "sha256sum -c shares the RUN with a function"]),
    ('RUN curl -fsSL "https://x.test/tool?a=1&b=2" -o /usr/local/bin/tool && ' + _SUM, []),
    ("RUN true\nADD\thttps://x.test/tool /usr/local/bin/tool", ["ADD from a URL"]),
    ("RUN " + _GET + " && " + _SUM + " && echo $'\\'' & echo 'x'",
     ["a download runs inside", "sha256sum -c runs inside"]),
    ("RUN set -e; x=n; set -$x; " + _GET + " && " + _SUM,
     ["a download shares the RUN with `set", "sha256sum -c shares the RUN with `set"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | ./sha256sum -c -', ["no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | /tmp/sha256sum -c -', ["no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | /usr/bin/sha256sum -c -', []),
    ("RUN set -e; set +[e]; " + _GET + " && " + _SUM,
     ["a download shares the RUN with `set", "sha256sum -c shares the RUN with `set"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | /bin/sha256sum -c -', []),
], ids=["curl-checked", "wget-checked", "apt-package", "add-checksum", "add-url", "path-prefix-unchecked",
        "piped-to-sh", "check-or-true", "wget-in-an-if", "checks-another-file", "remote-name", "in-a-substitution",
        "checked-before", "summed-not-checked", "or-true-at-the-end", "semicolon-later", "check-as-a-condition",
        "pipe-in-a-later-pipeline", "sidecar-checksum-file", "sidecar-read-into-the-line", "name-prefix",
        "ignore-missing", "digest-not-from-the-tree", "literal-digest", "through-a-shell-variable",
        "variable-also-set-from-a-file", "env-digest", "check-reads-a-file", "wget-long-output",
        "check-piped-behind-or", "check-as-a-nested-condition", "file-operand-after-a-pipe", "stdin-redirected",
        "legacy-env-digest", "tree-name-reassigned", "if-body-or-true", "download-in-a-substitution",
        "sidecar-through-cat", "printf-line", "negated-in-a-subshell", "target-through-a-variable",
        "digest-set-by-read", "env-digest-set-again", "trap-exit-0", "download-in-backquotes", "printf-three-words",
        "checked-line-extra-word", "line-from-the-download", "digest-arg-redeclared-without-a-value",
        "check-behind-an-or-and", "checks-behind-an-arch-guard", "digest-set-by-for", "digest-unset",
        "digest-set-by-getopts", "digest-set-by-a-quoted-export", "digest-set-by-read-of-a-quoted-name",
        "binary-mode-line", "two-urls-two-outputs", "second-url-to-its-remote-name",
        "schemeless-url-to-its-remote-name", "second-url-to-standard-output", "fi-word-closes-no-if",
        "sent-to-the-background", "check-in-a-subshell-behind-or", "check-in-a-negated-subshell-under-errexit",
        "subshell-piped", "done-as-an-assignment", "substitution-split-by-a-continuation",
        "add-after-a-double-backslash", "sh-c-after-an-apostrophe-and-a-backquote", "sha256sum-function",
        "output-dir", "digest-set-in-a-subshell", "digest-set-after-a-background", "digest-read-in-a-subshell",
        "digest-name-partly-quoted", "digest-set-after-a-case-pattern", "remote-name-all", "wget-two-outputs",
        "sha256sum-an-echo-argument", "sha256sum-a-colon-argument", "sha256sum-function-spaced",
        "ampersand-in-a-quoted-url", "add-after-a-tab", "ansi-c-quote-later-in-the-list",
        "set-of-a-variable-flag-before-a-download", "sha256sum-from-the-working-directory", "sha256sum-from-tmp",
        "sha256sum-by-its-path", "set-of-a-glob-before-a-download", "sha256sum-by-its-bin-path"])
def test_the_download_check_tells_a_checked_download_from_a_near_miss(text, expected):
    """The download check flags an ADD from a URL with no checksum, a download behind a path prefix or inside an `if`
    that nothing checks, a download piped to a shell, one to curl's remote name or captured by `$(…)`, a check of
    another file (a longer name with the file's as its prefix too) or one that runs before the download, a `sha256sum`
    without `-c` (it prints a sum and checks nothing), a download or check whose failure would not fail the RUN (a later
    `|| true` or `;`, an `if` condition, a pipeline into the check that runs only when the download fails, a condition
    inside a `then`), and a check whose digest is not from the tree: a checksum file downloaded beside the file (read by
    `-c <file>` or into the line), a name no ARG or ENV gives 64 hex or one the RUN sets again, a shell variable also
    set from a file, `--ignore-missing`, a check written to a file, or one that reads a file or a redirect instead of
    the pipe. It passes a checked curl or wget (`-O`, `--output-document=`), a digest from an ARG, an ENV (`NAME=value`
    or `NAME value`), a literal or a shell variable set only from one, a pipe in a later pipeline, an ADD with
    `--checksum`, and `curl` named as an apt package. Fold 3's read added: a download and check in an `if` body under
    `fi || true`, or inside a `$(…)` whose status an `echo` swallows; a check line that another command in the pipe (`|
    cat sidecar |`) can replace, or a `printf` whose format adds a line GNU sha256sum skips; a negated check in a
    subshell; a download to a file named through a variable; a digest name `read` sets, or an ENV sets again from a
    build arg; and a `trap` in the RUN. Fold 4's mutants added: a download and check in backquotes; a check line other
    than exactly `echo <digest> <file>` (a `printf` of the two words, a third word); a line piped through the download
    itself, which then prints the line it is checked against; and a digest ARG declared again without a value. Fold 4's
    read added: a check behind a guard under errexit (`false || false && check;`, and the multi-arch pair `[
    "$TARGETARCH" = amd64 ] && check;`, which checks nothing when TARGETARCH is unset); a digest name a `for`, `unset`
    or `getopts "ab:"` sets, or a quoted `export "NAME=…"` or `read "NAME"`; a curl naming a second URL to a second
    output or to its remote name (one with no scheme too), or to standard output redirected to a file; and an `if` body
    under `|| true` whose `fi` is written as a word first. It passes a binary-mode check line (`<digest> *<file>`). Fold
    5's reads added: a list a lone `&` sends to the background; a check in a compound behind `||` or `!`, or piped; a
    `done=1` that closes nothing; a `$(` a continuation splits; an ADD after a line ending in two backslashes (which
    continues nothing); an `sh -c '…'` after a message holding a backquote and an apostrophe; a function named
    `sha256sum`; a curl `--output-dir`, `--remote-name-all` or a wget naming two `-O` outputs; and a digest name set in
    a subshell, after a lone `&`, by a `read` in a subshell, after a case pattern's `)`, or written partly in quotes
    (`export TOOL_'SHA256'=…`). Fold 6's reads added: a `sha256sum -c -` that is only an argument of the stage's program
    (`| echo sha256sum -c -` checks nothing), a function `sha256sum ()` written with a space, an ADD whose keyword a tab
    ends, and an ANSI-C `$'…'` later in the check's own list; it passes a quoted URL holding a `&`. Fold 7's read A
    added: a `set` given a variable before the download (it can be a `set -n`), and a `sha256sum` run from the working
    directory or /tmp (any program can be put there); it passes `/usr/bin/sha256sum`. Fold 8's read B added: a `set`
    given a glob before the download; it passes `/bin/sha256sum`. Guards a check that finds "some checksum later"
    for a download it does not check, and a checksum fetched from where the file came from — whoever swaps the file
    swaps it too (T29's read, S2 and S3: D1, D2, D3, D5, D6; its fold-2 read, S1 and S2; the rules fold 3's mutants
    found no row for; its fold-3 read, S2, N2, N3, N4: Z2, Z8, Z9, Z11, D1, D2, D4, D5, D6, G8; fold 4's mutants:
    O5, D1, D2, D3, T2; its read, S1, S4, S5, N3: G-b, G-c, O-j, D-a, D-c, D-e, D-f, Z22, Z26, Z30, R3, R4, D6; fold
    5's read A, S1 to S4, N1, N4, N5: A2, B2, B5, B9, C5, D3, D7, E2, K5, N1; its read B, N2, N8: NM1, NM3, NM5;
    fold 6's read A, S1, S4, S6; its read B, S1, S3, N4: FA1, MA2; fold 7's read A, S2, N1; fold 8's read B, N1, N4:
    SH1)."""
    faults = download_faults(_TREE + text + "\n")
    assert _found(faults, expected), faults
