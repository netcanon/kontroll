"""A backup whose device answers with nothing records a failed capture and never writes it over the last one (I-5).

WHY. A backup's read can succeed with nothing in it: a raw-SSH command that exits 0 and prints nothing, a CLI `show`
that returns no lines, a RouterOS export with no output, an API that answers 200 with an empty body, an empty
/conf/config.xml, a Proxmox node or Docker host that answers none of its script's reads (the script still prints the
`### ` header it puts before each). Each of those backups wrote that answer as the capture, over the device's last
good one, and recorded it captured; the history play then committed the loss as a change (T27's runtime read, N9: a
raw-SSH `cmd=true` against a seeded good capture left a 0-byte file and rc 0). Each now records such an answer as a
failed capture, which fails the run at its summary, and does not run its write, so any earlier capture is kept. A
read a dry-run skips (`raw`, `shell`, the RouterOS command and the API's `uri` do not run under `--check`) gave no
answer: the dry-run previews an empty capture as before and refuses nothing. The reads that run in a dry-run
(`cli_command`'s `show`, `slurp`) are held to their answer there too. An unreachable host's read (the play tolerates
one) gave no answer either: it is not refused, and the summary says the host was not run. A Docker read that
exits non-zero (docker or the module's python missing, its daemon down or refusing a read, no access to its socket)
records its exit code as a failed capture instead, and nothing is written (I-62).

The `when:` conditions are read from the role files and evaluated as Ansible evaluates a conditional: every item must
be a boolean (ansible-core 2.19 refuses any other result) and true; core Jinja with StrictUndefined, `skipped`,
`unreachable` and `match` the tests Ansible has, and `b64decode` its filter. The results are the shapes ansible-core
2.18.19 and 2.19.4 register (measured for I-5, and `shell`'s for I-10): raw's check-mode skip holds `skipped`,
`changed` and `failed` alone, the RouterOS command's adds a `msg`, `shell`'s an empty output, an `rc` of 0 and a
`msg`, a `when` skip adds `skip_reason` and `false_condition`, and an unreachable host's read (the play's
`ignore_unreachable`) registers `unreachable` with no output.
"""
import base64
import glob
import os
import re

import jinja2
import jinja2.meta
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOTHING = "Record an empty capture as failed (any earlier capture is kept)"
WRITE, RECORD = "Write capture", "Record successful capture"
FAILED = "Record capture failure (reported in summary, not a hard abort)"  # the API's non-200; Docker's failed read
RECORDS_FAILURE = ("backend_api", "docker_host")


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _script(role):
    """The role's capture script: the text of the one `shell` task of its backup.yml."""
    with open(os.path.join(ROOT, "ansible", "roles", role, "tasks", "backup.yml"), encoding="utf-8") as fh:
        tasks = yaml.safe_load(fh)
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "ansible.builtin.shell" in node:
                found.append(node["ansible.builtin.shell"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(tasks)
    assert len(found) == 1 and isinstance(found[0], str), (role, found)
    return found[0]


def _headers(role):
    """The `### ` section headers the role's capture script echoes before each read, in order: its whole answer when
    no read answers (I-10). `test_every_line_of_the_capture_script_that_starts_with_an_echo_is_a_header` holds that
    none is echoed in another form."""
    found = re.findall(r'^\s*echo "(### [^"]*)"', _script(role), re.M)
    assert len(found) >= 2, (role, found)
    return found


def _shell(*lines):
    """A `shell` read's result, its output as the module registers it (the last newline stripped)."""
    return {"changed": False, "rc": 0, "stdout": "\n".join(lines), "stderr": ""}


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
# The two whose script prints a header before each read: a good answer has a line besides them (a guest's .conf
# separator alone counts), the empty one is the headers alone, the blank one adds blank lines between them.
_PVE, _DKR = _headers("proxmox"), _headers("docker_host")
ROLES.update({
    "proxmox": ("pve_cfg", "_pve_write", _shell(_PVE[0], _PVE[1], "auto lo", *_PVE[2:]), _shell(*_PVE),
                _shell(*(h + "\n \t" for h in _PVE))),
    "docker_host": ("dkr", "_dkr_write", _shell(*_DKR[:2], "CONTAINER ID   IMAGE   NAMES", *_DKR[2:]), _shell(*_DKR),
                    _shell(*(h + "\n \t" for h in _DKR))),
})
# The check-mode skip of each read a dry-run does not make. `cli_command` runs a `show` command in check mode (and
# fails any other), and `slurp` reads in check mode: those two have none.
SKIPS = {
    "backend_raw_ssh": {"skipped": True, "failed": False, "changed": False},
    "routeros": {"skipped": True, "failed": False, "changed": False,
                 "msg": "remote module (community.routeros.command) does not support check mode"},
    "backend_api": {"changed": False, "skipped": True,
                    "msg": "This action (ansible.builtin.uri) does not support check mode."},
}
SKIPS.update({role: {"changed": False, "skipped": True, "rc": 0, "stdout": "", "stderr": "",
                     "msg": "Command would have run if not in check mode"} for role in ("proxmox", "docker_host")})
WHEN_SKIPPED = {"changed": False, "skipped": True, "skip_reason": "Conditional result was False",
                "false_condition": False}
WRITTEN = {"changed": True, "dest": "/backups/capture.cfg"}
UNCHANGED = {"changed": False, "dest": "/backups/capture.cfg"}   # the nightly case: the config did not change
UNREACHABLE = {"changed": False, "unreachable": True, "msg": "Failed to connect to the host via ssh"}
# Each role's one read: the module whose answer the refusal and the write judge.
READ_MODULE = {"backend_raw_ssh": "ansible.builtin.raw", "backend_netcommon_cli": "ansible.netcommon.cli_command",
               "routeros": "community.routeros.command", "backend_api": "ansible.builtin.uri",
               "opnsense": "ansible.builtin.slurp", "proxmox": "ansible.builtin.shell",
               "docker_host": "ansible.builtin.shell"}
# What each refusal's message may name: never the read's register, which holds the device's config.
MESSAGE_NAMES = {"backend_raw_ssh": {"backend_backup_command"}, "backend_netcommon_cli": {"backend_backup_command"},
                 "routeros": set(), "backend_api": {"backend_api_recipe", "_api_recipe"}, "opnsense": set(),
                 "proxmox": set(), "docker_host": set()}


def _grid():
    """(role, check mode, the read's result, the one task that must run of the refusal, the write and, for the API,
    its non-200 record; None where none may)."""
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
            # An unreachable host's read gave no answer: nothing runs, and the summary says the host was not run. The
            # API's pull runs on the control node, which is never unreachable.
            if role != "backend_api":
                yield role, check_mode, UNREACHABLE, None
            # A Docker read that failed (the script stops at the read that fails, `set -e`) is recorded by its exit
            # code, whatever it printed before: the client's half of `docker version` without a daemon, or a header.
            if role == "docker_host":
                for rc, lines in ((1, (_DKR[0], "Client:", " Version:           27.3.1")), (127, (_DKR[0],))):
                    yield role, check_mode, dict(_shell(*lines), rc=rc, failed=False, failed_when_result=False), FAILED
                # A failed result with no exit code at all: the record alone, not the empty refusal too (the I-62
                # read A's F-2: both ran, and the refusal's words won).
                yield role, check_mode, {"changed": False, "failed": False, "failed_when_result": False,
                                         "msg": "MODULE FAILURE"}, FAILED


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
    env.tests["skipped"] = lambda r: bool(_result(r, "skipped").get("skipped", False))
    env.tests["unreachable"] = lambda r: bool(_result(r, "unreachable").get("unreachable", False))
    env.filters["b64decode"] = lambda s: base64.b64decode(s).decode()
    env.tests["match"] = lambda value, pattern="": re.match(pattern, value) is not None   # Ansible's own
    return env


def _result(value, test):
    """A registered result, as Ansible's result tests require one (they raise on anything else)."""
    if not isinstance(value, dict):
        raise TypeError("the '%s' test expects a dictionary, not %r" % (test, value))
    return value


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
    refuses. Neither runs for an unreachable host's read: it gave no answer, and the summary says the host was not
    run (I-5's runtime read, S-1: the refusal reported it as a read that answered nothing). For Docker, a read that
    exits non-zero runs the failure record alone, by its exit code, whatever it printed first: a daemon that is down
    leaves the client's half of `docker version`, which a write would have put over the last good manifest (I-62)."""
    read = ROLES[role][0]
    tasks = [NOTHING, WRITE] + ([FAILED] if role in RECORDS_FAILURE else [])
    ctx = {read: result, "ansible_check_mode": check_mode}
    assert [t for t in tasks if _runs(role, t, ctx)] == ([runs] if runs else [])


@pytest.mark.parametrize("role", sorted(ROLES))
def test_each_refusal_comes_before_its_write_and_fails_the_run(role):
    """Each refusal sits in the write's own task list, before it, and records a result that starts "FAILED:" with
    `kontroll_backup_failed: true`, which backup-configs.yml's summary fails the run on. Guards the refusal moving
    after the write (as a `failed_when` on the `copy`, it is evaluated after the copy has overwritten the good
    capture) and a refusal that the summary prints but the run passes, so a scheduled backup never says it failed.
    The read it judges is the role's one read: exactly one task sets the register, and it is the read module. Guards
    a task between them that registers the same name (its answer would be judged and written instead) and a renamed
    register (every capture refused; I-5's tests-and-docs read, N2)."""
    read = ROLES[role][0]
    setters = [t for t in _walk(_load(role)) if t.get("register") == read
               or read in (t.get("ansible.builtin.set_fact") or {})]
    assert len(setters) == 1 and READ_MODULE[role] in setters[0], (role, [t.get("name") for t in setters])
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
    write that ran (in a dry-run, its preview), one that found the capture unchanged included, and not for one its
    `when` skipped; and the write neither ignores its errors nor sets its own failure, so a write that failed fails
    its task there (the capture play's rescue then records the host FAILED, naming the task: I-12). Guards "captured"
    following a copy of the write's condition, or none: with the refusal's `when` wrong, the summary would say
    captured for a capture never written (I-6's runtime read, N1, on the API backend); "captured" said only of a
    changed capture, so a nightly run of an unchanged config says the host was not run (I-5's tests-and-docs read,
    N1); and a failed write recorded as captured."""
    write = ROLES[role][1]
    assert _task(role, WRITE).get("register") == write
    assert not {"ignore_errors", "failed_when"} & set(_task(role, WRITE)), _task(role, WRITE)
    when = _task(role, RECORD)["when"]
    names = set().union(*(jinja2.meta.find_undeclared_variables(_env().parse("{{ %s }}" % c))
                          for c in (when if isinstance(when, list) else [when])))
    assert names == {write}, names
    for check_mode in (True, False):
        assert _runs(role, RECORD, {write: WRITTEN, "ansible_check_mode": check_mode}) is True
        assert _runs(role, RECORD, {write: UNCHANGED, "ansible_check_mode": check_mode}) is True
        assert _runs(role, RECORD, {write: WHEN_SKIPPED, "ansible_check_mode": check_mode}) is False


@pytest.mark.parametrize("role", ["proxmox", "docker_host"])
def test_every_line_of_the_capture_script_that_starts_with_an_echo_is_a_header(role):
    """Every line of the Proxmox and Docker capture scripts that starts with `echo` or `printf` is an
    `echo "### …"` header, the one form the rows above build their answers from and the role's refusal rejects
    (`reject('match', '### ')`); and there is one header per such line.

    WHY: the rows' empty answer is built from the headers this file can read. A header renamed out of that form
    (`echo "## images"`) or a section added in another (`echo "== volumes"`) left the rows, so the empty answer was
    built without it and still refused, while the role, which rejects only `### ` lines, kept that line and wrote
    the headers as the capture again: the I-10 fault, back (mutants that survived on both cores, the I-10 reads). A
    guest's separator echoed inside a loop (`echo "--- $f"`) does not start its line, and it is config: kept."""
    script = _script(role)
    leading = [line.strip() for line in script.splitlines() if re.match(r"\s*(echo|printf)\b", line)]
    assert leading and all(re.match(r'echo "### [^"]*"', line) for line in leading), (role, leading)
    assert len(leading) == len(_headers(role)), (role, leading)


# Every other task that writes a capture under config_backup_dir, and why an empty one is the truth there.
EXEMPT = {
    ("logging_file_tail_ssh", "Write the authorized_keys capture"): "file",
    ("logging_journald_remote", "Write the journald.conf capture"): "file",
    ("openwrt_discovery_key", "Write the authorized_keys capture"): "file",
}


def _strings(value):
    """Every string in a task's arguments, however deep."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def _capture_writes():
    """{(role, task name): (the task, the role's tasks)} for every task of a role's task files (.yml or .yaml), at any
    depth below tasks/, whose arguments name `config_backup_dir` or a fact a set_fact in the same file builds from
    it: any module, any argument (a `dest`, a `path`, a `dir_path`, a shell redirect). The set_facts themselves are
    left out, and so is the `file: state=directory` that makes the directory. Two writes of one name in a role fail
    here: keyed by name, the second would hide the first from every row (the I-13 reads' S1)."""
    out = {}
    pattern = os.path.join(ROOT, "ansible", "roles", "*", "tasks", "**", "*.y*ml")
    for path in sorted(glob.glob(pattern, recursive=True)):
        role = os.path.relpath(path, os.path.join(ROOT, "ansible", "roles")).split(os.sep)[0]
        with open(path, encoding="utf-8") as fh:
            tasks = [t for t in _walk(yaml.safe_load(fh)) if isinstance(t, dict)]
        facts = {k for t in tasks for k, v in (t.get("ansible.builtin.set_fact") or {}).items()
                 if "config_backup_dir" in str(v)}
        for t in tasks:
            if "block" in t or "ansible.builtin.set_fact" in t:
                continue
            if (t.get("ansible.builtin.file") or {}).get("state") == "directory":
                continue
            args = {k: v for k, v in t.items() if k not in ("name", "when", "register", "delegate_to", "no_log")}
            if any("config_backup_dir" in s or any(f in s for f in facts) for s in _strings(args)):
                assert (role, t.get("name")) not in out, ("two capture writes share a name", role, t.get("name"))
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
    """Every role task whose arguments name `config_backup_dir` (by any module or argument, in any task file of the
    role, or through a fact built from it) is one of the writes above, which refuse an empty answer, or is exempt,
    and its reason is checked, not just stated: a file read (the logging and OpenWrt key roles `slurp` one file
    before they change it, as its rollback copy, and an empty file captured is the file as it was). Guards a new
    backup writing an empty answer over a good capture unseen, by a module, an argument, a file or a fact the net did
    not look at (I-5's tests-and-docs read, S1: a `cli_backup`, a `lineinfile`, a shell redirect, a nested task file
    and a fact-built `dest` all passed), and an exemption whose reason no longer holds; and a backup spared because
    its script's headers make the capture non-empty (I-10: a Proxmox node that answered none of its reads was written
    captured, its headers alone)."""
    writes = _capture_writes()
    refusing = {(role, WRITE) for role in ROLES}
    assert set(writes) == refusing | set(EXEMPT), sorted(set(writes) ^ (refusing | set(EXEMPT)))
    for key, reason in EXEMPT.items():
        source = _source(*writes[key])
        assert reason == "file" and "ansible.builtin.slurp" in source, (key, reason, source.get("name"))


def test_a_docker_read_that_fails_is_recorded_by_its_exit_code_alone_before_the_write():
    """Docker's read carries `failed_when: false` and no `ignore_errors`, and its failure record sits before the
    write, records a result that starts "FAILED:" with `kontroll_backup_failed: true`, and sets nothing but the result
    and the flag, and reads nothing of the read but its `rc`. Guards I-62: the read failed the host at its own task (a
    host without the compose plugin, whose `docker compose ls` was the script's last command, or a daemon that is
    down), so the summary printed no line for it and a capture that could be taken was not; a record that read the
    answer would print a container's command line, where a credential can show (the read is `no_log`, the summary's
    line is not), and so would a fact of the record's own that kept the answer, which `-v` prints (the I-62 read B's
    N-3)."""
    read = _task("docker_host", "Gather container + stack manifest (read-only, brace-free output)")
    assert read.get("register") == "dkr" and read.get("failed_when") is False and "ignore_errors" not in read, read
    names = [t.get("name") for t in _walk(_load("docker_host"))]
    assert names.index(FAILED) < names.index(WRITE), names
    facts = _task("docker_host", FAILED)["ansible.builtin.set_fact"]
    assert set(facts) == {"kontroll_backup_result", "kontroll_backup_failed"}, facts
    assert facts["kontroll_backup_failed"] is True and facts["kontroll_backup_result"].startswith("FAILED:"), facts
    tree = jinja2.Environment().parse(facts["kontroll_backup_result"])
    assert jinja2.meta.find_undeclared_variables(tree) == {"dkr"}
    uses = [n for n in tree.find_all(jinja2.nodes.Name) if n.name == "dkr"]
    reads = [n for n in tree.find_all(jinja2.nodes.Getattr) if isinstance(n.node, jinja2.nodes.Name)
             and n.node.name == "dkr"]
    assert len(uses) == len(reads) and {n.attr for n in reads} == {"rc"}, [n.attr for n in reads]
