"""A module reconfigure on a BAKED install plans from, and writes to, the propose clone — never the image tree.

WHY (the failure these guard). On a baked install the services run from a read-only image tree at paths.ROOT
(`/opt/kontroll`), and the one tree they may write is the propose clone at paths.write_root() (`/propose`). A class
is READ through the overlay — the clone's copy when there is one, the baked one otherwise (paths.module_file). The
capability reconfigures (telemetry, logging, backup) each spelled the path themselves, against ROOT, and identity
wrote to whatever path it had read. So on the install shape the project recommends:

  - a class the operator onboarded (it exists only in the clone) opened its dialog and was then "not onboarded";
  - a shipped class was planned from the image's bytes while its current values were read from the clone — an
    earlier reconfigure silently vanished from the text about to be written, and the no-drop check could not see
    it, because both structures it compares came from the clone;
  - the write went to the read-only image tree (a PermissionError inside the route);
  - and had it succeeded, the scoped commit staged the path in the CLONE, where nothing had changed.

paths.module_write_path() names the write target (always under the write root) and _blockwrite.write_module() is
the one place these reconfigures write. Two real directories stand in for the image and the clone; the last test
runs the real git seam and reads the proposal back out of the canonical.
"""
import os
import subprocess

import pytest

from kontroll import gitio, paths
from kontroll.service import backup, identity, logsvc, observe

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

KEY = "demo_sw"
REL = "modules/%s/module.yml" % KEY

# A hand-written class, as the image ships it...
SHIPPED = """# a hand-written class: this comment must survive a reconfigure
key: demo_sw
description: a demo switch
status: active
collections:
  - name: cisco.ios
role: cisco_ios
backend: network_cli
inventory_group: core_switch
secrets_domain: network
"""
# ...and as the clone holds it after an EARLIER reconfigure the image has never seen.
EDITED = SHIPPED.replace("description: a demo switch", "description: a demo switch, re-described in the clone")

# (name, the service module, a selection its build_plan accepts and that changes the class)
RECONFIGURES = [
    ("telemetry", observe, {"method": "host_node"}),
    ("logging", logsvc, {"method": "journald_remote"}),
    ("backup", backup, {"method": "backup", "params": {"schedule": "0 2 * * *"}}),
    ("identity", identity, {"values": {"status": "staged"}}),
]
IDS = [r[0] for r in RECONFIGURES]


def _put(root, text):
    path = root / "modules" / KEY / "module.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """(baked, clone): the read-only image tree as paths.ROOT and the propose clone as the write root. With ROOT
    repointed the three regenerators are their own self-protecting no-op, so no generator runs against this
    checkout."""
    baked, clone = tmp_path / "opt" / "kontroll", tmp_path / "propose"
    baked.mkdir(parents=True)
    clone.mkdir()
    monkeypatch.setattr(paths, "ROOT", str(baked))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(clone))
    return baked, clone


@pytest.mark.parametrize("name,service,selection", RECONFIGURES, ids=IDS)
def test_a_plan_reads_the_clone_copy_of_a_shipped_class(roots, name, service, selection):
    """With the class in BOTH roots and different bytes in each, the plan's `text_before` is the clone's, and what
    the clone added is still in `text_after`. Guards planning against the image's bytes: the earlier reconfigure
    would be dropped from the text about to be written, and the no-drop gate — which compares two structures that
    both come from the clone — could not notice."""
    baked, clone = roots
    _put(baked, SHIPPED)
    _put(clone, EDITED)
    plan = service.build_plan(KEY, selection)
    assert plan["error"] is None, plan
    assert plan["text_before"] == EDITED
    assert "re-described in the clone" in plan["text_after"] and plan["text_after"] != plan["text_before"]


@pytest.mark.parametrize("name,service,selection", RECONFIGURES, ids=IDS)
def test_a_plan_sees_a_class_the_operator_onboarded(roots, name, service, selection):
    """A class that exists ONLY in the clone — every class an operator onboards on a baked install — is planned,
    not answered with `not_onboarded`. Guards the dialog opening (its first step reads through the overlay and
    finds the class) and the propose step then denying that the class exists."""
    _, clone = roots
    _put(clone, SHIPPED)
    assert service.build_plan(KEY, selection)["error"] is None


@pytest.mark.parametrize("name,service,selection", RECONFIGURES, ids=IDS)
def test_apply_writes_the_clone_and_never_the_image_tree(roots, name, service, selection):
    """After apply the clone's file is the plan's `text_after`, the baked file is byte-for-byte what it was, the
    plan's write target lies under the write root, and the result lists the module among the paths to stage.
    Guards the write going to the read-only image tree (a PermissionError there; here, where the directory is
    writable, a changed baked file) and a proposal that stages a file in which nothing changed."""
    baked, clone = roots
    shipped = _put(baked, SHIPPED)
    mine = _put(clone, EDITED)
    plan = service.build_plan(KEY, selection)
    assert os.path.realpath(plan["mod_path"]).startswith(os.path.realpath(str(clone)) + os.sep)
    out = service.apply_plan(plan)
    assert out["changed"] is True and REL in out["paths"]
    assert mine.read_bytes().decode("utf-8") == plan["text_after"]
    assert shipped.read_bytes().decode("utf-8") == SHIPPED


@pytest.mark.parametrize("name,service,selection", RECONFIGURES, ids=IDS)
def test_a_shipped_class_the_clone_does_not_hold_lands_in_the_clone(roots, name, service, selection):
    """The class exists ONLY in the image tree: the plan is read from there, and apply creates the file — and its
    directory — in the clone, leaving the baked one alone. Guards the write target being whatever path the class
    was read from, which for such a class is the read-only tree (identity did exactly this)."""
    baked, clone = roots
    shipped = _put(baked, SHIPPED)
    plan = service.build_plan(KEY, selection)
    assert plan["error"] is None and plan["text_before"] == SHIPPED
    assert service.apply_plan(plan)["changed"] is True
    assert (clone / "modules" / KEY / "module.yml").read_bytes().decode("utf-8") == plan["text_after"]
    assert shipped.read_bytes().decode("utf-8") == SHIPPED


@pytest.mark.parametrize("name,service,selection,text", [
    ("identity", identity, {"values": {"status": "active"}}, SHIPPED),
    ("telemetry", observe, {"method": "host_node"}, SHIPPED + "metrics:\n  - {method: host_node}\n"),
], ids=["identity", "telemetry"])
def test_a_plan_that_changes_nothing_writes_nothing_in_either_root(roots, name, service, selection, text):
    """A proposal whose values are already the class's values reports no change and creates NO file in the clone,
    although the class is read from the image tree and the write target does not exist yet. Guards the read/write
    split turning a no-op into a copy of a shipped file into the clone — after which a second apply would no
    longer report "no change", and every later image update of that class would be shadowed by the stale copy."""
    baked, clone = roots
    _put(baked, text)
    plan = service.build_plan(KEY, selection)
    assert plan["error"] is None and plan["text_after"] == plan["text_before"]
    assert service.apply_plan(plan)["changed"] is False
    assert not (clone / "modules").exists()


# (name, the service module, the regenerator its apply calls) — the reconfigures that regenerate files
REGENERATING = [("telemetry", observe, "regenerate_observability"), ("logging", logsvc, "regenerate_logging"),
                ("backup", backup, "regenerate_schedules")]


@pytest.mark.parametrize("held_by_the_clone", [True, False], ids=["a-class-the-clone-holds", "a-class-only-the-image-holds"])
@pytest.mark.parametrize("name,service,regenerator", REGENERATING, ids=[r[0] for r in REGENERATING])
def test_a_module_the_clone_does_not_hold_is_not_handed_to_the_commit(roots, monkeypatch, name, service, regenerator,
                                                                      held_by_the_clone):
    """A plan that changes nothing, while the regenerator still returns a file — in production it returns its
    files whether or not they changed; here it is replaced by such a return value. The result lists the
    regenerated file, and the module ONLY when the clone holds it; and no file is created in the clone. Guards a
    path that is not in the clone reaching `git add`: for a class only the image holds, the add fails for the
    missing path and takes the whole stage with it."""
    baked, clone = roots
    selection = next(s for n, _service, s in RECONFIGURES if n == name)
    _put(baked, SHIPPED)
    declared = service.build_plan(KEY, selection)["text_after"]       # the class once this selection is declared
    assert declared != SHIPPED
    _put(baked, declared)
    if held_by_the_clone:
        _put(clone, declared)
    monkeypatch.setattr(service, regenerator, lambda *args: ["generated/by-the-stand-in.yml"])
    plan = service.build_plan(KEY, selection)
    assert plan["error"] is None and plan["text_after"] == plan["text_before"], plan
    out = service.apply_plan(plan)
    assert out["paths"] == ["generated/by-the-stand-in.yml"] + ([REL] if held_by_the_clone else [])
    assert (clone / "modules").exists() is held_by_the_clone


@pytest.mark.parametrize("name,service,selection", RECONFIGURES, ids=IDS)
def test_without_a_write_root_the_class_is_written_where_it_is_read(tmp_path, monkeypatch, name, service, selection):
    """With KONTROLL_WRITE_ROOT unset — the CLI, the test suite, the legacy clone-mounted deploy — the write
    target is the file under ROOT, and apply changes that one file. Guards the fix moving a write for every
    install that is not baked."""
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    here = _put(tmp_path, SHIPPED)
    plan = service.build_plan(KEY, selection)
    assert os.path.samefile(os.path.dirname(plan["mod_path"]), str(here.parent))
    assert service.apply_plan(plan)["changed"] is True
    assert here.read_bytes().decode("utf-8") == plan["text_after"]


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True, text=True).stdout


@pytest.mark.parametrize("held_by_the_clone", [True, False], ids=["a-class-the-clone-holds", "a-class-only-the-image-holds"])
@pytest.mark.parametrize("name,service,selection", [RECONFIGURES[0], RECONFIGURES[3]], ids=["telemetry", "identity"])
def test_the_staged_proposal_contains_the_module_change(tmp_path, monkeypatch, held_by_the_clone, name, service,
                                                        selection):
    """Through the REAL git seam, on the baked layout: a canonical, a clone of it as the write root, an image tree
    as ROOT, staging armed. After plan → apply → commit_and_push, the canonical's `proposed/<run_id>` differs from
    `main` in exactly `modules/<key>/module.yml`, that file there is the plan's `text_after`, the image tree is
    untouched, and the clone is back at `main`. Guards the fourth symptom, which no test of the planner or of the
    writer alone can see: a proposal that is committed, pushed and reported as staged — and does not contain the
    change it was made for."""
    canonical, clone, baked = tmp_path / "canonical.git", tmp_path / "propose", tmp_path / "opt" / "kontroll"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(canonical)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True, capture_output=True)
    for key, value in (("user.email", "t@example.test"), ("user.name", "t"), ("commit.gpgsign", "false"),
                       ("core.autocrlf", "false")):
        _git(clone, "config", key, value)
    (clone / "README.md").write_bytes(b"the canonical\n")
    if held_by_the_clone:
        _put(clone, EDITED)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "seed")
    _git(clone, "remote", "add", "local", str(canonical))
    _git(clone, "push", "-q", "local", "main")
    shipped = _put(baked, SHIPPED)
    monkeypatch.setattr(paths, "ROOT", str(baked))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(clone))
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")

    plan = service.build_plan(KEY, selection)
    assert plan["error"] is None, plan
    out = service.apply_plan(plan)
    result = gitio.commit_and_push(out["paths"], ["reconfigure %s" % KEY], run_id="004000000001")
    assert result["committed"] is True and result["staged"] is True and result["pushed_canonical"] is True

    ref = "proposed/004000000001"
    assert _git(canonical, "diff", "--name-only", "main", ref).split() == [REL]
    assert _git(canonical, "show", "%s:%s" % (ref, REL)) == plan["text_after"]
    assert shipped.read_bytes().decode("utf-8") == SHIPPED
    assert _git(clone, "status", "--porcelain") == ""
    mine = clone / "modules" / KEY / "module.yml"
    assert (mine.read_bytes().decode("utf-8") == EDITED) if held_by_the_clone else (not mine.exists())
