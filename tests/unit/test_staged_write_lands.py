"""A write the service stages reaches its proposal whole — or the stage fails, and leaves nothing behind.

WHY (the failures these guard). The API and the GUI stage a change by writing files into the propose clone and
handing their paths to `gitio.commit_and_push`, which runs `git add <paths>` and commits. Two things were wrong
with that, and neither could be seen by a suite that mocks git:

  - `instance/actuation/` — where an app-store unit and its values are written — was not re-included by the
    `instance/.gitignore` an instance is scaffolded with, so under the root rule `/instance/*` the path was
    IGNORED. `git add` refuses an ignored path. A unit could be created in the dialog and never reach a proposal.
  - `git add` exits non-zero for a path it refuses and STILL stages the paths it accepts; its status was not
    read. With one path the commit then had nothing to commit and the stage failed — but the roll-back
    (`git reset --hard`) does not remove an ignored file, so the unit stayed in the clone and was listed as
    registered, although it existed nowhere else. With several paths the commit went through WITHOUT the refused
    one: a proposal missing a file, reported as staged.

These tests run the real git: a bare canonical, a clone of it carrying the SHIPPED root `.gitignore` and the
shipped `instance.example/.gitignore`, staging armed. The "instance scaffolded before this change" is the same
tree with the two new re-include lines taken out again.
"""
import ast
import glob
import os
import re
import shutil
import subprocess

import pytest

from kontroll import gitio, paths
from kontroll.service import actuation

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHIPPED_REINCLUDES = open(os.path.join(ROOT, "instance.example", ".gitignore"), encoding="utf-8").read()


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), check=check, capture_output=True, text=True)


def _instance(tmp_path, monkeypatch, reincludes=SHIPPED_REINCLUDES):
    """(clone, canonical): an instance repository as `kontroll-init --fresh` leaves it — the shipped root ignore
    file, an `instance/.gitignore` holding `reincludes`, a fleet file — pushed to a bare canonical as `main`, with
    the clone as ROOT and staging armed."""
    canonical, clone = tmp_path / "canonical.git", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(canonical)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True, capture_output=True)
    for key, value in (("user.email", "t@example.test"), ("user.name", "t"), ("commit.gpgsign", "false"),
                       ("core.autocrlf", "false")):
        _git(clone, "config", key, value)
    shutil.copyfile(os.path.join(ROOT, ".gitignore"), str(clone / ".gitignore"))
    (clone / "instance").mkdir()
    (clone / "instance" / ".gitignore").write_bytes(reincludes.encode("utf-8"))
    (clone / "instance" / "fleet.yml").write_bytes(b"enabled_modules: []\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    _git(clone, "remote", "add", "local", str(canonical))
    _git(clone, "push", "-q", "local", "main")
    monkeypatch.setattr(paths, "ROOT", str(clone))
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    return clone, canonical


def _ignored(tree, rel):
    return _git(tree, "check-ignore", "-q", "--no-index", rel, check=False).returncode == 0


def _probe(entry):
    """A path git can be asked about for the overlay entry `entry`: the entry itself when it names a file, a file
    inside it when it names a directory."""
    name = entry.rsplit("/", 1)[-1]
    return entry if "." in name else entry + "/probe/file.yml"


def test_every_overlay_entry_the_tool_maps_is_one_git_will_take(tmp_path, monkeypatch):
    """Each entry of `paths._OVERLAY_MAP` — the one table that says where the tool writes instance configuration —
    is trackable in a scaffolded instance: not ignored by the root rule, because the shipped
    `instance.example/.gitignore` re-includes it. Guards the next overlay entry being mapped without its
    re-include line: the service writes there, `git add` refuses the path, and nothing staged from the GUI under
    it can reach a proposal. (`actuation` was that entry.)"""
    clone, _ = _instance(tmp_path, monkeypatch)
    targets = sorted({overlay for _legacy, overlay in paths._OVERLAY_MAP})
    assert "instance/actuation" in targets and "instance/fleet.yml" in targets, "the map this test reads has moved"
    ignored = [t for t in targets if _ignored(clone, _probe(t))]
    assert ignored == [], "mapped overlay entries the scaffolded instance/.gitignore does not re-include: %s" % ignored


def test_every_overlay_path_the_example_instance_names_is_one_git_will_take(tmp_path, monkeypatch):
    """Every top-level overlay entry that a file of `instance.example/` mentions (`instance/certs/pve-ca.pem` in
    the example `instance.yml`, which tells the operator to commit a CA certificate there) is trackable in a
    scaffolded instance. Guards an instruction nobody can follow: the file is written, `git add` refuses it, it
    never reaches the canonical, and a worktree rebuilt from the canonical fails its deploy on the missing
    certificate. This test reads PROSE: a sentence in `instance.example/` that names an overlay path as something
    NOT to keep there turns it red — reword the sentence; do not re-include the path."""
    clone, _ = _instance(tmp_path, monkeypatch)
    named = set()
    for directory, _dirs, files in os.walk(os.path.join(ROOT, "instance.example")):
        for name in files:
            try:
                text = open(os.path.join(directory, name), encoding="utf-8").read()
            except UnicodeDecodeError:
                continue
            named.update(m.group(1) for m in re.finditer(r"\binstance/([A-Za-z0-9_.-]+[A-Za-z0-9_])", text))
    assert {"certs", "secrets", "inventory"} <= named, "the example no longer names the paths this test reads: %s" % named
    ignored = sorted(n for n in named if _ignored(clone, _probe("instance/" + n)))
    assert ignored == [], "the example instance names overlay paths git ignores: %s" % ignored


def test_what_the_reinclude_file_says_about_a_key_under_certs_is_true(tmp_path, monkeypatch):
    """The shipped `instance.example/.gitignore` re-includes `certs/` and says, beside the line, what still keeps a
    private key out: its NAME (`*.key`, `id_ed25519`) is ignored there, and the script it names for the content
    check exists. A public certificate in the same directory is trackable. Guards the re-include being shipped
    without the rules it leans on — the two came from different changes, and with `!/certs/` alone a blanket
    `git add -A` would commit `instance/certs/server.key`, a private key that was ignored the day before."""
    clone, _ = _instance(tmp_path, monkeypatch)
    for name in ("server.key", "id_ed25519", "id_rsa", "id_ecdsa"):
        assert _ignored(clone, "instance/certs/" + name), "instance/certs/%s is trackable" % name
    assert not _ignored(clone, "instance/certs/pve-ca.pem")
    named = re.findall(r"\bscripts/[A-Za-z0-9_./-]+", SHIPPED_REINCLUDES)
    assert named, "the re-include file no longer names the guard this test reads"
    missing = [path for path in named if not os.path.isfile(os.path.join(ROOT, path.rstrip(".")))]
    assert missing == [], "the re-include file names scripts that are not in the tree: %s" % missing


def test_a_unit_staged_on_a_scaffolded_instance_reaches_its_proposal(tmp_path, monkeypatch):
    """The unit writer's own descriptor and values paths, written by the real writer and staged through the real
    seam, are IN the canonical's `proposed/<run_id>`, and the clone is back at `main` afterwards. Guards the
    app-store's create and configure steps staging a path git ignores: before the re-include line the stage
    failed (one path) and the unit could never be promoted."""
    clone, canonical = _instance(tmp_path, monkeypatch)
    unit_rel, vars_rel = actuation._unit_rel("demo"), actuation._vars_rel("demo")
    assert unit_rel == "instance/actuation/demo/unit.yml"
    out = actuation.apply_create_unit({"key": "demo", "unit_rel": unit_rel, "unit_content": "key: demo\n"})
    done = gitio.commit_and_push(out["paths"], ["create the unit"], run_id="x10000000001")
    assert done["committed"] is True and done["staged"] is True and "add_rc" not in done
    tree = _git(canonical, "ls-tree", "-r", "--name-only", "proposed/x10000000001").stdout.split()
    assert unit_rel in tree
    assert _git(clone, "status", "--porcelain", "--ignored").stdout == "" and not (clone / unit_rel).exists()

    out = actuation.apply_actuation_plan({"key": "demo", "vars_rel": vars_rel, "vars_content": "x: 1\n"})
    done = gitio.commit_and_push(out["paths"], ["configure the unit"], run_id="x10000000002")
    assert done["committed"] is True
    assert vars_rel in _git(canonical, "ls-tree", "-r", "--name-only", "proposed/x10000000002").stdout.split()


def _before_this_change():
    """The re-include file of an instance scaffolded before `actuation/` was added to it."""
    lines = [ln for ln in SHIPPED_REINCLUDES.split("\n") if ln.strip() not in ("!/actuation/", "!/certs/")]
    assert len(lines) == len(SHIPPED_REINCLUDES.split("\n")) - 2, "the shipped file no longer has the two lines"
    return "\n".join(lines)


def test_a_path_git_refuses_fails_the_whole_stage_and_leaves_nothing_behind(tmp_path, monkeypatch):
    """On an instance whose `instance/.gitignore` predates the re-include (the scaffold never overwrites an
    existing file, so every existing instance is one until the line is added by hand): staging a unit ALONE fails
    and the unit file is gone from the clone afterwards; staging a tracked file TOGETHER with the unit's values
    fails too — no commit, no `proposed/<run_id>` in the canonical — the tracked file is back at its canonical
    content and the values file is gone. Guards the two things the unchecked `git add` did: leaving a unit in the
    clone that the registry then listed although it existed nowhere else, and publishing a proposal that was
    missing one of its files while reporting it as staged."""
    clone, canonical = _instance(tmp_path, monkeypatch, reincludes=_before_this_change())
    unit_rel, vars_rel = actuation._unit_rel("demo"), actuation._vars_rel("demo")
    assert _ignored(clone, unit_rel), "the fixture is not the instance it claims to be"

    actuation.apply_create_unit({"key": "demo", "unit_rel": unit_rel, "unit_content": "key: demo\n"})
    alone = gitio.commit_and_push([unit_rel], ["create the unit"], run_id="x10000000003")
    assert alone["committed"] is False and alone["staged"] is False and alone["add_rc"] != 0
    assert not (clone / unit_rel).exists(), "a refused write was left in the clone, where the registry reads it"

    (clone / "instance" / "fleet.yml").write_bytes(b"enabled_modules: [demo]\n")
    actuation.apply_actuation_plan({"key": "demo", "vars_rel": vars_rel, "vars_content": "x: 1\n"})
    both = gitio.commit_and_push(["instance/fleet.yml", vars_rel], ["enable and configure"], run_id="x10000000004")
    assert both["committed"] is False and both["pushed_canonical"] is False and both["add_rc"] != 0
    refs = _git(canonical, "for-each-ref", "--format=%(refname)").stdout.split()
    assert refs == ["refs/heads/main"], "a part of the change was published: %s" % refs
    assert (clone / "instance" / "fleet.yml").read_bytes() == b"enabled_modules: []\n"
    assert not (clone / vars_rel).exists()
    assert _git(clone, "status", "--porcelain").stdout == ""


def _seam_with(monkeypatch, tmp_path, failing):
    """The seam with its git runner replaced: every call is recorded and the verbs in `failing` exit 1. A unit
    file and an ignored secrets directory stand in the tree, so the clean has a file to be given and a directory
    it must not be. Returns the recorded calls."""
    calls = []

    def fake_run(cmd, cwd=None, quiet_args=0):
        calls.append(list(cmd))
        return 1 if _verb(cmd) in failing else 0

    monkeypatch.setattr(gitio, "_run", fake_run)
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    (tmp_path / "instance" / "actuation" / "demo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "instance" / "actuation" / "demo" / "unit.yml").write_bytes(b"key: demo\n")
    (tmp_path / "instance" / "secrets").mkdir(exist_ok=True)
    (tmp_path / "instance" / "secrets" / "keys.txt").write_bytes(b"an ignored file the clean must never be given\n")
    return calls


def _verb(cmd):
    return cmd[2] if cmd[1] == "--literal-pathspecs" else cmd[1]


UNIT = "instance/actuation/demo/unit.yml"


def test_a_refused_add_commits_nothing_and_only_the_staging_clone_is_rolled_back(tmp_path, monkeypatch, capsys):
    """At the seam, with the git runner replaced: when `git add` exits non-zero no `commit` and no `push` is
    issued, and one line says so with the run id. A STAGING service then rolls its clone back and cleans — with
    literal pathspecs, and only the FILES among the paths it was given: never a directory, never a path that
    climbs out of the tree (which is skipped, not raised). A non-staging caller (the operator's own tree) is
    neither rolled back nor cleaned: the request's paths are taken back out of its index, literally, and nothing
    else. Guards the roll-back reaching a tree that is not the service's to reset; the clean being handed a
    directory — `git clean -x` on `instance/secrets` would delete what is ignored there; and the half of a
    refused request that git did stage being left in the operator's index."""
    calls = _seam_with(monkeypatch, tmp_path, failing={"add"})

    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    out = gitio.commit_and_push([UNIT, "instance/secrets", "../outside.yml"], ["m"], run_id="x10000000005")
    assert out == {"committed": False, "pushed_canonical": False, "pushed_origin": False, "commit_rc": None,
                   "add_rc": 1, "target_ref": None, "staged": False}
    assert [_verb(c) for c in calls] == ["add", "fetch", "reset", "clean"], calls
    assert calls[-1] == ["git", "--literal-pathspecs", "clean", "-f", "-x", "-q", "--", UNIT]
    said = capsys.readouterr().out
    assert "git add refused a path" in said and "(run_id x10000000005)" in said and "could not remove" not in said

    calls.clear()
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    out = gitio.commit_and_push([UNIT, "a?b.yml"], ["m"])
    assert out["committed"] is False and out["add_rc"] == 1
    assert calls == [["git", "add", UNIT, "a?b.yml"],
                     ["git", "--literal-pathspecs", "reset", "-q", "--", UNIT, "a?b.yml"]]


def test_a_refused_file_that_cannot_be_removed_is_said_so(tmp_path, monkeypatch, capsys):
    """When the clean after a refused add itself fails, one line says that a refused file is still in the clone,
    with the run id. Guards a silent leftover: the clean runs with `-q`, its status was not read, and a unit file
    that could not be removed would stay listed as registered with no line of the service's to say why."""
    _seam_with(monkeypatch, tmp_path, failing={"add", "clean"})
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    gitio.commit_and_push([UNIT], ["m"], run_id="x10000000006")
    said = capsys.readouterr().out
    assert "could not remove a file git refused" in said and said.count("(run_id x10000000006)") == 2


def test_a_failed_commit_leaves_no_part_of_the_request_staged(tmp_path, monkeypatch, capsys):
    """At the seam, with the git runner replaced and `git commit` exiting non-zero: nothing is pushed; the staging
    service rolls its clone back (and cleans nothing — git took every path); a non-staging caller gets the
    request's paths taken back out of its index — and with NO path, no reset is issued at all; and when that
    reset itself fails, one line says the paths are still staged. Guards what a failed commit left staged in the
    operator's tree riding into `main` inside the next request's commit, which takes the whole index; a `git
    reset` with no path, which resets the WHOLE index and would un-stage what the operator had staged elsewhere;
    and a locked index leaving the request staged with no line of the service's to say so."""
    calls = _seam_with(monkeypatch, tmp_path, failing={"commit"})

    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    out = gitio.commit_and_push([UNIT], ["m"], run_id="x10000000007")
    assert out["committed"] is False and out["commit_rc"] == 1 and "add_rc" not in out
    assert [_verb(c) for c in calls] == ["add", "commit", "fetch", "reset"], calls
    assert calls[-1] == ["git", "reset", "--hard", "local/main"]

    calls.clear()
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    out = gitio.commit_and_push([UNIT], ["m"])
    assert out["committed"] is False and out["commit_rc"] == 1
    assert [_verb(c) for c in calls] == ["add", "commit", "reset"], calls
    assert calls[-1] == ["git", "--literal-pathspecs", "reset", "-q", "--", UNIT]
    assert "could not take" not in capsys.readouterr().out

    calls.clear()
    assert gitio.commit_and_push([], ["m"])["committed"] is False
    assert [_verb(c) for c in calls] == ["add", "commit"], calls

    calls = _seam_with(monkeypatch, tmp_path / "locked", failing={"commit", "reset"})
    gitio.commit_and_push([UNIT], ["m"])
    assert [_verb(c) for c in calls] == ["add", "commit", "reset"], calls
    assert "could not take the request's paths back out of the index" in capsys.readouterr().out


def test_outside_staging_a_request_that_does_not_commit_leaves_the_index_empty(tmp_path, monkeypatch):
    """With the real git and WITHOUT `KONTROLL_STAGE_PUSHES` — the GUI run from a checkout — on an
    instance whose ignore file predates the re-include: after a refused add, and after a commit that cannot be
    made, the index is empty and the working tree is as the apply left it; and the next request's commit holds
    exactly its own file, while the canonical's `main` still has the first request's file as it was. Guards the
    half of a refused request that git had already staged reaching `main` inside the NEXT request's commit, under
    that request's message, after the first was answered "nothing committed"."""
    clone, canonical = _instance(tmp_path, monkeypatch, reincludes=_before_this_change())
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    vars_rel, fleet = actuation._vars_rel("demo"), clone / "instance" / "fleet.yml"
    fleet.write_bytes(b"enabled_modules: [demo]\n")
    actuation.apply_actuation_plan({"key": "demo", "vars_rel": vars_rel, "vars_content": "x: 1\n"})

    refused = gitio.commit_and_push(["instance/fleet.yml", vars_rel], ["enable and configure"])
    assert refused["committed"] is False and refused["add_rc"] != 0
    assert _git(clone, "diff", "--cached", "--name-only").stdout == ""
    assert fleet.read_bytes() == b"enabled_modules: [demo]\n" and (clone / vars_rel).exists(), \
        "the operator's working tree is not the service's to roll back"

    _git(clone, "config", "user.useConfigOnly", "true")              # a commit that cannot be made: no identity
    _git(clone, "config", "--unset", "user.email")
    failed = gitio.commit_and_push(["instance/fleet.yml"], ["enable"])
    assert failed["committed"] is False and failed["commit_rc"] != 0 and "add_rc" not in failed
    assert _git(clone, "diff", "--cached", "--name-only").stdout == ""

    _git(clone, "config", "user.email", "t@example.test")
    (clone / "README.md").write_bytes(b"another request\n")
    done = gitio.commit_and_push(["README.md"], ["another request"])
    assert done["committed"] is True and done["target_ref"] == "main" and done["pushed_canonical"] is True
    assert _git(clone, "show", "--name-only", "--format=", "HEAD").stdout.split() == ["README.md"]
    assert _git(canonical, "show", "main:instance/fleet.yml").stdout == "enabled_modules: []\n"


def _reads_committed(node):
    """True for `if not <name>["committed"]:` — the form in which a route asks the seam's result whether the stage
    committed."""
    test = node.test if isinstance(node, ast.If) else None
    return (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Subscript)
            and isinstance(test.operand.slice, ast.Constant) and test.operand.slice.value == "committed")


def _calls_the_seam(node):
    """True for a call of `commit_and_push`, as an attribute (`gitio.commit_and_push(…)`) or by its bare name."""
    if not isinstance(node, ast.Call):
        return False
    return ((isinstance(node.func, ast.Attribute) and node.func.attr == "commit_and_push")
            or (isinstance(node.func, ast.Name) and node.func.id == "commit_and_push"))


def routes_that_stage(root=ROOT):
    """[(file, function, reads the result)] for every function of the GUI and of the API's routes that calls
    `commit_and_push`."""
    found = []
    for path in [os.path.join(root, "gui", "app.py")] + sorted(glob.glob(os.path.join(root, "api", "routes", "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(_calls_the_seam(n) for n in ast.walk(fn)):
                found.append((os.path.relpath(path, root).replace(os.sep, "/"), fn.name,
                              any(_reads_committed(n) for n in ast.walk(fn))))
    return found


def seam_calls_the_census_does_not_read(root=ROOT):
    """["path:line"] of every call of `commit_and_push` under api/, gui/ and scripts/ — the seam's own module
    aside — that does not stand inside one of the functions `routes_that_stage()` returns: a caller in a service
    module or another file, or code at module level."""
    read = {(rel, name) for rel, name, _reads in routes_that_stage(root)}
    outside = []
    for top in ("api", "gui", "scripts"):
        for directory, dirs, names in os.walk(os.path.join(root, top)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(n for n in names if n.endswith(".py")):
                path = os.path.join(directory, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                if rel == "scripts/kontroll/gitio.py":
                    continue
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
                inside = set()
                for fn in ast.walk(tree):
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and (rel, fn.name) in read:
                        inside.update(id(n) for n in ast.walk(fn))
                outside += ["%s:%d" % (rel, n.lineno) for n in ast.walk(tree) if _calls_the_seam(n) and id(n) not in inside]
    return sorted(outside)


def test_every_route_that_stages_reads_whether_the_stage_committed():
    """Every function of the GUI and of the API's routes that calls `commit_and_push` tests the result's
    `committed` (`if not git["committed"]:`). Guards a route answering success for a stage that did not commit:
    the seam says a refused add or a failed commit in its RESULT, never by raising, so a route that does not read
    it answers 200 — the GUI's capability route did, and its page printed "committed to main"."""
    routes = routes_that_stage()
    assert {"gui/app.py", "api/routes/capability.py"} <= {f for f, _name, _reads in routes}, \
        "the routes this test reads have moved: %s" % routes
    blind = ["%s:%s" % (f, name) for f, name, reads in routes if not reads]
    assert blind == [], "routes that call commit_and_push and never test its `committed`: %s" % blind
    assert seam_calls_the_census_does_not_read() == [], \
        "callers of commit_and_push this test does not read (a route elsewhere, a service module, a bare-name call)"


def test_the_census_sees_a_caller_wherever_it_stands(tmp_path):
    """On a planted tree: a route that reads the result passes; a route that calls the seam by its BARE name
    (after `from kontroll.gitio import commit_and_push`) and never reads the result is flagged; and a call in a
    service module, which no route file holds, is reported as one the census does not read. Guards the census
    going blind the way the route did: it looked for one spelling of the call in two places, so a caller written
    another way, or standing somewhere else, would have been "every route" too."""
    files = {
        "gui/app.py": "def good():\n    git = gitio.commit_and_push([], [])\n    if not git[\"committed\"]:\n        return 500\n"
                      "    return 200\n\n\ndef bare():\n    git = commit_and_push([], [])\n    return 200\n",
        "api/routes/x.py": "def also_good(g):\n    git = g.commit_and_push([], [])\n    if not git[\"committed\"]:\n        raise E()\n",
        "scripts/kontroll/service/sneaky.py": "def helper():\n    return gitio.commit_and_push([], [])\n",
        "scripts/kontroll/gitio.py": "def commit_and_push(paths, lines):\n    return commit_and_push(paths, lines)\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert routes_that_stage(str(tmp_path)) == [("gui/app.py", "good", True), ("gui/app.py", "bare", False),
                                                ("api/routes/x.py", "also_good", True)]
    assert seam_calls_the_census_does_not_read(str(tmp_path)) == ["scripts/kontroll/service/sneaky.py:2"]
