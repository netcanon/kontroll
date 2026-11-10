"""Homepage receives ONLY its own env — never the stack-wide docker/.env (2026-10-08 review, finding 5).

WHY: `homepage.yaml` used `env_file: ../.env`, the file deploy-stack renders with EVERY stack secret — the Semaphore
DB password and access-key encryption secret (which decrypts the store holding the runner's age key), the Grafana
admin password, the GUI password, the API token, every exporter credential. Homepage is the documented
UNAUTHENTICATED, all-interfaces portal (SECURITY.md C3); a Homepage RCE, or anyone who can read the container's
environment, got the whole stack for a tile board that needs a handful of widget tokens at most.

The fix: deploy-stack renders `docker/.env.homepage` with exactly the `homepage_var_*` keys of the `dashboards` SOPS
domain, and the fragment reads that file alone. These pins keep the two halves honest and make sure no OTHER fragment
quietly starts `env_file`ing the stack env (the pattern reads naturally, which is how it got in).
"""
import glob
import os
import re
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STACK_ENV_NAMES = {".env"}                                    # the stack-wide env file deploy-stack renders


def _load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _env_files(service):
    """Normalise compose's env_file forms (string | list of strings | list of {path, required})."""
    ef = service.get("env_file")
    if ef is None:
        return []
    if isinstance(ef, str):
        return [ef]
    return [e["path"] if isinstance(e, dict) else e for e in ef]


def test_homepage_reads_only_its_own_env_file():
    """The fragment's env_file is `../.env.homepage` (optional, so `compose config` on a fresh checkout still
    validates) and NOT the stack `../.env`. Guards the one-line revert that hands every stack secret back to the
    unauthenticated portal."""
    svc = _load("docker/services/homepage.yaml")["services"]["homepage"]
    files = _env_files(svc)
    assert files == ["../.env.homepage"], "Homepage must env_file exactly its own file: %r" % (files,)
    entry = svc["env_file"][0]
    assert isinstance(entry, dict) and entry.get("required") is False, \
        "the file is optional — a fresh checkout has not rendered it yet"


def test_no_fragment_env_files_the_stack_wide_env():
    """SWEEP: no service fragment under docker/services/ may `env_file` the stack-wide `.env`. Every service that needs
    a secret takes it by name through `environment:` interpolation (`${VAR:?}`), which is explicit and reviewable; an
    `env_file` of the whole stack env is a blanket grant. Homepage was the only one; this keeps it the last."""
    offenders = []
    for path in sorted(glob.glob(os.path.join(ROOT, "docker", "services", "*.yaml"))):
        doc = _load(os.path.relpath(path, ROOT))
        for name, svc in (doc.get("services") or {}).items():
            for f in _env_files(svc):
                if os.path.basename(f) in STACK_ENV_NAMES:
                    offenders.append("%s: %s env_files %s" % (os.path.basename(path), name, f))
    assert offenders == [], "a fragment grants itself the whole stack env:\n  " + "\n  ".join(offenders)


def _deploy_tasks():
    doc = _load("ansible/playbooks/deploy-stack.yml")
    out = []
    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))
    for play in doc:
        for section in ("pre_tasks", "tasks", "post_tasks"):
            walk(play.get(section))
    return out


def test_deploy_stack_renders_the_portal_env_with_widget_keys_only_under_no_log():
    """The render task exists, writes `{{ stack_dir }}/.env.homepage` 0600, is `no_log` (it handles decrypted
    values), runs only when Homepage is deployed, and its template emits NOTHING but `homepage_var_*` keys: none of
    the stack secret names may appear in it. Guards both the missing render (Homepage silently loses its widgets)
    and a template that grows a stack secret back."""
    renders = [t for t in _deploy_tasks()
               if isinstance(t.get("ansible.builtin.copy"), dict)
               and str(t["ansible.builtin.copy"].get("dest", "")).endswith("/.env.homepage")]
    assert len(renders) == 1, "exactly one task renders .env.homepage"
    t = renders[0]
    copy = t["ansible.builtin.copy"]
    assert t.get("no_log") is True, "the render handles decrypted values — no_log"
    assert str(copy.get("mode")) == "0600"
    assert "'homepage' in stack_services" in str(t.get("when", "")), "rendered only when Homepage is deployed"
    content = copy["content"]
    assert "homepage_var_" in content, "the template must select the homepage_var_* keys"
    for forbidden in ("SEMAPHORE_", "GRAFANA_ADMIN", "GUI_PASSWORD", "GUI_USER", "KONTROLL_API_TOKEN", "PVE_",
                      "KONTROLL_STAGE_PUSHES", "_sem.", "_secret_env"):
        assert forbidden not in content, "the portal env must never carry %s" % forbidden


def test_the_stack_env_render_still_emits_no_homepage_tokens():
    """The two files are disjoint: the stack `.env` render carries no HOMEPAGE_VAR_* line (the tokens have one home),
    and `docker/.env.example` no longer defines them as stack env. Guards the tokens drifting back into the file
    every service used to inherit."""
    renders = [t for t in _deploy_tasks()
               if isinstance(t.get("ansible.builtin.copy"), dict)
               and str(t["ansible.builtin.copy"].get("dest", "")).endswith("/.env")]
    assert renders, "the stack .env render task must exist"
    assert "HOMEPAGE_VAR_" not in renders[0]["ansible.builtin.copy"]["content"]
    example = open(os.path.join(ROOT, "docker", ".env.example"), encoding="utf-8").read()
    assert not re.search(r"^HOMEPAGE_VAR_\w+=", example, re.M), \
        ".env.example must not define HOMEPAGE_VAR_* as stack env (they live in the dashboards domain)"


@pytest.mark.requires_checkout  # reads the tree through git: skipped outside a checkout (I-51)
def test_the_portal_env_file_is_git_ignored():
    """`docker/.env.homepage` holds decrypted widget tokens, so it must be ignored like `docker/.env`. Checked with
    git's own matcher (`check-ignore --no-index`), not a string search, because the existing `*.env` pattern does
    NOT match a `.env.homepage` suffix — which is exactly the kind of near-miss that leaks a file."""
    rc = subprocess.run(["git", "check-ignore", "-q", "--no-index", "docker/.env.homepage"], cwd=ROOT).returncode
    assert rc == 0, "docker/.env.homepage is not git-ignored"
