"""backup-configs.yml run for real, every capture recorded as failed: the run still removes its SSH keys, and still
fails (I-9).

WHY. The summary play's assert fails each host whose capture was recorded as failed, and when that is every host
Ansible ends the playbook at that play. The history, the prune and the per-run SSH-key cleanup came after it, so a
run whose every capture was recorded as failed left the run's keys in their per-run directory. Since I-5 one device
that answers nothing is enough: a one-host scheduled backup. (A capture that fails as a task on every host, a read
that exits non-zero, still ends the run at the capture play, before the cleanup: I-12.)
tests/unit/test_backup_summary_is_last.py holds the order; this row runs it. One host of the generic raw-SSH backend,
on a local connection, whose backup command (`true`) answers nothing; its good capture is in place and in the
history, and a stale key file is in the run's key directory before the run.

It needs ansible-playbook and git: where either is missing the row is skipped (the gate's images and CI's validate job
have both). Nothing of the developer's Ansible or git reaches the play: its configuration, home and temporary
directory, the inventory, the captures and the key directory are the row's own.
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
HOST, ADDRESS = "ap-empty", "192.0.2.51"
CAPTURE = "backend_raw_ssh_192-0-2-51.cfg"
GOOD = "config wifi-iface 'default_radio0'\n"
PLAYS = ("Snapshot the captures into local history (git)", "Prune capture history older than the retention window (git)",
         "Remove the per-run ephemeral SSH key dir", "Backup run summary")


def _git(backups, *argv):
    return subprocess.run(["git", "-C", backups, *argv], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def lab(tmp_path, isolated_git):
    """The one-host inventory, an Ansible home and configuration of the row's own, a captures directory holding the
    host's good capture, committed as the history play commits, and the run's key directory holding a stale key."""
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
        "hosts": {HOST: {"ansible_host": ADDRESS, "device_role": "backend_raw_ssh",
                         "backend_backup_command": "true"}}}}), encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    (backups / CAPTURE).write_text(GOOD, encoding="utf-8")
    _git(str(backups), "init", "-q")
    _git(str(backups), "add", "-A")
    _git(str(backups), "-c", "user.email=kontroll-backup@localhost", "-c", "user.name=kontroll backup",
         "commit", "-q", "-m", "backup before the run")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    (keys / ("%s.key" % HOST)).write_text("not a key: a stale file the cleanup must remove\n", encoding="utf-8")
    os.chmod(keys / ("%s.key" % HOST), 0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(keys), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "backups": str(backups),
                            "keys": str(keys), "env": env})()


def _play(lab):
    """Run backup-configs.yml against the lab's host; return its exit code, its output and each host's recap."""
    argv = ["ansible-playbook", "-i", lab.inventory, PLAYBOOK, "-e", json.dumps({"config_backup_dir": lab.backups})]
    p = subprocess.run(argv, cwd=lab.base, env=lab.env, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    recap = {m.group(1): int(m.group(2)) for m in
             re.finditer(r"^(\S+)\s*:\s*ok=\d+\s+changed=\d+\s+unreachable=\d+\s+failed=(\d+)", p.stdout, re.M)}
    return p.returncode, out, recap


def test_a_run_whose_every_capture_failed_still_removes_its_keys_and_fails(lab):
    """A real run whose only host answers nothing fails for that host at its summary, whose line says FAILED, with
    the run id stamped; the history, prune and key-cleanup plays run before the summary (the history play commits
    its `.gitignore`), and the run's key directory, stale key and all, is gone after it; the good capture is kept
    byte for byte, and no commit touches it. Guards I-9: the summary's assert failed every host, so the playbook
    ended there, and the history and the key cleanup after it never ran."""
    rc, out, recap = _play(lab)
    assert rc != 0, out
    assert recap == {HOST: 1, "localhost": 0}, (recap, out)
    assert re.search(r'"msg": "kontroll_run_id=[0-9]{8}T[0-9]{6}Z"', out), out
    assert re.search(r'"msg": "%s \(backend_raw_ssh\): FAILED: `true` answered nothing' % HOST, out), out
    at = [out.find("PLAY [%s]" % play) for play in PLAYS]
    assert -1 not in at and at == sorted(at), (at, out)
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)
    with open(os.path.join(lab.backups, CAPTURE), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    assert len(_git(lab.backups, "log", "--format=%H", "--", CAPTURE).split()) == 1
    assert _git(lab.backups, "ls-files", ".gitignore").strip() == ".gitignore"
    assert _git(lab.backups, "status", "--porcelain", "--untracked-files=no").strip() == ""
