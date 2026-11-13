"""ansible-playbook runs ansible/playbooks/tasks/refuse-local-connection.yml as a group-dispatched play imports it,
over an inventory of the connection spellings its reads measured, a templated `local`, chroot and `smart`, and
templates that render differently by context: only a host on a connection in the play's allowlist gets past it, and
every later task on that host, in the play and in a role, runs over the connection the refusal passed.

WHY. The static scan (tests/unit/test_group_dispatch_refuses_local.py) holds the refusal's text; it cannot see
what ansible-core puts in `ansible_connection` when the assert runs. The first cut refused two spellings of `local`
and read an unset connection through a `default` that never fired. Its read (2026-10-23-local-refusal, B1, N1)
measured four more spellings of the local plugin, network_cli, httpapi and netconf, and a templated value reaching
the role on the controller, and that ansible-core fills an unset connection with the plugin it loaded. Fold 1's read
(A, S1) measured a host var that renders `ssh` while the refusal runs and `local` in the role, which ran the role on
the controller; the refusal now freezes the connection, and these rows run that too. They run the shipped file under
the installed ansible-core, so a refusal that passes a host it should stop fails here.

Nothing contacts a host: the play gathers no facts, and its only tasks are the refusal (an `assert` and a `set_fact`)
and `debug`s, in the play and in a stub role, all actions on the controller. Every address is TEST-NET-1. It needs
ansible-playbook on PATH; tests/validate.sh runs it in the validate image, on the pinned cores. ansible-core 2.21
removed the paramiko_ssh connection plugin (2.20 deprecated it), so on a core from 2.21 on (a developer's; CI's pytest
legs run the shipped cores: I-94), the two hosts on it fail at the plugin load, before the refusal's condition, and the
rows expect that (`_NO_PARAMIKO`). Five hosts connect by a collection's plugin (network_cli bare and by its FQCN,
httpapi and netconf from ansible.netcommon, with the cisco.ios cliconf and the fortinet.fortios httpapi plugin; chroot
from community.general): where the run's Ansible lacks any of them (another uid, where an image keeps its collections
under root's home), those hosts failed at loading it, at the refusal's own task: the failed load, not the refusal, held
them out of every row, and the rows that name them failed. Every row then skips, naming them (I-76). tests/validate.sh
runs it at a step that fails on a skipped row.
"""
import os
import re
import shutil
import subprocess

import pytest

import _network_lab

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook on PATH"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REFUSAL = os.path.join(ROOT, "ansible", "playbooks", "tasks", "refuse-local-connection.yml")
SSH = ["ssh", "ansible.builtin.ssh", "paramiko_ssh", "ansible.builtin.paramiko_ssh"]
_HOSTS = {
    # `ssh` while `loc` is in the play, `local` once the refusal has failed it: at the freeze, one task later.
    # Listed first, so that its check is queued before `loc` has run: the hosts run each task side by side.
    "drift": {"ansible_connection": "{{ 'ssh' if 'loc' in ansible_play_hosts else 'local' }}",
              "ansible_host": "192.0.2.42"},
    "unset": {"ansible_host": "192.0.2.35"},
    "ssh": {"ansible_connection": "ssh", "ansible_host": "192.0.2.36"},
    "bssh": {"ansible_connection": "ansible.builtin.ssh", "ansible_host": "192.0.2.37"},
    "pk": {"ansible_connection": "paramiko_ssh", "ansible_host": "192.0.2.38"},
    "bpk": {"ansible_connection": "ansible.builtin.paramiko_ssh", "ansible_host": "192.0.2.39"},
    "loc": {"ansible_connection": "local"},
    "bloc": {"ansible_connection": "ansible.builtin.local"},
    "legloc": {"ansible_connection": "ansible.legacy.local"},
    "redir": {"ansible_connection": "redirected_local"},
    "bredir": {"ansible_connection": "ansible.builtin.redirected_local"},
    "lredir": {"ansible_connection": "ansible.legacy.redirected_local"},
    "tmpl": {"ansible_connection": "{{ 'lo' ~ 'cal' }}"},
    "ncli": {"ansible_connection": "network_cli", "ansible_network_os": "cisco.ios.ios", "ansible_host": "192.0.2.30"},
    "fncli": {"ansible_connection": "ansible.netcommon.network_cli", "ansible_network_os": "cisco.ios.ios",
              "ansible_host": "192.0.2.31"},
    "hapi": {"ansible_connection": "httpapi", "ansible_network_os": "fortinet.fortios.fortios",
             "ansible_host": "192.0.2.32"},
    "ncf": {"ansible_connection": "ansible.netcommon.netconf", "ansible_network_os": "junipernetworks.junos.junos",
            "ansible_host": "192.0.2.33"},
    "chr": {"ansible_connection": "community.general.chroot", "ansible_host": "/"},
    "smrt": {"ansible_connection": "smart", "ansible_host": "192.0.2.34"},
    # `ssh` while the import's vars are in scope, `local` after it; `local` inside a role, `ssh` outside one.
    "toc1": {"ansible_connection": "{{ 'ssh' if refusal_connections is defined else 'local' }}",
             "ansible_host": "192.0.2.40"},
    "toc2": {"ansible_connection": "{{ 'local' if role_name is defined else 'ssh' }}", "ansible_host": "192.0.2.41"},
    # renders to the text of a template, which a later render would turn into `ssh` or `local`
    "dbl": {"ansible_connection": "{{ '{{' }} 'ssh' if refusal_connections is defined else 'local' {{ '}}' }}",
            "ansible_host": "192.0.2.43"},
}


def _core():
    """(major, minor) of the ansible-core the `ansible-playbook` on PATH runs, read from its `--version` line
    (`ansible-playbook [core 2.19.4]`); None where there is none or the line does not parse."""
    if shutil.which("ansible-playbook") is None:
        return None
    out = subprocess.run(["ansible-playbook", "--version"], capture_output=True, text=True, timeout=300).stdout
    m = re.search(r"\[core (\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ansible-core 2.21 removed the paramiko_ssh connection plugin (2.20 deprecated it, `removed_in: "2.21"`): there a host
# on it fails at the plugin load, before the refusal's condition, as `drift` and `dbl` do. The shipped cores have it,
# and CI's pytest legs run them (I-94); a developer's core may be newer. A version line that does not parse keeps the
# two hosts, so a misread fails the rows rather than drops them.
_NO_PARAMIKO = {"pk", "bpk"} if (_core() or (0, 0)) >= (2, 21) else set()
_FAILS_AT_LOAD = {"drift", "dbl"} | _NO_PARAMIKO   # past the assert's condition or never at it: the load fails the host
# The collection plugins the hosts above connect by (ncli, fncli, hapi, ncf, chr). Every row runs under the same
# configuration (an empty `[defaults]`) and environment, so the first row's answer is every row's (I-76).
_COLLECTION_PLUGINS = (("connection", "ansible.netcommon.network_cli"),
                       ("cliconf", _HOSTS["ncli"]["ansible_network_os"]),
                       ("connection", "ansible.netcommon.httpapi"),
                       ("httpapi", _HOSTS["hapi"]["ansible_network_os"]),
                       ("connection", "ansible.netcommon.netconf"),
                       ("connection", _HOSTS["chr"]["ansible_connection"]))
_HAS_COLLECTION_PLUGINS = []


def _run(tmp_path, allow, *extra, before="", inv=None, files=None):
    """The output of the probe play importing the refusal with `refusal_connections: allow` (`None`: the import
    passes no list), over the inventory above (each host's own vars, plus `inv`'s YAML values for it) plus the
    implicit localhost, after the plays `before` holds. After the refusal it prints each host's connection, then
    includes a stub role that prints it again. Beside the plays sit `widened.yml`, a vars file holding `_WIDE`,
    `widened-template.yml`, one holding `_TMPL`, and each of `files` (a path under the plays' directory: its text).
    Where the run's Ansible lacks a plugin the network and chroot hosts connect by, the row skips, naming it (I-76)."""
    play = tmp_path / "playbooks"
    (play / "tasks").mkdir(parents=True, exist_ok=True)
    (play / "roles" / "stub" / "tasks").mkdir(parents=True, exist_ok=True)
    shutil.copy(REFUSAL, str(play / "tasks" / "refuse-local-connection.yml"))
    (play / "widened.yml").write_text("refusal_connections: %s\n" % _WIDE)
    (play / "widened-template.yml").write_text('refusal_connections: "%s"\n' % _TMPL)
    for rel, text in (files or {}).items():
        (play / rel).parent.mkdir(parents=True, exist_ok=True)
        (play / rel).write_text(text)
    (play / "roles" / "stub" / "tasks" / "main.yml").write_text(
        "- name: In the role\n  ansible.builtin.debug:\n"
        "    msg: \"ROLE {{ inventory_hostname }} {{ ansible_connection }}\"\n")
    imp = "      ansible.builtin.import_tasks: tasks/refuse-local-connection.yml\n"
    if allow is not None:
        imp += "      vars:\n        refusal_connections: %s\n" % allow
    (play / "probe.yml").write_text(
        before + "- name: Probe\n  hosts: g:localhost\n  gather_facts: false\n  tasks:\n    - name: Refuse\n" + imp +
        "    - name: Passed\n      ansible.builtin.debug:\n"
        "        msg: \"PASSED {{ inventory_hostname }} {{ ansible_connection }}\"\n"
        "    - name: Role\n      ansible.builtin.include_role:\n        name: stub\n")
    lines = ["all:", "  children:", "    g:", "      hosts:"]
    lines += ["        %s: {%s}" % (h, ", ".join(['%s: "%s"' % kv for kv in v.items()]
                                          + ["%s: %s" % kv for kv in (inv or {}).get(h, {}).items()]))
              for h, v in _HOSTS.items()]
    (tmp_path / "inv.yml").write_text("\n".join(lines) + "\n")
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")   # not the repo's: its collections_path hides the image's
    env = dict(os.environ, ANSIBLE_CONFIG=str(tmp_path / "ansible.cfg"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"), ANSIBLE_LOCALHOST_WARNING="0")
    if not _HAS_COLLECTION_PLUGINS:
        _HAS_COLLECTION_PLUGINS.append(_network_lab.has_plugins(env, *_COLLECTION_PLUGINS))
    if not _HAS_COLLECTION_PLUGINS[0]:
        pytest.skip("needs %s: without them the inventory's network and chroot hosts fail at loading their connection, "
                    "not at the refusal (I-76)" % ", ".join(name for _kind, name in _COLLECTION_PLUGINS))
    p = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(play / "probe.yml"), "--check"]
                       + list(extra), cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    assert "PLAY RECAP" in out, out
    return out


def _passed(tmp_path, allow, *extra, before="", inv=None, files=None):
    """The hosts that got past the refusal, and the run's output."""
    out = _run(tmp_path, allow, *extra, before=before, inv=inv, files=files)
    return set(re.findall(r'"msg": "PASSED (\S+)', out)), out


def test_only_a_host_on_an_allowlisted_connection_gets_past_the_refusal(tmp_path):
    """With the ssh family as the allowlist, the hosts on ssh (unset, so ansible-core fills in `ssh`; named;
    named by its FQCN) and paramiko_ssh (bare and by its FQCN) pass, and so do the two whose template renders `ssh`
    at the refusal. Every spelling of the local plugin, a templated `local`, network_cli (bare and by its FQCN),
    httpapi, netconf, chroot, `smart` and the implicit localhost are refused, and the refusal names the connection.
    On a core from 2.21 on, the two paramiko_ssh hosts fail at the load of a plugin that core no longer has, by that
    message and no other. Guards a refusal that lets the role run on the control node for a host whose connection
    executes there (its read, B1; fold 1's read B, S5: the FQCN network_cli and netconf rows); and the two hosts dropped
    on a newer core for any reason but the plugin's removal (the public CI's pytest jobs, then on the runner image's
    core, failed every row on them)."""
    passed, out = _passed(tmp_path, SSH)
    assert passed == {"unset", "ssh", "bssh", "pk", "bpk", "toc1", "toc2"} - _NO_PARAMIKO, out
    for host in _NO_PARAMIKO:   # a core without the plugin fails the host at its load, never past the refusal
        assert re.search(r"fatal: \[%s\]: FAILED! => .*the connection plugin '(ansible\.builtin\.)?paramiko_ssh' was "
                         r"not found" % host, out), out
    for host in (set(_HOSTS) | {"localhost"}) - passed - _FAILS_AT_LOAD:   # stopped by the refusal, not otherwise
        assert "%s: ansible_connection is " % host in out, host
    assert "ansible_connection is ansible.legacy.local; this play runs its role only over ssh" in out, out


def test_every_later_task_runs_over_the_connection_the_refusal_passed(tmp_path):
    """After the refusal, every host that passed runs the play's next task and a role's task over the allowlist's
    spelling of the connection the refusal checked: `ssh` for the two whose template renders `local` outside the
    import (toc1) or inside a role (toc2). A host whose template renders `ssh` at the check and `local` one task
    later, at the freeze (drift), passes the check and gets no further: the freeze stores '', which no plugin loads.
    A host whose value renders to a template's text (dbl) fails at the first task. Guards a refusal that checks a
    value the role never runs over: with no freeze, toc1 and toc2 ran the role over `local`, and with the host's own
    value frozen after the check, drift did, on 2.18 and 2.19 (fold 1's read A, S1)."""
    out = _run(tmp_path, SSH)
    seen = {(where, host): conn for where, host, conn in re.findall(r'"msg": "(PASSED|ROLE) (\S+) (\S*)"', out)}
    expected = {h: c for h, c in {"unset": "ssh", "ssh": "ssh", "bssh": "ansible.builtin.ssh", "pk": "paramiko_ssh",
                                  "bpk": "ansible.builtin.paramiko_ssh", "toc1": "ssh", "toc2": "ssh"}.items()
                if h not in _NO_PARAMIKO}
    assert seen == {(where, h): c for h, c in expected.items() for where in ("PASSED", "ROLE")}, out
    assert re.search(r"fatal: \[drift\]: FAILED!.*(non-empty plugin name|connection plugin '' was not found)", out), out
    assert re.search(r"fatal: \[dbl\]: FAILED!.*connection plugin .* was not found", out), out


def test_a_host_with_no_connection_is_refused_under_c_local(tmp_path):
    """Under `-c local`, ansible-core fills a host that sets no connection with `local`, and the refusal stops it;
    a host that names ssh still passes (a host var outranks `-c`). Guards the refusal reading the play context
    rather than what ansible-core puts in the host's `ansible_connection` (its read, N1)."""
    passed, out = _passed(tmp_path, SSH, "-c", "local")
    assert passed == {"ssh", "bssh", "pk", "bpk", "toc1", "toc2"} - _NO_PARAMIKO, out


_NONE = ("this play runs its role only over (none: the import passed no list, or an empty one), the connections it is "
         "written for, and never over the local plugin. Refused for this host; the other hosts go on.")
_LOCAL = "the import's list names the local plugin, so every host is refused."


@pytest.mark.parametrize("allow,why", [(None, _NONE), ("[]", _NONE), ("[ssh, local]", _LOCAL),
                                       ("[ssh, ansible.legacy.redirected_local]", _LOCAL)],
                         ids=["no-list", "empty-list", "list-with-local", "list-with-a-redirect-to-local"])
def test_an_import_with_no_list_or_a_list_naming_the_local_plugin_refuses_every_host(tmp_path, allow, why):
    """An import that passes no allowlist, an empty one, or one that names the local plugin under any spelling refuses
    every host, the ssh ones too, and the refusal says which: no list or an empty one, or a list that names the local
    plugin. Guards a play that forgets its list, or widens it to `local`, running its role anywhere: the refusal fails
    closed rather than open; and a message that called an empty list no list, or refused an ssh host under a list naming
    `local` by naming its connection, as if that were the reason (fold 11's read A, N2)."""
    passed, out = _passed(tmp_path, allow)
    assert passed == set(), out
    for host in (set(_HOSTS) | {"localhost"}) - _FAILS_AT_LOAD:
        assert re.search(r'"msg": "%s: [^"]*%s"' % (re.escape(host), re.escape(why)), out), host


_WIDE = "[ssh, network_cli]"
_TMPL = "{{ ['ssh', 'network_cli'] if ansible_play_name is defined else undefined_zz }}"   # undefined in `hostvars`
# a delegated loop stops at a host whose connection is a template, or one whose plugin the core does not have
_PLAIN = [h for h in ("unset", "ssh", "bssh", "pk", "bpk") if h not in _NO_PARAMIKO]
_ELSEWHERE = {   # a list widened to network_cli, set for some hosts before the dispatching play's import reads it
    "earlier-set-fact": ("- name: Earlier\n  hosts: g\n  gather_facts: false\n  tasks:\n"
                         "    - ansible.builtin.set_fact:\n        refusal_connections: %s\n" % _WIDE, (), None,
                         None, sorted(set(_HOSTS) - _FAILS_AT_LOAD)),
    "delegated-set-fact": ("- name: Earlier\n  hosts: localhost\n  gather_facts: false\n  tasks:\n"
                           "    - ansible.builtin.set_fact:\n        refusal_connections: %s\n"
                           "      delegate_to: \"{{ item }}\"\n      delegate_facts: true\n"
                           "      loop: [%s]\n" % (_WIDE, ", ".join(_PLAIN)), (), None, None, _PLAIN),
    "earlier-include-vars": ("- name: Earlier\n  hosts: g\n  gather_facts: false\n  tasks:\n"
                             "    - ansible.builtin.include_vars: widened.yml\n", (), None, None,
                             sorted(set(_HOSTS) - _FAILS_AT_LOAD)),
    "inventory": ("", (), {h: {"refusal_connections": _WIDE} for h in _PLAIN}, None, _PLAIN),
    # an empty list is set too: a test of its length in place of its presence passed it
    "inventory-empty": ("", (), {h: {"refusal_connections": "[]"} for h in _PLAIN}, None, _PLAIN),
    "extra-var": ("", ("-e", '{"refusal_connections": ["ssh", "network_cli"]}'), None, None,
                  sorted(set(_HOSTS) - _FAILS_AT_LOAD) + ["localhost"]),
    # a list that renders widened in a task and is undefined in `hostvars`, where 2.19 renders a test of its value
    "extra-var-template": ("", ("-e", '{"refusal_connections": "%s"}' % _TMPL), None, None,
                           sorted(set(_HOSTS) - _FAILS_AT_LOAD) + ["localhost"]),
    # a null list: read before the presence test, it failed each host with no word of the outside list (fold 12's
    # read A, N1)
    "extra-var-null": ("", ("-e", '{"refusal_connections": null}'), None, None,
                       sorted(set(_HOSTS) - _FAILS_AT_LOAD) + ["localhost"]),
    "earlier-include-vars-template": ("- name: Earlier\n  hosts: g\n  gather_facts: false\n  tasks:\n"
                                      "    - ansible.builtin.include_vars: widened-template.yml\n", (), None, None,
                                      sorted(set(_HOSTS) - _FAILS_AT_LOAD)),
    "group-vars-beside-the-playbook": ("", (), None, {"group_vars/g.yml": "refusal_connections: %s\n" % _WIDE},
                                       sorted(set(_HOSTS) - _FAILS_AT_LOAD)),
    # the list set beside the name the host is read by: a check by `hostvars[inventory_hostname]` read the
    # controller's vars for every host, and passed them (fold 12's read A, S1)
    "earlier-set-fact-beside-the-name": ("- name: Earlier\n  hosts: g\n  gather_facts: false\n  tasks:\n"
                                         "    - ansible.builtin.set_fact:\n        refusal_connections: %s\n"
                                         "        inventory_hostname: localhost\n" % _WIDE, (), None, None,
                                         sorted(set(_HOSTS) - _FAILS_AT_LOAD)),
}
_OUTSIDE = (r"[^\"]*: a `refusal_connections` is set outside the import for a host of this play \(([^;\"]*); by "
            r"the inventory, a vars file beside the playbooks, an earlier play or -e\), and only the import may name "
            r"it\. Every host of this play is refused\.\"")


@pytest.mark.parametrize("before,extra,inv,files,widened", list(_ELSEWHERE.values()), ids=list(_ELSEWHERE))
def test_a_list_set_for_the_host_outside_the_import_refuses_it(tmp_path, before, extra, inv, files, widened):
    """A `refusal_connections` set for a host anywhere but the import (an earlier play's set_fact or include_vars, a
    set_fact delegated to the host with `delegate_facts`, an operator's `-e`, the inventory, a vars file beside the
    playbooks; by `-e` or an earlier play's include_vars, a template that renders the list widened in a task and is
    undefined in `hostvars`; a set_fact of the list beside `inventory_hostname: localhost`) is refused: all but the
    inventory's and the vars file's outrank the list the import passes, which is only task vars, and those two are in
    `hostvars` too. So the refusal refuses every host of the play, the ssh ones and those the list was not set for
    too, each with one message that says the list is set outside the import, names among the hosts that hold it every
    host it was set for, and names no list and no connection. A host is found by ansible-core's own `fatal: [<host>]`,
    since the planted name changes the one the message prints. Guards the allowlist being widened from outside the
    play: with a
    list an earlier play left, install-node-exporter.yml ran node_exporter over network_cli on the controller on 2.18
    and 2.19, with every scan test and the static gate green (fold 9's read A, S3); and a message that misnamed the
    list, calling an outside one "the connections it is written for", or the import's own one set outside it (fold 10's
    read A, N1); and, on 2.19, a test of the list's value in place of its presence: rendered in the host's own context,
    the template's list was undefined there, so it passed, while the check read it widened, and the role ran over
    network_cli (fold 11's read A, S1). An empty list the inventory sets is refused as well: a test of the list's
    length in place of its presence passed every row on 2.18 (fold 12's own mutant run). A check of the host's own
    name, `hostvars[inventory_hostname]`, passed every host once the list was set beside `inventory_hostname:
    localhost`, and the role ran over network_cli (fold 12's read A, S1). A null list set by `-e`, read before the
    presence test, failed every host with no word of the outside list (its N1)."""
    passed, out = _passed(tmp_path, SSH, *extra, before=before, inv=inv, files=files)
    assert passed == set(), out   # one host's outside list refuses every host of the play
    for host in (set(_HOSTS) | {"localhost"}) - _FAILS_AT_LOAD:   # by the host ansible-core ran, not a printed name
        m = re.search(r'fatal: \[%s\]: FAILED! => .*"msg": "' % re.escape(host) + _OUTSIDE, out)
        assert m, host
        assert set(widened) <= set(m.group(1).split(", ")), (host, m.group(1))
        assert not re.search(r'fatal: \[%s\]: FAILED! => .*"msg": "[^"]*: ansible_connection is ' % re.escape(host),
                             out), host
