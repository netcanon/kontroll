"""deploy-stack.yml's G9 age-key gate, run for real: its own stat and assert tasks, with ansible-playbook, against
each shape the key's path can take on a host.

WHY: tests/unit/test_g9_age_key_refusal.py evaluates the gate's expressions against stat results written by hand.
That holds the logic, not what the real stat says about a path, and I-22 (2026-11-08) was in that gap twice, and a
third hole was in the rule itself. A directory Docker made when onboard-gui started without the key exists and is
other-readable, so the gate passed it, and the remedy it prints, run against that directory, installs the key INTO
it. And a stat that does not follow a link describes the link, whose own mode is 0777, so a link to a key uid 1001
cannot read passed the other-readable leg, while Docker mounts the link's target. And "uid 1001 owns it" stood in
for the mode, passing a key whose owner read bit is off and refusing a root:1001 0640 key the GUI can read. Each row
here makes one shape on disk, with a stand-in of the deploy's config stamp beside it; one play runs the three tasks
(the stat, the forget that drops the GUI's record when the key is refused (I-31), the assert), copied from the
playbook as they are (not rewritten), once per shape, and records what the gate did, what the stat said and what the
stamp holds after.

Every row runs as an ordinary user except the rows whose key has uid or gid 1001, which need root: they run where the
suite is run as root (tests/validate.sh's as-root step runs this file) and are skipped elsewhere. The two whose key
belongs to the suite's own uid, which uid 1001 must not read, are skipped where the suite runs as uid 1001 (GitHub's
hosted runner does: the first CI run of these rows failed there, the key being 1001's own). It needs POSIX and
ansible-playbook (CI's validate job installs it); without them every row is skipped.
"""
import importlib.util
import json
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix" or not shutil.which("ansible-playbook"),
                       reason="needs POSIX and ansible-playbook (it runs deploy-stack.yml's own gate tasks)"),
]

RUNTIME_UID = 1001
_BYTES = b"a stand-in for the key, not a key\n"
# owner/group/mode the suite's own uid cannot give a file: these rows need root
_ROOT_ONLY = {"file-0600-runtime-uid": (RUNTIME_UID, RUNTIME_UID, 0o600),
              "file-0044-runtime-uid": (RUNTIME_UID, RUNTIME_UID, 0o044),
              "file-0640-root-group-1001": (0, RUNTIME_UID, 0o640),
              "file-0604-root-group-1001": (0, RUNTIME_UID, 0o604)}
# a key the suite's own uid makes and owns, which uid 1001 must not read: a suite run as uid 1001 (GitHub's hosted
# runner is) owns it as 1001, so these rows are skipped there
_OTHER_UID = ("file-0600-other-uid", "link-to-0600-other-uid")
# the mode each other shape is made with, its owner and group the suite's (a link's: its target's)
_MADE_MODE = {"docker-directory": 0o755, "directory-a-key-was-installed-into": 0o755, "fifo-0644": 0o644,
              "file-0600-other-uid": 0o600, "link-to-0600-other-uid": 0o600, "file-0644": 0o644,
              "link-to-0644": 0o644, "empty-0644": 0o644}

# shape -> what the gate must do: "PASSED", or a phrase its refusal must carry
_EXPECT = {
    "missing": "is MISSING.",
    "dangling-link": "is MISSING.",
    "docker-directory": "is a DIRECTORY",
    "directory-a-key-was-installed-into": "is a DIRECTORY",
    "fifo-0644": "is not a regular file.",
    "empty-0644": "is EMPTY.",
    "file-0600-other-uid": "is not readable by the runtime uid 1001.",
    "link-to-0600-other-uid": "is not readable by the runtime uid 1001.",
    "file-0644": "PASSED",
    "link-to-0644": "PASSED",
    "file-0600-runtime-uid": "PASSED",
    "file-0640-root-group-1001": "PASSED",
    "file-0044-runtime-uid": "is not readable by the runtime uid 1001.",
    "file-0604-root-group-1001": "is not readable by the runtime uid 1001.",
}


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _gate_tasks():
    """The stat, the forget and the assert, as deploy-stack.yml has them."""
    with open(DEPLOY, encoding="utf-8") as fh:
        tasks = list(_tasks(yaml.safe_load(fh.read())))
    stat = [t for t in tasks if str(t.get("name", "")).startswith("Stat the onboard-gui age key")]
    forget = [t for t in tasks if str(t.get("name", "")).startswith("Forget what onboard-gui last loaded")]
    gate = [t for t in tasks if "the actual remedy" in str(t.get("name", ""))]
    assert len(stat) == 1 and len(forget) == 1 and len(gate) == 1, "the G9 tasks moved — move this test with them"
    return stat[0], forget[0], gate[0]


# the deploy's config stamp, as a stand-in: the GUI's record beside another service's
_STAMP = {"caddy": "c", "onboard-gui": "g"}


def _write(path, mode, data=_BYTES):
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)


def _unit():
    """tests/unit/test_g9_age_key_refusal.py, for its hand-written stat results."""
    spec = importlib.util.spec_from_file_location(
        "_g9_unit_rows", os.path.join(ROOT, "tests", "unit", "test_g9_age_key_refusal.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make(directory, shape):
    """Lay out `<directory>/age.key` in the given shape."""
    os.makedirs(directory)
    key = os.path.join(directory, "age.key")
    target = os.path.join(directory, "target.key")
    if shape in ("file-0644", "file-0600-other-uid"):
        _write(key, 0o644 if shape == "file-0644" else 0o600)
    elif shape in _ROOT_ONLY:
        uid, gid, mode = _ROOT_ONLY[shape]
        _write(key, mode)
        os.chown(key, uid, gid)
    elif shape in ("docker-directory", "directory-a-key-was-installed-into"):
        os.mkdir(key)
        os.chmod(key, 0o755)                     # Docker's: root:root 0755 when the suite is root
        if shape == "directory-a-key-was-installed-into":
            _write(os.path.join(key, "keys.txt"), 0o600)   # where `install … keys.txt <dir>` puts it
    elif shape in ("link-to-0600-other-uid", "link-to-0644"):
        _write(target, 0o600 if shape == "link-to-0600-other-uid" else 0o644)
        os.symlink(target, key)
    elif shape == "dangling-link":
        os.symlink(os.path.join(directory, "gone.key"), key)
    elif shape == "empty-0644":
        _write(key, 0o644, b"")
    elif shape == "fifo-0644":
        os.mkfifo(key)
        os.chmod(key, 0o644)                     # other-readable: only the regular-file leg can refuse it
    else:
        assert shape == "missing", shape


def _run_gate(base, shapes, check=False):
    """One ansible-playbook run (`--check` if `check`): for each shape, the three tasks in a block whose rescue
    records the refusal; returns {shape: {"result": "PASSED" or the refusal's message, "stat": what the stat
    registered, "stamp": the stand-in stamp after}}. The play sits two levels down from a copy of the script, as
    deploy-stack.yml sits from scripts/, so the forget's own command line runs it."""
    for shape in shapes:
        _make(str(base / "shapes" / shape), shape)
        store = base / "stores" / shape
        store.mkdir(parents=True)
        (store / "config-loaded.json").write_text(json.dumps(_STAMP), encoding="utf-8")
        os.chmod(store / "config-loaded.json", 0o600)
    (base / "scripts").mkdir()
    shutil.copy(os.path.join(ROOT, "scripts", "stale-config.py"), str(base / "scripts"))
    stat_task, forget_task, gate_task = _gate_tasks()
    one_shape = [{
        "name": "One shape, through the gate's own tasks",
        "block": [
            stat_task,
            {"name": "Record what the stat said",
             "ansible.builtin.set_fact": {"_stats": "{{ _stats | combine({shape: _gui_age_key.stat}) }}"}},
            forget_task,
            gate_task,
            {"name": "Record a pass",
             "ansible.builtin.set_fact": {"_results": "{{ _results | combine({shape: 'PASSED'}) }}"}},
        ],
        "rescue": [
            {"name": "Record the refusal",
             "ansible.builtin.set_fact": {"_results": "{{ _results | combine({shape: ansible_failed_result.msg}) }}"}},
        ],
    }]
    (base / "one-shape.yml").write_text(yaml.safe_dump(one_shape, sort_keys=False), encoding="utf-8")
    results = base / "results.json"
    play = [{
        "name": "The G9 age-key gate against each shape",
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {"stack_services": ["onboard-gui"], "_results": {}, "_stats": {}},
        "tasks": [
            {"name": "Each shape",
             "ansible.builtin.include_tasks": str(base / "one-shape.yml"),
             "loop": shapes,
             "loop_control": {"loop_var": "shape"},
             "vars": {"kontroll_gui_dir": str(base / "shapes") + "/{{ shape }}",
                      "kontroll_storage_root": str(base / "stores") + "/{{ shape }}"}},
            {"name": "Write down what happened",
             "ansible.builtin.copy": {"content": "{{ {'results': _results, 'stats': _stats} | to_json }}",
                                      "dest": str(results), "mode": "0600"},
             "check_mode": False},
        ],
    }]
    playbooks = base / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    home = base / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("""[defaults]
retry_files_enabled = False
interpreter_python = auto_silent
""", encoding="utf-8")
    (base / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
        "ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1")
    argv = ["ansible-playbook", "-i", str(base / "hosts.yml"), str(playbooks / "play.yml")] + (["--check"] if check
                                                                                              else [])
    p = subprocess.run(argv, cwd=str(base), env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0 and results.exists(), p.stdout + p.stderr
    got = json.loads(results.read_text(encoding="utf-8"))
    assert sorted(got["results"]) == sorted(shapes), (shapes, got["results"])
    return {s: {"result": got["results"][s], "stat": got["stats"].get(s),
                "stamp": json.loads((base / "stores" / s / "config-loaded.json").read_text(encoding="utf-8"))}
            for s in shapes}


@pytest.fixture(scope="module")
def gate_run(tmp_path_factory):
    """Every shape this suite's uid can make, through one run of the gate (see `_run_gate`)."""
    shapes = [s for s in _EXPECT if (s not in _ROOT_ONLY or os.geteuid() == 0) and
              (s not in _OTHER_UID or os.geteuid() != RUNTIME_UID)]
    return _run_gate(tmp_path_factory.mktemp("g9-age-key"), shapes)


@pytest.mark.parametrize("shape", sorted(_EXPECT))
def test_the_real_gate_admits_only_a_regular_file_the_runtime_uid_can_read(gate_run, shape):
    """Each shape through the gate as deploy-stack.yml runs it. A key uid 1001 can read passes (other-readable, its
    own with the owner read bit, group 1001's with the group read bit), through a link too; a directory (Docker's,
    or one the printed remedy then installed a key into), a pipe, an empty file, a link to a key uid 1001 cannot
    read, a link to nothing, a 1001-owned key without its owner read bit and a group-1001 key whose group bits give
    no read are refused, each with the wording that names it. Guards I-22's holes (no regular-file leg, a stat that
    did not follow the link, ownership standing in for the mode, no size leg) against a real stat, not a
    hand-written one."""
    if shape not in gate_run:
        pytest.skip("needs root to give the key an owner or group of %d" % RUNTIME_UID if shape in _ROOT_ONLY else
                    "run as uid %d, which would own the key itself" % RUNTIME_UID)
    result, want = gate_run[shape]["result"], _EXPECT[shape]
    if want == "PASSED":
        assert result == "PASSED", (shape, result)
    else:
        assert result != "PASSED" and want in result, (shape, result)
        assert "onboard-gui's age key at " in result and "install -m 0600 -o 1001 -g 1001" in result, result


def test_the_real_stat_reports_the_shapes_the_unit_rows_assume_and_reads_no_byte(gate_run):
    """The unit rows' hand-written stat results are the real stat's: each shape's real result holds every field the
    unit's own `_FILE`, `_DIR`, `_FIFO` or `_MISSING` (with `_mode` of the shape's owner, group and mode) gives, with
    the same value, so a field the unit writes wrong, or one the stat stops returning, fails here (I-22's reader B,
    N-6: this compared types and four shapes only). A path that is not there gives `exists` alone, a link to nothing
    included (the stat follows it); one that is gives the type flags the gate reads, of the link's TARGET for a link
    (not a link, and its target's mode); and no row's result holds a checksum or a MIME type, because the stat is
    told to compute neither (a digest of the GUI's private key is not what a verbose deploy should print)."""
    stats = {shape: run["stat"] for shape, run in gate_run.items()}
    assert stats["missing"] == {"exists": False} and stats["dangling-link"] == {"exists": False}, stats
    unit = _unit()
    for shape, real in stats.items():
        if shape in ("missing", "dangling-link"):
            continue
        uid, gid, mode = _ROOT_ONLY.get(shape) or (real["uid"], real["gid"], _MADE_MODE[shape])
        kind = (unit._DIR if shape in ("docker-directory", "directory-a-key-was-installed-into")
                else unit._FIFO if shape == "fifo-0644" else unit._FILE)
        hand = dict(kind, **unit._mode(uid, gid, mode))
        if shape == "empty-0644":
            hand["size"] = 0
        assert {k: real.get(k) for k in hand} == hand, (shape, hand, real)
    for shape, stat in stats.items():
        if stat == {"exists": False}:
            continue
        flags = ("isreg", "isdir", "islnk", "rusr", "rgrp", "roth")
        assert all(isinstance(stat.get(f), bool) for f in flags), (shape, stat)
        assert isinstance(stat.get("uid"), int) and isinstance(stat.get("gid"), int), (shape, stat)
        assert "checksum" not in stat and "mimetype" not in stat, (shape, sorted(stat))
    assert stats["docker-directory"]["isdir"] is True and stats["docker-directory"]["isreg"] is False
    assert stats["docker-directory"]["roth"] is True, "a 0755 directory is other-readable: the hole was real"
    for link, mode_roth in (("link-to-0644", True), ("link-to-0600-other-uid", False)):
        if link not in stats:                    # skipped where the suite runs as uid 1001
            continue
        assert stats[link]["islnk"] is False and stats[link]["isreg"] is True, (link, stats[link])
        assert stats[link]["roth"] is mode_roth, (link, stats[link])
    assert stats["fifo-0644"]["isfifo"] is True and stats["fifo-0644"]["roth"] is True, stats["fifo-0644"]


def test_a_refused_key_drops_the_guis_record_and_a_passed_one_keeps_it(gate_run):
    """After each shape, the stand-in stamp: a shape the gate refused has lost onboard-gui's record and kept the other
    service's; one it passed keeps both. So the first deploy past the gate after a refusal finds no record of the GUI,
    which is stale, and restarts a running GUI whatever key came back (I-31's two reads: the record was always of a
    key the gate passed, which the printed install puts back exactly, so the re-run named nothing). Run by the real
    task, on the real stat of each shape, through its own command line."""
    for shape, run in gate_run.items():
        want = dict(_STAMP) if _EXPECT[shape] == "PASSED" else {"caddy": "c"}
        assert run["stamp"] == want, (shape, run["result"], run["stamp"])


def test_a_dry_runs_refusal_drops_the_record_too(tmp_path):
    """The same tasks in `--check`: a refused key's record is dropped and a passed one's kept, as in an apply. The
    mandated flow is a dry-run first; were the forget skipped there, a dry-run that refused, the key then put right,
    a passing dry-run and its apply would never drop the record, and the apply would restart nothing (I-31's two
    reads). Dropping a record can only add a restart, never hide one."""
    got = _run_gate(tmp_path, ["missing", "file-0644"], check=True)
    assert got["missing"]["result"] != "PASSED" and got["missing"]["stamp"] == {"caddy": "c"}, got["missing"]
    assert got["file-0644"]["result"] == "PASSED" and got["file-0644"]["stamp"] == _STAMP, got["file-0644"]


def test_the_rows_that_need_root_run_in_the_gate_as_root():
    """The rows of uid or gid 1001 need root and are skipped elsewhere, and the public CI runs the suite as an
    ordinary user, so they ran nowhere: the group leg and the owner read bit against a real stat (I-22's two
    readers, 2026-11-08). tests/validate.sh's as-root step runs the files that need root and fails `--strict` on
    any skip; it must name this one."""
    with open(os.path.join(ROOT, "tests", "validate.sh"), encoding="utf-8") as fh:
        text = fh.read()
    step = text.split("step canon-drop-as-root", 1)[1].split("\n  fi\n", 1)[0]
    assert "tests/integration/test_g9_age_key_gate_real_stat.py" in step, step
