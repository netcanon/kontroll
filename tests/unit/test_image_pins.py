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

How a Dockerfile is read. A Dockerfile is any file under docker/ whose name Dependabot's docker fetcher reads as one.
A RUN is read in shell form, split into commands on `&&`, `||`, `;` and `|`, with the operator on each side kept;
quotes are not parsed, so a separator inside a quoted string only adds a boundary. A command is found by its words
anywhere in it (`pip … install`, `apk … add`, `curl`, `wget`, `sha256sum … -c`), so a path before the program, an
option before the subcommand, a shell keyword (`then`) or a `$(…)` does not hide it; a `curl` or `wget` that an
`apt-get install` or `apk add` names is a package, not a download. A command that must stop the build (a
`pip install`, a `pip check`, an `apk add`, a download, a `sha256sum -c`) is followed by `&&` or ends the RUN, and
does not follow `||`: under `/bin/sh -c`, a failure before `;`, `||` or `|` does not fail the RUN (`;` reads as `&&`
in a RUN that opens with `set -e`). Exec-form RUNs
(`RUN [...]`) and heredocs are refused, since these checks cannot read them. What the checks still cannot see: a
command built at run time (`$TOOL install`), a package manager they do not name (`npm`, `gem`, `go install`), a
download inside an `sh -c '…'` string that also names its checksum, and the apt packages, which float inside the
pinned base (SECURITY.md R-IMG-2).
"""
import os
import re

import pytest
import yaml

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
_OPERATORS = re.compile(r"(&&|\|\||;|\|)")
_PIP_INSTALL = re.compile(r"\bpip[\d.]*\"?\s+(?:\S+\s+)*?install\b(.*)$")
_PIP_CHECK = re.compile(r"\bpip[\d.]*\"?(?:\s+-\S+)*\s+check$")
_PIP_INDEX_OPTIONS = ("-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--trusted-host")
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


def _commands(run):
    """[(command, the operator before it, the operator after it)] of a RUN's shell text, split on `&&`, `||`, `;` and
    `|`; the operator is '' at either end of the RUN."""
    parts = _OPERATORS.split(run)
    return [(parts[i].strip(), parts[i - 1] if i else "", parts[i + 1] if i + 1 < len(parts) else "")
            for i in range(0, len(parts), 2) if parts[i].strip()]


def _runs(text):
    """[the commands of each RUN] of a Dockerfile. In a RUN that opens with `set -e` a failure before `;` does fail
    the RUN, so there `;` reads as `&&`."""
    out = []
    for keyword, args in instructions(text):
        if keyword != "RUN":
            continue
        commands = _commands(args)
        if commands and re.match(r"set\s+-[a-z]*e", commands[0][0]):
            commands = [(c, "&&" if b == ";" else b, "&&" if a == ";" else a) for c, b, a in commands]
        out.append(commands)
    return out


def _gate_faults(what, command, before, after):
    """A command that must stop the build is followed by `&&` or ends the RUN, and does not follow `||`."""
    faults = []
    if after not in ("&&", ""):
        faults.append("%s not followed by && (its failure would not fail the RUN): %s" % (what, command))
    if before == "||":
        faults.append("%s runs only when the command before it fails: %s" % (what, command))
    return faults


def _unquote(token):
    return token.strip("\"'")


def image_reference(value):
    """The image a compose `image:` value pulls: the default of a `${VAR:-default}`, else the value itself."""
    m = re.fullmatch(r"\$\{\w+:?-(.+)\}", value.strip())
    return m.group(1) if m else value.strip()


def _tag(ref):
    """The tag of an image reference (after the last `:` of its last path segment), without a digest; '' if none."""
    name = ref.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else ""


def from_pin_faults(text):
    """["<reason>: <args>"] for each FROM, and each COPY or ADD `--from=`, that pulls an image not pinned as
    `<image>:<tag>@sha256:<64 hex>` with a tag other than `latest`. An earlier stage's name (`FROM base`,
    `--from=build`, a stage index) and `scratch` pull nothing. A `${NAME}` or `$NAME` in the reference is read through
    the default of an ARG declared before the first FROM; with no default it stays unread, so unpinned."""
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


def form_faults(text):
    """["<reason>: <line>"] for what these checks cannot read: an exec-form RUN, a heredoc, and a `# syntax=` parser
    directive not pinned by digest (BuildKit pulls that frontend image on every build)."""
    faults = []
    for line in text.splitlines():
        m = re.match(r"\s*#\s*syntax\s*=\s*(\S+)", line, re.I)
        if m and not _DIGEST.search(m.group(1)):
            faults.append("a syntax directive with no digest: " + line.strip())
    for keyword, args in instructions(text):
        if keyword == "RUN" and re.sub(r"^(?:--\S+\s+)*", "", args).startswith("["):
            faults.append("an exec-form RUN: " + args)
        if "<<" in args:
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
    """The pip a command runs: its text up to and including the `pip` word (`"$VENV/pip"`, `python3 -m pip`)."""
    m = re.search(r"\bpip[\d.]*\"?", command)
    return command[:m.end()].strip() if m else ""


def pip_installs(text):
    """[(the pip command, its -r targets, its other non-flag arguments, its flags, the commands after it in the same
    RUN, the operators before and after it)] for every `pip install` in a Dockerfile."""
    out = []
    for commands in _runs(text):
        for i, (command, before, after) in enumerate(commands):
            m = _PIP_INSTALL.search(command)
            if not m:
                continue
            tokens = [_unquote(t) for t in m.group(1).split()]
            files, bare, flags, j = [], [], [], 0
            while j < len(tokens):
                if tokens[j] in ("-r", "--requirement"):
                    files.append(tokens[j + 1] if j + 1 < len(tokens) else "")
                    j += 2
                    continue
                if tokens[j].startswith("--requirement="):
                    files.append(tokens[j].split("=", 1)[1])
                elif tokens[j].startswith("-"):
                    flags.append(tokens[j])
                else:
                    bare.append(tokens[j])
                j += 1
            out.append((command, files, bare, flags, commands[i + 1:], before, after))
    return out


def pip_install_faults(text):
    """["<reason>: <command>"] for each `pip install` in a Dockerfile that names a package itself, names an index,
    lacks `--no-deps`, does not stop the build, or is not followed in the same RUN by a `pip check` of the same pip
    that stops the build."""
    faults = []
    for command, files, bare, flags, later, before, after in pip_installs(text):
        if not files or bare:
            faults.append("installs a package by name, not from -r <file>: " + command)
        if "--no-deps" not in flags:
            faults.append("no --no-deps: " + command)
        if any(f.split("=", 1)[0] in _PIP_INDEX_OPTIONS for f in flags):
            faults.append("names an index: " + command)
        faults += _gate_faults("pip install", command, before, after)
        checks = [(c, b, a) for c, b, a in later if _PIP_CHECK.search(c) and _pip(c) == _pip(command)]
        if not checks:
            faults.append("no `pip check` of the same pip after it: " + command)
        else:
            faults += _gate_faults("pip check", *checks[0])
    return faults


def apk_faults(text):
    """["<reason>: <command>"] for each `apk add` whose packages do not all carry `~=` or `=`, or that does not stop
    the build, and each `apk upgrade` (it moves every base package past the digest)."""
    faults = []
    for commands in _runs(text):
        for command, before, after in commands:
            m = _APK.search(command)
            if not m:
                continue
            if m.group(1) == "upgrade":
                faults.append("apk upgrade moves the base's packages: " + command)
                continue
            packages = [_unquote(t) for t in m.group(2).split() if not t.startswith("-")]
            if not packages or not all(re.fullmatch(r"[\w.+-]+~?=[\w.+-]+", p) for p in packages):
                faults.append("an apk package with no version constraint: " + command)
            faults += _gate_faults("apk add", command, before, after)
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


def _pipeline(commands, j):
    """The words of the pipeline that ends at commands[j]: it and each command joined to it by `|`."""
    k = j
    while k > 0 and commands[k][1] == "|":
        k -= 1
    return " ".join(c for c, _, _ in commands[k:j + 1])


def download_faults(text):
    """["<reason>: <args>"] for each ADD from a URL without `--checksum=sha256:`, and each curl or wget that writes no
    named file, does not stop the build, or whose file no later `sha256sum -c` of the same RUN names (in its own
    words or the pipeline feeding it); a `sha256sum -c` that does not stop the build is one too."""
    faults = []
    for keyword, args in instructions(text):
        if keyword == "ADD":
            sources = [t for t in args.split() if not t.startswith("--")][:-1]
            if any(re.match(r"(?:https?|git|ssh)://|git@", s) for s in sources) and not re.search(
                    r"--checksum=sha256:[0-9a-f]{64}(?:\s|$)", args):
                faults.append("ADD from a URL with no --checksum=sha256: " + args)
    for commands in _runs(text):
        for i, (command, before, after) in enumerate(commands):
            if _SHA_CHECK.search(command):
                faults += _gate_faults("sha256sum -c", command, before, after)
            if not _DOWNLOAD.search(command) or _APT_INSTALL.search(command) or _APK.search(command):
                continue
            faults += _gate_faults("a download", command, before, after)
            target = _download_target(command)
            if not target:
                faults.append("a download to no named file: " + command)
            elif not any(_SHA_CHECK.search(c) and target in _pipeline(commands, j)
                         for j, (c, _, _) in enumerate(commands) if j > i):
                faults.append("a download no later sha256sum -c checks: " + command)
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


def test_every_dockerfile_is_one_these_checks_read():
    """Every Dockerfile under docker/ (each file Dependabot's docker fetcher reads as one, so `Dockerfile.debug` too)
    runs its RUNs in shell form, holds no heredoc, and pins any `# syntax=` frontend by digest. Guards a build step
    these checks would read wrongly or not at all, and a frontend image BuildKit pulls unpinned on every build (T29's
    read, S3: F1, F2, P6, P7)."""
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
], ids=["exec-form", "exec-form-after-mount", "heredoc", "syntax-unpinned", "syntax-pinned", "shell-form"])
def test_the_form_check_refuses_what_the_checks_cannot_read(text, expected):
    """The form check flags an exec-form RUN (behind a `--mount` too), a heredoc, and a `# syntax=` directive with no
    digest; it passes a pinned directive and a shell-form RUN that holds a `[`. Guards a form check that passes
    everything, or refuses an ordinary RUN."""
    assert _found(form_faults(text), expected), form_faults(text)


def test_every_dockerfile_from_is_tag_and_digest_pinned():
    """Every FROM under docker/, and every COPY or ADD `--from=` image, names a tag other than `latest` and an
    `@sha256:` digest: the digest is what is pulled, the tag is the label a reader checks it against. Guards a
    rebuild silently landing on a different base than the one reviewed and published (F11-4, F17-12)."""
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
], ids=["no-digest", "latest", "digest-only", "registry-port-and-stage", "short-digest", "registry-port-no-tag",
        "arg-default-pinned", "arg-default-latest", "arg-no-default", "arg-after-from", "earlier-stage",
        "scratch-and-index", "copy-from-image", "copy-from-pinned"])
def test_the_from_check_tells_a_pin_from_a_near_miss(text, faults):
    """The FROM check flags a tag with no digest, `latest` or no tag with a digest, a digest one hex digit short, an
    ARG-built reference whose default is unpinned or missing (or declared after the first FROM, where a FROM cannot
    read it), and a COPY `--from=` an unpinned image; it passes a pinned reference behind a `--platform` flag, on a
    registry with a port, naming a stage, through a pinned ARG default, an earlier stage by name (any case), `scratch`
    and a stage index. Guards a check that passes everything, reads a registry port as a tag, or refuses a correct
    multi-stage or ARG-built pin (T29's read, N4: F3, F5)."""
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
    (_INSTALL + " && python3 -m pip check || true", ["pip check not followed"]),
    (_INSTALL + "; python3 -m pip check", ["pip install not followed"]),
    (_INSTALL + " && python3 -m pip check; rm -f /tmp/r.txt", ["pip check not followed"]),
    (_INSTALL + " && python3 -m pip check | tee /tmp/log", ["pip check not followed"]),
    (_INSTALL + " && pip check", ["no `pip check`"]),
    ("python3 -m pip install -r /tmp/r.txt && python3 -m pip check", ["no --no-deps"]),
    ("python3 -m pip install --no-deps --extra-index-url=https://x.test -r /tmp/r.txt && python3 -m pip check",
     ["names an index"]),
    ("false || " + _INSTALL + " && python3 -m pip check", ["pip install runs only"]),
    ("set -eu; " + _INSTALL + "; python3 -m pip check; rm -f /tmp/r.txt", []),
    ("set -eu; " + _INSTALL + "; python3 -m pip check || true", ["pip check not followed"]),
], ids=["module", "venv-pip", "bare", "option-first", "versioned-pip", "uv", "check-or-true", "install-then-semicolon",
        "check-then-semicolon", "check-piped", "check-another-pip", "no-no-deps", "extra-index", "behind-or",
        "set-e-semicolons", "set-e-or-true"])
def test_the_pip_check_tells_a_pinned_install_from_a_near_miss(run, expected):
    """The pip check flags an install that names a package (with an option before `install`, a versioned pip, or
    `uv pip`), lacks `--no-deps`, names an index, or runs behind `||`; a `pip check` that is missing, of another pip,
    or switched off by `|| true`, `;` or a pipe; and an install not joined to its check by `&&`. It passes the two
    shapes the images use, and `;` in a RUN that opens with `set -e` (where `|| true` still switches the check off).
    Guards a pin check that a common install shape walks past, and a `pip check` that cannot fail the build (T29's
    read, S2 and S3: P1, P2, P5, P8)."""
    faults = pip_install_faults("RUN " + run + "\n")
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
], ids=["pinned", "option-first", "upgrade", "in-an-if", "then-semicolon", "or-true", "one-unpinned"])
def test_the_apk_check_tells_a_pinned_add_from_a_near_miss(run, expected):
    """The apk check flags a package with no constraint (with an option before `add`, inside an `if`, or one of
    several), an add that cannot fail the build, and any `apk upgrade`; it passes `~=` and `=` constraints. Guards a
    check that only reads `apk add` written first (T29's read, S3: A1, A2, A3)."""
    faults = apk_faults("RUN " + run + "\n")
    assert _found(faults, expected), faults


def test_downloads_in_an_image_build_are_checksum_verified():
    """Every `curl` / `wget` in a Dockerfile RUN writes a named file that a later `sha256sum -c` in the same RUN
    names, and both stop the build; no ADD fetches a URL without `--checksum=sha256:`. Guards a swapped release
    asset (or apt signing key) baked into the image that holds the docker socket."""
    seen = 0
    for rel in dockerfiles():
        text = _read(rel)
        assert download_faults(text) == [], rel
        seen += sum(1 for commands in _runs(text) for c, _, _ in commands
                    if _DOWNLOAD.search(c) and not _APT_INSTALL.search(c))
    assert seen >= 2, "the installer fetches the apt key and the sops binary"


_SUM = 'echo "$sha  /usr/local/bin/tool" | sha256sum -c -'


@pytest.mark.parametrize("text,expected", [
    ("RUN curl -fsSL https://x.test/tool -o /usr/local/bin/tool && " + _SUM + " && chmod 0755 /usr/local/bin/tool",
     []),
    ("RUN wget -qO /usr/local/bin/tool https://x.test/tool && " + _SUM, []),
    ("RUN apt-get install -y --no-install-recommends curl ca-certificates", []),
    ("ADD --checksum=sha256:" + "a" * 64 + " https://x.test/tool /usr/local/bin/tool", []),
    ("ADD https://x.test/tool /usr/local/bin/tool", ["ADD from a URL"]),
    ("RUN /usr/bin/curl -fsSL https://x.test/tool -o /usr/local/bin/tool", ["no later sha256sum"]),
    ("RUN curl -fsSL https://x.test/i.sh | sh && " + _SUM, ["a download not followed", "no named file"]),
    ("RUN curl -fsSL https://x.test/tool -o /usr/local/bin/tool && " + _SUM + " || true",
     ["sha256sum -c not followed"]),
    ("RUN if true; then wget -O /usr/local/bin/tool https://x.test/tool; fi", ["a download not followed",
                                                                                "no later sha256sum"]),
    ('RUN curl -fsSL https://x.test/tool -o /usr/local/bin/tool && echo "$sha  /tmp/other" | sha256sum -c -',
     ["no later sha256sum"]),
    ("RUN curl -fsSLO https://x.test/tool && sha256sum -c sums.txt", ["no named file"]),
    ('RUN v="$(curl -fsSL https://x.test/version)" && true', ["no named file"]),
    ("RUN " + _SUM + " && curl -fsSL https://x.test/tool -o /usr/local/bin/tool", ["no later sha256sum"]),
    ("RUN curl -fsSL https://x.test/tool -o /usr/local/bin/tool && sha256sum /usr/local/bin/tool",
     ["no later sha256sum"]),
], ids=["curl-checked", "wget-checked", "apt-package", "add-checksum", "add-url", "path-prefix-unchecked",
        "piped-to-sh", "check-or-true", "wget-in-an-if", "checks-another-file", "remote-name", "in-a-substitution",
        "checked-before", "summed-not-checked"])
def test_the_download_check_tells_a_checked_download_from_a_near_miss(text, expected):
    """The download check flags an ADD from a URL with no checksum, a download behind a path prefix or inside an
    `if` that nothing checks, a download piped to a shell, one to curl's remote name or captured by `$(…)`, a check
    of another file or one that runs before the download, a `sha256sum` without `-c` (it prints a sum and checks
    nothing), and a `sha256sum -c` switched off by `|| true`; it passes a checked curl or wget, an ADD with
    `--checksum`, and `curl` named as an apt package. Guards a check that only reads a download written first, or
    finds "some checksum later" for a download it does not check (T29's read, S2 and S3: D1, D2, D3, D5, D6)."""
    faults = download_faults(text + "\n")
    assert _found(faults, expected), faults
