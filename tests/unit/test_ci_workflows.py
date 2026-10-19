"""Text pins on the CI workflows the public split changed (publish target, the zizmor SARIF seat, Dependabot
strategy, third-party action pins) and on the gate wiring (the `homelab*` push trigger, the Windows shim).

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
