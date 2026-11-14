"""CI runs the offline suite on several worker processes (pytest-xdist), from one count the workflow sets.

In one process the suite held one core of a runner while the others idled: the rows that run ansible-playbook took
each pytest leg and the validate job's pytest step from minutes to most of an hour. `.github/workflows/ci.yml` sets
`KONTROLL_PYTEST_WORKERS` once, at the workflow's top; the tests job's offline pytest step passes it to `-n`, with
`--dist worksteal`, and tests/validate.sh's pytest step does the same when the variable is set, and runs the suite in
one process when it is not (a local run, the gate's own image, which has no xdist); validate's refuse-local-runtime
and key-render-runtime steps take the same arguments, its as-root step none. tests/README.md "Parallel runs" says
what a row must not do to stay correct under it.
"""
import os
import re
import shlex
import shutil
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VAR = "KONTROLL_PYTEST_WORKERS"
OFFLINE = 'pytest -m "not e2e and not slow"'


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _workflow():
    return yaml.safe_load(_read(".github/workflows/ci.yml"))


def test_the_workflow_sets_one_worker_count_that_both_suite_runs_read():
    """ci.yml's top-level env sets KONTROLL_PYTEST_WORKERS to a positive count; no job or step sets it again; the
    tests job's offline pytest step passes it to `-n` as one quoted word, with `--dist worksteal`; the validate job
    runs tests/validate.sh, which reads it; and tests/requirements-dev.txt, which both jobs install, names
    pytest-xdist. Guards the suite going back to one core in either job (the variable or the `-n` dropped), the two
    jobs drifting to different counts (a job or step env that sets it again), a `-n` with no xdist installed, which
    fails every leg at its first argument, and xdist's default scheduler, `load`, under which the rows of
    tests/integration, collected first and most of the suite's time, landed on one worker (measured: one worker busy
    887 s of a 913 s run, the other 241 s; `worksteal`, 591 s and 579 s of 627 s)."""
    wf = _workflow()
    count = str((wf.get("env") or {}).get(VAR, ""))
    assert re.fullmatch(r"[1-9][0-9]*", count), "ci.yml's top-level env must set %s to a positive count" % VAR
    again = ["%s: job env" % name for name, job in wf["jobs"].items() if VAR in (job.get("env") or {})]
    again += ["%s: step %r" % (name, s.get("name")) for name, job in wf["jobs"].items()
              for s in job.get("steps", []) if VAR in (s.get("env") or {})]
    assert again == [], "%s is set again below the workflow's top: %s" % (VAR, again)
    offline = [s for s in wf["jobs"]["tests"]["steps"] if str(s.get("run", "")).startswith(OFFLINE)]
    assert len(offline) == 1, "the tests job runs the offline suite in one step"
    words = shlex.split(offline[0]["run"])
    assert "-n" in words and words[words.index("-n") + 1] == "$" + VAR, \
        "the tests job's offline pytest step must pass -n \"$%s\"" % VAR
    assert ' -n "$%s"' % VAR in offline[0]["run"], "the count is passed as one quoted word"
    assert "--dist" in words and words[words.index("--dist") + 1] == "worksteal", \
        "the tests job's offline pytest step must pass --dist worksteal (load leaves one worker most of the suite)"
    assert any(str(s.get("run", "")).strip() == "bash tests/validate.sh --strict"
               for s in wf["jobs"]["validate"]["steps"]), "the validate job runs tests/validate.sh --strict"
    reqs = [line.split("#")[0].strip() for line in _read("tests/requirements-dev.txt").splitlines()]
    assert any(re.match(r"pytest-xdist([<>=!~ ]|$)", r) for r in reqs), "tests/requirements-dev.txt names pytest-xdist"


def _validate_step_lines():
    """tests/validate.sh's lines that build the pytest step's worker arguments and run the step."""
    lines = _read("tests/validate.sh").splitlines()
    picked = [line.strip() for line in lines
              if re.match(r"\s*xdist=\(", line) or re.match(r'\s*\[ -n "\$\{%s:-\}" \]' % VAR, line)
              or re.match(r"\s*step pytest 0 ", line)]
    assert len(picked) == 3, "validate.sh's worker lines and its pytest step: %r" % picked
    return picked


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash, which tests/validate.sh runs under")
@pytest.mark.parametrize("value, expect", [
    (None, None),
    ("", None),
    ("2", ["-n", "2", "--dist", "worksteal"]),
    ("2 3", ["-n", "2 3", "--dist", "worksteal"]),
    ("*", ["-n", "*", "--dist", "worksteal"]),
])
def test_the_gates_pytest_step_passes_the_count_only_where_it_is_set(value, expect):
    """tests/validate.sh's own lines, run under bash with a stand-in `step` that prints each argument it is given:
    with KONTROLL_PYTEST_WORKERS unset or empty the step runs pytest with no `-n` (one process, as a local run or the
    gate's image, which has no xdist, needs), and with a count it ends `-n <count> --dist worksteal`, the count one
    argument as given, never split or globbed (a value xdist refuses must reach xdist and fail the step, not turn into
    other arguments). Guards the gate ignoring the variable (validate stays on one core while the tests job does
    not), passing `-n` with an empty count (every local run fails), the count reaching pytest as words of its own,
    and the gate left on xdist's default `load`, which gave one worker most of the suite."""
    script = "set -uo pipefail\nPY=python3\nstep() { shift 2; printf '%s\\n' \"$@\"; }\n" + \
        "\n".join(_validate_step_lines()) + "\n"
    env = {k: v for k, v in os.environ.items() if k != VAR}
    if value is not None:
        env[VAR] = value
    p = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    args = p.stdout.splitlines()
    head = ["env", "KONTROLL_REQUIRE_CHECKOUT=1", "python3", "-m", "pytest", "-m", "not e2e and not slow", "-q"]
    assert args[:len(head)] == head, args
    assert args[len(head):] == (expect or []), args


def _validate_step(name):
    """tests/validate.sh's `step <name> <status> bash -c '<body>' "$PY"<tail>` command: (body, tail)."""
    m = re.search(r"step %s \S+ bash -c '(.*?)' \"\$PY\"([^\n]*)\n" % re.escape(name), _read("tests/validate.sh"),
                  re.S)
    assert m, "validate.sh runs a %s step as `step %s <status> bash -c '...' \"$PY\"`" % (name, name)
    return m.group(1), m.group(2).strip()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash, which tests/validate.sh runs under")
@pytest.mark.parametrize("name", ["refuse-local-runtime", "key-render-runtime"])
def test_the_gates_runtime_steps_take_the_worker_arguments_and_the_as_root_step_none(tmp_path, name):
    """validate's refuse-local-runtime and key-render-runtime steps pass the pytest step's worker arguments
    (`${xdist[@]+"${xdist[@]}"}`) to their own pytest, after their own words, and their body still refuses a SKIPPED
    line: run under bash with a stand-in for python that prints each argument it is given, the body ends its pytest
    with `-n 2 --dist worksteal` when given them and with `--color=no` when given none. The sops-guard-conformance
    and canon-drop-as-root steps take none. Guards the two steps, most of the gate's time after its suite step
    (measured: 165 s and 76 s in one process, 107 s and 43 s on two workers), left on one core; the arguments
    reaching pytest before its own words or split; and the as-root step put on workers, where one of its rows
    (test_canon_drop's pty row) forks a pty in a threaded process, which Python warns may deadlock."""
    body, tail = _validate_step(name)
    assert tail == '${xdist[@]+"${xdist[@]}"}', (name, tail)
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done\n", encoding="utf-8")
    stub.chmod(0o755)
    bash = shutil.which("bash")
    for given, ends in ((["-n", "2", "--dist", "worksteal"], ["--color=no", "-n", "2", "--dist", "worksteal"]),
                        ([], ["-rs", "--color=no"])):
        p = subprocess.run([bash, "-c", body, str(stub)] + given, capture_output=True, text=True, timeout=30,
                           env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
        assert p.returncode == 0, (given, p.stdout, p.stderr)
        args = p.stdout.splitlines()
        assert args[:2] == ["-m", "pytest"] and args[-len(ends):] == ends, (given, args)
    for serial in ("sops-guard-conformance", "canon-drop-as-root"):
        assert "xdist" not in _validate_step(serial)[1], serial
