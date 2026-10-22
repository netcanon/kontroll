"""Text pins on the CI workflows the public split changed (publish target, the zizmor SARIF seat, Dependabot
strategy, third-party action pins) and on the gate wiring (the `homelab*` push trigger, the Windows shim), plus the
package-install helper every workflow step that runs apt goes through (.github/scripts/apt-install.sh), run against
a fake apt.

WHY — a workflow file has no unit test of its own, so a behaviour the 2026-10-09 review fixed can quietly come back
in a later edit: the publish target hard-coded to one org again, the SARIF seat scanning `.github/` (every alert URI
wrong), an `|| true` hiding a real zizmor failure, a `security-events: write` grant minted on every run, a pip
ecosystem that chases `>=` floors, or a third-party action tag-pinned in the one job that can push to the package
namespace. Each test names the failure it guards and reads the committed YAML as text (plus a YAML parse where the
shape matters). CLAUDE.md: the test lands with the behaviour change.
"""
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


def test_every_pip_ecosystem_uses_increase_if_necessary():
    """Every pip ecosystem in dependabot.yml uses `versioning-strategy: increase-if-necessary`, because the
    requirements files are `>=` floors: the default strategy opens a floor-bump PR for every release (five the
    first week). Also pins that the api/ requirements file is covered at all (it ships in the images)."""
    doc = yaml.safe_load(_read(".github/dependabot.yml"))
    pip = [u for u in doc["updates"] if u["package-ecosystem"] == "pip"]
    assert pip, "dependabot.yml must cover the pip ecosystems"
    for u in pip:
        assert u.get("versioning-strategy") == "increase-if-necessary", u["directory"]
    assert {u["directory"] for u in pip} >= {"/gui", "/api", "/tests"}, "gui, api and tests requirements are covered"


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
