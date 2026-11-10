"""deploy-stack.yml's bring-up with no service to bring up, run for real by ansible-playbook against a stand-in docker.

WHY: the play brings the stack up with `docker compose ... up -d <the services>`, and compose reads `up -d` with no
service named as EVERY service in the stack. The list is empty when stack_services is empty, or names only
snmp-exporter on a node without its credentials (the play drops it so it does not crash-loop), and the node runs no
Caddy (tls_mode acme adds caddy). So a run meant to bring up nothing started everything, onboard-gui among it, and
the G9 age-key gate, which runs only when onboard-gui is in stack_services, was skipped (I-32, 2026-11-08, found by
the I-22 read). A blank name did the same, and a name with a space before it brought onboard-gui up with the gate
skipped (its read A, S-1 and S-2). The play now refuses a stack_services that is not a list of compose service
names, and skips the `up` on an empty list and says why. These rows take the four tasks (the refusal, compute the
list, the note, the `up`) from the playbook as they are and run them with a `docker` on PATH that writes down each
call: on an empty list it is never called and the note is printed; on a list with a service it is called with
exactly that list, and the note is not printed; on a malformed list the run fails at the refusal and docker is
never called. Needs POSIX and ansible-playbook; without them every row is skipped.
"""
import json
import os
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")
NAMES = ("Refuse a stack_services that is not a list of compose service names", "Compute the services to bring up",
         "Say there is no service to bring up", "Bring up the selected stack service(s)")

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
    """The four tasks, in the playbook's order, each found once by the start of its name, each a task of the same
    play's own list. The rows run a task without whatever encloses it, so one moved into a block would run here
    without the block's `when` (its read B, N-3: the note moved into a block that runs only in check mode passed,
    and the `up`'s guard moved onto a block read as a broken guard)."""
    with open(DEPLOY, encoding="utf-8") as fh:
        plays = yaml.safe_load(fh.read())
    tasks = list(_tasks(plays))
    found = []
    for name in NAMES:
        hits = [t for t in tasks if str(t.get("name", "")).startswith(name)]
        assert len(hits) == 1, "%r moved or is gone — move this test with it" % name
        found.append(hits[0])
    homes = [[p for p in plays if any(t is x for x in p.get("tasks") or [])] for t in found]
    for name, home in zip(NAMES, homes):
        assert len(home) == 1, "%r moved into a block — move this test with it" % name
    assert all(h[0] is homes[0][0] for h in homes), "the four tasks are not in one play — move this test with it"
    order = [tasks.index(t) for t in found]
    assert order == sorted(order), "the note and the up must follow the list they read"
    return found


def _run(tmp_path, stack_services, snmp_creds=False, caddy=False, extra=()):
    """Run the four tasks in a play shaped as deploy-stack.yml's (local, no facts) with a stand-in `docker` first on
    PATH, and `extra` on ansible-playbook's command line; return (rc, output, the docker calls, the note task's
    result: None where the run never reached it)."""
    refuse, compute, note, up = _the_tasks()
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
        "tasks": [refuse, compute, dict(note, register="_note"), up,
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
    p = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "hosts.yml"), str(tmp_path / "play.yml"), *extra],
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


@pytest.mark.parametrize("stack_services, snmp_creds, reason", [
    ([], False, "stack_services is empty"),
    (["snmp-exporter"], False,
     "stack_services names only snmp-exporter, which is not brought up while it has no credentials"),
], ids=["no-service", "snmp-exporter-without-credentials"])
def test_an_empty_list_brings_up_nothing_and_says_why(tmp_path, stack_services, snmp_creds, reason):
    """An empty stack_services, and one naming only snmp-exporter on a node without its credentials (which the play
    drops), leave nothing to bring up: docker is never called, so compose cannot read a bare `up -d` as every
    service, and the note says there is none and which of the two reasons it is. The run does not fail: a node
    with nothing to bring up yet is not a broken one. Guards I-32 (the bare `up -d` started the whole stack with
    the G9 gate skipped), and a note that gave no reason, or a false one (its read A, N-1: both passed)."""
    rc, out, calls, note = _run(tmp_path, stack_services, snmp_creds=snmp_creds)
    assert rc == 0, out
    assert calls == [], calls
    assert note and not note.get("skipped"), out
    assert "no service to bring up on this node (%s)" % reason in note["msg"], note
    assert "every service in the stack" in note["msg"], note


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


@pytest.mark.parametrize("stack_services, extra", [
    ("semaphore", ()),
    ('["semaphore"]', ()),
    ({"semaphore": True}, ()),
    ([1], ()),
    ([""], ()),
    (["", "snmp-exporter"], ()),
    ([" onboard-gui"], ()),
    (["semaphore onboard-gui"], ()),
    (["semaphore"], ("-e", "stack_services=semaphore")),
    (["semaphore"], ("-e", 'stack_services=["semaphore"]')),
], ids=["a-string", "a-string-of-json", "a-mapping", "a-number", "a-blank-name", "a-blank-name-beside-snmp-exporter",
        "a-name-with-a-space-before-it", "two-names-in-one", "key-value-e", "key-value-e-of-json"])
def test_a_stack_services_that_is_not_a_list_of_service_names_is_refused(tmp_path, stack_services, extra):
    """A stack_services that is not a list of compose service names fails the run at the play's first task, before
    anything is rendered or brought up: docker is never called, the note never runs, and the message gives the JSON
    form. A string (as a key=value `-e` passes one; the last two rows pass it so) is read by the list below a character
    at a time; a blank name leaves the `up` naming none, which compose reads as every service; a name with a space
    before it brings onboard-gui up while `'onboard-gui' in stack_services`, the G9 gate's condition, is false; two
    names in one are split by the `up` into two services. Guards I-32's read A, S-1 and S-2 (each brought up services
    no gate had judged) and N-2 (the key=value form the docs gave)."""
    rc, out, calls, note = _run(tmp_path, stack_services, extra=extra)
    assert rc != 0, out
    assert calls == [], calls
    assert note is None, out
    assert "stack_services must be a JSON list of compose service names" in out, out
    assert re.search(r"""-e '\{\\?"stack_services\\?":\[\\?"semaphore\\?"\]\}'""", out), out   # printed JSON-escaped
