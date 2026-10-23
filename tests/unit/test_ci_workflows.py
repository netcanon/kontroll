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

import pytest
import yaml

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
            images = {s["image"].split("@", 1)[0].rsplit(":", 1)[0]
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
], ids=["fragment-names", "include-only-root", "dot-compose", "docker-compose", "generated-unignored",
        "generated-ignored"])
def test_the_compose_entry_check_tells_a_read_directory_from_a_dead_one(tmp_path, files, ignore, expected):
    """The compose-entry check flags a directory of `<service>.yaml` fragments (the shape T29 first shipped) and one
    whose only fetched file holds `include:` and no `services:`, and a generated file's image the entry does not
    ignore; it passes `<name>.compose.yaml` and `docker-compose.yml` files, and a generated image that is ignored.
    Guards a check that passes the dead entry, or refuses a directory the fetcher reads."""
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


def pip_audit_faults(workflow):
    """({the -r files the workflow's pip-audit runs read}, [faults]) of a parsed workflow. A step runs pip-audit when
    a line of its `run:` that is not a comment starts a command with it (after a `;`, `&&`, `||` or `|`, or at the
    start). A fault is such a step that cannot fail its job (`||` on the line, `set +e` in the step,
    `continue-on-error` on the step or the job) or that runs on a condition (`if:` on the step or the job)."""
    audited, faults = set(), []
    for name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            lines = [ln for ln in str(step.get("run") or "").splitlines() if not ln.lstrip().startswith("#")]
            runs = [ln for ln in lines if re.search(r"(?:^|&&|\|\||;|\|)\s*pip-audit(?:\s|$)", ln)]
            if not runs:
                continue
            for ln in runs:
                audited |= set(re.findall(r"(?:^|\s)(?:-r|--requirement)[\s=]+(\S+)", ln))
                if "||" in ln:
                    faults.append("%s: `||` on a pip-audit line: %s" % (name, ln.strip()))
            if any(re.search(r"\bset\s+\+\w*e", ln) for ln in lines):
                faults.append("%s: `set +e` in a pip-audit step" % name)
            for where, node in (("step", step), ("job", job)):
                if node.get("continue-on-error"):
                    faults.append("%s: continue-on-error on the pip-audit %s" % (name, where))
                if "if" in node:
                    faults.append("%s: the pip-audit %s runs on a condition" % (name, where))
    return audited, faults


def test_pip_audit_covers_the_image_requirement_files():
    """The pip-audit job reads every requirements file an image installs, as well as the floors, and nothing switches
    it off. The workflow is read through its parsed steps, so a commented-out step reads nothing. Guards the audit
    going back to reading only the floors — which name no version an image runs — so a known vulnerability in a
    pinned image package (one was found when this landed: certifi) would pass unseen; and a step that is still
    there but cannot fail (T29's read, S2: W1, W2)."""
    audited, faults = pip_audit_faults(yaml.safe_load(_read(".github/workflows/security.yml")))
    images = {d.lstrip("/") + "/requirements.txt" for d in _image_requirement_dirs()}
    assert images and images <= audited, sorted(images - audited)
    assert {"gui/requirements.txt", "api/requirements.txt", "tests/requirements-dev.txt"} <= audited, audited
    assert faults == []


_AUDIT = "pip-audit --no-deps -r docker/a/requirements.txt"


@pytest.mark.parametrize("step,job,read,expected", [
    ({"run": _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "pip install --quiet pip-audit"}, {}, set(), []),
    ({"run": "echo pip-audit -r docker/a/requirements.txt"}, {}, set(), []),
    ({"run": "# " + _AUDIT}, {}, set(), []),
    ({"run": _AUDIT + " || true"}, {}, {"docker/a/requirements.txt"}, ["`||`"]),
    ({"run": _AUDIT, "continue-on-error": True}, {}, {"docker/a/requirements.txt"}, ["continue-on-error"]),
    ({"run": _AUDIT}, {"continue-on-error": True}, {"docker/a/requirements.txt"}, ["continue-on-error"]),
    ({"run": "set +e\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, ["set +e"]),
    ({"run": _AUDIT, "if": "false"}, {}, {"docker/a/requirements.txt"}, ["condition"]),
    ({"run": "# never `set +e` here\n" + _AUDIT}, {}, {"docker/a/requirements.txt"}, []),
    ({"run": "cd x && " + _AUDIT + " --requirement=b.txt"}, {}, {"docker/a/requirements.txt", "b.txt"}, []),
], ids=["audit", "install-only", "named-not-run", "commented-out", "or-true", "step-continue", "job-continue",
        "set-plus-e", "conditional", "set-plus-e-in-a-comment", "after-cd"])
def test_the_pip_audit_check_tells_a_live_audit_from_a_dead_one(step, job, read, expected):
    """The pip-audit check reads the files of a live `pip-audit` run (after a `cd … &&`, `--requirement=` too), reads
    nothing from a commented-out run, the step that installs pip-audit or a command that only names it, and flags a
    run that `|| true`, `continue-on-error`, `set +e` or an `if:` switches off (not a comment that names `set +e`).
    Guards a check that counts a dead audit as coverage."""
    audited, faults = pip_audit_faults({"jobs": {"a": dict(job, steps=[step])}})
    assert audited == read
    assert len(faults) == len(expected) and all(e in f for e, f in zip(expected, faults)), faults


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
