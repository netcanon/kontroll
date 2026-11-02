"""Text pins on the CI workflows the public split changed (publish target, the zizmor SARIF seat, Dependabot
strategy, third-party action pins) and on the gate wiring (the `homelab*` push trigger, the Windows shim), plus the
package-install helper every workflow step that runs apt goes through (.github/scripts/apt-install.sh), run against
a fake apt.

WHY — a workflow file has no unit test of its own, so a behaviour the 2026-10-09 review fixed can quietly come back
in a later edit: the publish target hard-coded to one org again, the SARIF seat scanning `.github/` (every alert URI
wrong), an `|| true` hiding a real zizmor failure, a `security-events: write` grant minted on every run, a pip
ecosystem that chases `>=` floors, or a third-party action tag-pinned in the one job that can push to the package
namespace. The Dependabot and pip-audit coverage of the images, and the gate's tool versions against the compose
pins, are held the same way. Each test names the failure it guards and reads the committed YAML as text (plus a
YAML parse where the shape matters). CLAUDE.md: the test lands with the behaviour change.
"""
import glob
import os
import re
import subprocess
import sys

import pytest
import yaml
from _build_read import commands, gate_faults, image_reference

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.path.join(ROOT, ".github", "workflows")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_publish_target_is_derived_from_the_repository_owner():
    """publish-images.yml must push to `ghcr.io/${{ steps.meta.outputs.owner }}/kontroll-*` and name NO owner
    literally — the same file publishes the public tool's packages and any instance's own. A `sha-<short>` dispatch
    tag must be checked against the commit being built, and the image must carry the OCI source/revision labels a
    re-pin reviewer verifies. Guards the hard-coded private org coming back and an unverifiable tag claim."""
    wf = _read(".github/workflows/publish-images.yml")
    assert "tags: ghcr.io/${{ steps.meta.outputs.owner }}/kontroll-${{ matrix.image }}" in wf
    assert not re.search(r"ghcr\.io/[a-z0-9-]+/kontroll", wf), "no literal owner may appear in the publish target"
    assert "tr '[:upper:]' '[:lower:]'" in wf, "the owner must be lowercased (ghcr namespaces are lowercase)"
    assert 'sha-*) short="${TAG#sha-}"' in wf and "does not name the commit being built" in wf, \
        "a sha-<short> tag must be verified against GITHUB_SHA"
    assert "org.opencontainers.image.revision=${{ github.sha }}" in wf, "the image must carry its revision label"
    assert "org.opencontainers.image.source=" in wf


def test_zizmor_sarif_seat_is_gated_rooted_unmasked_and_least_privilege():
    """The SARIF seat must be its OWN job, gated on `vars.KONTROLL_CODE_SCANNING == 'true'` at the job level (so the
    `security-events: write` grant is never minted where code scanning does not exist), scan `.` (rooted at `.github/`
    every uploaded URI points at a path that does not exist), and carry no `|| true` (SARIF mode exits 0 with
    findings; a non-zero exit is a real failure that must surface). Workflow-level permissions stay `contents: read`
    only."""
    text = _read(".github/workflows/zizmor.yml")
    doc = yaml.safe_load(text)
    top_perms = doc.get("permissions") or {}
    assert top_perms == {"contents": "read"}, "workflow-level permissions must be contents: read only"
    sarif_jobs = [j for j in doc["jobs"].values() if "security-events" in (j.get("permissions") or {})]
    assert len(sarif_jobs) == 1, "exactly one job may hold security-events: write"
    job = sarif_jobs[0]
    assert job.get("if") == "${{ vars.KONTROLL_CODE_SCANNING == 'true' }}", "the SARIF job must be variable-gated"
    runs = [s.get("run", "") for s in job["steps"] if "run" in s]
    assert any(r.strip() == "zizmor --format sarif . > zizmor.sarif" for r in runs), \
        "the SARIF scan must be rooted at the repository (`.`) and write zizmor.sarif"
    assert "|| true" not in text, "no step may mask a zizmor failure"
    uploads = [s for s in job["steps"] if str(s.get("uses", "")).startswith("github/codeql-action/upload-sarif@")]
    assert uploads and uploads[0].get("continue-on-error") is True, "the upload is advisory (continue-on-error)"


_FLOORS = {"/gui", "/api", "/tests"}


def _updates(ecosystem):
    """The dependabot.yml update entries of one ecosystem."""
    return [u for u in yaml.safe_load(_read(".github/dependabot.yml"))["updates"]
            if u["package-ecosystem"] == ecosystem]


def _directories(update):
    """The directories one dependabot.yml entry covers (`directory:` or `directories:`)."""
    return set(update.get("directories") or [update["directory"]])


def _image_requirement_dirs():
    """{"/docker/<dir>"} of every requirements file under docker/ — the exact pins an image installs
    (tests/unit/test_image_pins.py holds that these are exactly the files the Dockerfiles install)."""
    found = glob.glob(os.path.join(ROOT, "docker", "**", "requirements*.txt"), recursive=True)
    return {"/" + os.path.relpath(os.path.dirname(p), ROOT).replace(os.sep, "/") for p in found}


def test_pip_floors_use_increase_if_necessary_and_image_pins_do_not():
    """The pip ecosystems for the `>=` floors (gui, api, tests) use `versioning-strategy: increase-if-necessary`:
    the default strategy opens a floor-bump PR for every release (five the first week). The image pin files
    (docker/…/requirements.txt) keep the default strategy: they are exact pins, so each release must be a PR, and
    `increase-if-necessary` would leave a pin that still "admits" nothing newer untouched forever. Guards either
    kind drifting to the other's strategy, and a floor or an image pin file losing its entry."""
    pip = _updates("pip")
    floors = [u for u in pip if _directories(u) <= _FLOORS]
    pins = [u for u in pip if not _directories(u) & _FLOORS]
    assert len(floors) + len(pins) == len(pip), "an entry mixes a floor directory with another"
    assert set().union(*map(_directories, floors)) == _FLOORS, "gui, api and tests requirements are covered"
    for u in floors:
        assert u.get("versioning-strategy") == "increase-if-necessary", u
    image_dirs = _image_requirement_dirs()
    assert image_dirs and set().union(*map(_directories, pins)) == image_dirs, "each image pin file has an entry"
    for u in pins:
        assert "versioning-strategy" not in u, u


# The file names Dependabot reads (dependabot-core docker/lib/dependabot/docker/file_fetcher.rb DOCKER_REGEXP and
# docker_compose/file_fetcher.rb FILENAME_REGEX, each matched anywhere in a file's name).
_DOCKERFILE_NAME = re.compile(r"dockerfile|containerfile", re.I)
_COMPOSE_FETCHED = re.compile(r"(docker-)?compose(-[\w]+)?(?>\.[\w-]+)?\.ya?ml", re.I)


def test_dependabot_covers_every_dockerfile_directory():
    """Every directory under docker/ that holds a Dockerfile (a file Dependabot's docker fetcher reads as one:
    `Dockerfile`, `Dockerfile.debug`, `x.containerfile`) is in a `docker` ecosystem entry, so each pinned base (tag
    and digest) has an update process. Guards a new image added with no way for its base to move but by hand (the
    installer and Vector images had none)."""
    dirs = dockerfile_dirs()
    covered = set().union(*map(_directories, _updates("docker")))
    assert dirs and dirs <= covered, sorted(dirs - covered)


def dockerfile_dirs(root=ROOT):
    """{"/docker/<dir>"} of every directory under docker/ holding a file Dependabot's docker fetcher reads."""
    return {"/" + os.path.relpath(d, root).replace(os.sep, "/")
            for d, _, names in os.walk(os.path.join(root, "docker")) if any(map(_DOCKERFILE_NAME.search, names))}


def test_dockerfile_dirs_follow_dependabots_name_rule(tmp_path):
    """dockerfile_dirs() names a directory holding a `Dockerfile.debug` or a `build.containerfile`, and not one holding
    only a README. Guards the coverage check reading only files named exactly `Dockerfile`, while Dependabot's
    fetcher reads more (T29's read, S3: F2)."""
    for rel in ("docker/a/Dockerfile.debug", "docker/b/build.containerfile", "docker/c/README.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("FROM scratch\n", encoding="utf-8")
    assert dockerfile_dirs(str(tmp_path)) == {"/docker/a", "/docker/b"}


def _image_name(ref):
    """An image reference without its digest and tag (`ghcr.io:443/x/y:1.2@sha256:…` -> `ghcr.io:443/x/y`)."""
    name = ref.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    return (head + "/" if head else "") + last.split(":", 1)[0]


def compose_entry_faults(update, root=ROOT):
    """["<directory>: <reason>"] for one `docker-compose` dependabot.yml entry: a directory holding no file whose
    name Dependabot's compose fetcher reads and whose `services:` name an `image:` (the entry can never open a PR,
    and its run fails), and an image that a generated file the fetcher reads there names and the entry does not
    ignore (a bot edit to it fails `gen-exporters.py --check`, so the PR can never merge)."""
    faults = []
    ignored = {i["dependency-name"] for i in update.get("ignore") or [] if not i.get("update-types")}
    for directory in sorted(_directories(update)):
        path = os.path.join(root, directory.lstrip("/"))
        imaged = []
        for name in sorted(os.listdir(path)) if os.path.isdir(path) else []:
            if not _COMPOSE_FETCHED.search(name) or not os.path.isfile(os.path.join(path, name)):
                continue
            with open(os.path.join(path, name), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            services = (doc.get("services") if isinstance(doc, dict) else None) or {}
            images = {_image_name(image_reference(s["image"]))
                      for s in services.values() if isinstance(s, dict) and isinstance(s.get("image"), str)}
            if images:
                imaged.append(name)
            if ".generated." in name:
                faults += ["%s: %s names %s, not ignored" % (directory, name, i) for i in sorted(images - ignored)]
        if not imaged:
            faults.append("%s: no file Dependabot's compose fetcher reads names an image" % directory)
    return faults


def test_dependabot_compose_entries_read_a_compose_file():
    """Every `docker-compose` entry's directory holds a file whose name Dependabot's compose fetcher reads
    (`compose.yaml`, `docker-compose.yml`, `<name>.compose.yaml`) and whose `services:` name an image, and ignores the
    images of the generated files there. Today there is no such entry: the fragments in docker/services are named
    `<service>.yaml`, which the fetcher does not read, so the compose tags are bumped by hand (docker/README.md
    § Refreshing a pin). Guards the dead entry coming back: T29 first shipped one on docker/services, which its header,
    three docs and a test called coverage, and which could never open a PR (T29's read, B1)."""
    assert [f for u in _updates("docker-compose") for f in compose_entry_faults(u)] == []


_SERVICE = "services:\n  api:\n    image: x/api:1.2.3\n"


@pytest.mark.parametrize("files,ignore,expected", [
    ({"api.yaml": _SERVICE, "db.yaml": _SERVICE}, [], ["no file"]),
    ({"compose.yaml": "include:\n  - api.yaml\n", "api.yaml": _SERVICE}, [], ["no file"]),
    ({"api.compose.yaml": _SERVICE}, [], []),
    ({"docker-compose.yml": _SERVICE}, [], []),
    ({"api.compose.yaml": _SERVICE, "x.generated.compose.yaml": "services:\n  x:\n    image: prom/x:1.2.3\n"},
     [], ["prom/x, not ignored"]),
    ({"api.compose.yaml": _SERVICE, "x.generated.compose.yaml": "services:\n  x:\n    image: prom/x:1.2.3\n"},
     ["prom/x"], []),
    ({}, [], ["no file"]),
    ({"api.compose.yaml": _SERVICE,
      "x.generated.compose.yaml": "services:\n  x:\n    image: ${X_IMAGE:-prom/x:1.2.3}\n"}, ["prom/x"], []),
    ({"api.compose.yaml": _SERVICE,
      "x.generated.compose.yaml": "services:\n  x:\n    image: ghcr.io:443/prom/x:1.2.3\n"},
     ["ghcr.io:443/prom/x"], []),
], ids=["fragment-names", "include-only-root", "dot-compose", "docker-compose", "generated-unignored",
        "generated-ignored", "missing-directory", "generated-default-ignored", "generated-registry-port-ignored"])
def test_the_compose_entry_check_tells_a_read_directory_from_a_dead_one(tmp_path, files, ignore, expected):
    """The compose-entry check flags a directory of `<service>.yaml` fragments (the shape T29 first shipped), one
    whose only fetched file holds `include:` and no `services:`, a directory that does not exist, and a generated
    file's image the entry does not ignore; it passes `<name>.compose.yaml` and `docker-compose.yml` files, and a
    generated image that is ignored — named through a `${VAR:-…}` default, or on a registry with a port. Guards a
    check that passes the dead entry, or refuses a directory the fetcher reads (T29's fold-2 read, N5, N6)."""
    for name, text in files.items():
        (tmp_path / "d" / name).parent.mkdir(exist_ok=True)
        (tmp_path / "d" / name).write_text(text, encoding="utf-8")
    entry = {"directory": "/d", "ignore": [{"dependency-name": n} for n in ignore]}
    faults = compose_entry_faults(entry, str(tmp_path))
    assert len(faults) == len(expected) and all(e in f for e, f in zip(expected, faults)), faults


def _compose_tag(fragment, image):
    """The version of `image` in a docker/services fragment's `image:` (the `${VAR:-…}` default included), with a
    leading `v`, a `-<variant>` suffix and a digest removed."""
    m = re.search(re.escape(image) + r":v?([0-9][0-9.]*)", _read("docker/services/" + fragment))
    assert m, (fragment, image)
    return m.group(1)


def test_ci_tool_versions_match_the_compose_pins():
    """The promtool and vector binaries ci.yml installs for the gate are the versions the stack runs: promtool's
    release equals the prom/prometheus tag, and vector's equals the timberio/vector tag. Guards a one-sided bump (a
    bot's compose or Dockerfile PR) leaving the gate validating with a different version than the one that runs —
    the "versions track the compose pins" comment in ci.yml is otherwise a promise no check keeps."""
    ci = _read(".github/workflows/ci.yml")
    promtool = re.findall(r"prometheus/releases/download/v([0-9.]+)/", ci)
    vector = re.findall(r"vectordotdev/vector/releases/download/v([0-9.]+)/", ci)
    assert promtool == [_compose_tag("prometheus.yaml", "prom/prometheus")], promtool
    assert vector == [_compose_tag("vector.yaml", "timberio/vector")], vector


# The `if:` values that run a step or job whenever an unconditioned one would run, or more often.
_UNCONDITIONED = {"always()", "success()", "!cancelled()"}

# The env keys that change what a pip-audit step runs: a file bash sources first (`BASH_ENV`; `ENV`, which only an
# interactive shell reads, is refused to be safe), options it starts with (`SHELLOPTS`, `BASHOPTS`), a prompt it
# expands when it traces (`PS4`: its `${BASH_CMDS[…]:=…}` makes an entry that stands in for a program), where it
# finds a program (`PATH`), a function it imports (`BASH_FUNC_<name>%%`), where Python finds pip-audit's modules
# (`PYTHONPATH`, `PYTHONHOME`, `PYTHONUSERBASE`) and a library the loader puts first (`LD_PRELOAD`,
# `LD_LIBRARY_PATH`) or loads to audit the others (`LD_AUDIT`).
_BASH_ENV = (r"BASH_ENV|ENV|SHELLOPTS|BASHOPTS|PS4|PATH|BASH_FUNC_.*"
             r"|PYTHONPATH|PYTHONHOME|PYTHONUSERBASE|LD_PRELOAD|LD_AUDIT|LD_LIBRARY_PATH")


def _script(run):
    """A `run:` script as one line of shell text, read whole as bash reads it: a quote stays open across a newline (the
    newline is then a character of the string, marked, as a quote still open at the end is, and `pip_audit_faults`
    refuses the step), a backslash and newline outside single quotes are removed, a backslash
    inside single quotes is literal, a backquoted command is read as a quote, a `#` that opens a word outside quotes
    cuts the rest of its line (one right after a `)` is marked, and `pip_audit_faults` refuses the step: bash opens a
    comment after a subshell's `)` and not after a `$(…)`'s), and the lines are joined with `;` (a newline ends a
    command list as `;` does), but a line ending in `&&`, `||` or `|` with a space (the list goes on to the next
    line)."""
    lines, buf, quote, k, text = [], "", None, 0, str(run or "")
    while k < len(text):
        ch = text[k]
        if ch == "\\" and quote != "'":
            if text[k + 1:k + 2] != "\n":
                buf += text[k:k + 2]
            k += 2
            continue
        if quote:
            quote = None if ch == quote else quote
            buf += " \x04" if ch == "\n" else ch
        elif ch in "'\"`":
            quote, buf = ch, buf + ch
        elif ch == "#" and (not buf or buf[-1].isspace() or buf[-1] in ";&|("):
            while k < len(text) and text[k] != "\n":
                k += 1
            continue
        elif ch == "#" and buf.endswith(")"):
            buf += "\x03#"
        elif ch == "\n":
            lines.append(buf.strip())
            buf = ""
        else:
            buf += ch
        k += 1
    out = ""
    for ln in [ln for ln in lines + [buf.strip()] if ln]:
        out += (" " if re.search(r"(?:&&|\|\||\|)$", out) else "; " if out else "") + ln
    return out + ("\x04" if quote else "")


def _shell(workflow, job, step):
    """The `shell:` a step runs under: its own, else its job's `defaults.run.shell`, else the workflow's; None if no
    one names one (GitHub's default, `bash -e {0}`)."""
    for node in (step, job.get("defaults") or {}, workflow.get("defaults") or {}):
        shell = node.get("shell") if node is step else (node.get("run") or {}).get("shell")
        if shell:
            return shell
    return None


def _named_unix_runner(runs_on):
    """True if a job's `runs-on` names, without an expression, a GitHub-hosted Linux or macOS image (`ubuntu-…`,
    `macos-…`) or a label list holding `linux` or `macos` (a self-hosted runner's OS label) and not `windows`: a runner
    whose unnamed shell is `bash -e {0}`. Anything else (an expression, a Windows image, a list with no OS label, no
    `runs-on`) may run an unnamed shell as pwsh."""
    labels = [runs_on] if isinstance(runs_on, str) else runs_on if isinstance(runs_on, list) else []
    labels = [str(label).lower() for label in labels]
    if not labels or any("${{" in label or "windows" in label for label in labels):
        return False
    return any(re.match(r"(?:ubuntu|macos)-", label) or label in ("linux", "macos") for label in labels)


def pip_audit_faults(workflow):
    """({the -r files the workflow's pip-audit runs read}, [faults]) of a parsed workflow. A step runs pip-audit when a
    command of its script (`_script`, split as tests/unit/_build_read.py splits a RUN) starts with it. A fault is such a
    step whose audit cannot fail its job: a `shell:` other than the default, `bash` or `sh` (each of those runs with
    errexit; another one need not), an audit whose failure would not fail the script (a pipe or a lone `&` after it, a
    later `||`, a `;` after it unless the audit starts its AND-OR list and that list ends there, a `;` inside a
    compound, a compound behind `||` or `!`, a condition, a `$(…)` or quoted string around it, a function or an alias in
    the step or an entry in bash's command tables (`hash`, `enable`, `BASH_CMDS`, `BASH_ALIASES`), a `trap`, `exec`,
    `return`, an `exit` other than `exit 1` to `exit 255`, a brace expansion the reader cannot read (an item holding a
    nested substitution or parameter, or one an operator inside a quote or a substitution cuts), or a `set -n` or `set
    -t` (or a `shopt` naming noexec or onecmd, or a `set` or `shopt` given a variable, a backquoted command, a glob or a
    brace expansion) in the step: the _build_read rules, errexit on at the start and off after a `set +e` or such a
    `set`), a `set +e`, `set +o errexit` or a `shopt` naming errexit anywhere in the step, a heredoc in the step (its
    body is not read as the shell reads it), `continue-on-error` on the step or the job, a step that names no shell
    (itself or a `defaults.run.shell`) under a `runs-on` that is no named Linux or macOS runner (`_named_unix_runner`:
    an expression, which a repository variable may point at a Windows runner, a Windows image, or labels with no OS
    label or holding an expression; a Windows runner's default shell is pwsh, which need not stop on a failure); or an
    audit that runs on a condition (`if:` on the step or the job other than `always()`, `success()` or `!cancelled()`);
    a whitespace character other than a space, a tab or a line feed, a `${{` expression (in a comment too: GitHub writes
    its value in first), a `#` right after a `)` (bash opens a comment after a subshell's `)`, not after a `$(…)`'s, and
    this check tells neither), a newline inside a quoted string or a quote left open (an `eval` reads a
    backslash-newline there as a continuation, and this check does not read a quote nested in a `"$(…)"`), or a C0
    control character that is not whitespace (a NUL, which bash drops inside a word, or one of this check's own marks),
    in the step's script; an env key, in any case (a Windows runner reads `Path` as `PATH`), that changes what the step
    runs (`_BASH_ENV`: `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`, `PS4`, `PATH`, `BASH_FUNC_…`, `PYTHONPATH`,
    `PYTHONHOME`, `PYTHONUSERBASE`, `LD_PRELOAD`, `LD_AUDIT`, `LD_LIBRARY_PATH`), or an env that is not a mapping, on
    the step, the job or the workflow; or a `container:` on the job (its env and its options reach bash unread). It
    refuses sound shapes too, each loudly: a pipe after the audit under `shell: bash` (which runs with pipefail), an
    `exit 0` after it, and the word `exit`, `return`, `trap` or `exec` in a message (`echo "will exit on error"`), an
    `&>` redirect after the audit, an audit sent to the background and waited for (`& wait $!`), a `$(…)#` (to bash the
    `#` is part of a word), an ANSI-C `$'…'` in the audit's list, the word `alias`, `hash` or `enable` in a message (and
    a `hash -r`), the word `set` or `shopt` in a message before a word holding `$`, a backquote, a glob character, `{`
    or `(` (`echo "set up (once)"`), a newline inside a quoted string (a message over two lines), a C0 control character
    in a message (the ESC of an ANSI colour code), a `shopt -uo` of noexec or onecmd, an ANSI-C `$'…'` before the audit,
    a brace expansion where bash builds no command word from it (an assignment's value, a redirect's target, a quoted
    string: `TARGETS={exit,0}`, `x={exit,0} true`, `echo hi >{exit,}`, `echo "mode: {exec,dry-run}"`, `jq
    '{hash,set}'`), a brace expansion the reader cannot read where bash builds no command word from it (`mkdir -p
    o/{${x:-${y}},c}`), an arithmetic expansion, `${#}`, `${#x}` or `$0` glued to a command word (`exit$((0))`, which
    bash runs as `exit0`, a command not found: the reader removes each, so that a substitution holding one is read),
    `$-` glued to one (`exit$- 0`: a step's shell runs with `-e`, so `$-` is never empty) and under bash `$_` (`exit$_
    0` after `true` runs `exittrue`), a pattern replacement of an unset variable (`${x/#/exit} 0`), a `set --` given a
    variable (`set -- $x`), a brace after an escaped `$` (`echo \\${a,b}`) or left open (`echo {a,b`), and an audit that
    opens a subshell (`(pip-audit -r a)`), sits inside a function or is spelt other than `pip-audit …` (`python -m
    pip_audit`, the pip-audit action): none of these is read as a run, so the coverage test fails, or (a function over
    several lines) it is read and refused. It cannot see an earlier step that writes one of those env keys through
    `$GITHUB_ENV`, or a PATH entry through `$GITHUB_PATH`: a later step runs under it; nor another env key that leads
    the audit to a planted file; nor a `working-directory:` or a `cd` that moves the file a `-r` names (a copy planted
    at `<dir>/docker/…` is then audited instead; with none, pip-audit fails on the missing file); nor a command word
    built at run time from what a variable or a positional parameter holds or a substitution prints, or by a glob
    (`x=ex; ${x}it 0`, `exi$_ 0` after `: t` (bash), `set -- ex; ${1}it 0`, `$(printf ex)it 0`, `` `echo exit` 0 ``,
    `$(echo hash) -p …`, `ex?t 0`). An expansion glued to a command word or splitting it is read as empty and again as a
    space, all but `$#`, `$?` and `$$`, which never come out empty (`exit$x 0`, `` exit`:` 0 ``, `e$(:)xit 0`, `set$x
    +e`, `hash${x} -p …`, `exit${IFS}0`, `exit$*0` after `set -- ' '`, `exit$( (:) ) 0`, `exit$(:|:) 0`), and a
    parameter, named, indirect or subscripted too, given a `-`, `:-`, `+`, `:+`, `=` or `:=` operator or a pattern
    replacement as its word (`${1-exit} 0`, `${x:-exit} 0`, `${0:+exit} 0`, `${1-${2-exit}} 0`, `${x:=exit} 0`,
    `${0/*/exit} 0`, `${!x:-exit} 0`, `${a[0]:-exit} 0`): those are read."""
    audited, faults = set(), []
    for name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            script = commands(_script(step.get("run")))
            runs = [i for i, (c, _, _) in enumerate(script)
                    if re.match(r"(?:(?:if|elif|while|until|then|do|else|!)\s+)*pip-audit(?:\s|$)", c)]
            if not runs:
                continue
            shell = _shell(workflow, job, step)
            if shell not in (None, "bash", "sh"):
                faults.append("%s: the pip-audit step runs under `shell: %s`, which need not stop on a failure"
                              % (name, shell))
            if shell is None and not _named_unix_runner(job.get("runs-on")):
                faults.append("%s: the pip-audit step names no shell and its runner is not a named Linux or macOS "
                              "one (a Windows runner's default shell is pwsh, which need not stop on a failure)" % name)
            for i in runs:
                audited |= set(re.findall(r"(?:^|\s)(?:-r|--requirement)(?:\s+|=)(\S+)", script[i][0]))
                faults += ["%s: %s" % (name, f) for f in gate_faults(
                    "pip-audit", script, i, on=shell in (None, "bash", "sh"), unit="step")]
            if any(re.search(r"\bset\s+(?:.*\s)?\+(?:[a-zA-Z]*e|o\s+errexit)|\bshopt\b.*\berrexit\b", c)
                   for c, _, _ in script):
                faults.append("%s: `set +e`, `set +o errexit` or `shopt … errexit` in a pip-audit step" % name)
            if "<<" in re.sub(r"\$\(\(.*?\)\)", "", _script(step.get("run"))):
                faults.append("%s: a heredoc in a pip-audit step (its body is not read as the shell reads it)" % name)
            if re.search(r"[^\S \t\n]", str(step.get("run"))):
                faults.append("%s: a whitespace character other than a space, a tab or a line feed in a pip-audit "
                              "step (this check splits words on it and bash does not)" % name)
            if "\x03" in _script(step.get("run")):
                faults.append("%s: a `#` right after a `)` in a pip-audit step (bash opens a comment after a "
                              "subshell's `)` and not after a `$(…)`'s; this check tells neither)" % name)
            if "\x04" in _script(step.get("run")):
                faults.append("%s: a newline inside a quoted string, or a quote left open, in a pip-audit step "
                              "(an `eval` reads a backslash-newline there as a continuation, and this check does "
                              "not read a quote nested in a `\"$(…)\"`, so it can misplace where a string ends)"
                              % name)
            if re.search(r"[\x00-\x08\x0e-\x1b]", str(step.get("run"))):
                faults.append("%s: a C0 control character that is not whitespace in a pip-audit step (bash drops "
                              "a NUL inside a word, and this check uses such characters as its own marks)" % name)
            if "${{" in str(step.get("run")):
                faults.append("%s: an expression in a pip-audit step's script (GitHub writes its value into the "
                              "script before bash reads it)" % name)
            if "container" in job:
                faults.append("%s: a `container:` on the pip-audit job (its env and its options reach bash and are "
                              "not read here)" % name)
            for where, node in (("step", step), ("job", job), ("workflow", workflow)):
                env = node.get("env")
                if env is not None and (not isinstance(env, dict)
                                        or any(re.fullmatch(_BASH_ENV, str(k), re.I) for k in env)):
                    faults.append("%s: `BASH_ENV` or another env key that changes what the step runs "
                                  "(`ENV`, `SHELLOPTS`, `BASHOPTS`, `PS4`, `PATH`, `BASH_FUNC_…`, `PYTHONPATH`, "
                                  "`PYTHONHOME`, `PYTHONUSERBASE`, `LD_PRELOAD`, `LD_AUDIT`, `LD_LIBRARY_PATH`), "
                                  "or an env that is not a mapping, on the pip-audit %s" % (name, where))
            for where, node in (("step", step), ("job", job)):
                if node.get("continue-on-error"):
                    faults.append("%s: continue-on-error on the pip-audit %s" % (name, where))
                if "if" in node and re.sub(r"^\$\{\{\s*|\s*\}\}$", "", str(node["if"]).strip()) not in _UNCONDITIONED:
                    faults.append("%s: the pip-audit %s runs on a condition" % (name, where))
    return audited, faults


def test_pip_audit_covers_the_image_requirement_files():
    """The pip-audit job reads every requirements file an image installs, as well as the floors, and each audit can
    fail its job (pip_audit_faults' rules). The workflow is read through its parsed steps, so a commented-out step
    reads nothing. Guards the audit going back to reading only the floors — which name no version an image runs — so
    a known vulnerability in a pinned image package (one was found when this landed: certifi) would pass unseen; and
    a step that is still there but cannot fail (T29's read, S2: W1, W2; its fold-2 read, S1), among them one that a
    Windows runner picked by the repository variable would run under pwsh (its fold-3 read, N5)."""
    text = _read(".github/workflows/security.yml")
    assert not re.search("[\x85\u2028\u2029]", text), \
        "NEL, LS or PS in security.yml: the YAML reader breaks a line there and bash does not"
    audited, faults = pip_audit_faults(yaml.safe_load(text))
    images = {d.lstrip("/") + "/requirements.txt" for d in _image_requirement_dirs()}
    assert images and images <= audited, sorted(images - audited)
    assert {"gui/requirements.txt", "api/requirements.txt", "tests/requirements-dev.txt"} <= audited, audited
    assert faults == []


_AUDIT = "pip-audit --no-deps -r docker/a/requirements.txt"
_RUNNER = "${{ fromJSON(vars.KONTROLL_RUNS_ON || '\"ubuntu-latest\"') }}"


@pytest.mark.parametrize("step,job,read,expected", [
    ({"run": _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "pip install --quiet pip-audit"}, {}, set(), []),
    ({"run": "echo pip-audit -r docker/a/requirements.txt"}, {}, set(), []),
    ({"run": "# " + _AUDIT}, {}, set(), []),
    ({"run": _AUDIT + " || true"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": _AUDIT, "continue-on-error": True}, {}, {"docker/a/requirements.txt"}, ["continue-on-error"]),
    ({"run": _AUDIT}, {"continue-on-error": True}, {"docker/a/requirements.txt"}, ["continue-on-error"]),
    ({"run": "set +e\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["set +e"]),
    ({"run": _AUDIT, "if": "false"}, {}, {"docker/a/requirements.txt"}, ["condition"]),
    ({"run": "# never `set +e` here\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "cd x && " + _AUDIT + " --requirement=b.txt"}, {}, {"docker/a/requirements.txt", "b.txt"}, []),
    ({"run": "pip-audit --no-deps \\\n  -r docker/a/requirements.txt \\\n  -r docker/b/requirements.txt"}, {},
     {"docker/a/requirements.txt", "docker/b/requirements.txt"}, []),
    ({"run": _AUDIT + " \\\n  || true"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": _AUDIT + " | tee pip-audit.txt"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": _AUDIT + "\necho done", "shell": "bash {0}"}, {}, {"docker/a/requirements.txt"},
     ["shell: bash {0}", "not followed"]),
    ({"run": _AUDIT}, {"defaults": {"run": {"shell": "pwsh"}}}, {"docker/a/requirements.txt"}, ["shell: pwsh"]),
    ({"run": "set +o errexit\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["not followed", "set +o errexit"]),
    ({"run": _AUDIT + " && echo ok\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT + "  # the image pins", "shell": "bash"}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT, "if": "always()"}, {"if": "${{ !cancelled() }}"}, {"docker/a/requirements.txt"}, []),
    ({"run": "if " + _AUDIT + "; then echo ok; fi"}, {}, {"docker/a/requirements.txt"}, ["is a condition"]),
    ({"run": _AUDIT + "  # never add || true here"}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "# was: cd x; pip-audit -r old.txt | tee log\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "{ cd .; " + _AUDIT + "; } || true"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "trap 'exit 0' EXIT\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `trap"]),
    ({"run": "cat <<'EOF'\n" + _AUDIT + "\nEOF"}, {}, {"docker/a/requirements.txt"}, ["a heredoc"]),
    ({"run": _AUDIT + " --desc 'see #1' || true"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "if set +e; then :; fi\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["not followed", "set +e"]),
    ({"run": _AUDIT}, {"runs-on": _RUNNER}, {"docker/a/requirements.txt"}, ["names no shell"]),
    ({"run": _AUDIT}, {"runs-on": _RUNNER, "defaults": {"run": {"shell": "bash"}}}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT}, {"runs-on": "ubuntu-latest"}, {"docker/a/requirements.txt"}, []),
    ({"run": "# the audit below \\\n" + _AUDIT + " || true"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "echo a#b && " + _AUDIT + " || true"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "echo $((1 << 2))\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "test -f nope && " + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "shopt -uo errexit\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["not followed", "shopt … errexit"]),
    ({"run": "eval 'set +e'\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["not followed", "set +e"]),
    ({"run": _AUDIT + ' --desc "a \\" #" || true'}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": _AUDIT + ";# || true, never set +e"}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT + " --desc 'x' # || true"}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT + ' --desc "see #1" || true'}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": _AUDIT + "\nexit 0"}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit 0`"]),
    ({"run": _AUDIT + "\npython -c 0"}, {"runs-on": "windows-latest"}, {"docker/a/requirements.txt"},
     ["names no shell"]),
    ({"run": _AUDIT}, {"runs-on": ["self-hosted"]}, {"docker/a/requirements.txt"}, ["names no shell"]),
    ({"run": _AUDIT}, {"runs-on": None}, {"docker/a/requirements.txt"}, ["names no shell"]),
    ({"run": _AUDIT}, {"runs-on": ["self-hosted", "Linux", "X64"]}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT}, {"runs-on": ["self-hosted", "Linux", "Windows"]}, {"docker/a/requirements.txt"},
     ["names no shell"]),
    ({"run": _AUDIT}, {"runs-on": "macos-14"}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT + " &"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "true || (true && " + _AUDIT + " && true)"}, {}, {"docker/a/requirements.txt"}, ["runs in a compound"]),
    ({"run": "echo $'it\\'s'; sh -c 'cd .; " + _AUDIT + "; true'"}, {}, {"docker/a/requirements.txt"},
     ["runs inside", "not followed", "a newline inside a quoted string"]),
    ({"run": "echo $'a\\'b #'; trap 'exit 0' EXIT\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["runs inside"]),
    ({"run": 'x="a\nb" # ; ' + _AUDIT}, {}, set(), []),
    ({"run": 'x="start\n# "; set +e; y="\n"\n' + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["not followed", "set +e", "a newline inside a quoted string"]),
    ({"run": "test -f nope &&\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "true ||\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["runs only when"]),
    ({"run": "cd . &&\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "pip-audit() { :; }\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["a function or an alias"]),
    ({"run": "shopt -s expand_aliases\nalias pip-audit=true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": _AUDIT + " --desc 'x\\' # ' || true"}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT}, {"runs-on": ["self-hosted", "linux", "${{ vars.X }}"]}, {"docker/a/requirements.txt"},
     ["names no shell"]),
    ({"run": "ex\\\nit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit 0`"]),
    ({"run": "set +\\\ne\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["set +e"]),
    ({"run": "set `cat o`\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `set", "not followed"]),
    ({"run": "shopt -s expand_aliases\n'alias' pip-audit=true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "\"pip-audit\"() { :; }\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["a function or an alias"]),
    ({"run": "function pip-audit { :; }\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["a function or an alias"]),
    ({"run": _AUDIT + " && echo $'\\' # ' || true"}, {}, {"docker/a/requirements.txt"}, ["runs inside"]),
    ({"run": _AUDIT + " &&\necho $'\\''\necho 'done'"}, {}, {"docker/a/requirements.txt"},
     ["runs inside", "a newline inside a quoted string"]),
    ({"run": _AUDIT + " && echo $'\\'' & echo 'x'"}, {}, {"docker/a/requirements.txt"},
     ["runs inside", "a newline inside a quoted string"]),
    ({"run": _AUDIT + " && echo `echo # ` || true"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": _AUDIT + " && echo $(true)# || true"}, {}, {"docker/a/requirements.txt"},
     ["not followed", "right after a `)`"]),
    ({"run": _AUDIT + " && (true)# || true"}, {}, {"docker/a/requirements.txt"},
     ["not followed", "right after a `)`"]),
    ({"run": "shopt -s lastpipe\ntrue |&exit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `&exit 0`"]),
    ({"run": "pip-audit --no-deps -r\xa0docker/a/requirements.txt"}, {}, {"docker/a/requirements.txt"},
     ["a whitespace character"]),
    ({"run": "echo \"${{ github.ref }}\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["an expression"]),
    ({"run": _AUDIT, "env": {"BASH_ENV": "/tmp/e"}}, {}, {"docker/a/requirements.txt"}, ["`BASH_ENV`"]),
    ({"run": _AUDIT}, {"env": {"BASH_ENV": "/tmp/e"}}, {"docker/a/requirements.txt"}, ["`BASH_ENV`"]),
    ({"run": _AUDIT, "env": "${{ fromJSON(vars.E) }}"}, {}, {"docker/a/requirements.txt"}, ["`BASH_ENV`"]),
    ({"run": _AUDIT + " && (true)# it\'s fine\necho done"}, {}, {"docker/a/requirements.txt"},
     ["right after a `)`", "a newline inside a quoted string"]),
    ({"run": _AUDIT + " && (cd .)# see `x\necho done"}, {}, {"docker/a/requirements.txt"}, ["right after a `)`",
     "a newline inside a quoted string"]),
    ({"run": _AUDIT + "\x0b# || true"}, {}, {"docker/a/requirements.txt"}, ["a whitespace character"]),
    ({"run": _AUDIT + "\u3000# || true"}, {}, {"docker/a/requirements.txt"}, ["a whitespace character"]),
    ({"run": "# ${{ github.event.head_commit.message }}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["an expression"]),
    ({"run": "(:)# && " + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["right after a `)`"]),
    ({"run": "((1))#; " + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["right after a `)`"]),
    ({"run": "hash -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["command tables"]),
    ({"run": "BASH_CMDS[pip-audit]=/bin/true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["command tables"]),
    ({"run": "shopt -s expand_aliases\nBASH_ALIASES[pip-audit]=true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["command tables"]),
    ({"run": "enable -f ./x.so pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["command tables"]),
    ({"run": "eval 'ex\\\nit 0'\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a newline inside a quoted string"]),
    ({"run": "x=n; set -$x\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `set"]),
    ({"run": "set +[e]\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `set", "not followed"]),
    ({"run": "set +?\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `set", "not followed"]),
    ({"run": "set +*\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `set", "not followed"]),
    ({"run": _AUDIT, "env": {"BASH_FUNC_pip-audit%%": "() { true; }"}}, {}, {"docker/a/requirements.txt"},
     ["another env key"]),
    ({"run": _AUDIT}, {"env": {"SHELLOPTS": "noexec"}}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT}, {"env": {"BASHOPTS": "expand_aliases"}}, {"docker/a/requirements.txt"},
     ["another env key"]),
    ({"run": _AUDIT, "env": {"PATH": "/tmp/fake"}}, {}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT, "env": {"ENV": "/tmp/e"}}, {}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT, "env": {"PYTHONPATH_X": "/tmp/p", "ENVIRONMENT": "ci", "XBASH_ENV": "x"}}, {},
     {"docker/a/requirements.txt"}, []),
    ({"run": _AUDIT}, {"container": {"image": "python:3.12", "env": {"BASH_ENV": "/tmp/e"}}},
     {"docker/a/requirements.txt"},
     ["a `container:`"]),
    ({"run": "(hash -p /bin/true pip-audit; " + _AUDIT + " )"}, {}, {"docker/a/requirements.txt"},
     ["command tables"]),
    ({"run": "hash>/dev/null -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["command tables"]),
    ({"run": "shopt -s expand_aliases\nalias</dev/null pip-audit=true\n" + _AUDIT}, {},
     {"docker/a/requirements.txt"}, ["a function or an alias"]),
    ({"run": ": ${BASH_CMDS[pip-audit]:=/bin/true}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["command tables"]),
    ({"run": "set -{n,n}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `set"]),
    ({"run": "set +{e,}\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `set", "not followed"]),
    ({"run": "if set +{e,}; then :; fi\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `if set", "not followed"]),
    ({"run": "shopt -s extglob\nset +@(e)\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `set", "not followed"]),
    ({"run": "set -t\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `set -t`"]),
    ({"run": "set -o onecmd\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `set -o onecmd`"]),
    ({"run": "shopt -os noexec\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt -os noexec`"]),
    ({"run": "shopt -so onecmd\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt -so onecmd`"]),
    ({"run": "x=errexit; shopt -ou $x\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt", "not followed"]),
    ({"run": "eval \"ex\\\\\nit 0\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a newline inside a quoted string"]),
    ({"run": "eval 'ex\\'\"\n\"'it 0'\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a newline inside a quoted string"]),
    ({"run": 'x="$(echo "a"; # )"; ' + _AUDIT + '; echo "\n)"'}, {}, {"docker/a/requirements.txt"},
     ["a newline inside a quoted string"]),
    ({"run": 'echo "it\'s" \\\n&& echo \'x\'\n' + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\ne\x00xit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["a C0 control character"]),
    ({"run": "true\ne\x01xit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["a C0 control character"]),
    ({"run": "echo x\x02" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["a C0 control character"]),
    ({"run": "(:)\\\n# && " + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["right after a `)`"]),
    ({"run": "set -x\n" + _AUDIT, "env": {"PS4": "${BASH_CMDS[pip-audit]:=/bin/true}"}}, {},
     {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": "set -x\n" + _AUDIT}, {"env": {"PS4": "${BASH_CMDS[pip-audit]:=/bin/true}"}},
     {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT, "env": {"PYTHONPATH": "/tmp/p"}}, {}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT}, {"env": {"PYTHONHOME": "/tmp/p"}}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT, "env": {"PYTHONUSERBASE": "/tmp/p"}}, {}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT}, {"env": {"LD_PRELOAD": "/tmp/x.so"}}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT, "env": {"LD_LIBRARY_PATH": "/tmp"}}, {}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": _AUDIT}, {"container": "python:3.12"}, {"docker/a/requirements.txt"}, ["a `container:`"]),
    ({"run": "true\nexit$\"\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit"]),
    ({"run": "$\"\"hash -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "hash$\"\" -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "set$\"\" -n\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `set"]),
    ({"run": "set$\"\" +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "shopt -so no{exec,}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `shopt"]),
    ({"run": "shopt -so `echo noe`xec\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `shopt"]),
    ({"run": "shopt -so noexe[c]\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `shopt"]),
    ({"run": "shopt -so noex*\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `shopt"]),
    ({"run": "shopt -so noexe?\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `shopt"]),
    ({"run": "shopt -s extglob\nshopt -so @(noe)xec\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt"]),
    ({"run": "if shopt -uo err{exit,exit}; then :; fi\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `if shopt", "not followed"]),
    ({"run": "shopt -uo `echo err`exit\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt", "not followed"]),
    ({"run": "shopt -uo errexi[t]\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt", "not followed"]),
    ({"run": "shopt -uo errex*\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt", "not followed"]),
    ({"run": "shopt -uo errexi?\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `shopt", "not followed"]),
    ({"run": "shopt -s extglob\nshopt -uo @(err)exit\n" + _AUDIT + "\necho done"}, {},
     {"docker/a/requirements.txt"}, ["shares the step with `shopt", "not followed"]),
    ({"run": _AUDIT}, {"env": {"LD_AUDIT": "/tmp/x.so"}}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": "true\n{exit,0}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exit,0}"]),
    ({"run": "{hash,-p,/bin/true,pip-audit}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "{set,-n}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `{set,-n}"]),
    ({"run": "true\ncommand {exit,0}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `command {exit,0}"]),
    ({"run": "if {set,+e}; then :; fi\n" + _AUDIT + "\necho done"}, {},
     {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "{e..e}xit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{e..e}xit"]),
    ({"run": "{e..e}x{i..i}t 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{e..e}x{i..i}t"]),
    ({"run": "if {s..s}et +e; then :; fi\n" + _AUDIT + "\necho done"}, {},
     {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "if shopt -uo err{exit,\"a b\"}; then :; fi\n" + _AUDIT + "\necho done"}, {},
     {"docker/a/requirements.txt"}, ["shares the step with `if shopt", "not followed"]),
    ({"run": "set {+e,\"a b\"}\n" + _AUDIT + "\necho done"}, {},
     {"docker/a/requirements.txt"}, ["shares the step with `set {+e", "not followed"]),
    ({"run": _AUDIT}, {"env": {"Path": "./tools"}}, {"docker/a/requirements.txt"}, ["another env key"]),
    ({"run": "true\nexit$@\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$@`"]),
    ({"run": "true\n$@exit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `$@exit 0`"]),
    ({"run": "true\nexit$*\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$*`"]),
    ({"run": "true\nexit$!\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$!`"]),
    ({"run": "true\nexit$1\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$1`"]),
    ({"run": "true\n$9exit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `$9exit 0`"]),
    ({"run": "true\nexit${@}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${@}`"]),
    ({"run": "hash$@ -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "set$@ +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "true\n$\"ex\"it 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `$\"ex\"it 0`"]),
    ({"run": "echo {a,b}\nmkdir -p out/{a,b}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "echo {a..c}\nmkdir -p out/{1..3}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "echo $\"hello\"\necho \"cost: $\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "echo \"$@\" $1 $* $!\necho \"cost: $1\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "{exec,true,\"a b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,\"a b\"}`"]),
    ({"run": "{hash,-p,/bin/true,pip-audit,\"a b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "{exec,true,{}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,{}}`"]),
    ({"run": "true\n{exit,${@}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exit,${@}}`"]),
    ({"run": "{exec,true,$(echo a b)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,$(echo a b)}`"]),
    ({"run": "{exec,true,{{}}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,{{}}}`"]),
    ({"run": "{exec,true,`echo a b`}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,`echo a b`}`"]),
    ({"run": "true\n{,,exit,0}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `{,,exit,0}`"]),
    ({"run": "true\n{exit,} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `{exit,} 0`"]),
    ({"run": "{e..e}{x..x}{i..i}t 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{e..e}{x..x}{i..i}t"]),
    ({"run": "true\nexit${*}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${*}`"]),
    ({"run": "true\nexit${!}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${!}`"]),
    ({"run": "true\nexit${1}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${1}`"]),
    ({"run": "true\nexit${@:-}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${@:-}`"]),
    ({"run": "true\nexit${10}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${10}`"]),
    ({"run": "true\n${1-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-exit} 0`"]),
    ({"run": "set${10} +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "find . -name nothing -exec echo {} \\; ; mkdir -p o/{b,c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     []),
    ({"run": "echo \"${1:-none}\" ${10} ${@:-all}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "TARGETS={exit,0}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `TARGETS={exit,0}`"]),
    ({"run": "x={exit,0} true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `x={exit,0} true`"]),
    ({"run": "echo hi >{exit,}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `echo hi >{exit,}`"]),
    ({"run": "echo \"mode: {exec,dry-run}\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `echo \"mode: {exec,dry-run}\"`"]),
    ({"run": "jq '{hash,set}' a.json\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["a function or an alias"]),
    ({"run": "{exec,true,'a b'}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,'a b'}`"]),
    ({"run": "{exec,true,a\\ b}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,a\\ b}`"]),
    ({"run": "{exec,true,\"a\\\" b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,\"a\\\" b\"}`"]),
    ({"run": "{exec,true,\"}\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,\"}\"}`"]),
    ({"run": "{exec,true,${x:-a b}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,${x:-a b}}`"]),
    ({"run": "{set,-n,\"a b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{set,-n,\"a b\"}`"]),
    ({"run": "{trap,\"ex\"\"it 0\",EXIT}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{trap,\"ex\"\"it 0\",EXIT}`"]),
    ({"run": "true\n${@:-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${@:-exit} 0`"]),
    ({"run": "true\nexit${@#x}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${@#x}`"]),
    ({"run": "{exec,true,{${x}}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,{${x}}}`"]),
    ({"run": "{hash,-p,/bin/true,pip-audit,{\"a,b\"}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "{exec,true,{\\,}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,{\\,}}`"]),
    ({"run": "{exec,true,{$(echo a)}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,{$(echo a)}}`"]),
    ({"run": "{set,+e,{${x}}}\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{set,+e,{${x}}}`", "not followed"]),
    ({"run": "true\n${10-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${10-exit} 0`"]),
    ({"run": "echo {x$(:,exit)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "{exec,true,${x:-${y}}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,${x:-${y}}}`"]),
    ({"run": "{exec,true,${x:-${y:-a b}}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,${x:-${y:-a`"]),
    ({"run": "{exec,true,${x:-\"}\"}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,${x:-…}}`"]),
    ({"run": "{exec,true,$(echo $(echo a))}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,$(echo`"]),
    ({"run": "{exec,true,$( (echo a) )}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,$(`"]),
    ({"run": "{exec,true,<(echo a)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,<(echo`"]),
    ({"run": "{exec,true,>(cat -)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,>(cat`"]),
    ({"run": "{exec,true,\"$(echo \" \")\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,…`"]),
    ({"run": "{exec,true,$(echo\ntrue)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,$(echo ; true)}`", "cannot read (`{exec,true,$(echo`"]),
    ({"run": "{exec,true,\"`echo \"a b\"`\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,\"`echo \"a b\"`\"}`", "cannot read (`{exec,true,…}`"]),
    ({"run": "{exec,true,${ echo a;}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,${ echo a ; }}`", "cannot read (`{exec,true,${`"]),
    ({"run": "{exec,true,\"a;b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["cannot read (`{exec,true,\"a`"]),
    ({"run": "{exec,true,'a|b'}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["cannot read (`{exec,true,'a`"]),
    ({"run": "{exec,true,\"a&&b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["cannot read (`{exec,true,\"a`"]),
    ({"run": "{exec,true,$(:;:)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,$(: ; :)}`", "cannot read (`{exec,true,$(:`"]),
    ({"run": "{exec,true,a\\;b}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["cannot read (`{exec,true,a\\`"]),
    ({"run": "{hash,-p,/bin/true,pip-audit,\"a;b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{hash,-p,/bin/true,pip-audit,\"a`"]),
    ({"run": "{set,-n,\"a;b\"}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["cannot read (`{set,-n,\"a`"]),
    ({"run": "true\nexit${010}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${010}`"]),
    ({"run": "true\nexit${01}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${01}`"]),
    ({"run": "true\n${0:+exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${0:+exit} 0`"]),
    ({"run": "true\n${#:+exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${#:+exit} 0`"]),
    ({"run": "true\n${?:+exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${?:+exit} 0`"]),
    ({"run": "true\n${$:+exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${$:+exit} 0`"]),
    ({"run": "true\n${-:+exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${-:+exit} 0`"]),
    ({"run": "true\n${1-${2-exit}} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-${2-exit}} 0`"]),
    ({"run": "true\n${1-$@exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-$@exit} 0`"]),
    ({"run": "true\n${_:+exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${_:+exit} 0`"]),
    ({"run": "set${010} +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "${1-${2-set}} +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "${0:+set} +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "{exec,true,${x:-{a b}}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,${x:-{a`"]),
    ({"run": "{hash,-p,/bin/true,pip-audit,${x:-{a b}}}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{hash,-p,/bin/true,pip-audit,${x:-{a`"]),
    ({"run": "{exec,true,$(echo $(echo a b))}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,$(echo`"]),
    ({"run": "{exec,true,$(case a in a) echo a b;; esac)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,$(case`"]),
    ({"run": "{exec,true,<(echo a b)}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,<(echo`"]),
    ({"run": "{exec,true,$((1 + 2))}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["cannot read (`{exec,true,$((1`"]),
    ({"run": "{exec,true,`echo a\\` b`}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exec,true,`echo a\\` b`}`"]),
    ({"run": "true\n${100-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${100-exit} 0`"]),
    ({"run": "true\nexit$x 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$x 0`"]),
    ({"run": "true\nexit`:` 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit`:` 0`"]),
    ({"run": "true\ne$(:)xit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `e$(:)xit 0`"]),
    ({"run": "true\n`:`exit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with ``:`exit 0`"]),
    ({"run": "true\n${x:-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${x:-exit} 0`"]),
    ({"run": "true\nexit${0##*}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${0##*}`"]),
    ({"run": "true\nexit${#%?}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${#%?}`"]),
    ({"run": "true\nexit${?%?}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit${?%?}`"]),
    ({"run": "true\nexit${$##*}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${$##*}`"]),
    ({"run": "true\nexit${-##*}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${-##*}`"]),
    ({"run": "set$x +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "set${0##*} +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "hash${x} -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "true\n${_-exit} 0\n" + _AUDIT, "shell": "sh"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${_-exit} 0`"]),
    ({"run": "true\nexit$_ 0\n" + _AUDIT, "shell": "sh"}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit$_ 0`"]),
    ({"run": "mkdir -p o/{\"a b\",c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "mkdir -p o/{'a b',c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "mkdir -p o/{a\\ b,c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "mkdir -p o/{${HOME:+h},c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "mkdir -p o/{$(echo a),c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "mkdir -p o/{`echo a`,c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "mkdir -p o/{${x:-${y}},c}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with a brace expansion the check cannot read (`{${x:-${y}},c}`"]),
    ({"run": "echo \"{exec,true,${x:-${y}}}\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "echo \"${x:-a b}\" ${HOME:-none}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "{ echo a,b; echo c..d; }\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\n${*:-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${*:-exit} 0`"]),
    ({"run": "true\n${!:-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${!:-exit} 0`"]),
    ({"run": "true\n${1-${@}exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-${@}exit} 0`"]),
    ({"run": "true\n${1-${*}exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-${*}exit} 0`"]),
    ({"run": "true\n${1-${!}exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-${!}exit} 0`"]),
    ({"run": "true\n${1-${010}exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-${010}exit} 0`"]),
    ({"run": "true\n${1-${@%x}exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-${@%x}exit} 0`"]),
    ({"run": "true\ne$(: $((1)))xit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `e$(: $((1)))xit 0`"]),
    ({"run": "true\nexit${x:-${y}} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${x:-${y}} 0`"]),
    ({"run": "x=A; echo ${x,,}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\nexit$( (:) ) 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit$( (:) ) 0`"]),
    ({"run": "true\nexit$(:|:) 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit$(: | :) 0`"]),
    ({"run": "true\nexit$(:;:) 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit$(: ; :) 0`"]),
    ({"run": "true\nexit$(: && :) 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit$(: && :) 0`"]),
    ({"run": "true\nexit${x+;} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${x+ ; } 0`"]),
    ({"run": "true\nexit${x+|} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${x+ | } 0`"]),
    ({"run": "true\nexit`:;:` 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit`: ; :` 0`"]),
    ({"run": "true\nexit`:\\`:\\`` 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit`:\\`:\\`` 0`"]),
    ({"run": "true\nexit$( ( (:) ) ) 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit$( ( (:) ) ) 0`"]),
    ({"run": "true\n\"exit$( (:) )\" 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `\"exit$( (:) )\" 0`"]),
    ({"run": "true\nexit${ :;} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${ : ; } 0`"]),
    ({"run": "true\nexit${| :;} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${ | : ; } 0`"]),
    ({"run": "true\nexec$( (:) ) true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exec$( (:) ) true`"]),
    ({"run": "set$( (:) ) +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "set$(:|:) -n\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `set$(: | :) -n`"]),
    ({"run": "hash$( (:) ) -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "true\n${x:=exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${x:=exit} 0`"]),
    ({"run": "true\n${x=exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${x=exit} 0`"]),
    ({"run": "x=y\n${!x:-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${!x:-exit} 0`"]),
    ({"run": "true\n${a[0]:-exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${a[0]:-exit} 0`"]),
    ({"run": "true\n${0/*/exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${0/*/exit} 0`"]),
    ({"run": "true\n${0//*/exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${0//*/exit} 0`"]),
    ({"run": "${0/*/set} -n\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `${0/*/set} -n`"]),
    ({"run": "${a[0]-set} -n\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${a[0]-set} -n`"]),
    ({"run": "${x=set} +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "true\n${x/#/exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${x/#/exit} 0`"]),
    ({"run": "true\nexit${IFS}0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${IFS}0`"]),
    ({"run": "true\nexit$(echo \" \")0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit$(echo \" \")0`"]),
    ({"run": "true\nexec${IFS}true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exec${IFS}true`"]),
    ({"run": "set${IFS}+e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "hash${IFS}-p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "true\nexit$_x 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$_x 0`"]),
    ({"run": "set$_x +e\n" + _AUDIT + "\necho done"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "true\ne\\xit$x 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `e\\xit$x 0`"]),
    ({"run": "true\nex\"\"it$x 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `ex\"\"it$x 0`"]),
    ({"run": "true\n${1-exit$x} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-exit$x} 0`"]),
    ({"run": "true\n{exit$x,} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `{exit$x,} 0`"]),
    ({"run": "ha\\sh$x -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
    ({"run": "true\n${1-${001}exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-${001}exit} 0`"]),
    ({"run": "true\nexit$# 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\nexit$? 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\nexit$$ 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\nexit${x:+{}} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\n${x?exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "true\n${x:?exit} 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "echo -j$(nproc) $((1 + 2))\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "ls $HOME/x ${XDG:-/tmp}/y || true\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "v=\"$(python3 -c 'import sys; print(1)' || echo 3)\"\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "mkdir -p o/{a,b}/c\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "set -- $x\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `set -- $x`"]),
    ({"run": "echo \\${a,b}\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["cannot read (`{a,b}`"]),
    ({"run": "echo {a,b\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["cannot read (`{a,b`"]),
    ({"run": "set -- ' '\nexit$*0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$*0`"]),
    ({"run": "set -- ' '\nexit$@0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$@0`"]),
    ({"run": "set -- ' '\nexit${*}0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${*}0`"]),
    ({"run": "set -- ' '\nexit$10\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$10`"]),
    ({"run": ": ' '\nexit$_0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["shares the step with `exit$_0`"]),
    ({"run": "_x=' '\nexit${_x}0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${_x}0`"]),
    ({"run": "true\n${1-exit}${IFS}0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `${1-exit}${IFS}0`"]),
    ({"run": "true\ne`:\\`:\\``xit 0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `e`:\\`:\\``xit 0`"]),
    ({"run": "true\nset -- ' a' a; exit${*%${2}}0\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["shares the step with `exit${*%${2}}0`"]),
    ({"run": "set$(:|:) +e\n" + _AUDIT + "\ntrue"}, {}, {"docker/a/requirements.txt"}, ["not followed"]),
    ({"run": "hash$(:|:) -p /bin/true pip-audit\n" + _AUDIT}, {}, {"docker/a/requirements.txt"},
     ["a function or an alias"]),
], ids=["audit", "install-only", "named-not-run", "commented-out", "or-true", "step-continue", "job-continue",
        "set-plus-e", "conditional", "set-plus-e-in-a-comment", "after-cd", "continued-lines",
        "or-true-on-a-continuation", "piped-to-tee", "another-shell", "job-default-shell", "errexit-switched-off",
        "not-last-of-its-list", "semicolon-under-errexit", "explicit-bash-and-a-comment", "always-and-not-cancelled",
        "audit-as-a-condition", "operator-in-a-trailing-comment", "old-audit-in-a-comment-line",
        "brace-group-or-true", "trap-exit-0", "heredoc", "quoted-hash-before-or-true", "set-plus-e-as-a-condition",
        "runner-from-an-expression", "runner-from-an-expression-shell-named", "runner-named",
        "comment-line-ending-in-a-backslash", "mid-word-hash", "arithmetic-shift", "behind-a-failing-guard",
        "shopt-errexit-off", "eval-set-plus-e", "escaped-quote-then-hash", "comment-after-a-semicolon",
        "comment-after-a-closed-quote", "double-quoted-hash-before-or-true",
        "exit-0-after-the-audit", "windows-image", "self-hosted-no-os-label", "no-runs-on", "self-hosted-linux",
        "linux-and-windows-labels", "macos-image", "sent-to-the-background", "in-a-subshell-behind-or",
        "ansi-c-quote-then-sh-c", "ansi-c-quote-then-hash", "in-a-comment-after-a-multiline-quote",
        "set-plus-e-on-a-hash-line-inside-a-quote", "guard-and-and-at-a-line-end", "or-at-a-line-end",
        "and-and-at-a-line-end", "audit-function", "audit-alias", "backslash-in-single-quotes-then-hash",
        "expression-label-in-a-list", "exit-split-by-a-continuation", "set-plus-e-split-by-a-continuation",
        "set-of-a-backquoted-command", "alias-through-quotes", "function-named-through-quotes", "function-keyword",
        "ansi-c-quote-hides-a-hash", "ansi-c-quote-across-lines", "ansi-c-quote-before-a-background",
        "hash-inside-backquotes", "hash-after-a-substitution", "hash-after-a-subshell",
        "exit-after-a-pipe-amp-under-lastpipe",
        "no-break-space-in-the-audit", "expression-in-the-script", "bash-env-on-the-step", "bash-env-on-the-job",
        "env-not-a-mapping", "quote-in-a-subshell-comment", "backquote-in-a-subshell-comment",
        "vertical-tab-before-a-hash", "ideographic-space-before-a-hash", "expression-in-a-comment",
        "audit-commented-after-a-subshell", "audit-commented-after-an-arithmetic-command", "hash-stands-in",
        "bash-cmds-stands-in", "bash-aliases-stands-in", "enable-stands-in", "eval-rejoins-an-exit",
        "set-of-a-variable-flag", "set-of-a-glob", "set-of-a-question-mark-glob", "set-of-a-star-glob",
        "imported-function-on-the-step", "shellopts-on-the-job",
        "bashopts-on-the-job", "path-on-the-step", "env-file-on-the-step", "env-keys-that-only-look-like-them",
        "job-container", "hash-in-a-subshell", "hash-glued-to-a-redirect", "alias-glued-to-a-redirect",
        "bash-cmds-inside-a-word", "set-of-a-brace-expansion", "set-plus-of-a-brace-expansion",
        "set-plus-of-a-brace-expansion-in-a-closed-if", "set-of-an-extended-glob", "set-t", "set-o-onecmd",
        "shopt-noexec", "shopt-onecmd", "shopt-of-a-variable",
        "eval-rejoins-through-double-quotes", "eval-rejoins-through-juxtaposed-quotes",
        "comment-in-a-quote-nested-in-a-substitution", "apostrophe-in-double-quotes-before-a-continuation",
        "nul-inside-an-exit", "soh-inside-an-exit",
        "reader-mark-in-the-script", "subshell-hash-split-by-a-continuation",
        "ps4-on-the-step", "ps4-on-the-job", "pythonpath-on-the-step", "pythonhome-on-the-job",
        "pythonuserbase-on-the-step", "ld-preload-on-the-job", "ld-library-path-on-the-step",
        "job-container-image-only", "exit-glued-to-a-locale-string", "locale-string-before-hash",
        "hash-glued-to-a-locale-string", "set-n-glued-to-a-locale-string", "set-plus-e-glued-to-a-locale-string",
        "shopt-noexec-of-a-brace-expansion", "shopt-noexec-of-a-backquoted-command", "shopt-noexec-of-a-bracket-glob",
        "shopt-noexec-of-a-star-glob", "shopt-noexec-of-a-question-mark-glob", "shopt-noexec-of-a-extended-glob",
        "shopt-errexit-of-a-brace-expansion", "shopt-errexit-of-a-backquoted-command",
        "shopt-errexit-of-a-bracket-glob", "shopt-errexit-of-a-star-glob", "shopt-errexit-of-a-question-mark-glob",
        "shopt-errexit-of-a-extended-glob", "ld-audit-on-the-job",
        "exit-built-by-a-brace-expansion", "hash-built-by-a-brace-expansion",
        "set-n-built-by-a-brace-expansion", "command-exit-built-by-a-brace-expansion",
        "set-plus-e-built-by-a-brace-expansion-in-a-closed-if", "exit-built-by-a-brace-sequence",
        "exit-built-by-two-brace-sequences",
        "set-plus-e-built-by-a-brace-sequence-in-a-closed-if", "shopt-errexit-of-a-brace-list-with-a-quoted-space",
        "set-plus-e-of-a-brace-list-with-a-quoted-space", "path-in-another-case-on-the-job",
        "exit-glued-to-an-empty-parameter", "empty-parameter-glued-to-exit", "exit-glued-to-a-star-parameter",
        "exit-glued-to-the-last-background-pid", "exit-glued-to-a-positional-parameter",
        "positional-parameter-nine-glued-to-exit", "exit-glued-to-a-braced-empty-parameter",
        "hash-glued-to-an-empty-parameter", "set-plus-e-glued-to-an-empty-parameter",
        "exit-spelled-through-a-locale-string",
        "brace-list-in-a-message-and-a-path", "brace-sequence-in-a-message-and-a-path", "locale-strings-in-messages",
        "special-parameters-in-messages", "exec-built-by-a-brace-list-with-a-quoted-space",
        "hash-built-by-a-brace-list-with-a-quoted-space", "exec-built-by-a-brace-list-with-an-empty-brace",
        "exit-built-by-a-brace-list-with-a-braced-parameter", "exec-built-by-a-brace-list-with-a-substitution",
        "exec-built-by-a-brace-list-with-a-nested-brace", "exec-built-by-a-brace-list-with-a-backquoted-command",
        "exit-built-by-the-third-item-of-a-brace-expansion", "exit-built-by-a-brace-expansion-with-an-empty-item",
        "exit-built-by-three-brace-sequences", "exit-glued-to-a-braced-star-parameter",
        "exit-glued-to-the-braced-last-background-pid", "exit-glued-to-a-braced-positional-parameter",
        "exit-glued-to-an-empty-parameter-with-a-default", "exit-glued-to-the-tenth-parameter",
        "exit-as-the-default-of-an-empty-parameter", "set-plus-e-glued-to-the-tenth-parameter",
        "find-braces-then-a-brace-list", "parameters-with-operators-in-a-message", "brace-list-in-an-assignment",
        "brace-list-in-an-assignment-before-a-command", "brace-list-in-a-redirect-target",
        "brace-list-in-a-double-quoted-message", "brace-list-in-a-single-quoted-argument",
        "exec-built-by-a-brace-list-with-a-single-quoted-space", "exec-built-by-a-brace-list-with-an-escaped-space",
        "exec-built-by-a-brace-list-with-an-escaped-quote", "exec-built-by-a-brace-list-with-a-quoted-closing-brace",
        "exec-built-by-a-brace-list-with-a-default-holding-a-space", "set-n-built-by-a-brace-list-with-a-quoted-space",
        "trap-built-by-a-brace-list-with-a-quoted-space", "exit-as-the-colon-default-of-an-empty-parameter",
        "exit-glued-to-an-empty-parameter-with-another-operator",
        "exec-built-by-a-brace-list-around-a-brace-holding-a-braced-parameter",
        "hash-built-by-a-brace-list-around-a-brace-holding-a-quoted-comma",
        "exec-built-by-a-brace-list-around-a-brace-holding-an-escaped-comma",
        "exec-built-by-a-brace-list-around-a-brace-holding-a-substitution",
        "set-plus-e-built-by-a-brace-list-around-a-brace-holding-a-braced-parameter",
        "exit-as-the-default-of-the-tenth-parameter", "substitution-holding-a-comma-inside-a-brace",
        "exec-built-by-a-brace-list-with-a-nested-default",
        "exec-built-by-a-brace-list-with-a-nested-default-holding-a-space",
        "exec-built-by-a-brace-list-with-a-default-holding-a-quoted-closing-brace",
        "exec-built-by-a-brace-list-with-a-nested-substitution",
        "exec-built-by-a-brace-list-with-a-subshell-in-a-substitution",
        "exec-built-by-a-brace-list-with-a-process-substitution",
        "exec-built-by-a-brace-list-with-an-output-process-substitution",
        "exec-built-by-a-brace-list-with-a-quote-in-a-quoted-substitution",
        "exec-built-by-a-brace-list-with-a-substitution-over-two-lines",
        "exec-built-by-a-brace-list-with-quotes-in-a-quoted-backquoted-command",
        "exec-built-by-a-brace-list-with-a-command-in-a-braced-substitution",
        "exec-built-by-a-brace-list-with-a-quoted-semicolon", "exec-built-by-a-brace-list-with-a-single-quoted-pipe",
        "exec-built-by-a-brace-list-with-a-quoted-and", "exec-built-by-a-brace-list-with-a-semicolon-in-a-substitution",
        "exec-built-by-a-brace-list-with-an-escaped-semicolon", "hash-built-by-a-brace-list-with-a-quoted-semicolon",
        "set-n-built-by-a-brace-list-with-a-quoted-semicolon", "exit-glued-to-the-tenth-parameter-with-a-leading-zero",
        "exit-glued-to-the-first-parameter-with-a-leading-zero", "exit-as-the-alternative-of-the-shell-name",
        "exit-as-the-alternative-of-the-parameter-count", "exit-as-the-alternative-of-the-last-status",
        "exit-as-the-alternative-of-the-shell-pid", "exit-as-the-alternative-of-the-flags", "exit-as-a-nested-default",
        "exit-glued-to-an-empty-parameter-in-a-default", "exit-as-the-alternative-of-the-last-argument",
        "set-plus-e-glued-to-the-tenth-parameter-with-a-leading-zero", "set-plus-e-as-a-nested-default",
        "set-plus-e-as-the-alternative-of-the-shell-name", "exec-built-by-a-brace-list-with-a-default-holding-a-brace",
        "hash-built-by-a-brace-list-with-a-default-holding-a-brace",
        "exec-built-by-a-brace-list-with-a-nested-substitution-holding-a-space",
        "exec-built-by-a-brace-list-with-a-case-in-a-substitution",
        "exec-built-by-a-brace-list-with-a-process-substitution-holding-a-space",
        "exec-built-by-a-brace-list-with-an-arithmetic-substitution",
        "exec-built-by-a-brace-list-with-an-escaped-backquote", "exit-as-the-default-of-the-hundredth-parameter",
        "exit-glued-to-a-named-variable", "exit-glued-to-a-backquoted-command", "exit-split-by-a-substitution",
        "backquoted-command-glued-before-exit", "exit-as-a-named-default", "exit-glued-to-a-stripped-shell-name",
        "exit-glued-to-a-stripped-parameter-count", "exit-glued-to-a-stripped-last-status",
        "exit-glued-to-a-stripped-shell-pid", "exit-glued-to-the-stripped-flags",
        "set-plus-e-glued-to-a-named-variable", "set-plus-e-glued-to-a-stripped-shell-name",
        "hash-glued-to-a-braced-variable", "exit-as-the-default-of-the-last-argument-under-sh",
        "exit-glued-to-the-last-argument-under-sh", "brace-list-with-a-double-quoted-item",
        "brace-list-with-a-single-quoted-item", "brace-list-with-an-escaped-space-item",
        "brace-list-with-a-braced-parameter-item", "brace-list-with-a-substitution-item",
        "brace-list-with-a-backquoted-item", "unreadable-brace-list-in-a-mkdir",
        "unreadable-brace-list-in-a-quoted-message", "named-defaults-in-a-message", "commas-and-dots-in-a-brace-group",
        "exit-as-the-default-of-the-star-parameter", "exit-as-the-default-of-the-last-background-pid",
        "exit-glued-to-a-braced-empty-parameter-in-a-default", "exit-glued-to-a-braced-star-parameter-in-a-default",
        "exit-glued-to-the-braced-last-background-pid-in-a-default",
        "exit-glued-to-a-braced-parameter-with-a-leading-zero-in-a-default",
        "exit-glued-to-a-stripped-empty-parameter-in-a-default",
        "exit-split-by-a-substitution-holding-an-arithmetic-one",
        "exit-glued-to-a-nested-named-default", "case-modification-in-a-message",
        "exit-glued-to-a-subshell-substitution", "exit-glued-to-a-piped-substitution",
        "exit-glued-to-a-listed-substitution", "exit-glued-to-an-and-list-substitution",
        "exit-glued-to-an-alternative-holding-a-semicolon", "exit-glued-to-an-alternative-holding-a-pipe",
        "exit-glued-to-a-listed-backquoted-command", "exit-glued-to-a-nested-backquoted-command",
        "exit-glued-to-a-doubly-nested-subshell-substitution", "exit-glued-to-a-quoted-subshell-substitution",
        "exit-glued-to-a-current-shell-substitution", "exit-glued-to-a-value-substitution",
        "exec-glued-to-a-subshell-substitution", "set-plus-e-glued-to-a-subshell-substitution",
        "set-n-glued-to-a-piped-substitution", "hash-glued-to-a-subshell-substitution",
        "exit-as-an-assigned-colon-default", "exit-as-an-assigned-default", "exit-as-an-indirect-colon-default",
        "exit-as-a-subscripted-colon-default", "exit-as-a-pattern-replacement-of-the-shell-name",
        "exit-as-a-global-pattern-replacement-of-the-shell-name", "set-n-as-a-pattern-replacement-of-the-shell-name",
        "set-n-as-a-subscripted-default", "set-plus-e-as-an-assigned-default",
        "exit-as-the-anchored-replacement-of-an-unset-variable", "exit-glued-to-the-field-separator",
        "exit-glued-to-a-substitution-that-prints-a-space", "exec-glued-to-the-field-separator",
        "set-plus-e-glued-to-the-field-separator", "hash-glued-to-the-field-separator",
        "exit-glued-to-a-name-that-starts-with-an-underscore",
        "set-plus-e-glued-to-a-name-that-starts-with-an-underscore", "exit-escaped-and-glued-to-a-named-variable",
        "exit-split-by-a-quote-pair-and-glued-to-a-named-variable", "exit-glued-to-a-named-variable-in-a-default",
        "exit-glued-to-a-named-variable-in-a-brace-list", "hash-escaped-and-glued-to-a-named-variable",
        "exit-glued-to-a-braced-parameter-with-two-leading-zeros-in-a-default", "exit-glued-to-the-parameter-count",
        "exit-glued-to-the-last-status", "exit-glued-to-the-shell-pid",
        "exit-glued-to-an-alternative-holding-a-brace-pair", "exit-as-the-error-word-of-an-unset-variable",
        "exit-as-the-colon-error-word-of-an-unset-variable", "substitution-and-arithmetic-in-arguments",
        "variables-and-a-default-in-arguments", "substitution-holding-a-list-in-a-quoted-assignment",
        "brace-list-in-a-path", "positional-parameters-set-from-a-variable", "escaped-dollar-before-a-brace-list",
        "brace-list-left-open",
        "exit-glued-to-the-arguments-a-set-gives-a-space", "exit-glued-to-the-argument-list-a-set-gives-a-space",
        "exit-glued-to-the-braced-arguments-a-set-gives-a-space",
        "exit-glued-to-a-positional-parameter-a-set-gives-a-space",
        "exit-glued-to-the-last-argument-a-command-leaves-a-space",
        "exit-glued-to-a-braced-name-that-starts-with-an-underscore", "exit-as-a-default-glued-to-the-field-separator",
        "exit-split-by-a-backquoted-command-holding-escaped-ones", "exit-glued-to-the-arguments-stripped-by-another",
        "set-plus-e-glued-to-a-piped-substitution", "hash-glued-to-a-piped-substitution"])
def test_the_pip_audit_check_tells_a_live_audit_from_a_dead_one(step, job, read, expected):
    """The pip-audit check reads the files of a live `pip-audit` run (after a `cd … &&`, `--requirement=` too, and
    across backslash continuations), reads nothing from a commented-out run, the step that installs pip-audit or a
    command that only names it, and flags a run that cannot fail its job: `|| true` (on a continuation line too, and
    after a quoted `#`, which is no comment), a pipe after it (the default `bash -e {0}` has no pipefail, so `| tee`
    hides the audit), a shell other than the default, `bash` or `sh` (named by the step or the job's defaults), a later
    command after `set +e` or `set +o errexit` (as an `if` condition too), `&& …` before a later line (errexit spares a
    command that is not the last of its list), a `;` inside a brace group under `|| true`, a `trap`, a heredoc, an `if`
    condition, `continue-on-error`, an `if:` other than `always()`, `success()` or `!cancelled()`, and no named shell
    under a `runs-on` an expression gives. It passes a later line under the default errexit, a comment — trailing or a
    line of its own — that holds an operator or an old audit (the shell never runs it), and an expression runner with a
    named shell or a named runner; a comment line ending in a backslash continues nothing (the shell does not join it),
    so the audit on the next line is read; a `#` inside a word opens no comment, so a later `|| true` is still read; and
    an arithmetic `$((1 << 2))` is no heredoc. It also flags an audit behind a guard (`test -f nope && pip-audit …`
    before a later line: a failing guard skips the audit and errexit spares the guard), a `shopt -uo errexit` or an
    `eval 'set +e'` before a later line, a `|| true` after an escaped quote and a `#` (`\\"` opens no quote, so that `#`
    is inside the string, no comment), an `exit 0` after the audit (sound, but refused), and no named shell under a
    Windows image, labels with no OS label or a Windows label beside a Linux one, or no `runs-on` at all; it passes a
    comment after a `;` or a closed quote that holds `|| true` or `set +e`, a self-hosted Linux runner and a macOS
    image; and it flags a `|| true` after a double-quoted `"see #1"`. Fold 5's reads added: an audit a lone `&` sends to
    the background, one in a subshell behind `||`, one in an `sh -c '…'` or after a `#` that an ANSI-C `$'…'` holds, one
    sharing the step with a function or an alias named `pip-audit`, and no named shell under a label list holding an
    expression; a step's script is read whole, so a quote open across a newline holds the `#` and `set +e` inside it,
    and a line ending in `&&` or `||` joins the next (a guard or an `||` there is read), while it reads no audit from a
    comment after a quote that spans two lines, passes `cd . &&` before the audit on the next line, and passes a `#`
    after a single-quoted `'x\\'` (a backslash is literal there, so the `|| true` after it is comment). Fold 6's mutants
    added: an `exit 0` and a `set +e` that a backslash-newline splits inside a word (the shell removes it, so the word
    is whole). Fold 6's reads added: a `set` given a backquoted command; an alias or a function named through quotes,
    and one defined with `function`; an ANSI-C `$'…'` in the audit's list (it hides a `#`, a quote's end, a lone `&`:
    dash and bash read it differently); a `#` inside backquotes or after a `$(…)` (no comment: the `|| true` after it is
    read; fold 7's reads refuse a `#` right after any `)`, below); an `exit 0` after a `|&`; a no-break space in the
    audit (the check splits the word there, bash does not); a `${{` expression in the script (GitHub writes its value in
    first); and a `BASH_ENV`, or an env that is not a mapping, on the step or the job (bash runs that file first). Fold
    7's reads added: a `#` right after a subshell's or an arithmetic command's `)` (bash comments out what follows, the
    audit too; a quote or a backquote in that comment too), a VT or an ideographic space before a `#`, a `${{`
    expression in a comment, a `hash`, `BASH_CMDS`, `BASH_ALIASES` or `enable` entry for `pip-audit`, an `eval` that
    rejoins an `exit 0` split by a backslash-newline inside single quotes, a `set` given a variable flag or a glob (`set
    -$x`, `set +[e]`, `set +?`, `set +*`), an imported function, `SHELLOPTS`, `BASHOPTS`, `PATH` or `ENV` in the env,
    and a `container:` on the job; it passes env keys that only look like those (`ENVIRONMENT`, `XBASH_ENV`). Fold 8's
    reads added: a `hash` after a `(` or glued to a redirect, an `alias` glued to one, a `BASH_CMDS` entry inside a word
    (`: ${BASH_CMDS[…]:=…}`); a `set` given a brace expansion (inside a closed `if` too) or an extended glob, `set -t`,
    `set -o onecmd`, `shopt -os noexec`, `shopt -so onecmd` and a `shopt` given a variable; an `eval` whose string a
    backslash-newline inside double quotes or between juxtaposed quotes rejoins, and a comment in a quote nested in a
    `"$(…)"` (each a newline inside a quoted string, which replaces the single-quote rule fold 8 wrote); a NUL, or one
    of the check's own marks, in the script; a `PS4`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONUSERBASE`, `LD_PRELOAD` or
    `LD_LIBRARY_PATH` in the env; a `container:` that names only an image; and a `)#` a backslash-newline splits. It
    passes an apostrophe inside double quotes on a line a backslash continues. Fold 9's read added: a `shopt` given each
    other shape of its class (a brace expansion, a backquoted command, a bracket, star, question-mark or extended glob),
    naming noexec or errexit; a locale string's `$` glued to `exit`, `hash` or `set` (`exit$""`, `$""hash`, `hash$""`,
    `set$"" -n`, `set$"" +e`: bash expands `$""` to nothing); a NUL on a line after the first (bash drops it inside a
    word; on the first line it refuses the script, loudly); and an `LD_AUDIT` in the env. Fold 10's mutant run added a
    SOH inside an `exit` on a line after the first, so a class cut to a NUL and STX fails. Its read A added: a command
    word a brace expansion builds, from a list or a sequence (`{exit,0}`, `command {exit,0}`, `{e..e}xit 0`,
    `{hash,-p,/bin/true,pip-audit}`, `{set,-n}`, and `{set,+e}` or `{s..s}et +e` in a closed `if`); a `shopt` given a
    brace list holding a quoted space (`shopt -uo err{exit,"a b"}`); and an env key in another case (`Path`, which a
    Windows runner reads as `PATH`). It passes a brace expansion in a message or a path (`echo {a,b}`, `mkdir -p
    out/{1..3}`) and a locale string in a message (`echo $"hello"`, `echo "cost: $"`). Its read B added: a special
    parameter the script starts with empty glued to `exit`, `hash` or `set` (`exit$@`, `$@exit 0`, `exit$*`, `exit$!`,
    `exit$1`, `$9exit 0`, `exit${@}`, `hash$@ …`, `set$@ +e`: a step's script is given no arguments, so each is dead)
    and a locale string that is not empty (`$"ex"it 0`: bash expands it to its text); and the two extended-glob `shopt`
    rows turn `extglob` on first (without it bash refuses them as it parses, loudly). It passes those parameters in a
    message (`echo "$@" $1 $* $!`, `echo "cost: $1"`). Fold 11's mutant run added `{e..e}x{i..i}t 0`, and fold 11 a
    `set` given a brace list holding a quoted space (`set {+e,"a b"}`). Fold 11's reads added: a command word a brace
    list builds from an item holding a quoted or escaped space or a quoted `}`, a substitution, a backquoted command, a
    braced parameter, or an empty or nested brace (`{exec,true,"a b"}`, `{hash,-p,/bin/true,pip-audit,"a b"}`,
    `{exec,true,$(echo a b)}`, `` {exec,true,`echo a b`} ``, `{exit,${@}}`, `{exec,true,{}}`, `{exec,true,{{}}}`,
    `{exec,true,'a b'}`, `{exec,true,a\\ b}`, `{exec,true,"a\\" b"}`, `{exec,true,"}"}`, `{exec,true,${x:-a b}}`, and a
    `set -n` or a `trap` one builds the same way, `{set,-n,"a b"}`, `{trap,"ex""it 0",EXIT}`), from a list's third or
    empty item (`{,,exit,0}`, `{exit,} 0`) or from three sequences (`{e..e}{x..x}{i..i}t 0`); and a special parameter
    braced (`exit${*}`, `exit${!}`, `exit${1}`), past `$9` (`exit${10}`, `set${10} +e`) or with a `-` or `:-` default
    (`exit${@:-}`, `${1-exit} 0`, `${@:-exit} 0`) or another operator (`exit${@#x}`): bash expands a brace before a
    parameter, and both before it runs the command word. They pass `find`'s `{}` before a brace list and those
    parameters in a message (`echo "${1:-none}" ${10} ${@:-all}`), and refuse, sound, a brace list in an assignment's
    value, a redirect's target or a quoted string (`TARGETS={exit,0}`, `x={exit,0} true`, `echo hi >{exit,}`, `echo
    "mode: {exec,dry-run}"`, `jq '{hash,set}'`). Fold 12's mutant run added a command word a brace list builds around a
    brace that holds no list, only a braced parameter, a quoted or escaped comma or a substitution
    (`{exec,true,{${x}}}`, `{hash,-p,/bin/true,pip-audit,{"a,b"}}`, `{exec,true,{\\,}}`, `{exec,true,{$(echo a)}}`, and
    `{set,+e,{${x}}}`, which bash expands to `set +e {}`), and the tenth parameter's default (`${10-exit} 0`); and it
    passes a substitution holding a comma inside a brace (`echo {x$(:,exit)}`: bash's brace_gobbler skips the whole
    `$(…)`, so the brace holds no comma and is kept). Fold 12's reads added: a command word a brace list builds from an
    item the view reads one level deep only, refused as a brace the check cannot read (a `${…}` or `$(…)` nested in
    another, `{exec,true,${x:-${y}}}`, `{exec,true,${x:-{a b}}}`, `{exec,true,$(echo $(echo a b))}`; a quoted `}` in a
    default, `{exec,true,${x:-"}"}}`; a `$( (…) )`, a `<(…)` or `>(…)`, a `$((…))`, a `case` or a line break inside a
    `$(…)`, quotes inside a quoted backquoted command, or bash 5.3's `${ cmd;}`), or from one an operator inside a quote
    or a substitution cuts (`{exec,true,"a;b"}`, `{exec,true,'a|b'}`, `{exec,true,"a&&b"}`, `{exec,true,$(:;:)}`,
    `{exec,true,a\\;b}`, and `hash` and `set -n` built so); a backquoted command holding an escaped backquote, which the
    view now reads whole (`` {exec,true,`echo a\\` b`} ``); and a special parameter with a leading zero or past `$99`,
    or given an operator, read as its word under `-`, `:-`, `+` or `:+` and removed under any other (`exit${010}`,
    `exit${01}`, `${100-exit} 0`, `${0:+exit} 0`, `${#:+exit} 0`, `${?:+exit} 0`, `${$:+exit} 0`, `${-:+exit} 0`,
    `${_:+exit} 0`, `${1-${2-exit}} 0`, `${1-$@exit} 0`, and `set` built the same ways before `+e`). Fold 13 added: an
    expansion that can come out empty glued to a command word or splitting it (`exit$x 0`, `` exit`:` 0 ``, `e$(:)xit
    0`, `` `:`exit 0 ``, `exit${0##*}`, `exit${#%?}`, `exit${?%?}`, `exit${$##*}`, `exit${-##*}`, `set$x +e`,
    `set${0##*} +e`, `hash${x} -p …`), a named parameter's default (`${x:-exit} 0`), and, under `shell: sh`, `$_`, which
    dash starts unset (`${_-exit} 0`, `exit$_ 0`). It passes each kind of piece a brace list's item may hold (`mkdir -p
    o/{"a b",c}`, `o/{'a b',c}`, `o/{a\\ b,c}`, `o/{${HOME:+h},c}`, `o/{$(echo a),c}`, `` o/{`echo a`,c} ``), a brace
    the check cannot read inside a quoted message, named defaults in a message (`echo "${x:-a b}" ${HOME:-none}`), and
    commas and dots in a brace group (`{ echo a,b; echo c..d; }`); it refuses a brace list it cannot read in a `mkdir`
    (`mkdir -p o/{${x:-${y}},c}`: sound, since bash builds no command word from it). Fold 13's mutants added: `$*` and
    `$!` given a default (`${*:-exit} 0`, `${!:-exit} 0`), a braced special parameter glued to the word of another's
    default (`${1-${@}exit} 0`, `${1-${*}exit} 0`, `${1-${!}exit} 0`, `${1-${010}exit} 0`, `${1-${@%x}exit} 0`), a
    substitution holding an arithmetic one (`e$(: $((1)))xit 0`) and a named default holding another (`exit${x:-${y}}
    0`); and it passes a case modification (`x=A; echo ${x,,}`). Fold 13's reads added: an expansion holding a `(`
    (`exit$( (:) ) 0`, `exit$( ( (:) ) ) 0`, `"exit$( (:) )" 0`, `exec$( (:) ) true`, `set$( (:) ) +e`, `hash$( (:) ) -p
    …`), one `commands` cuts at an operator inside it (`exit$(:|:) 0`, `exit$(:;:) 0`, `exit$(: && :) 0`, `exit${x+;}
    0`, `exit${x+|} 0`, `` exit`:;:` 0 ``, `set$(:|:) -n`), a backquoted command holding an escaped one (``
    exit`:\\`:\\`` 0 ``), and bash 5.3's `exit${ :;} 0` and `exit${| :;} 0`; a parameter's word under `=`, `:=` or a
    pattern replacement, or of an indirect or subscripted one (`${x:=exit} 0`, `${x=exit} 0`, `${0/*/exit} 0`,
    `${0//*/exit} 0`, `${!x:-exit} 0`, `${a[0]:-exit} 0`, `${0/*/set} -n`, `${a[0]-set} -n`, `${x=set} +e`, and
    `${x/#/exit} 0`, sound with `x` unset but refused); an expansion that splits a word in two (`exit${IFS}0`,
    `exit$(echo " ")0`, `exec${IFS}true`, `set${IFS}+e`, `hash${IFS}-p …`); `$_` glued to a name (`exit$_x 0`, `set$_x
    +e`); a word that reads as a command word only in `_plain`'s view, glued to an expansion (`e\\xit$x 0`, `ex""it$x
    0`, `${1-exit$x} 0`, `{exit$x,} 0`, `ha\\sh$x -p …`); a braced parameter with two leading zeros (`${1-${001}exit}
    0`); and, refused although sound, `set -- $x`, `echo \\${a,b}` and `echo {a,b`. It passes `exit$# 0`, `exit$? 0` and
    `exit$$ 0` (each a command not found), `exit${x:+{}} 0`, `${x?exit} 0` and `${x:?exit} 0` (each fails the step), and
    arguments holding a substitution, an arithmetic expansion, a default or a brace list (`echo -j$(nproc) $((1 + 2))`,
    `ls $HOME/x ${XDG:-/tmp}/y || true`, `v="$(python3 -c 'import sys; print(1)' || echo 3)"`, `mkdir -p o/{a,b}/c`).
    Fold 14 added: a special parameter `_plain` removes as empty, read as a space too (`exit$*0`, `exit$@0`, `exit${*}0`
    and `exit$10` after `set -- ' '`, `exit$_0` after `: ' '`, `exit${_x}0` after `_x=' '`), and a default glued to the
    field separator (`${1-exit}${IFS}0`). Fold 14's mutants added: a command word a backquoted command holding escaped
    ones splits (`` e`:\\`:\\``xit 0 ``), `$*` stripped of a pattern another parameter gives (`exit${*%${2}}0` after
    `set -- ' a' a`, a space), and a `set +e` or a `hash` glued to a piped substitution (`set$(:|:) +e`, `hash$(:|:) -p
    …`). Each row's job runs on ubuntu-latest unless the row names another. Guards a
    check that counts a dead audit as coverage (T29's fold-2 read, S1: PA1, PA2, PA4, PA5, Y7, Y8; N3; its fold-3 read,
    S2: Z7; N2 and N5: PA-trap, PA-heredoc-body, PA-windows-default-shell, PA-set+e-in-if, PA-quoted-hash-hides-or; fold
    4's mutants: W58, Q2, H2; its read, S1: A-a; N1: E-a; N5: A-b, A-c, A-d; N6: A-f; fold 5's read A, S1, S2, S5, N2,
    N4: A7, B7, F2, F3, H1 to H5, K7, K8; its read B, N8: UC1, RN8; fold 6's mutant W56; its read A, S2, S4, S5, S7, N3;
    its read B, S2 to S5, N4: H1 to H5, FA2; fold 7's read A, S1 to S5; its read B, S1, S2, N6; fold 8's read A, S1 to
    S5, N1: M1 to M13; its read B, S1 to S4, N2, N3, N6, N7: CT1, CT2, SI2, MK2; fold 9's mutant X9a; its read B, S1,
    S2, N1, N8; fold 10's mutant run: B9-control-class-nul-and-stx-only; its read A, S1, N1; its read B, S1, S2, N1, N2;
    fold 11's mutant X11h; its read A, S1, S2, N1: A10; its read B, S1, S2, N6, N7; fold 12's mutant run: X12u, X12aa;
    its read A (interim, no verdict): b01 to b22, c01 to c09, s01 to s37; its read B, S1, S2; fold 13's mutant run: X12r
    to X12t, X12y, X13e, X13f, X13q, X13t, X13y, X13G; its read A, S1 to S3, N1, N3: a01 to a28, j01 to j06, p01, k01 to
    k05, q01, q02, t08 to t10; its read B, S1, S2, N1; fold 14's mutant run: X14b, X14e, X14m, X14o)."""
    audited, faults = pip_audit_faults({"jobs": {"a": dict({"runs-on": "ubuntu-latest"}, **job, steps=[step])}})
    assert audited == read
    assert len(faults) == len(expected) and all(e in f for e, f in zip(expected, faults)), faults


def test_the_pip_audit_check_reads_a_long_word_after_a_brace_at_once():
    """The pip-audit check reads a step whose word opens a brace and then holds forty command substitutions with no
    comma or closing brace after them (`echo {$(a)$(a)…`) at once, in a fresh interpreter given a minute: each piece
    of a brace's item is read once, whole where it can be (tests/unit/_build_read.py, `_PIECE`). Guards a piece
    pattern that tries each way of cutting a `$(…)` (whole, or one character at a time) before it gives up, which
    doubles its time with each one, so forty would hang every suite that reads a workflow or a Dockerfile (T29's fold-12
    mutant run, X12ab; its read B, N1, N2)."""
    step = {"run": "echo {" + "$(a)" * 40 + "\n" + _AUDIT}
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import test_ci_workflows as t; "
            "print(t.pip_audit_faults({'jobs': {'a': {'runs-on': 'ubuntu-latest', 'steps': [%r]}}}))" % step)
    here = os.path.dirname(os.path.abspath(__file__))
    # `-B`: the child writes no bytecode beside the tests, which a mutant harness's copy would then reuse
    r = subprocess.run([sys.executable, "-B", "-c", code, here], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "({'docker/a/requirements.txt'}, [])", (r.stdout, r.stderr)


@pytest.mark.parametrize("shell,runs_on,expected", [
    ("pwsh", "ubuntu-latest", ["shell: pwsh"]),
    ("bash", _RUNNER, []),
], ids=["workflow-default-pwsh", "workflow-default-bash-under-an-expression-runner"])
def test_the_pip_audit_check_reads_the_workflows_default_shell(shell, runs_on, expected):
    """A workflow-level `defaults.run.shell` is the shell of a step that names none and whose job names none: the
    check flags a workflow default of pwsh, and takes a workflow default of bash as a named shell under a runner an
    expression picks. Guards a reader that looks only at the step and the job (T29's fold-3 read, N6: V1)."""
    workflow = {"defaults": {"run": {"shell": shell}}, "jobs": {"a": {"runs-on": runs_on, "steps": [{"run": _AUDIT}]}}}
    audited, faults = pip_audit_faults(workflow)
    assert audited == {"docker/a/requirements.txt"}
    assert len(faults) == len(expected) and all(e in f for e, f in zip(expected, faults)), faults


@pytest.mark.parametrize("env", [{"BASH_ENV": "/tmp/e"}, "${{ fromJSON(vars.E) }}",
                                 {"PS4": "${BASH_CMDS[pip-audit]:=/bin/true}"}],
                         ids=["bash-env", "not-a-mapping", "ps4"])
def test_the_pip_audit_check_reads_the_workflows_env(env):
    """A `BASH_ENV` in the workflow's `env:`, another key `_BASH_ENV` lists (a `PS4`, which a traced step expands
    into a `BASH_CMDS` entry for `pip-audit`), or a workflow env that is not a mapping (an expression can hold one),
    is refused for a pip-audit step: bash runs the file it names before every step's script, where it can define a
    `pip-audit` function or switch errexit off unread. Guards a check that reads the step's and the job's env but
    not the workflow's, or only `BASH_ENV` there (T29's fold-6 read A, N3; fold 8's read B, N3: EV1)."""
    workflow = {"env": env, "jobs": {"a": {"runs-on": "ubuntu-latest", "steps": [{"run": _AUDIT}]}}}
    audited, faults = pip_audit_faults(workflow)
    assert audited == {"docker/a/requirements.txt"}
    assert len(faults) == 1 and "`BASH_ENV`" in faults[0] and "workflow" in faults[0], faults


def test_third_party_actions_are_sha_pinned():
    """Every `uses:` of a third-party action (publisher other than actions/ or github/, and not a local path) in any
    workflow is pinned to a full 40-hex commit SHA with a version comment — the .github/zizmor.yml policy. The
    docker/* actions run in the one job that can push to the package namespace; a mutable tag there is the
    supply-chain hole the policy exists for, and the zizmor scan only FLAGS it (advisory)."""
    bad = []
    for fn in sorted(os.listdir(WF)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        for n, line in enumerate(_read(os.path.join(".github", "workflows", fn)).splitlines(), 1):
            m = re.search(r"uses:\s*([^\s#]+)", line)
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith(("actions/", "github/", "./", "docker://")):
                continue
            if not re.search(r"@[0-9a-f]{40}\s+#\s*v\d", line):
                bad.append("%s:%d %s" % (fn, n, ref))
    assert bad == [], "third-party actions must be SHA-pinned with a version comment: %s" % bad


# --- the gate wiring (2026-10-08 review, finding 18 / N18) ---------------------------------------------------

GATING_WORKFLOWS = ("ci.yml", "pii-guard.yml", "security.yml", "zizmor.yml")


def _triggers(fn):
    doc = yaml.safe_load(_read(os.path.join(".github", "workflows", fn)))
    return doc.get("on") or doc.get(True)   # PyYAML reads the bare key `on` as the boolean True


def test_gating_workflows_run_on_pushes_to_main_and_the_homelab_tine():
    """Every gating workflow runs on a push to `main` AND to any `homelab*` branch (plus pull requests). The
    permissive `homelab` tine is a long-lived branch that is never merged to `main`; for months its only CI came
    from a never-merge pull request against `main`, which on a repository without branch protection is one mis-click
    from landing it (2026-10-08 review, finding 18 / N18). The push trigger gates the tine with no PR at all; a public
    repository has no `homelab*` branch, so the entry is inert there. Guards the trigger being tidied back to main."""
    for fn in GATING_WORKFLOWS:
        trig = _triggers(fn)
        branches = (trig.get("push") or {}).get("branches") or []
        assert "main" in branches and "homelab*" in branches, "%s push.branches = %r" % (fn, branches)
        assert "pull_request" in trig, fn


def test_publish_workflow_never_runs_on_a_branch_push():
    """publish-images.yml pushes to the package namespace; it runs on `v*` tags and manual dispatch ONLY — never on a
    branch push, so gating the tine on push can never publish an image from it. Guards a `branches:` entry creeping
    into the one workflow with `packages: write`."""
    push = _triggers("publish-images.yml").get("push") or {}
    assert "branches" not in push and push.get("tags") == ["v*"], push


def test_validate_ps1_is_a_shim_over_validate_sh():
    """tests/validate.ps1 must delegate to tests/validate.sh (through Git for Windows' bash) and define no gate step
    of its own. The hand-maintained Windows runner had drifted to 12 of the gate's 30+ steps — none of the
    `gen-*.py --check` staleness gates, no identifier-leak gate — so a Windows contributor could pass locally and
    fail CI (2026-10-08 review, finding 18). It must also propagate the gate's exit code, or a red gate shows green."""
    ps1 = _read("tests/validate.ps1")
    assert "tests/validate.sh" in ps1
    assert not re.search(r'^\s*Step\s+"', ps1, re.M), "validate.ps1 must not define gate steps of its own"
    assert "$LASTEXITCODE" in ps1, "the gate's exit code must be propagated"


def test_validate_has_a_warn_state_that_never_fails_the_run():
    """`tests/validate.sh` has a third outcome beside ok and FAIL: `advise` maps exit 3 to WARN, counts it, and shows
    the count in both summary lines, without touching `fail` and without reading `--strict`. Guards a later edit
    that folds the warning into a failure (`main` red because a date passed, on an unchanged tree) or drops it from
    the summary (a warning nobody sees), a case label that lets another exit status than 3 be a warning, and the
    status step leaving `advise` for `step`."""
    text = _read("tests/validate.sh")
    body = text.split("advise() {", 1)[1].split("\n}\n", 1)[0]
    assert '3) echo "WARN  $name"; warn=$((warn+1))' in body and "fail=$((fail+1))" in body
    assert "STRICT" not in body, "--strict must not promote a warning"
    assert body.count("fail=") == 1, "only the catch-all branch may count a failure"
    assert 'advise status-as-of "$PY" tests/check-status.py' in text
    assert '$warn warned)"; exit 1' in text and 'echo "VALIDATE: PASS ($skip skipped, $warn warned)"' in text
    labels = re.findall(r"^\s*([^\s()]+)\)\s", body, re.M)
    assert labels == ["0", "3", "*"], "only exit 3 may be a warning: %s" % labels
    assert not re.search(r'\$\{?warn\}?"?\s+-(?:ne|gt|ge|eq|lt|le)\b', text), "a warning is counted, never tested"
    assert 'if [ "$fail" -ne 0 ]; then echo "VALIDATE: FAIL' in text


def test_a_release_refuses_a_stale_status_page():
    """The publish workflow's `gate` job — the one the `publish` job needs — runs `tests/check-status.py --release`,
    where a stale STATUS.md is a failure. Everywhere else the age is a warning; the release is the one reader that
    cannot look away, at the moment the page matters most. Guards the step being removed, moved out of the
    gate, or made unable to fail (`continue-on-error`, an `if:`)."""
    wf = yaml.safe_load(_read(".github/workflows/publish-images.yml"))
    runs = [str(s.get("run", "")) for s in wf["jobs"]["gate"]["steps"]]
    assert "python tests/check-status.py --release" in runs, runs
    assert "gate" in str(wf["jobs"]["publish"].get("needs"))
    step = next(s for s in wf["jobs"]["gate"]["steps"] if s.get("run") == "python tests/check-status.py --release")
    assert "continue-on-error" not in step and "if" not in step, step


_APT_HELPER = os.path.join(ROOT, ".github", "scripts", "apt-install.sh")
# apt's own texts, measured on apt 2.6.1, 2.8.3 and 3.0.3 (the 2026-10-22 read of this helper): each lock error
# another process causes, then the failures that only look like one.
_LISTS_LOCK = "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 7 (apt-get)"
_ARCHIVES_LOCK = "E: Could not get lock /var/cache/apt/archives/lock. It is held by process 7 (apt-get)"
_FRONTEND_TIMEOUT = ("E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process "
                     "using it?")
_DPKG_TIMEOUT = "E: Unable to lock the administration directory (/var/lib/dpkg/), is another process using it?"
_NL = "\\n"   # the fake apt-get prints one plan line as several (printf %b)
_NO_PERMISSION = ("E: Could not open lock file /var/lib/apt/lists/lock - open (13: Permission denied)" + _NL
                  + "E: Unable to lock directory /var/lib/apt/lists/")
_NOT_ROOT = ("E: Could not open lock file /var/lib/dpkg/lock-frontend - open (13: Permission denied)" + _NL
             + "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), are you root?")
_RUNS_APT = re.compile(r"\bapt(-get)?\b|--with-deps|install-deps")


def test_every_workflow_step_that_runs_apt_goes_through_the_lock_retry():
    """Every workflow step whose command runs apt — `apt`, `apt-get`, or a tool that runs it for itself
    (`playwright install --with-deps`, `install-deps`) — is one line through .github/scripts/apt-install.sh, and no
    workflow names `apt-get` outside it. The self-hosted runners on one host share apt's locks, and a bare
    `apt-get update` failed a pytest job of an unrelated change on another job's lock (2026-10-22). Guards a new
    install step written the old way, and one that hides apt inside another tool (the e2e job's Playwright step ran
    the same failing pair, and the first form of this row, which searched for `apt-get`, did not see it)."""
    seen = 0
    for name in sorted(os.listdir(WF)):
        text = _read(os.path.join(".github", "workflows", name))
        assert "apt-get" not in text, "%s calls apt-get directly; use .github/scripts/apt-install.sh" % name
        for job in (yaml.safe_load(text).get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = str(step.get("run", "")).strip()
                if _RUNS_APT.search(run):
                    assert run.startswith("bash .github/scripts/apt-install.sh ") and "\n" not in run, (name, run)
                    seen += 1
    assert seen >= 3, "the apt steps are gone — if they moved, move this test with them"
    ci = _read(".github/workflows/ci.yml")
    assert "bash .github/scripts/apt-install.sh --run python -m playwright install --with-deps chromium" in ci


def _run_helper(tmp_path, plan, args=("gawk", "mawk"), tries=4, delay="0"):
    """Run the helper with a fake `sudo` (runs its arguments without LC_ALL, as a sudoers that keeps no locale would —
    sudo's default keeps it, so the helper's export and the pair's own `env LC_ALL=C` each prove out only here), a
    fake `sleep` (records its argument) and a fake
    `apt-get` that answers each call from `plan` in turn ("<rc>|<stderr text>", `\\n` between lines; a blank plan line
    or a spent plan = success) and records its arguments and its LC_ALL. `tries`/`delay` None leaves the helper's
    own default. Returns (rc, output, calls, sleeps, locales)."""
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if os.name == "nt" or not bash:
        pytest.skip("needs a POSIX bash")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "sudo").write_text('#!/usr/bin/env bash\nexec env -u LC_ALL "$@"\n')
    (fake / "sleep").write_text('#!/usr/bin/env bash\necho "$1" >> "$FAKE_DIR/sleeps"\n')
    (fake / "apt-get").write_text(
        '#!/usr/bin/env bash\necho "$*" >> "$FAKE_DIR/calls"\necho "${LC_ALL-unset}" >> "$FAKE_DIR/locales"\n'
        '{ IFS= read -r line; cat > "$FAKE_DIR/plan.rest"; } < "$FAKE_DIR/plan"\n'
        'mv "$FAKE_DIR/plan.rest" "$FAKE_DIR/plan"\n'
        '[ -z "$line" ] && exit 0\nprintf "%b\\n" "${line#*|}" >&2\nexit "${line%%|*}"\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    (tmp_path / "plan").write_text("".join(line + "\n" for line in plan))
    env = {k: v for k, v in os.environ.items() if k not in ("APT_INSTALL_TRIES", "APT_INSTALL_DELAY", "LC_ALL")}
    env.update({"PATH": "%s%s%s" % (fake, os.pathsep, os.environ["PATH"]), "FAKE_DIR": str(tmp_path)})
    if tries is not None:
        env["APT_INSTALL_TRIES"] = str(tries)
    if delay is not None:
        env["APT_INSTALL_DELAY"] = delay
    r = subprocess.run([bash, _APT_HELPER, *args], env=env, capture_output=True, text=True, timeout=60)

    def lines(name):
        return (tmp_path / name).read_text().splitlines() if (tmp_path / name).exists() else []
    return r.returncode, r.stdout + r.stderr, lines("calls"), lines("sleeps"), lines("locales")


def _verbs(calls):
    return [next(w for w in c.split() if w in ("update", "install")) for c in calls]


def test_the_helper_retries_an_update_another_job_holds_the_lists_lock_for(tmp_path):
    """`apt-get update` fails at once on the lists lock whatever DPkg::Lock::Timeout says (measured, apt 2.6.1 to
    3.0.3), which is how the pytest job failed: two lock errors, then the pair runs and succeeds. Guards the retry,
    and the install still receiving the packages with --no-install-recommends and dpkg's lock wait, in the C locale
    the lock pattern is written for (apt's messages are translated) — set past sudo by the pair itself, so it holds
    whatever the host's sudoers keeps."""
    rc, out, calls, sleeps, locales = _run_helper(tmp_path, ["100|" + _LISTS_LOCK, "100|" + _LISTS_LOCK])
    assert rc == 0, out
    assert _verbs(calls) == ["update", "update", "update", "install"], calls
    assert calls[-1] == "-o DPkg::Lock::Timeout=60 install -y --no-install-recommends gawk mawk"
    assert out.count("another process holds apt's lock") == 2 and sleeps == ["0", "0"]
    assert set(locales) == {"C"}, locales


@pytest.mark.parametrize("error", [_ARCHIVES_LOCK, _FRONTEND_TIMEOUT, _DPKG_TIMEOUT],
                         ids=["archives-lock", "dpkg-frontend-wait-ran-out", "dpkg-lock-wait-ran-out"])
def test_the_helper_retries_an_install_that_meets_a_lock(tmp_path, error):
    """`install` fails at once on the archives lock, and on either of dpkg's locks once its 60 s wait runs out —
    apt's own texts, measured — so the retry covers the install as well as the update. Guards the first form of the
    pattern, which matched no dpkg-wait error ("Unable to acquire the dpkg frontend lock … is another process using
    it?"), so a host's long unattended upgrade failed the job after one wait."""
    rc, out, calls, _, _ = _run_helper(tmp_path, ["", "100|" + error])
    assert rc == 0, out
    assert _verbs(calls) == ["update", "install", "update", "install"], calls


@pytest.mark.parametrize("plan", [
    ["100|E: Unable to locate package mawk"],
    ["", "100|Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 7"],
    ["100|" + _NO_PERMISSION],
    ["", "100|" + _NOT_ROOT],
], ids=["no-such-package", "a-wait-line-is-no-lock-error", "lock-file-not-opened", "not-root"])
def test_a_failure_that_names_no_lock_fails_at_once_with_apts_status(tmp_path, plan):
    """A failure that is not another process's lock is not retried: it fails on the first try with apt's own status,
    as the bare command did, so a wrong package name costs no retries. apt's line while it waits for dpkg's lock
    carries the same words as the error, so only an `E:` line counts; and apt's "Unable to lock directory" follows
    a lock file it could not OPEN (a permission, a read-only /var) as well as one another process holds, so it does
    not count either — the first form of the pattern retried a permission failure for every try, saying another
    process held the lock."""
    rc, out, calls, sleeps, _ = _run_helper(tmp_path, plan)
    assert rc == 100, out
    assert len(calls) == len(plan) and sleeps == [], calls
    assert "another process holds apt's lock" not in out


def test_a_failure_after_a_lock_is_judged_on_its_own_output(tmp_path):
    """A try that fails for another reason after an earlier try met a lock fails at once: each try's output is judged
    alone. Guards an appending capture, which would carry the earlier lock line forward and retry a wrong package
    name until the tries ran out."""
    rc, out, calls, _, _ = _run_helper(tmp_path, ["100|" + _LISTS_LOCK, "", "100|E: Unable to locate package mawk"])
    assert rc == 100, out
    assert _verbs(calls) == ["update", "update", "install"], calls


def test_a_lock_that_is_never_released_fails_after_the_default_bound(tmp_path):
    """With nothing set, a lock held through every try fails the step after twenty tries fifteen seconds apart (the
    numbers the CHANGELOG and the doc give), with no sleep after the last try, and says so, rather than hang the job.
    Guards the wait itself — a helper that stopped sleeping would spend every try in a second, before any real holder
    let go — and the bound."""
    rc, out, calls, sleeps, _ = _run_helper(tmp_path, ["100|" + _LISTS_LOCK] * 20, tries=None, delay=None)
    assert rc == 1, out
    assert len(calls) == 20 and sleeps == ["15"] * 19, (len(calls), sleeps)
    assert "still held after 20 tries" in out and "try 20 of 20" not in out


def test_the_helper_runs_a_command_that_runs_apt_itself_the_same_way(tmp_path):
    """`--run <command>...` runs the command (here the fake apt-get, as Playwright's `--with-deps` runs the real
    one), retries it on a lock error and fails a failure that names no lock at once, and a call with nothing to run
    is a usage error. The command runs in the C locale the helper exports, the only setting that reaches a command
    the helper does not write itself. Guards the e2e job's Playwright step, which runs apt's update and install
    itself."""
    def run(name, plan, args):
        (tmp_path / name).mkdir()
        return _run_helper(tmp_path / name, plan, args=args)
    rc, out, calls, _, locales = run("retried", ["100|" + _LISTS_LOCK, ""], ("--run", "apt-get", "update"))
    assert rc == 0 and calls == ["update", "update"] and set(locales) == {"C"}, (out, calls, locales)
    rc, out, calls, _, _ = run("failed", ["100|E: Unable to locate package mawk"], ("--run", "apt-get", "install"))
    assert rc == 100 and calls == ["install"], (out, calls)
    for name, args in (("bare-run", ("--run",)), ("nothing", ())):
        rc, out, calls, _, _ = run(name, [], args)
        assert rc == 2 and "usage" in out and calls == [], (args, out)
