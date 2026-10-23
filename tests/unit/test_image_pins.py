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

How a Dockerfile is read. A Dockerfile is any file whose name Dependabot's docker fetcher reads as one; every one
lives under docker/. A RUN is read in shell form by tests/unit/_build_read.py (its docstring gives the rules): split
into commands on `&&`, `||`, `;` and `|`, a command found by its words anywhere in it (`pip … install`, `apk … add`,
`curl`, `wget`, `sha256sum … -c`), so a path before the program, an option before the subcommand, a shell keyword
(`then`) or a `$(…)` does not hide it; a `curl` or `wget` that an `apt-get install` or `apk add` names is a package,
not a download. A command that must stop the build (a `pip install`, a `pip check`, an `apk add`, a download, a
`sha256sum -c`) must fail the RUN when it fails: nothing is piped after it, no `||` follows it, it is no condition,
and every operator after it to the end of the RUN is `&&` — or errexit is on (`set -e`, `set -o errexit`) and `;`
ends its AND-OR list right after it. A download is checked by a line that names its file and a digest from the tree,
fed to `sha256sum -c -` (`echo "$TOOL_SHA256  /path" | sha256sum -c -`, the name an ARG or ENV holding 64 hex, or a
shell variable the RUN sets only from one); a checksum file next to the download is not a check, since whoever swaps
the file swaps it too. An exec-form RUN (`RUN [...]` that parses as JSON), a heredoc, and an `# escape=` directive
are refused, since these checks cannot read them. What the checks still cannot see: a command built at run time
(`$TOOL install`), a package manager they do not name (`npm`, `gem`, `go install`), a download inside an
`sh -c '…'` string that also names its checksum, a shell variable set by `read` or `for`, and the apt packages, which
float inside the pinned base (SECURITY.md R-IMG-2). Refused although sound (each fails loudly): a `pip check` in a
later RUN than its install, a `pip install` option that takes a value and is not named here (its value reads as a
package), and a check written to a file and read by `sha256sum -c <file>`.
"""
import json
import os
import re

import pytest
import yaml
from _build_read import commands as _commands
from _build_read import gate_faults, image_reference
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
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
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


def instructions(text):
    """[(KEYWORD, arguments)] of a Dockerfile: continuation lines joined, comment and blank lines dropped."""
    out, buf = [], ""
    for line in text.splitlines():
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if buf and line.lstrip().startswith("#"):
            continue                                      # a comment inside a continued instruction
        buf += line.rstrip()
        if buf.endswith("\\"):
            buf = buf[:-1] + " "
            continue
        keyword, _, args = buf.strip().partition(" ")
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


def _exec_form(args):
    """True if a RUN's arguments (after its `--` flags) parse as a JSON array, which Docker runs in exec form."""
    try:
        return isinstance(json.loads(re.sub(r"^(?:--\S+\s+)*", "", args)), list)
    except ValueError:
        return False


def form_faults(text):
    """["<reason>: <line>"] for what these checks cannot read: an exec-form RUN, a heredoc (a `<<` outside a
    `$((…))`), an `# escape=` directive (it moves which lines continue a RUN), and a `# syntax=` parser directive not
    pinned by digest (BuildKit pulls that frontend image on every build)."""
    faults = []
    for line in text.splitlines():
        m = re.match(r"\s*#\s*syntax\s*=\s*(\S+)", line, re.I)
        if m and not _DIGEST.search(m.group(1)):
            faults.append("a syntax directive with no digest: " + line.strip())
        if re.match(r"\s*#\s*escape\s*=", line, re.I):
            faults.append("an escape directive: " + line.strip())
    for keyword, args in instructions(text):
        if keyword == "RUN" and _exec_form(args):
            faults.append("an exec-form RUN: " + args)
        if "<<" in re.sub(r"\$\(\(.*?\)\)", "", args):
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
    shell keyword before it (`if`, `then`, `!`) dropped."""
    m = re.search(r"\bpip[\d.]*\"?", command)
    pip = command[:m.end()].strip() if m else ""
    return re.sub(r"^(?:(?:if|elif|while|until|then|do|else|!)\s+)*", "", pip)


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
    the build, and each `apk upgrade` (it moves every base package past the digest)."""
    faults = []
    for commands in _runs(text):
        for i, (command, _, _) in enumerate(commands):
            m = _APK.search(command)
            if not m:
                continue
            if m.group(1) == "upgrade":
                faults.append("apk upgrade moves the base's packages: " + command)
                continue
            packages = [_unquote(t) for t in m.group(2).split() if not t.startswith("-")]
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


def _is_digest(word, names):
    """True for 64 hex, or a `$name`/`${name}` of one of `names`."""
    m = re.fullmatch(r"\$\{?(\w+)\}?", word)
    return bool(re.fullmatch(r"[0-9a-f]{64}", word) or (m and m.group(1) in names))


def tree_digests(text):
    """{the name of each ARG or ENV of a Dockerfile whose value is 64 hex}: a digest written in the tree."""
    names = set()
    for keyword, args in instructions(text):
        if keyword not in ("ARG", "ENV"):
            continue
        tokens = args.split()
        pairs = [t.partition("=")[::2] for t in tokens if "=" in t]
        if keyword == "ENV" and len(tokens) >= 2 and "=" not in tokens[0]:
            pairs = [(tokens[0], " ".join(tokens[1:]))]           # the legacy `ENV NAME value`
        names |= {name for name, value in pairs if re.fullmatch(r"[0-9a-f]{64}", _unquote(value))}
    return names


def _digest_names(commands, digests):
    """The names a RUN reads a digest from the tree through: each of `digests` the RUN does not assign, and each shell
    variable whose every `name=value` assignment in the RUN is 64 hex or one of these names."""
    assigned = {}
    for command, _, _ in commands:
        for name, value in re.findall(r"(?:^|[\s)])(\w+)=(\S*)", command):
            assigned.setdefault(name, []).append(_unquote(value))
    names, grown = {n for n in digests if n not in assigned}, True
    while grown:
        more = {n for n, values in assigned.items() if n not in names and all(_is_digest(v, names) for v in values)}
        names, grown = names | more, bool(more)
    return names


def _checked_words(commands, j, names):
    """The words of the line a `sha256sum -c` at commands[j] checks, when it reads that line from the pipeline feeding
    it (no file operand but `-`, no `<`, no `--ignore-missing`) and the line holds a digest from the tree; else
    None."""
    words = commands[j][0].split()
    at = next((k for k, w in enumerate(words) if re.search(r"(?:^|/)sha256sum$", w)), None)
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
    if any(o != "-" for o in operands) or not any(_is_digest(w, names) for w in line):
        return None
    return line


def download_faults(text):
    """["<reason>: <args>"] for each ADD from a URL without `--checksum=sha256:`, and each curl or wget that writes no
    named file, does not stop the build, or whose file no later `sha256sum -c` of the same RUN checks against a
    digest from the tree (a line naming the file as a whole word and the digest, piped to `sha256sum -c -`); a
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
            if not target:
                faults.append("a download to no named file: " + command)
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
], ids=["exec-form", "exec-form-after-mount", "heredoc", "syntax-unpinned", "syntax-pinned", "shell-form",
        "test-bracket", "arithmetic-shift", "escape-directive"])
def test_the_form_check_refuses_what_the_checks_cannot_read(text, expected):
    """The form check flags an exec-form RUN (behind a `--mount` too), a heredoc, an `# escape=` directive, and a
    `# syntax=` directive with no digest; it passes a pinned directive, a shell-form RUN that holds a `[` or opens
    with a `[ … ]` test (not JSON, so Docker runs it in a shell), and a `<<` inside `$((…))`. Guards a form check that
    passes everything, or refuses an ordinary RUN (T29's fold-2 read, N3 and N4)."""
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
], ids=["no-digest", "latest", "digest-only", "registry-port-and-stage", "short-digest", "registry-port-no-tag",
        "arg-default-pinned", "arg-default-latest", "arg-no-default", "arg-after-from", "earlier-stage",
        "scratch-and-index", "copy-from-image", "copy-from-pinned", "mount-from-image", "mount-from-stage"])
def test_the_from_check_tells_a_pin_from_a_near_miss(text, faults):
    """The FROM check flags a tag with no digest, `latest` or no tag with a digest, a digest one hex digit short, an
    ARG-built reference whose default is unpinned or missing (or declared after the first FROM, where a FROM cannot
    read it), and a COPY `--from=` or a RUN `--mount=…,from=` an unpinned image; it passes a pinned reference behind a
    `--platform` flag, on a registry with a port, naming a stage, through a pinned ARG default, an earlier stage by
    name (any case, a mount's too), `scratch`, a stage index and a mount with no `from=`. Guards a check that passes
    everything, reads a registry port as a tag, or refuses a correct multi-stage or ARG-built pin (T29's read, N4:
    F3, F5; its fold-2 read, N4: a `--mount` pulled `busybox:latest` unread)."""
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
    ("set -e; " + _INSTALL + " && python3 -m pip check; rm -f /tmp/r.txt", ["pip install not followed"]),
    ("set -o errexit; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -e; set +o errexit; " + _INSTALL + "; python3 -m pip check; true",
     ["pip install not followed", "pip check not followed"]),
    ("set -e; if " + _INSTALL + "; then python3 -m pip check; fi", ["pip install is a condition"]),
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
], ids=["module", "venv-pip", "bare", "option-first", "versioned-pip", "uv", "check-or-true", "install-then-semicolon",
        "check-then-semicolon", "check-piped", "check-another-pip", "no-no-deps", "extra-index", "behind-or",
        "set-e-semicolons", "set-e-or-true", "or-true-at-the-end", "semicolon-later", "set-e-not-last-of-its-list",
        "set-o-errexit", "errexit-switched-off", "install-as-a-condition", "check-negated", "pipe-in-a-later-pipeline",
        "index-prefix", "index-attached", "index-in-a-cluster", "editable-long", "editable-short", "valued-options",
        "set-plus-e", "set-double-dash", "check-before-install"])
def test_the_pip_check_tells_a_pinned_install_from_a_near_miss(run, expected):
    """The pip check flags an install that names a package (with an option before `install`, a versioned pip, `uv
    pip`, or an `-e`/`--editable` project), lacks `--no-deps`, names an index (in full, by an unambiguous prefix,
    attached to `-i`, or in a flag cluster), or runs behind `||`; a `pip check` that is missing or of another pip; and
    an install or check whose failure would not fail the RUN: a later `|| true` or `;` anywhere after it, a pipe right
    after it, a `;` under errexit when it is not the last command of its AND-OR list, a `;` after errexit is switched
    off (`set +e`, `set +o errexit`; `set -- -e` sets no option), an `if` condition, a `!`, and a `pip check` that
    runs before the install. It passes the two shapes the images use, `;` under `set -e` or `set -o errexit`
    right after each, a pipe in a later pipeline, and `--target`, `-c` and an attached `-r<file>`. Guards a pin check
    that a common install shape walks past, and a `pip check` that cannot fail the build — "make the cleanup
    tolerant" with a trailing `|| true` was one (T29's read, S2 and S3: P1, P2, P5, P8; its fold-2 read, S1, N3,
    N4)."""
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
], ids=["pinned", "option-first", "upgrade", "in-an-if", "then-semicolon", "or-true", "one-unpinned",
        "or-true-later"])
def test_the_apk_check_tells_a_pinned_add_from_a_near_miss(run, expected):
    """The apk check flags a package with no constraint (with an option before `add`, inside an `if`, or one of
    several), an add that cannot fail the build (a `;` or `|| true` right after it or later in the RUN), and any
    `apk upgrade`; it passes `~=` and `=` constraints. Guards a check that only reads `apk add` written first (T29's
    read, S3: A1, A2, A3; its fold-2 read, S1)."""
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
     ["sha256sum -c is a condition"]),
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
     ["sha256sum -c is a condition"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | sha256sum -c /tmp/tool.sha256',
     ["no later sha256sum"]),
    ('RUN ' + _GET + ' && echo "$TOOL_SHA256  /usr/local/bin/tool" | sha256sum -c -</tmp/tool.sha256',
     ["no later sha256sum"]),
    ("ENV TOOL2_SHA256 " + "c" * 64 + '\nRUN ' + _GET + ' && echo "$TOOL2_SHA256  /usr/local/bin/tool" '
     '| sha256sum -c -', []),
    ('RUN TOOL_SHA256="$(cat /tmp/x)" && ' + _GET + ' && ' + _SUM, ["no later sha256sum"]),
], ids=["curl-checked", "wget-checked", "apt-package", "add-checksum", "add-url", "path-prefix-unchecked",
        "piped-to-sh", "check-or-true", "wget-in-an-if", "checks-another-file", "remote-name", "in-a-substitution",
        "checked-before", "summed-not-checked", "or-true-at-the-end", "semicolon-later", "check-as-a-condition",
        "pipe-in-a-later-pipeline", "sidecar-checksum-file", "sidecar-read-into-the-line", "name-prefix",
        "ignore-missing", "digest-not-from-the-tree", "literal-digest", "through-a-shell-variable",
        "variable-also-set-from-a-file", "env-digest", "check-reads-a-file", "wget-long-output",
        "check-piped-behind-or", "check-as-a-nested-condition", "file-operand-after-a-pipe", "stdin-redirected",
        "legacy-env-digest", "tree-name-reassigned"])
def test_the_download_check_tells_a_checked_download_from_a_near_miss(text, expected):
    """The download check flags an ADD from a URL with no checksum, a download behind a path prefix or inside an
    `if` that nothing checks, a download piped to a shell, one to curl's remote name or captured by `$(…)`, a check
    of another file (a longer name with the file's as its prefix too) or one that runs before the download, a
    `sha256sum` without `-c` (it prints a sum and checks nothing), a download or check whose failure would not fail
    the RUN (a later `|| true` or `;`, an `if` condition, a pipeline into the check that runs only when the download
    fails, a condition inside a `then`), and a check whose digest is not from the tree: a checksum
    file downloaded beside the file (read by `-c <file>` or into the line), a name no ARG or ENV gives 64 hex or one
    the RUN sets again, a shell variable also set from a file, `--ignore-missing`, a check written to a file, or one
    that reads a file or a redirect instead of the pipe. It passes a checked curl or wget (`-O`,
    `--output-document=`), a digest from an ARG, an ENV (`NAME=value` or `NAME value`), a literal or a shell variable
    set only from one, a pipe in a later pipeline, an ADD with `--checksum`, and `curl` named as an apt package.
    Guards a check that finds "some checksum later" for a download it does not check, and a checksum fetched from
    where the file came from — whoever swaps the file swaps it too (T29's read, S2 and S3: D1, D2, D3, D5, D6; its
    fold-2 read, S1 and S2; the rules fold 3's mutants found no row for)."""
    faults = download_faults(_TREE + text + "\n")
    assert _found(faults, expected), faults
