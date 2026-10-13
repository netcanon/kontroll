"""A line of `instance/fleet.yml` that is not a device-class key — what every reader of the list does with it.

WHY (the failures these guard): the fleet's `enabled_modules` is a hand-editable list, and an unquoted `no`, a typo
with a slash or a mapping left over from a merge is an ordinary mistake. Each deploy-time reader used to format
the entry straight into a path: a `..` entry was ACCEPTED as a class (its generated target planned under a path
with `..` in it), a non-string was a traceback in three generators, and the generator that decides what is
INSTALLED silently installed less than the fleet declared. The rule these tests hold:

  * ONE classifier (`paths.fleet_problems`, the complement of `paths.enabled_keys`): the entry's place counted from
    one, the rule, never the entry;
  * a generator that RENDERS skips such a line and says so — some of them run inside the API and the GUI, where
    stopping would kill a worker after a module file was written — and renders and prunes NOTHING for a file that
    holds no list at all;
  * the generator that decides what is installed REFUSES the fleet — at bootstrap, at a local-image deploy and at
    the promote gate, whose refusal is what keeps a proposal from bringing the line to `main`;
  * the plays run that check, in Python, and STOP on it: the bootstrap before anything joins an entry into a path,
    a deploy before its canonical play commits the worktree and before its stack play calls a generator.
"""
import datetime
import importlib.util
import os
import re
import shutil
import subprocess as sp
import sys

import pytest
import yaml

from kontroll import inventory, paths
from kontroll.service import actuation, backup, logsvc, observe

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARK = "SECRET-MARKER"
# The ways a line fails to be a class key that this table holds: wrong types (an unquoted `no` / `~` / a number is
# how most arrive), a path, a pattern that climbs out and back in to a real class, a newline, a case the charset
# does not hold, the empty string, one character too long — and one that carries a marker no message may repeat.
HOSTILE = [None, True, False, 123, 1.5, {"a": 1}, ["x"], "../x", "/etc", "a/b", "bad\n", "Upper", "", "k" * 65,
           "cisco_ios/../cisco_ios", "cisco_ios/../../modules/cisco_ios", MARK + "/../x",
           datetime.date(2026, 1, 1), b"cisco_ios", "cisco_ios ", " cisco_ios", "cisco_ios٣", "_core"]
IDS = ["null", "true", "false", "int", "float", "mapping", "list", "dotdot", "absolute", "slash", "newline",
       "uppercase", "empty", "too-long", "climbs-back-to-a-class", "climbs-back-by-two", "marker",
       "date", "bytes", "trailing-space", "leading-space", "non-ascii-digit", "leading-underscore"]
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test", "PYTHONIOENCODING": "utf-8"}


def _script(name):
    """Load scripts/<name>.py by path (a hyphenated file is not importable by name)."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_fleet_line",
                                                  os.path.join(ROOT, "scripts", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _example():
    with open(os.path.join(ROOT, "instance.example", "fleet.yml"), encoding="utf-8") as fh:
        fleet = yaml.safe_load(fh)
    return fleet, inventory.merged_inventory("instance.example/inventory/hosts.yml")


# (generator, a callable (module, fleet, inventory) -> something comparable) — each generator's fleet-walking function.
WALKERS = [
    ("gen-observability", lambda g, f, inv: g.metrics_files(f, inv)),
    ("gen-logging", lambda g, f, inv: (g.logging_files(f, inv), g.loki_retention_files(f))),
    ("gen-homepage", lambda g, f, inv: g.fleet_rows(f, inv)),
    ("gen-backup", lambda g, f, inv: g.backup_schedules(f)),
    ("gen-prometheus-jobs", lambda g, f, inv: g.jobs_files(f)),
    ("gen-exporters", lambda g, f, inv: g.exporter_files(f)),
    ("gen-secret-env", lambda g, f, inv: g.secret_env_files(f)),
    ("gen-class-capabilities", lambda g, f, inv: g.drift(f)),
    ("gen-validate-live", lambda g, f, inv: [(entry, ctx, vars(target)) for entry, ctx, target in g.collect(f, inv)]),
]


# --- the classifier ---------------------------------------------------------------------------------------------- #
def test_fleet_problems_names_the_position_and_never_the_entry():
    """`fleet_problems` answers one message per entry that is not a class key, each naming the entry's position
    counted from ONE and the rule, and none repeating a character sequence of the entry; a clean list answers [].

    Guards a payload — or a credential pasted into the wrong file — travelling on in a deploy log or a promote
    refusal, and a position counted from zero, which is not the line an operator counts in the file."""
    fleet = {"enabled_modules": ["cisco_ios", MARK + "/../x", "docker_host", None]}
    problems = paths.fleet_problems(fleet)
    assert len(problems) == 2 and "entry 2 " in problems[0] and "entry 4 " in problems[1], problems
    assert all(MARK not in p and "../" not in p and "device-class key" in p for p in problems), problems
    assert paths.fleet_problems({"enabled_modules": ["cisco_ios", "edge-fw", "a_b", "0x"]}) == []


@pytest.mark.parametrize("fleet,expected", [
    ({}, []), ({"enabled_modules": None}, []), ({"enabled_modules": []}, []),
    ({"enabled_modules": "cisco_ios"}, ["enabled_modules is not a list"]),
    ({"enabled_modules": {"cisco_ios": 1}}, ["enabled_modules is not a list"]),
    ({"enabled_modules": 5}, ["enabled_modules is not a list"]),
    (["cisco_ios"], ["the file is not a mapping"]), ("cisco_ios", ["the file is not a mapping"]),
    (None, ["the file is not a mapping"]),
], ids=["empty-mapping", "no-list", "empty-list", "a-string", "a-mapping", "a-number", "file-a-list", "file-a-word",
        "file-null"])
def test_fleet_problems_names_a_list_or_a_file_of_the_wrong_shape(fleet, expected):
    """A fleet with no list (or an empty one) has no problem; a list VALUE that is a string, a mapping or a number,
    and a FILE that is not a mapping, each have one — and `enabled_keys` reads every one of them as no class.

    Guards the silent empty fleet: a string was iterated character by character and every character skipped, and
    a mapping worked as if it were a list, so a deploy rendered nothing (or everything) without a word."""
    assert paths.fleet_problems(fleet) == expected
    if expected:
        assert paths.enabled_keys(fleet) == []


def test_enabled_keys_and_fleet_problems_split_the_list_between_them():
    """Over every hostile entry, twice in a list with two real keys: each entry of the list is either a key
    `enabled_keys` returns or a position `fleet_problems` names — never both, never neither.

    Guards the two helpers drifting apart: a line one of them drops and the other does not report is skipped by
    every renderer and refused by nobody."""
    for entry in HOSTILE:
        mods = ["cisco_ios", entry, "docker_host", entry]
        fleet = {"enabled_modules": mods}
        keys, problems = paths.enabled_keys(fleet), paths.fleet_problems(fleet)
        assert keys == ["cisco_ios", "docker_host"], entry
        assert len(keys) + len(problems) == len(mods), entry
        assert ["entry 2 " in problems[0], "entry 4 " in problems[1]] == [True, True], problems


# --- the renderers and the checks: skip, and say so ---------------------------------------------------------------- #
@pytest.mark.parametrize("entry", HOSTILE, ids=IDS)
@pytest.mark.parametrize("name,walk", WALKERS, ids=[w[0] for w in WALKERS])
def test_no_generator_joins_a_fleet_line_that_is_not_a_class_key(name, walk, entry):
    """With a line that is not a class key added to the shipped example fleet — first, in the middle and last —
    each generator's fleet-walking function returns exactly what it returns for the clean fleet, and raises nothing.

    Guards a fleet line read as a class: an entry that climbs out with `..` and back in to a real class used to be
    ACCEPTED as that class by the walkers whose answer is keyed by the entry (its target planned under a path with
    `..` in it — a file written outside its directory), and a non-string was a traceback in the walkers that join
    with `os.path.join`. (That no walker even ASKS the filesystem about such a line — the ones whose answer cannot
    show it included — is `test_no_generator_asks_the_filesystem_about_a_fleet_line_that_is_not_a_class_key`.)"""
    gen = _script(name)
    fleet, inv = _example()
    clean = walk(gen, fleet, inv)
    mods = list(fleet["enabled_modules"])
    for at in (0, len(mods) // 2, len(mods)):
        hostile = dict(fleet, enabled_modules=mods[:at] + [entry] + mods[at:])
        assert walk(gen, hostile, inv) == clean, (name, at)


@pytest.mark.parametrize("mod,fn,args", [(observe, "regenerate_observability", ("cisco_ios",)),
                                         (logsvc, "regenerate_logging", ("cisco_ios",)),
                                         (backup, "regenerate_schedules", ())],
                         ids=["observability", "logging", "backup"])
def test_a_bad_fleet_line_does_not_kill_the_capability_regen(mod, fn, args, tmp_path, monkeypatch, capfd):
    """With lines that are not class keys in the fleet the write root holds, each in-process regeneration — the
    REAL generator, run the way the API and the GUI run it after a capability promote — returns normally, and says
    on stderr which positions it skipped.

    Guards a renderer being made to stop on such a line "to match the collection generator": its `sys.exit` is a
    SystemExit inside the API or GUI worker, which no route catches, after the module file was already written."""
    (tmp_path / "instance").mkdir()
    (tmp_path / "instance" / "fleet.yml").write_text(
        "enabled_modules:\n  - cisco_ios\n  - null\n  - {a: 1}\n  - %s/../x\n  - docker_host\n" % MARK, encoding="utf-8")
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))
    getattr(mod, fn)(*args)                                       # must not raise — SystemExit included
    err = capfd.readouterr().err
    assert all("ignoring: instance/fleet.yml: enabled_modules entry %d " % n in err for n in (2, 3, 4)), err
    assert MARK not in err


def test_the_membership_readers_take_class_keys_only(tmp_path, monkeypatch):
    """The two readers that only ask "is this class enabled" — the unit validator's (`gen-actuation.py`) and the
    create route's (`service/actuation.py`) — return the class keys of the fleet and nothing else.

    Guards a third rule for one list: neither reader joins or hashes an entry today — `validate_unit` tests
    membership — so a raw list here crashes nothing; it would let "is this class enabled" mean something else in
    the places a unit is validated than everywhere a class is resolved."""
    fleet = tmp_path / "fleet.yml"
    fleet.write_text("enabled_modules:\n  - docker_host\n  - {a: 1}\n  - ../x\n  - null\n", encoding="utf-8")
    monkeypatch.setattr(paths, "resolve", lambda rel: str(fleet))
    assert actuation._enabled_modules() == ["docker_host"]
    assert _script("gen-actuation")._enabled_modules() == ["docker_host"]


# --- the reader that decides what is installed: refuse ------------------------------------------------------------- #
def _tree(root, fleet_text):
    """A minimal tree the collection generator can judge as DATA (`--root`): the core set, one real class, a fleet."""
    (root / "modules" / "docker_host").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "modules", "_core.yml"), root / "modules" / "_core.yml")
    shutil.copy(os.path.join(ROOT, "modules", "docker_host", "module.yml"), root / "modules" / "docker_host" / "module.yml")
    for rel in ("config", "instance"):
        (root / rel).mkdir(exist_ok=True)
        (root / rel / "fleet.yml").write_text(fleet_text, encoding="utf-8")
    return root


def _gen_requirements(root, *flags):
    return sp.run([sys.executable, "-B", os.path.join(ROOT, "scripts", "gen-requirements.py"), *flags, "--root", str(root)],
                  cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8")


@pytest.mark.parametrize("flags", [("--fleet-check",), ("--conflict-check",), ()], ids=["fleet-check", "conflict-check", "generate"])
@pytest.mark.parametrize("fleet_text,says", [
    ("enabled_modules:\n  - docker_host\n  - null\n", "entry 2 "),
    ("enabled_modules:\n  - docker_host\n  - no\n", "entry 2 "),
    ("enabled_modules:\n  - %s/../x\n  - docker_host\n" % MARK, "entry 1 "),
    ("enabled_modules:\n  - docker_host\n  - Docker_Host\n", "entry 2 "),
    ("enabled_modules: docker_host\n", "not a list"),
    ("enabled_modules: {docker_host: 1}\n", "not a list"),
    ("- docker_host\n", "not a mapping"),
], ids=["null", "unquoted-no", "a-path", "uppercase", "a-string", "a-mapping", "file-a-list"])
def test_gen_requirements_refuses_a_fleet_that_is_not_a_list_of_class_keys(tmp_path, flags, fleet_text, says):
    """A real run of the collection generator over a throwaway tree whose fleet holds a line that is not a class
    key — in each of its seats: the plays' `--fleet-check`, the promote gate's `--conflict-check`, a generate —
    exits non-zero with ONE line that names the file, the position and what ends the condition, and never the entry.

    Guards the install-gating generator installing less than the fleet declares, silently (a skipped class takes
    its collections out of the lockfile); a traceback as the promote gate's reason; and the gate letting such a
    line reach `main`, where every reader that renders would then skip the class."""
    p = _gen_requirements(_tree(tmp_path, fleet_text), *flags)
    assert p.returncode != 0, p.stdout
    assert "instance/fleet.yml" in p.stderr and says in p.stderr and "Traceback" not in p.stderr, p.stderr
    assert "a proposal that does promotes" in p.stderr and MARK not in p.stderr, p.stderr
    assert len(p.stderr.strip().splitlines()) == 1, p.stderr
    assert not (tmp_path / "ansible").exists()                    # refused before anything was written


@pytest.mark.parametrize("flags", [("--fleet-check",), ("--conflict-check",)], ids=["fleet-check", "conflict-check"])
def test_gen_requirements_passes_a_clean_fleet_and_a_class_with_no_directory(tmp_path, flags):
    """The same run over a fleet of class keys exits 0 — also when one of them has no module directory in the tree.

    Guards the refusal being wider than the rule: a well-formed key whose directory is elsewhere (a baked image
    holds the classes, the generator runs over the clone) is not a line that is not a key, and must not stop a
    promote or a deploy."""
    p = _gen_requirements(_tree(tmp_path, "enabled_modules:\n  - docker_host\n  - a-class-with-no-directory\n"), *flags)
    assert p.returncode == 0, p.stderr


def test_all_modules_ignores_a_directory_that_is_not_a_class_key(tmp_path, monkeypatch):
    """`--all-modules` (the published image's superset) takes the directories under `modules/` that are class keys
    and no other.

    Guards a stray directory — a dotted name, an upper-case one, an editor's backup — joining the set the
    published runner image is built from."""
    genreq = _script("gen-requirements")
    for d in ("docker_host", "cisco_ios", "Stray.Dir", "UPPER", ".git"):
        (tmp_path / "modules" / d).mkdir(parents=True)
    (tmp_path / "modules" / "_core.yml").write_text("collections: []\n", encoding="utf-8")
    monkeypatch.setattr(genreq, "ROOT", str(tmp_path))
    assert genreq.all_module_keys() == ["cisco_ios", "docker_host"]


# --- the promote gate ---------------------------------------------------------------------------------------------- #
def _git(repo, *args):
    sp.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True)


def _fleet(repo, text):
    for rel in ("config", "instance"):
        (repo / rel / "fleet.yml").write_text(text, encoding="utf-8")


def test_the_promote_gate_refuses_a_bad_fleet_line_and_lets_its_removal_through(tmp_path, capsys):
    """Real git and the real gate. A proposal that ADDS a line that is not a class key is refused (exit 2, `main`
    not moved) with the generator's own sentence — the position, never the entry — under a lead that does not
    talk about pins. With such a line already ON `main`, an unrelated proposal is refused too, and the proposal
    that removes the line is not.

    Guards the gate stranding a box: it judges the whole prospective tree, so one bad line on `main` refuses every
    promote — the way out has to be promotable, and the message has to say what to correct (it used to tell the
    operator to re-pin a unit)."""
    promote = _script("kontroll-promote")
    repo = _tree(tmp_path / "repo", "enabled_modules:\n  - docker_host\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    # a proposal that adds the line
    _git(repo, "checkout", "-q", "-b", "proposed/0000000000a1")
    _fleet(repo, "enabled_modules:\n  - docker_host\n  - %s/../x\n" % MARK)
    _git(repo, "commit", "-qam", "adds a line that is not a class key")
    _git(repo, "checkout", "-q", "main")
    head = sp.run(["git", "rev-parse", "main"], cwd=repo, capture_output=True, text=True).stdout
    assert promote.main(["0000000000a1", "--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "does not resolve" in err and "entry 2 " in err and "instance/fleet.yml" in err, err
    assert "on `main` already" not in err, err                 # `main` resolves: the line is the proposal's own
    assert MARK not in err and "Re-pin" not in err and "Traceback" not in err, err
    assert sp.run(["git", "rev-parse", "main"], cwd=repo, capture_output=True, text=True).stdout == head
    # the line is on main already (a hand edit); an unrelated proposal, and the one that removes it
    _fleet(repo, "enabled_modules:\n  - docker_host\n  - null\n")
    _git(repo, "commit", "-qam", "a hand edit put the line on main")
    _git(repo, "checkout", "-q", "-b", "proposed/0000000000b2")
    (repo / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "0000000000b2")
    _git(repo, "checkout", "-q", "-b", "proposed/0000000000c3", "main")
    _fleet(repo, "enabled_modules:\n  - docker_host\n")
    _git(repo, "commit", "-qam", "removes the line")
    _git(repo, "checkout", "-q", "main")
    def gate(run_id):
        gitdir = promote.canon.git_dir(str(repo))
        proposal = promote.canon.read_ref(gitdir, promote.canon.proposal_ref(run_id))
        return promote._tree_refusal(proposal[0], gitdir, promote.canon.read_ref(gitdir, promote.canon.MAIN)[0])

    assert "entry 2 " in (gate("0000000000b2") or "")
    assert gate("0000000000c3") is None


# --- the plays ----------------------------------------------------------------------------------------------------- #
def _tasks(play):
    with open(os.path.join(ROOT, "ansible", "playbooks", play), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return [t for p in doc for t in (p.get("tasks") or [])]


def _is_fleet_check(task):
    cmd = (task.get("ansible.builtin.command") or {})
    return isinstance(cmd, dict) and "gen-requirements.py --fleet-check" in str(cmd.get("cmd"))


def test_bootstrap_checks_the_fleet_before_it_joins_an_entry_into_a_path():
    """`bootstrap.yml` runs `gen-requirements.py --fleet-check` — failing the play on a non-zero exit, also under
    `--check` — before the first task that reads `fleet.enabled_modules`, and holds no copy of the key's charset.

    Guards the play's own `lookup('file', modules_dir ~ '/' ~ item ~ …)` being reached with an entry that is not
    a key (it failed with a lookup error that printed the joined path), and a second, hand-kept copy of the rule
    in Jinja drifting from `kontroll.paths`."""
    tasks = _tasks("bootstrap.yml")
    check = [n for n, t in enumerate(tasks) if _is_fleet_check(t)]
    readers = [n for n, t in enumerate(tasks) if not _is_fleet_check(t) and "enabled_modules" in yaml.safe_dump(t)]
    assert len(check) == 1 and readers and check[0] < min(readers), (check, readers)
    task = tasks[check[0]]
    assert task.get("check_mode") is False and "failed_when" not in task and "ignore_errors" not in task, task
    assert "when" not in task, task                             # unconditional: the installer container runs it too
    assert task["ansible.builtin.command"]["cmd"] == "python3 -B scripts/gen-requirements.py --fleet-check", task
    walks = next(t for t in tasks if "modules_dir ~" in str(t.get("vars")))    # the play's own loop over the list
    assert "default([], true)" in str(walks.get("loop")), walks   # `enabled_modules:` with nothing under it is null
    with open(os.path.join(ROOT, "ansible", "playbooks", "bootstrap.yml"), encoding="utf-8") as fh:
        assert "a-z0-9_" not in fh.read()                        # the key's charset lives in kontroll.paths only


def test_deploy_checks_the_fleet_before_any_generator_and_stops_on_it():
    """`deploy-stack.yml` runs the same check — that command and no other — before ANY generator command (the lock
    reader `gen-image-digests.py --env`, which reads no fleet, apart), unconditionally, also under `--check`, and
    nothing softens its failure: no `failed_when`, no `ignore_errors`, no warning task after it.

    Guards a deploy that goes on past a line that is not a class key: every generator that renders then drops
    that class from files the running stack reads — and on a node that builds its images the play stops anyway
    further down, at the collection lockfile, with those files already rewritten. (Stop is the built default;
    making it a warning is the operator's decision, and this test is what has to change with it.)"""
    tasks = _tasks("deploy-stack.yml")
    check = [n for n, t in enumerate(tasks) if _is_fleet_check(t)]
    gens = [n for n, t in enumerate(tasks)
            if isinstance(t.get("ansible.builtin.command"), dict)
            and any("scripts/gen-%s.py" % g in str(t["ansible.builtin.command"].get("cmd"))
                    for g in ("observability", "prometheus-jobs", "exporters", "logging", "homepage", "requirements"))
            and not _is_fleet_check(t)]
    assert len(check) == 1 and gens and check[0] < min(gens), (check, gens)
    task, after = tasks[check[0]], tasks[check[0] + 1]
    assert task.get("check_mode") is False and "when" not in task, task
    assert not {"failed_when", "ignore_errors", "register"} & set(task), task
    assert "ansible.builtin.debug" not in after, after
    assert task["ansible.builtin.command"]["cmd"] == "python3 -B scripts/gen-requirements.py --fleet-check", task
    above = yaml.safe_dump(tasks[:check[0]], width=10 ** 6)     # ANY generator, not only the ones named above
    assert set(re.findall(r"scripts/(gen-[a-z-]+\.py)", above)) <= {"gen-image-digests.py"}, above


def test_the_canonical_checks_the_fleet_before_it_commits_the_worktree():
    """`local-canonical.yml` — which every deploy imports — runs the check, unconditionally and also under
    `--check`, before the task that stages the whole worktree (`git add -A`), and the task after the check fails
    the play on ANY non-zero exit — nothing softens it — saying that nothing was staged, committed or pushed.

    Guards a way into the canonical's `main` that passes no promote gate: that commit and the mirror take the
    operator's worktree as it is — on a node whose overlay git tracks, a hand-edited line would be on `main` when
    the deploy ends, and from then on the gate refuses every proposal, whatever it changes."""
    tasks = _tasks("local-canonical.yml")
    check = [n for n, t in enumerate(tasks) if _is_fleet_check(t)]
    commit = [n for n, t in enumerate(tasks) if "git add -A" in str(t.get("ansible.builtin.shell"))]
    assert len(check) == 1 and len(commit) == 1 and check[0] < commit[0], (check, commit)
    task, refuse = tasks[check[0]], tasks[check[0] + 1]
    assert task.get("check_mode") is False and task.get("register") == "_fleet_check_canonical", task
    assert "ansible.builtin.fail" in refuse and refuse.get("when") == "_fleet_check_canonical.rc != 0", refuse
    assert "staged, committed and pushed nothing" in refuse["ansible.builtin.fail"]["msg"]
    assert not {"ignore_errors", "failed_when"} & set(refuse), refuse       # nothing softens the refusal
    assert not {"when", "ignore_errors"} & set(task), task                  # and the check itself is unconditional
    assert task["ansible.builtin.command"]["cmd"] == "python3 -B scripts/gen-requirements.py --fleet-check", task


# --- rows added by the as-built read ------------------------------------------------------------------------------ #
@pytest.mark.parametrize("name,walk", WALKERS, ids=[w[0] for w in WALKERS])
def test_no_generator_asks_the_filesystem_about_a_fleet_line_that_is_not_a_class_key(name, walk, monkeypatch):
    """With every hostile line added to the shipped example fleet at once, each generator's fleet-walking function
    asks `os.path.exists` about exactly the paths it asks about for the clean fleet: no path is built from a line
    that is not a class key.

    Guards the walkers whose RETURN VALUE cannot show a raw walk. The scrape jobs, the exporter fragments, the
    secret-env manifest and the Loki retention file are keyed by method or by class, so a line that climbs out with
    `..` and back in to a class the fleet already enables returns what the clean fleet returns — while the entry
    is still formatted into a path and the filesystem is asked about it (a module file read from outside
    `modules/`)."""
    gen = _script(name)
    fleet, inv = _example()
    real = os.path.exists

    def asked(candidate):
        seen = []
        monkeypatch.setattr(os.path, "exists", lambda p: seen.append(os.path.normpath(str(p))) or real(p))
        try:
            walk(gen, candidate, inv)
        finally:
            monkeypatch.setattr(os.path, "exists", real)
        return sorted(set(seen))

    clean = asked(fleet)
    assert any("modules" in p for p in clean), clean                    # the spy sees the module files at all
    assert asked(dict(fleet, enabled_modules=list(fleet["enabled_modules"]) + HOSTILE)) == clean


@pytest.mark.parametrize("name", [w[0] for w in WALKERS])
def test_every_generator_says_which_fleet_positions_it_skipped(name, monkeypatch, capsys, tmp_path):
    """Each generator's entry point, reading the shipped example fleet with three lines that are not class keys
    added, prints one `<generator>: ignoring: instance/fleet.yml: enabled_modules entry N …` line per such line on
    stderr — never the entry — and its `--check` still finds the committed example outputs up to date.

    Guards a class disappearing without a word from a generator that is NOT one of the three the services run in
    process — the tiles, the scrape jobs, the exporter fragments, the secret-env manifest and the two checks: their
    skip goes silent the day the two lines in the entry point are dropped, and nothing else fails."""
    gen = _script(name)
    fleet, _inv = _example()
    mods = list(fleet["enabled_modules"])
    hostile = dict(fleet, enabled_modules=mods[:1] + [None, {"a": 1}, MARK + "/../x"] + mods[1:])
    real = gen._load
    monkeypatch.setattr(gen, "_load",
                        lambda rel: hostile if str(rel).replace("\\", "/").endswith("fleet.yml") else real(rel))
    if name == "gen-validate-live":                                   # no --check mode: collect, run no check
        _verdicts, rc = gen.run(only_check="no-such-check", fetchers=object(), audit_path=str(tmp_path / "audit.log"))
    else:
        rc = gen.main(["--check"])                                    # reads the example, writes nothing
    said = capsys.readouterr()
    assert rc == 0, said.out
    for n in (2, 3, 4):
        assert "%s: ignoring: instance/fleet.yml: enabled_modules entry %d " % (name, n) in said.err, said.err
    assert MARK not in said.err + said.out


def test_the_fleet_check_writes_nothing_and_does_not_say_it_refuses(tmp_path):
    """`--fleet-check` over a clean fleet exits 0, says so on stdout and writes NOTHING into the tree; over a fleet
    with a line that is not a class key it exits non-zero with the finding and WITHOUT the words of a refusal
    ("refusing", "Fail closed") — which a generate over the same fleet does say.

    Guards the plays' check becoming a generate: every play runs it with `check_mode: false`, so a mode that fell
    through to the default would write the lockfile during the `--check --diff` dry-run; and the check resolves
    nothing, so "refusing to resolve the collection set … Fail closed: a class that is skipped here would install
    less" would describe a resolve this mode does not do."""
    clean = _tree(tmp_path / "clean", "enabled_modules:\n  - docker_host\n")
    before = sorted(str(q.relative_to(clean)) for q in clean.rglob("*"))
    p = _gen_requirements(clean, "--fleet-check")
    assert p.returncode == 0 and "--fleet-check" in p.stdout, (p.stdout, p.stderr)
    assert sorted(str(q.relative_to(clean)) for q in clean.rglob("*")) == before          # nothing written
    bad = "enabled_modules:\n  - docker_host\n  - no\n"
    found = _gen_requirements(_tree(tmp_path / "bad", bad), "--fleet-check")
    assert found.returncode != 0 and "entry 2 " in found.stderr, found.stderr
    assert "refusing" not in found.stderr and "Fail closed" not in found.stderr, found.stderr
    refused = _gen_requirements(_tree(tmp_path / "bad2", bad))
    assert "refusing to resolve the collection set" in refused.stderr and "Fail closed" in refused.stderr, refused.stderr


def test_the_override_flag_says_everything_it_skips(capsys):
    """`kontroll-promote.py --help` describes `--skip-conflict-check` as skipping the whole resolve gate — a fleet
    line that is not a class key included — and not a pin conflict alone.

    Guards an operator reaching for the override believing it waives only a pin conflict: with it a line that is
    not a class key lands on `main`, where every renderer skips the class and the next bootstrap stops."""
    promote = _script("kontroll-promote")
    with pytest.raises(SystemExit):
        promote.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "fleet line that is not a class key" in text and "pin-conflict gate (emergency" not in text, text


def test_the_removal_promotes_end_to_end_on_a_bare_canonical(tmp_path, capsys):
    """Real git and the real promote, on a BARE canonical whose tree keeps the fleet where a node keeps it
    (`instance/fleet.yml` and no legacy `config/fleet.yml`). With a line that is not a class key already on `main`:
    an unrelated proposal is refused (exit 2, `main` not moved, the proposal left staged), and the proposal that
    removes the line PROMOTES (exit 0, `main` is that commit, the proposal ref is gone).

    Guards the way out being promotable only on paper: the gate answering None is not yet a promote — the
    fast-forward check, the compare-and-swap and the ref clean-up run after it, and they run on a bare repository,
    which the working-tree repository of the test above is not."""
    promote = _script("kontroll-promote")
    work, bare = tmp_path / "work", tmp_path / "canonical.git"
    (work / "modules" / "docker_host").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "modules", "_core.yml"), work / "modules" / "_core.yml")
    shutil.copy(os.path.join(ROOT, "modules", "docker_host", "module.yml"), work / "modules" / "docker_host" / "module.yml")
    (work / "instance").mkdir()
    fleet = work / "instance" / "fleet.yml"
    fleet.write_text("enabled_modules:\n  - docker_host\n  - null\n", encoding="utf-8")       # the hand edit, on main
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main, with a line that is not a class key")
    _git(work, "checkout", "-q", "-b", "0000000000b2")
    (work / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "0000000000b2")
    _git(work, "checkout", "-q", "-b", "0000000000c3", "main")
    fleet.write_text("enabled_modules:\n  - docker_host\n", encoding="utf-8")
    _git(work, "commit", "-qam", "removes the line")
    _git(work, "push", "-q", str(bare), "main", "0000000000b2:refs/heads/proposed/0000000000b2",
         "0000000000c3:refs/heads/proposed/0000000000c3")

    def ref(name):
        return sp.run(["git", "rev-parse", "--verify", "--quiet", name], cwd=bare, env=ENV, capture_output=True,
                      text=True).stdout.strip()

    main_was, removal = ref("refs/heads/main"), ref("refs/heads/proposed/0000000000c3")
    assert promote.main(["0000000000b2", "--repo", str(bare)]) == 2
    said = capsys.readouterr().err
    assert "entry 2 " in said and "on `main` already" in said, said        # also on a bare repository
    assert ref("refs/heads/main") == main_was and ref("refs/heads/proposed/0000000000b2")
    assert promote.main(["0000000000c3", "--repo", str(bare)]) == 0
    assert ref("refs/heads/main") == removal != main_was and not ref("refs/heads/proposed/0000000000c3")


RENDERERS = ["gen-observability", "gen-logging", "gen-homepage", "gen-backup", "gen-prometheus-jobs", "gen-exporters",
             "gen-secret-env"]


@pytest.mark.parametrize("fleet", [["proxmox", "docker_host"], "proxmox", {"enabled_modules": "proxmox"},
                                   {"enabled_modules": {"proxmox": None}}, {"enabled_modules": 5}],
                         ids=["file-a-list", "file-a-word", "value-a-string", "value-a-mapping", "value-a-number"])
@pytest.mark.parametrize("name", RENDERERS)
def test_a_fleet_whose_list_cannot_be_read_is_not_rendered_as_an_empty_fleet(name, fleet, monkeypatch, capsys):
    """A renderer whose fleet file holds no LIST of classes — the file is not a mapping, or `enabled_modules` is a
    string, a mapping or a number — returns non-zero from its entry point WITHOUT raising, says that it renders
    nothing, and never compares an empty set of outputs (its `--check` reports no file as stale or orphaned; the
    bare run, which is the one that writes and prunes, is the test below).

    Guards a damaged fleet file being taken for an empty fleet: the bare run then writes nothing and PRUNES every
    target file, drop-in, tile and schedule the node ever generated — where the same file used to stop the deploy
    at the first generator with nothing touched. (No `sys.exit`: three of these run inside the API and the GUI.)"""
    gen = _script(name)
    real = gen._load
    monkeypatch.setattr(gen, "_load",
                        lambda rel: fleet if str(rel).replace("\\", "/").endswith("fleet.yml") else real(rel))
    rc = gen.main(["--check"])                                        # must not raise — SystemExit included
    said = capsys.readouterr()
    assert rc == 1 and "%s: not rendering: instance/fleet.yml" % name in said.err, (rc, said.err)
    assert "STALE" not in said.out and "orphaned" not in said.out, said.out


def test_a_line_yaml_read_as_another_type_is_told_so():
    """The message for an entry that is not text — an unquoted `no`, `~`, a number, a date, a mapping — says that
    YAML read the line as another type; the message for a string outside the charset states the charset. Both
    name the position and neither the entry.

    Guards the commonest mistake being answered with a rule the line visibly satisfies: `- no` IS one to sixty-four
    characters of the key's alphabet as it stands in the file, and "is not a device-class key (1-64 chars of
    [a-z0-9_-] …)" sends the operator looking for a character that is not there."""
    not_text, bad_text = paths.fleet_problems({"enabled_modules": [False, "Upper/" + MARK]})
    assert "entry 1 " in not_text and "YAML does not read it as text" in not_text and "[a-z0-9_-]" not in not_text
    assert "entry 2 " in bad_text and "[a-z0-9_-]" in bad_text and "YAML" not in bad_text
    assert MARK not in not_text + bad_text and "False" not in not_text


@pytest.mark.parametrize("flags", [("--fleet-check",), ("--conflict-check",), ()], ids=["fleet-check", "conflict-check", "generate"])
@pytest.mark.parametrize("fleet_text", ["enabled_modules:\n  - docker_host\n  - !%s x\n" % MARK,
                                        "enabled_modules:\n  - docker_host\n  - *%s\n" % MARK,
                                        "enabled_modules:\n\t- docker_host\n"],
                         ids=["a-tag", "an-alias-nothing-defines", "a-tab"])
def test_a_fleet_file_that_is_not_yaml_is_one_sentence_without_the_line(tmp_path, flags, fleet_text):
    """A fleet file the parser cannot read is answered, in each seat of the collection generator, with ONE line
    that says where it stops parsing — and no traceback, and no piece of the file.

    Guards the parser's own message travelling on: its last line quotes the tag or the alias name it stumbled over
    — text of the operator's file, in a play's output and in a promote's refusal."""
    p = _gen_requirements(_tree(tmp_path, fleet_text), *flags)
    assert p.returncode != 0 and "Traceback" not in p.stderr and MARK not in p.stderr, p.stderr
    assert "is not YAML this tool can read (line " in p.stderr and len(p.stderr.strip().splitlines()) == 1, p.stderr


@pytest.mark.parametrize("other", ["--check", "--conflict-check", "--core", "--all-modules"])
def test_the_fleet_check_refuses_to_ride_with_another_mode(tmp_path, other):
    """`--fleet-check` together with a mode that resolves — or with one that reads no fleet at all — exits non-zero
    and says so, for a clean fleet as well.

    Guards a pass that was never a check: beside `--core` or `--all-modules` no fleet is read, and beside
    `--conflict-check` no pin is resolved, yet the exit status would say the thing a caller named was fine."""
    p = _gen_requirements(_tree(tmp_path, "enabled_modules:\n  - docker_host\n"), "--fleet-check", other)
    assert p.returncode != 0 and "cannot be combined with %s" % other in p.stderr, (p.returncode, p.stderr)


def test_a_refusal_for_a_line_already_on_main_says_where_the_line_is(tmp_path, capsys):
    """When `main` as it stands is refused as well, the gate's refusal of a proposal adds that the fault is on `main`
    already, and what corrects it there.

    Guards an operator promoting an unrelated change and being told "entry 2 is not a device-class key": the
    proposal in front of them did not bring that entry, and nothing else on the screen says which tree did."""
    promote = _script("kontroll-promote")
    repo = _tree(tmp_path / "repo", "enabled_modules:\n  - docker_host\n  - null\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main, with a hand-edited line")
    _git(repo, "checkout", "-q", "-b", "proposed/0000000000b2")
    (repo / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "0000000000b2")
    _git(repo, "checkout", "-q", "main")
    assert promote.main(["0000000000b2", "--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "entry 2 " in err and "`main` as it stands is refused as well" in err and "on `main` already" in err, err


# --- rows added by the read of the fix round ---------------------------------------------------------------------- #
# Where each renderer's BARE run looks for the files it wrote earlier (relative to its root) — the files it prunes.
_OUTPUTS = {
    "gen-observability": lambda g: ["prometheus/targets/node/zz.generated.yml"],
    "gen-logging": lambda g: [g.GEN_DIR + "/syslog_push_zz.generated.yaml",
                              os.path.dirname(g.LOKI_OVERRIDES_REL) + "/zz.generated.yaml"],
    "gen-homepage": lambda g: ["instance/dashboards/homepage/services.yaml"],
    "gen-backup": lambda g: [g.OUT],
    "gen-prometheus-jobs": lambda g: ["prometheus/jobs.d/zz.generated.yml"],
    "gen-exporters": lambda g: ["docker/services/zz.generated.yaml"],
    "gen-secret-env": lambda g: [os.path.dirname(g.MANIFEST_REL) + "/zz.manifest.generated.yml"],
}


@pytest.mark.parametrize("name", RENDERERS)
def test_a_bare_run_over_a_fleet_with_no_list_leaves_every_generated_file_alone(name, tmp_path, monkeypatch, capsys):
    """The BARE run of each renderer — the mode a deploy and the in-process regenerations use, the one that writes
    and PRUNES — over a throwaway root whose fleet file holds no list (`enabled_modules:` forgotten above the
    items) returns 1, says that it renders nothing, and leaves every generated file it finds there byte for byte:
    the target files, the drop-ins, the retention file, the schedule spec, the manifest, and the portal's tiles.

    Guards the branch standing in front of `--check` only: the row above runs `--check`, which never writes, so a
    renderer that refused under `--check` and took the same file for an empty fleet in its bare run would pass it —
    and delete every file the node ever generated at the next deploy or capability change."""
    gen = _script(name)
    shutil.copytree(os.path.join(ROOT, "instance.example"), tmp_path / "instance")       # an inventory to read
    (tmp_path / "instance" / "fleet.yml").write_text("- proxmox\n- docker_host\n", encoding="utf-8")
    kept = {}
    for rel in _OUTPUTS[name](gen):
        out = tmp_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_text("# generated by an earlier run\n", encoding="utf-8")
        kept[rel] = out.read_bytes()
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))       # where the fleet and the inventory are read
    monkeypatch.setattr(gen, "ROOT", str(tmp_path))                # where this generator writes and prunes
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))              # gen-homepage's target hangs off paths.ROOT
    rc = gen.main([])                                              # must not raise — SystemExit included
    assert rc == 1 and "%s: not rendering:" % name in capsys.readouterr().err, rc
    assert {rel: (tmp_path / rel).read_bytes() if (tmp_path / rel).exists() else None for rel in kept} == kept


def test_the_canonical_refusal_tells_a_bad_fleet_from_a_check_that_did_not_run():
    """The message of `local-canonical.yml`'s refusal, rendered: for a finding of the fleet check it repeats the
    check's own sentence and says to correct `instance/fleet.yml` in the worktree; for a check that printed nothing,
    or died of something else (no PyYAML for `python3`, no fleet file), it says that nothing was checked and what
    has to run — and in every case that nothing was staged, committed or pushed.

    Guards the refusal sending an operator to edit a fleet file that is fine: the task refuses on ANY non-zero
    exit, so "correct instance/fleet.yml" under a traceback about a missing module is advice for another fault."""
    import jinja2
    refuse = next(t for t in _tasks("local-canonical.yml")
                  if "ansible.builtin.fail" in t and "_fleet_check_canonical" in str(t.get("when")))
    message = refuse["ansible.builtin.fail"]["msg"]
    render = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(message).render
    found = "gen-requirements: instance/fleet.yml: enabled_modules entry 2 is not a device-class key (the rule)."
    said = render(_fleet_check_canonical={"stderr": found, "stderr_lines": [found]}, worktree="/w")
    assert found in said and "Correct instance/fleet.yml in /w" in said and "Nothing was checked" not in said, said
    died = "ModuleNotFoundError: No module named 'yaml'"
    for result in ({"stderr": "", "stderr_lines": []}, {},
                   {"stderr": "Traceback\n" + died, "stderr_lines": ["Traceback", died]}):
        said = render(_fleet_check_canonical=result, worktree="/w")
        assert "Nothing was checked" in said and "Correct instance/fleet.yml" not in said, said
        assert "staged, committed and pushed nothing" in said, said


def test_the_refusal_says_the_fault_is_on_main_exactly_when_main_itself_is_refused(tmp_path, capsys):
    """With a line that is not a class key on `main`, a proposal that EDITS the fleet file somewhere else — which is
    what an onboard of a new class is — is refused WITH the sentence that the fault is on `main` already; and a
    proposal that brings a fault of its own to a `main` that resolves (two units pinning one collection differently,
    the fleet file untouched) is refused WITHOUT it.

    Guards the hint being keyed on "this proposal does not touch the fleet file": the commonest proposal touches
    it, and was then told "entry 2 is not a device-class key" with no word about which tree holds entry 2 — while a
    pin conflict of the proposal's own was told that the fault is on `main` and every other proposal refused."""
    promote = _script("kontroll-promote")
    repo = _tree(tmp_path / "line-on-main", "enabled_modules:\n  - docker_host\n  - null\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main, with a hand-edited line")
    _git(repo, "checkout", "-q", "-b", "proposed/0000000000d4")
    _fleet(repo, "enabled_modules:\n  - docker_host\n  - null\n  - proxmox\n")       # the onboard's own line
    _git(repo, "commit", "-qam", "an onboard enables a class")
    _git(repo, "checkout", "-q", "main")
    assert promote.main(["0000000000d4", "--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "entry 2 " in err and "on `main` already" in err, err
    repo = _tree(tmp_path / "main-resolves", "enabled_modules:\n  - docker_host\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "checkout", "-q", "-b", "proposed/0000000000e5")
    for key, version in (("dock_a", "==1.0.0"), ("dock_b", "==2.0.0")):
        unit = repo / "instance" / "actuation" / key
        unit.mkdir(parents=True)
        (unit / "unit.yml").write_text(yaml.safe_dump({
            "schema": 1, "key": key, "status": "active",
            "unit": {"kind": "role", "collection": "community.docker", "name": key},
            "install": {"collections": [{"name": "community.docker", "version": version}]},
            "target": {"device_class": "docker_host", "inventory_group": "docker_hosts", "blast_radius": "LAN"}}),
            encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two units that pin one collection differently")
    _git(repo, "checkout", "-q", "main")
    assert promote.main(["0000000000e5", "--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "community.docker" in err and "on `main` already" not in err, err


# --- rows added by the read of the fix round ---------------------------------------------------------------------- #
def test_a_refusal_that_is_not_about_the_fleet_says_nothing_about_the_fleet(tmp_path, capsys, monkeypatch):
    """When the gate refuses for a reason that does not name the fleet file — two conflicting pins — the refusal
    carries no sentence about the fleet file or about `main`, although the proposal does not change that file.

    Guards the hint being printed under every refusal: "every other proposal is refused until then" is untrue
    under a pin conflict that is this proposal's own, and it sends the operator to `main` for a fault that the
    proposal in front of them holds."""
    promote = _script("kontroll-promote")
    repo = _tree(tmp_path / "repo", "enabled_modules:\n  - docker_host\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "checkout", "-q", "-b", "proposed/0000000000f6")
    (repo / "notes.txt").write_text("a change of its own\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "0000000000f6")
    _git(repo, "checkout", "-q", "main")
    # the gate refuses the PROPOSAL's commit (it is asked with `main`'s id beside it) and passes `main`'s own
    monkeypatch.setattr(promote, "_tree_refusal", lambda commit_id, gitdir, main_id=None: (
        "requirements pin conflict: collection community.docker is pinned to both '==3.0.0' and '==4.0.0' by "
        "different actuation units — re-pin one (gen-requirements fails closed)." if main_id else None))
    assert promote.main(["0000000000f6", "--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "pin conflict" in err and "fleet file" not in err and "`main` already" not in err, err


@pytest.mark.parametrize("flags", [("--fleet-check",), ("--conflict-check",), ()], ids=["fleet-check", "conflict-check", "generate"])
@pytest.mark.parametrize("fleet_bytes", [
    ("enabled_modules:\n  - docker_host\n  - !!int %s\n" % MARK).encode(),
    ("enabled_modules:\n  - docker_host\n  - !!bool %s\n" % MARK).encode(),
    ("enabled_modules:\n  - docker_host\n  - !!float %s\n" % MARK).encode(),
    ("enabled_modules:\n  - docker_host\n  - !!timestamp %s\n" % MARK).encode(),
    b"enabled_modules:\n  - docker_host\n  - 2026-02-30\n",
    b"# caf\xe9\nenabled_modules:\n  - docker_host\n",
], ids=["int-tag", "bool-tag", "float-tag", "timestamp-tag", "a-date-that-does-not-exist", "not-utf-8"])
def test_a_fleet_file_a_constructor_rejects_is_one_sentence_too(tmp_path, flags, fleet_bytes):
    """A fleet file that PARSES as YAML and then fails in a constructor (`!!int x`, `!!bool x`, `!!float x`,
    `!!timestamp x`, a date that does not exist), or that is not UTF-8, is answered in each seat of the collection
    generator with one line — no traceback, and no piece of the file.

    Guards the errors that are NOT a YAMLError: a ValueError's and a KeyError's last line quotes the scalar itself
    — text of the operator's file, in a play's output and in a promote's refusal."""
    root = _tree(tmp_path, "enabled_modules: []\n")
    for rel in ("config", "instance"):
        (root / rel / "fleet.yml").write_bytes(fleet_bytes)
    p = _gen_requirements(root, *flags)
    assert p.returncode != 0 and "Traceback" not in p.stderr, p.stderr[-400:]
    assert MARK.lower() not in p.stderr.lower() and "2026" not in p.stderr, p.stderr
    assert "is not YAML this tool can read" in p.stderr and len(p.stderr.strip().splitlines()) == 1, p.stderr


@pytest.mark.parametrize("flags", [("--fleet-check",), ("--conflict-check",), ()], ids=["fleet-check", "conflict-check", "generate"])
def test_a_fleet_file_that_cannot_be_opened_is_one_sentence(tmp_path, flags):
    """With an `instance/` directory and no fleet file in it or at the legacy path, each seat of the collection
    generator says in one line that the file cannot be opened — no traceback, no path of the node.

    Guards the check's last line being a `FileNotFoundError` that names `config/fleet.yml`: the play that prints
    it then tells the operator to correct `instance/fleet.yml`, a file the message never mentioned."""
    root = _tree(tmp_path, "enabled_modules: []\n")
    for rel in ("config", "instance"):
        (root / rel / "fleet.yml").unlink()
    p = _gen_requirements(root, *flags)
    assert p.returncode != 0 and "Traceback" not in p.stderr and root.name not in p.stderr, p.stderr[-400:]
    assert "instance/fleet.yml cannot be opened" in p.stderr and len(p.stderr.strip().splitlines()) == 1, p.stderr


@pytest.mark.parametrize("fleet_text", ["- docker_host\n", "docker_host\n", "enabled_modules: docker_host\n"],
                         ids=["file-a-list", "file-a-word", "value-a-string"])
@pytest.mark.parametrize("service,apply", [("observe", "apply_telemetry_plan"), ("logsvc", "apply_logging_plan"),
                                           ("backup", "apply_plan")])
def test_a_reconfigure_is_refused_before_it_writes_when_the_fleet_holds_no_list(service, apply, fleet_text, tmp_path,
                                                                             monkeypatch):
    """A capability promote (telemetry, logging, backup) over a fleet file that holds no list of classes raises
    the repository's refusal (`RepositoryDataError`: 409, an audit line) BEFORE the module file is written, and its
    sentence names the file and the rule only.

    Guards a regeneration that did not happen being reported as done: each generator renders nothing for such a
    file and returns non-zero from its entry point, and these callers never looked at that — the module change was
    written, staged beside its old generated files and answered "applied", with two lines on a container's stderr
    as the only trace."""
    import importlib
    mod = importlib.import_module("kontroll.service." + service)
    (tmp_path / "instance").mkdir()
    (tmp_path / "instance" / "fleet.yml").write_text(fleet_text, encoding="utf-8")
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))
    wrote = []
    monkeypatch.setattr(mod, "write_module", lambda plan: wrote.append(plan) or True)
    with pytest.raises(paths.RepositoryDataError) as refused:
        getattr(mod, apply)({"key": "docker_host", "method": "blackbox", "text_before": "a", "text_after": "b",
                             "mod_path": str(tmp_path / "module.yml"), "mod_rel": "modules/docker_host/module.yml"})
    assert not wrote and "instance/fleet.yml" in str(refused.value) and "docker_host" not in str(refused.value)


@pytest.mark.parametrize("fleet_bytes", [b"enabled_modules: [\n  - docker_host\n", b"enabled_modules:\n  - \xff\xfe\n"],
                         ids=["not-yaml", "not-utf-8"])
def test_the_reconfigure_guard_leaves_a_fleet_file_it_cannot_read_to_the_generator(fleet_bytes, tmp_path, monkeypatch):
    """`_blockwrite.require_fleet_list` does not refuse a fleet file that cannot be parsed: it answers nothing, and
    the reconfigure goes on to the generator, which stops on such a file as it always did.

    Guards the guard growing a second answer for a fault that already has one: its sentence says the file "holds no
    list of classes", and under a file that does not parse that would send the operator after the wrong fault."""
    from kontroll.service import _blockwrite
    (tmp_path / "instance").mkdir()
    (tmp_path / "instance" / "fleet.yml").write_bytes(fleet_bytes)
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))
    assert _blockwrite.require_fleet_list() is None


@pytest.mark.parametrize("play", ["bootstrap.yml", "deploy-stack.yml", "local-canonical.yml"])
def test_a_plays_fleet_check_leaves_no_bytecode_in_the_worktree(play):
    """Each play runs its fleet check as `python3 -B …`.

    Guards the mandated dry-run writing into the operator's checkout: the check is the one generator call that
    runs under `--check`, it imports the `kontroll` package, and inside the installer container it runs as root —
    without `-B` a `kontroll check` left a root-owned `scripts/kontroll/__pycache__/` in the bound worktree."""
    (task,) = [t for t in _tasks(play) if _is_fleet_check(t)]
    assert str(task["ansible.builtin.command"]["cmd"]).split()[:2] == ["python3", "-B"], task
