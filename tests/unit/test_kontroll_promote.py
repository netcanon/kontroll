"""kontroll-promote.py — the FIX-M9 resolve gate before the fast-forward promote (it began as the pin-conflict gate).

WHY (the failures these guard):
  * Two app-store units each pinning the SAME collection to a DIFFERENT exact `==` version both becoming `active`
    makes `gen-requirements` fail closed at GENERATE time — which halts the NEXT deploy's pre-image-build, a brick
    one seam removed from the cause. The promote gate must REFUSE such a promote (and NEVER advance `main`), so two
    conflicting `==` units can never co-exist. A clean proposal must still promote.
  * The detector is THIS tree's `gen-requirements.py --conflict-check --root <copy>` run over the proposal's files
    as DATA — never the proposal's own copy of the script (2026-10-08 review, finding 3: that executed unreviewed
    code as root / with SOPS_AGE_KEY in scope). The wiring (read the commit → write its files → trusted run →
    refuse on non-zero) is exercised end-to-end against real git + subprocess, not only a mock (the L2b lesson: a
    mock hides the plumbing bug), and the trust property is falsified with a proposal whose generator is a canary.
  * The files the gate judges are the COMMIT'S OWN (`canon.read_tree_verified`), and a gate that cannot judge
    REFUSES: every way it used to answer "nothing to check" about a tree it had not read was a way to switch it
    off (the rows from "the gate reads the commit itself" on).
"""
import importlib.util
import os
import shutil
import subprocess as sp

import pytest
import yaml

import _canon_repo as R
from kontroll import canon

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
    assert promote.main(["0000deadbeef", "--repo", "/srv/kontroll.git"]) == 2 and called == []


def test_promote_proceeds_when_clean(monkeypatch):
    """A clean proposal (the gate returns None) promotes — `promote_ref` runs with the run_id and the rc is 0."""
    monkeypatch.setattr(promote, "_would_brick_generate", lambda run_id, repo: None)
    called = []
    monkeypatch.setattr(promote.gitio, "promote_ref", lambda run_id, cwd=None: called.append(run_id) or True)
    assert promote.main(["0000deadbeef"]) == 0 and called == ["0000deadbeef"]


def test_skip_flag_bypasses_the_gate(monkeypatch):
    """--skip-conflict-check is the documented emergency override: it bypasses the gate so `promote_ref` runs even
    when the gate WOULD refuse. Guards the escape hatch (and that the gate is otherwise on by default)."""
    monkeypatch.setattr(promote, "_would_brick_generate", lambda *a, **k: "conflict (would normally refuse)")
    called = []
    monkeypatch.setattr(promote.gitio, "promote_ref", lambda *a, **k: called.append(1) or True)
    assert promote.main(["0000deadbeef", "--skip-conflict-check"]) == 0 and called == [1]


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
    Proves the read → copy → trusted `gen-requirements --conflict-check --root` plumbing actually detects (a
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


# ---- the gate reads the commit itself (C10) --------------------------------------------------------------------- #
# From here on the canonical is a BARE repository built from bytes (tests/unit/_canon_repo.py), `main` is a tree
# the generator passes, and each test stages one proposal and asks the gate — or the whole command — about it.

_ALPHA = b"key: alpha\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n"
_BETA_SAME = b"key: beta\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n"
_BETA_CONFLICT = b"key: beta\ncollections:\n  - name: acme.appliance\n    version: '==2.0.0'\n"
_CLEAN = {b"modules/_core.yml": b"collections: []\n", b"config/fleet.yml": b"enabled_modules: [alpha, beta]\n",
          b"modules/alpha/module.yml": _ALPHA, b"modules/beta/module.yml": _BETA_SAME}
_CONFLICT = {**_CLEAN, **{b"modules/beta/module.yml": _BETA_CONFLICT}}
RID = "0000000000b1"


def _canonical(tmp_path, proposal_files=None, main_files=None):
    """A bare canonical whose `main` holds `main_files` (default: the clean tree) and, when `proposal_files` is
    given, a proposal `RID` one commit ahead of it. Returns the namespace of `_canon_repo.build` with `main`
    re-pointed and `proposal` set."""
    r = R.build(tmp_path)
    r.main = R.commit(r.bare, R.snapshot(r.bare, main_files or _CLEAN), [r.main], subject=b"main")
    R.set_ref(r.bare, canon.MAIN, r.main)
    r.proposal = None
    if proposal_files is not None:
        r.proposal = R.commit(r.bare, R.snapshot(r.bare, proposal_files), [r.main], subject=b"feat: a proposal")
        R.set_ref(r.bare, canon.PROPOSED + RID, r.proposal)
    return r


def _main_of(bare):
    return canon.read_ref(bare, canon.MAIN)[0]


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("hidden", [b"modules export-ignore\n", b"modules/beta export-ignore\n",
                                    b"modules/beta/module.yml export-ignore\n", b"* export-ignore\n"],
                         ids=["the-registry", "one-class", "one-file", "everything"])
def test_a_proposal_cannot_hide_a_path_from_the_gate(tmp_path, hidden):
    """A proposal whose `.gitattributes` marks `modules/`, one class, one module file or everything
    `export-ignore`, and whose hidden content holds a real `==` conflict, is REFUSED on that conflict. Guards the
    read the gate made before: it judged `git archive`'s export of the proposal, so the proposal's own attribute
    file took the conflict — or the whole registry, after which the gate answered "nothing to check" — out of
    what was judged, and the fast-forward then landed all of it."""
    r = _canonical(tmp_path, {**_CONFLICT, **{b".gitattributes": hidden}})
    msg = promote._would_brick_generate(RID, r.bare)
    assert msg and "acme.appliance" in msg and "conflict" in msg.lower()
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main


@pytest.mark.usefixtures("isolated_git")
def test_the_generator_is_handed_the_committed_bytes(tmp_path, monkeypatch):
    """With `* export-subst` on a module file that holds `$Format:%H$`, an `info/attributes` in the repository
    that hides the fleet file, and a filter the repository's config routes every path through, the temp root the
    generator is run over holds each file BYTE FOR BYTE as committed — and the filter's program never runs.
    Guards the other half of "the gate judges the commit": an export rewrites a file for the judge
    (`export-subst`), a repository file that is in no commit changes the judged tree (`info/attributes`), and a
    filter is a program of the repository's choosing run as whoever promotes."""
    files = {**_CLEAN, **{b".gitattributes": b"* export-subst\n* filter=x\n",
                            b"modules/alpha/notes.txt": b"commit $Format:%H$\n"}}
    r = _canonical(tmp_path, files)
    canary = tmp_path / "ran"
    os.makedirs(os.path.join(r.bare, "info"), exist_ok=True)
    with open(os.path.join(r.bare, "info", "attributes"), "w", encoding="ascii", newline="\n") as fh:
        fh.write("config export-ignore\n")
    prog = "sh -c 'echo ran >> %s; cat'" % str(canary).replace("\\", "/")
    R.git(r.bare, "config", "filter.x.smudge", prog)
    R.git(r.bare, "config", "filter.x.clean", prog)
    seen = {}
    real_run = promote.subprocess.run

    def spy(cmd, *a, **kw):
        if "--root" in cmd:                                 # the generator's run, not a git of the fixture
            root = cmd[cmd.index("--root") + 1]
            for dirpath, _, names in os.walk(root):
                for name in names:
                    full = os.path.join(dirpath, name)
                    with open(full, "rb") as fh:
                        seen[os.path.relpath(full, root).replace(os.sep, "/").encode("utf-8")] = fh.read()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(promote.subprocess, "run", spy)
    assert promote._would_brick_generate(RID, r.bare) is None
    assert seen == files
    assert not canary.exists(), "a filter of the repository ran while the gate read the proposal"


@pytest.mark.usefixtures("isolated_git")
def test_the_gate_judges_the_tree_the_commit_names_not_the_commit_graphs(tmp_path):
    """A proposal with a real conflict, in a repository whose commit-graph gives the proposal's commit the CLEAN
    tree of `main`, is refused on the conflict. Guards the gate against a file under `objects/` — which the
    service must be able to write — deciding which tree is judged."""
    r = _canonical(tmp_path, _CONFLICT)
    R.plant_lying_commit_graph(r, tree_of=(r.proposal, R.git(r.bare, "rev-parse", r.main + "^{tree}").decode()))
    msg = promote._would_brick_generate(RID, r.bare)
    assert msg and "acme.appliance" in msg and "conflict" in msg.lower()


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("which", ["blob", "tree"])
def test_an_object_swapped_under_the_proposal_refuses_the_promote(tmp_path, which):
    """The proposal holds a conflict; then the conflicting module file's object (or the proposal's root tree) is
    replaced IN THE OBJECT STORE by the clean one under the same name. The command exits 2 — "not what its name
    says" — and `main` does not move. Guards judged-is-promoted against a writer of the object store: git returns
    the swapped object without complaint, so a gate that believed it would pass the proposal, and the writer puts
    the real object back before the node reads `main`."""
    r = _canonical(tmp_path, _CONFLICT)
    if which == "blob":
        R.swap_loose(r.bare, R.blob(r.bare, _BETA_CONFLICT), b"blob", _BETA_SAME)
    else:
        clean_tree = R.git(r.bare, "rev-parse", r.main + "^{tree}").decode()
        R.swap_loose(r.bare, R.git(r.bare, "rev-parse", r.proposal + "^{tree}").decode(), b"tree",
                     R.raw(r.bare, "tree", clean_tree))
    msg = promote._would_brick_generate(RID, r.bare)
    assert msg and "not what its name says" in msg
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("label, entry, rule", [
    ("a link", (b"120000", b"link.yml"), "not a regular file"),
    ("a submodule", (b"160000", b"sub"), "not a regular file"),
    ("a .git entry", (b"100644", b".git"), "no checkout could take"),
    ("a name with a newline", (b"100644", b"a\nb.yml"), "no checkout could take"),
])
def test_a_proposal_with_an_entry_no_checkout_could_take_is_refused(tmp_path, capsys, label, entry, rule):
    """A proposal whose tree holds, among its modules, a symbolic link, a submodule entry, a `.git` entry or a
    name with a newline exits 2 with the rule; `main` does not move, and the sentence holds nothing of the entry.
    Guards "refuse, never skip" at the door: a link in a promoted tree is a file some later reader opens THROUGH
    — and the old gate unpacked the export with `tar`, which created it."""
    r = _canonical(tmp_path)
    mode, name = entry
    target = r.base if mode == b"160000" else R.blob(r.bare, b"/etc/hostname")
    mods = R.raw_tree(r.bare, [(b"100644", b"_core.yml", R.blob(r.bare, b"collections: []\n")), (mode, name, target)])
    top = R.raw_tree(r.bare, [(b"40000", b"modules", mods)])
    R.set_ref(r.bare, canon.PROPOSED + RID, R.commit(r.bare, top, [r.main]))
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    err = capsys.readouterr().err
    assert rule in err and "link.yml" not in err and "hostname" not in err


@pytest.mark.usefixtures("isolated_git")
def test_a_proposal_that_removes_the_registry_is_refused_and_one_without_any_is_not(tmp_path, capsys):
    """A proposal with no `modules/` where `main` has one exits 2 — it would remove the device-class registry. In
    a repository where neither `main` nor the proposal has one (a toy repository, the tests' own), the gate has
    nothing to judge and the promote goes through. Guards the one "nothing to check" the gate still has: it is
    decided on two trees that were read, where it used to be the answer to any export that came out without a
    `modules/` — including one the proposal emptied."""
    r = _canonical(tmp_path, {b"config/fleet.yml": b"enabled_modules: []\n"})
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    assert "would remove the device-class registry" in capsys.readouterr().err

    bare = _canonical(tmp_path / "toy", {b"notes.txt": b"x\n", b"more.txt": b"y\n"}, main_files={b"notes.txt": b"x\n"})
    assert promote._would_brick_generate(RID, bare.bare) is None
    assert promote.main([RID, "--repo", bare.bare]) == 0 and _main_of(bare.bare) == bare.proposal


@pytest.mark.usefixtures("isolated_git")
def test_a_gate_that_cannot_run_refuses_and_says_so(tmp_path, monkeypatch, capsys):
    """The ways the gate cannot judge — this checkout has no generator, the generator does not finish, the
    repository cannot be read, git cannot be started for the proposal's tree — each exit 2 with "the gate could not run" and the reason, `main` unmoved; and with
    `--skip-conflict-check` the same promote goes through. Guards the returns that used to fail OPEN: each of
    them let the absence of a judgment count as a pass, so breaking the gate was a way through it. The override
    is the human's, and it is said in the sentence."""
    r = _canonical(tmp_path, {**_CLEAN, b"modules/gamma/module.yml": b"key: gamma\n"})
    real_root, real_seconds = promote.ROOT, promote._GATE_SECONDS
    elsewhere = tmp_path / "checkout-without-a-generator"
    (elsewhere / "scripts").mkdir(parents=True)
    monkeypatch.setattr(promote, "ROOT", str(elsewhere))
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    err = capsys.readouterr().err
    assert "the gate could not run" in err and "gen-requirements.py" in err and "--skip-conflict-check" in err

    (elsewhere / "scripts" / "gen-requirements.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(promote, "_GATE_SECONDS", 1)
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    assert "did not finish" in capsys.readouterr().err

    monkeypatch.setattr(promote, "ROOT", real_root)
    monkeypatch.setattr(promote, "_GATE_SECONDS", real_seconds)
    planted = R.build(tmp_path / "other")
    R.plant_commondir(r, planted.bare)
    assert promote.main([RID, "--repo", r.bare]) == 2
    assert "the gate could not run" in capsys.readouterr().err
    os.remove(os.path.join(r.bare, "commondir"))
    assert _main_of(r.bare) == r.main

    # the refs read fine, and then git cannot be started for the tree: still "could not run", never a pass
    real_popen = canon.subprocess.Popen

    def absent(argv, *a, **kw):
        if list(argv[:2]) == ["git", "cat-file"]:               # the tree reader's process, and only it
            raise FileNotFoundError("git")
        return real_popen(argv, *a, **kw)
    monkeypatch.setattr(canon.subprocess, "Popen", absent)
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    assert "the gate could not run" in capsys.readouterr().err
    monkeypatch.setattr(canon.subprocess, "Popen", real_popen)

    monkeypatch.setattr(promote, "ROOT", str(elsewhere))
    assert promote.main([RID, "--repo", r.bare, "--skip-conflict-check"]) == 0 and _main_of(r.bare) == r.proposal


@pytest.mark.usefixtures("isolated_git")
def test_a_ref_that_is_not_a_proposal_is_not_judged_as_one(tmp_path, capsys):
    """A symbolic ref under a proposal's name (pointing at `main`) is refused by the gate — exit 2, "not a
    proposal" — and a run id that names no ref is not judged at all: the fast-forward says there is no such
    proposal and the exit is 1. Guards the gate against judging the tree a POINTER leads to: that tree is `main`'s
    own, which passes, while what the name stands for is not a commit anybody proposed."""
    r = _canonical(tmp_path)
    R.write_ref_file(r.bare, canon.PROPOSED + RID, "ref: " + canon.MAIN)
    assert promote.main([RID, "--repo", r.bare]) == 2
    assert "not a proposal" in capsys.readouterr().err
    assert promote._would_brick_generate("ffffffffffff", r.bare) is None
    assert promote.main(["ffffffffffff", "--repo", r.bare]) == 1 and _main_of(r.bare) == r.main


@pytest.mark.usefixtures("isolated_git")
def test_a_proposal_ref_that_names_a_tag_is_not_a_proposal(tmp_path, capsys):
    """A ref under a proposal's name whose value is an annotated TAG of a commit (written as a file — `update-ref`
    would not) exits 2 with "not a proposal" and `main` does not move. Guards the second half of that test: a
    gate that turned away only a symbolic ref would peel the tag, or report the wrong fault for it, and the
    fast-forward resolves the same name with a peel of its own."""
    r = _canonical(tmp_path, _CLEAN)
    tag = R.git(r.bare, "hash-object", "-t", "tag", "-w", "--stdin",
                stdin=b"object " + r.proposal.encode() + b"\ntype commit\ntag t\ntagger t <t@example.test> 1750000000 +0000\n\nx\n").decode()
    os.remove(os.path.join(r.bare, "refs", "heads", "proposed", RID))
    R.write_ref_file(r.bare, canon.PROPOSED + RID, tag)
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    assert "not a proposal" in capsys.readouterr().err


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("shape", ["symbolic", "a-tag"])
def test_a_main_that_is_not_a_plain_branch_is_not_no_main_at_all(tmp_path, capsys, shape):
    """With `refs/heads/main` a symbolic ref to another branch — or the id of an annotated tag — and a proposal
    that REMOVES `modules/`, the promote exits 2 ("`main` is not a branch") and neither `main` nor the branch
    behind the pointer moves. Guards the gate's one comparison with `main`: a `main` it could not take as a
    commit used to count as "no `main` to compare with", so the proposal that removes the registry passed and
    the fast-forward — which follows the pointer, and peels the tag — landed it, exit 0."""
    r = _canonical(tmp_path, {b"config/fleet.yml": b"enabled_modules: []\n"})
    os.remove(os.path.join(r.bare, "refs", "heads", "main"))
    if shape == "symbolic":
        R.set_ref(r.bare, "refs/heads/real", r.main)
        R.write_ref_file(r.bare, canon.MAIN, "ref: refs/heads/real")
    else:
        tag = R.git(r.bare, "hash-object", "-t", "tag", "-w", "--stdin",
                    stdin=b"object " + r.main.encode() + b"\ntype commit\ntag t\ntagger t <t@example.test> 1750000000 +0000\n\nx\n").decode()
        R.write_ref_file(r.bare, canon.MAIN, tag)
    before = canon.read_ref(r.bare, canon.MAIN)
    assert promote.main([RID, "--repo", r.bare]) == 2
    assert "`main` is not a branch" in capsys.readouterr().err
    assert canon.read_ref(r.bare, canon.MAIN) == before
    if shape == "symbolic":
        assert R.git(r.bare, "rev-parse", "refs/heads/real").decode() == r.main


@pytest.mark.usefixtures("isolated_git")
def test_only_a_directory_called_modules_is_a_registry(tmp_path):
    """In a repository where `main` has no registry, a proposal that holds a FILE called `modules` and a directory
    `modules-old/` has no registry either: the gate has nothing to judge and answers None. Guards the one test
    that decides whether the generator runs at all — a prefix test without its slash would call both a registry,
    run the generator over a tree with no `modules/_core.yml`, and refuse a proposal for a traceback."""
    r = _canonical(tmp_path, {b"notes.txt": b"x\n", b"modules": b"a file\n", b"modules-old/x.yml": b"k: v\n"},
                   main_files={b"notes.txt": b"x\n"})
    assert promote._would_brick_generate(RID, r.bare) is None


@pytest.mark.usefixtures("isolated_git")
def test_a_proposal_without_a_fleet_file_is_not_judged_with_this_checkouts_example(tmp_path, capsys):
    """`main` keeps its fleet in `instance/fleet.yml`; the proposal deletes `instance/`, adds a class whose pin
    conflicts with another's, and names both in an `instance.example/fleet.yml` of its own. The promote exits 2.
    Guards "the gate judges the commit": for a tree with no `instance/` and no `config/fleet.yml` the generator
    reads the shipped example fleet of the checkout it RUNS from — the promoter's, where neither class is
    enabled — so the gate passed, and the same tree, once it is the checkout, reads its own example and stops on
    the conflict."""
    main_files = {b"modules/_core.yml": b"collections: []\n", b"instance/fleet.yml": b"enabled_modules: [alpha]\n",
                  b"modules/alpha/module.yml": _ALPHA}
    proposal = {b"modules/_core.yml": b"collections: []\n", b"modules/alpha/module.yml": _ALPHA,
                b"modules/beta/module.yml": _BETA_CONFLICT, b"instance.example/fleet.yml": b"enabled_modules: [alpha, beta]\n"}
    r = _canonical(tmp_path, proposal, main_files=main_files)
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    assert "no fleet file" in capsys.readouterr().err


@pytest.mark.usefixtures("isolated_git")
def test_a_name_no_file_system_takes_is_the_proposals_fault_not_the_gates(tmp_path, capsys):
    """A proposal with a clean registry and one file whose name is 300 bytes long exits 2 as a REFUSED TREE — "no
    checkout could take" — and not as "the gate could not run". Guards who is told what: the second sentence
    offers `--skip-conflict-check` for a node whose gate is broken, and a proposal must not be able to choose it."""
    r = _canonical(tmp_path, {**_CLEAN, b"n" * 300: b"x\n"})
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    err = capsys.readouterr().err
    assert "no checkout could take" in err and "could not run" not in err


@pytest.mark.usefixtures("isolated_git")
def test_what_the_generator_said_is_shown_bounded(tmp_path, capsys):
    """A proposal whose `modules/_core.yml` is nested three thousand lists deep (the generator ends in a
    RecursionError with a traceback of thousands of lines) is refused with at most a screen of it. Guards the
    refusal an operator reads — and a Semaphore task logs — against being as long as the proposal makes it."""
    r = _canonical(tmp_path, {**_CLEAN, b"modules/_core.yml": b"collections: " + b"[" * 3000 + b"]" * 3000 + b"\n"})
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    err = capsys.readouterr().err
    assert len(err.splitlines()) < 30 and "earlier lines of the generator's output are not shown" in err


@pytest.mark.usefixtures("isolated_git")
def test_a_generator_that_fails_without_a_word_still_refuses(tmp_path, monkeypatch, capsys):
    """When the generator exits non-zero and writes nothing, the promote is refused (exit 2) with a sentence of the
    gate's own, and `main` does not move; and when the generator cannot be STARTED the promote is refused with
    "the gate could not run". Guards the last two fail-open returns of the old gate's shape: the refusal is the
    generator's EXIT STATUS, never whether it had something to say."""
    r = _canonical(tmp_path, _CLEAN)
    elsewhere = tmp_path / "checkout"
    (elsewhere / "scripts").mkdir(parents=True)
    (elsewhere / "scripts" / "gen-requirements.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(promote, "ROOT", str(elsewhere))
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    assert "generation would fail closed" in capsys.readouterr().err

    real_run = promote.subprocess.run

    def no_interpreter(cmd, *a, **kw):
        if "--root" in cmd:
            raise PermissionError("the interpreter cannot be run")
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(promote.subprocess, "run", no_interpreter)
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    err = capsys.readouterr().err
    assert "the gate could not run" in err and "could not be started" in err


@pytest.mark.usefixtures("isolated_git")
def test_the_hint_about_main_is_given_only_when_main_was_judged_and_refused(tmp_path, monkeypatch, capsys):
    """A proposal refused for its own conflict, in a repository whose `main` cannot be judged at that moment (the
    gate raises "could not run" for `main`'s commit), is refused WITHOUT the sentence "`main` as it stands is
    refused as well". Guards the hint against being a guess: it sends the operator to correct `main`, so it is
    said only when the same gate was asked about `main` and refused it."""
    r = _canonical(tmp_path, _CONFLICT)
    real = promote._tree_refusal

    def main_cannot_be_judged(commit_id, gitdir, main_id=None):
        if commit_id == r.main:
            raise promote.GateCouldNotRun("the collection generator could not be started")
        return real(commit_id, gitdir, main_id)
    monkeypatch.setattr(promote, "_tree_refusal", main_cannot_be_judged)
    assert promote.main([RID, "--repo", r.bare]) == 2 and _main_of(r.bare) == r.main
    err = capsys.readouterr().err
    assert "acme.appliance" in err and "refused as well" not in err


@pytest.mark.parametrize("bad", ["-h0", "--repo", "main", "refs/heads/main", "proposed/0000000000b1", "0000000000B1",
                                 "0000000000b1\n", "../0000000000b1", ""])
def test_only_a_minted_run_id_reaches_the_gate_or_git(monkeypatch, capsys, bad):
    """Anything but a minted run id — an option-like word, a branch name, a full ref name, upper case, the id with
    a newline — exits 1 before the gate or the fast-forward is called, and the sentence does not repeat it. Guards
    the argument's way into a ref name: the id used to be formatted into `refs/heads/proposed/%s` as given."""
    called = []
    monkeypatch.setattr(promote, "_would_brick_generate", lambda *a, **k: called.append("gate"))
    monkeypatch.setattr(promote.gitio, "promote_ref", lambda *a, **k: called.append("promote") or True)
    assert promote.main(["--repo", "/srv/kontroll.git", "--", bad]) == 1 and called == []
    assert bad.strip() == "" or bad.strip() not in capsys.readouterr().err
