"""Where scripts/lib/run-log.sh writes a run-log, against the one directory Vector ships them from (I-29).

WHY: Vector binds `<operator home>/.local/state/kontroll/runs` read-only (docker/services/vector.yaml), and the
launchers and the deploy make that directory. run-log.sh wrote under `${XDG_STATE_HOME:-~/.local/state}`, so an
operator whose shell sets XDG_STATE_HOME kept run-logs Vector never read, with no error anywhere: the internal_scripts
stream just stayed empty. These rows source the helper under bash, as its callers run it (`set -euo pipefail`), with
a scratch HOME, and look at what it wrote and said. Each row's HOME stands for the home the stack was deployed with:
a run whose HOME is another (root's, under sudo) is not told apart here (I-46).
"""
import os
import re
import shutil
import subprocess
import time

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HELPER = os.path.join(ROOT, "scripts", "lib", "run-log.sh")
TARGET = "/host/operator-runs"
# The source spellings compose takes for the operator's home, then the path under it: the same read as
# tests/unit/test_launcher_operator_home_binds.py's _OPERATOR_HOME.
_OPERATOR_HOME = re.compile(r"^(?:\$\{KONTROLL_OPERATOR_HOME(?::?[-?][^}]*)?\}|"
                            r"\$KONTROLL_OPERATOR_HOME(?![A-Za-z0-9_]))"
                            r"(/[^:]+)(?::|$)")

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"),
                                 reason="sources the bash helper (CI and the gate image have bash)")]


def _vector_runs_bind():
    """The path under the operator's home of Vector's run-log bind, read from docker/services/vector.yaml as compose
    reads it: the one volume, short form or long form, whose target is exactly /host/operator-runs. A commented-out
    bind is not read; a bind there from anywhere but the operator's home fails the read."""
    with open(os.path.join(ROOT, "docker", "services", "vector.yaml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    found = []
    for svc in (doc.get("services") or {}).values():
        for vol in svc.get("volumes") or []:
            if isinstance(vol, dict):
                src, target = vol.get("source") or "", vol.get("target")
            else:
                hit = re.search(r":(/[^:]+)(?::[a-z,]+)?$", vol)   # the target, then any mode (`:ro`)
                src, target = vol, hit.group(1) if hit else None
            if target == TARGET:
                m = _OPERATOR_HOME.match(src)
                assert m, "Vector's run-log bind is not from the operator's home: %s" % src
                found.append(m.group(1))
    assert len(found) == 1, found
    return found[0]


def _bash(tmp_path, home=True, cwd=None, **env):
    """Source run-log.sh under `bash -c 'set -euo pipefail; …'`, call `run_log_init t`, then print
    `made <RUN_LOG_FILE or none>` and `body`. HOME is <tmp>/home (made, if not yet), or unset when `home` is false;
    XDG_STATE_HOME and KONTROLL_RUN_LOG_DIR are unset but as `env` sets them. Returns (the home, the process). The
    output is whole when bash's pipes close: the helper's tee holds them until it has written."""
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    script = 'set -euo pipefail; . "%s"; run_log_init t; echo "made ${RUN_LOG_FILE:-none}"; echo body' % HELPER
    base = {k: v for k, v in os.environ.items() if k not in ("XDG_STATE_HOME", "KONTROLL_RUN_LOG_DIR", "HOME")}
    if home:
        base["HOME"] = str(h)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=cwd,
                       env=dict(base, **env))
    return str(h), p


def _run(tmp_path, cwd=None, **env):
    """_bash with a HOME, for a run that makes its log: returns (the home, the log file, the run's output)."""
    home, p = _bash(tmp_path, cwd=cwd, **env)
    assert p.returncode == 0, p.stderr
    made = re.search(r"^made (.+)$", p.stdout, re.M)
    assert made and made.group(1) != "none", p.stdout + p.stderr
    return home, made.group(1), p.stdout


def _files(top):
    """Every file under `top`, relative to it."""
    return sorted(os.path.relpath(os.path.join(d, f), top) for d, _, fs in os.walk(top) for f in fs)


@pytest.mark.parametrize("xdg", [None, "elsewhere", "home/.state", "home/.state-with-old-logs"])
def test_a_run_log_is_written_where_vector_reads_it_whatever_xdg_state_home_says(tmp_path, xdg):
    """With XDG_STATE_HOME unset, set to a directory outside HOME, set under HOME (its usual place), and set where
    an earlier helper left run-logs (`kontroll/runs/old.log`, the very operators I-29 hit), the helper writes its
    run-log in `$HOME` + the path Vector's bind names (read from vector.yaml), adds nothing under XDG_STATE_HOME,
    and says nothing about a log kept elsewhere. Guards I-29: the helper read XDG_STATE_HOME, so such a shell's
    run-logs were kept where Vector never looks, and the stream it ships stayed empty with no error; a helper that
    read it again only under HOME, or only where its old directory exists, fails here."""
    env = {}
    if xdg:
        x = tmp_path / xdg
        x.mkdir(parents=True)
        if xdg.endswith("-with-old-logs"):
            (x / "kontroll" / "runs").mkdir(parents=True)
            (x / "kontroll" / "runs" / "old.log").write_text("an earlier run\n")
        env["XDG_STATE_HOME"] = str(x)
        before = _files(str(x))
    home, made, out = _run(tmp_path, **env)
    assert os.path.dirname(made) == home + _vector_runs_bind(), made
    assert os.path.isfile(made) and "body" in open(made, encoding="utf-8").read()
    if xdg:
        assert _files(env["XDG_STATE_HOME"]) == before, "the helper wrote under XDG_STATE_HOME"
    assert "Vector does not read" not in out, out


def test_the_helper_does_not_read_xdg_state_home():
    """The helper's lines, its comment lines left out, name no XDG_STATE_HOME. Guards I-29 from coming back in a
    shape the rows above do not try: any read of the variable is one."""
    with open(HELPER, encoding="utf-8") as fh:
        code = [ln for ln in fh if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "XDG_STATE_HOME" in ln], "run-log.sh reads XDG_STATE_HOME"


def test_a_run_log_kept_under_kontroll_run_log_dir_says_vector_does_not_read_it(tmp_path):
    """KONTROLL_RUN_LOG_DIR set to another directory: the run-log is written there, and the run's first lines, on
    the console and in the log itself, say it is kept there, that Vector does not read it, and which directory
    Vector ships. Set relative, it is the caller's working directory's, and the note spells it absolute. Guards the
    override: it is the operator's to set, but a log kept off Vector's bind must say so, where before it was as
    silent as the XDG_STATE_HOME case, and say it in a form that reads as another directory."""
    own = str(tmp_path / "own")
    home, made, out = _run(tmp_path, KONTROLL_RUN_LOG_DIR=own)
    shipped = home + _vector_runs_bind()
    assert os.path.dirname(made) == own, made
    said = "# run-log: kept in %s (KONTROLL_RUN_LOG_DIR), which Vector does not read; it ships %s" % (own, shipped)
    assert said in out.splitlines()[:3], out
    assert said in open(made, encoding="utf-8").read().splitlines()[:3]
    (tmp_path / "cwd").mkdir()
    home, made, out = _run(tmp_path, cwd=str(tmp_path / "cwd"), KONTROLL_RUN_LOG_DIR="rel")
    rel = str(tmp_path / "cwd" / "rel")
    assert os.path.dirname(made) == "rel", made
    assert any("# run-log: kept in %s (KONTROLL_RUN_LOG_DIR)" % r in out for r in (rel, os.path.realpath(rel))), out


@pytest.mark.parametrize("spelling", ["trailing-slash", "two-trailing-slashes", "inner-double-slash", "dot-dot",
                                      "symlink"])
def test_the_shipped_directory_spelled_another_way_says_nothing(tmp_path, spelling):
    """KONTROLL_RUN_LOG_DIR set to the shipped directory itself, spelled another way (a trailing slash, as a shell
    completion leaves it; two; a `//` inside; a `..`; a symlink to it): the log lands in the shipped directory and
    nothing is said. Guards the note against a string compare, which told the operator Vector does not read the
    directory it reads."""
    home = str(tmp_path / "home")
    shipped = home + _vector_runs_bind()
    os.makedirs(shipped)                              # the launchers make it
    parent, leaf = os.path.split(shipped)
    value = {"trailing-slash": shipped + "/", "two-trailing-slashes": shipped + "//",
             "inner-double-slash": home + "/" + _vector_runs_bind(),
             "dot-dot": os.path.join(parent, "..", os.path.basename(parent), leaf),
             "symlink": str(tmp_path / "link")}[spelling]
    if spelling == "symlink":
        os.symlink(shipped, value)
    home, made, out = _run(tmp_path, KONTROLL_RUN_LOG_DIR=value)
    assert os.path.realpath(os.path.dirname(made)) == os.path.realpath(shipped), made
    assert "Vector does not read" not in out, out


def test_without_home_a_run_goes_on_and_an_override_still_keeps_its_log(tmp_path):
    """HOME unset, as `env -i` or a service manager can leave it, under the callers' `set -u`: with
    KONTROLL_RUN_LOG_DIR set the log is kept there and the note names the shipped directory by its place under the
    operator's home; with neither, there is no run-log, the helper says nothing, and the script goes on. Guards the
    helper killing its caller on an unbound HOME before the script's own work, where it calls itself fail-soft."""
    own = str(tmp_path / "own")
    _, p = _bash(tmp_path, home=False, KONTROLL_RUN_LOG_DIR=own)
    assert p.returncode == 0 and "body" in p.stdout, p.stderr
    assert re.search(r"^made %s/t-\S+\.log$" % re.escape(own), p.stdout, re.M), p.stdout
    assert "it ships <operator home>%s" % _vector_runs_bind() in p.stdout, p.stdout
    _, p = _bash(tmp_path, home=False)
    assert p.returncode == 0 and "made none" in p.stdout and "body" in p.stdout, p.stdout + p.stderr
    assert "run-log" not in p.stdout + p.stderr, p.stdout + p.stderr


def test_an_override_that_cannot_be_made_says_so_and_the_run_goes_on(tmp_path):
    """KONTROLL_RUN_LOG_DIR under a regular file (mkdir cannot make it, even as root): the run says it cannot make
    the directory and goes on without a run-log. Guards the silence there was: no note, no log, no word."""
    (tmp_path / "file").write_text("")
    _, p = _bash(tmp_path, KONTROLL_RUN_LOG_DIR=str(tmp_path / "file" / "runs"))
    assert p.returncode == 0 and "made none" in p.stdout and "body" in p.stdout, p.stdout + p.stderr
    assert "run-log: cannot make %s" % (tmp_path / "file" / "runs") in p.stderr, p.stderr


def test_the_prune_takes_old_logs_from_the_directory_written_and_no_other(tmp_path):
    """Under KONTROLL_RUN_LOG_DIR a log older than the retention (40 days, against the default 30) is pruned from
    that directory, and one as old in the shipped directory is kept. Guards the prune reading the shipped directory
    where it should read the one this run writes: an override's logs never pruned, the shipped ones pruned by a run
    that did not write there."""
    home = str(tmp_path / "home")
    shipped = home + _vector_runs_bind()
    own = str(tmp_path / "own")
    old = time.time() - 40 * 86400
    for d in (shipped, own):
        os.makedirs(d)
        with open(os.path.join(d, "old.log"), "w") as fh:
            fh.write("an earlier run\n")
        os.utime(os.path.join(d, "old.log"), (old, old))
    _run(tmp_path, KONTROLL_RUN_LOG_DIR=own)
    assert not os.path.exists(os.path.join(own, "old.log")), "the override's old log was not pruned"
    assert os.path.exists(os.path.join(shipped, "old.log")), "the shipped directory was pruned by an override run"
