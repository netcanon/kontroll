"""A backup whose device answers with nothing records a failed capture and never writes it over the last one (I-5).

WHY. A backup's read can succeed with nothing in it: a raw-SSH command that exits 0 and prints nothing, a CLI `show`
that returns no lines, a RouterOS export with no output, an API that answers 200 with an empty body, an empty
/conf/config.xml. Each of those backups wrote that answer as the capture, over the device's last good one, and
recorded it captured; the history play then committed the loss as a change (T27's runtime read, N9: a raw-SSH
`cmd=true` against a seeded good capture left a 0-byte file and rc 0). Each now records such an answer as a failed
capture, which fails the run at its summary, and does not run its write, so any earlier capture is kept. A read a
dry-run skips (`raw` and the RouterOS command have no check mode, nor has the API's `uri`) gave no answer: the dry-run
previews an empty capture as before and refuses nothing. The reads that run in a dry-run (`cli_command`'s `show`,
`slurp`) are held to their answer there too.

The `when:` conditions are read from the role files and evaluated as Ansible evaluates a conditional: every item must
be a boolean (ansible-core 2.19 refuses any other result) and true; core Jinja with StrictUndefined, `skipped` the
test Ansible registers and `b64decode` its filter. The results are the shapes ansible-core 2.18.19 and 2.19.4
register (measured for this change): raw's check-mode skip holds `skipped`, `changed` and `failed` alone, the RouterOS
command's adds a `msg`, and a `when` skip adds `skip_reason` and `false_condition`.
"""
import base64
import glob
import os

import jinja2
import jinja2.meta
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOTHING = "Record an empty capture as failed (any earlier capture is kept)"
WRITE, RECORD = "Write capture", "Record successful capture"
FAILED = "Record capture failure (reported in summary, not a hard abort)"  # the API's non-200, its own test's


def _b64(text):
    return base64.b64encode(text.encode()).decode()


# role: (the read's register, the write's register, a result with a config in it, an empty one, a blank one)
ROLES = {
    "backend_raw_ssh": ("_cfg", "_cfg_write", {"rc": 0, "stdout": "config system\n", "stderr": ""},
                        {"rc": 0, "stdout": "", "stderr": ""}, {"rc": 0, "stdout": "\n \n", "stderr": ""}),
    "backend_netcommon_cli": ("_cfg", "_cfg_write", {"changed": False, "stdout": "hostname sw1\n!"},
                              {"changed": False, "stdout": ""}, {"changed": False, "stdout": "\r\n"}),
    "routeros": ("ros_export", "_ros_write", {"changed": False, "stdout": ["# by RouterOS 7.16\n/ip address"]},
                 {"changed": False, "stdout": [""]}, {"changed": False, "stdout": ["\n \n"]}),
    "backend_api": ("_api_resp", "_api_write", {"changed": False, "status": 200, "content": "config system global\n"},
                    {"changed": False, "status": 200, "content": ""},
                    {"changed": False, "status": 200, "content": "\n \n"}),
    "opnsense": ("opn_cfg", "_opn_write", {"changed": False, "encoding": "base64", "content": _b64("<opnsense/>\n")},
                 {"changed": False, "encoding": "base64", "content": ""},
                 {"changed": False, "encoding": "base64", "content": _b64(" \n")}),
}
# The check-mode skip of each read a dry-run does not make. `cli_command` runs a `show` command in check mode (and
# fails any other), and `slurp` reads in check mode: those two have none.
SKIPS = {
    "backend_raw_ssh": {"skipped": True, "failed": False, "changed": False},
    "routeros": {"skipped": True, "failed": False, "changed": False,
                 "msg": "remote module (community.routeros.command) does not support check mode"},
    "backend_api": {"changed": False, "skipped": True,
                    "msg": "This action (ansible.builtin.uri) does not support check mode."},
}
WHEN_SKIPPED = {"changed": False, "skipped": True, "skip_reason": "Conditional result was False",
                "false_condition": False}
WRITTEN = {"changed": True, "dest": "/backups/capture.cfg"}
# What each refusal's message may name: never the read's register, which holds the device's config.
MESSAGE_NAMES = {"backend_raw_ssh": {"backend_backup_command"}, "backend_netcommon_cli": {"backend_backup_command"},
                 "routeros": set(), "backend_api": {"backend_api_recipe", "_api_recipe"}, "opnsense": set()}


def _grid():
    """(role, check mode, the read's result, the one task that must run of the refusal, the write and, for the API,
    its non-200 record)."""
    for role, (_, _, good, empty, blank) in sorted(ROLES.items()):
        for check_mode in (False, True):
            yield role, check_mode, good, WRITE
            yield role, check_mode, empty, NOTHING
            yield role, check_mode, blank, NOTHING
            # A read a `when` skipped, or a real run left skipped, gave nothing to write: refused (the API's by its
            # non-200 record, test_backend_api_check_mode.py).
            yield role, check_mode, WHEN_SKIPPED, FAILED if role == "backend_api" else NOTHING
            if role in SKIPS:
                yield role, check_mode, SKIPS[role], (WRITE if check_mode else
                                                      FAILED if role == "backend_api" else NOTHING)


def _walk(tasks):
    for t in tasks or []:
        yield t
        for k in ("block", "rescue", "always"):
            yield from _walk(t.get(k))


def _load(role):
    with open(os.path.join(ROOT, "ansible", "roles", role, "tasks", "backup.yml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _task(role, name):
    found = [t for t in _walk(_load(role)) if t.get("name") == name]
    assert len(found) == 1, (role, name)
    return found[0]


def _env():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.tests["skipped"] = lambda r: r.get("skipped", False) if isinstance(r, dict) else _raise(r)
    env.filters["b64decode"] = lambda s: base64.b64decode(s).decode()
    return env


def _raise(value):
    raise TypeError("the 'skipped' test expects a dictionary, not %r" % (value,))


def _runs(role, name, ctx):
    """Whether the task runs: every item of its `when:` a boolean and true, as Ansible evaluates them."""
    when = _task(role, name)["when"]
    for c in when if isinstance(when, list) else [when]:
        value = _env().compile_expression(c)(**ctx)
        assert isinstance(value, bool), (role, name, c, value)  # ansible-core 2.19 refuses a non-boolean conditional
        if not value:
            return False
    return True


@pytest.mark.parametrize("role, check_mode, result, runs", list(_grid()))
def test_an_answer_with_nothing_in_it_is_refused_and_never_written(role, check_mode, result, runs):
    """Of the refusal, the write and (for the API) its non-200 record, exactly the one named runs: the write for an
    answer with a config in it, and for the read a dry-run skips (it previews an empty capture); the refusal for an
    answer that is empty or only blank lines, in a real run and in a dry-run whose read answered, and for a read
    skipped by a `when` or left skipped in a real run. Guards I-5 (an empty answer written over the last good
    capture and recorded captured), the refusal reaching a dry-run's skipped read (the mandated `--check` would fail
    every raw-SSH and RouterOS host, as before T27), and a dry-run that read the device passing what a real run
    refuses."""
    read = ROLES[role][0]
    tasks = [NOTHING, WRITE] + ([FAILED] if role == "backend_api" else [])
    ctx = {read: result, "ansible_check_mode": check_mode}
    assert [t for t in tasks if _runs(role, t, ctx)] == [runs]


@pytest.mark.parametrize("role", sorted(ROLES))
def test_each_refusal_comes_before_its_write_and_fails_the_run(role):
    """Each refusal sits in the write's own task list, before it, and records a result that starts "FAILED:" with
    `kontroll_backup_failed: true`, which backup-configs.yml's summary fails the run on. Guards the refusal moving
    after the write (as a `failed_when` on the `copy`, it is evaluated after the copy has overwritten the good
    capture) and a refusal that the summary prints but the run passes, so a scheduled backup never says it failed."""
    lists = [t["block"] for t in _walk(_load(role)) if isinstance(t.get("block"), list)] + [_load(role)]
    names = next(([t.get("name") for t in ts] for ts in lists if any(t.get("name") == WRITE for t in ts)))
    assert NOTHING in names and names.index(NOTHING) < names.index(WRITE), names
    facts = _task(role, NOTHING)["ansible.builtin.set_fact"]
    assert facts["kontroll_backup_failed"] is True
    assert facts["kontroll_backup_result"].startswith("FAILED:"), facts


@pytest.mark.parametrize("role", sorted(ROLES))
def test_each_refusal_names_the_read_and_nothing_of_its_answer(role):
    """Each refusal's message reads only the names it may: the command, the recipe and its path, or none. Guards the
    message printing the answer (the read's register is `no_log` because it holds the device's configuration) or the
    API's token: the summary prints this message, and it is not `no_log`."""
    message = _task(role, NOTHING)["ansible.builtin.set_fact"]["kontroll_backup_result"]
    names = jinja2.meta.find_undeclared_variables(jinja2.Environment().parse(message))
    assert names == MESSAGE_NAMES[role], names
    assert not names & {ROLES[role][0], "_api_token", "_api_headers"}


@pytest.mark.parametrize("role", sorted(ROLES))
def test_each_capture_is_recorded_where_its_write_ran_and_only_there(role):
    """The write registers its result, and "Record successful capture" reads that and nothing else: it runs for a
    write that ran (in a dry-run, its preview) and not for one its `when` skipped. Guards "captured" following a copy
    of the write's condition, or none: with the refusal's `when` wrong, the summary would say captured for a capture
    never written (I-6's runtime read, N1, on the API backend)."""
    write = ROLES[role][1]
    assert _task(role, WRITE).get("register") == write
    when = _task(role, RECORD)["when"]
    names = set().union(*(jinja2.meta.find_undeclared_variables(_env().parse("{{ %s }}" % c))
                          for c in (when if isinstance(when, list) else [when])))
    assert names == {write}, names
    for check_mode in (True, False):
        assert _runs(role, RECORD, {write: WRITTEN, "ansible_check_mode": check_mode}) is True
        assert _runs(role, RECORD, {write: WHEN_SKIPPED, "ansible_check_mode": check_mode}) is False


# Every other task that writes a capture under config_backup_dir, and why an empty one is the truth there.
EXEMPT = {
    ("docker_host", "Write capture"): "header",
    ("proxmox", "Write capture"): "header",
    ("logging_file_tail_ssh", "Write the authorized_keys capture"): "file",
    ("logging_journald_remote", "Write the journald.conf capture"): "file",
    ("openwrt_discovery_key", "Write the authorized_keys capture"): "file",
}


def _capture_writes():
    """{(role, task name): (the task, the role's tasks)} for every task of a role's task files that writes a `dest`
    under `config_backup_dir`."""
    out = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "ansible", "roles", "*", "tasks", "*.yml"))):
        role = os.path.basename(os.path.dirname(os.path.dirname(path)))
        with open(path, encoding="utf-8") as fh:
            tasks = list(_walk(yaml.safe_load(fh)))
        for t in tasks:
            if isinstance(t, dict) and any(isinstance(v, dict) and "config_backup_dir" in str(v.get("dest", ""))
                                           for v in t.values()):
                out[(role, t.get("name"))] = (t, tasks)
    return out


def _source(write, tasks):
    """The task whose register the write's content reads."""
    content = next(v["content"] for v in write.values() if isinstance(v, dict) and "content" in v)
    names = jinja2.meta.find_undeclared_variables(_env().parse(content))
    found = [t for t in tasks if t.get("register") in names]
    assert len(found) == 1, (write.get("name"), names)
    return found[0]


def test_every_capture_write_refuses_an_empty_answer_or_is_exempt_for_a_reason_it_holds():
    """Every role task that writes a capture under `config_backup_dir` is one of the five above, which refuse an
    empty answer, or is exempt, and its reason is checked, not just stated: a header read (Docker's and Proxmox's
    `shell` prints a `### ` section header before each read, so its capture is never empty), or a file read (the
    logging and OpenWrt key roles `slurp` one file before they change it, as its rollback copy, and an empty file
    captured is the file as it was). Guards a new backup writing an empty answer over a good capture unseen, and an
    exemption whose reason no longer holds (a Proxmox read that loses its headers can answer nothing)."""
    writes = _capture_writes()
    refusing = {(role, WRITE) for role in ROLES}
    assert set(writes) == refusing | set(EXEMPT), sorted(set(writes) ^ (refusing | set(EXEMPT)))
    for key, reason in EXEMPT.items():
        source = _source(*writes[key])
        if reason == "header":
            script = source["ansible.builtin.shell"]
            assert script.lstrip().startswith('echo "### '), (key, script)
        else:
            assert "ansible.builtin.slurp" in source, (key, source.get("name"))
