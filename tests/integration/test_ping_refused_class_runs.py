"""ping.yml run for real with a host whose class has no `check` entrypoint installed: the host is named REFUSED, the
run still removes its SSH keys and fails, and the other hosts are probed and checked as before (I-83).

WHY. ping.yml's reach play includes the host's class (`device_role`) with `tasks_from: check` inside the probe's
block. Where that entrypoint was not installed, the include could not be loaded, which is no task failure: Ansible
dropped the host from every later play (no such role: alone, the run ended there with rc 0), ended the whole run (a
role with no tasks/check.yml, rc 4; no `device_role` on 2.18, rc 1) or reported it UNREACHABLE (no `device_role` on
2.19), so the run's keys were left in their per-run directory where no later play ran; and a class ansible-core loads
as a role that runs nothing (an empty `device_role`, a role with no tasks/ directory, a check with no task in it) was
reported OK. The reach play now refuses such a host first, in a block of its own whose rescue records the refusal
(the probe's rescue would report it UNREACHABLE); the summary names it REFUSED, and the run's last play, after the key
cleanup, fails it. The reach play also overrides a caller's any_errors_fatal, which otherwise failed the other hosts
once one host's probe or refusal failed. tests/unit/test_ping_entrypoint_refusal.py holds that shape; these rows run
it.

Hosts on a local connection, at 127.0.0.1: one of the generic raw-SSH backend probed at a port the row listens on
(reachable: its check runs `true`), one of that backend at a port nothing listens on (unreachable), and one of each
kind the check refuses, at the listening port (so a refused host that reached the probe would reach the include
too); and one whose role's check includes a file that is missing, which the check does not refuse (the residue).
The run's key directory holds a stale key file for each host before the run.

It needs ansible-playbook: where it is missing the rows are skipped (the gate's images and CI's validate job have
it). Nothing of the developer's Ansible reaches the play: its configuration, home and temporary directory, the
inventory and the key directory are the row's own.
"""
import os
import re
import shutil
import socket
import subprocess

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="needs ansible-playbook (it runs ping.yml)"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "ping.yml")
ROLE = {  # host: its device_role (None: it names none)
    "p-good": "backend_raw_ssh",        # reachable, and its check runs
    "p-down": "backend_raw_ssh",        # nothing listens at its port: UNREACHABLE, and the run still passes
    "p-missing": "zz_no_such_class",    # no installed role answers it
    "p-noentry": "zz_no_check",         # its role has no tasks/check.yml
    "p-notasks": "zz_no_tasks",         # its role has no tasks/ directory: ansible-core loads it, and it runs nothing
    "p-norole": None,                   # it names no device_role
    "p-empty": "",                      # an empty device_role: ansible-core loads it as a role that runs nothing
    "p-emptycheck": "zz_empty_check",   # its role's tasks/check.yml holds comments only: it loads and runs nothing
    "p-broken": "zz_broken_check",      # its role's check includes a missing file: not refused, cannot load
}
REFUSED_HOSTS = ["p-missing", "p-noentry", "p-notasks", "p-norole", "p-empty", "p-emptycheck"]
EXTRA_ROLES = {  # the lab's second roles_path entry
    "zz_no_check/tasks/main.yml": "- ansible.builtin.debug:\n    msg: not a check\n",
    "zz_no_tasks/defaults/main.yml": "zz_no_tasks_default: 1\n",
    "zz_empty_check/tasks/check.yml": "---\n# tasks file for zz_empty_check\n",
    "zz_broken_check/tasks/check.yml": "- ansible.builtin.include_tasks: no_such_file.yml\n",
}
PLAYS = ("Reach the fleet (per-class liveness)", "Reachability summary", "Remove the per-run ephemeral SSH key dir",
         "Fail the run for a host whose class has no check entrypoint installed")
SUMMARY = '"msg": "%s: %s"'
BROKEN = ('"msg": "p-broken: the check entrypoint of the class zz_broken_check could not be loaded (a file or role it '
          'includes is missing, or the collection it names is not installed), so the run fails here."')
REFUSED = "REFUSED: its class has no check entrypoint installed"


@pytest.fixture
def ports():
    """(open, closed): a port the row listens on at 127.0.0.1 for the run, and one nothing listens on."""
    with socket.create_server(("127.0.0.1", 0), backlog=64) as listener:
        with socket.create_server(("127.0.0.1", 0)) as gone:
            closed = gone.getsockname()[1]
        yield listener.getsockname()[1], closed


def _lab(tmp_path, ports, hosts):
    """The inventory of `hosts` (p-down at the closed port, every other host at the open one), an Ansible home and
    configuration of the row's own (its roles path the repo's roles, then EXTRA_ROLES), and the run's key directory
    holding a stale key for each host."""
    home = tmp_path / "ansible-home"
    home.mkdir(parents=True)
    extra = tmp_path / "extra-roles"
    for rel, content in EXTRA_ROLES.items():
        (extra / rel).parent.mkdir(parents=True, exist_ok=True)
        (extra / rel).write_text(content, encoding="utf-8")
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = %s:%s\nfilter_plugins = %s\nretry_files_enabled = False\n"
                   "interpreter_python = auto_silent\n"
                   % (os.path.join(ROOT, "ansible", "roles"), extra, os.path.join(ROOT, "ansible", "filter_plugins")),
                   encoding="utf-8")
    entries = {}
    for host in hosts:
        entries[host] = {"ansible_host": "127.0.0.1", "mgmt_port": ports[1] if host == "p-down" else ports[0]}
        if ROLE[host] is not None:
            entries[host]["device_role"] = ROLE[host]
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "hosts": entries}}), encoding="utf-8")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    for host in hosts:
        (keys / ("%s.key" % host)).write_text("not a key: a stale file the cleanup must remove\n", encoding="utf-8")
        os.chmod(keys / ("%s.key" % host), 0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(keys), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "keys": str(keys), "env": env})()


def _play(lab, *extra):
    """Run ping.yml against the lab's hosts (with `extra` arguments); return its exit code, its output and each host's
    recap as (failed, rescued)."""
    p = subprocess.run(["ansible-playbook", "-i", lab.inventory, PLAYBOOK, *extra], cwd=lab.base, env=lab.env,
                       capture_output=True, text=True, timeout=600)
    recap = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in
             re.finditer(r"^(\S+)\s*:\s*ok=\d+\s+changed=\d+\s+unreachable=\d+\s+failed=(\d+)\s+skipped=\d+\s+"
                         r"rescued=(\d+)", p.stdout, re.M)}
    return p.returncode, p.stdout + p.stderr, recap


def _task(out, name):
    """The output of the task `name` (from its banner to the next banner)."""
    at = out.find("TASK [%s] " % name)
    assert at != -1, (name, out)
    end = out.find("\nTASK [", at + 1)
    return out[at:] if end == -1 else out[at:end]


@pytest.mark.parametrize("host", REFUSED_HOSTS)
def test_a_class_whose_check_entrypoint_is_not_installed_is_refused_and_its_keys_removed(tmp_path, ports, host):
    """A dry run, then a real run, of one host whose class has no `check` entrypoint installed (its `device_role`
    names no installed role; names a role with no tasks/check.yml, with no tasks/ directory, or whose check holds
    comments only; is missing; is empty)
    each fail with rc 2: the refusal names the host, the host skips the probe, the summary names it REFUSED, and the
    last play fails it after the key-cleanup play (its recap: one rescued, one failed); the real run removes the run's
    key directory, stale key and all. Guards I-83: such an include could not be loaded, which is no task failure, so
    Ansible dropped the host from every later play or ended the whole run before the key cleanup, leaving the run's
    keys on disk with no summary line; or (no tasks/, an empty name, an empty check) it loaded a role that runs nothing
    and the summary said OK, a host whose class checks nothing reported reachable (the I-83 reads' B S1). (Measured
    on the parent: a missing role alone ended with rc 0 and no summary line; no `device_role` was UNREACHABLE on 2.19
    and ended the run on 2.18.)"""
    lab = _lab(tmp_path, ports, [host])
    for extra in (("--check", "--diff"), ()):
        rc, out, recap = _play(lab, *extra)
        assert rc == 2, (extra, rc, out)
        assert recap == {host: (1, 1)}, (extra, recap, out)
        assert '"msg": "%s: the class %s has no `check` entrypoint installed' % (
            host, "(none: the host names no device_role)" if ROLE[host] is None else ROLE[host]) in out, (extra, out)
        assert "skipping: [%s]" % host in _task(out, "TCP reachability probe from the control node"), (extra, out)
        assert SUMMARY % (host, REFUSED) in out, (extra, out)
        at = [out.find("PLAY [%s]" % play) for play in PLAYS]
        assert -1 not in at and at == sorted(at), (extra, at, out)
        assert "fatal: [%s]" % host in _task(out, "Fail a host whose class has no check entrypoint installed"), out
    assert re.search(r'"msg": "kontroll_run_id=[0-9]{8}T[0-9]{6}Z"', out), out
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


def test_a_refused_class_beside_good_and_unreachable_hosts_leaves_them_reported(tmp_path, ports):
    """A real run of p-good (reachable), p-down (nothing listens at its port) and p-noentry (its role has no
    tasks/check.yml) fails with rc 2 for p-noentry alone: p-good's check runs and it is OK, p-down is UNREACHABLE
    (rescued, not failed), p-noentry is REFUSED, and the key directory is gone. Guards I-83 where the class is one
    host's among several: a role with no entry ended the whole run (rc 4), the other hosts' checks, their summary
    lines and the key cleanup with it; and the refusal against failing or rescuing any host but its own."""
    lab = _lab(tmp_path, ports, ["p-good", "p-down", "p-noentry"])
    rc, out, recap = _play(lab)
    assert rc == 2, (rc, out)
    assert recap == {"p-good": (0, 0), "p-down": (0, 1), "p-noentry": (1, 1)}, (recap, out)
    assert "ok: [p-good]" in _task(out, "backend_raw_ssh : Liveness via raw command"), out
    for host, said in (("p-good", "OK"), ("p-down", "UNREACHABLE"), ("p-noentry", REFUSED)):
        assert SUMMARY % (host, said) in out, (host, out)
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


def test_a_run_that_refuses_no_class_passes_with_an_unreachable_host(tmp_path, ports):
    """A real run of p-good and p-down passes (rc 0): p-good is OK, p-down UNREACHABLE, the last play fails no host,
    and the key directory is gone. Guards the last play against failing a host the refusal did not record (one that
    is unreachable, or reachable): a ping with a down device passes, as the fresh-node check and a scheduled ping
    read it, and only a class with no check entrypoint fails the run."""
    lab = _lab(tmp_path, ports, ["p-good", "p-down"])
    rc, out, recap = _play(lab)
    assert rc == 0, (rc, out)
    assert recap == {"p-good": (0, 0), "p-down": (0, 1)}, (recap, out)
    for host, said in (("p-good", "OK"), ("p-down", "UNREACHABLE")):
        assert SUMMARY % (host, said) in out, (host, out)
    assert "PLAY [%s]" % PLAYS[-1] in out, out
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


def test_a_class_whose_check_cannot_be_loaded_fails_the_run_in_the_probes_rescue(tmp_path, ports):
    """A dry run and a real run whose only host's class is installed but whose check includes a file that is missing
    each fail with rc 2, the probe's rescue failing the host with a message that names it and its class; neither runs
    the summary, and the run's keys stay (the residue: the host leaves every later play, the cleanup among them).
    Guards the load failure the entrypoint check cannot see: such an include is no task failure, so the host came to
    the probe's rescue with no failed task and left every later play, and with it alone the run ended with rc 0, a
    ping that checked nothing reported as passed to a scheduler that reads the exit code."""
    lab = _lab(tmp_path, ports, ["p-broken"])
    for extra in (("--check", "--diff"), ()):
        rc, out, recap = _play(lab, *extra)
        assert rc == 2, (extra, rc, out)
        assert BROKEN in out, (extra, out)
        assert "PLAY [Reachability summary]" not in out, (extra, out)
    assert os.path.exists(lab.keys), out


def test_a_class_whose_check_cannot_be_loaded_beside_a_good_host_leaves_the_good_one_reported(tmp_path, ports):
    """A real run of p-broken (its class's check includes a missing file) and p-good fails with rc 2: p-good's check
    runs and it is OK, the key directory is gone, and p-broken gets the probe's rescue failure and no summary line.
    Guards the rescue's `fail` against stopping the other hosts: it fails its own host only, and the good host's
    check, the summary and the key cleanup still run."""
    lab = _lab(tmp_path, ports, ["p-broken", "p-good"])
    rc, out, recap = _play(lab)
    assert rc == 2, (rc, out)
    assert recap["p-good"] == (0, 0) and recap["p-broken"][0] >= 1, (recap, out)
    assert BROKEN in out, out
    assert "ok: [p-good]" in _task(out, "backend_raw_ssh : Liveness via raw command"), out
    assert SUMMARY % ("p-good", "OK") in out, out
    assert not re.search(r'"msg": "p-broken: (OK|UNREACHABLE|REFUSED)', out), out
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


@pytest.mark.parametrize("other", ["p-down", "p-noentry"])
def test_a_callers_any_errors_fatal_leaves_every_host_reported(tmp_path, ports, other):
    """A real run of p-good beside p-down (nothing listens at its port) or p-noentry (its role has no tasks/check.yml),
    under a caller's `ANSIBLE_ANY_ERRORS_FATAL=True`: p-good's check runs and it is OK, the other host is UNREACHABLE
    or REFUSED, the key directory is gone, and the run passes (p-down) or fails for p-noentry alone (rc 2). Guards the
    reach play's `any_errors_fatal: false` (the I-83 reads' B S3, A N4): without it the setting failed every other host
    once one host's probe or refusal failed, rescue or not, so beside p-down, p-good was moved into the probe's rescue,
    whose `fail` then ended the run before the key cleanup with the keys left (the parent, which had no such `fail`,
    removed them), and beside p-noentry, p-good was dropped from every later play with no summary line."""
    lab = _lab(tmp_path, ports, ["p-good", other])
    lab.env["ANSIBLE_ANY_ERRORS_FATAL"] = "True"
    rc, out, recap = _play(lab)
    assert rc == (0 if other == "p-down" else 2), (rc, out)
    assert recap["p-good"] == (0, 0), (recap, out)
    assert "ok: [p-good]" in _task(out, "backend_raw_ssh : Liveness via raw command"), out
    for host, said in (("p-good", "OK"), (other, "UNREACHABLE" if other == "p-down" else REFUSED)):
        assert SUMMARY % (host, said) in out, (host, out)
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)
