"""Ansible's own display of a failed task's message, as the tree's ansible.cfg sets it — for the rows that hold a
message's printed commands to what the operator sees and pastes (read A-f8, S2: a one-line message is shown as a
YAML quoted scalar, each `'` doubled and the line wrapped mid-command).

`PLAYBOOK` is `ansible-playbook` on the PATH, or None (a row skips then). `shown(tmp_path, messages)` runs it on a
play of one failed task per message (`ansible.builtin.fail`, `ignore_errors`), the messages handed in as extra
variables, on localhost; the display is the `default` callback with `callback_result_format = yaml`, which it first
checks the tree's ansible.cfg sets, read with an empty config otherwise (the tree's inventory and vars plugins are
not the row's). Returns each task's result block, one per message, from its `fatal: [localhost]: FAILED! =>` line to
its `...ignoring`: ansible-core 2.19 also prints each message raw, in an `[ERROR]: Task failed:` line before the
block, which holds every command as written whatever the YAML display does, so a row that read the whole output held
nothing on 2.19 (fold 9's own mutant G9-lc-owner-blank-off: the not-operator refusal's line break dropped passed
there). 2.18, which the installer pins, prints the block alone.
"""
import configparser
import json
import os
import shutil
import subprocess

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYBOOK = shutil.which("ansible-playbook")


def shown(tmp_path, messages):
    """What `ansible-playbook` prints for a failed task whose `msg` is each of `messages` (see the module)."""
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(os.path.join(ROOT, "ansible", "ansible.cfg"), encoding="utf-8")
    assert cfg["defaults"]["stdout_callback"] == "default" and cfg["defaults"]["callback_result_format"] == "yaml"
    names = ["m%d" % i for i in range(len(messages))]
    (tmp_path / "vars.json").write_text(json.dumps(dict(zip(names, messages))), encoding="utf-8")
    play = [{"hosts": "localhost", "gather_facts": False, "tasks": [
        {"name": n, "ansible.builtin.fail": {"msg": "{{ %s }}" % n}, "ignore_errors": True} for n in names]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play), encoding="utf-8")
    (tmp_path / "empty.cfg").write_text("", encoding="utf-8")
    env = dict(os.environ, ANSIBLE_CONFIG=str(tmp_path / "empty.cfg"), ANSIBLE_STDOUT_CALLBACK="default",
               ANSIBLE_CALLBACK_RESULT_FORMAT="yaml", ANSIBLE_NOCOLOR="1", ANSIBLE_HOME=str(tmp_path / "home"),
               ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false")
    p = subprocess.run([PLAYBOOK, "-i", "localhost,", "-c", "local", "-e", "@" + str(tmp_path / "vars.json"),
                        str(tmp_path / "play.yml")], capture_output=True, cwd=str(tmp_path), env=env, timeout=300)
    assert p.returncode == 0, (p.stdout, p.stderr)
    blocks, block = [], None
    for line in p.stdout.decode("utf-8").splitlines():
        if line.startswith("fatal: [localhost]: FAILED! =>"):
            block = [line]
            blocks.append(block)
        elif line == "...ignoring":
            block = None
        elif block is not None:
            block.append(line)
    assert len(blocks) == len(messages), p.stdout
    return "\n".join("\n".join(b) for b in blocks)
