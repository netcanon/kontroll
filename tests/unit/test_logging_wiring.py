"""Pins the device-side logging WIRING layer so the blind-joe ENACT path can't reference phantom artifacts.

logsvc.logging_enact_commands emits, for a PUSH logging method, an `ansible-playbook wire-logging.yml -e
role=<wiring_role> -e target=<key>` step the operator runs to make logs flow. Nothing in the pure-python test
suite would notice if that playbook, the named role, or the backend entrypoint the role delegates to didn't
exist — the enact would simply fail at apply time on the operator's machine. These tests make the enact
contract real: every wiring_role a method names is a built role (with a backup entrypoint per CLAUDE.md), the
dispatcher exists, and the backend `configure` entrypoint the syslog push path needs is present. And (I-103) that
the push tells a network device that took no connection (not run, which passes the run, and a report after the key
cleanup names it) from one that took the connection and failed, past the one jump-host test both probing plays
hold; and (I-92) that every method's wiring connections are ones on which the run names a host it could not reach.
"""
import ast
import os
import re

import pytest
import yaml

from kontroll import catalog, paths

pytestmark = pytest.mark.unit

ROLES = os.path.join(paths.ROOT, "ansible", "roles")
PLAYBOOKS = os.path.join(paths.ROOT, "ansible", "playbooks")


def test_wire_logging_dispatcher_playbook_exists():
    """The dispatcher logsvc.logging_enact_commands references (ansible/playbooks/wire-logging.yml) must exist,
    or every push-method ENACT step points at a playbook that isn't there (the failure that shipped: the enact
    string referenced wire-logging.yml before it was built)."""
    assert os.path.exists(os.path.join(PLAYBOOKS, "wire-logging.yml"))


def test_every_named_wiring_role_is_built_with_a_backup_entrypoint():
    """Each logging method that declares a wiring_role must be a real role with tasks/main.yml (the wiring),
    tasks/backup.yml (the rollback reference — CLAUDE.md: new roles carry a backup entrypoint), and a README.
    Guards a descriptor naming a role that was never built — the include_role would fail at apply time."""
    referenced = sorted({m.get("wiring_role") for m in catalog.load_logging() if m.get("wiring_role")})
    assert referenced, "expected at least syslog_push to declare a wiring_role"
    for role in referenced:
        rdir = os.path.join(ROLES, role)
        assert os.path.exists(os.path.join(rdir, "tasks", "main.yml")), "%s: no tasks/main.yml" % role
        assert os.path.exists(os.path.join(rdir, "tasks", "backup.yml")), "%s: no tasks/backup.yml" % role
        assert os.path.exists(os.path.join(rdir, "README.md")), "%s: no README.md" % role


def test_backend_configure_entrypoint_exists_for_syslog_push():
    """syslog_push's wiring role delegates to backend_netcommon_cli `tasks_from: configure` (the device_role
    dispatch seam) — that entrypoint had to be added (the backend previously had only check/backup). Guards
    include_role resolving a missing tasks file at apply time."""
    assert os.path.exists(os.path.join(ROLES, "backend_netcommon_cli", "tasks", "configure.yml"))


def _yaml(*rel):
    with open(os.path.join(paths.ROOT, *rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_the_cli_push_tells_a_device_not_reached_from_a_push_that_failed():
    """backend_netcommon_cli's `configure` clears `kontroll_configure_unreached`, then pushes in a block whose one task
    is the `no_log` `cli_config`; the rescue probes the device's port once (a `wait_for` on the controller, out of
    check mode, its failure ignored, not made behind a jump host), records the device not run where the probe found
    no connection, and fails where it found one (or made none); after the block an unreachable push is recorded not
    run, and the result record runs only for a push that neither failed nor was unreachable. Guards I-103: under
    network_cli a device that takes no connection fails the push as a task, not as unreachable, so one network device
    down failed the whole wiring run; and the ways the rescue would mislead: a probe left to check mode or made from
    the device's side, a probe whose failure fails the host, the jump host probed (the controller cannot reach its
    address, so every push that failed there would read as not reached), the record and the fail swapped (a refused
    credential would read as a device that is down), a result record that reads a failed push as already applied, or
    a rescue that reads the push's no_log result. tests/integration/test_wire_logging_unreached_runs.py runs it."""
    tasks = _yaml("ansible", "roles", "backend_netcommon_cli", "tasks", "configure.yml")
    clear, push, unreachable, result = tasks
    assert clear["ansible.builtin.set_fact"] == {"kontroll_configure_unreached": False}, clear
    (cli,) = push["block"]
    assert cli["no_log"] is True and cli["register"] == "_cli_config" and "ansible.netcommon.cli_config" in cli, cli
    probe, record, fails = push["rescue"]
    assert probe["delegate_to"] == "localhost" and probe["check_mode"] is False and probe["ignore_errors"] is True
    assert probe["register"] == "_cli_push_probe" and "ansible.builtin.wait_for" in probe, probe
    assert probe["when"] == "not (kontroll_via_jump | default(false) | bool)", probe
    assert record["when"] == "_cli_push_probe is failed", record
    assert record["ansible.builtin.set_fact"]["kontroll_configure_unreached"] is True, record
    assert fails["when"] == "_cli_push_probe is not failed" and "ansible.builtin.fail" in fails, fails
    assert "_cli_config" not in yaml.safe_dump(push["rescue"]), push["rescue"]
    assert unreachable["when"] == "_cli_config is unreachable", unreachable
    assert unreachable["ansible.builtin.set_fact"]["kontroll_configure_unreached"] is True, unreachable
    assert result["when"] == "_cli_config is not failed and _cli_config is not unreachable", result
    assert set(result["ansible.builtin.set_fact"]) == {"kontroll_configure_result"}, result


def test_both_probing_plays_hold_one_jump_host_test_and_nothing_reads_another():
    """backup-configs.yml's capture play and wire-logging.yml's device play each hold `kontroll_via_jump` as a play
    var, the same expression, reading each proxy form (a ProxyCommand, ProxyJump or `-J` in the SSH arguments, a
    paramiko or libssh proxy command); and every name ending `via_jump` that a playbook, a task file or a role reads
    is that one. Guards I-103: the CLI backend's push probe reads the flag the backup's probes read, so a probe that
    read a name its play does not define (the backup play's was `kontroll_backup_via_jump`) reads the default false
    and probes a device behind a jump host, which the controller cannot reach, and every failure there reads as not
    reached; and two copies that drift. A play var, not a vars file: the group-dispatch scan
    (test_group_dispatch_refuses_local.py) reads no vars file, so it holds a play that loads one as one that may set
    `device_role`, and the capture play would have to open with the connection refusal."""
    found = {}
    for name, title in (("backup-configs.yml", "Capture device configs"),
                        ("wire-logging.yml", "Wire the logging method onto the target device class")):
        (play,) = [p for p in _yaml("ansible", "playbooks", name) if str(p.get("name", "")).startswith(title)]
        assert "vars_files" not in play, (name, play["vars_files"])
        found[name] = play["vars"]["kontroll_via_jump"]
    assert len(set(found.values())) == 1, found
    flag = found["wire-logging.yml"]
    for form in ("ansible_paramiko_proxy_command", "ansible_libssh_proxy_command", "ansible_ssh_common_args",
                 "ansible_ssh_extra_args", "ansible_ssh_args", "proxycommand|proxyjump|(^| )-J"):
        assert form in flag, form
    names = set()
    for base, _dirs, files in os.walk(os.path.join(paths.ROOT, "ansible")):
        if "collections" in base.split(os.sep):
            continue
        for f in files:
            if f.endswith((".yml", ".yaml")):
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    names |= set(re.findall(r"\b\w*via_jump\b", fh.read()))
    assert names == {"kontroll_via_jump"}, names


def test_the_wiring_report_names_each_device_not_run_after_the_cleanup():
    """After wire-logging.yml's key cleanup a play over the device play's own `hosts:` pattern, on the controller,
    gathering nothing, runs first a `debug` naming the host and its `kontroll_configure_result` where
    `kontroll_configure_unreached` is true (its two lines after it, an ssh host's, are
    tests/unit/test_fleet_play_rescue.py's: I-92). Guards I-103's report: a device that took no connection passes the
    run, and with the report dropped (or run over another pattern, or reading another record) nothing but a probe's
    line would say the device was never wired."""
    plays = _yaml("ansible", "playbooks", "wire-logging.yml")
    cleanup = [i for i, p in enumerate(plays) if "_cleanup-ssh-keys" in str(p.get("ansible.builtin.import_playbook"))]
    (device,) = [p for p in plays if p.get("name") == "Wire the logging method onto the target device class"]
    (report,) = [p for p in plays if p.get("name") == "Wiring run report"]
    assert plays.index(report) > cleanup[0], "the report runs before the key cleanup"
    assert report["hosts"] == device["hosts"] and report["connection"] == "local", report
    assert report["gather_facts"] is False, report
    task = report["tasks"][0]
    assert task["ansible.builtin.debug"] == {"msg": "{{ inventory_hostname }}: {{ kontroll_configure_result }}"}, task
    assert task["when"] == "kontroll_configure_unreached | default(false) | bool", task


# The network_cli spellings: the CLI backend's push (backend_netcommon_cli's `configure`) probes a device it could not
# reach, and the run's report names it (I-103).
NETWORK_CLI = {"network_cli", "ansible.netcommon.network_cli"}


def test_every_methods_wiring_connections_are_ones_the_run_names_an_unreached_host_on():
    """Each logging method that names a wiring role lists `wiring_connections` all of the ssh family wire-logging.yml's
    device play probes (the literal list its probe's `when` names: a host there is probed before its role and re-checked
    after it, and the report names one that answers neither), or all network_cli, where its role pushes through
    backend_netcommon_cli's `configure`, whose probe names a device that took no connection (I-103). Guards a method
    added on another connection (an API, a container), whose host the play neither probes nor re-checks: down or
    dropping, its wiring would pass the run with nothing naming it but its UNREACHABLE lines, as every ssh host's did
    before I-92."""
    (device,) = [p for p in _yaml("ansible", "playbooks", "wire-logging.yml")
                 if p.get("name") == "Wire the logging method onto the target device class"]
    (probe,) = [t for b in device["tasks"] for t in b.get("block") or [] if t.get("register") == "_kontroll_wire_probe"]
    family = set(ast.literal_eval(probe["when"].split(" in ", 1)[1]))
    methods = [m for m in catalog.load_logging() if m.get("wiring_role")]
    assert methods, "no logging method names a wiring role"
    for method in methods:
        conns = set(method.get("wiring_connections") or [])
        assert conns, method
        if conns <= family:
            continue
        assert conns <= NETWORK_CLI, (method["wiring_role"], conns)
        tasks = _yaml("ansible", "roles", method["wiring_role"], "tasks", "main.yml")
        pushes = [t for t in tasks
                  if (t.get("ansible.builtin.include_role") or {}).get("name") == "backend_netcommon_cli"]
        assert pushes and all(t["ansible.builtin.include_role"].get("tasks_from") == "configure" for t in pushes), tasks
