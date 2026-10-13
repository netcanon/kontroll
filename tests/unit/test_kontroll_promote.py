"""kontroll-promote.py — the FIX-M9 resolve gate before the fast-forward promote (it began as the pin-conflict gate).

WHY (the failures these guard):
  * Two app-store units each pinning the SAME collection to a DIFFERENT exact `==` version both becoming `active`
    makes `gen-requirements` fail closed at GENERATE time — which halts the NEXT deploy's pre-image-build, a brick
    one seam removed from the cause. The promote gate must REFUSE such a promote (and NEVER advance `main`), so two
    conflicting `==` units can never co-exist. A clean proposal must still promote, and the gate must fail OPEN on
    its own read error (it hardens; it must never itself block a clean promote).
  * The detector is THIS tree's `gen-requirements.py --conflict-check --root <extract>` run over the extracted
    prospective tree as DATA — never the proposal's own copy of the script (2026-10-08 review, finding 3: that
    executed unreviewed code as root / with SOPS_AGE_KEY in scope). The wiring (archive → extract → trusted run →
    refuse on non-zero) is exercised end-to-end against real git + subprocess, not only a mock (the L2b lesson: a
    mock hides the plumbing bug), and the trust property is falsified with a proposal whose generator is a canary.
"""
import importlib.util
import os
import shutil
import subprocess as sp

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_script(modname, filename):
    """Load a hyphenated scripts/<filename> by path (not importable as a module name)."""
    spec = importlib.util.spec_from_file_location(modname, os.path.join(ROOT, "scripts", filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


promote = _load_script("kontroll_promote", "kontroll-promote.py")


def test_promote_refused_on_pin_conflict(monkeypatch):
    """A proposal whose prospective tree fails the conflict-check is REFUSED — `promote_ref` is NEVER called and the
    exit is non-zero (2). Guards two conflicting `==` units both reaching `main` and bricking a later deploy."""
    monkeypatch.setattr(promote, "_would_brick_generate", lambda run_id, repo: "pin conflict: community.docker")
    called = []
    monkeypatch.setattr(promote.gitio, "promote_ref", lambda *a, **k: called.append(1) or True)
    assert promote.main(["deadbeef", "--repo", "/srv/kontroll.git"]) == 2 and called == []


def test_promote_proceeds_when_clean(monkeypatch):
    """A clean proposal (the gate returns None) promotes — `promote_ref` runs with the run_id and the rc is 0."""
    monkeypatch.setattr(promote, "_would_brick_generate", lambda run_id, repo: None)
    called = []
    monkeypatch.setattr(promote.gitio, "promote_ref", lambda run_id, cwd=None: called.append(run_id) or True)
    assert promote.main(["deadbeef"]) == 0 and called == ["deadbeef"]


def test_skip_flag_bypasses_the_gate(monkeypatch):
    """--skip-conflict-check is the documented emergency override: it bypasses the gate so `promote_ref` runs even
    when the gate WOULD refuse. Guards the escape hatch (and that the gate is otherwise on by default)."""
    monkeypatch.setattr(promote, "_would_brick_generate", lambda *a, **k: "conflict (would normally refuse)")
    called = []
    monkeypatch.setattr(promote.gitio, "promote_ref", lambda *a, **k: called.append(1) or True)
    assert promote.main(["deadbeef", "--skip-conflict-check"]) == 0 and called == [1]


def _unit(repo, key, ver):
    """Write a minimal active community.docker unit pinning `ver` under the repo's instance overlay."""
    d = repo / "instance" / "actuation" / key
    d.mkdir(parents=True)
    (d / "unit.yml").write_text(yaml.safe_dump({
        "schema": 1, "key": key, "status": "active",
        "unit": {"kind": "role", "collection": "community.docker", "name": key},
        "install": {"collections": [{"name": "community.docker", "version": ver}]},
        "target": {"device_class": "docker_host", "inventory_group": "docker_hosts", "blast_radius": "LAN"}}),
        encoding="utf-8")


def test_would_brick_generate_detects_a_real_conflict(tmp_path):
    """End-to-end (real git + subprocess): a `proposed/<id>` ref whose tree has two `==`-conflicting active units
    makes `_would_brick_generate` return a conflict message naming the collection; a single-unit tree returns None.
    Proves the archive → extract → trusted `gen-requirements --conflict-check --root` plumbing actually detects (a
    mock would hide a cwd/path/extract bug — the L2b lesson)."""
    repo = tmp_path / "repo"
    shutil.copytree(os.path.join(ROOT, "scripts"), repo / "scripts")
    (repo / "modules").mkdir()
    shutil.copy(os.path.join(ROOT, "modules", "_core.yml"), repo / "modules" / "_core.yml")
    for rel in ("config", "instance"):
        (repo / rel).mkdir(parents=True, exist_ok=True)
        (repo / rel / "fleet.yml").write_text("enabled_modules: [docker_host]\n", encoding="utf-8")
    _unit(repo, "dock_a", "==1.0.0")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "one"],
                ["git", "branch", "proposed/00000000c1ea"]):
        sp.run(cmd, cwd=repo, env=env, check=True)
    assert promote._would_brick_generate("00000000c1ea", str(repo)) is None        # one unit → no conflict → proceed
    _unit(repo, "dock_b", "==2.0.0")                                        # a second, conflicting `==` pin
    for cmd in (["git", "add", "-A"], ["git", "commit", "-qm", "two"], ["git", "branch", "proposed/000000c0f11c"]):
        sp.run(cmd, cwd=repo, env=env, check=True)
    msg = promote._would_brick_generate("000000c0f11c", str(repo))
    assert msg and "community.docker" in msg and "conflict" in msg.lower()  # the gate would REFUSE this promote


def _fake_proposal(repo, conflict, canary):
    """A minimal tree the generator can judge — modules/_core.yml, a fleet enabling two classes, two module.yml files
    pinning the same collection (`==1.0.0` vs `==2.0.0` when `conflict`) — plus a MALICIOUS scripts/gen-requirements.py
    that writes `canary` and exits 0 ("no conflict"). Committed and exposed as proposed/<id> by the caller."""
    (repo / "modules").mkdir(parents=True)
    (repo / "modules" / "_core.yml").write_text("collections: []\n", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "config" / "fleet.yml").write_text("enabled_modules: [alpha, beta]\n", encoding="utf-8")
    for key, ver in (("alpha", "==1.0.0"), ("beta", "==2.0.0" if conflict else "==1.0.0")):
        (repo / "modules" / key).mkdir()
        (repo / "modules" / key / "module.yml").write_text(
            "key: %s\ncollections:\n  - name: acme.appliance\n    version: '%s'\n" % (key, ver), encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "gen-requirements.py").write_text(
        "import pathlib, sys\npathlib.Path(%r).write_text('the proposal ran its own code')\nsys.exit(0)\n"
        % str(canary), encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "proposal"],
                ["git", "branch", "proposed/0000000000b1"]):
        sp.run(cmd, cwd=repo, env=env, check=True)


def test_the_gate_runs_the_trusted_generator_never_the_proposals_own(tmp_path):
    """THE TRUST PROPERTY (finding 3). A proposal whose own scripts/gen-requirements.py is a canary that exits 0 —
    "no conflict" — while its DATA carries a real `==` conflict. The old gate executed that script (as root on the
    sudo CLI) and would have promoted the conflict; the gate must refuse on the conflict the TRUSTED generator sees,
    and the canary file must never appear."""
    canary = tmp_path / "canary.txt"
    repo = tmp_path / "repo"
    _fake_proposal(repo, conflict=True, canary=canary)
    msg = promote._would_brick_generate("0000000000b1", str(repo))
    assert msg and "acme.appliance" in msg and "conflict" in msg.lower(), "the trusted generator must see the data conflict"
    assert not canary.exists(), "the proposal's own gen-requirements.py must NEVER execute"


def test_the_gate_passes_only_the_whitelisted_environment_and_the_trusted_script(tmp_path, monkeypatch):
    """The generator subprocess is THIS tree's script (under promote.ROOT), run from ROOT with `--root <extract>`
    and `--conflict-check`, and sees none of the caller's secrets: a SOPS_AGE_KEY in the promote's environment (the
    Semaphore path) must not reach it, nor a caller's KONTROLL_WRITE_ROOT. A clean proposal still passes (None)."""
    canary = tmp_path / "canary.txt"
    repo = tmp_path / "repo"
    _fake_proposal(repo, conflict=False, canary=canary)
    monkeypatch.setenv("SOPS_AGE_KEY", "key-material-that-must-not-reach-the-child")
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("PYTHONPATH", os.environ.get("PYTHONPATH", ""))      # Python's own path config must pass
    seen = {}
    real_run = promote.subprocess.run

    def spy(cmd, *a, **kw):
        if any(os.path.basename(str(c)) == "gen-requirements.py" for c in cmd):   # equality, not a substring
            seen["cmd"], seen["env"], seen["cwd"] = list(cmd), dict(kw.get("env") or {}), kw.get("cwd")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(promote.subprocess, "run", spy)
    assert promote._would_brick_generate("0000000000b1", str(repo)) is None
    assert not canary.exists()
    assert seen["cmd"][1:3] == ["-B", os.path.join(promote.ROOT, "scripts", "gen-requirements.py")], (
        "the trusted checkout's generator, never the extract's — and with -B, so that a promote run with sudo from "
        "the operator's checkout leaves no root-owned __pycache__ in it")
    assert "--conflict-check" in seen["cmd"] and "--root" in seen["cmd"], seen["cmd"]
    assert seen["cwd"] == promote.ROOT, "run from the trusted root, not inside the extract"
    assert "SOPS_AGE_KEY" not in seen["env"] and "KONTROLL_WRITE_ROOT" not in seen["env"], \
        "the child inherits only the whitelisted environment"
    assert set(seen["env"]) <= set(promote._GATE_ENV) | {"PYTHONIOENCODING"}
    assert "PYTHONPATH" in seen["env"], \
        "Python's own path configuration must pass through — a runner that locates its packages via PYTHONPATH " \
        "otherwise cannot import yaml in the child (the first private sync found exactly that)"


@pytest.mark.parametrize("other", ["refs/tags/proposed/0000000000b1", "refs/proposed/0000000000b1"], ids=["a-tag", "a-ref-under-refs"])
def test_the_gate_judges_the_ref_the_fast_forward_takes(tmp_path, other):
    """The gate extracts `refs/heads/proposed/<id>` — the ref `promote_ref` fast-forwards — also when another ref
    answers to the short name `proposed/<id>` first: a tag, or a ref directly under `refs/`, that points at a tree
    without the conflict.

    Guards the gate and the fast-forward reading two different refs. Git resolves a short name to
    `refs/<name>` and `refs/tags/<name>` before `refs/heads/<name>`, and the canonical's update hook refuses the
    network service nothing but `main` — so one extra ref beside a proposal made the gate pass a clean tree while
    `main` took the other one, with no race and no `.gitattributes`."""
    repo = tmp_path / "repo"
    _fake_proposal(repo, conflict=True, canary=tmp_path / "canary.txt")     # refs/heads/proposed/0000000000b1: a real conflict
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (repo / "modules" / "beta" / "module.yml").write_text(
        "key: beta\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n", encoding="utf-8")
    for cmd in (["git", "commit", "-qam", "the same tree without the conflict"], ["git", "update-ref", other, "HEAD"],
                ["git", "reset", "-q", "--hard", "HEAD~1"]):
        sp.run(cmd, cwd=repo, env=env, check=True)
    msg = promote._would_brick_generate("0000000000b1", str(repo))
    assert msg and "acme.appliance" in msg and "conflict" in msg.lower(), "the gate judged %s, not the branch" % other
