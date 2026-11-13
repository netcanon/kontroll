"""The API backend's check and backup take a read the dry-run skipped as not made, never as a failed answer (I-6).

WHY. `ansible.builtin.uri` has no check mode, so `--check` skips the backend_api role's GET and its pull, and a
skipped task's result has no `status`. The check's refusal and the backup's failure record read that as status 0. So
in the mandated `--check --diff` dry-run an API-class host with a good credential failed its check, which ping.yml's
rescue reported as UNREACHABLE (CHECK FAILED since I-84), and failed its backup, which fails the run (T27's runtime read, N8; until then
tests/fresh-node-check.sh could take the class no further than its credential gate). Each now tells a read check mode
skipped apart, and nothing else: a real run reads the status as before, a read a real run left skipped is still
refused, a dry-run read a `when` skipped is refused as a real run's is, and a dry-run read that did answer (were `uri`
to gain check mode) is held to its status.

The `when:` conditions are read from the role files and evaluated as Ansible evaluates a conditional: every item must be
a boolean (ansible-core 2.19 refuses any other result) and true; core Jinja with StrictUndefined, and `skipped` and
`failed` the tests Ansible registers (a result holding `skipped: true`, `failed: true`). The results are the shapes both
cores register: a check-mode skip (`changed`, `msg`, `skipped`), a `when` skip (which adds `skip_reason` and
`false_condition`), a success with content, a success whose body is empty or only blank lines (a failed capture since
I-5, still a live check), a 401, and a refused connection, which `failed_when: false` keeps with a status of -1.
"""
import os
import re

import jinja2
import jinja2.meta
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASKS = os.path.join(ROOT, "ansible", "roles", "backend_api", "tasks")
SKIPPED = {"changed": False, "skipped": True, "msg": "This action (ansible.builtin.uri) does not support check mode."}
WHEN_SKIPPED = {"changed": False, "skipped": True, "skip_reason": "Conditional result was False",
                "false_condition": "probe_skip_read"}
OK = {"changed": False, "status": 200, "content": "config system global\n"}
EMPTY = {"changed": False, "status": 200, "content": ""}
BLANK = {"changed": False, "status": 200, "content": "\n \n"}
DENIED = {"changed": False, "status": 401, "content": ""}
REFUSED = {"changed": False, "status": -1, "msg": "Status code was -1 and not [200]: Request failed"}
# The API port's probe (I-65), as wait_for registers it on 2.18.19 (2.19.4 adds `failed: false` to the first and an
# `exception` to the second): where the port took the connection, and where it took none in the probe's timeout.
TOOK = {"changed": False, "elapsed": 0, "port": 443, "state": "started", "search_regex": None, "match_groups": [],
        "match_groupdict": {}, "path": None}
NO_CONNECTION = {"changed": False, "elapsed": 5, "failed": True, "msg": "Timeout when waiting for 192.0.2.4:443"}
WRITTEN = {"changed": True, "dest": "/backups/FortiGate_192-0-2-4_full-configuration.conf"}

REFUSE = "Refuse unless the API actually answered successfully"
REPORT = "Report the check endpoint as not read in a dry-run"
FAILED = "Record capture failure (reported in summary, not a hard abort)"
NOTHING = "Record an empty capture as failed (any earlier capture is kept)"
BREPORT = "Report the backup endpoint as not read in a dry-run"
PROBE = "Probe the API port of a device that gave the pull no answer"
UNREACH = "Record a device that took no connection as not run (the run passes; the summary names it)"
WRITE, RECORD = "Write capture", "Record successful capture"
# Every task of the two entrypoints that reads a registered read, and the reads themselves.
READERS = {("check.yml", "GET the check endpoint"), ("check.yml", REPORT), ("check.yml", REFUSE),
           ("backup.yml", "Pull config via the recipe's backup endpoint"), ("backup.yml", BREPORT),
           ("backup.yml", PROBE), ("backup.yml", FAILED), ("backup.yml", NOTHING), ("backup.yml", WRITE),
           ("backup.yml", RECORD)}
GRID = [  # (check mode, the read's result)
    (True, SKIPPED), (False, OK), (False, DENIED), (False, REFUSED), (False, SKIPPED), (False, WHEN_SKIPPED),
    (False, EMPTY), (False, BLANK), (True, OK), (True, DENIED), (True, REFUSED), (True, WHEN_SKIPPED), (True, EMPTY)]


def _walk(tasks, forced=None):
    """Each task, wherever in its blocks it sits, with the `check_mode` of the blocks around it where it sets none."""
    for t in tasks or []:
        here = t.get("check_mode", forced)
        yield t, here
        for k in ("block", "rescue", "always"):
            yield from _walk(t.get(k), here)


def _tasks(entrypoint):
    with open(os.path.join(TASKS, entrypoint), encoding="utf-8") as fh:
        return [t for t, _ in _walk(yaml.safe_load(fh))]


def _task(entrypoint, name):
    """The task of that name in the entrypoint, wherever in its blocks it sits."""
    found = [t for t in _tasks(entrypoint) if t.get("name") == name]
    assert len(found) == 1, (entrypoint, name)
    return found[0]


def _skipped(result):
    if not isinstance(result, dict):
        raise TypeError("the 'skipped' test expects a dictionary")
    return result.get("skipped", False)


def _items(entrypoint, name):
    when = _task(entrypoint, name)["when"]
    return when if isinstance(when, list) else [when]


def _failed(result):
    if not isinstance(result, dict):
        raise TypeError("the 'failed' test expects a dictionary")
    return result.get("failed", False)


def _runs(entrypoint, name, ctx):
    """Whether the task runs: every item of its `when:` a boolean and true, as Ansible evaluates them."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.tests["skipped"] = _skipped
    env.tests["failed"] = _failed
    for c in _items(entrypoint, name):
        value = env.compile_expression(c)(**ctx)
        assert isinstance(value, bool), (name, c, value)  # ansible-core 2.19 refuses a non-boolean conditional
        if not value:
            return False
    return True


def _names(entrypoint, name):
    """The variables the task's `when:` reads."""
    env = jinja2.Environment()
    env.tests["skipped"] = _skipped
    env.tests["failed"] = _failed
    return set().union(*(jinja2.meta.find_undeclared_variables(env.parse("{{ %s }}" % c))
                         for c in _items(entrypoint, name)))


@pytest.mark.parametrize("check_mode, result", GRID)
def test_the_check_refuses_every_answer_but_a_success_and_reports_a_check_mode_skip_as_not_made(check_mode, result):
    """check.yml's refusal fires for a 401 and a refused connection, in a real run or a dry-run that answered, for a
    read a real run left skipped and for a dry-run read a `when` skipped; never for a success (one with an empty
    body too: a check reads the status, and only a capture needs the body), nor for the read check mode skips, which
    the report task names instead, and only then. Guards I-6 (a dry-run failed an API host with a good credential at
    the refusal, and ping reported it UNREACHABLE); the fix widening into a real run, where the check's teeth, the
    only thing that tells ping a firewall did not answer (test_backend_api_check_teeth.py), must still bite; and the
    guard taking any skip for check mode's, so that a `when` added to the GET would pass every host in a dry-run
    without a request (I-6's runtime read, N3)."""
    made = check_mode and result is SKIPPED
    ctx = {"_api_check": result, "ansible_check_mode": check_mode}
    assert _runs("check.yml", REFUSE, ctx) is (not made and result.get("status") not in (200, 201, 204))
    assert _runs("check.yml", REPORT, ctx) is made


@pytest.mark.parametrize("check_mode, result", GRID)
def test_the_backup_fails_every_answer_but_a_200_and_writes_and_reports_a_check_mode_skip(check_mode, result):
    """backup.yml records a failed capture (which fails the run) for a 401 and a refused connection, in a real run or
    a dry-run that answered, for a pull a real run left skipped and for a dry-run pull a `when` skipped; it writes the
    capture for a 200 with a config in its body, and for the pull check mode skips, which it reports as not made and
    previews an empty capture as the raw-SSH and RouterOS writes do (test_backup_capture_check_mode.py); and a 200
    whose body is empty or only blank lines is a failed capture of its own (I-5), in a real run or a dry-run that
    answered. Exactly one of the three runs. Guards I-6 (a dry-run failed an API host with a good credential, and
    the run with it), a real run's failed pull being written or passed as a capture, a dry-run's skipped pull going
    unsaid (I-6's runtime read, N4) or a `when` skip passing as check mode's (N3), and an empty 200 written over the
    last good capture (I-5; test_backup_empty_capture_refused.py holds the other backends). The port is probed for
    every read but check mode's skip that holds no status, or one below 0 (I-65); here the probe found the port open,
    so the record is the answer's (test_a_pull_with_no_answer_is_recorded_by_its_probe holds the port that is not)."""
    made = check_mode and result is SKIPPED
    answered = result.get("status") == 200
    config = bool(str(result.get("content", "")).strip())
    ctx = {"_api_resp": result, "ansible_check_mode": check_mode, "_api_probe": TOOK}
    assert _runs("backup.yml", PROBE, ctx) is (not made and result.get("status", -1) < 0)
    runs = {name: _runs("backup.yml", name, ctx) for name in (FAILED, NOTHING, WRITE)}
    assert runs[FAILED] is (not made and not answered)
    assert runs[NOTHING] is (answered and not config)
    assert runs[WRITE] is (made or (answered and config))
    assert sum(runs.values()) == 1, runs
    assert _runs("backup.yml", BREPORT, ctx) is made


def test_the_capture_is_recorded_where_its_write_ran_and_only_there():
    """The write registers `_api_write`, and "Record successful capture" reads that and nothing else: it runs for a
    write that ran (in a dry-run, its preview) and not for one its `when` skipped. Guards the record following a copy
    of the write's condition instead of the write: with the write's dry-run guard dropped, the dry-run still said
    "captured", and the fresh-node check, which reads that line to hold the write, passed (I-6's runtime read, N1)."""
    assert _task("backup.yml", WRITE).get("register") == "_api_write"
    assert _names("backup.yml", RECORD) == {"_api_write"}
    for check_mode in (True, False):
        assert _runs("backup.yml", RECORD, {"_api_write": WRITTEN, "ansible_check_mode": check_mode}) is True
        assert _runs("backup.yml", RECORD, {"_api_write": WHEN_SKIPPED, "ansible_check_mode": check_mode}) is False


@pytest.mark.parametrize("entrypoint, name", [("check.yml", REPORT), ("backup.yml", BREPORT)])
def test_each_dry_run_report_prints_the_host_and_the_recipe_and_nothing_of_the_read(entrypoint, name):
    """Each report task is a `debug`, not `no_log`, and its message reads only the host's name and the recipe's.
    Guards a new task becoming the leak the role's other tasks are built to avoid: the read carries the token and its
    answer can be the device's configuration (`_api_check`, `_api_resp`, `_api_token`, `_api_headers`), and a
    `no_log` on it would hide what it exists to say."""
    task = _task(entrypoint, name)
    assert "ansible.builtin.debug" in task and task.get("no_log") is not True, task
    names = jinja2.meta.find_undeclared_variables(jinja2.Environment().parse(task["ansible.builtin.debug"]["msg"]))
    assert names == {"inventory_hostname", "backend_api_recipe"}, names


@pytest.mark.parametrize("check_mode", [False, True])
@pytest.mark.parametrize("probe, unreachable", [(NO_CONNECTION, True), (TOOK, False),
                                                (WHEN_SKIPPED, False)])
def test_a_pull_with_no_answer_is_recorded_by_its_probe(check_mode, probe, unreachable):
    """A pull that got no answer (status -1) whose port probe took no TCP connection is recorded not run,
    unreachable, and not as a failed capture, so the run passes (I-65: one device down never fails the fleet's run);
    one whose probe found the port open, or did not run, is the failed capture, which fails the run. Guards the probe
    read the wrong way round (every API device that answered badly passed), and both records running (the failure's
    would win, and a device not reached would fail the run again)."""
    ctx = {"_api_resp": REFUSED, "_api_probe": probe, "ansible_check_mode": check_mode}
    assert _runs("backup.yml", UNREACH, ctx) is unreachable
    assert _runs("backup.yml", FAILED, ctx) is (not unreachable)


def test_every_task_that_reads_a_registered_read_is_one_these_tests_evaluate():
    """Every task of check.yml and backup.yml that names `_api_check`, `_api_resp` or `_api_write` (outside its
    name) is the read that registers it or a task the tests above evaluate. Guards a new task between the read and
    the refusal that reads the status (a `set_fact` of `_api_check.status`): the tables above evaluate only the tasks
    they name, so it would fail every dry-run unseen here, and the fresh-node check that would catch it does not run
    where KONTROLL_RUNS_ON is set (I-6's tests-and-docs read, N4)."""
    found = set()
    for entrypoint in ("check.yml", "backup.yml"):
        for t in _tasks(entrypoint):
            body = {k: v for k, v in t.items() if k not in ("name", "block", "rescue", "always")}
            if any(r in yaml.safe_dump(body) for r in ("_api_check", "_api_resp", "_api_write")):
                found.add((entrypoint, t.get("name")))
    assert found == READERS, sorted(found ^ READERS)


def test_neither_read_is_forced_to_run_in_a_dry_run():
    """No `uri` task of the role (the check's GET, the backup's pull), nor a block around one, carries a false
    `check_mode`. Guards the read the fix must not make: forced, the mandated `--check` run sends the token to the
    device and acts on its answer. test_check_mode_resolvers.py's device-role ban reads only command, shell and raw
    tasks, and the fresh-node check, which catches it only by making the request, does not run where KONTROLL_RUNS_ON
    is set (I-6's tests-and-docs read, S1)."""
    reads = []
    for entrypoint in sorted(os.listdir(TASKS)):
        with open(os.path.join(TASKS, entrypoint), encoding="utf-8") as fh:
            reads += [(entrypoint, t.get("name"), here) for t, here in _walk(yaml.safe_load(fh))
                      if "ansible.builtin.uri" in t or "uri" in t]
    assert {e for e, _, _ in reads} >= {"check.yml", "backup.yml"}, reads
    assert [r for r in reads if str(r[2]).strip().lower() == "false"] == [], reads


def test_no_block_around_a_read_is_skipped_in_a_dry_run():
    """No block around the check's GET or the backup's pull has a `when:` that reads `ansible_check_mode`. Guards the
    dry-run skipping the role whole (`- not ansible_check_mode` on the block): every task above is then skipped with
    it, so the tables still pass, ping reports the host OK and the backup records nothing, and only the fresh-node
    check, which does not run where KONTROLL_RUNS_ON is set, would see it (I-6's tests-and-docs read, N4)."""
    def around(tasks, blocks):
        for t in tasks or []:
            if "ansible.builtin.uri" in t:
                yield t.get("name"), blocks
            for k in ("block", "rescue", "always"):
                yield from around(t.get(k), blocks + [t])

    for entrypoint, read in (("check.yml", "GET the check endpoint"),
                             ("backup.yml", "Pull config via the recipe's backup endpoint")):
        with open(os.path.join(TASKS, entrypoint), encoding="utf-8") as fh:
            found = [b for name, b in around(yaml.safe_load(fh), []) if name == read]
        assert len(found) == 1 and found[0], (entrypoint, read)
        for block in found[0]:  # a word match: the blocks' conditions use Ansible's filters, which core Jinja lacks
            assert not re.search(r"\bansible_check_mode\b", str(block.get("when") or "")), (entrypoint, block["when"])
