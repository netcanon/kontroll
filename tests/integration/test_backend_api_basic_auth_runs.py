"""A basic-auth API host's check and backup, run for real: the pair each name resolves reaches the API, a missing pair
is refused by name before the request, and no credential is printed (I-16).

WHY. tests/unit/test_backend_api_check_teeth.py evaluates the role's expressions; only a run shows what reaches the
device and what the operator reads. Before I-16 a basic recipe's username and password were looked up in the
request's own arguments: a name not in scope failed the request under its `no_log` before `failed_when: false`
applied, so the fatal arrived CENSORED on both cores and the refusal of a rejected credential was never reached; and
the onboard wrote the pair as `ansible_user` and `ansible_password`, which the role never read, so an onboarded basic
host could not authenticate at all.

These rows run the role's own `check` and `backup` entrypoints with ansible-playbook for six hosts of a basic recipe
written for the row, against an HTTPS server the row runs on the loopback (a self-signed certificate; the recipe does
not validate it, as the shipped one does not). The server answers the one `Authorization` the row gives it with 200
(and a config body for the backup's path) and any other with 401, and records each it receives. The hosts hold the
pair under the recipe's names, under the onboarded per-host names, under both (the recipe's holding the right one),
under the recipe's names empty and the per-host ones right, under neither, and under the per-host names with a wrong
password.

It needs ansible-playbook and openssl: where either is missing every row is skipped (the gate's images and CI's
validate job have both). Nothing of the developer's Ansible reaches the play, nor a proxy: its configuration, home,
inventory and captures are the row's own.
"""
import base64
import http.server
import os
import re
import shutil
import ssl
import subprocess
import threading

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not all(shutil.which(tool) for tool in ("ansible-playbook", "openssl")),
                       reason="needs ansible-playbook and openssl (it runs the role against a local HTTPS server)"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
USER, PASSWORD = "kontroll-reader", "pw-right-6c1f9e"
WRONG_USER, WRONG_PASSWORD = "kontroll-nobody", "pw-wrong-0b7d42"
CONFIG = "config system global\n    set hostname probe-device\nend\n"
HOSTS = {   # host: its vars, beside the group's
    "api-recipe": {"probe_user": USER, "probe_pass": PASSWORD},
    "api-onboarded": {"api_onboarded_api_username": USER, "api_onboarded_api_password": PASSWORD},
    "api-both": {"probe_user": USER, "probe_pass": PASSWORD,
                 "api_both_api_username": WRONG_USER, "api_both_api_password": WRONG_PASSWORD},
    "api-empty": {"probe_user": "", "probe_pass": "",
                  "api_empty_api_username": USER, "api_empty_api_password": PASSWORD},
    "api-none": {},
    "api-wrong": {"api_wrong_api_username": USER, "api_wrong_api_password": WRONG_PASSWORD},
}
AUTHENTICATE = ["api-recipe", "api-onboarded", "api-both", "api-empty"]
SECRETS = (PASSWORD, WRONG_PASSWORD, base64.b64encode(("%s:%s" % (USER, PASSWORD)).encode()).decode())
REFUSED = ("api-none: the API username or password (the failed assertion says which) is not in scope, so this %s "
           "cannot authenticate. The probe recipe reads `probe_user` and `probe_pass`; the onboard writes "
           "`api_none_api_username` and `api_none_api_password`. Neither name is defined, or both are empty.")
ASSERTION = re.compile(r"assertion\W{1,6}_api_username \| default\(''\) \| length > 0")
REJECTED = ("HTTP 401 from /api/status. The API rejected the credential: check that `probe_user` (or, if it is empty, "
            "`api_wrong_api_username`) and `probe_pass` (or, if it is empty, `api_wrong_api_password`) hold a current "
            "username and password with read permission.")
RECAP = re.compile(r"^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.M)


class _Api(http.server.BaseHTTPRequestHandler):
    right = "Basic " + base64.b64encode(("%s:%s" % (USER, PASSWORD)).encode()).decode()

    def do_GET(self):   # noqa: N802 (the stdlib's name)
        auth = self.headers.get("Authorization")
        self.server.seen.append((self.path, auth))
        body = (CONFIG if self.path == "/api/backup" else '{"status": "ok"}\n').encode()
        if auth != self.right or self.path not in ("/api/status", "/api/backup"):
            self.send_response(401 if auth != self.right else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def lab(tmp_path):
    """The HTTPS server on the loopback, a copy of the role beside a basic recipe aimed at it, the inventory, an
    Ansible home and configuration of the row's own, and a captures directory."""
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", "1", "-subj", "/CN=127.0.0.1"], check=True, capture_output=True)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    httpd.seen = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    tree = tmp_path / "tree" / "ansible"
    shutil.copytree(os.path.join(ROOT, "ansible", "roles", "backend_api"), tree / "roles" / "backend_api")
    (tree / "backends" / "api" / "recipes").mkdir(parents=True)
    (tree / "backends" / "api" / "recipes" / "probe.yml").write_text(yaml.safe_dump({
        "name": "probe", "port": httpd.server_address[1], "validate_certs": False,
        "auth": {"type": "basic", "username_var": "probe_user", "password_var": "probe_pass"},
        "check": {"method": "GET", "path": "/api/status"},
        "backup": {"method": "GET", "path": "/api/backup", "suffix": "config.txt"}}), encoding="utf-8")
    home = tmp_path / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = %s\nretry_files_enabled = False\ninterpreter_python = auto_silent\n"
                   % (tree / "roles"), encoding="utf-8")
    backups = tmp_path / "backups"
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}",
                 "ansible_host": "127.0.0.1", "backend_api_recipe": "probe", "config_backup_dir": str(backups)},
        "hosts": {host: dict(hostvars, backend_backup_label=host) for host, hostvars in HOSTS.items()}}}),
        encoding="utf-8")
    play = tmp_path / "play.yml"
    play.write_text(yaml.safe_dump([{
        "name": "The backend_api entrypoint under test", "hosts": "all", "gather_facts": False,
        "tasks": [{"name": "Run it", "ansible.builtin.include_role": {
            "name": "backend_api", "tasks_from": "{{ kontroll_test_entry }}"}}]}], sort_keys=False),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANSIBLE_", "KONTROLL_")) and k.lower() not in
           ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1", no_proxy="127.0.0.1,localhost")

    def run(entry, *extra):
        del httpd.seen[:]
        done = subprocess.run(["ansible-playbook", "-i", str(inventory), str(play), "-e",
                               "kontroll_test_entry=%s" % entry, *extra],
                              env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=300)
        out = done.stdout + done.stderr
        return {"rc": done.returncode, "out": out, "seen": list(httpd.seen),
                "recap": {m.group(1): int(m.group(5)) for m in RECAP.finditer(done.stdout)}}

    yield type("Lab", (), {"run": staticmethod(run), "backups": backups})()
    httpd.shutdown()
    httpd.server_close()


def _no_secret_printed(result):
    for secret in SECRETS:
        assert secret not in result["out"], "a credential was printed"
    assert "censored" not in result["out"], "a no_log task failed, and says nothing (I-16's fault)"


def test_a_basic_host_is_checked_with_the_pair_each_name_resolves_and_refused_by_name_without_it(lab):
    """The real check: each host whose pair resolves (the recipe's names, the onboarded per-host names, both with the
    recipe's first, the recipe's empty and the per-host ones taken) authenticates; the host with neither is refused
    by name before any request, the failed assertion naming the username; the host with a wrong password is refused
    with the 401 hint naming both names of each; the API saw one request per host but the refused one, the right
    pair four times and the wrong one once; nothing secret is printed, and nothing is censored. Guards I-16 end to
    end: a missing name failed the request under no_log, censored, and the onboard's names were never read."""
    result = lab.run("check")
    assert result["recap"] == dict({host: 0 for host in AUTHENTICATE}, **{"api-none": 1, "api-wrong": 1}), \
        result["out"][-3000:]
    assert REFUSED % "check" in result["out"] and ASSERTION.search(result["out"]), result["out"][-3000:]
    assert REJECTED in result["out"], result["out"][-3000:]
    assert sorted(path for path, _ in result["seen"]) == ["/api/status"] * 5
    assert sorted(auth == _Api.right for _, auth in result["seen"]) == [False] + [True] * 4
    _no_secret_printed(result)


def test_a_dry_run_sends_no_credential_and_still_refuses_a_missing_pair(lab):
    """The check under --check: the pair is resolved and the host with none is refused by name, as in a real run; no
    request is made (`uri` has no check mode), so the API sees nothing and the wrong password is not found out, and
    each other host says its endpoint was not read; nothing secret is printed. Guards the pair's refusal holding in
    the mandated dry-run, and the dry-run sending no credential."""
    result = lab.run("check", "--check")
    assert result["recap"] == dict({host: 0 for host in AUTHENTICATE + ["api-wrong"]}, **{"api-none": 1}), \
        result["out"][-3000:]
    assert REFUSED % "check" in result["out"] and ASSERTION.search(result["out"]), result["out"][-3000:]
    assert result["out"].count("check endpoint is not read in a dry-run") == 5, result["out"][-3000:]
    assert result["seen"] == []
    _no_secret_printed(result)


def test_a_basic_hosts_backup_captures_with_the_resolved_pair_and_prints_no_secret(lab):
    """The real backup: each host whose pair resolves pulls with the right pair and its capture holds the config;
    the host with none is refused by name before any request and has no capture; the host with a wrong password is
    a failed capture, recorded and not fatal, and has none; the config and the credentials are never printed.
    Guards I-16 in the nightly path, where a censored failure is read by nobody."""
    result = lab.run("backup")
    assert result["recap"] == dict({host: 0 for host in AUTHENTICATE + ["api-wrong"]}, **{"api-none": 1}), \
        result["out"][-3000:]
    assert REFUSED % "capture" in result["out"] and ASSERTION.search(result["out"]), result["out"][-3000:]
    assert sorted(path for path, _ in result["seen"]) == ["/api/backup"] * 5
    captures = sorted(os.listdir(lab.backups))
    assert captures == sorted("%s_127-0-0-1_config.txt" % host for host in AUTHENTICATE), captures
    for name in captures:
        assert open(os.path.join(lab.backups, name), encoding="utf-8").read() == CONFIG
    assert "set hostname probe-device" not in result["out"], "the config was printed"
    _no_secret_printed(result)
