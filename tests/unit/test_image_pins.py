"""Every input an image build or the composed stack pulls is pinned: a base by tag and digest, a compose image by an
exact tag, a Python package by an exact version from a committed file, an apk package by its minor, a downloaded
binary by its checksum.

WHY (the failure this guards). A rebuild is the project's recovery move (docs/floating-prod-rebuild.md), and a
rebuild from `FROM semaphoreui/semaphore:latest` plus a bare `pip install paramiko flask fastapi uvicorn` landed on
whatever upstream published that day: a different Semaphore, a different library in Semaphore's ansible venv, a
base no reviewer saw (L11 F11-4, L17 F17-12). The published images were pinned by digest (docker/images.lock.yml);
the bases they were built from, and every local build, were not.

These checks read the committed text only. They fail when a pin is MISSING, never because a pin is old — nothing
here compares a version with upstream; Dependabot (.github/dependabot.yml) and the refresh procedure in
docker/README.md move the pins. The host's legacy ansible-core range is held by test_fresh_install_compat.py.
"""
import glob
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
_EXACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!_-]*$")


def _read(rel, root=ROOT):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def dockerfiles(root=ROOT):
    """Every Dockerfile under docker/, repo-relative."""
    return sorted(os.path.relpath(p, root).replace(os.sep, "/")
                  for p in glob.glob(os.path.join(root, "docker", "**", "Dockerfile"), recursive=True))


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
    """The shell commands of a RUN, split on `&&`, `;` and `|`."""
    return [c.strip() for c in re.split(r"&&|;|\|", run) if c.strip()]


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
    """["<reason>: <FROM args>"] for each FROM that is not `<image>:<tag>@sha256:<64 hex>` with a tag other than
    `latest`."""
    faults = []
    for keyword, args in instructions(text):
        if keyword != "FROM":
            continue
        ref = next((t for t in args.split() if not t.startswith("--")), "")
        if not _DIGEST.search(ref):
            faults.append("no digest: " + args)
        if _tag(ref) in ("", "latest"):
            faults.append("no tag: " + args)
    return faults


def compose_images(root=ROOT):
    """{image reference: the file naming it} for every `image:` in docker/services/*.yaml and telemetry/*.yml."""
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

    for pattern in ("docker/services/*.yaml", "telemetry/*.yml"):
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            walk(yaml.safe_load(_read(rel, root)), rel)
    return found


def pip_installs(text):
    """[(the pip command, its -r targets, its other non-flag arguments, the commands after it in the same RUN)] for
    every `pip install` in a Dockerfile."""
    out = []
    for keyword, args in instructions(text):
        if keyword != "RUN":
            continue
        commands = _commands(args)
        for i, command in enumerate(commands):
            m = re.search(r"\bpip3?\"?\s+install\b(.*)$", command)
            if not m:
                continue
            tokens = [_unquote(t) for t in m.group(1).split()]
            files, bare, j = [], [], 0
            while j < len(tokens):
                if tokens[j] in ("-r", "--requirement"):
                    files.append(tokens[j + 1] if j + 1 < len(tokens) else "")
                    j += 2
                    continue
                if not tokens[j].startswith("-"):
                    bare.append(tokens[j])
                j += 1
            out.append((command, files, bare, commands[i + 1:]))
    return out


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
        for _, files, _, _ in pip_installs(text):
            for target in files:
                out[copied_from(text, target) or "<not COPY'd: %s>" % target] = rel
    return out


def _dist(line):
    """The normalized distribution name of a requirements line (PEP 503), or None for a comment/blank line."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    return re.sub(r"[-_.]+", "-", re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0]).lower()


def test_every_dockerfile_from_is_tag_and_digest_pinned():
    """Every FROM under docker/ names a tag other than `latest` and an `@sha256:` digest: the digest is what is
    pulled, the tag is the label a reader checks it against. Guards a rebuild silently landing on a different base
    than the one reviewed and published (F11-4, F17-12)."""
    files = dockerfiles()
    assert len(files) >= 3, files                         # the runner, the installer, the Vector image
    faults = {rel: from_pin_faults(_read(rel)) for rel in files}
    assert {rel: f for rel, f in faults.items() if f} == {}


@pytest.mark.parametrize("line,faults", [
    ("FROM debian:12-slim", ["no digest: debian:12-slim"]),
    ("FROM semaphoreui/semaphore:latest@sha256:" + "a" * 64,
     ["no tag: semaphoreui/semaphore:latest@sha256:" + "a" * 64]),
    ("FROM semaphoreui/semaphore@sha256:" + "a" * 64, ["no tag: semaphoreui/semaphore@sha256:" + "a" * 64]),
    ("FROM --platform=$BUILDPLATFORM ghcr.io:443/x/y:1.2@sha256:" + "b" * 64 + " AS build", []),
    ("FROM x:1.2@sha256:" + "c" * 63, ["no digest: x:1.2@sha256:" + "c" * 63]),
    ("FROM ghcr.io:443/x/y@sha256:" + "d" * 64, ["no tag: ghcr.io:443/x/y@sha256:" + "d" * 64]),
], ids=["no-digest", "latest", "digest-only", "registry-port-and-stage", "short-digest", "registry-port-no-tag"])
def test_the_from_check_tells_a_pin_from_a_near_miss(line, faults):
    """The FROM check flags a tag with no digest, `latest` or no tag with a digest, and a digest one hex digit
    short; it passes a pinned reference behind a `--platform` flag, on a registry with a port, naming a stage, and
    flags one on a registry with a port but no tag. Guards a check that passes everything, or reads a registry port
    as a tag."""
    assert from_pin_faults(line + "\n") == faults


def floating(ref):
    """True if a compose image reference names no tag of at least major.minor (a local-build name excepted)."""
    return ref not in _LOCAL_BUILDS and not re.match(r"^v?\d+\.\d+", _tag(ref))


@pytest.mark.parametrize("ref,is_floating", [
    ("postgres:16-alpine", True),
    ("postgres:16.15-alpine", False),
    ("prom/prometheus:v3.5.4", False),
    ("grafana/grafana:latest", True),
    ("grafana/grafana", True),
    ("ghcr.io:443/x/y", True),
    ("kontroll/semaphore:latest", False),
], ids=["bare-major", "major-minor", "v-prefix", "latest", "no-tag", "registry-port-no-tag", "local-build"])
def test_the_compose_tag_check_tells_exact_from_floating(ref, is_floating):
    """The compose-tag check flags a bare major (`16-alpine`), `latest`, no tag, and a registry port with no tag;
    it passes major.minor with or without a `v` and a `-<variant>`, and the local-build names. Guards a check that
    reads `16-alpine` as pinned — the floating tag this test was written for."""
    assert floating(ref) is is_floating


def test_every_compose_image_names_an_exact_tag():
    """Every `image:` of a stack fragment and of a telemetry descriptor (the default of a `${VAR:-…}` included)
    has a tag of at least major.minor — not `latest`, not a bare major like `16-alpine`. The local-build names are
    exempt by exact name (tags the deploy creates, never pulled), and each exemption must still be named. Guards a
    fragment shipping a floating tag: `postgres:16-alpine` moved to a new minor on every fresh pull."""
    images = compose_images()
    assert {ref: rel for ref, rel in images.items() if floating(ref)} == {}
    assert _LOCAL_BUILDS <= set(images), "stale _LOCAL_BUILDS rows: %r" % sorted(_LOCAL_BUILDS - set(images))


def test_image_builds_install_python_packages_only_from_a_pinned_file():
    """Every `pip install` in a Dockerfile installs from `-r <file>` and names no package itself; the file is
    COPY'd from the repository, and each of its lines is `name==version`. Guards an unpinned package moving a
    library inside Semaphore's ansible venv on the next build."""
    seen = 0
    for rel in dockerfiles():
        text = _read(rel)
        for command, files, bare, _ in pip_installs(text):
            seen += 1
            assert files and not bare, "%s: %r" % (rel, command)
            for target in files:
                source = copied_from(text, target)
                assert source, "%s: %s is not COPY'd from the repository" % (rel, target)
                lines = [ln.strip() for ln in _read(source).splitlines() if ln.strip() and not ln.startswith("#")]
                assert lines and all(_EXACT.match(ln) for ln in lines), (source, [ln for ln in lines
                                                                                  if not _EXACT.match(ln)])
    assert seen >= 2, "the runner and the installer each install Python packages"


def test_every_image_pip_install_is_the_whole_set_and_checked():
    """Each image `pip install` carries `--no-deps` and is followed in the same RUN by `pip check`: nothing the
    file does not name can be installed or moved, and the build fails if the set is incomplete or conflicts with
    the base. Guards the pinned file silently ceasing to be the whole truth (a dependency pulled at whatever
    version is newest)."""
    for rel in dockerfiles():
        for command, _, _, after in pip_installs(_read(rel)):
            assert "--no-deps" in command.split(), "%s: %r" % (rel, command)
            assert any(re.search(r"\bpip3?\"?\s+check$", c) for c in after), "%s: no `pip check` after it" % rel


def test_image_requirements_cover_the_declared_runtime_floors():
    """Every package gui/requirements.txt and api/requirements.txt name ships in the control image: in
    docker/semaphore-runner/requirements.txt, or from the base and imported by the runner Dockerfile's build-time
    check. A floor names no extra (`uvicorn[standard]`): the image installs `--no-deps`, so an extra's packages
    would never ship. Guards the image and the declared runtime deps drifting apart (they had: PyYAML, the uvicorn
    extra)."""
    closure = {_dist(ln) for ln in _read("docker/semaphore-runner/requirements.txt").splitlines()} - {None}
    runner = _read("docker/semaphore-runner/Dockerfile")
    imported = set()
    for keyword, args in instructions(runner):
        for m in re.finditer(r"-c\s+\"import ([\w, ]+)\"", args) if keyword == "RUN" else ():
            imported |= {name.strip() for name in m.group(1).split(",")}
    for floor in ("gui/requirements.txt", "api/requirements.txt"):
        for line in _read(floor).splitlines():
            dist = _dist(line)
            if dist is None:
                continue
            assert "[" not in line, "%s names an extra the image does not ship: %r" % (floor, line)
            assert dist in closure or _FROM_THE_BASE.get(dist) in imported, "%s: %s ships nowhere" % (floor, dist)
    assert set(_FROM_THE_BASE.values()) <= imported, "the base packages must be import-checked at build"
    assert not closure & set(_FROM_THE_BASE), "a base package in the closure would re-install it over the base's"


def test_apk_packages_carry_a_version_constraint():
    """Each package an `apk add` installs carries `~=` (a minor, its patch revision free) or `=`. Guards a bare
    package name coming back, which takes whatever the Alpine branch serves that day."""
    seen = 0
    for rel in dockerfiles():
        for keyword, args in instructions(_read(rel)):
            for command in _commands(args) if keyword == "RUN" else ():
                if not re.match(r"apk\s+add\b", command):
                    continue
                packages = [_unquote(t) for t in command.split()[2:] if not t.startswith("-")]
                seen += len(packages)
                assert packages and all(re.fullmatch(r"[\w.+-]+~?=[\w.+-]+", p) for p in packages), (rel, command)
    assert seen, "the runner installs apk packages"


def test_downloads_in_an_image_build_are_checksum_verified():
    """Every `curl` / `wget` in a Dockerfile RUN is followed, in the same RUN, by a `sha256sum -c`. Guards a
    swapped release asset (or apt signing key) baked into the image that holds the docker socket."""
    seen = 0
    for rel in dockerfiles():
        for keyword, args in instructions(_read(rel)):
            commands = _commands(args) if keyword == "RUN" else []
            for i, command in enumerate(commands):
                if re.match(r"(curl|wget)\b", command):
                    seen += 1
                    assert any(re.match(r"sha256sum\s+-c\b", c) for c in commands[i + 1:]), "%s: %r" % (rel, command)
    assert seen >= 2, "the installer fetches the apt key and the sops binary"


def test_the_requirement_files_are_exactly_the_ones_the_images_install():
    """The requirements files under docker/ are exactly the files a Dockerfile pip-installs. Dependabot's pip entries
    and the pip-audit step (tests/unit/test_ci_workflows.py) are held to the same set, so this keeps them honest
    about what the images run. Guards a file that no image installs (audited, never shipped) and an install from
    a file that is not where the others look."""
    on_disk = {os.path.relpath(p, ROOT).replace(os.sep, "/")
               for p in glob.glob(os.path.join(ROOT, "docker", "**", "requirements*.txt"), recursive=True)}
    assert set(image_requirement_files()) == on_disk
