"""The per-device backup alert and the failed-job alert read the lines a scheduled backup writes (I-65, I-82).

WHY. Before I-65 a backup run that missed a device said so in one line of its summary, and the one alert on it waited
as long as its window, so it never fired; a device not reached failed the run, or passed it unnamed; and the ansible
log the alert reads was written by no scheduled job, as Semaphore hands a job only the variables it is told to forward
(I-82). Now the run's summary prints one `KONTROLL-BACKUP-MISSED` line per device it did not back up, the backup
alert reads each as its own series (device, class, reason, run id, Semaphore task), and the job alert reads
Semaphore's own `Failed to run task` line per task. No Loki runs in the suite, so these rows read each rule's query in
Python (tests/_missed_alerts.py, which refuses any form it does not know) over lines measured from a real Semaphore
run (v2.19.17, ansible-core 2.20, the yaml callback this tree's ansible.cfg sets), folded as the callback folds them.
tests/integration/test_backup_missed_runs.py runs the backup itself.
"""
import json
import os
import re

import pytest
import yaml

import _missed_alerts as ma

ROOT = ma.ROOT
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")
SEMAPHORE = os.path.join(ROOT, "docker", "services", "semaphore.yaml")
CONTAINERS = os.path.join(ROOT, "docker", "vector", "config.d", "sources", "internal_containers.yaml")
SINK = os.path.join(ROOT, "docker", "vector", "config.d", "sinks", "loki.yaml")
ROLES = os.path.join(ROOT, "ansible", "roles")
STREAM = {"source": "internal", "service": "ansible", "host": "control-vm", "level": "info"}
RUN = "20261113T021016Z"
# From the measured run's ansible log: the stamp, a summary line, the summary's last line before the misses, and a miss
# folded where the callback folded it (each event ends at the next line that starts with a date, as Vector cuts them).
LOG = "\n".join([
    "2026-11-13 02:10:16,544 p=241 u=semaphore n=ansible INFO| TASK [Log kontroll_run_id] ***",
    "2026-11-13 02:10:16,606 p=241 u=semaphore n=ansible INFO| ok: [ap-good -> localhost] => ",
    "    msg: kontroll_run_id=" + RUN,
    "",
    "2026-11-13 02:10:33,989 p=241 u=semaphore n=ansible INFO| ok: [api-down] => ",
    "    msg: 'api-down (fortigate): not run — unreachable: the device took no TCP connection",
    "        on the fortios API port 443, so nothing was read and any earlier capture is kept.'",
    "",
    "2026-11-13 02:10:34,078 p=241 u=semaphore n=ansible INFO| TASK [Name a device this run did not back up, for the"
    " alert] ***",
    "2026-11-13 02:10:34,116 p=241 u=semaphore n=ansible INFO| skipping: [ap-good]",
    "2026-11-13 02:10:34,132 p=241 u=semaphore n=ansible INFO| ok: [api-down] => ",
    "    msg: KONTROLL-BACKUP-MISSED device=api-down class=fortigate reason=unreachable kontroll_run_id=" + RUN,
    "        semaphore_task=1",
    "",
    "2026-11-13 02:10:34,208 p=241 u=semaphore n=ansible INFO| TASK [Fail the run if any capture failed] ***",
])
MISS = ("KONTROLL-BACKUP-MISSED device=api-down class=fortigate reason=unreachable kontroll_run_id=%s semaphore_task=1"
        % RUN)
SERIES = {"device": "api-down", "class": "fortigate", "reason": "unreachable", "run_id": RUN, "semaphore_task": "1"}
HEAD = "2026-11-13 02:10:34,132 p=241 u=semaphore n=ansible INFO| ok: [api-down] => "
# From the measured run's Semaphore log: the two lines that name a failed task, and lines of the same tasks that do not.
FAILED_1 = ('time="2026-11-13T02:10:34Z" level=warning msg="Failed to run task" context=task_runner'
            ' error="exit status 2" task_id=1 task_status=running')
PARSER_1 = ('time="2026-11-13T02:10:34Z" level=warning msg="invalid ansible result host: Failed to run task:'
            ' exit status 2" content=ansible_result_parser task_id=1')
FAILED_2 = ('time="2026-11-13T02:10:36Z" level=warning msg="Failed to run task" context=task_runner'
            ' error="exit status 1" task_id=2 task_status=running')
QUIET = ['time="2026-11-13T02:10:34Z" level=info msg="Task status updated" context=task_logger status=error task_id=1',
         'time="2026-11-13T02:10:34Z" level=info msg="Stopped running task backup" task_id=1',
         'time="2026-11-13T02:10:14Z" level=info msg="Task started" context=task_pool task_id=1 task_name=backup']


def _event(message, **labels):
    """A Vector event of the ansible log, as the internal_ansible shape labels it."""
    return {"message": message, "labels": dict(STREAM, run_id=ma.run_id(message), **labels)}


def _container(message, name="semaphore"):
    """A Vector event of a container's log, as the internal_containers shape labels it."""
    return {"message": message, "container_name": name,
            "labels": dict(STREAM, service=name, run_id=ma.run_id(message))}


def _seconds(text):
    return int(text[:-1]) * {"s": 1, "m": 60, "h": 3600}[text[-1]]


def _group(uid):
    """The rule group of logging-rules.yaml that holds the rule `uid` (exactly one)."""
    with open(ma.RULES, encoding="utf-8") as fh:
        (group,) = [g for g in yaml.safe_load(fh)["groups"] if any(r["uid"] == uid for r in g["rules"])]
    return group


def _tasks(node):
    """Every task in a task list, a block's, rescue's and always' tasks included."""
    for task in node or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _tasks(task.get(key))


def _role_tasks(role):
    with open(os.path.join(ROLES, role, "tasks", "backup.yml"), encoding="utf-8") as fh:
        return list(_tasks(yaml.safe_load(fh)))


@pytest.mark.parametrize("uid", [ma.BACKUP, ma.JOB])
def test_the_reader_reads_each_rule_in_its_one_form_and_refuses_another(uid, monkeypatch):
    """Each rule's query is in the one form tests/_missed_alerts.py reads, and the reader refuses a query with a stage
    added, one dropped or changed, or a regexp Python and RE2 may read otherwise. Why: these rows read the query in
    Python because no Loki runs here; a reader that passed a stage it did not read would pass a rule Loki reads
    otherwise, and the rows would hold nothing."""
    p = ma.pipeline(uid)
    assert p["regexps"] and p["by"] and p["window"] == "1h"
    expr = ma.expression(uid)
    changed = [expr.replace('__error__=""', 'level="error"'), expr.replace(' | line_format "{{.message}}"', ""),
               expr.replace("[1h]", "| logfmt [1h]"), expr.replace("(?P<", "(?=x)(?P<", 1),
               expr.replace("+)`", "{2})`", 1), expr.replace("sum by (", "sum without (")]
    for one in changed:
        assert one != expr
        monkeypatch.setattr(ma, "expression", lambda _uid, one=one: one)
        with pytest.raises(AssertionError):
            ma.pipeline(uid)


@pytest.mark.parametrize("uid", [ma.BACKUP, ma.JOB])
def test_each_rule_fires_inside_its_window_and_names_what_its_series_carry(uid):
    """Each rule waits at most two of its group's evaluation intervals (the group evaluates each minute; each rule
    waits one), so its wait and one more evaluation end inside its window (an hour), and each of its nodes looks back
    exactly that window; its annotations name only labels its series carry, each written `$$labels` (Grafana's file
    provisioning reads a single `$` as an environment variable); no label it groups by is one a regexp of it extracts
    under a name the stream already has (Loki would name the extracted one `<name>_extracted`); and each label it
    groups by is one a regexp extracts or the sink sets. Guards I-65's first fault: the backup rule's `for` was as
    long as its window, so a run's one line left the window as the wait ended, and it never fired; and a wait of
    most of the window (`for: 59m`, which this row's first form passed) pages an hour late, or never where an
    evaluation falls after the line has left."""
    rule, p = ma.rule(uid), ma.pipeline(uid)
    window = _seconds(p["window"])
    interval, wait = _seconds(_group(uid)["interval"]), _seconds(rule["for"])
    assert 0 < wait <= 2 * interval and wait + interval <= window, (wait, interval, window)
    assert {node["relativeTimeRange"]["from"] for node in rule["data"]} == {window}
    text = " ".join(rule["annotations"].values())
    named = re.findall(r"\$\$labels\.([a-z_]+)", text)
    assert named and set(named) <= set(p["by"]), named
    assert text.count("labels.") == len(named), text
    with open(SINK, encoding="utf-8") as fh:
        sink = set(yaml.safe_load(fh)["labels"])
    extracted = {name for one in p["regexps"] for name in re.compile(one).groupindex}
    assert not extracted & sink, extracted & sink
    assert set(p["by"]) <= extracted | sink, set(p["by"]) - extracted - sink
    assert rule["noDataState"] == "OK" and rule["execErrState"] == "Error"


def test_a_missed_device_is_one_series_however_the_line_folds():
    """The backup rule reads the measured miss as one series of the device, its class, the reason, the run's id (from
    the stream's `run_id`, which the internal_ansible shape reads off the same line) and the Semaphore task: as the
    callback folded it, unfolded, folded at each space in turn, folded at every space, and as the JSON callback prints
    it. Guards the line's fields against the callback's fold, which breaks a long line at any space, so the rule reads
    each field on its own; and the run's id against a series that names no run."""
    (event,) = [e for e in ma.events(LOG) if "KONTROLL-BACKUP-MISSED" in e]
    assert ma.read(ma.BACKUP, _event(event)) == SERIES
    spaces = [i for i, c in enumerate(MISS) if c == " "]
    shapes = [MISS, " ".join(MISS.split(" ")).replace(" ", "\n        ")]
    shapes += [MISS[:i] + "\n        " + MISS[i + 1:] for i in spaces]
    for msg in shapes:
        (one,) = ma.events(HEAD + "\n    msg: " + msg + "\n")
        assert ma.read(ma.BACKUP, _event(one)) == SERIES, msg
    (json_event,) = ma.events(HEAD + '{\n    "msg": "%s"\n}' % MISS)
    assert ma.read(ma.BACKUP, _event(json_event)) == SERIES


def test_a_manual_run_and_another_device_are_their_own_series():
    """A run outside Semaphore names its task `none`, and one with no stamp its run `none`, and each is still a
    series; two devices of one run are two series, so a run that misses two sends two alerts. Guards the alert per
    device (I-65): one alert per run would name one device and hide the rest."""
    manual = MISS.replace("semaphore_task=1", "semaphore_task=none").replace(RUN, "none")
    assert ma.read(ma.BACKUP, _event(manual)) == dict(SERIES, run_id="none", semaphore_task="none")
    other = MISS.replace("api-down", "sw-plain").replace("fortigate", "cisco_ios").replace("unreachable", "failed")
    assert ma.read(ma.BACKUP, _event(other)) == dict(SERIES, device="sw-plain", **{"class": "cisco_ios"},
                                                     reason="failed")
    assert ma.read(ma.BACKUP, _event(MISS.replace("unreachable", "not-run")))["reason"] == "not-run"


def test_no_other_line_of_the_run_is_counted():
    """The stamp, a summary line, the task's own header, a skipped host and the run's next task are no miss; nor is a
    miss's text in another stream (a container's log). Guards a count of lines that are not a miss: a rule that
    fired on the summary's every line, or on the task's header, would page each night for every device."""
    misses = [e for e in ma.events(LOG) if ma.read(ma.BACKUP, _event(e)) is not None]
    assert len(misses) == 1 and len(ma.events(LOG)) == 7
    assert ma.read(ma.BACKUP, _container(MISS)) is None
    assert ma.read(ma.BACKUP, _event(MISS, service="semaphore")) is None


def test_the_stamp_and_the_miss_name_one_run():
    """The run id the stream of the miss carries is the one the run's stamp printed. Guards I-65's run id: the miss
    names its run by `_kontroll_run_id`, which the stamp keeps (ansible/playbooks/_log-run-id.yml); a second `now()`
    would name a run no other line of the log names."""
    stamp, miss = [e for e in ma.events(LOG) if "kontroll_run_id=" in e]
    assert ma.run_id(stamp) == ma.run_id(miss) == RUN


def test_the_job_rule_counts_each_failed_task_once_per_task():
    """The job rule reads each of the measured Semaphore lines that name a failed task as the series of that task (the
    backup's two lines are one series), and none of the same tasks' other lines; nor a line of another container that
    says the same. Guards the job alert (I-65): a run that failed before its summary, and every other scheduled job,
    name no device, and Semaphore's own line is the one place each failed task is named."""
    assert ma.read(ma.JOB, _container(FAILED_1)) == ma.read(ma.JOB, _container(PARSER_1)) == {"task_id": "1"}
    assert ma.read(ma.JOB, _container(FAILED_2)) == {"task_id": "2"}
    assert [ma.read(ma.JOB, _container(line)) for line in QUIET] == [None] * len(QUIET)
    assert ma.read(ma.JOB, _container(FAILED_1, name="kontroll-api")) is None
    assert ma.read(ma.JOB, _event(FAILED_1)) is None


def test_the_job_rule_names_the_container_semaphore_runs_in_and_vector_reads():
    """The container the job rule reads is the Semaphore service's `container_name`, and the container log source
    does not exclude it. Guards a rule that names a container no log comes from, which never fires and says so to no
    one (a renamed service, an exclude that takes its prefix)."""
    with open(SEMAPHORE, encoding="utf-8") as fh:
        services = yaml.safe_load(fh)["services"]
    names = {s.get("container_name") for s in services.values()}
    assert ma.pipeline(ma.JOB)["container"] in names
    with open(CONTAINERS, encoding="utf-8") as fh:
        excluded = yaml.safe_load(fh)["exclude_containers"]
    assert not [x for x in excluded if ma.pipeline(ma.JOB)["container"].startswith(x)], excluded


def test_semaphore_forwards_the_log_path_to_its_jobs_and_nothing_else():
    """The Semaphore service forwards ANSIBLE_LOG_PATH to the jobs it runs, and only it, written as the JSON list
    Semaphore decodes (it reads the setting with a JSON parser and does not start on an error, so a YAML flow list
    such as `[ANSIBLE_LOG_PATH]` would stop the service, and a YAML read of it passed that), and the path is in the
    directory Vector's ansible source reads (the one host directory, mounted in each). Guards I-82: of the server's
    own environment Semaphore hands a job only PATH and the variables its SEMAPHORE_FORWARDED_ENV_VARS names, so
    ANSIBLE_LOG_PATH on the server reached no job, no scheduled run wrote the log, and no alert on it could fire.
    Only it: the server's environment holds its database password and the key that encrypts its stored credentials,
    and a variable named here is in every job's environment."""
    with open(SEMAPHORE, encoding="utf-8") as fh:
        semaphore = yaml.safe_load(fh)["services"]["semaphore"]
    env = semaphore["environment"]
    assert json.loads(env["SEMAPHORE_FORWARDED_ENV_VARS"]) == ["ANSIBLE_LOG_PATH"]
    with open(ma.SOURCE, encoding="utf-8") as fh:
        (read,) = yaml.safe_load(fh)["include"]
    with open(os.path.join(ROOT, "docker", "services", "vector.yaml"), encoding="utf-8") as fh:
        vector = yaml.safe_load(fh)["services"]["vector"]

    def host_dir(service, path):
        """The host side of the service's bind of `path`'s directory (a `${VAR:-default}` holds a colon too)."""
        binds = [v[:-3] if v.endswith((":ro", ":rw")) else v for v in service["volumes"] if isinstance(v, str)]
        (found,) = [v.rsplit(":", 1)[0] for v in binds if v.rsplit(":", 1)[1] == os.path.dirname(path)]
        return found

    assert os.path.basename(env["ANSIBLE_LOG_PATH"]) == os.path.basename(read)
    assert host_dir(semaphore, env["ANSIBLE_LOG_PATH"]) == host_dir(vector, read)


def test_the_summary_prints_the_fields_the_rule_reads_for_each_missed_device():
    """The summary's miss line, as Jinja renders it, is read by the backup rule as the device's series; it runs only
    in a real run (a dry-run pages no one), for a host recorded FAILED, recorded unreachable, or with no result, and
    its reason says which, FAILED first. Guards the line and the rule drifting apart: a field renamed or dropped on
    either side reads as an empty label, and a miss printed in a dry-run would page for a run that read nothing."""
    import jinja2

    with open(PLAYBOOK, encoding="utf-8") as fh:
        (summary,) = [p for p in yaml.safe_load(fh) if p.get("name") == "Backup run summary"]
    (task,) = [t for t in summary["tasks"] if t["name"] == "Name a device this run did not back up, for the alert"]
    # Chainable, as Ansible's undefined is: outside Semaphore `semaphore_vars.task_details.id` is undefined, no error.
    rendered = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(task["ansible.builtin.debug"]["msg"])
    msg = rendered.render(inventory_hostname="api-down", device_role="fortigate", _kontroll_missed_reason="unreachable",
                          _kontroll_run_id=RUN, semaphore_vars={"task_details": {"id": 1}})
    assert msg == MISS
    assert ma.read(ma.BACKUP, _event(msg)) == SERIES
    bare = rendered.render(_kontroll_missed_reason="unreachable", inventory_hostname="api-down")
    assert bare == MISS.replace("fortigate", "?").replace(RUN, "none").replace("task=1", "task=none")
    assert ma.read(ma.BACKUP, _event(bare))["class"] == "?"
    assert task["when"][0] == "not ansible_check_mode"
    assert task["when"][1].split() == (
        "(kontroll_backup_failed | default(false) | bool) or (kontroll_backup_unreachable | default(false) | bool)"
        " or kontroll_backup_result is not defined").split()
    assert task["vars"]["_kontroll_missed_reason"].split() == (
        "{{ 'failed' if (kontroll_backup_failed | default(false) | bool) else ('unreachable' if"
        " (kontroll_backup_unreachable | default(false) | bool) else 'not-run') }}").split()
    tasks = [t["name"] for t in summary["tasks"]]
    assert tasks.index("Per-host backup result") < tasks.index(task["name"]) < tasks.index("Fail the run if any"
                                                                                           " capture failed")


def _backends():
    """Each role whose backup records a result, with the role its backup includes (None where it reads itself)."""
    out = {}
    for role in sorted(os.listdir(ROLES)):
        if role.startswith(".") or not os.path.exists(os.path.join(ROLES, role, "tasks", "backup.yml")):
            continue   # .role-template is the copy-me, held to the rule by its contract below
        tasks = _role_tasks(role)
        included = [t["ansible.builtin.include_role"]["name"] for t in tasks if "ansible.builtin.include_role" in t]
        if included or any("kontroll_backup_result" in (t.get("ansible.builtin.set_fact") or {}) for t in tasks):
            out[role] = included[0] if included else None
    return out


def test_every_backup_that_reads_a_device_records_one_not_reached_as_not_run():
    """Each backup entrypoint that reads a device itself records a device it did not reach as not run, unreachable:
    a result that starts `not run — unreachable: ` and `kontroll_backup_unreachable`, never `kontroll_backup_failed`,
    on a read the play could not reach (`<its read> is unreachable`) or, where a read that does not connect fails as a
    task (network_cli) or gives no answer (an API pull's status below 0), on a probe of the device's port that took no
    connection; and each class that
    includes a backend includes one that does; and the copy-me role template's contract says so, as a new class
    starts from it. Guards I-65: a device not reached is tolerated (CLAUDE.md: never let one
    unreachable host fail a fleet-wide run) and named, never recorded FAILED, which fails the run, nor left with no
    result, which the summary printed as "unreachable or skipped" for any cause."""
    with open(os.path.join(ROLES, ".role-template", "tasks", "backup.yml"), encoding="utf-8") as fh:
        contract = " ".join(line.lstrip("# ") for line in fh.read().split("\n") if line.startswith("#"))
    assert "kontroll_backup_unreachable: true" in contract and "`not run — unreachable: `" in contract, contract
    backends = _backends()
    readers = {role for role, inc in backends.items() if inc is None}
    assert {"backend_api", "backend_netcommon_cli", "backend_raw_ssh", "routeros"} <= readers, readers
    assert {inc for inc in backends.values() if inc} <= readers
    for role in readers:
        tasks = _role_tasks(role)
        registered = {t.get("register") for t in tasks}
        marks = [t for t in tasks if (t.get("ansible.builtin.set_fact") or {}).get("kontroll_backup_unreachable")]
        assert marks, role
        for mark in marks:
            fact = mark["ansible.builtin.set_fact"]
            assert fact["kontroll_backup_result"].startswith("not run — unreachable: ") and \
                "kontroll_backup_failed" not in fact, (role, fact)
            m = re.fullmatch(r"(\w+) is (unreachable|failed)", mark["when"])
            assert m and m.group(1) in registered, (role, mark["when"])
            if m.group(2) == "failed":
                (probe,) = [t for t in tasks if t.get("register") == m.group(1)]
                assert "ansible.builtin.wait_for" in probe and probe["delegate_to"] == "localhost", (role, probe)
                assert probe["check_mode"] is False and probe["ignore_errors"] is True, (role, probe)
