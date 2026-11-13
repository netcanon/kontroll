"""ansible-playbook runs the shipped ansible/playbooks/wire-logging.yml for each wiring role a logging method names,
over a group holding one host per connection family, and only the hosts on that method's `wiring_connections` reach
the role.

WHY. The unit test (tests/unit/test_group_dispatch_refuses_local.py) pins the string wire-logging.yml passes as
`refusal_connections` (`hostvars['localhost']._wire_connections`), and the runtime refusal test
(test_refuse_local_connection_runtime.py) runs the refusal on a literal list; neither runs the localhost play that
resolves `_wire_connections` from logging/<method>.yml. With its `selectattr('wiring_role', 'equalto', role)` dropped,
every method's list is unioned, and a network_cli host reached logging_journald_remote, whose lineinfile then ran on
the control node (fold 1's read B, S3: W8, W9). Each wiring role is a stub (`debug`), so nothing touches a host; every
address is TEST-NET-1. It needs ansible-playbook on PATH; tests/validate.sh runs it in the validate image.
"""
import os
import re
import shutil
import subprocess

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook on PATH"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOSTS = {
    "ssh": {"ansible_connection": "ssh", "ansible_host": "192.0.2.36"},
    "unset": {"ansible_host": "192.0.2.35"},
    "ncli": {"ansible_connection": "network_cli", "ansible_network_os": "cisco.ios.ios", "ansible_host": "192.0.2.30"},
    "fncli": {"ansible_connection": "ansible.netcommon.network_cli", "ansible_network_os": "cisco.ios.ios",
              "ansible_host": "192.0.2.31"},
    "hapi": {"ansible_connection": "httpapi", "ansible_network_os": "fortinet.fortios.fortios",
             "ansible_host": "192.0.2.32"},
    "loc": {"ansible_connection": "local"},
}
# What ansible-core puts in `ansible_connection` for each host when the refusal runs (an unset host: `ssh`).
_FILLED = {"ssh": "ssh", "unset": "ssh", "ncli": "network_cli", "fncli": "ansible.netcommon.network_cli",
           "hapi": "httpapi", "loc": "local"}


def _methods():
    """Every logging/<method>.yml (or .yaml, as wire-logging.yml reads both) that names a wiring role."""
    out = []
    for name in sorted(os.listdir(os.path.join(ROOT, "logging"))):
        if name.endswith((".yml", ".yaml")):
            with open(os.path.join(ROOT, "logging", name), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            if isinstance(doc, dict) and doc.get("wiring_role"):
                out.append(doc)
    return out


def _wire(tmp_path, role, as_yaml=()):
    """The hosts that reached the stub of `role` when the real wire-logging.yml ran in `--check` with `-e role=role`
    over the group above, and the run's output. The methods named in `as_yaml` are written as `<method>.yaml`. Two
    methods may name one wiring role (wire-logging.yml unions their lists), so a role's stub is written once. The stub
    reports the host's `ansible_connection` as the role's task sees it, which must be the value the refusal passed
    for that host (a loop variable or a templated `set_fact` key in the play would otherwise run it on the controller
    unseen: fold 3's read A, S3), and the delegate it ran on, which must be none: a delegated block or `apply:`, or a
    `local_action:` import, runs it on the controller while `ansible_connection` still reads `ssh` (fold 4's read A,
    S2; read B, N2). The run must reach the key cleanup (`_cleanup-ssh-keys.yml`, imported last), which runs on a
    key dir under `tmp_path` (never the developer's) holding one file, and must report `changed`, not skip, and not
    fail, and the dir must be gone: the fold's docs lean on it running whenever a target passes (fold 2's read B, N3),
    and a dry run removes its own (the real run's path with `.check` appended), as a real run does, leaving a real
    run's dir as it was (I-87). Beside the copied playbooks, `host_vars/localhost.yml` seeds `_wire_methods` with every
    wiring role on every connection family the group holds but `local` (which the refusal refuses in any list): the
    validation play appends to that list, and a seed it read widened each role's allowlist (local-refusal fold 12's
    read A, S2), so every run here also holds that the method list starts empty."""
    tree = tmp_path / "t"
    shutil.copytree(os.path.join(ROOT, "ansible", "playbooks"), str(tree / "ansible" / "playbooks"))
    (tree / "ansible" / "playbooks" / "host_vars").mkdir(exist_ok=True)
    (tree / "ansible" / "playbooks" / "host_vars" / "localhost.yml").write_text(yaml.safe_dump({"_wire_methods": [
        {"wiring_role": m["wiring_role"], "wiring_connections": ["ssh", "network_cli", "ansible.netcommon.network_cli",
                                                                "httpapi"]} for m in _methods()]}))
    shutil.copytree(os.path.join(ROOT, "logging"), str(tree / "logging"))
    for name in as_yaml:
        os.rename(str(tree / "logging" / (name + ".yml")), str(tree / "logging" / (name + ".yaml")))
    (tree / "modules" / "probe").mkdir(parents=True)
    (tree / "modules" / "probe" / "module.yml").write_text("inventory_group: g\n")
    (tree / "instance").mkdir()
    (tree / "instance" / "instance.yml").write_text("mgmt_ip: 192.0.2.5\n")
    for m in _methods():
        stub = tree / "ansible" / "roles" / m["wiring_role"] / "tasks"
        stub.mkdir(parents=True, exist_ok=True)
        (stub / "main.yml").write_text(
            '- name: Stub\n  ansible.builtin.debug:\n'
            '    msg: "PASSED {{ inventory_hostname }} {{ ansible_connection }} '
            '{{ (ansible_delegated_vars | default({})) | list | join(\',\') or \'-\' }}"\n')
    lines = ["all:", "  children:", "    g:", "      hosts:"]
    lines += ["        %s: {%s}" % (h, ", ".join('%s: "%s"' % kv for kv in v.items())) for h, v in _HOSTS.items()]
    (tree / "inv.yml").write_text("\n".join(lines) + "\n")
    (tree / "ansible" / "ansible.cfg").write_text("[defaults]\nroles_path = roles\n")
    env = dict(os.environ, ANSIBLE_CONFIG=str(tree / "ansible" / "ansible.cfg"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"), ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(tmp_path / "keys"))
    for held in ("keys", "keys.check"):   # the real run's key dir, and the dry run's own (`.check`: I-87)
        (tmp_path / held).mkdir()
        (tmp_path / held / "h.key").write_text("not a key\n")
    p = subprocess.run(["ansible-playbook", "-i", str(tree / "inv.yml"), "playbooks/wire-logging.yml", "--check",
                        "-e", "role=%s" % role, "-e", "target=probe"],
                       cwd=str(tree / "ansible"), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    assert "PLAY RECAP" in out, out
    cleanup = out.split("TASK [Delete the materialized per-host SSH keys (ephemeral)]", 1)
    assert len(cleanup) == 2 and "fatal:" not in cleanup[1].split("PLAY RECAP")[0], out
    assert "changed: [" in cleanup[1].split("PLAY RECAP")[0], out   # it ran on the key dir
    assert not (tmp_path / "keys.check").exists(), out                  # and removed it, in --check too (I-87)
    assert os.listdir(str(tmp_path / "keys")) == ["h.key"], out          # a real run's dir is not a dry run's
    ran = re.findall(r'"msg": "PASSED (\S+) (\S+) (\S+)"', out)
    assert all(conn == _FILLED[host] for host, conn, _to in ran), ran   # still the value the refusal passed
    assert all(to == "-" for _host, _conn, to in ran), ran                # on the host, not a delegate
    return {host for host, _conn, _to in ran}, out


def _expected(method):
    """The hosts whose connection, as ansible-core fills it in, is on `method`'s `wiring_connections`."""
    return {h for h, c in _FILLED.items() if c in method["wiring_connections"]}


def test_every_wiring_role_has_a_method_to_run():
    """At least one logging method names a wiring role, so the parametrized test below runs rather than passing over
    nothing. Guards the method files moving or renaming the key, which would leave the allowlist resolution untested
    with every check green."""
    assert _methods()


@pytest.mark.parametrize("method", _methods(), ids=lambda m: m["wiring_role"])
def test_only_a_host_on_the_methods_wiring_connections_reaches_the_wiring_role(tmp_path, method):
    """For each wiring role, the real wire-logging.yml, in `--check`, lets through to the role exactly the hosts
    whose connection is in that method's `wiring_connections`: for syslog_push, network_cli bare and by its FQCN;
    for the others, ssh and a host that names none. Guards the localhost play's resolution reading another method's
    list, or every method's, and so running a role on the control node for a host on another method's connection
    (fold 1's read B, S3), or reading a method list seeded beside the playbooks (local-refusal fold 12's read A,
    S2)."""
    passed, out = _wire(tmp_path, method["wiring_role"])
    assert passed == _expected(method), out


def test_a_method_written_as_yaml_is_read(tmp_path):
    """A method file named `<method>.yaml`, which the catalog offers as it does a `.yml` one, gives wire-logging.yml
    its list too: the role runs over that method's connections rather than the run stopping at "no
    logging/<method>.yml names its wiring_connections". Guards the validation play globbing `*.yml` alone, which turned
    a drop-in the catalog and logsvc accept into an enact that never runs (fold 1's read B, N4)."""
    method = next(m for m in _methods() if m["wiring_role"] == "logging_journald_remote")
    passed, out = _wire(tmp_path, method["wiring_role"], as_yaml=("journald_remote",))
    assert passed == _expected(method), out
