"""wire-logging.yml run for real with the shipped syslog-push wiring over network_cli: a device that takes no connection
is not run, passes the run and is named by the run's report; one that takes the connection and fails, or sits behind
a jump host, fails the run and is named by its summary (I-103).

WHY. The device play tolerates a down host (`ignore_unreachable`: a down host is tolerated, not fatal, CLAUDE.md), but
under network_cli a device that takes no connection fails the push (`cli_config`) as a task, not as unreachable: one
network device down failed the whole wiring run, as the backup's read did before I-65. The CLI backend's push now
probes the device's port after a failed push. These rows run the shipped playbook, the shipped logging_syslog_push and
backend_netcommon_cli roles and the shipped logging methods from a copy of the tree, with a device class of the row's
own (group `g`), over devices at this machine's ports (conftest.py's `ports`): one that takes no connection, one that
takes the connection and closes it (no SSH answers), and one at the closed port behind a proxy command (a jump host),
real and `--check --diff` (the dry run CLAUDE.md asks for first). Where the image has no SSH library for network_cli
(neither paramiko nor ansible-pylibssh: the validate images, CI's legs, I-104), each push fails for the missing library
before it connects, so there the rows hold the probe and the records alone; with one, the push opens the connection
as it would to a device. They need ansible-playbook, ansible.netcommon's network_cli and cisco.ios's cliconf, else
they are skipped. Nothing of the developer's Ansible reaches the run but the collections path its configuration names.
"""
import os
import re
import shutil
import subprocess

import pytest
import yaml

import _network_lab

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not all(shutil.which(t) for t in ("ansible-playbook", "ansible-doc")),
                       reason="needs ansible-playbook and ansible-doc (it runs wire-logging.yml for real)"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NETWORK_OS = "cisco.ios.ios"
PASSWORD = "test-only-password"
FAIL_TASK = "Fail a push to a device that took the connection or sits behind a jump host (its message says which)"


def _device(ports, port, **more):
    return dict({"ansible_connection": "ansible.netcommon.network_cli", "ansible_network_os": NETWORK_OS,
                 "ansible_host": ports.host, "ansible_port": port, "ansible_user": "t",
                 "ansible_password": PASSWORD}, **more)


def _wire(tmp_path, hosts, *args):
    """Run wire-logging.yml with `-e role=logging_syslog_push -e target=probe` (and `args`) from a copy of the tree
    (its playbooks, logging methods and the two roles the push runs, a class `probe` whose group is `g`, and an
    instance.yml naming the collector) over `hosts` in `g`. Return the exit code, the output, and whether the run's
    key dir is gone."""
    tree = tmp_path / "t"
    shutil.copytree(os.path.join(ROOT, "ansible", "playbooks"), str(tree / "ansible" / "playbooks"))
    shutil.copytree(os.path.join(ROOT, "logging"), str(tree / "logging"))
    for role in ("logging_syslog_push", "backend_netcommon_cli"):
        shutil.copytree(os.path.join(ROOT, "ansible", "roles", role), str(tree / "ansible" / "roles" / role))
    (tree / "modules" / "probe").mkdir(parents=True)
    (tree / "modules" / "probe" / "module.yml").write_text("inventory_group: g\n")
    (tree / "instance").mkdir()
    (tree / "instance" / "instance.yml").write_text("mgmt_ip: 192.0.2.5\n")
    (tree / "inv.yml").write_text(yaml.safe_dump({"all": {"children": {"g": {"hosts": hosts}}}}))
    home = tmp_path / "ansible-home"
    home.mkdir()
    (tree / "ansible" / "ansible.cfg").write_text("[defaults]\nroles_path = roles\nretry_files_enabled = False\n"
                                                  "host_key_checking = False\n")
    keys = tmp_path / "keys"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(tree / "ansible" / "ansible.cfg"), ANSIBLE_HOME=str(home),
               ANSIBLE_COLLECTIONS_PATH=_network_lab.collections_path(), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_PERSISTENT_CONNECT_TIMEOUT="15", ANSIBLE_PERSISTENT_COMMAND_TIMEOUT="15",
               KONTROLL_SSH_KEY_DIR=str(keys), PYTHONDONTWRITEBYTECODE="1")
    if not _network_lab.has_network_cli(env, NETWORK_OS):
        pytest.skip("needs ansible.netcommon's network_cli and %s" % NETWORK_OS)
    cmd = ["ansible-playbook", "-i", str(tree / "inv.yml"), "playbooks/wire-logging.yml",
           "-e", "role=logging_syslog_push", "-e", "target=probe", *args]
    p = subprocess.run(cmd, cwd=str(tree / "ansible"), env=env, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    assert "PLAY RECAP" in out, out[-4000:]
    return p.returncode, out, not keys.exists()


def _task(out, name):
    """The output of the task `name` (from its banner, with or without its role's name, to the next banner or play)."""
    start = re.search(r"^TASK \[(?:[^\]\n]* : )?%s\]" % re.escape(name), out, re.M).start()
    ends = [i for i in (out.find("\nTASK [", start + 1), out.find("\nPLAY ", start + 1)) if i != -1]
    return out[start:min(ends) if ends else len(out)]


def _not_run(host, port, text):
    """Whether `text` holds the report's line for `host`: not run, the port that took no connection (the dash between
    is left out: a callback may print it escaped)."""
    head, tail = '"msg": "%s: syslog-push: not run ' % host, (
        " unreachable: the device took no TCP connection on port %d, so nothing was pushed\"" % port)
    return any(line.find(head) != -1 and line.find(tail) > line.find(head) for line in text.splitlines())


def _failed(host):
    return '"msg": "%s: FAILED: the wiring stopped at the task \\"%s\\""' % (host, FAIL_TASK)


@pytest.mark.parametrize("args", [(), ("--check", "--diff")], ids=["real", "check"])
def test_a_network_device_that_takes_no_connection_is_not_run_and_the_run_passes(tmp_path, ports, args):
    """One device, at a port that takes no connection: the run passes (rc 0), the report after the key cleanup names it
    not run with the port, nothing is named FAILED, and the key dir is gone; in a real run and in a `--check --diff`
    dry run alike. Guards I-103: the push failed as a task, so the device was recorded a failed wiring and one network
    device down failed the run (CLAUDE.md: never let one unreachable host fail a fleet-wide run); and the probe left to
    a dry run's check mode (it is a TCP connect, which changes nothing), where it would probe nothing and the device
    would be named FAILED."""
    rc, out, gone = _wire(tmp_path, {"sw-closed": _device(ports, ports.closed)}, *args)
    assert rc == 0, out[-4000:]
    assert _not_run("sw-closed", ports.closed, _task(out, "Name a device not run (it took no connection; the run "
                                                          "passes)")), out[-4000:]
    assert "FAILED: the wiring" not in out and gone, out[-4000:]
    assert PASSWORD not in out


@pytest.mark.parametrize("args", [(), ("--check", "--diff")], ids=["real", "check"])
def test_a_device_that_took_the_connection_or_sits_behind_a_jump_host_fails_the_run(tmp_path, ports, args):
    """Three devices: one at a port that takes no connection, one at a port that takes the connection and closes it,
    and one at the closed port behind a proxy command (a jump host). The run fails (rc 2) at its summary, after the key
    cleanup (the key dir is gone): the summary names the second and the third FAILED at the backend's `fail`, whose
    messages say the port took the connection and that a jump host no probe reaches; the report names the first not
    run, and only it; no host's password is in the output. Guards the probe read the wrong way (a push that failed on a
    device that answered, a refused credential, passing as a device not reached), the jump host probed (the
    controller cannot reach its address, so every push that failed there would pass as not reached), and a device not
    reached named FAILED beside them (I-103)."""
    hosts = {"sw-closed": _device(ports, ports.closed), "sw-plain": _device(ports, ports.plain),
             "sw-jump": _device(ports, ports.closed, ansible_paramiko_proxy_command="false %h %p")}
    rc, out, gone = _wire(tmp_path, hosts, *args)
    assert rc == 2 and gone, out[-4000:]
    summary = _task(out, "Fail a host whose wiring failed, naming the task it stopped at")
    assert _failed("sw-plain") in summary and _failed("sw-jump") in summary, out[-4000:]
    assert '"msg": "sw-closed: FAILED' not in summary and "skipping: [sw-closed]" in summary, summary
    push = _task(out, FAIL_TASK)
    assert ("sw-plain: the device took a TCP connection on port %d, and the push of syslog-push failed" % ports.plain
            in push), push
    assert "sw-jump: the device is reached through a jump host, which no probe reaches, and the push" in push, push
    report = _task(out, "Name a device not run (it took no connection; the run passes)")
    assert _not_run("sw-closed", ports.closed, report), report
    assert "sw-plain:" not in report and "sw-jump:" not in report, report
    assert PASSWORD not in out
