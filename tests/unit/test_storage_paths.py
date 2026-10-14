"""Storage-location paradigm regression guards (the C8 at-rest contract for KONTROLL_STORAGE_ROOT + overrides).

Why each guards a real failure:
  * a storage bind source with no uid-scoped deploy-stack provisioner is the G9 hole — `docker compose up`
    auto-creates the missing host source as root:root 0755, world-readable secret-bearing logs (C8/C12). The
    guard (check-storage-chown.offending) is exercised here so a regression fails the pytest matrix AND the
    validate gate together (the dual-surface pattern of test_mgmt_parameterization.py);
  * a guard that has never been observed to FAIL is not a guard — so we inject a synthetic un-provisioned bind
    and assert it is flagged (the negative-path proof the review required), AND assert that removing the age.key
    fail-closed guard re-flags the age.key bind (the M-1 lock: an EXACT-own-path match, never a grandparent walk);
  * a bare ${VAR} bind source resolves EMPTY when unset → a `/`-rooted or empty-prefixed mount (fail-open);
  * deploy-stack and .env.example must agree on the storage root default, or a no-override deploy diverges from
    the documented behaviour (the render-path trap the mgmt test already guards for IP/domain).
"""
import glob
import importlib.util
import os

import pytest

from kontroll import paths

pytestmark = pytest.mark.unit

_RELOCATABLE_VARS = ("KONTROLL_LOGS_DIR", "KONTROLL_BACKUPS_DIR", "KONTROLL_API_AUDIT_DIR",
                     "KONTROLL_GUI_AUDIT_DIR", "KONTROLL_ANSIBLE_LOG_DIR")
_DEPLOY = os.path.join(paths.ROOT, "ansible", "playbooks", "deploy-stack.yml")


def _guard():
    path = os.path.join(paths.ROOT, "tests", "check-storage-chown.py")
    spec = importlib.util.spec_from_file_location("check_storage_chown", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_tree_every_storage_bind_is_provisioned():
    """On the real tree NO storage-root-derived compose bind source lacks a uid-scoped deploy-stack provisioner
    — the C8 at-rest contract holds (the G9 root:root 0755 hole is closed for every relocatable store)."""
    assert _guard().offending() == []


def test_guard_flags_an_unprovisioned_synthetic_store(tmp_path):
    """NEGATIVE-PATH PROOF: a synthetic compose file with a storage-root-derived bind that NO deploy-stack task
    provisions is reported by offending(). A guard never observed to fail on a hole is worthless — this is the
    evidence it actually breaks CI when a future store is added without its chown."""
    fake = tmp_path / "fake.yaml"
    fake.write_text("    volumes:\n      - ${KONTROLL_FAKE_DIR:-/var/lib/kontroll/fake}:/fake\n", encoding="utf-8")
    with open(_DEPLOY, encoding="utf-8") as fh:
        deploy_text = fh.read()
    bad = _guard().offending(service_files=[str(fake)], deploy_text=deploy_text)
    assert any(np == "/var/lib/kontroll/fake" for _rel, _n, np, _raw in bad), \
        "the guard must flag an in-scope bind source with no uid-scoped deploy-stack provisioner"


def test_removing_the_age_key_failclosed_guard_reflags_it(tmp_path):
    """M-1 LOCK: the age.key bind is covered ONLY by its own fail-closed absence guard (the file is
    host-provisioned, never minted). Removing that guard must re-flag the age.key bind — proving the guard does
    an EXACT own-path match, not a permissive grandparent walk (a chowned parent dir would wrongly pass it).

    Since 2026-07-28 the guard is a `stat` + a separate `assert` (a `failed_when` cannot carry a message, and the
    refusal now prints the exact command to run). So neutralising it means dropping the existence CONDITION, not
    a `failed_when` key — the stat itself survives, which is also the sharper test: a stat that merely looks at
    the path, with nothing fail-closed reading the result, must NOT count as a provisioner."""
    g = _guard()
    with open(_DEPLOY, encoding="utf-8") as fh:
        deploy_text = fh.read()
    assert g.offending() == []                                  # green with the guard present
    stripped = deploy_text.replace("              - _gui_age_key.stat.exists\n", "              - true\n")
    assert stripped != deploy_text                              # the guard line existed (it is really there)
    bad = g.offending(deploy_text=stripped)
    assert any(np.endswith("/onboard-gui/age.key") for _rel, _n, np, _raw in bad), \
        "removing the age.key fail-closed stat guard must re-open it as an offending (un-provisioned) bind"


def test_a_clone_made_in_an_imported_task_file_is_provisioned_with_the_imports_values(tmp_path):
    """The content clones are made in a task file deploy-stack.yml imports (tasks/content-clone.yml), so the
    guard reads an imported file's tasks as the play's own, with the import's `vars:` put in: a git task there
    covers the clone the IMPORT names. Without that the clones' binds would be flagged — or, read without the
    values, a `{{ _clone_dir }}` that matches nothing would hide a missing provisioner. The import is followed,
    and its values are what count."""
    g = _guard()
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "clone.yml").write_text(
        "- name: clone\n  ansible.builtin.git:\n    repo: file:///srv/kontroll.git\n    dest: \"{{ _clone_dir }}\"\n",
        encoding="utf-8")
    play = ("- hosts: localhost\n  tasks:\n    - name: one clone\n"
            "      ansible.builtin.import_tasks: tasks/clone.yml\n"
            "      vars:\n        _clone_dir: \"{{ kontroll_api_dir }}/repo\"\n")
    _owned, git_dests, _stat = g.deploy_provisioners(play, playbook_dir=str(tmp_path))
    assert git_dests == {"/var/lib/kontroll/api/repo"}, git_dests


def test_storage_sources_are_fail_closed():
    """Every relocatable bind source uses ${VAR:-/var/lib/kontroll/…} (a concrete today-exact default), never a
    bare ${VAR} (empty when unset → a '/'-rooted or empty-prefixed mount). Mirrors the retention fail-closed rule."""
    for f in sorted(glob.glob(os.path.join(paths.ROOT, "docker", "services", "*.yaml"))):
        text = open(f, encoding="utf-8").read()
        for var in _RELOCATABLE_VARS:
            if ("${%s" % var) in text:
                assert ("${%s:-/var/lib/kontroll" % var) in text, \
                    "%s in %s must be fail-closed ${%s:-/var/lib/kontroll/…}" % (var, os.path.basename(f), var)


def test_deploy_stack_and_env_example_agree_on_root():
    """deploy-stack derives the storage root with the today-exact default AND .env.example documents the same
    root — a no-override deploy binds + chowns exactly where it does today (behaviour-preserving)."""
    ds = open(_DEPLOY, encoding="utf-8").read()
    assert "kontroll_storage_root" in ds and "default('/var/lib/kontroll'" in ds
    env = open(os.path.join(paths.ROOT, "docker", ".env.example"), encoding="utf-8").read()
    assert "KONTROLL_STORAGE_ROOT=/var/lib/kontroll" in env
