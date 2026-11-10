"""backup-configs.yml's dry-run of a CLI class whose backup command is not a `show` passes, and reads nothing (I-11).

WHY. The CLI backend (backend_netcommon_cli) reads with `ansible.netcommon.cli_command`, which in check mode runs only
a command that starts with `show` and fails the host on any other ("Only show commands are supported when using
check_mode"). A drop-in class whose backup is `display current-configuration` therefore failed the mandated
`--check --diff` of every fleet that holds one, at the read. The read is now not made in a dry-run for such a
command: the run notes it, previews an empty capture and refuses nothing; a real run reads. The condition rows are
tests/unit/test_backup_empty_capture_refused.py's; only a run says that the play as a whole passes, that nothing is
written, and that a real run still makes the read.

A local connection stands in for the device: in the dry-run the read is skipped before any connection, so the run
needs none; in the real run the read is made and fails on it (cli_command needs network_cli), which shows it was made.
It needs ansible-playbook, git and ansible.netcommon: where one is missing every row is skipped (the gate's images and
CI's validate job have all three; netcommon comes in as cisco.ios's dependency). Nothing of the developer's Ansible or
git reaches the play, but for where the collections are: its configuration, home and temporary directory, the
inventory and the captures are the row's own. The row's own home would move the default collection path under it,
so the play is given the path the suite's own Ansible reads (the outer ANSIBLE_COLLECTIONS_PATH, else the account's
~/.ansible/collections and the system directory); the first gate run without it failed both run rows on an
unresolvable `ansible.netcommon.cli_command`.
"""
import json
import os
import re
import shutil
import subprocess

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not all(shutil.which(tool) for tool in ("ansible-playbook", "git")),
                       reason="needs ansible-playbook and git (it runs backup-configs.yml for real)"),
]

# where the suite's own Ansible finds its collections; the row's own ANSIBLE_HOME would move the default under it
COLLECTIONS = os.environ.get("ANSIBLE_COLLECTIONS_PATH") or os.pathsep.join(
    [os.path.expanduser("~/.ansible/collections"), "/usr/share/ansible/collections"])
_NETCOMMON = any(os.path.isfile(os.path.join(d, "ansible_collections", "ansible", "netcommon", "plugins", "modules",
                                             "cli_command.py")) for d in COLLECTIONS.split(os.pathsep) if d)
pytestmark.append(pytest.mark.skipif(not _NETCOMMON, reason="needs ansible.netcommon in %s (cli_command's own "
                                                            "check-mode rule is the one I-11 is about)" % COLLECTIONS))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")
HOST, ADDRESS, COMMAND = "sw-display", "192.0.2.41", "display current-configuration"
CAPTURE = "backend_netcommon_cli_192-0-2-41.cfg"
GOOD = "sysname sw-display\n"                    # the capture before the run


@pytest.fixture
def lab(tmp_path, isolated_git):
    """The inventory (one CLI host on a local connection, its backup command `display current-configuration`), an
    Ansible home and configuration of the row's own (the collections the suite's own Ansible reads), and a captures
    directory holding the host's good capture, committed as the history play commits."""
    home = tmp_path / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = %s\nfilter_plugins = %s\nretry_files_enabled = False\n"
                   "interpreter_python = auto_silent\n"
                   % (os.path.join(ROOT, "ansible", "roles"), os.path.join(ROOT, "ansible", "filter_plugins")),
                   encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "hosts": {HOST: {"ansible_host": ADDRESS, "device_role": "backend_netcommon_cli",
                         "backend_backup_command": COMMAND}}}}), encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    (backups / CAPTURE).write_text(GOOD, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.email=kontroll-backup@localhost", "-c",
                                                 "user.name=kontroll backup", "commit", "-q", "-m", "before"]):
        subprocess.run(["git", "-C", str(backups), *argv], check=True, capture_output=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_COLLECTIONS_PATH=COLLECTIONS,
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(tmp_path / "keys"), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "backups": str(backups),
                            "env": env})()


def _play(lab, check):
    argv = ["ansible-playbook", "-i", lab.inventory, PLAYBOOK, "-e", json.dumps({"config_backup_dir": lab.backups})]
    if check:
        argv += ["--check", "--diff"]
    p = subprocess.run(argv, cwd=lab.base, env=lab.env, capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout + p.stderr


def _summary(out):
    """The host's lines of the play's summary, as its `debug` printed them (JSON strings)."""
    return [json.loads('"%s"' % m).split(": ", 1)[1]
            for m in re.findall(r'"msg": "(%s \(backend_netcommon_cli\): .*)"' % re.escape(HOST), out)]


def test_the_dry_run_passes_notes_the_read_and_writes_nothing(lab):
    """`--check --diff` passes; it says `NOT PREVIEWED` with the command; the host's summary line says captured (the
    write's preview ran); the good capture and the history are as they were. Guards I-11 at the play: before it, the
    read failed the host in every dry-run, and a fix that skipped the read but kept its refusal would fail the run at
    the summary ("answered nothing")."""
    head = subprocess.run(["git", "-C", lab.backups, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout
    rc, out = _play(lab, check=True)
    assert rc == 0, out
    assert "NOT PREVIEWED — `%s` is not a `show` command" % COMMAND in out, out
    assert _summary(out) == ["captured -> %s" % CAPTURE], out
    with open(os.path.join(lab.backups, CAPTURE), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    assert subprocess.run(["git", "-C", lab.backups, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout == head


def test_a_real_run_still_makes_the_read(lab):
    """A real run makes the read (here it fails on the local connection, as a CLI read needs network_cli), so the
    host fails at the read and is neither refused nor recorded captured; the good capture is kept. Guards the skip
    reaching a real run, where a backup would never read the device again (the refusal would then fail the run
    each night, naming an answer the device was never asked for)."""
    rc, out = _play(lab, check=False)
    assert rc != 0, out
    assert re.search(r"TASK \[backend_netcommon_cli : Pull config via cli_command\][^\n]*\n(?:[^\n]*\n)*?"
                     r"fatal: \[%s\]" % re.escape(HOST), out), out
    assert "NOT PREVIEWED" not in out, out
    assert not any(line.startswith(("captured", "FAILED")) for line in _summary(out)), out
    with open(os.path.join(lab.backups, CAPTURE), encoding="utf-8") as fh:
        assert fh.read() == GOOD
