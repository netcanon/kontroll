"""The cores CI's pytest legs run, held to the cores the project ships (I-80, I-81, I-94).

WHY. Until I-94, ci.yml's `tests` job installed no Ansible, so each row that runs ansible-playbook ran on whatever the
GitHub runner image carried: a pipx install, unpinned (a core from 2.21 when I-94 was found), which no shipped piece
runs. Every Semaphore job runs the control image's core and the installer runs its own, and no leg ran either (I-80);
a row's verdict moved when the image did, under an unchanged tree. Each leg now installs a pinned core. These rows
hold each pin to the file that decides a shipped core (the installer's requirements.txt and Debian base, the control
image's Dockerfile, the validate job's install step) and each leg's Python to the range its core supports, so a bump
there that leaves CI on the old core fails here, not after a green CI has claimed a core it did not run. They read the
committed text and YAML; tests/integration/test_ci_leg_core.py holds a leg's run to its pin.
"""
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CI = ".github/workflows/ci.yml"
INSTALLER_PINS = "docker/installer/requirements.txt"
INSTALLER_DOCKERFILE = "docker/installer/Dockerfile"
CONTROL_DOCKERFILE = "docker/semaphore-runner/Dockerfile"
FILES = (CI, INSTALLER_PINS, INSTALLER_DOCKERFILE, CONTROL_DOCKERFILE)

# The main ruleset requires this job's name with each of these Pythons: a leg dropped or renamed leaves a required
# check that never reports, which blocks every pull request.
JOB_NAME = "pytest (unit · integration · gui) — py${{ matrix.python }}"
REQUIRED_PYTHONS = ["3.11", "3.12", "3.13", "3.14"]
# The controller Pythons each ansible-core series supports (its support matrix): a leg outside its core's range runs
# a pair no shipped piece runs, which the core may refuse or run differently.
CONTROLLER_PYTHONS = {(2, 18): ((3, 11), (3, 13)), (2, 19): ((3, 11), (3, 13)), (2, 20): ((3, 12), (3, 14))}
# The python3 each Debian release ships, for the installer image's base.
DEBIAN_PYTHON = {"12": "3.11", "13": "3.13"}
PIN = "${{ matrix.ansible-core }}"
INSTALL_CORE = 'pip install --quiet "ansible-core==$ANSIBLE_CORE"'
INSTALL = [
    "set -euo pipefail",
    INSTALL_CORE,
    'mkdir "$RUNNER_TEMP/gen" && git archive HEAD | tar -x -C "$RUNNER_TEMP/gen"',
    'python3 "$RUNNER_TEMP/gen/scripts/gen-requirements.py" "$RUNNER_TEMP/all-modules.yml" --all-modules',
    'ansible-galaxy collection install -r "$RUNNER_TEMP/all-modules.yml"',
    "ansible-playbook --version",
]
CORE = r"\d+\.\d+\.\d+"


def _read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def _one(pattern, text, what, faults):
    """The single match of `pattern` in `text` (multiline), else None, with a fault naming `what`."""
    found = re.findall(pattern, text, re.M)
    if len(found) != 1:
        faults.append("%s: %d matches, not one" % (what, len(found)))
        return None
    return found[0]


def _numbers(version, n):
    """The first `n` numbers of a version (`2.20.10`, 2 -> (2, 20))."""
    return tuple(int(x) for x in version.split(".")[:n])


def shipped_cores(root, faults):
    """{who: (core, python or None)}: each core the project ships, read from the file that decides it, with the
    Python it runs on where a leg must match that too (None: any leg's Python will do)."""
    validate = yaml.safe_load(_read(root, CI))["jobs"]["validate"]["steps"]
    validate = "\n".join(str(s.get("run", "")) for s in validate)
    core = _one(r"^ansible-core==(%s)$" % CORE, _read(root, INSTALLER_PINS), "the installer's ansible-core pin", faults)
    debian = _one(r"^FROM debian:(\d+)[-\w.]*@sha256:", _read(root, INSTALLER_DOCKERFILE),
                  "the installer's Debian base", faults)
    if debian is not None and debian not in DEBIAN_PYTHON:
        faults.append("the installer's base is Debian %s, whose python3 DEBIAN_PYTHON does not name" % debian)
    control = _one(r"^# The base's ansible venv ships ansible-core (%s) on Python (\d+\.\d+):" % CORE,
                   _read(root, CONTROL_DOCKERFILE), "the control image's core line", faults)
    return {"the installer": (core, DEBIAN_PYTHON.get(debian)),
            "the control image": control or (None, None),
            "the validate job": (_one(r'"ansible-core==(%s)"' % CORE, validate, "the validate job's ansible-core pin",
                                      faults), None)}


def leg_faults(root=ROOT):
    """[fault] of ci.yml's `tests` legs against the cores the project ships; an empty list is a pass."""
    faults = []
    job = yaml.safe_load(_read(root, CI))["jobs"]["tests"]
    if job.get("name") != JOB_NAME:
        faults.append("the job's name is %r, not %r (the ruleset's required checks)" % (job.get("name"), JOB_NAME))
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    if set(matrix) != {"include"}:
        faults.append("the matrix holds %s, not `include` alone (one leg per Python, each naming its core)"
                      % sorted(matrix))
    legs = []
    for entry in matrix.get("include") or []:
        if set(entry) != {"python", "ansible-core"}:
            faults.append("a leg names %s, not python and ansible-core" % sorted(entry))
        else:
            legs.append((str(entry["python"]), str(entry["ansible-core"])))
    pythons = [p for p, _ in legs]
    if pythons != REQUIRED_PYTHONS:
        faults.append("the legs' Pythons are %s, not the ruleset's %s" % (", ".join(pythons),
                                                                        ", ".join(REQUIRED_PYTHONS)))
    shipped = shipped_cores(root, faults)
    for who, (core, python) in shipped.items():
        if core is None:
            continue
        if python is None and core not in [c for _, c in legs]:
            faults.append("%s's ansible-core %s runs in no leg" % (who, core))
        elif python is not None and (python, core) not in legs:
            faults.append("%s's ansible-core %s on Python %s runs in no leg" % (who, core, python))
    cores = {core for core, _ in shipped.values()}
    for python, core in legs:
        if core not in cores:
            faults.append("the py%s leg runs ansible-core %s, which no shipped piece runs" % (python, core))
        if not re.fullmatch(CORE, core) or not re.fullmatch(r"\d+\.\d+", python):
            faults.append("the py%s leg's versions do not parse (%s)" % (python, core))
            continue
        span = CONTROLLER_PYTHONS.get(_numbers(core, 2))
        if span is None:
            faults.append("the py%s leg's ansible-core %s: CONTROLLER_PYTHONS names no range for its series"
                          % (python, core))
        elif not span[0] <= _numbers(python, 2) <= span[1]:
            faults.append("the py%s leg's ansible-core %s supports controller Python %d.%d to %d.%d"
                          % ((python, core) + span[0] + span[1]))
    steps = job.get("steps") or []
    install = [i for i, s in enumerate(steps) if INSTALL_CORE in str(s.get("run", ""))]
    pytest_steps = [i for i, s in enumerate(steps) if str(s.get("run", "")).startswith("pytest ")]
    if len(install) != 1 or len(pytest_steps) != 1:
        faults.append("%d steps install the leg's core and %d run pytest, not one each"
                      % (len(install), len(pytest_steps)))
        return faults
    step, run = steps[install[0]], steps[pytest_steps[0]]
    if str(step.get("run", "")).strip().splitlines() != INSTALL:
        faults.append("the install step runs %r" % str(step.get("run")))
    if step.get("env") != {"ANSIBLE_CORE": PIN}:
        faults.append("the install step's env is %r, not the leg's pin as ANSIBLE_CORE" % step.get("env"))
    if (run.get("env") or {}).get("KONTROLL_ANSIBLE_CORE") != PIN:
        faults.append("the pytest step does not hand the leg's pin to the run (KONTROLL_ANSIBLE_CORE)")
    if install[0] > pytest_steps[0]:
        faults.append("the leg's core is installed after pytest runs")
    for where, item in (("the job", job), ("the install step", step), ("the pytest step", run)):
        soft = sorted(k for k in ("continue-on-error", "if") if k in item)
        if soft:
            faults.append("%s is softened or skipped (%s)" % (where, ", ".join(soft)))
    return faults


def test_the_pytest_legs_run_the_cores_the_project_ships():
    """Each core the project ships runs in a pytest leg: the installer's pin on its Debian base's python3, the control
    image's core on its venv's Python (both as a leg's exact pair), the validate job's pin on any leg; no leg runs a
    core nothing ships or a Python its core does not support; the job keeps the ruleset's four check names; each leg
    installs its pin (and every module's collections, the generator in an export of HEAD) before pytest, hands the pin
    to the run, and neither step nor the job is softened. Guards CI's legs left on an old core when a shipped one
    moves (the control image's base bump, the installer's pin, the validate job's), and the rows run on the runner
    image's own unpinned core again (I-94)."""
    assert leg_faults() == []


def _leg(python, core):
    """A leg as ci.yml spells it."""
    return "          - python: '%s'\n            ansible-core: '%s'\n" % (python, core)


def _bump(version):
    """The next patch release of a version (`2.18.19` -> `2.18.20`)."""
    head, last = version.rsplit(".", 1)
    return "%s.%d" % (head, int(last) + 1)


def _case(name, live):
    """(edits, expected faults) for a drift named `name`, built from the live tree's own pins so a legitimate bump
    needs no edit here. An edit is (file, old, new); `old` must occur exactly once."""
    legs, (ic, ipy), (cc, cpy), vc = live["legs"], live["the installer"], live["the control image"], live["vc"]

    def orphaned(old, still):
        return ["the py%s leg runs ansible-core %s, which no shipped piece runs" % (p, c) for p, c in legs
                if c == old and old not in still]

    if name == "installer-pin":
        return ([(INSTALLER_PINS, "ansible-core==%s\n" % ic, "ansible-core==%s\n" % _bump(ic))],
                ["the installer's ansible-core %s on Python %s runs in no leg" % (_bump(ic), ipy)]
                + orphaned(ic, {cc, vc}))
    if name == "installer-debian":
        debian = re.search(r"(?m)^FROM debian:(\d+)", _read(ROOT, INSTALLER_DOCKERFILE)).group(1)
        other = next(d for d in sorted(DEBIAN_PYTHON) if DEBIAN_PYTHON[d] != ipy)
        assert (DEBIAN_PYTHON[other], ic) not in legs, "the case would show nothing"
        return ([(INSTALLER_DOCKERFILE, "FROM debian:%s" % debian, "FROM debian:%s" % other)],
                ["the installer's ansible-core %s on Python %s runs in no leg" % (ic, DEBIAN_PYTHON[other])])
    if name == "control-core":
        return ([(CONTROL_DOCKERFILE, "ships ansible-core %s on" % cc, "ships ansible-core %s on" % _bump(cc))],
                ["the control image's ansible-core %s on Python %s runs in no leg" % (_bump(cc), cpy)]
                + orphaned(cc, {ic, vc}))
    if name == "control-line-gone":
        return ([(CONTROL_DOCKERFILE, "# The base's ansible venv ships", "# The base ships")],
                ["the control image's core line: 0 matches, not one"] + orphaned(cc, {ic, vc}))
    if name == "validate-pin":
        return ([(CI, '"ansible-core==%s"' % vc, '"ansible-core==%s"' % _bump(vc))],
                ["the validate job's ansible-core %s runs in no leg" % _bump(vc)] + orphaned(vc, {ic, cc}))
    last_python, last_core = legs[-1]
    if name == "leg-out-of-range":
        low = min(CONTROLLER_PYTHONS, key=lambda s: CONTROLLER_PYTHONS[s][1])
        core = next(c for c in (ic, cc, vc) if _numbers(c, 2) == low)
        span = CONTROLLER_PYTHONS[low]
        assert _numbers(last_python, 2) > span[1], "the case would show nothing"
        return ([(CI, _leg(last_python, last_core), _leg(last_python, core))],
                ["the py%s leg's ansible-core %s supports controller Python %d.%d to %d.%d"
                 % ((last_python, core) + span[0] + span[1])])
    if name == "leg-unknown-series":
        core = "%d.%d.0" % (max(CONTROLLER_PYTHONS)[0], max(CONTROLLER_PYTHONS)[1] + 1)
        return ([(CI, _leg(last_python, last_core), _leg(last_python, core))],
                ["the py%s leg runs ansible-core %s, which no shipped piece runs" % (last_python, core),
                 "the py%s leg's ansible-core %s: CONTROLLER_PYTHONS names no range for its series"
                 % (last_python, core)])
    if name == "leg-dropped":
        kept = [p for p, _ in legs[:-1]]
        return ([(CI, _leg(last_python, last_core), "")],
                ["the legs' Pythons are %s, not the ruleset's %s" % (", ".join(kept), ", ".join(REQUIRED_PYTHONS))])
    if name == "job-renamed":
        return ([(CI, "name: %s\n" % JOB_NAME, "name: pytest — py${{ matrix.python }}\n")],
                ["the job's name is 'pytest — py${{ matrix.python }}', not %r (the ruleset's required checks)"
                 % JOB_NAME])
    if name == "no-pin-to-the-run":
        return ([(CI, "          KONTROLL_ANSIBLE_CORE: %s\n" % PIN, "")],
                ["the pytest step does not hand the leg's pin to the run (KONTROLL_ANSIBLE_CORE)"])
    if name == "generator-in-the-checkout":
        old = 'python3 "$RUNNER_TEMP/gen/scripts/gen-requirements.py"'
        text = _read(ROOT, CI)
        run = [s for s in yaml.safe_load(text)["jobs"]["tests"]["steps"] if INSTALL_CORE in str(s.get("run", ""))]
        return ([(CI, old, "python3 scripts/gen-requirements.py")],
                ["the install step runs %r" % str(run[0]["run"]).replace(old, "python3 scripts/gen-requirements.py")])
    if name == "unpinned-install":
        return ([(CI, INSTALL_CORE, "pip install --quiet ansible-core")],
                ["0 steps install the leg's core and 1 run pytest, not one each"])
    if name == "install-softened":
        return ([(CI, "        env:\n          ANSIBLE_CORE: %s\n" % PIN,
                  "        continue-on-error: true\n        env:\n          ANSIBLE_CORE: %s\n" % PIN)],
                ["the install step is softened or skipped (continue-on-error)"])
    raise AssertionError(name)


CASES = ["installer-pin", "installer-debian", "control-core", "control-line-gone", "validate-pin", "leg-out-of-range",
         "leg-unknown-series", "leg-dropped", "job-renamed", "no-pin-to-the-run", "generator-in-the-checkout",
         "unpinned-install", "install-softened"]


@pytest.mark.parametrize("name", CASES)
def test_the_leg_check_names_each_drift(tmp_path, name):
    """A copy of the four files with one drift applied fails the check with exactly the faults that drift causes: a
    shipped core moved where no leg follows (the installer's pin or Debian base, the control image's core, the
    validate job's pin; a leg then left on a core nothing ships is named too), the control image's core line gone, a
    leg on a Python its core does not support or on a series the table does not know, a leg dropped, the job renamed,
    the pin not handed to the run, the generator run in the checkout, the core installed unpinned, the install
    softened. Each drift is built from the live pins, and the live tree passes first. Guards the check above passing
    because a reader matched nothing or a comparison never fires."""
    faults = []
    live = shipped_cores(ROOT, faults)
    assert faults == []
    job = yaml.safe_load(_read(ROOT, CI))["jobs"]["tests"]
    live["legs"] = [(str(e["python"]), str(e["ansible-core"])) for e in job["strategy"]["matrix"]["include"]]
    live["vc"] = live["the validate job"][0]
    edits, expected = _case(name, live)
    for rel in FILES:
        text = _read(ROOT, rel)
        for path, old, new in edits:
            if path == rel:
                assert text.count(old) == 1, (rel, old)
                text = text.replace(old, new)
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    assert leg_faults(str(tmp_path)) == expected
