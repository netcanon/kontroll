"""The runner image's no-log censor step, and the rules of the script it runs (I-75).

WHY. ansible-core 2.19 and later keep `exception` in a censored `no_log: true` result, and the default callback prints
its message, so a no_log task that fails prints what it was given in its error message (CVE-2026-123036, upstream
ansible/ansible#85696; no fixed release). The runner image's base ships such a core, the core of every Semaphore job
and of the API's and the onboard GUI's Ansible runs. docker/semaphore-runner/no_log_censor.py drops `exception` from
the censor's preserved keys in the image's own ansible-core, refuses a file it does not know, then checks the core. No
pull-request job builds an image, so these rows read the Dockerfile and call the script's own functions: the step is
in the image, runs as root with the venv's Python, fails the build when it fails, and is not undone by a later step;
the rewrite drops `exception` alone, under a notice that the file was modified, and refuses every other shape; and the
check, run against a stand-in for ansible-playbook, reports a marker printed on any channel, a run that did not fail,
a missing notice and a control that printed nothing. tests/integration/test_no_log_censor_runs.py runs the script on a
copy of the test run's own ansible-core.
"""
import importlib.util
import os
import re
import subprocess
import types

import pytest
from _build_read import commands, gate_faults, instructions

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCKERFILE = os.path.join(ROOT, "docker", "semaphore-runner", "Dockerfile")
SCRIPT = os.path.join(ROOT, "docker", "semaphore-runner", "no_log_censor.py")
COPY = "docker/semaphore-runner/no_log_censor.py /tmp/k/no_log_censor.py"
VENV = 'VENV="$(dirname "$(ls /opt/semaphore/apps/ansible/*/venv/bin/python)")"'
CALL = '"$VENV/python" /tmp/k/no_log_censor.py'
# the line as ansible-core 2.19 and 2.20 ship it (ansible/executor/task_result.py)
SHIPPED = "_PRESERVE = {'attempts', 'changed', 'retries', '_ansible_no_log', 'exception', 'warnings', 'deprecations'}"
# what a later step would run to put an unpatched ansible-core back: pip in any form, or the package's name
REINSTALL = re.compile(r"\bpip[0-9.]*\b|ansible[-_]core", re.I)


def _script():
    """The script, loaded as a module (its `main` does not run)."""
    spec = importlib.util.spec_from_file_location("no_log_censor", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_runner_image_runs_the_censor_as_root_with_its_ansible_venv():
    """The runner Dockerfile COPYs the script, then one RUN calls it with the base's ansible venv Python (the venv
    every Semaphore job runs), as root (`USER root` is the last USER before it; the base runs as 1001, which may not
    own the file the script rewrites), in the image's only stage, before the final `USER 1001`; the call fails the
    build when the script fails (the same reading the pin checks get: nothing after it but `&&`, no `||`, no `;`), and
    the RUN does nothing else; and no RUN after it runs pip or names ansible-core. Guards the step dropped, moved after
    the USER switch, run by another Python (a system python3 changes some other ansible-core, or none), made unable to
    fail the build, or undone by a later step that installs the core again (the image would ship it unpatched with
    the check passed)."""
    steps = instructions(open(DOCKERFILE, encoding="utf-8").read())
    assert [k for k, _ in steps].count("FROM") == 1, "the runner Dockerfile is one stage"
    at = steps.index(("COPY", COPY))
    run = next(i for i in range(at + 1, len(steps)) if steps[i][0] == "RUN")
    cmds = commands(steps[run][1])
    assert [c for c, _, _ in cmds] == [VENV, CALL, "rm -rf /tmp/k"], cmds
    assert [op for _, _, op in cmds] == ["&&", "&&", ""], cmds
    assert gate_faults("the no-log censor", cmds, 1) == []
    users = [(i, a) for i, (k, a) in enumerate(steps) if k == "USER"]
    assert [a for i, a in users if i < run][-1] == "root", users
    assert users[-1][0] > run and users[-1][1] == "1001", users
    later = [a for k, a in steps[run + 1:] if k in ("RUN", "ONBUILD")]
    assert not [a for a in later if REINSTALL.search(a)], later


def test_the_censor_drops_exception_alone_and_keeps_the_rest_in_order():
    """The shipped line loses 'exception' and nothing else, the other keys in their order, under one line of notice
    that the file was modified (ansible-core is GPL-3.0, whose section 5a asks for one), and the text around the line
    is left as it was; run on its own output it writes nothing (`dropped` False), as it does for 2.18's tuple (which
    holds no 'exception'); a tuple that holds it loses it and stays a tuple. Guards a rewrite that drops another key
    the callback reads (`changed`, `_ansible_no_log`), reorders or retypes the set, touches the rest of the file,
    ships the modified file with no notice, or writes a notice the next run reads as a second `_PRESERVE` line."""
    mod = _script()
    assert not mod.NOTICE.startswith("_PRESERVE") and "\n" not in mod.NOTICE and mod.NOTICE.startswith("# Modified")
    source = "import x\n\n" + SHIPPED + "\n_SUB_PRESERVE = {'a': {'b'}}\n"
    new, dropped = mod.drop_exception(source)
    assert dropped is True
    assert new == ("import x\n\n" + mod.NOTICE + "\n_PRESERVE = {'attempts', 'changed', 'retries', '_ansible_no_log', "
                   "'warnings', 'deprecations'}\n_SUB_PRESERVE = {'a': {'b'}}\n")
    assert mod.drop_exception(new) == (new, False)
    old = "_PRESERVE = ('attempts', 'changed', 'retries')\n"
    assert mod.drop_exception(old) == (old, False)
    new, dropped = mod.drop_exception("_PRESERVE = ('changed', 'exception')\n")
    assert dropped is True and new == mod.NOTICE + "\n_PRESERVE = ('changed',)\n"


@pytest.mark.parametrize("source", [
    "_IGNORE = ('failed',)\n",
    SHIPPED + "\n" + SHIPPED + "\n",
    "_PRESERVE = frozenset({'changed', 'exception'})\n",
    "_PRESERVE = {'changed', EXCEPTION}\n",
    "_PRESERVE = {'changed': 1, 'exception': 2}\n",
    "_PRESERVE = {\n    'changed',\n    'exception',\n}\n",
    "_PRESERVE: set[str] = {'changed', 'exception'}\n",
    "class A:\n    _PRESERVE = {'changed', 'exception'}\n",
    "_PRESERVE = {'changed', 'exception'} | _MORE\n",
    "_PRESERVE = ()\n",
    "_PRESERVE = {'exception'}\n",
    "_PRESERVE = ('exception',)\n",
], ids=["no line", "two lines", "frozenset call", "a name", "a dict", "over three lines", "annotated", "in a class",
        "a union", "empty", "exception alone in a set", "exception alone in a tuple"])
def test_the_censor_refuses_a_shape_it_does_not_know(source):
    """Any other shape of the preserved keys raises Refused (the script then writes nothing and exits 1): no
    `_PRESERVE = ` line at the top level, two, one that is not a set or tuple literal of strings on one line, or one
    holding 'exception' alone (dropping it would leave `{}`, a dict, or `()`). Guards a script that guesses: a base
    whose core moved the censor (a new file, a frozenset, the keys built up elsewhere) would be left unpatched while the
    build passed, or patched wrong; refused, the build stops and the new core is read before it ships."""
    mod = _script()
    with pytest.raises(mod.Refused):
        mod.drop_exception(source)


def test_a_refusal_for_no_line_names_where_each_core_keeps_the_censor():
    """The refusal for a file with no `_PRESERVE = ` line names both places a core keeps its censor: `_PRESERVE` in
    ansible/executor/task_result.py (2.19, 2.20) and `PRESERVE` in ansible/_internal/_task.py (2.21 and later). Guards
    the message a base bump to 2.21 meets sending its reader to a file that no longer holds the censor."""
    mod = _script()
    with pytest.raises(mod.Refused) as e:
        mod.drop_exception("_IGNORE = ('failed',)\n")
    assert "found 0" in str(e.value)
    assert "ansible/executor/task_result.py" in str(e.value) and "ansible/_internal/_task.py" in str(e.value)


def test_the_check_holds_a_control_for_every_censored_case():
    """The script's check runs a no_log `fail` given the marker in its message and a no_log `command` whose argv names
    a path holding it (the two kinds 2.19 prints: an action's message, a module's), each templating `{{ marker }}`,
    and the same `fail` without no_log, which must print it. Guards a check left with no control or with only one kind
    of failure, and a censored case that is given no marker (it could print nothing whatever the core did)."""
    cases = _script().CASES
    assert {name for name, (_, censored) in cases.items() if censored} == {"no_log fail", "no_log command"}
    assert [name for name, (_, censored) in cases.items() if not censored] == ["fail"]
    assert cases["fail"][0] == cases["no_log fail"][0].split("\n", 1)[1].lstrip()
    assert all("{{ marker }}" in task for task, _ in cases.values()), cases


CENSORED_LINE = ('fatal: [localhost]: FAILED! => {"censored": "the output has been hidden due to the fact that '
                 "'no_log: true' was specified for this result\"}\n")


def _core(fault):
    """A stand-in for `ansible-playbook` under the check: it reads the marker from the run's `-e @` vars file, writes
    the log at the run's ANSIBLE_LOG_PATH (a KeyError where the check set none), and answers as a patched core does
    (each run fails, rc 2; a no_log one prints its censored line only, the control its message), but for `fault`."""
    def run(argv, cwd=None, env=None, **_):
        extra = argv[argv.index("-e") + 1]
        marker = open(extra[1:], encoding="utf-8").read().split(":", 1)[1].strip()
        book = open(argv[-1], encoding="utf-8").read()
        assert marker not in book, book
        rc, err = 2, ""
        if "no_log: true" not in book:
            out = 'fatal: [localhost]: FAILED! => {"changed": false, "msg": "%s"}\n' % marker
            if fault == "silent control":
                out = 'fatal: [localhost]: FAILED! => {"changed": false}\n'
            log = out
        else:
            out = log = CENSORED_LINE
            if fault == "stdout":
                out += marker + "\n"
            elif fault == "stderr":
                err = "[ERROR]: Task failed: Action failed: %s\n" % marker
            elif fault == "log":
                log += "[ERROR]: Task failed: Action failed: %s\n" % marker
            elif fault == "rc 0":
                rc = 0
            elif fault == "no FAILED!":
                out = log = CENSORED_LINE.replace("FAILED! => ", "")
            elif fault == "no notice":
                out = log = 'fatal: [localhost]: FAILED! => {"changed": false}\n'
        with open(env["ANSIBLE_LOG_PATH"], "w", encoding="utf-8") as fh:
            fh.write(log)
        return subprocess.CompletedProcess(argv, rc, out, err)
    return run


BOTH = ("no_log fail", "no_log command")


@pytest.mark.parametrize("fault, reasons", [
    (None, []),
    ("stdout", ["%s: the marker it was given was printed" % n for n in BOTH]),
    ("stderr", ["%s: the marker it was given was printed" % n for n in BOTH]),
    ("log", ["%s: the marker it was given was printed" % n for n in BOTH]),
    ("rc 0", ["%s: expected a failed run (rc 2, a FAILED! line), got rc 0" % n for n in BOTH]),
    ("no FAILED!", ["%s: expected a failed run (rc 2, a FAILED! line), got rc 2" % n for n in BOTH]),
    ("no notice", ["%s: no line says its output is hidden" % n for n in BOTH]),
    ("silent control", ["fail: an ordinary failure no longer prints its message (the check sees nothing)"]),
], ids=["a patched core", "marker on stdout", "marker on stderr", "marker in the log only", "a run that passes",
        "no FAILED! line", "no hidden-output notice", "a control that prints nothing"])
def test_the_check_reports_every_way_a_core_is_not_safe(monkeypatch, fault, reasons):
    """The check, run against a stand-in core: a patched core gives no reason; a marker printed only on stdout, only
    on stderr, or only in the ansible log, a no_log run that passes or prints no `FAILED!`, one that prints no notice
    that its output is hidden, and a control that prints nothing are each reported, for the case it touched, in the
    check's words. Guards the check's own branches, which the integration rows reach only through a marker on stdout:
    a check that reads one channel, sets no log path, ignores the rc or the notice, or passes a core that prints no
    failure at all would let a build pass on a core the check exists to stop."""
    mod = _script()
    monkeypatch.setattr(mod, "subprocess", types.SimpleNamespace(run=_core(fault)))
    assert mod.check() == reasons
