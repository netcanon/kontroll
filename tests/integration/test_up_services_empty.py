"""deploy-stack.yml's bring-up with no service to bring up, run for real by ansible-playbook against a stand-in docker.

WHY: the play brings the stack up with `docker compose ... up -d <the services>`, and compose reads `up -d` with no
service named as EVERY service in the stack. The list is empty when stack_services is, or names only snmp-exporter
on a node without its credentials (the play drops it so it does not crash-loop). So a run meant to bring up nothing
started everything, onboard-gui among it, and the G9 age-key gate, which runs only when onboard-gui is in
stack_services, was skipped (I-32, 2026-11-08, found by the I-22 read). The play now skips the `up` on an empty list
and says why. These rows take the three tasks (compute the list, the note, the `up`) from the playbook as they are
and run them with a `docker` on PATH that writes down each call: on an empty list it is never called and the note
is printed; on a list with a service it is called with exactly that list, and the note is not printed. Needs POSIX
and ansible-playbook; without them every row is skipped.
"""
import json
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")
NAMES = ("Compute the services to bring up", "Say there is no service to bring up",
         "Bring up the selected stack service(s)")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix" or not shutil.which("ansible-playbook"),
                       reason="needs POSIX and ansible-playbook (it runs deploy-stack.yml's own tasks)"),
]


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _the_tasks():
    """The three tasks, in the playbook's order, each found once by the start of its name."""
    with open(DEPLOY, encoding="utf-8") as fh:
        tasks = list(_tasks(yaml.safe_load(fh.read())))
    found = []
    for name in NAMES:
        hits = [t for t in tasks if str(t.get("name", "")).startswith(name)]
        assert len(hits) == 1, "%r moved or is gone — move this test with it" % name
        found.append(hits[0])
    order = [tasks.index(t) for t in found]
    assert order == sorted(order), "the note and the up must follow the list they read"
    return found


def _run(tmp_path, stack_services, snmp_creds=False, caddy=False):
    """Run the three tasks in a play shaped as deploy-stack.yml's (local, no facts) with a stand-in `docker` first on
    PATH; return (rc, output, the docker calls, the note task's result)."""
    compute, note, up = _the_tasks()
    calls = tmp_path / "docker-calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nprintf '%%s\\n' \"$*\" >> '%s'\necho ' Container stand-in  Running'\n" % calls,
                      encoding="utf-8")
    docker.chmod(0o755)
    result = tmp_path / "note.json"
    play = [{
        "name": "The bring-up tasks, as deploy-stack.yml runs them",
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {"stack_services": stack_services, "stack_dir": str(tmp_path / "docker"),
                 "_has_snmp_creds": snmp_creds, "_runs_caddy": {"stdout": "yes" if caddy else "no"}},
        "tasks": [compute, dict(note, register="_note"), up,
                  {"name": "Write down the note's result",
                   "ansible.builtin.copy": {"content": "{{ _note | to_json }}", "dest": str(result), "mode": "0600"}}],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    home_a = tmp_path / "ansible-home"
    home_a.mkdir()
    cfg = home_a / "ansible.cfg"
    cfg.write_text("[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n", encoding="utf-8")
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
        "ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home_a), ANSIBLE_LOCAL_TEMP=str(home_a / "tmp"),
               ANSIBLE_REMOTE_TEMP=str(home_a / "rtmp"), ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0",
               ANSIBLE_LOCALHOST_WARNING="0", PYTHONDONTWRITEBYTECODE="1",
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    p = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "hosts.yml"), str(tmp_path / "play.yml")],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    made = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return p.returncode, out, made, (json.loads(result.read_text(encoding="utf-8")) if result.exists() else None)


def _up_args(call):
    """The services a `docker compose ... up -d <services>` call names, or None for any other call."""
    words = call.split()
    if "up" not in words:
        return None
    return words[words.index("up") + 2:] if words[words.index("up") + 1:][:1] == ["-d"] else None


@pytest.mark.parametrize("stack_services, snmp_creds", [
    ([], False),
    (["snmp-exporter"], False),
], ids=["no-service", "snmp-exporter-without-credentials"])
def test_an_empty_list_brings_up_nothing_and_says_why(tmp_path, stack_services, snmp_creds):
    """An empty stack_services, and one naming only snmp-exporter on a node without its credentials (which the play
    drops), leave nothing to bring up: docker is never called, so compose cannot read a bare `up -d` as every
    service, and the note says there is none and why. The run does not fail: a node with nothing to bring up yet
    is not a broken one. Guards I-32 (the bare `up -d` started the whole stack with the G9 gate skipped)."""
    rc, out, calls, note = _run(tmp_path, stack_services, snmp_creds=snmp_creds)
    assert rc == 0, out
    assert calls == [], calls
    assert note and not note.get("skipped"), out
    assert "no service to bring up on this node" in note["msg"] and "every service in the stack" in note["msg"], note


@pytest.mark.parametrize("stack_services, snmp_creds, caddy, expected", [
    (["prometheus"], False, False, ["prometheus"]),
    (["snmp-exporter"], True, False, ["snmp-exporter"]),
    (["prometheus", "snmp-exporter"], False, True, ["prometheus", "caddy"]),
    ([], False, True, ["caddy"]),
], ids=["one-service", "snmp-exporter-with-credentials", "snmp-dropped-caddy-added", "caddy-alone"])
def test_a_list_with_a_service_is_brought_up_as_it_is(tmp_path, stack_services, snmp_creds, caddy, expected):
    """With a service to bring up, docker is called once, with `up -d` and exactly the list the play computed
    (snmp-exporter dropped without its credentials, caddy added when the node runs it), and the note is skipped. So
    the guard that skips an empty list skips nothing else, including a list that only caddy makes non-empty."""
    rc, out, calls, note = _run(tmp_path, stack_services, snmp_creds=snmp_creds, caddy=caddy)
    assert rc == 0, out
    ups = [_up_args(c) for c in calls if _up_args(c) is not None]
    assert ups == [expected], calls
    assert note and note.get("skipped"), note
