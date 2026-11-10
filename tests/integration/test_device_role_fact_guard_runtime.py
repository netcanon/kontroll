"""ansible-playbook runs ansible/playbooks/tasks/refuse-device-role-fact.yml as a play that dispatches by
`{{ device_role }}` imports it, after an earlier play whose task sets `device_role` as a fact: the host so set is
refused, whatever module set it, and a host whose `device_role` only the inventory names gets past it.

WHY. backup-configs.yml and ping.yml include `{{ device_role }}`, so a host runs the role its inventory names. A
module that returns facts sets a host fact, which outranks the inventory: an earlier play's `ansible.utils.update_fact`
made a `local` host run a fixed role from backup-configs.yml on 2.18 and 2.19 with the scan
(tests/unit/test_group_dispatch_refuses_local.py) green, as did its `action:` form, a play's `module_defaults`, a
templated path and a `!vault` one (local-refusal fold 12's read A, S3). The scan names the setters it knows; the guard
refuses the class by where the value lands (`ansible_facts`). These rows run it under the installed ansible-core after
three setters of different kinds: update_fact itself, a module in a `library/` beside the playbooks, and a `set_fact`
with `cacheable: true`. Each row first runs without the guard, to show that its setter does make the host dispatch by
the fixed role. The unit test holds that each play that dispatches by `{{ device_role }}` imports the guard first.

Nothing contacts a host: both hosts are `local`, the plays gather no facts, and their tasks are the setter, the guard
and a `debug`, all on the controller, in a directory under `tmp_path`. It needs ansible-playbook on PATH and the
ansible.utils collection; tests/validate.sh runs it in the validate image.
"""
import os
import re
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook on PATH"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.path.join(ROOT, "ansible", "playbooks", "tasks", "refuse-device-role-fact.yml")
_MODULE = ("#!/usr/bin/python\nfrom ansible.module_utils.basic import AnsibleModule\n\n\n"
           "def main():\n    module = AnsibleModule(argument_spec={})\n"
           "    module.exit_json(changed=False, ansible_facts={'device_role': 'fixed'})\n\n\nmain()\n")
_SETTERS = {   # an earlier play's task that sets `device_role: fixed` as a fact of the host `touched`
    "update-fact": ("    - ansible.utils.update_fact:\n        updates:\n"
                    "          - path: device_role\n            value: fixed\n"),
    "library-module": "    - set_role: {}\n",
    "cacheable-set-fact": "    - ansible.builtin.set_fact:\n        device_role: fixed\n        cacheable: true\n",
}
_REFUSED = ("touched: `device_role` is a fact for this host (a module that returns facts set it, in an earlier play "
            "of this run), and only the inventory may name the role a host dispatches by. Refused for this host; the "
            "other hosts go on.")


def _run(tmp_path, setter, guard=True):
    """{host: the role it would dispatch by} for each host that reached the probe play's last task, and the run's
    output. An earlier play runs `setter`'s task (none: no earlier play) on `touched`; the probe play then imports the
    guard (unless `guard` is false) on `touched` and `plain`, both `local` with `device_role: own` in the inventory,
    and prints each host's `device_role`."""
    play = tmp_path / "playbooks"
    (play / "tasks").mkdir(parents=True)
    (play / "library").mkdir()
    shutil.copy(GUARD, str(play / "tasks" / "refuse-device-role-fact.yml"))
    (play / "library" / "set_role.py").write_text(_MODULE)
    before = ("- name: Earlier\n  hosts: touched\n  gather_facts: false\n  tasks:\n" + _SETTERS[setter]
              if setter else "")
    imp = ("    - name: Refuse a device_role fact\n"
           "      ansible.builtin.import_tasks: tasks/refuse-device-role-fact.yml\n" if guard else "")
    (play / "probe.yml").write_text(
        before + "- name: Probe\n  hosts: g\n  gather_facts: false\n  tasks:\n" + imp +
        "    - name: Dispatch\n      ansible.builtin.debug:\n"
        "        msg: \"PASSED {{ inventory_hostname }} {{ device_role }}\"\n")
    (tmp_path / "inv.yml").write_text(
        "all:\n  vars:\n    ansible_python_interpreter: \"{{ ansible_playbook_python }}\"\n"
        "  children:\n    g:\n      hosts:\n"
        "        touched: {ansible_connection: local, device_role: own}\n"
        "        plain: {ansible_connection: local, device_role: own}\n")
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")   # not the repo's: its collections_path hides the image's
    env = dict(os.environ, ANSIBLE_CONFIG=str(tmp_path / "ansible.cfg"), ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCAL_TEMP=str(tmp_path / "tmp"), ANSIBLE_LOCALHOST_WARNING="0")
    p = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(play / "probe.yml")],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    assert "PLAY RECAP" in out, out
    return dict(re.findall(r'"msg": "PASSED (\S+) (\S+)"', out)), out


def test_a_host_whose_device_role_only_the_inventory_names_gets_past_the_guard(tmp_path):
    """With no earlier play, both hosts pass the guard and dispatch by the role their inventory names. Guards a guard
    that refuses every host, which would turn every fleet backup and liveness check into a refusal."""
    passed, out = _run(tmp_path, None)
    assert passed == {"touched": "own", "plain": "own"}, out


@pytest.mark.parametrize("setter", sorted(_SETTERS))
def test_a_device_role_a_module_set_as_a_fact_is_refused(tmp_path, setter):
    """Without the guard, the earlier play's setter makes `touched` dispatch by `fixed`, the fact outranking the
    inventory's `own`. With it, `touched` is refused with the guard's message before the dispatching task, and `plain`,
    which no setter touched, dispatches by `own`. Guards the class of setter the scan cannot name ahead of time: an
    update_fact ran a fixed role on a `local` host from backup-configs.yml with the scan green (local-refusal fold 12's
    read A, S3), and a collection's or a `library/` module, or a cacheable set_fact, would do the same."""
    passed, out = _run(tmp_path / "bare", setter, guard=False)
    assert passed == {"touched": "fixed", "plain": "own"}, out   # the setter sets it: the row is not vacuous
    passed, out = _run(tmp_path / "guarded", setter)
    assert passed == {"plain": "own"}, out
    assert re.search(r'fatal: \[touched\]: FAILED! => .*"msg": "%s"' % re.escape(_REFUSED), out), out
