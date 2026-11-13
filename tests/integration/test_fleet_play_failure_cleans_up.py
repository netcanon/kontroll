"""ansible-playbook runs the shipped install-node-exporter.yml and wire-logging.yml for real, outside `--check`, with
the role each dispatches replaced by a stub that fails on the hosts a row names, and a stale key in the run's key dir.

WHY. Ansible runs no later play once every host of a play's batch has failed. Both playbooks render each key-auth
host's SSH key into the run's key dir in their preamble and remove it in their last import, so an install or a wiring
that failed on every host, or a wiring request the validation play refused, ended the run before the cleanup, and the
run's keys stayed in its key dir (I-64). The rows hold that such a run now removes the key dir and still fails, naming
each host and the task it stopped at; that a host the work did not fail is not failed; that a handler a failed host's
task notified does not run, as it did not before; that a host with no failed task of its own (its role could not be
loaded, or a `run_once` task failed on another host) is failed by name; that a refused request targets no host
whatever `_wire_group` an `-e` or a vars file holds; that the install play still tolerates an unreachable host, and
since I-89 runs nothing more on it, the real node_exporter role included (it failed such a host at its OS-family task,
its facts missing); and what a caller's any_errors_fatal does: every host's work stops at the first failure, and with
more than one host the run ends there with the keys kept (a residual the docs name), with one it reaches the cleanup;
and that a wiring run under a `--limit` that leaves localhost out, which skips the validation play and so wired nothing
and passed, now fails naming the limit to use (I-88), while `--limit <host>:localhost` wires that host alone. And
(I-86) that a handler the role's tasks notify, or one such a handler notifies, runs inside the rescued block: one that
fails on every host is recorded, the run reaches the cleanup and names each host (by the handler's name from
ansible-core 2.19.2, by the rescue's fixed text before it); one that fails on one host of two leaves the other done; a
host that carries the handlers' marker into the play is still failed by name when its role cannot be loaded; and under
a caller's any_errors_fatal, from the environment or from ansible.cfg, the host a failed handler stopped is failed by
name. And (I-92) that the install's report, after the cleanup, names a host whose gathering was unreachable and one
that answered it and then not the re-check after its install, while the run passes for both; and (I-97) that under a
caller's any_errors_fatal the rescue names a host whose own gathering or re-check was unreachable by that cause.
And (I-92, I-97) the same of the wiring: its report names an ssh host that did not answer the probe before its role
(nothing run on it, the real wiring role included) and one that answered it and then not the re-check after it, the
run passing for both, and under a caller's any_errors_fatal the rescue names a host whose own probe or re-check was
unreachable by that cause. tests/unit/test_fleet_play_rescue.py pins the shapes. The stubs run `debug`, `fail` and
`include_tasks`, which open no connection. install-node-exporter.yml's fact gathering and re-check, and
wire-logging.yml's probe and re-check, meet a stand-in `ssh` first on PATH (tests/_stand_in_ssh.py), whether or not
the machine has an ssh client (the validate image has none): for a host whose address the row lists as up it runs the
command on the row's own machine, which stands in for the device (pipelined, so the module comes on its stdin), and for
any other it answers as a refused connection does (exit 255), so the host is unreachable; a host the row gives a count
of connections answers that many and refuses the rest, as one that drops mid-run does. The refused host is
`ansible_connection: local`, which the plays' refusal refuses as it does a network plugin's host, so no row needs a
collection. It needs ansible-playbook on PATH; tests/validate.sh runs it in the validate image.
"""
import os
import re
import shutil
import subprocess

import pytest
import yaml

import _stand_in_ssh

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook on PATH"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WIRE_ROLE = "logging_journald_remote"       # a wiring role whose method's connections are the ssh family
STAMP = r'"msg": "kontroll_run_id=[0-9]{8}T[0-9]{6}Z"'
AEF = {"ANSIBLE_ANY_ERRORS_FATAL": "True"}
# The stub: a step that reports a change and notifies a handler, a `run_once` step that fails (on the first host) when
# `stub_run_once_fail` is set, one that fails on the hosts in `stub_fail`, and one after them. A host in
# `stub_load_fail` includes a task file that does not exist: a load failure, not a task failure.
STUB = [
    {"name": "Stub step that notifies", "ansible.builtin.debug": {"msg": "STUB {{ inventory_hostname }}"},
     "changed_when": True, "notify": "Stub handler"},
    {"name": "Stub step run once", "ansible.builtin.fail": {"msg": "stub run_once failure"},
     "run_once": True, "when": "stub_run_once_fail | default(false)"},
    {"name": "Stub step that fails", "ansible.builtin.fail": {"msg": "stub failure on {{ inventory_hostname }}"},
     "when": "inventory_hostname in (stub_fail | default([]))"},
    {"name": "Stub step that loads",
     "ansible.builtin.include_tasks": "{{ 'missing.yml' if inventory_hostname in (stub_load_fail | default([])) "
                                      "else 'ok.yml' }}"},
    {"name": "Stub step after", "ansible.builtin.debug": {"msg": "AFTER {{ inventory_hostname }}"}},
]
# The stub's handler prints its line, then fails on the hosts in `handler_fail`.
HANDLERS = [{"name": "Stub handler", "ansible.builtin.debug": {"msg": "HANDLER RAN {{ inventory_hostname }}"},
             "failed_when": "inventory_hostname in (handler_fail | default([]))"}]
# A forward chain: the stub's handler reports a change and notifies one defined after it, which fails on the hosts in
# `handler_fail` (the only shape tests/unit/test_fleet_play_rescue.py lets a role's handlers take).
CHAIN = [{"name": "Stub handler", "ansible.builtin.debug": {"msg": "HANDLER RAN {{ inventory_hostname }}"},
          "changed_when": True, "notify": "Stub handler next"},
         {"name": "Stub handler next", "ansible.builtin.debug": {"msg": "NEXT RAN {{ inventory_hostname }}"},
          "failed_when": "inventory_hostname in (handler_fail | default([]))"}]
# The first ansible-core whose rescue sees a failed handler as `ansible_failed_task` (its 2.19.2 changelog,
# ansible/ansible#85682); before it the rescue sees none.
NAMED_HANDLER = (2, 19, 2)
ADDRESS = {"a": "192.0.2.36", "b": "192.0.2.37"}
INSTALL = "Install + start node_exporter (the via:host host metrics agent)"


def _run(tmp_path, playbook, role, hosts, extra=None, env_extra=None, append=None, args=(), up=(), real=False,
         handlers=None, cfg="", calls=None):
    """Run the shipped `playbook` from a copy of ansible/playbooks (with logging/, a probe module whose group is `g`
    and an instance.yml), the role `role` a stub (STUB, and `handlers` or HANDLERS) or, when `real`, the shipped role
    of that name, over `hosts` in group `g`, with `extra` as one JSON `-e` and `args` after it, each text in `append`
    added to the copy's file it names, `cfg` added to ansible.cfg's [defaults], a key dir under `tmp_path` holding one
    stale key, and the stand-in `ssh` (tests/_stand_in_ssh.py) first on PATH, answering for the addresses in `up`,
    each address in `calls` for that many connections only. Return the exit code, the output, each host's failed
    count from the recap, and whether the key dir is gone."""
    tree = tmp_path / "t"
    shutil.copytree(os.path.join(ROOT, "ansible", "playbooks"), str(tree / "ansible" / "playbooks"))
    shutil.copytree(os.path.join(ROOT, "logging"), str(tree / "logging"))
    (tree / "modules" / "probe").mkdir(parents=True)
    (tree / "modules" / "probe" / "module.yml").write_text("inventory_group: g\n")
    (tree / "instance").mkdir()
    (tree / "instance" / "instance.yml").write_text("mgmt_ip: 192.0.2.5\n")
    stub = tree / "ansible" / "roles" / role
    if real:
        shutil.copytree(os.path.join(ROOT, "ansible", "roles", role), str(stub))
    else:
        (stub / "tasks").mkdir(parents=True)
        (stub / "handlers").mkdir()
        (stub / "tasks" / "main.yml").write_text(yaml.safe_dump(STUB, sort_keys=False))
        (stub / "tasks" / "ok.yml").write_text(yaml.safe_dump([{"name": "Stub loaded", "ansible.builtin.debug": {
            "msg": "LOADED {{ inventory_hostname }}"}}]))
        (stub / "handlers" / "main.yml").write_text(yaml.safe_dump(handlers or HANDLERS, sort_keys=False))
    for rel, text in (append or {}).items():
        with open(str(tree / rel), "a", encoding="utf-8") as fh:
            fh.write(text)
    (tree / "inv.yml").write_text(yaml.safe_dump({"all": {"children": {"g": {"hosts": hosts}}}}))
    (tree / "ansible" / "ansible.cfg").write_text("[defaults]\nroles_path = roles\nretry_files_enabled = False\n"
                                                  + cfg + "[ssh_connection]\npipelining = True\n")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    (keys / "h.key").write_text("not a key: a stale file the cleanup must remove\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "seen").mkdir()
    _stand_in_ssh.install(bin_dir)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(tree / "ansible" / "ansible.cfg"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"), ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_HOST_KEY_CHECKING="False", KONTROLL_SSH_KEY_DIR=str(keys),
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), FAKESSH_UP=",".join(up),
               FAKESSH_CALLS=",".join("%s=%d" % kv for kv in (calls or {}).items()),
               FAKESSH_SEEN=str(tmp_path / "seen"), **(env_extra or {}))
    cmd = ["ansible-playbook", "-i", str(tree / "inv.yml"), "playbooks/" + playbook]
    if extra is not None:
        cmd += ["-e", yaml.safe_dump(extra, default_flow_style=True).strip()]
    cmd += list(args)
    p = subprocess.run(cmd, cwd=str(tree / "ansible"), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    assert "PLAY RECAP" in out, out
    return p.returncode, out, _recap(p.stdout, "failed"), not keys.exists()


def _recap(stdout, field):
    """Each host's `field` count (failed, rescued, ...) from the PLAY RECAP."""
    return {m.group(1): int(m.group(2)) for m in
            re.finditer(r"^(\S+)\s*:\s*ok=\d+\s.*?\b%s=(\d+)" % field, stdout, re.M)}


def _ssh(address):
    return {"ansible_connection": "ssh", "ansible_host": address,
            "ansible_python_interpreter": "{{ ansible_playbook_python }}"}


def _task(out, name):
    """The output of the task `name` (from its banner to the next banner or play)."""
    at = out.find("TASK [%s] " % name)
    assert at != -1, (name, out)
    ends = [i for i in (out.find("\nTASK [", at + 1), out.find("\nPLAY [", at + 1)) if i != -1]
    return out[at:min(ends)] if ends else out[at:]


def _wire(tmp_path, extra, role=WIRE_ROLE, env_extra=None, refused=True, only=None, append=None, args=(),
          handlers=None, host_vars=None, cfg="", up=("a", "b"), calls=None, real=False):
    """wire-logging.yml with `-e role=<role> -e target=probe` over the ssh hosts a and b (those in `only`), those in
    `up` answering the play's probe and re-check through the stand-in `ssh`, each in `calls` for that many
    connections only, and (with `refused`) the local host n, which the refusal refuses; `real` runs the shipped
    WIRE_ROLE in place of the stub."""
    hosts = {"a": _ssh("192.0.2.36"), "b": _ssh("192.0.2.37")}
    if only:
        hosts = {h: hosts[h] for h in only}
    if refused:
        hosts["n"] = {"ansible_connection": "local", "ansible_host": "192.0.2.30"}
    for host, more in (host_vars or {}).items():
        hosts[host].update(more)
    return _run(tmp_path, "wire-logging.yml", WIRE_ROLE, hosts, dict(role=role, target="probe", **extra), env_extra,
                append, args, up=[ADDRESS[h] for h in up], real=real, handlers=handlers, cfg=cfg,
                calls={ADDRESS[h]: n for h, n in (calls or {}).items()})


def _stopped(host, task, what="wiring"):
    return '"msg": "%s: FAILED: the %s stopped at the task \\"%s\\""' % (host, what, task)


def _no_task_of_its_own(host, role="the wiring role %s" % WIRE_ROLE):
    return ('"msg": "%s: stopped with no failed task of its own: %s could not be loaded, or another host\'s failure '
            'stopped it (a run_once task, a caller\'s any_errors_fatal, under which before ansible-core 2.19.2 its '
            'own failed handler names no task either); the cause is printed above"' % (host, role))


def test_a_wiring_that_fails_on_every_host_removes_the_keys_and_names_each_host(tmp_path):
    """Both ssh hosts fail at the stub's `fail`, and the local host is refused: the run fails (rc 2) at the summary,
    after the key cleanup, which removed the key dir; the summary names each failed host and the task it stopped at;
    the refused host is refused as before; and neither failed host runs the handler its first step notified, nor its
    step after the failure. The run prints the preamble's kontroll_run_id stamp (a cheap check that the run is the
    shipped playbook's, not that the summary reads the id). Guards I-64: on main the play's every host failed, Ansible
    ended the run there, and the run's keys stayed in the key dir."""
    rc, out, recap, gone = _wire(tmp_path, {"stub_fail": ["a", "b"]})
    assert rc == 2, out
    assert gone, out
    assert recap == {"a": 1, "b": 1, "n": 1, "localhost": 0}, (recap, out)
    for host in ("a", "b"):
        assert _stopped(host, "Stub step that fails") in out, out
        assert "HANDLER RAN %s" % host not in out and "AFTER %s" % host not in out, out
    assert "Refused for this host" in out and "STUB n" not in out, out
    assert re.search(STAMP, out), out


def test_a_wiring_that_fails_on_one_host_wires_the_others(tmp_path):
    """Host a fails at the stub's `fail`; host b runs every step, and its handler: the run fails (rc 2) naming a alone,
    and the key dir is gone. Guards the rescue reaching past its own host, and holds that a host the work did not fail
    runs as it did (its notified handler included), where the failed host runs none."""
    rc, out, recap, gone = _wire(tmp_path, {"stub_fail": ["a"]})
    assert rc == 2 and gone, out
    assert recap == {"a": 1, "b": 0, "n": 1, "localhost": 0}, (recap, out)
    assert _stopped("a", "Stub step that fails") in out and '"msg": "b: FAILED' not in out, out
    assert "AFTER b" in out and "HANDLER RAN b" in out, out
    assert "AFTER a" not in out and "HANDLER RAN a" not in out, out


def test_a_refused_wiring_request_removes_the_keys_and_fails_the_run(tmp_path):
    """`-e role` names no role the roles path holds: the validation play refuses the request, no host reaches the
    device play, the key dir is gone, and the run fails (rc 2) naming the task that refused it. Guards I-64's
    validation case: on main the refusal ended the run at the validation play, before the key cleanup."""
    rc, out, recap, gone = _wire(tmp_path, {}, role="logging_no_such_role")
    assert rc == 2 and gone, out
    assert recap.get("localhost") == 1 and not any(recap.get(h) for h in ("a", "b", "n")), (recap, out)
    assert 'refused at the task \\"Fail if the wiring role is missing\\"' in out, out
    assert "STUB " not in out and "Refused for this host" not in out, out


@pytest.mark.parametrize("where", ["extra var", "vars file"])
def test_a_refused_request_targets_no_host_whatever_wire_group_holds(tmp_path, where):
    """A refused request (no such role) with `_wire_group: g` from an `-e`, or from the playbooks' group_vars/all.yml
    (which localhost reads too): no host reaches the device play or the summary, the key dir is gone, and the run fails
    (rc 2) at the refusal. Guards the device play's `hosts:` reading `_wire_group` before `_wire_refused`: an extra var
    outranks any `set_fact` retarget, so a refused request would run the wiring role on group g (the I-64 reads' N1),
    and a retarget dropped from the rescue would leave a vars file's group in place."""
    if where == "extra var":
        rc, out, recap, gone = _wire(tmp_path, {"_wire_group": "g"}, role="logging_no_such_role")
    else:
        rc, out, recap, gone = _wire(tmp_path, {}, role="logging_no_such_role",
                                     append={"ansible/playbooks/group_vars/all.yml": "\n_wire_group: g\n"})
    assert rc == 2 and gone, out
    assert recap.get("localhost") == 1 and not any(recap.get(h) for h in ("a", "b", "n")), (recap, out)
    assert 'refused at the task \\"Fail if the wiring role is missing\\"' in out, out
    assert "STUB " not in out and "Refused for this host" not in out and ": FAILED: the wiring" not in out, out


@pytest.mark.parametrize("stale", [False, True], ids=["no-marker", "stale-marker"])
def test_a_wiring_role_that_cannot_be_loaded_fails_its_host_beside_a_good_one(tmp_path, stale):
    """Host a's stub includes a task file that does not exist, which is no task failure: the host reaches the rescue
    with no failed task, and the rescue's `fail` fails it with the message that names both causes it cannot tell apart
    (its recap counts the load error and the `fail`, on both cores); b is wired, the key dir is gone, and the run fails
    (rc 2). Guards the load failure left to the record alone: the host is dropped from every later play, so no summary
    would fail it and the run would end green (the I-12 reads' S1, in backup-configs.yml's rescue). With a carrying
    `_kontroll_handlers_due: true` into the play (an inventory var here; a marker kept from an earlier play of a run
    that imports both playbooks does the same), the outcome is the same: the block clears the marker first. Without
    the clearing, the rescue recorded a as stopped while its handlers ran, and no later play named it (I-86, both
    reads)."""
    rc, out, recap, gone = _wire(tmp_path, {"stub_load_fail": ["a"]},
                                 host_vars={"a": {"_kontroll_handlers_due": True}} if stale else None)
    assert rc == 2 and gone, out
    assert recap == {"a": 2, "b": 0, "n": 1, "localhost": 0}, (recap, out)
    assert _no_task_of_its_own("a") in out, out
    assert "LOADED b" in out and "AFTER b" in out and "AFTER a" not in out, out


def test_a_run_once_failure_fails_the_other_host_by_name_and_reaches_the_cleanup(tmp_path):
    """A `run_once` step fails on host a, the first: Ansible moves b into the rescue too, with no failed task of its
    own, and the rescue fails b with the message that says another host's failure may have stopped it; a is recorded
    and the summary names it, after the cleanup, which removed the key dir; the run fails (rc 2); neither host runs a
    step after it. Guards the I-64 reads' finding that this rescue blamed b's role ("could not be loaded") for a's
    failure, and holds that such a run reaches the cleanup."""
    rc, out, recap, gone = _wire(tmp_path, {"stub_run_once_fail": True}, refused=False)
    assert rc == 2 and gone, out
    assert recap == {"a": 1, "b": 1, "localhost": 0}, (recap, out)
    assert _stopped("a", "Stub step run once") in out and _no_task_of_its_own("b") in out, out
    assert "AFTER a" not in out and "AFTER b" not in out and "HANDLER RAN" not in out, out


def test_a_callers_any_errors_fatal_over_two_hosts_stops_both_and_keeps_the_keys(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True, host a's failure stops host b too: b runs no step after the one a failed
    at, and the rescue fails b with the message that says another host's failure may have stopped it; a is rescued
    (recap `failed=0 rescued=1`) and recorded; and the run ends at that play (any_errors_fatal stops the run at b's
    unrescued failure), so no summary runs and the key dir stays: the residual the docs name ("any_errors_fatal stops
    more than one host"). Guards the rescue turning a state-changing play the caller asked to stop at the first failure
    into one that goes on changing the other hosts (backup-configs.yml's capture play overrides the setting, which a
    read-only capture may; this play must not), and the docs' account of the keys going false unnoticed. No host is
    refused here: a refused host fails first, at the refusal, and the setting then stops every host before any work
    (measured)."""
    rc, out, recap, gone = _wire(tmp_path, {"stub_fail": ["a"]}, env_extra=AEF, refused=False)
    assert rc == 2 and not gone, out
    assert "STUB b" in out and "AFTER b" not in out and "LOADED b" not in out, out
    assert recap.get("a") == 0 and _recap(out, "rescued").get("a") == 1 and recap.get("b") == 1, (recap, out)
    assert _no_task_of_its_own("b") in out and ": FAILED: the wiring" not in out, out


def test_a_callers_any_errors_fatal_over_one_host_reaches_the_cleanup(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True with host a alone: a's failure is rescued and recorded, no other host is
    stopped, the run reaches the cleanup, which removed the key dir, and the summary names a (rc 2). Guards the docs'
    "with one host it reaches the cleanup" and the summary dropped for such a run."""
    rc, out, recap, gone = _wire(tmp_path, {"stub_fail": ["a"]}, env_extra=AEF, refused=False, only=("a",))
    assert rc == 2 and gone, out
    assert recap == {"a": 1, "localhost": 0}, (recap, out)
    assert _stopped("a", "Stub step that fails") in out and '"msg": "a: stopped with no failed task' not in out, out


WIRE_PROBE = "Probe the host before its wiring (the run's report names one that does not answer, I-92)"
WIRE_INCLUDE = "Apply the dispatched logging wiring role (collector resolved control-side)"
WIRE_RECHECK = "Re-check the host after its wiring (the run's report names one that no longer answers, I-92)"
# The rescue's words for a host whose own probe, or re-check, was unreachable under any_errors_fatal (I-97).
PROBE_STOPPED = "%s: it did not answer the probe before its wiring, so nothing was wired on it, and it was stopped"
WIRE_RECHECK_STOPPED = "%s: it did not answer the re-check after its wiring, which may not be done, and a caller's"


def _not_wired(host):
    """The wiring report's line for an ssh host that did not answer the probe before its wiring (I-92)."""
    return '"msg": "%s: not wired: it did not answer the probe before its wiring"' % host


def _wiring_may_not_be_done(host):
    """The wiring report's line for an ssh host that answered the probe, then not the re-check (I-92)."""
    return ('"msg": "%s: its wiring may not be done: it answered the probe before it, then not the re-check after it '
            '(each task it did not answer was ignored)"' % host)


@pytest.mark.parametrize("args", [(), ("--check", "--diff")], ids=["real", "check"])
def test_a_down_ssh_host_is_not_wired_and_is_named_and_the_run_passes(tmp_path, args):
    """Host a answers through the stand-in `ssh` and b does not. Real and `--check --diff` (the dry run CLAUDE.md asks
    for first): a's probe answers and its stub wiring runs to its last step, its handler and its re-check too; b's
    probe is unreachable and ignored, and its role's include and its re-check are skipped, so no step of the stub runs
    on it; the run passes (rc 0) with neither host failed, the key dir is gone in the real run, and the report, after
    the cleanup, names b alone, as not wired. Guards I-92 for the wiring: an ssh host that took no connection passed
    with nothing in the run naming it but its UNREACHABLE lines, each of its role's tasks waiting on the connection in
    turn (measured with a stub that connects, on 2.18.19 and 2.19.4); the probe dropped or skipped, the include run
    on a host the probe did not reach, or the report reading another result, fails this row."""
    rc, out, recap, gone = _wire(tmp_path, {}, up=("a",), refused=False, args=args)
    assert rc == 0 and (gone or args), out
    assert recap == {"a": 0, "b": 0, "localhost": 0}, (recap, out)
    assert "AFTER a" in out and "HANDLER RAN a" in out, out
    assert "STUB b" not in out and "FAILED: the wiring" not in out, out
    probed = _task(out, WIRE_PROBE)
    assert "ok: [a]" in probed and re.search(r"\[b\]: UNREACHABLE!", probed), probed
    included = _task(out, WIRE_INCLUDE)
    assert "skipping: [b]" in included and "skipping: [a]" not in included, included
    rechecked = _task(out, WIRE_RECHECK)
    assert "ok: [a]" in rechecked and "skipping: [b]" in rechecked, rechecked
    assert _not_wired("b") in out and _not_wired("a") not in out, out
    assert _wiring_may_not_be_done("a") not in out and _wiring_may_not_be_done("b") not in out, out


@pytest.mark.parametrize("args", [(), ("--check", "--diff")], ids=["real", "check"])
def test_an_ssh_host_that_stops_answering_after_its_probe_is_named_and_the_run_passes(tmp_path, args):
    """Host a answers its probe through the stand-in `ssh` and refuses every later connection; b answers every one.
    Real and `--check --diff`: a's stub wiring runs (its steps open no connection), its re-check is unreachable and
    ignored, b's answers; the run passes (rc 0) with neither failed, the key dir is gone in the real run, and the
    report names a alone, as a host whose wiring may not be done, not as one not wired. Guards I-92 for the wiring:
    such a host's wiring ended with each unanswered task ignored, rc 0, and nothing naming it but its UNREACHABLE lines
    (measured with a stub that connects and with the shipped logging_journald_remote, on 2.18.19 and 2.19.4); the
    re-check dropped or kept from a dry run, or the report reading the probe's result for the re-check's, fails this
    row."""
    rc, out, recap, gone = _wire(tmp_path, {}, calls={"a": 1}, refused=False, args=args)
    assert rc == 0 and (gone or args), out
    assert recap == {"a": 0, "b": 0, "localhost": 0}, (recap, out)
    assert "AFTER a" in out and "AFTER b" in out and "FAILED: the wiring" not in out, out
    rechecked = _task(out, WIRE_RECHECK)
    assert re.search(r"\[a\]: UNREACHABLE!", rechecked) and "ok: [b]" in rechecked, rechecked
    assert _wiring_may_not_be_done("a") in out and _wiring_may_not_be_done("b") not in out, out
    assert _not_wired("a") not in out and _not_wired("b") not in out, out


@pytest.mark.parametrize("play", ["install", "wire"])
def test_a_start_at_a_task_the_run_never_meets_runs_nothing_and_passes(tmp_path, play):
    """`--start-at-task` naming a task of the role the play reaches only through its include (the stub's `Stub step
    after`), for install-node-exporter.yml and wire-logging.yml with both hosts up: ansible-core never meets it, so no
    task runs (no `TASK [` banner, the stub's lines absent), the key dir keeps the stale key the row seeded (the key
    cleanup ran neither), and the run exits 0 after the core's `No matching task "<name>" found` line. Pins the core's
    behaviour the fleet playbooks' usage lines and docs/troubleshooting.md describe (I-99, measured on 2.18.19 and
    2.19.4 for all four fleet playbooks), so a core that changes it (a failing rc, a run that starts) fails here and the
    words are updated with it."""
    args = ("--start-at-task", "Stub step after")
    rc, out, _recap_failed, gone = (_install(tmp_path, {}, args=args) if play == "install" else
                                    _wire(tmp_path, {}, refused=False, args=args))
    assert rc == 0 and not gone, out
    assert 'No matching task "Stub step after" found' in out, out
    assert "TASK [" not in out and "AFTER a" not in out, out


def test_the_real_wiring_role_runs_nothing_on_a_down_ssh_host(tmp_path):
    """The shipped logging_journald_remote over host a, down: its probe is unreachable and ignored, the role's include
    is skipped and none of the role's tasks runs, the run passes (rc 0) with a not failed, the key dir is gone, and
    the report names a as not wired. Guards the probe's `when` on the include with the role a run wires in place:
    without it each of the role's tasks connects in turn, each unreachable and ignored (measured on 2.18.19 and
    2.19.4). A skipped include never loads the role, so the row passes with an empty role too; nothing runs on the
    host, so the real run touches nothing."""
    rc, out, recap, gone = _wire(tmp_path, {}, up=(), only=("a",), refused=False, real=True)
    assert rc == 0 and gone, out
    assert recap == {"a": 0, "localhost": 0}, (recap, out)
    assert "FAILED!" not in out and "FAILED: the wiring" not in out, out
    assert "skipping: [a]" in _task(out, WIRE_INCLUDE), out
    assert "TASK [%s : " % WIRE_ROLE not in out, out
    assert _not_wired("a") in out, out


def test_a_callers_any_errors_fatal_over_a_down_ssh_host_names_its_probe(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True with host a alone, down: its ignored unreachable probe trips the setting as
    a failure does, ansible-core moves a into the rescue and holds it failed, and the run ends at the device play (rc
    2, the key dir kept: the residual the header names). The rescue's message names a's own unreachable probe, not the
    causes it cannot tell apart (a role that could not be loaded, another host's failure), which were false for it.
    Guards I-97 for the wiring: a down host alone was named by those causes (measured with a stub that connects, on
    2.18.19 and 2.19.4)."""
    rc, out, _recap, gone = _wire(tmp_path, {}, up=(), env_extra=AEF, refused=False, only=("a",))
    assert rc == 2 and not gone, out
    assert PROBE_STOPPED % "a" in out and "a: stopped with no failed task of its own" not in out, out
    assert "STUB a" not in out and _not_wired("a") not in out, out


def test_a_callers_any_errors_fatal_over_an_up_and_a_down_ssh_host_names_each_by_its_own_cause(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True over host a, up, and b, down: b's unreachable probe stops both before the
    role; the rescue fails b naming its own unreachable probe and a with the causes it cannot tell apart (another
    host's failure stopped it); rc 2, the key dir kept, no stub step run. Guards I-97 for the wiring: both hosts got
    the second message, b's false for it; and the first message given to a, which answered."""
    rc, out, _recap, gone = _wire(tmp_path, {}, up=("a",), env_extra=AEF, refused=False)
    assert rc == 2 and not gone, out
    assert PROBE_STOPPED % "b" in out and "b: stopped with no failed task of its own" not in out, out
    assert _no_task_of_its_own("a") in out and PROBE_STOPPED % "a" not in out, out
    assert "STUB a" not in out and "STUB b" not in out, out


def test_a_callers_any_errors_fatal_over_an_ssh_host_that_stops_answering_names_its_recheck(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True with host a alone, answering its probe and refusing every later connection:
    its stub wiring runs, its unreachable re-check trips the setting, and the rescue names that cause (it did not
    answer the re-check, its wiring may not be done), not the role or another host; rc 2, the key dir kept. Guards
    I-97's second branch for the wiring: a host that drops mid-wiring under any_errors_fatal named by causes false for
    it."""
    rc, out, _recap, gone = _wire(tmp_path, {}, calls={"a": 1}, env_extra=AEF, refused=False, only=("a",))
    assert rc == 2 and not gone, out
    assert "AFTER a" in out and WIRE_RECHECK_STOPPED % "a" in out, out
    assert "a: stopped with no failed task of its own" not in out and _wiring_may_not_be_done("a") not in out, out


def _install(tmp_path, extra, up=("a", "b"), real=False, args=(), calls=None, env_extra=None, only=("a", "b")):
    """install-node-exporter.yml over the hosts in `only` (a and b), those in `up` answering through the stand-in, each
    host in `calls` for that many connections only, `args` after the `-e`."""
    hosts = {h: _ssh(ADDRESS[h]) for h in only}
    return _run(tmp_path, "install-node-exporter.yml", "node_exporter", hosts, dict(target="g", **extra),
                up=[ADDRESS[h] for h in up], real=real, args=args, env_extra=env_extra,
                calls={ADDRESS[h]: n for h, n in (calls or {}).items()})


def _not_installed(host):
    """The install report's line for a host whose fact gathering was unreachable (I-92)."""
    return '"msg": "%s: not installed: its fact gathering was unreachable"' % host


def _may_not_be_done(host):
    """The install report's line for a host that answered the gathering, then not the re-check (I-92)."""
    return ('"msg": "%s: its install may not be done: it answered the fact gathering, then not the re-check after its '
            'install (each task it did not answer was ignored)"' % host)


RECHECK = "Re-check the host after its install (the run's report names one that no longer answers, I-92)"
# The rescue's words for a host whose own gathering, or re-check, was unreachable under any_errors_fatal (I-97).
GATHER_STOPPED = "%s: its fact gathering was unreachable, so nothing was installed on it, and it was stopped with the"
RECHECK_STOPPED = "%s: it did not answer the re-check after its install, which may not be done, and a caller's"


def test_an_install_that_fails_on_every_host_removes_the_keys_and_names_each_host(tmp_path):
    """Both hosts' stub install fails (their fact gathering answered through the stand-in `ssh`): the run fails (rc 2)
    at the summary, after the key cleanup, which removed the key dir, naming each host and the task it stopped at;
    neither runs its notified handler. The run prints the preamble's kontroll_run_id stamp (a cheap check that the run
    is the shipped playbook's). Guards I-64 for install-node-exporter.yml."""
    rc, out, recap, gone = _install(tmp_path, {"stub_fail": ["a", "b"]})
    assert rc == 2 and gone, out
    assert recap == {"a": 1, "b": 1}, (recap, out)
    for host in ("a", "b"):
        assert _stopped(host, "Stub step that fails", "install") in out, out
        assert "HANDLER RAN %s" % host not in out, out
    assert re.search(STAMP, out), out


def test_an_install_role_that_cannot_be_loaded_fails_its_host_beside_a_good_one(tmp_path):
    """Host a's stub install includes a task file that does not exist: the rescue fails a with the message that names
    both causes (the node_exporter role could not be loaded, or another host's failure stopped it); b installs, the key
    dir is gone, and the run fails (rc 2). Guards the install rescue's `fail` dropped or kept from running (a `tags`,
    a `failed_when`): a would be dropped from every later play and the run would end green (the I-64 reads' N2)."""
    rc, out, recap, gone = _install(tmp_path, {"stub_load_fail": ["a"]})
    assert rc == 2 and gone, out
    assert recap == {"a": 2, "b": 0}, (recap, out)
    assert _no_task_of_its_own("a", "the node_exporter role") in out, out
    assert "LOADED b" in out and "AFTER b" in out and "AFTER a" not in out, out


def test_an_unreachable_host_is_tolerated_and_not_recorded(tmp_path):
    """Both hosts are unreachable at the fact gathering: the install is skipped for both (the stub never runs), the run
    passes (rc 0), no host is named FAILED, and the key dir is gone. Guards the rescue taking an unreachable host for a
    failed install, which would turn the play's `ignore_unreachable` (a down host is tolerated, not fatal: CLAUDE.md)
    into a failed run; and I-89: the install ran on a host whose gathering was unreachable, with no facts. And I-92:
    the run's report, after the cleanup, names each as not installed (before it only the gathering's UNREACHABLE line
    did), and the re-check is skipped for both (it runs only where the gathering answered)."""
    rc, out, recap, gone = _install(tmp_path, {}, up=())
    assert rc == 0 and gone, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "FAILED: the install" not in out and "STUB a" not in out and "STUB b" not in out, out
    assert re.search(r"\[a\]: UNREACHABLE!", out) and re.search(r"\[b\]: UNREACHABLE!", out), out
    skipped = _task(out, INSTALL)
    assert "skipping: [a]" in skipped and "skipping: [b]" in skipped, skipped
    rechecked = _task(out, RECHECK)
    assert "skipping: [a]" in rechecked and "skipping: [b]" in rechecked, rechecked
    for host in ("a", "b"):
        assert _not_installed(host) in out and _may_not_be_done(host) not in out, out


def test_a_down_host_beside_an_up_one_gets_nothing_run_and_the_up_one_installs(tmp_path):
    """Host a answers through the stand-in `ssh` and b does not: a's gathering and its stub install run (to its last
    step), b's install is skipped, the run passes (rc 0) with neither named FAILED, and the key dir is gone. Guards
    I-89's skip against reading another host's gathering (a skipped beside a down host, or b run beside an up one),
    and the up host's gathering, through the stand-in, against a row that cannot reach the stub at all. And I-92: the
    report names b alone, as not installed; a, which answered its re-check, is not named."""
    rc, out, recap, gone = _install(tmp_path, {}, up=("a",))
    assert rc == 0 and gone, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "AFTER a" in out and "STUB b" not in out and "FAILED: the install" not in out, out
    assert not re.search(r"\[a\]: UNREACHABLE!", out) and re.search(r"\[b\]: UNREACHABLE!", out), out
    included = _task(out, INSTALL)
    assert "skipping: [b]" in included and "skipping: [a]" not in included, included
    assert "ok: [a]" in _task(out, RECHECK), out
    assert _not_installed("b") in out and _not_installed("a") not in out, out
    assert _may_not_be_done("a") not in out and _may_not_be_done("b") not in out, out


def test_a_dry_run_over_a_down_host_beside_an_up_one_skips_the_down_one(tmp_path):
    """`--check --diff`, the dry run CLAUDE.md asks for first, over host a answering through the stand-in `ssh` and b
    not: a's stub install runs (to its last step), b's install is skipped, and the run passes (rc 0) with neither named
    FAILED. Guards I-89 in the dry run, which a down host failed too before the fix: a `when` that lets a dry run past
    the skip (`... or ansible_check_mode`) passes every real-run row. The key dir is not asserted here: what a dry
    run's cleanup removes is tests/integration/test_ssh_key_render_runs.py's. The dry run's report names b as not
    installed, as the real run's does (I-92)."""
    rc, out, recap, _gone = _install(tmp_path, {}, up=("a",), args=("--check", "--diff"))
    assert rc == 0, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "AFTER a" in out and "STUB b" not in out and "FAILED: the install" not in out, out
    assert re.search(r"\[b\]: UNREACHABLE!", out), out
    included = _task(out, INSTALL)
    assert "skipping: [b]" in included and "skipping: [a]" not in included, included
    assert _not_installed("b") in out and _not_installed("a") not in out and _may_not_be_done("a") not in out, out


def test_the_real_role_runs_nothing_on_a_down_host(tmp_path):
    """The shipped node_exporter role, over two hosts whose gathering is unreachable: the run passes (rc 0), no host is
    named FAILED, its include is skipped for both and none of the role's tasks runs, and the key dir is gone. Guards
    I-89's `when` with the role a run installs in place (without the `when` the role is included for both hosts). A
    skipped include never loads the role, so the row passes with an empty role too: that the shipped role then failed
    each down host ("'ansible_os_family' is undefined", rc 2, so CLAUDE.md's "never let one unreachable host fail a
    fleet-wide run" was false for it) was measured on 2.18.19 and 2.19.4 before the fix, not by this row."""
    rc, out, recap, gone = _install(tmp_path, {}, up=(), real=True)
    assert rc == 0 and gone, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "FAILED!" not in out and "is undefined" not in out and "FAILED: the install" not in out, out
    skipped = _task(out, INSTALL)
    assert "skipping: [a]" in skipped and "skipping: [b]" in skipped, skipped
    assert "TASK [node_exporter : " not in out, out
    assert _not_installed("a") in out and _not_installed("b") in out, out


@pytest.mark.parametrize("args", [(), ("--check", "--diff")], ids=["real", "check"])
def test_a_host_that_stops_answering_after_its_gathering_is_named_and_the_run_passes(tmp_path, args):
    """Host a answers its fact gathering through the stand-in `ssh` and refuses every later connection; b answers
    every one. Real and `--check --diff`: a's stub install runs (its steps open no connection), its re-check is
    unreachable and ignored, b's answers; the run passes (rc 0) with neither failed, the key dir is gone in the real
    run, and the report, after the cleanup, names a alone, as a host whose install may not be done, not as one whose
    gathering was unreachable. Guards I-92: such a host's install ended with each unanswered task ignored, rc 0, and
    nothing in the run naming it but its UNREACHABLE lines (the I-89 read A's R8); the re-check dropped, kept from a
    dry run, or the report reading the gathering's result instead of the re-check's, would fail this row."""
    rc, out, recap, gone = _install(tmp_path, {}, up=("a", "b"), calls={"a": 1}, args=args)
    assert rc == 0 and (gone or args), out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "AFTER a" in out and "AFTER b" in out and "FAILED: the install" not in out, out
    rechecked = _task(out, RECHECK)
    assert re.search(r"\[a\]: UNREACHABLE!", rechecked) and "ok: [b]" in rechecked, rechecked
    assert _may_not_be_done("a") in out and _may_not_be_done("b") not in out, out
    assert _not_installed("a") not in out and _not_installed("b") not in out, out


@pytest.mark.skipif(not (os.path.exists("/etc/debian_version") or os.path.exists("/etc/alpine-release")),
                    reason="the real role installs on Debian or Alpine; the stand-in runs the host's modules here")
def test_the_real_role_over_a_host_that_stops_answering_after_its_gathering_passes_and_names_it(tmp_path):
    """The shipped node_exporter role in `--check --diff` (the stand-in runs a host's modules on this machine: a dry
    run installs nothing here even if the count misfired) over host a, which answers its fact gathering and refuses
    every later connection, and b, down: a's OS family (this machine's, Debian or Alpine) picks the role's task file,
    each of its tasks is unreachable and ignored, as is its re-check; the run passes (rc 0) with neither host failed,
    and the report names a as a host whose install may not be done and b as not installed. Guards I-92 with the role a
    run installs in place: a task of the role that failed on an ignored unreachable result (a registered value read
    after it) would fail a instead, and the report would not be what names it."""
    rc, out, recap, _gone = _install(tmp_path, {}, up=("a",), calls={"a": 1}, real=True, args=("--check", "--diff"))
    assert rc == 0, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "TASK [node_exporter : " in out and "FAILED!" not in out and "FAILED: the install" not in out, out
    assert re.search(r"\[a\]: UNREACHABLE!", _task(out, RECHECK)), out
    assert _may_not_be_done("a") in out and _not_installed("b") in out, out


def test_a_callers_any_errors_fatal_over_a_host_whose_gathering_is_unreachable_names_that_cause(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True with host a alone, down: its ignored unreachable gathering trips the setting
    as a failure does, ansible-core moves a into the rescue and holds it failed, and the run ends at the install play
    (rc 2, the key dir kept: the residual the header names). The rescue's message names a's own unreachable gathering,
    not the causes it cannot tell apart (a role that could not be loaded, another host's failure), which were false for
    it. Guards I-97 (the I-89 read A's R11): the message blamed another host's failure for a host alone; and the rescue
    letting such a host go on instead of failing it (the issue's first design): ansible-core still holds it failed, so
    the run ends at the install play with rc 0, the keys kept and nothing failed (measured on 2.18.19 and 2.19.4)."""
    rc, out, _recap, gone = _install(tmp_path, {}, up=(), env_extra=AEF, only=("a",))
    assert rc == 2 and not gone, out
    assert GATHER_STOPPED % "a" in out and "a: stopped with no failed task of its own" not in out, out
    assert _not_installed("a") not in out, out


def test_a_callers_any_errors_fatal_over_an_up_and_a_down_host_names_each_by_its_own_cause(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True over host a, up, and b, down: b's unreachable gathering stops both before the
    install; the rescue fails b naming its own unreachable gathering and a with the causes it cannot tell apart
    (another host's failure stopped it); rc 2, the key dir kept, no stub step run. Guards I-97 (the I-89 read A's R7a):
    both hosts got the second message, b's false for it; and the first message given to a, which answered."""
    rc, out, _recap, gone = _install(tmp_path, {}, up=("a",), env_extra=AEF)
    assert rc == 2 and not gone, out
    assert GATHER_STOPPED % "b" in out and "b: stopped with no failed task of its own" not in out, out
    assert _no_task_of_its_own("a", "the node_exporter role") in out and GATHER_STOPPED % "a" not in out, out
    assert "STUB a" not in out, out


def test_a_callers_any_errors_fatal_over_a_host_that_stops_answering_names_its_recheck(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True with host a alone, answering its fact gathering and refusing every later
    connection: its stub install runs, its unreachable re-check trips the setting, and the rescue names that cause (it
    did not answer the re-check, its install may not be done), not the role or another host; rc 2, the key dir kept.
    Guards I-97's second branch: a host that drops mid-install under any_errors_fatal named by causes false for it."""
    rc, out, _recap, gone = _install(tmp_path, {}, up=("a",), calls={"a": 1}, env_extra=AEF, only=("a",))
    assert rc == 2 and not gone, out
    assert "AFTER a" in out and RECHECK_STOPPED % "a" in out, out
    assert "a: stopped with no failed task of its own" not in out and _may_not_be_done("a") not in out, out


NOT_VALIDATED = ("the wiring request was not validated, so no host was wired: the validation play runs on localhost, "
                 "which this run's --limit leaves out (or its --tags left out its tasks). Add localhost to the limit "
                 "(--limit <host>:localhost), or run it without --tags.")


@pytest.mark.parametrize("limit,check", [("a", False), ("a", True), ("g", False)], ids=["real", "check", "group"])
def test_a_limit_that_leaves_localhost_out_fails_the_run_and_wires_nothing(tmp_path, limit, check):
    """A wiring run, real and `--check`, under `--limit a`, and a real one under `--limit g` (both hosts): the
    validation play (`hosts: localhost`) does not run, so no host is wired, and the run fails (rc 2) at the guard play
    after the key cleanup, naming the limit to use, once (one message, over two limited hosts too); the real run's key
    dir is gone. Guards I-88: the device play then matched no host, every play after the preamble printed `skipping: no
    hosts matched`, and the run (the mandated dry-run too) passed having wired nothing; and the guard's `run_once`
    (the I-88 reads' B-2: over one host, a guard without it fails once as well)."""
    rc, out, recap, gone = _wire(tmp_path, {}, refused=False, args=["--limit", limit] + (["--check"] if check else []))
    assert rc == 2, out
    assert set(recap) == ({"a"} if limit == "a" else {"a", "b"}) and recap["a"] == 1, (recap, out)
    assert out.count('"msg": "%s"' % NOT_VALIDATED) == 1, out
    assert "STUB " not in out, out
    assert gone or check, out


def test_a_limit_with_localhost_wires_the_limited_host_alone(tmp_path):
    """A real wiring run under `--limit a:localhost` (the header's usage line): the validation play runs, host a is
    wired and b is not, the guard play does not fail it, the key dir is gone, and the run passes. Guards the guard's
    `when` against failing a run whose request was validated, and the usage line the header and the failure name."""
    rc, out, recap, gone = _wire(tmp_path, {}, refused=False, args=["--limit", "a:localhost"])
    assert rc == 0 and gone, out
    assert recap == {"a": 0, "localhost": 0}, (recap, out)
    assert "STUB a" in out and "STUB b" not in out and NOT_VALIDATED not in out, out


def _core():
    """(major, minor, patch) of the ansible-core on PATH, from `ansible-playbook --version`: the patch matters, the
    rescue's view of a failed handler changing at 2.19.2 (NAMED_HANDLER). Called in a row's body only: a file's top
    level runs at collection, before the module's skip (I-98)."""
    out = subprocess.run(["ansible-playbook", "--version"], capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"\[core (\d+)\.(\d+)\.(\d+)", out)
    assert m, out
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _handler_stopped(host, what, handler="Stub handler"):
    """The summary's line for a host whose handler failed: by the handler's name from ansible-core 2.19.2, which gives
    the rescue the handler as its failed task, and by the rescue's fixed text before it, which gives it none."""
    if _core() >= NAMED_HANDLER:
        return _stopped(host, handler, what)
    return '"msg": "%s: FAILED: the %s stopped while the role\'s handlers ran (the failure is printed above)"' % (
        host, what)


@pytest.mark.parametrize("what", ["install", "wiring"])
def test_a_handler_that_fails_on_every_host_removes_the_keys_and_names_each_host(tmp_path, what):
    """The stub's handler fails on both hosts, after every stub step ran: the run fails (rc 2) at the summary, after
    the key cleanup, which removed the key dir, and names each host (by the handler's name from 2.19.2; before it, as
    stopped while the role's handlers ran). Guards I-86: the role's handlers ran at the play's end, outside the block,
    so a handler that failed on every host ended the run there, before the cleanup, and the keys stayed; with the
    flush dropped that is so again, and before 2.19.2, with the marker dropped, the rescue fails each host and the run
    ends there too."""
    extra = {"handler_fail": ["a", "b"]}
    rc, out, recap, gone = _install(tmp_path, extra) if what == "install" else _wire(tmp_path, extra, refused=False)
    assert rc == 2 and gone, out
    per_host = 1 if _core() >= NAMED_HANDLER else 2     # before 2.19.2 the handler's failure counts too
    assert {h: recap[h] for h in ("a", "b")} == {"a": per_host, "b": per_host}, (recap, out)
    for host in ("a", "b"):
        assert _handler_stopped(host, what) in out, out
        assert "AFTER %s" % host in out and "HANDLER RAN %s" % host in out, out


@pytest.mark.parametrize("what", ["install", "wiring"])
def test_a_handler_a_handler_notifies_runs_inside_the_block(tmp_path, what):
    """The stub's handler notifies one defined after it (CHAIN), which fails on both hosts: both run inside the block's
    flush, so the run fails (rc 2) at the summary, after the key cleanup, which removed the key dir, and names each
    host (by the second handler's name from 2.19.2; before it, as stopped while the role's handlers ran). Holds the
    premise of tests/unit/test_fleet_play_rescue.py's handler row, which lets a role's handlers notify forward only:
    a chain defined backward runs its second handler at the play's end, outside the block, and failing on every host
    ends the run before the cleanup with the keys kept (both reads of I-86 measured it on all three cores)."""
    extra = {"handler_fail": ["a", "b"]}
    if what == "install":
        hosts = {h: _ssh(address) for h, address in ADDRESS.items()}
        rc, out, recap, gone = _run(tmp_path, "install-node-exporter.yml", "node_exporter", hosts,
                                    dict(target="g", **extra), up=list(ADDRESS.values()), handlers=CHAIN)
    else:
        rc, out, recap, gone = _wire(tmp_path, extra, refused=False, handlers=CHAIN)
    assert rc == 2 and gone, out
    per_host = 1 if _core() >= NAMED_HANDLER else 2
    assert {h: recap[h] for h in ("a", "b")} == {"a": per_host, "b": per_host}, (recap, out)
    for host in ("a", "b"):
        assert _handler_stopped(host, what, "Stub handler next") in out, out
        assert "HANDLER RAN %s" % host in out and "NEXT RAN %s" % host in out, out


def test_a_handler_that_fails_on_one_host_leaves_the_other_installed(tmp_path):
    """The stub's handler fails on host a alone: b runs every step and its handler and is not named, a is named at the
    summary, the key dir is gone, and the run fails (rc 2). Guards the handler's record reaching past its own host."""
    rc, out, recap, gone = _install(tmp_path, {"handler_fail": ["a"]})
    assert rc == 2 and gone, out
    assert recap["b"] == 0 and _handler_stopped("a", "install") in out and '"msg": "b: FAILED' not in out, out
    assert "AFTER b" in out and "HANDLER RAN b" in out, out


@pytest.mark.parametrize("what,source", [("install", "env"), ("wiring", "env"), ("install", "cfg")])
def test_a_callers_any_errors_fatal_over_two_hosts_and_a_failed_handler_names_the_host_it_stopped(tmp_path, what,
                                                                                                   source):
    """Under any_errors_fatal the stub's handler fails on host a: b, which a's failure stopped, is failed in the rescue
    by name, and the run ends at that play with the key dir kept, as for a task failure under the setting (the residual
    the docs name); from 2.19.2 a is rescued and recorded, before it a is failed in the rescue too (the rescue cannot
    tell its failed handler from another host's stop). The setting comes from ANSIBLE_ANY_ERRORS_FATAL=True, or (the
    install) from ansible.cfg's `any_errors_fatal = True` with no such variable. Guards the fail's `when` losing the
    config's clause (b would be recorded alone, then dropped from every later play, and never named), and reading the
    setting from the environment alone (the second read's mutant passed every row that set it there: ansible.cfg's is
    the same setting)."""
    extra = {"handler_fail": ["a"]}
    env, cfg = (AEF, "") if source == "env" else (None, "any_errors_fatal = True\n")
    if what == "install":
        rc, out, recap, gone = _run(tmp_path, "install-node-exporter.yml", "node_exporter",
                                    {h: _ssh(address) for h, address in ADDRESS.items()}, dict(target="g", **extra),
                                    env, up=list(ADDRESS.values()), cfg=cfg)
        role = "the node_exporter role"
    else:
        rc, out, recap, gone = _wire(tmp_path, extra, env_extra=env, refused=False, cfg=cfg)
        role = "the wiring role %s" % WIRE_ROLE
    assert rc == 2 and not gone, out
    assert _no_task_of_its_own("b", role) in out and ": FAILED: the %s" % what not in out, out
    if _core() >= NAMED_HANDLER:
        assert recap.get("a") == 0 and _recap(out, "rescued").get("a") == 1 and recap.get("b") == 1, (recap, out)
    else:
        assert _no_task_of_its_own("a", role) in out and recap.get("b") == 1, (recap, out)


def test_a_callers_any_errors_fatal_over_one_host_and_a_failed_handler(tmp_path):
    """Under ANSIBLE_ANY_ERRORS_FATAL=True with host a alone, the stub's handler fails: from 2.19.2 the rescue records
    it by the handler's name, the run reaches the cleanup, which removed the key dir, and the summary names a; before
    2.19.2 the rescue fails a there (the residual the headers name) and the key dir stays. rc 2 either way. Guards the
    docs' account of a one-host run under the setting on each side of 2.19.2."""
    rc, out, recap, gone = _run(tmp_path, "install-node-exporter.yml", "node_exporter", {"a": _ssh(ADDRESS["a"])},
                                dict(target="g", handler_fail=["a"]), AEF, up=[ADDRESS["a"]])
    assert rc == 2, out
    if _core() >= NAMED_HANDLER:
        assert gone and _handler_stopped("a", "install") in out, out
    else:
        assert not gone and _no_task_of_its_own("a", "the node_exporter role") in out, out
