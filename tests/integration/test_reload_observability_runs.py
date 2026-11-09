"""reload-observability.yml run for real, and as its dry-run, against a stand-in for Prometheus on the loopback (I-8).

WHY. `uri` has no check mode, so a `--check` run skips the play's reload POST, and the skipped result has no status.
The play's report read `_reload.status` regardless, so the dry-run failed ("object of type 'dict' has no attribute
'status'"): the mandated dry-run of the GUI's Tier-1 enact could never pass. I-6's runtime read found it. The play
now says, in a dry-run, that the reload was not sent, and passes; a real run reports the status as before. Only a run
of the play can say that the dry-run sends nothing and that a real run sends one reload: these rows serve
`/-/reload` from a thread of their own on 127.0.0.1, count what reaches it, and point the play at it with
`-e prometheus_url=…`, the operator's override.

It needs ansible-playbook: where it is missing every row is skipped (the gate's images and CI's validate job have it).
Nothing of the developer's Ansible reaches the play: its configuration, home, temporary directory and inventory are
the row's own.
"""
import http.server
import os
import re
import shutil
import socket
import subprocess
import threading

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                       reason="needs ansible-playbook (it runs reload-observability.yml for real)"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "reload-observability.yml")


@pytest.fixture
def prometheus():
    """A stand-in for Prometheus' lifecycle endpoint: it answers each request with `status` and records it."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        status = 200

        def _answer(self):
            seen.append((self.command, self.path))
            body = b"" if self.status == 200 else b"failed to reload config\n"
            self.send_response(self.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield type("Prometheus", (), {"url": "http://127.0.0.1:%d" % server.server_address[1], "seen": seen,
                                  "handler": Handler})()
    server.shutdown()
    server.server_close()


def _play(tmp_path, url, check=False, hosts=None):
    """Run the play against `url`, with an Ansible home, configuration and inventory of the row's own; return its exit
    code, its output and its recap's counts for localhost. The inventory names localhost unless `hosts` gives its
    hosts (`{}`: an inventory with none, where the play runs on the implicit localhost)."""
    home = tmp_path / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n", encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    if hosts is None:
        hosts = {"localhost": {"ansible_connection": "local",
                               "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}
    inventory.write_text(yaml.safe_dump({"all": {"hosts": hosts} if hosts else {}}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1", NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
    argv = ["ansible-playbook", "-i", str(inventory), PLAYBOOK, "-e", "prometheus_url=%s" % url]
    if check:
        argv += ["--check", "--diff"]
    p = subprocess.run(argv, cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    m = re.search(r"^localhost\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", p.stdout, re.M)
    recap = dict(zip(("ok", "changed", "unreachable", "failed"), map(int, m.groups()))) if m else None
    return p.returncode, p.stdout + p.stderr, recap


def _stamped(out):
    """The run id line the play's first import prints: the key its output is correlated by."""
    return re.search(r'"msg": "kontroll_run_id=[0-9]{8}T[0-9]{6}Z"', out) is not None


def test_a_dry_run_sends_no_reload_says_so_and_passes(tmp_path, prometheus):
    """The I-8 fault: `--check --diff` must pass, send nothing to Prometheus, and say the reload was not sent (not that
    it was made). Before the fix the report read the skipped POST's missing status and the dry-run failed."""
    rc, out, recap = _play(tmp_path, prometheus.url, check=True)
    assert rc == 0, out
    assert prometheus.seen == [], out
    assert "Prometheus config reload not sent in a dry-run (%s/-/reload;" % prometheus.url in out, out
    assert "Prometheus config reloaded" not in out, out
    assert recap == {"ok": recap["ok"], "changed": 0, "unreachable": 0, "failed": 0}, out
    assert _stamped(out), out


def test_a_real_run_sends_one_reload_and_reports_its_status(tmp_path, prometheus):
    """A real run is unchanged by the fix: it POSTs `/-/reload` once, reports the status Prometheus answered, counts
    as a change, and does not print the dry-run's line."""
    rc, out, recap = _play(tmp_path, prometheus.url)
    assert rc == 0, out
    assert prometheus.seen == [("POST", "/-/reload")], out
    assert "Prometheus config reloaded (%s/-/reload -> 200)." % prometheus.url in out, out
    assert "Prometheus config reload not sent" not in out, out
    assert recap["changed"] == 1 and recap["failed"] == 0, out
    assert _stamped(out), out


def test_a_reload_prometheus_refuses_fails_the_real_run(tmp_path, prometheus):
    """A reload Prometheus refuses (it answers 500 for a config it cannot load, and keeps the old one) still fails the
    real run at the POST, as before: the dry-run's branch must not turn a refusal into a pass."""
    prometheus.handler.status = 500
    rc, out, recap = _play(tmp_path, prometheus.url)
    assert rc != 0, out
    assert prometheus.seen == [("POST", "/-/reload")], out
    assert "Prometheus config reloaded" not in out and "Prometheus config reload not sent" not in out, out
    assert recap["failed"] == 1, out


def test_a_real_run_with_no_prometheus_listening_fails(tmp_path):
    """A real run against a Prometheus that is not there (nothing listening on the port: uri's status -1) fails at the
    POST and claims no reload. A reload that was never received must not pass, nor be reported as made; the project's
    "a down host is tolerated" rule is about fleet runs, and softening this POST to it would hide a reload never sent."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:%d" % s.getsockname()[1]
    s.close()
    rc, out, recap = _play(tmp_path, url)
    assert rc != 0, out
    assert "Prometheus config reloaded" not in out and "Prometheus config reload not sent" not in out, out
    assert recap["failed"] == 1, out


def test_a_real_run_on_an_inventory_with_no_hosts_is_stamped(tmp_path, prometheus):
    """I-15: with an inventory that has no hosts (`all: {}`), the play still runs on the implicit localhost and POSTs
    the reload, so its output must carry the run id. On `hosts: all` the stamp matched nothing (the implicit
    localhost is not in `all`), and the reload was sent with nothing to correlate it by, by the installer and by
    Semaphore alike."""
    rc, out, recap = _play(tmp_path, prometheus.url, hosts={})
    assert rc == 0, out
    assert prometheus.seen == [("POST", "/-/reload")], out
    assert _stamped(out), out
    assert out.index("kontroll_run_id=") < out.index("Prometheus config reloaded"), out
