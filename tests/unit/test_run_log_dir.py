"""Where scripts/lib/run-log.sh writes a run-log, against the one directory Vector ships them from (I-29).

WHY: Vector binds `<operator home>/.local/state/kontroll/runs` read-only (docker/services/vector.yaml), and the
launchers and the deploy make that directory. run-log.sh wrote under `${XDG_STATE_HOME:-~/.local/state}`, so an
operator whose shell sets XDG_STATE_HOME kept run-logs Vector never read, with no error anywhere: the internal_scripts
stream just stayed empty. These rows source the helper under bash with a scratch HOME and look at what it wrote.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HELPER = os.path.join(ROOT, "scripts", "lib", "run-log.sh")

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"),
                                 reason="sources the bash helper (CI and the gate image have bash)")]


def _vector_runs_bind():
    """The path under the operator's home of Vector's run-log bind, read from docker/services/vector.yaml (the one
    bind whose target is /host/operator-runs)."""
    with open(os.path.join(ROOT, "docker", "services", "vector.yaml"), encoding="utf-8") as fh:
        found = re.findall(r"\$\{KONTROLL_OPERATOR_HOME[^}]*\}(/[^:\s]+):/host/operator-runs", fh.read())
    assert len(found) == 1, found
    return found[0]


def _run(tmp_path, **env):
    """Source run-log.sh under bash with HOME=<tmp>/home and neither XDG_STATE_HOME nor KONTROLL_RUN_LOG_DIR but as
    `env` sets them, call `run_log_init t` and print a line. Returns (the home, the log file the helper made, the
    run's output). The output is whole when bash's pipes close: the helper's tee holds them until it has written."""
    home = tmp_path / "home"
    home.mkdir()
    script = '. "%s"; run_log_init t; echo "made $RUN_LOG_FILE"; echo body' % HELPER
    base = {k: v for k, v in os.environ.items() if k not in ("XDG_STATE_HOME", "KONTROLL_RUN_LOG_DIR")}
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       env=dict(base, HOME=str(home), **env))
    assert p.returncode == 0, p.stderr
    made = re.search(r"^made (.+)$", p.stdout, re.M)
    assert made, p.stdout
    return str(home), made.group(1), p.stdout


@pytest.mark.parametrize("xdg", [None, "elsewhere"])
def test_a_run_log_is_written_where_vector_reads_it_whatever_xdg_state_home_says(tmp_path, xdg):
    """With XDG_STATE_HOME unset, and set to another directory, the helper writes its run-log in
    `$HOME` + the path Vector's bind names (read from vector.yaml), writes nothing under XDG_STATE_HOME, and says
    nothing about a log kept elsewhere. Guards I-29: the helper read XDG_STATE_HOME, so such a shell's run-logs
    were kept where Vector never looks, and the stream it ships stayed empty with no error."""
    env = {"XDG_STATE_HOME": str(tmp_path / xdg)} if xdg else {}
    if xdg:
        (tmp_path / xdg).mkdir()
    home, made, out = _run(tmp_path, **env)
    assert os.path.dirname(made) == home + _vector_runs_bind(), made
    assert os.path.isfile(made) and "body" in open(made, encoding="utf-8").read()
    if xdg:
        assert os.listdir(str(tmp_path / xdg)) == [], "the helper wrote under XDG_STATE_HOME"
    assert "Vector does not read" not in out, out


def test_a_run_log_kept_under_kontroll_run_log_dir_says_vector_does_not_read_it(tmp_path):
    """KONTROLL_RUN_LOG_DIR set to another directory: the run-log is written there, and the run's first lines, on
    the console and in the log itself, say it is kept there, that Vector does not read it, and which directory
    Vector ships. Set to the shipped directory itself (with a trailing slash, as a shell completion leaves it),
    nothing is said. Guards the override: it is the operator's to set, but a log kept off Vector's bind must say
    so, where before it was as silent as the XDG_STATE_HOME case."""
    own = str(tmp_path / "own")
    home, made, out = _run(tmp_path, KONTROLL_RUN_LOG_DIR=own)
    shipped = home + _vector_runs_bind()
    assert os.path.dirname(made) == own, made
    said = "# run-log: kept in %s (KONTROLL_RUN_LOG_DIR), which Vector does not read; it ships %s" % (own, shipped)
    assert said in out.splitlines()[:3], out
    assert said in open(made, encoding="utf-8").read().splitlines()[:3]
    shutil.rmtree(home)
    home, made, out = _run(tmp_path, KONTROLL_RUN_LOG_DIR=shipped + "/")
    assert os.path.dirname(made.replace("//", "/")) == shipped and "Vector does not read" not in out, out
