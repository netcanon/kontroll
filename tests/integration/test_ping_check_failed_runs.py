"""ping.yml run for real with a reachable host whose class check fails: the summary names it CHECK FAILED and the task
its check stopped at, and the run fails after the key cleanup; a down host stays UNREACHABLE and tolerated, unless
the caller requires every host reachable, as galaxy.py's onboard verify does; a dry run says its port was not probed
(I-84, I-85).
 WHY. The probe and the class check share one block, and its rescue reported any failure as UNREACHABLE, so a device
that answered on its port but failed its check (a credential it rejects, a protocol error) read as down, and the run
passed: the smoke gate (scripts/run-smoke-gate.sh, the ping-fleet template) passed a fleet whose every reachable device
rejected its credentials, and galaxy.py's onboard verify, which reads ping's exit code, printed "reachable & managed"
for a host that did not answer or whose check failed (I-84, measured on 2.18.19 and 2.19.4). And in a dry run the probe
is skipped, so a host whose port nothing listens on was reported OK (I-85). The rescue now tells the probe's failure
from the check's, the summary says which, and the last play fails a CHECK FAILED host (not in a dry run, which cannot
run every check) and, under `kontroll_ping_require_reachable`, an UNREACHABLE one. tests/unit/test_ping_check_failed.py
holds that shape; these rows run it.

Hosts on a local connection, at 127.0.0.1: two of the generic raw-SSH backend (its check runs `true`), one at a port
the row listens on and one at a port nothing listens on, and two of a stand-in class whose check is a `fail` (a device
that answers and rejects the credential), at each port. The run's key directory holds a stale key file for each host
before the run. The require rows run galaxy.py's own verify arguments, and its verify's environment.

It needs ansible-playbook: where it is missing the rows are skipped (the gate's images and CI's validate job have
it). Nothing of the developer's Ansible reaches the play: its configuration, home and temporary directory, the
inventory and the key directory are the row's own.
"""
import os
import re
import shutil
import socket
import subprocess
import sys

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="needs ansible-playbook (it runs ping.yml)"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import galaxy  # noqa: E402  (the onboard verify's own arguments)

PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "ping.yml")
CLOSED = {"c-down", "c-faildown"}  # at the port nothing listens on
ROLE = {
    "c-good": "backend_raw_ssh",        # answers, and its check passes: OK
    "c-down": "backend_raw_ssh",        # nothing answers on its port: UNREACHABLE, tolerated
    "c-fail": "zz_rejecting_check",     # answers, and its check fails: CHECK FAILED, the run fails
    "c-faildown": "zz_rejecting_check",  # nothing answers: UNREACHABLE (the probe fails first; its check never runs)
}
STOPPED_AT = "Ask the stand-in device, which rejects the credential"
EXTRA_ROLES = {  # the lab's second roles_path entry
    "zz_rejecting_check/tasks/check.yml": (
        "- name: %s\n  ansible.builtin.fail:\n    msg: the stand-in device rejected the credential\n" % STOPPED_AT),
}
SUMMARY = '"msg": "%s: %s"'
CHECK_FAILED = "CHECK FAILED: its check stopped at the task: " + STOPPED_AT
DRY = " (dry run: the port was not probed)"
LAST = "Fail a host refused, whose check failed, or unreachable where the caller requires it"
REFUSE = "Refuse a dry run asked to prove each host reachable"
PLAYS = ("Reach the fleet (per-class liveness)", "Reachability summary", "Remove the per-run ephemeral SSH key dir",
         "Fail the run for a host refused, whose check failed, or unreachable where the caller requires it")


@pytest.fixture
def ports():
    """(open, closed): a port the row listens on at 127.0.0.1 for the run, and one nothing listens on."""
    with socket.create_server(("127.0.0.1", 0), backlog=64) as listener:
        with socket.create_server(("127.0.0.1", 0)) as gone:
            closed = gone.getsockname()[1]
        yield listener.getsockname()[1], closed


def _lab(tmp_path, ports, hosts):
    """The inventory of `hosts` (each in CLOSED at the closed port, every other at the open one), an Ansible home and
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
    entries = {host: {"ansible_host": "127.0.0.1", "mgmt_port": ports[1] if host in CLOSED else ports[0],
                      "device_role": ROLE[host]} for host in hosts}
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


def _play(lab, *extra, inventory=None, env=None):
    """Run ping.yml against the lab's hosts, or `inventory` (with `extra` arguments, and `env` over the lab's
    environment); return its exit code, its output and each host's recap as (failed, rescued)."""
    p = subprocess.run(["ansible-playbook", "-i", inventory or lab.inventory, PLAYBOOK, *extra], cwd=lab.base,
                       env=dict(lab.env, **(env or {})), capture_output=True, text=True, timeout=600)
    recap = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in
             re.finditer(r"^(\S+)\s*:\s*ok=\d+\s+changed=\d+\s+unreachable=\d+\s+failed=(\d+)\s+skipped=\d+\s+"
                         r"rescued=(\d+)", p.stdout, re.M)}
    return p.returncode, p.stdout + p.stderr, recap


def _task(out, name):
    """The output of the task `name` (from its banner to the next banner or the recap)."""
    at = out.find("TASK [%s] " % name)
    assert at != -1, (name, out)
    end = min(i for i in (out.find("\nTASK [", at + 1), out.find("\nPLAY RECAP", at + 1), len(out)) if i != -1)
    return out[at:end]


def test_a_reachable_host_whose_check_fails_fails_the_run_after_the_key_cleanup(tmp_path, ports):
    """A real run of all four hosts fails with rc 2 for c-fail alone: c-good is OK, c-down and c-faildown are
    UNREACHABLE (rescued, not failed: the probe failed, so c-faildown's failing check never ran), c-fail is CHECK FAILED
    naming the task its check stopped at, and the last play, after the key-cleanup play, fails c-fail alone with its
    summary line; the key directory is gone. Guards I-84: a reachable host whose check failed was summarised
    UNREACHABLE, as a down one, and the run passed, so the smoke gate passed a fleet whose devices rejected their
    credentials; and the rescue's record against naming a down host's probe failure a check failure (c-faildown), or
    keeping the failed task's result (the device's answer) rather than its name."""
    lab = _lab(tmp_path, ports, ["c-good", "c-down", "c-fail", "c-faildown"])
    rc, out, recap = _play(lab)
    assert rc == 2, (rc, out)
    assert recap == {"c-good": (0, 0), "c-down": (0, 1), "c-fail": (1, 1), "c-faildown": (0, 1)}, (recap, out)
    for host, said in (("c-good", "OK"), ("c-down", "UNREACHABLE"), ("c-fail", CHECK_FAILED),
                       ("c-faildown", "UNREACHABLE")):
        assert SUMMARY % (host, said) in out, (host, out)
    assert not re.search(r'"msg": "c-\w+: [^"]*rejected the credential', out), out
    at = [out.find("PLAY [%s]" % play) for play in PLAYS]
    assert -1 not in at and at == sorted(at), (at, out)
    last = _task(out, LAST)
    assert re.findall(r"fatal: \[([\w-]+)\]", last) == ["c-fail"], last
    assert "c-fail: %s (see the summary)" % CHECK_FAILED in last, last
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


def test_a_dry_run_says_its_port_was_not_probed_and_fails_no_check(tmp_path, ports):
    """A dry run (`--check --diff`) of all four hosts passes (rc 0): the probe is skipped, so every summary line says
    `(dry run: the port was not probed)`; c-good and c-down are OK with it (the raw-SSH check does not run in a dry
    run), c-fail and c-faildown are CHECK FAILED with it (a `fail` runs in check mode, and nothing probed their port),
    the last play, after the key-cleanup play, fails no host. Guards I-85: c-down, whose port nothing listens on,
    read a bare `OK`; and the last play against failing a dry run for a check, which cannot run against every device
    in check mode (tests/fresh-node-check.sh runs ping.yml --check on a mock inventory and needs it to pass)."""
    lab = _lab(tmp_path, ports, ["c-good", "c-down", "c-fail", "c-faildown"])
    rc, out, recap = _play(lab, "--check", "--diff")
    assert rc == 0, (rc, out)
    for host, said in (("c-good", "OK"), ("c-down", "OK"), ("c-fail", CHECK_FAILED), ("c-faildown", CHECK_FAILED)):
        assert SUMMARY % (host, said + DRY) in out, (host, out)
    assert recap == {"c-good": (0, 0), "c-down": (0, 0), "c-fail": (0, 1), "c-faildown": (0, 1)}, (recap, out)
    at = [out.find("PLAY [%s]" % play) for play in PLAYS]
    assert -1 not in at and at == sorted(at), (at, out)
    assert "fatal:" not in _task(out, LAST), out


@pytest.mark.parametrize("host, said, rc_bare, rc_required", [
    ("c-good", "OK", 0, 0),
    ("c-down", "UNREACHABLE", 0, 2),
    ("c-fail", CHECK_FAILED, 2, 2),
    ("c-faildown", "UNREACHABLE", 0, 2)])
def test_galaxys_verify_passes_only_a_host_that_answered_and_passed_its_check(tmp_path, ports, host, said, rc_bare,
                                                                              rc_required):
    """A real run under `--limit <host>`, bare, with `-e kontroll_ping_require_reachable=false`, and with galaxy.py's
    own verify arguments (`-e kontroll_ping_require_reachable=true`) and environment (VERIFY_ENV): bare or false, only
    a CHECK FAILED host fails it;
    required, only an OK host passes, the last play failing any other host with its summary line; each time the
    summary names the host and the key directory is gone. Guards I-84: galaxy.py's onboard verify ran ping.yml bare
    and printed "reachable & managed" on rc 0, which a host that did not answer (and, before this change, one whose
    check failed) got; and the require var against failing a host that answered and passed, being read when it is not
    given, or being read as text (an extra var is a string, and `false` is a true one)."""
    assert galaxy.VERIFY_PING[:2] == ["ansible-playbook", "playbooks/ping.yml"], galaxy.VERIFY_PING
    runs = (((), rc_bare, None), (("-e", "kontroll_ping_require_reachable=false"), rc_bare, None),
            (tuple(galaxy.VERIFY_PING[2:]), rc_required, galaxy.VERIFY_ENV))
    for n, (extra, want, env) in enumerate(runs):
        lab = _lab(tmp_path / str(n), ports, [host])
        rc, out, _ = _play(lab, "--limit", host, *extra, env=env)
        assert rc == want, (extra, rc, out)
        assert SUMMARY % (host, said) in out, (extra, out)
        last = _task(out, LAST)
        assert ("fatal: [%s]" % host in last) == (want == 2), (extra, last)
        if want == 2:
            assert "%s: %s (see the summary)" % (host, said) in last, (extra, last)
        assert not os.path.exists(lab.keys), (extra, os.listdir(lab.keys))


@pytest.mark.parametrize("value", ["true", "false"])
def test_a_dry_run_asked_to_prove_each_host_reachable_is_refused_by_name(tmp_path, ports, value):
    """A dry run of all four hosts with `-e kontroll_ping_require_reachable=true` fails with rc 2: the summary's lines
    are the dry run's, and the last play, after the key-cleanup play, fails every host at its first task, naming the
    var and `--check`, so its assert is reached by none. With `=false` the run passes, the last play failing no host.
    (What a dry run's cleanup removes is not this row's: it holds the order of the plays only.) Guards I-84: a dry run probes no port, so asked to prove each host reachable it passed
    c-down, whose port nothing listens on, as OK, and failed c-fail for a check it cannot run on every device; and the
    refusal against firing in a dry run not asked (an extra var is a string, and `false` is a true one)."""
    lab = _lab(tmp_path, ports, ["c-good", "c-down", "c-fail", "c-faildown"])
    rc, out, _ = _play(lab, "--check", "-e", "kontroll_ping_require_reachable=%s" % value)
    for host, said in (("c-good", "OK"), ("c-down", "OK"), ("c-fail", CHECK_FAILED), ("c-faildown", CHECK_FAILED)):
        assert SUMMARY % (host, said + DRY) in out, (host, out)
    at = [out.find("PLAY [%s]" % play) for play in PLAYS]
    assert -1 not in at and at == sorted(at), (at, out)
    last_play = out[at[-1]:]
    if value == "false":
        assert rc == 0 and "fatal:" not in last_play, (rc, out)
        return
    assert rc == 2, (rc, out)
    refuse = _task(out, REFUSE)
    assert sorted(re.findall(r"fatal: \[([\w-]+)\]", refuse)) == sorted(ROLE), refuse
    for host in ROLE:
        assert ("%s: kontroll_ping_require_reachable asks the run to prove the host reachable, which a dry run cannot "
                "(it probes no port): run it without --check." % host) in refuse, (host, refuse)
    assert "TASK [%s]" % LAST not in last_play, last_play


def test_galaxys_verify_fails_a_run_that_read_no_inventory(tmp_path, ports):
    """ping.yml under `--limit c-good` with galaxy.py's verify arguments, given an inventory that does not exist, so
    Ansible parses none (as where it ignores a world-writable checkout's ansible.cfg): without the verify's
    environment the run passes, rc 0, with no line for the host, every play skipped; under VERIFY_ENV it fails, rc 1,
    naming the limit no host matched, before any play. Guards I-84's verify: its OK on rc 0, "its port answered and
    its check passed", for a host that ping never ran."""
    lab = _lab(tmp_path, ports, ["c-good"])
    none = os.path.join(lab.base, "no-such-inventory.yml")
    rc, out, _ = _play(lab, "--limit", "c-good", *galaxy.VERIFY_PING[2:], inventory=none)
    assert rc == 0 and '"msg": "c-good' not in out, (rc, out)
    rc, out, _ = _play(lab, "--limit", "c-good", *galaxy.VERIFY_PING[2:], inventory=none, env=galaxy.VERIFY_ENV)
    assert rc == 1 and "Could not match supplied host pattern, ignoring: c-good" in out, (rc, out)
    assert "PLAY [" not in out, out
