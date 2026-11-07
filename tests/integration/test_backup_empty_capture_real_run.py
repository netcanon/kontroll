"""backup-configs.yml run for real: a device that answers nothing keeps its last good capture, and the run fails (I-5).

WHY. tests/unit/test_backup_empty_capture_refused.py evaluates each backup's conditions; it cannot say what a run
does with them: that the refusal runs before the write, that its failure reaches the summary and fails the run there,
that the other hosts' captures are still written and committed to the capture history, and that a dry-run writes
nothing and refuses nothing it did not read. T27's runtime read found the fault this way: a raw-SSH `cmd=true`
against a seeded good capture left a 0-byte capture, and the run passed. These rows run the real play against three
hosts of the generic raw-SSH backend on a local connection, the one backend whose read a local connection makes for
real, with local commands for backup commands: one prints a config, one prints nothing, one prints only blank lines.
Each host has a good capture in place and in the history before the run.

It needs ansible-playbook and git: where either is missing every row is skipped (the gate's images and CI's validate
job have both). Nothing of the developer's Ansible or git reaches the play: its configuration, home and temporary
directory, the inventory and the captures are the row's own.
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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "backup-configs.yml")
HOSTS = {  # host: (address, its backup command)
    "ap-good": ("192.0.2.31", "printf 'config system\\n'"),
    "ap-empty": ("192.0.2.32", "true"),
    "ap-blank": ("192.0.2.33", "printf '\\n \\n'"),
}
GOOD = "config wifi-iface 'default_radio0'\n"   # each host's capture before the run


def _capture(host):
    return "backend_raw_ssh_%s.cfg" % HOSTS[host][0].replace(".", "-")


def _git(backups, *argv):
    return subprocess.run(["git", "-C", backups, *argv], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def lab(tmp_path):
    """The inventory, an Ansible home and configuration of the row's own, and a captures directory holding each
    host's good capture, committed as the history play commits."""
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
        "hosts": {host: {"ansible_host": address, "device_role": "backend_raw_ssh",
                         "backend_backup_command": command} for host, (address, command) in HOSTS.items()}}}),
        encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    for host in HOSTS:
        (backups / _capture(host)).write_text(GOOD, encoding="utf-8")
    _git(str(backups), "init", "-q")
    _git(str(backups), "add", "-A")
    _git(str(backups), "-c", "user.email=kontroll-backup@localhost", "-c", "user.name=kontroll backup",
         "commit", "-q", "-m", "backup before the run")
    # The fleet plays' key directory is the row's own: the last play deletes it, and its default is a fixed path
    # under /tmp. No bytecode is written next to the tree's filter plugins.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(tmp_path / "keys"), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "backups": str(backups),
                            "env": env})()


def _play(lab, check=False):
    """Run backup-configs.yml against the lab's hosts; return its exit code, its output and each host's recap."""
    argv = ["ansible-playbook", "-i", lab.inventory, PLAYBOOK, "-e", json.dumps({"config_backup_dir": lab.backups})]
    if check:
        argv += ["--check", "--diff"]
    p = subprocess.run(argv, cwd=lab.base, env=lab.env, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    recap = {m.group(1): int(m.group(2)) for m in
             re.finditer(r"^(\S+)\s*:\s*ok=\d+\s+changed=\d+\s+unreachable=\d+\s+failed=(\d+)", p.stdout, re.M)}
    return p.returncode, out, recap


def _summary(out, host):
    """The host's line of the play's summary, as its `debug` printed it (a JSON string)."""
    found = re.findall(r'"msg": "(%s \(backend_raw_ssh\): .*)"' % re.escape(host), out)
    assert len(found) == 1, (host, out)
    return json.loads('"%s"' % found[0]).split(": ", 1)[1]


def test_a_real_run_refuses_the_empty_answers_keeps_their_captures_and_fails(lab):
    """A real run writes and commits ap-good's new capture, and nothing else: ap-empty's and ap-blank's summary lines
    say FAILED, naming the command that answered nothing, their good captures are kept byte for byte, the run fails
    at its summary for those two hosts only, and the history play still runs. Guards I-5: before it, both empty
    answers were written over the good captures, recorded captured, and committed as a change, and the run passed."""
    rc, out, recap = _play(lab)
    assert rc != 0, out
    assert recap == {"ap-good": 0, "ap-empty": 1, "ap-blank": 1, "localhost": 0}, (recap, out)
    assert _summary(out, "ap-good") == "captured -> %s" % _capture("ap-good")
    assert _summary(out, "ap-empty").startswith("FAILED: `true` answered nothing, so no capture was written")
    assert _summary(out, "ap-blank").startswith("FAILED: `printf '\\n \\n'` answered nothing")
    for host in ("ap-empty", "ap-blank"):
        with open(os.path.join(lab.backups, _capture(host)), encoding="utf-8") as fh:
            assert fh.read() == GOOD, host
    with open(os.path.join(lab.backups, _capture("ap-good")), encoding="utf-8") as fh:
        assert fh.read() == "config system\n"
    assert _git(lab.backups, "rev-list", "--count", "HEAD").strip() == "2"
    changed = _git(lab.backups, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert [n for n in changed if n.endswith(".cfg")] == [_capture("ap-good")], changed   # and the play's .gitignore
    assert _git(lab.backups, "status", "--porcelain", "--untracked-files=no").strip() == ""


def test_a_dry_run_previews_every_capture_writes_nothing_and_passes(lab):
    """`--check --diff` passes: `raw` has no check mode, so no host's command runs, each previews its capture and is
    recorded captured, and no capture or commit changes. Guards the refusal reaching a read the dry-run skipped: the
    mandated dry-run would then fail every raw-SSH host (OpenWrt's class) at its summary, as T27's fresh-node check
    found the write's own template did before it was fixed."""
    rc, out, recap = _play(lab, check=True)
    assert rc == 0 and set(recap.values()) == {0}, (rc, recap, out)
    for host in HOSTS:
        assert _summary(out, host) == "captured -> %s" % _capture(host)
        with open(os.path.join(lab.backups, _capture(host)), encoding="utf-8") as fh:
            assert fh.read() == GOOD, host
    assert _git(lab.backups, "rev-list", "--count", "HEAD").strip() == "1"
