"""backup-configs.yml run for real: the ansible log names each device the run did not back up, and why (I-65).

WHY. tests/unit/test_backup_missed_alert.py reads the alert's query over measured lines; it cannot say which devices a
run names, with which reason, run id and task. These rows run the real play, with ANSIBLE_LOG_PATH set as Semaphore's
jobs now have it (I-82) and the yaml callback the tree's ansible.cfg sets, and read the log they write with the backup
rule's own query (tests/_missed_alerts.py), cut into events as Vector cuts them. The hosts are on a local connection, as
in tests/integration/test_backup_empty_capture_real_run.py: a raw-SSH host that answers a config (no line), one that
answers nothing (failed, the run fails), one that cannot be reached (the row's `down` connection plugin, at a port that
takes no connection: unreachable, the run passes), one the `down` plugin cannot reach at a port that takes the
connection (a session that did not open, as a refused key: failed), one behind a jump host (failed, not probed), one
that is not backup-capable (a logged skip, no line), a class of the row's own whose backup records nothing (not-run),
and two API hosts, whose pull is a real `uri` request to this machine: one at a port that takes no connection
(unreachable), and one at a port that takes the connection and closes it, which speaks no HTTPS (failed). The
network_cli rows do the same against Cisco IOS and RouterOS hosts, where ansible.netcommon and the class's collection
are installed, with a third behind a jump host (failed, not probed). A host that leaves the run in the capture play is
named by the summary's first host (its own row). A dry run prints no line.

It needs ansible-playbook and git: where either is missing every row is skipped. Nothing of the developer's Ansible or
git reaches the play but the collections path its configuration names: its configuration, home and temporary directory,
the inventory, the captures and the log are the row's own.
"""
import functools
import json
import os
import re
import shutil
import socket
import subprocess
import threading

import pytest
import yaml

import _missed_alerts as ma

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not all(shutil.which(tool) for tool in ("ansible-playbook", "git")),
                       reason="needs ansible-playbook and git (it runs backup-configs.yml for real)"),
]

ROOT = ma.ROOT
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")
TASK = 7   # the Semaphore task the rows name the run by (`semaphore_vars`, as Semaphore passes it)
DOWN_PLUGIN = '''from ansible.errors import AnsibleConnectionFailure
from ansible.plugins.connection import ConnectionBase

DOCUMENTATION = """
name: down
short_description: a host that cannot be reached
description: Every use fails as an ssh connection to a host that is down does.
author: kontroll tests
options: {}
"""


class Connection(ConnectionBase):
    transport = "down"
    has_pipelining = False

    def _connect(self):
        raise AnsibleConnectionFailure("down: the host cannot be reached")

    def exec_command(self, cmd, in_data=None, sudoable=True):
        self._connect()

    def put_file(self, in_path, out_path):
        self._connect()

    def fetch_file(self, in_path, out_path):
        self._connect()

    def close(self):
        self._connected = False
'''
GONE = "- name: Include a file the class does not ship\n  ansible.builtin.include_tasks: missing.yml\n"
QUIET = "- name: Read nothing and record nothing\n  ansible.builtin.debug:\n    msg: a backup that records no result\n"


def _listener(address):
    """A socket bound to `address` at a free port, or None where this machine has no such address."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((address, 0))
    except OSError:
        server.close()
        return None
    return server


@pytest.fixture
def ports():
    """A port on this machine that takes no connection, and one that takes each connection and closes it at once, both
    at `host`: a second loopback address (127.0.0.2, where this machine has one, as Linux does; else 127.0.0.1), which
    the hosts that probe them are given. So a probe of the controller's own loopback address in place of the host's
    finds no listener: a mutant that probed 127.0.0.1 passed this fixture's first form, whose hosts were all there
    (I-65's review, N-2)."""
    server = _listener("127.0.0.2") or _listener("127.0.0.1")
    host = server.getsockname()[0]
    probe = socket.socket()
    probe.bind((host, 0))
    closed = probe.getsockname()[1]
    probe.close()
    server.listen(16)
    server.settimeout(0.5)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield type("Ports", (), {"host": host, "closed": closed, "plain": server.getsockname()[1]})()
    stop.set()
    thread.join(5)
    server.close()


def _raw(command, **more):
    return dict({"ansible_host": "192.0.2.31", "device_role": "backend_raw_ssh", "backend_backup_command": command},
                **more)


def _api(recipe, host):
    return {"ansible_host": host, "device_role": "backend_api", "backend_api_recipe": recipe,
            "t_api_token": "test-only-token"}


@functools.lru_cache(maxsize=None)
def _collections_path():
    """The collections path this machine's Ansible reads, as its own configuration sets it: the lab's home is the
    row's own, which would hide the collections an image installs under the user's home."""
    p = subprocess.run(["ansible-config", "dump", "--format", "json"], capture_output=True, text=True, timeout=120)
    (row,) = [r for r in json.loads(p.stdout) if isinstance(r, dict) and r.get("name") == "COLLECTIONS_PATHS"]
    return os.pathsep.join(row["value"])


def _lab(tmp_path, ports, hosts):
    """The inventory of `hosts`; an Ansible home and configuration of the row's own, with the `down` connection
    plugin and, first on the roles path, a copy of backend_api beside the row's two API recipes (the role reads its
    recipes beside its roles directory), the class `quiet_class`, whose backup records nothing, and the class
    `gone_class`, whose backup includes a file it does not ship (the host leaves the run in the capture play); a
    captures directory (the play makes its history); and the run's log path."""
    home = tmp_path / "ansible-home"
    (home / "connection_plugins").mkdir(parents=True)
    (home / "connection_plugins" / "down.py").write_text(DOWN_PLUGIN, encoding="utf-8")
    own = tmp_path / "ansible"
    shutil.copytree(os.path.join(ROOT, "ansible", "roles", "backend_api"), own / "roles" / "backend_api",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (own / "roles" / "quiet_class" / "tasks").mkdir(parents=True)
    (own / "roles" / "quiet_class" / "tasks" / "backup.yml").write_text(QUIET, encoding="utf-8")
    (own / "roles" / "gone_class" / "tasks").mkdir(parents=True)
    (own / "roles" / "gone_class" / "tasks" / "backup.yml").write_text(GONE, encoding="utf-8")
    recipes = own / "backends" / "api" / "recipes"
    recipes.mkdir(parents=True)
    for name, port in (("t-closed", ports.closed), ("t-plain", ports.plain)):
        (recipes / (name + ".yml")).write_text(yaml.safe_dump({
            "name": name, "port": port, "validate_certs": False, "auth": {"type": "bearer", "token_var": "t_api_token"},
            "backup": {"method": "GET", "path": "/backup", "label": "TAPI-" + name, "suffix": "config"}}),
            encoding="utf-8")
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = %s:%s\nfilter_plugins = %s\nconnection_plugins = %s\n"
                   "retry_files_enabled = False\ninterpreter_python = auto_silent\nhost_key_checking = False\n"
                   % (own / "roles", os.path.join(ROOT, "ansible", "roles"),
                      os.path.join(ROOT, "ansible", "filter_plugins"), home / "connection_plugins"), encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "hosts": hosts}}), encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_COLLECTIONS_PATH=_collections_path(), ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home),
               ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_LOG_PATH=str(tmp_path / "ansible.log"), ANSIBLE_CALLBACK_RESULT_FORMAT="yaml",
               ANSIBLE_PERSISTENT_CONNECT_TIMEOUT="15", ANSIBLE_PERSISTENT_COMMAND_TIMEOUT="15",
               KONTROLL_SSH_KEY_DIR=str(tmp_path / "keys"), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "backups": str(backups),
                            "log": str(tmp_path / "ansible.log"), "env": env})()


def _play(lab, *argv):
    """Run backup-configs.yml against the lab as Semaphore's task TASK; return its exit code, output and log."""
    extra = {"config_backup_dir": lab.backups, "semaphore_vars": {"task_details": {"id": TASK}}}
    p = subprocess.run(["ansible-playbook", "-i", lab.inventory, PLAYBOOK, "-e", json.dumps(extra), *argv],
                       cwd=lab.base, env=lab.env, capture_output=True, text=True, timeout=900)
    with open(lab.log, encoding="utf-8") as fh:
        log = fh.read()
    return p.returncode, p.stdout + p.stderr, log


def _failed(out):
    """The hosts the play's recap counts a failure for."""
    recap = (r"^(\S+)\s*:\s*ok=\d+\s+changed=\d+\s+"
             r"unreachable=\d+\s+failed=([1-9]\d*)")
    return {m.group(1) for m in re.finditer(recap, out, re.M)}


def _series(log):
    """The series the backup rule counts in the log, by device, and the run ids the log's stamp printed."""
    found, stamps = {}, set()
    for event in ma.events(log):
        labels = ma.read(ma.BACKUP, {"message": event, "labels": {"source": "internal", "service": "ansible",
                                                                    "run_id": ma.run_id(event)}})
        if labels is not None:
            assert labels["device"] not in found, (labels, log[-4000:])
            found[labels["device"]] = labels
        elif "msg: kontroll_run_id=" in event:
            stamps.add(ma.run_id(event))
    return found, stamps


def _said(out, line):
    """Whether the play's output holds `line`, read across the yaml callback's fold (it breaks a long message at a
    space, onto an indented line)."""
    return line in " ".join(out.split())


def _want(stamp, **devices):
    return {d: {"device": d, "class": c, "reason": r, "run_id": stamp, "semaphore_task": str(TASK)}
            for d, (c, r) in devices.items()}


def _hosts(ports):
    return {"ap-good": _raw("printf 'config system\\n'"), "ap-empty": _raw("true", ansible_host="192.0.2.32"),
            "ap-down": _raw("printf 'config system\\n'", ansible_host="192.0.2.34", ansible_connection="down"),
            "ap-refused": _raw("printf 'config system\\n'", ansible_host=ports.host, ansible_port=ports.plain,
                               ansible_connection="down"),
            "ap-jump": _raw("printf 'config system\\n'", ansible_host="192.0.2.37", ansible_connection="down",
                            ansible_ssh_common_args="-o ProxyJump=jump.invalid"),
            "ap-skip": _raw("printf 'config system\\n'", ansible_host="192.0.2.35", backup_capable=False),
            "quiet": {"ansible_host": "192.0.2.36", "device_role": "quiet_class"},
            "api-closed": dict(_api("t-closed", ports.host), ansible_port=ports.plain),
            "api-plain": _api("t-plain", ports.host)}


def test_a_real_run_names_each_device_it_did_not_back_up_with_its_reason(tmp_path, ports):
    """A real run's log holds one miss per device it did not back up, which the backup rule reads as that device's
    series: ap-empty (its read answered nothing), api-plain (its API port took the connection and gave no HTTP answer),
    ap-refused (its port took the connection, and its session did not open) and ap-jump (behind a jump host, so not
    probed) failed; ap-down (no connection) and api-closed (its API port took none) unreachable; quiet (its backup
    recorded nothing) not-run; each with its class, the run id the log's one stamp printed and the Semaphore task.
    ap-good (captured) and ap-skip (a logged skip) have none. The run fails, at its summary, for the four failed hosts
    alone; the summary says each unreachable host was not run, unreachable, and quiet not run, with no result. Guards
    the S1-S3 reads of I-65's review: Ansible calls a refused key unreachable, so before the capture play's probe a
    rotated key on an SSH device read as a device that is down and passed the run; and a probe of a jump host's own
    address, which the controller cannot reach, would say the same of any read that failed there. And api-closed's own
    port (its `ansible_port`) takes the connection: the capture play does not probe again a host its class probed, which
    would call a device whose API port takes no connection failed. Guards I-65: a device not reached failed the run, or
    passed it unnamed; a run's misses were one line the alert never fired on; and a miss must name the run the log's
    other lines name."""
    lab = _lab(tmp_path, ports, _hosts(ports))
    rc, out, log = _play(lab)
    found, stamps = _series(log)
    assert len(stamps) == 1, (stamps, log[-4000:])
    (stamp,) = stamps
    assert found == _want(stamp, **{"ap-empty": ("backend_raw_ssh", "failed"), "ap-down": ("backend_raw_ssh",
                                                                                            "unreachable"),
                                    "quiet": ("quiet_class", "not-run"), "api-closed": ("backend_api", "unreachable"),
                                    "api-plain": ("backend_api", "failed"),
                                    "ap-refused": ("backend_raw_ssh", "failed"),
                                    "ap-jump": ("backend_raw_ssh", "failed")}), log[-6000:]
    assert rc == 2 and _failed(out) == {"ap-empty", "api-plain", "ap-refused", "ap-jump"}, out[-4000:]
    assert _said(out, "ap-down (backend_raw_ssh): not run — unreachable: the device took no TCP connection on port "
                      "22,")
    assert _said(out, "ap-refused (backend_raw_ssh): FAILED: the device took a TCP connection on port %d, and its "
                      "session did not open" % ports.plain)
    assert _said(out, "ap-jump (backend_raw_ssh): FAILED: the device is reached through a jump host, which no probe "
                      "reaches, and its session did not open")
    for host, role in (("ap-down", "backend_raw_ssh"), ("api-closed", "backend_api")):
        assert re.search(r"%s \(%s\): not run — unreachable: " % (host, role), out), out[-4000:]
    assert _said(out, "quiet (quiet_class): not run — no result: its backup recorded none")
    assert "test-only-token" not in log and "test-only-token" not in out


def test_a_dry_run_names_no_device(tmp_path, ports):
    """A `--check --diff` run of the same hosts prints no miss, and passes: the API pulls are not made (`uri` has no
    check mode), the raw-SSH reads are previews, and a host not reached or with no result is not paged for a run
    that read nothing. Guards a dry run, which CLAUDE.md asks for before every state-changing play, paging someone
    each time it runs (I-65)."""
    lab = _lab(tmp_path, ports, _hosts(ports))
    rc, out, log = _play(lab, "--check", "--diff")
    assert "KONTROLL-BACKUP-MISSED" not in log and "KONTROLL-BACKUP-MISSED" not in out, log[-4000:]
    assert rc == 0 and _failed(out) == set(), out[-4000:]
    found, stamps = _series(log)
    assert found == {} and len(stamps) == 1


def _network_cli(env, network_os):
    """Whether the lab's Ansible (its environment `env`) has the network_cli connection and `network_os`'s cliconf."""
    for kind, name in (("connection", "ansible.netcommon.network_cli"), ("cliconf", network_os)):
        p = subprocess.run(["ansible-doc", "-t", kind, "-j", name], env=env, capture_output=True, text=True,
                           timeout=120)
        if p.returncode != 0 or name not in p.stdout:
            return False
    return True


@pytest.mark.parametrize("role, network_os", [("cisco_ios", "cisco.ios.ios"),
                                              ("routeros", "community.routeros.routeros")])
def test_a_network_device_not_reached_is_unreachable_and_one_that_answered_badly_failed(tmp_path, ports, role,
                                                                                         network_os):
    """Three hosts of a network_cli class over network_cli, as the class reads a real device (Cisco IOS, through the CLI
    backend; RouterOS, whose export has its own rescue): one at a port that takes no connection is named unreachable,
    and one at a port that takes the connection and closes it (no SSH) is named failed, which fails the run; each miss
    names the run by the id given with `-e kontroll_run_id`. Guards the rescue each read gained (I-65): under
    network_cli a device that takes no connection fails the read as a task, not as unreachable, so before the probe it
    was recorded FAILED and failed the run; and a probe that read a session that failed as no connection would hide a
    bad credential. A third, at the closed port behind a proxy command (a jump host), is failed and not probed: a probe
    of its own address, which the controller cannot reach past a jump host, would call any read that failed there a
    device not reached. Where the image has no SSH library for network_cli (neither paramiko nor ansible-pylibssh: the
    validate images), each read fails for the missing library before it connects, so there the row holds the probe and
    the records alone; with one (the runner image's ansible-core 2.20 and paramiko) the read opens the connection as it
    would to a device."""
    cli = {"ansible_connection": "ansible.netcommon.network_cli", "ansible_network_os": network_os,
           "ansible_host": ports.host, "ansible_user": "t", "ansible_password": "test-only-password",
           "device_role": role}
    lab = _lab(tmp_path, ports, {"sw-closed": dict(cli, ansible_port=ports.closed),
                                 "sw-plain": dict(cli, ansible_port=ports.plain),
                                 "sw-jump": dict(cli, ansible_port=ports.closed,
                                                 ansible_paramiko_proxy_command="false %h %p")})
    if not shutil.which("ansible-doc") or not _network_cli(lab.env, network_os):
        pytest.skip("needs ansible.netcommon's network_cli and %s" % network_os)
    rc, out, log = _play(lab, "-e", "kontroll_run_id=run-i65")
    found, stamps = _series(log)
    assert stamps == {"run-i65"}, log[-4000:]
    assert found == _want("run-i65", **{"sw-closed": (role, "unreachable"), "sw-plain": (role, "failed"),
                                        "sw-jump": (role, "failed")}), log[-6000:]
    assert rc == 2 and _failed(out) == {"sw-plain", "sw-jump"}, out[-4000:]
    assert _said(out, "sw-jump (%s): FAILED: the device is reached through a jump host, which no probe reaches," % role)
    assert "test-only-password" not in log and "test-only-password" not in out


def test_a_device_that_left_the_run_before_its_summary_is_named(tmp_path, ports):
    """A host that fails in the capture play (its class's backup includes a file the class does not ship, so its
    include cannot be loaded and the rescue fails it) is in no later play; the summary's first host names it once:
    its summary line says it left the run before its summary, and its miss names it failed, with its class and the
    run's id. The run fails for it alone. A dry run names it in no miss. Guards S2 of I-65's review: such a host was
    in no summary line and no miss, so the per-device alert never named it (only the job's alert said the run
    failed)."""
    hosts = {"ap-good": _raw("printf 'config system\\n'"),
             "gone": {"ansible_host": "192.0.2.38", "device_role": "gone_class"}}
    lab = _lab(tmp_path, ports, hosts)
    rc, out, log = _play(lab, "-e", "kontroll_run_id=run-gone")
    found, stamps = _series(log)
    assert stamps == {"run-gone"}, log[-4000:]
    assert found == _want("run-gone", gone=("gone_class", "failed")), log[-6000:]
    assert rc == 2 and _failed(out) == {"gone"}, out[-4000:]
    assert _said(out, "gone (gone_class): FAILED: it left the run before its summary")
    rc, out, log = _play(_lab(tmp_path / "check", ports, hosts), "--check", "--diff")
    assert "KONTROLL-BACKUP-MISSED" not in log and rc == 2 and _failed(out) == {"gone"}, out[-4000:]
