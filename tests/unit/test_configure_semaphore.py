"""scripts/configure-semaphore.py — the operational-template drop-in registry (config/semaphore/templates/).

Verifies the data-driven registration that replaced the inline ping-fleet: the loader reads every template
spec, the C10 `promote-proposal` template declares its `run_id` survey variable, every registered playbook
actually exists, and `template_payload` threads survey_vars/arguments ONLY when declared.

Why each guards a real failure:
- A registry that silently dropped a template file would leave an operational task (e.g. the promote "approve"
  action) unregistered in Semaphore with no error — the loader must return exactly the declared set.
- A `promote-proposal` missing its required `run_id` survey var would make the GUI promote unusable (no way to
  name the proposal), or worse, promote with an undefined id.
- A template pointing at a typo'd `playbook:` path would fail only at Semaphore runtime — the existence check
  turns that into a CI failure (config-as-data, validated).
- Threading an EMPTY survey_vars onto ping-fleet would churn its template on every re-run (a non-idempotent
  upsert) and is semantically wrong (ping takes no input).
"""
import importlib.util
import json
import os
import shutil
import types

import pytest

from kontroll import paths

pytestmark = pytest.mark.unit


def _load(path=None, name="configure_semaphore"):
    """Load scripts/configure-semaphore.py (hyphenated → not importable by name) as a module — or the copy at
    `path`, whose repository root is then the copy's."""
    path = path or os.path.join(paths.ROOT, "scripts", "configure-semaphore.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load()


def test_registry_loads_ping_and_promote_with_required_keys():
    """The loader returns one spec per config/semaphore/templates/*.yml, each with name+playbook+description,
    and includes both the migrated ping-fleet and the new promote-proposal."""
    specs = cs.load_operational_templates()
    by_name = {s["name"]: s for s in specs}
    assert {"ping-fleet", "promote-proposal"} <= set(by_name)
    for s in specs:
        assert s.get("name") and s.get("playbook") and s.get("description"), s.get("_file")


def test_promote_proposal_declares_required_run_id_survey_var():
    """promote-proposal exposes exactly the `run_id` survey variable, required — the operator must name the
    proposal to promote; ping-fleet, which takes no input, declares no survey vars."""
    by_name = {s["name"]: s for s in cs.load_operational_templates()}
    sv = by_name["promote-proposal"].get("survey_vars")
    assert sv and len(sv) == 1
    assert sv[0]["name"] == "run_id" and sv[0]["required"] is True
    assert not by_name["ping-fleet"].get("survey_vars")


def test_every_registered_playbook_exists():
    """Each template's repo-relative playbook path resolves to a real file — a typo would otherwise surface
    only when Semaphore runs the job."""
    for s in cs.load_operational_templates():
        assert os.path.isfile(os.path.join(paths.ROOT, s["playbook"])), s["playbook"]


def test_template_payload_threads_survey_vars_and_arguments_only_when_present():
    """template_payload includes survey_vars/arguments ONLY when the spec declares them (so a no-input template
    carries no empty survey_vars and re-runs don't churn), and always carries the common project ids."""
    common = {"project_id": 1, "inventory_id": 2, "repository_id": 3, "environment_id": 4,
              "app": "ansible", "type": "", "arguments": "[]"}

    promote = {"name": "promote-proposal", "playbook": "ansible/playbooks/promote.yml",
               "description": "d", "survey_vars": [{"name": "run_id", "required": True}]}
    p = cs.template_payload(promote, common)
    assert p["survey_vars"] == promote["survey_vars"]
    assert p["name"] == "promote-proposal" and p["project_id"] == 1

    ping = {"name": "ping-fleet", "playbook": "ansible/playbooks/ping.yml", "description": "d"}
    p2 = cs.template_payload(ping, common)
    assert "survey_vars" not in p2

    with_args = {"name": "x", "playbook": "ansible/playbooks/ping.yml", "description": "d",
                 "arguments": ["-e", "k=v"]}
    p3 = cs.template_payload(with_args, common)
    assert p3["arguments"] == '["-e", "k=v"]'        # JSON-serialised


def test_wait_for_semaphore_retries_until_it_responds(monkeypatch):
    """F15 (live single-pass dogfood): Semaphore's HTTP server isn't listening the instant deploy-stack starts its
    container, so configure-semaphore must POLL past transient connection failures and proceed once it RESPONDS —
    else a fresh deploy-then-configure flow races it with a ConnectionResetError. Mocks urlopen to fail twice then
    succeed (and sleep to a no-op); asserts it returns after the server comes up, not on the first refusal."""
    import urllib.error
    calls = {"n": 0}

    def fake_open(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return object()                               # any response object → the server is up
    monkeypatch.setattr(cs.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    cs.wait_for_semaphore(timeout=30)                 # returns without raising, after the 3rd poll
    assert calls["n"] == 3


def test_admin_password_self_decrypts_from_sops_when_env_absent(monkeypatch, tmp_path):
    """Without SEMAPHORE_ADMIN_PASSWORD in the env, configure-semaphore SELF-DECRYPTS it from the semaphore SOPS
    domain via `sops -d --extract '["semaphore_admin_password"]'` — so the documented `python3 scripts/configure-
    semaphore.py` works on the control node with NO manual export. The live fresh-install dogfood failed with
    'SEMAPHORE_ADMIN_PASSWORD not set' precisely because that export was undocumented; this guards the fallback. An
    env value, when present, WINS and no decrypt runs (never re-shells sops when the operator already supplied it)."""
    monkeypatch.delenv("SEMAPHORE_ADMIN_PASSWORD", raising=False)
    secrets = tmp_path / "semaphore.sops.yml"
    secrets.write_text("encrypted", encoding="utf-8")
    monkeypatch.setattr(cs, "SEMAPHORE_SECRETS", str(secrets))
    calls = {}

    def fake_run(argv, capture_output=False, text=False):
        calls["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="s3cret-from-sops\n", stderr="")
    monkeypatch.setattr(cs.subprocess, "run", fake_run)

    assert cs.admin_password() == "s3cret-from-sops"
    assert calls["argv"][:3] == ["sops", "-d", "--extract"]
    assert '["semaphore_admin_password"]' in calls["argv"] and str(secrets) in calls["argv"]

    monkeypatch.setenv("SEMAPHORE_ADMIN_PASSWORD", "from-env")
    calls.clear()
    assert cs.admin_password() == "from-env"
    assert not calls                                  # env wins → sops was never invoked


def test_admin_password_none_when_no_env_and_no_secrets_file(monkeypatch, tmp_path):
    """No env var AND no instance/secrets/semaphore.sops.yml (a half-provisioned node) ⇒ admin_password() returns
    None — so main() fails with a clear hint instead of crashing in sops — and sops is NOT invoked when the file is
    absent (no spurious subprocess on a node that hasn't minted the semaphore domain yet)."""
    monkeypatch.delenv("SEMAPHORE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(cs, "SEMAPHORE_SECRETS", str(tmp_path / "absent.sops.yml"))
    called = {"n": 0}
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert cs.admin_password() is None
    assert called["n"] == 0


class _FakeSemaphore:
    """Semaphore's REST API as configure-semaphore drives it: one list of objects per collection path, each object
    given an id when it is POSTed, a PUT to `<path>/<id>` updating it. Records nothing it is not sent."""

    def __init__(self):
        self.lists, self._next = {}, 0

    def __call__(self, method, path, body=None):
        if path == "/api/auth/login":
            return None
        if method == "GET":
            return [dict(o) for o in self.lists.get(path, [])]
        if method == "POST":
            self._next += 1
            obj = dict(body, id=self._next)
            self.lists.setdefault(path, []).append(obj)
            return dict(obj)
        if method == "PUT":
            base, _, oid = path.rpartition("/")
            for o in self.lists.get(base, []):
                if o["id"] == int(oid):
                    o.update(body)
            return None
        raise AssertionError("configure-semaphore sent %s %s, which this fake does not serve" % (method, path))


def test_reads_what_gen_backup_renders(tmp_path, monkeypatch):
    """configure-semaphore, run against a fake Semaphore from a tree whose `schedules.generated.yml` is what
    gen-backup renders, registers one schedule per spec with the spec's name and cron, tied to a template that runs
    the spec's playbook with the spec's `--limit` (`all` for a class with no group) and passes its retention — for
    the example fleet's specs plus one carrying both optional knobs and one with no group.

    Guards a format change on one side only — a key renamed in `render`, or read under another name in step 7 —
    registering no schedule, or one with the wrong limit, while every test stays green: test_gen_backup.py checks
    the file against its own belief of the reader, and nothing else here read a file the generator wrote (S33)."""
    spec = importlib.util.spec_from_file_location("gen_backup", os.path.join(paths.ROOT, "scripts", "gen-backup.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    specs = gen.backup_schedules(gen._load("instance.example/fleet.yml")) + [
        {"name": "backup-knobs", "cron": "30 3 * * 0", "limit": "edge_router", "key": "knobs",
         "playbook": "ansible/playbooks/backup-configs.yml", "retention": "30d", "destination": "local"},
        {"name": "backup-nogroup", "cron": "0 4 * * *", "limit": None, "key": "nogroup",
         "playbook": "ansible/playbooks/backup-configs.yml"}]
    assert len(specs) > 2, "the example fleet generated no backup schedule"
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "config" / "semaphore").mkdir(parents=True)
    shutil.copyfile(os.path.join(paths.ROOT, "scripts", "configure-semaphore.py"),
                    str(root / "scripts" / "configure-semaphore.py"))
    (root / "config" / "semaphore" / "schedules.generated.yml").write_bytes(gen.render(specs).encode("utf-8"))
    copy = _load(str(root / "scripts" / "configure-semaphore.py"), "configure_semaphore_copy")
    fake = _FakeSemaphore()
    for home in ("HOME", "USERPROFILE"):                  # no optional key file exists, so none is read
        monkeypatch.setenv(home, str(tmp_path / "home"))
    monkeypatch.setattr(copy, "api", fake)
    monkeypatch.setattr(copy, "admin_password", lambda: "x")
    monkeypatch.setattr(copy, "wait_for_semaphore", lambda timeout=120: None)
    monkeypatch.setattr(copy, "read_file", lambda path: "not a key")
    copy.main()

    project = "/api/project/%d" % fake.lists["/api/projects"][0]["id"]
    schedules = {s["name"]: s for s in fake.lists[project + "/schedules"]}
    templates = {t["name"]: t for t in fake.lists[project + "/templates"]}
    assert sorted(schedules) == sorted(s["name"] for s in specs)
    for s in specs:
        registered, template = schedules[s["name"]], templates[s["name"]]
        assert registered["cron_format"] == s["cron"] and registered["active"] is True
        assert registered["template_id"] == template["id"] and template["playbook"] == s["playbook"]
        args = json.loads(template["arguments"])
        assert args[args.index("--limit") + 1] == (s["limit"] or "all")
        retention = [a for a in args if a.startswith("kontroll_backup_retention=")]
        assert retention == (["kontroll_backup_retention=" + s["retention"]] if s.get("retention") else []), s["name"]
