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
and passed, now fails naming the limit to use (I-88), while `--limit <host>:localhost` wires that host alone.
tests/unit/test_fleet_play_rescue.py pins the shapes. The stubs run `debug`, `fail` and `include_tasks`, which open no
connection. install-node-exporter.yml's fact gathering meets a stand-in `ssh` first on PATH (FAKE_SSH), whether or not
the machine has an ssh client (the validate image has none): for a host whose address the row lists as up it runs the
command on the row's own machine, which stands in for the device (pipelined, so the module comes on its stdin), and for
any other it answers as a refused connection does (exit 255), so the host is unreachable. The refused host is
`ansible_connection: local`, which the plays' refusal refuses as it does a network plugin's host, so no row needs a
collection. It needs ansible-playbook on PATH; tests/validate.sh runs it in the validate image.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

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
HANDLERS = [{"name": "Stub handler", "ansible.builtin.debug": {"msg": "HANDLER RAN {{ inventory_hostname }}"}}]
# The stand-in `ssh`: for a host in $FAKESSH_UP (a comma list of addresses) it runs the remote command here, under
# /bin/sh, as an ssh login would on the device; for any other it answers as a refused connection does (exit 255).
FAKE_SSH = """#!%s
import os, sys
argv, i, host, cmd = sys.argv[1:], 0, None, []
WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")
while i < len(argv):
    a = argv[i]
    if a.startswith("-") and len(a) > 1:
        i += 2 if a[1] in WITH_ARG and len(a) == 2 else 1
        continue
    host, cmd = a, argv[i + 1:]
    break
if host is None or host not in os.environ.get("FAKESSH_UP", "").split(","):
    sys.stderr.write("ssh: connect to host %%s port 22: Connection refused\\n" %% host)
    sys.exit(255)
os.execv("/bin/sh", ["/bin/sh", "-c", " ".join(cmd)])
""" % sys.executable
ADDRESS = {"a": "192.0.2.36", "b": "192.0.2.37"}
INSTALL = "Install + start node_exporter (the via:host host metrics agent)"


def _run(tmp_path, playbook, role, hosts, extra=None, env_extra=None, append=None, args=(), up=(), real=False):
    """Run the shipped `playbook` from a copy of ansible/playbooks (with logging/, a probe module whose group is `g`
    and an instance.yml), the role `role` a stub (STUB, HANDLERS) or, when `real`, the shipped role of that name, over
    `hosts` in group `g`, with `extra` as one JSON `-e` and `args` after it, each text in `append` added to the copy's
    file it names, a key dir under `tmp_path` holding one stale key, and FAKE_SSH first on PATH, answering for the
    addresses in `up`. Return the exit code, the output, each host's failed count from the recap, and whether the key
    dir is gone."""
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
        (stub / "handlers" / "main.yml").write_text(yaml.safe_dump(HANDLERS))
    for rel, text in (append or {}).items():
        with open(str(tree / rel), "a", encoding="utf-8") as fh:
            fh.write(text)
    (tree / "inv.yml").write_text(yaml.safe_dump({"all": {"children": {"g": {"hosts": hosts}}}}))
    (tree / "ansible" / "ansible.cfg").write_text("[defaults]\nroles_path = roles\nretry_files_enabled = False\n"
                                                  "[ssh_connection]\npipelining = True\n")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    (keys / "h.key").write_text("not a key: a stale file the cleanup must remove\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(FAKE_SSH)
    os.chmod(str(bin_dir / "ssh"), 0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(tree / "ansible" / "ansible.cfg"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"), ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_HOST_KEY_CHECKING="False", KONTROLL_SSH_KEY_DIR=str(keys),
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), FAKESSH_UP=",".join(up),
               **(env_extra or {}))
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


def _wire(tmp_path, extra, role=WIRE_ROLE, env_extra=None, refused=True, only=None, append=None, args=()):
    hosts = {"a": _ssh("192.0.2.36"), "b": _ssh("192.0.2.37")}
    if only:
        hosts = {h: hosts[h] for h in only}
    if refused:
        hosts["n"] = {"ansible_connection": "local", "ansible_host": "192.0.2.30"}
    return _run(tmp_path, "wire-logging.yml", WIRE_ROLE, hosts, dict(role=role, target="probe", **extra), env_extra,
                append, args)


def _stopped(host, task, what="wiring"):
    return '"msg": "%s: FAILED: the %s stopped at the task \\"%s\\""' % (host, what, task)


def _no_task_of_its_own(host, role="the wiring role %s" % WIRE_ROLE):
    return ('"msg": "%s: stopped with no failed task of its own: %s could not be loaded, or another host\'s failure '
            'stopped it (a run_once task, a caller\'s any_errors_fatal); the cause is printed above"' % (host, role))


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


def test_a_wiring_role_that_cannot_be_loaded_fails_its_host_beside_a_good_one(tmp_path):
    """Host a's stub includes a task file that does not exist, which is no task failure: the host reaches the rescue
    with no failed task, and the rescue's `fail` fails it with the message that names both causes it cannot tell apart
    (its recap counts the load error and the `fail`, on both cores); b is wired, the key dir is gone, and the run fails
    (rc 2). Guards the load failure left to the record alone: the host is dropped from every later play, so no summary
    would fail it and the run would end green (the I-12 reads' S1, in backup-configs.yml's rescue)."""
    rc, out, recap, gone = _wire(tmp_path, {"stub_load_fail": ["a"]})
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


def _install(tmp_path, extra, up=("a", "b"), real=False, args=()):
    """install-node-exporter.yml over hosts a and b, those in `up` answering through FAKE_SSH, `args` after the `-e`."""
    hosts = {h: _ssh(address) for h, address in ADDRESS.items()}
    return _run(tmp_path, "install-node-exporter.yml", "node_exporter", hosts, dict(target="g", **extra),
                up=[ADDRESS[h] for h in up], real=real, args=args)


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
    into a failed run; and I-89: the install ran on a host whose gathering was unreachable, with no facts."""
    rc, out, recap, gone = _install(tmp_path, {}, up=())
    assert rc == 0 and gone, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "FAILED: the install" not in out and "STUB a" not in out and "STUB b" not in out, out
    assert re.search(r"\[a\]: UNREACHABLE!", out) and re.search(r"\[b\]: UNREACHABLE!", out), out
    skipped = _task(out, INSTALL)
    assert "skipping: [a]" in skipped and "skipping: [b]" in skipped, skipped


def test_a_down_host_beside_an_up_one_gets_nothing_run_and_the_up_one_installs(tmp_path):
    """Host a answers through the stand-in `ssh` and b does not: a's gathering and its stub install run (to its last
    step), b's install is skipped, the run passes (rc 0) with neither named FAILED, and the key dir is gone. Guards
    I-89's skip against reading another host's gathering (a skipped beside a down host, or b run beside an up one),
    and the up host's gathering, through the stand-in, against a row that cannot reach the stub at all."""
    rc, out, recap, gone = _install(tmp_path, {}, up=("a",))
    assert rc == 0 and gone, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "AFTER a" in out and "STUB b" not in out and "FAILED: the install" not in out, out
    assert not re.search(r"\[a\]: UNREACHABLE!", out) and re.search(r"\[b\]: UNREACHABLE!", out), out
    included = _task(out, INSTALL)
    assert "skipping: [b]" in included and "skipping: [a]" not in included, included


def test_a_dry_run_over_a_down_host_beside_an_up_one_skips_the_down_one(tmp_path):
    """`--check --diff`, the dry run CLAUDE.md asks for first, over host a answering through the stand-in `ssh` and b
    not: a's stub install runs (to its last step), b's install is skipped, and the run passes (rc 0) with neither named
    FAILED. Guards I-89 in the dry run, which a down host failed too before the fix: a `when` that lets a dry run past
    the skip (`... or ansible_check_mode`) passes every real-run row. The key dir is not asserted here: what a dry
    run's cleanup removes is tests/integration/test_ssh_key_render_runs.py's."""
    rc, out, recap, _gone = _install(tmp_path, {}, up=("a",), args=("--check", "--diff"))
    assert rc == 0, out
    assert recap == {"a": 0, "b": 0}, (recap, out)
    assert "AFTER a" in out and "STUB b" not in out and "FAILED: the install" not in out, out
    assert re.search(r"\[b\]: UNREACHABLE!", out), out
    included = _task(out, INSTALL)
    assert "skipping: [b]" in included and "skipping: [a]" not in included, included


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
