"""The API backend's check and backup take a read the dry-run skipped as not made, never as a failed answer (I-6).

WHY. `ansible.builtin.uri` has no check mode, so `--check` skips the backend_api role's GET and its pull, and a
skipped task's result has no `status`. The check's refusal and the backup's failure record read that as status 0. So
in the mandated `--check --diff` dry-run an API-class host with a good credential failed its check, which ping.yml's
rescue reports as UNREACHABLE, and failed its backup, which fails the run (T27's runtime read, N8; until then
tests/fresh-node-check.sh could take the class no further than its credential gate). Each now tells a read a dry-run
skipped apart, and nothing else: a real run reads the status as before, a read a real run left skipped is still
refused, and a dry-run read that did answer (were `uri` to gain check mode) is held to its status.

The `when:` conditions are read from the role files and evaluated as Ansible evaluates a conditional, every item
true: core Jinja with StrictUndefined, and `skipped` the test Ansible registers (a result holding `skipped: true`).
The results are the shapes `uri` registers: a skipped task's, a success with content, a 401, and a refused
connection, which `failed_when: false` keeps with a status of -1.
"""
import os

import jinja2
import jinja2.meta
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASKS = os.path.join(ROOT, "ansible", "roles", "backend_api", "tasks")
SKIPPED = {"changed": False, "skipped": True, "msg": "remote module (ansible.legacy.uri) does not support check mode"}
OK = {"changed": False, "status": 200, "content": "config system global\n"}
DENIED = {"changed": False, "status": 401, "content": ""}
REFUSED = {"changed": False, "status": -1, "msg": "Status code was -1 and not [200]: Request failed"}


def _task(entrypoint, name):
    """The task of that name in the entrypoint, wherever in its blocks it sits."""
    def walk(tasks):
        for t in tasks or []:
            yield t
            for k in ("block", "rescue", "always"):
                yield from walk(t.get(k))
    with open(os.path.join(TASKS, entrypoint), encoding="utf-8") as fh:
        found = [t for t in walk(yaml.safe_load(fh)) if t.get("name") == name]
    assert len(found) == 1, (entrypoint, name)
    return found[0]


def _skipped(result):
    if not isinstance(result, dict):
        raise TypeError("the 'skipped' test expects a dictionary")
    return result.get("skipped", False)


def _runs(entrypoint, name, register, result, check_mode):
    """Whether the task runs: every item of its `when:` true, as Ansible evaluates them."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.tests["skipped"] = _skipped
    when = _task(entrypoint, name)["when"]
    ctx = {register: result, "ansible_check_mode": check_mode}
    return all(bool(env.compile_expression(c)(**ctx)) for c in (when if isinstance(when, list) else [when]))


REFUSE = "Refuse unless the API actually answered successfully"
REPORT = "Report the check endpoint as not read in a dry-run"


@pytest.mark.parametrize("check_mode, result, refused, reported", [
    (True, SKIPPED, False, True),
    (False, OK, False, False),
    (False, DENIED, True, False),
    (False, REFUSED, True, False),
    (False, SKIPPED, True, False),
    (True, DENIED, True, False),
    (True, OK, False, False)])
def test_the_check_refuses_every_answer_but_a_success_and_reports_a_dry_run_read_as_not_made(
        check_mode, result, refused, reported):
    """check.yml's refusal fires for a 401 and a refused connection, in a real run or a dry-run that answered, and
    for a read a real run left skipped; never for a success, nor for the read a dry-run skips, which the report task
    names instead, and only then. Guards I-6 (a dry-run failed an API host with a good credential at the refusal, and
    ping reported it UNREACHABLE), and the fix widening into a real run: the check's teeth, which are the only thing
    that tells ping a firewall did not answer (test_backend_api_check_teeth.py), must still bite there."""
    assert _runs("check.yml", REFUSE, "_api_check", result, check_mode) is refused
    assert _runs("check.yml", REPORT, "_api_check", result, check_mode) is reported


@pytest.mark.parametrize("check_mode, result, failed, written", [
    (True, SKIPPED, False, True),
    (False, OK, False, True),
    (False, DENIED, True, False),
    (False, REFUSED, True, False),
    (False, SKIPPED, True, False),
    (True, DENIED, True, False),
    (True, OK, False, True)])
def test_the_backup_fails_every_answer_but_a_200_and_writes_a_dry_run_read(check_mode, result, failed, written):
    """backup.yml records a failed capture (which fails the run) for a 401 and a refused connection, in a real run
    or a dry-run that answered, and for a pull a real run left skipped; it writes and records the capture for a 200,
    and for the pull a dry-run skips, which previews an empty capture as the raw-SSH and RouterOS writes do
    (test_backup_capture_check_mode.py). Never both, never neither. Guards I-6 (a dry-run failed an API host with a
    good credential, and the run with it) and a real run's failed pull being written or passed as a capture."""
    assert _runs("backup.yml", "Record capture failure (reported in summary, not a hard abort)", "_api_resp", result,
                 check_mode) is failed
    for name in ("Write capture", "Record successful capture"):
        assert _runs("backup.yml", name, "_api_resp", result, check_mode) is written, name


def test_the_dry_run_report_prints_the_host_and_the_recipe_and_nothing_of_the_read():
    """The report task is a `debug`, not `no_log`, and its message reads only the host's name and the recipe's.
    Guards the new task becoming the leak the check's other tasks are built to avoid: the GET carries the token and
    its answer can be the device's configuration (`_api_check`, `_api_token`, `_api_headers`), and a `no_log` on it
    would hide what it exists to say."""
    task = _task("check.yml", REPORT)
    assert "ansible.builtin.debug" in task and task.get("no_log") is not True, task
    names = jinja2.meta.find_undeclared_variables(jinja2.Environment().parse(task["ansible.builtin.debug"]["msg"]))
    assert names == {"inventory_hostname", "backend_api_recipe"}, names
