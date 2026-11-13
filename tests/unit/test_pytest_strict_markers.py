"""The suite's own pytest configuration makes a misspelt marker a collection error on the pytest that runs it.

WHY. pytest 9.0.0-9.0.3 ignore `--strict-markers` when it comes from addopts, the suite's own pyproject.toml included
(pytest #14442, fixed in 9.1.0), so on a host with one of them a misspelt marker was a warning: a row whose mark
selects or skips it went quiet, and nothing said so (I-71). pyproject.toml also sets the `strict_markers` ini key,
which pytest reads from 9.0; before 9 the addopts flag holds. Each row runs a pytest collection in a subprocess, under a
copy of the suite's pyproject.toml (or one edited as the row says), over one file whose one test carries a marker no one
registered, with `PYTEST_ADDOPTS` cleared.
"""
import os
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MISSPELT = "uniit"
_ERROR = "'%s' not found in `markers` configuration option" % _MISSPELT
_TEST_FILE = "import pytest\n\n\n@pytest.mark.%s\ndef test_x():\n    pass\n" % _MISSPELT


def _pyproject():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        return fh.read()


def _collect(tmp_path, config, *args):
    """The return code and output of a collection, under `config` written as tmp_path's pyproject.toml, of a file
    whose one test carries the misspelt marker."""
    (tmp_path / "pyproject.toml").write_text(config, encoding="utf-8")
    (tmp_path / "test_m.py").write_text(_TEST_FILE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", "--collect-only", "-p", "no:cacheprovider",
                        "-c", str(tmp_path / "pyproject.toml"), "--rootdir", str(tmp_path), *args,
                        str(tmp_path / "test_m.py")],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def test_a_misspelt_marker_is_a_collection_error_under_the_suites_config(tmp_path):
    """Under the suite's pyproject.toml as it is, its addopts kept, a test that carries an unregistered marker is a
    collection error naming it, on whichever pytest runs this row. Guards a host whose pytest drops the addopts flag
    (9.0.0-9.0.3) running the suite with a misspelt mark as a warning nobody reads (I-71)."""
    rc, out = _collect(tmp_path, _pyproject())
    assert rc != 0 and _ERROR in out, out


@pytest.mark.skipif(pytest.version_tuple < (9,), reason="the strict_markers ini key is read from pytest 9.0")
def test_the_ini_key_alone_makes_the_markers_strict(tmp_path):
    """With the addopts dropped (`-o addopts=`, as on a pytest that ignores the flag there), the suite's ini key alone
    still makes the misspelt marker a collection error. Guards the key being dropped from pyproject.toml, which the
    first row cannot see on a pytest whose addopts flag works (9.1 and later), and which leaves 9.0.0-9.0.3 lax."""
    rc, out = _collect(tmp_path, _pyproject(), "-o", "addopts=")
    assert rc != 0 and _ERROR in out, out


def test_without_the_flag_and_the_key_the_same_file_collects(tmp_path):
    """With the addopts dropped and the ini key removed, the same file collects (rc 0): the error the rows above see is
    the configuration's, not one of the file's own. Guards the rows above passing on a file that fails to collect for
    another reason."""
    config, n = re.subn(r"(?m)^strict_markers\s*=\s*true[ \t]*\r?\n", "", _pyproject())
    assert n == 1, "pyproject.toml sets strict_markers = true once"
    rc, out = _collect(tmp_path, config, "-o", "addopts=")
    assert rc == 0 and "test_x" in out, out
