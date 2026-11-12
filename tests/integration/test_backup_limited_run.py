"""backup-configs.yml run for real under a `--limit` that leaves localhost out, as every Semaphore backup schedule runs
it (`--limit <inventory_group>`, scripts/gen-backup.py): the capture dir's check, the history commit and the prune
still run (I-90).

WHY. The three controller plays were `hosts: localhost`, and a `--limit` that does not name localhost leaves the
implicit localhost out: each printed `skipping: no hosts matched`, the capture was written and never committed, no prune
ran, and the run passed. So no scheduled run kept the history the diff views read, or enforced its retention. They now
start from `localhost:all` and run once, every task delegated to the controller
(tests/unit/test_backup_controller_plays.py holds the shape); these rows run them: a run limited to a group (the
schedule's shape), to one host and to `all` commits the limited hosts' captures alone; one with a finite retention
window prunes; one with no capture dir is refused at the check, on the controller, naming the remedy (I-91); and an
unlimited run still runs each task on localhost, as before. Hosts of the generic raw-SSH class on a local connection,
each printing a config, with a good capture in place and in the history, and a stale key file in the run's key directory
before the run.

It needs ansible-playbook and git: where either is missing the row is skipped. Nothing of the developer's Ansible or
git reaches the play: its configuration, home and temporary directory, the inventory, the captures and the key
directory are the row's own.
"""
import json
import os
import re
import shutil
import subprocess
import time

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
GROUP = "wireless_ap"
HOSTS = {"ap-one": ("192.0.2.61", "config one\n"), "ap-two": ("192.0.2.62", "config two\n")}
GOOD = "config before the run\n"
COMMIT = "Commit a timestamped snapshot when configs changed"
REFUSED = "config_backup_dir is not defined, so there is nowhere to write captures"


def _capture(host):
    return "backend_raw_ssh_%s.cfg" % HOSTS[host][0].replace(".", "-")


def _git(backups, *argv, env=None):
    return subprocess.run(["git", "-C", backups, *argv], check=True, capture_output=True, text=True,
                          env=env).stdout


def _lab(tmp_path, aged=False):
    """The inventory (both hosts in GROUP), an Ansible home and configuration of the row's own (its roles path the
    repo's roles), a captures directory holding each host's GOOD capture, committed as the history play commits (40
    days ago when `aged`), and the run's key directory holding a stale key."""
    home = tmp_path / "ansible-home"
    home.mkdir(parents=True)
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = %s\nfilter_plugins = %s\nretry_files_enabled = False\n"
                   "interpreter_python = auto_silent\n"
                   % (os.path.join(ROOT, "ansible", "roles"), os.path.join(ROOT, "ansible", "filter_plugins")),
                   encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    hosts = {host: {"ansible_host": address, "device_role": "backend_raw_ssh",
                    "backend_backup_command": "printf '%s'" % text.replace("\n", "\\n")}
             for host, (address, text) in HOSTS.items()}
    inventory.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "children": {GROUP: {"hosts": hosts}}}}), encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    for host in HOSTS:
        (backups / _capture(host)).write_text(GOOD, encoding="utf-8")
    _git(str(backups), "init", "-q")
    _git(str(backups), "add", "-A")
    env = dict(os.environ)
    if aged:
        stamp = "%d +0000" % (time.time() - 40 * 86400)
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    _git(str(backups), "-c", "user.email=kontroll-backup@localhost", "-c", "user.name=kontroll backup",
         "commit", "-q", "-m", "backup before the run", env=env)
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    (keys / "ap-one.key").write_text("not a key: a stale file the cleanup must remove\n", encoding="utf-8")
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    run_env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
                   ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
                   KONTROLL_SSH_KEY_DIR=str(keys), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "backups": str(backups),
                            "keys": str(keys), "env": run_env})()


def _play(lab, *extra, capture_dir=True):
    """Run backup-configs.yml against the lab (with `extra` arguments; the capture dir passed unless not
    `capture_dir`); return its exit code, its output and each host's recap."""
    argv = ["ansible-playbook", "-i", lab.inventory, PLAYBOOK, *extra]
    if capture_dir:
        argv += ["-e", json.dumps({"config_backup_dir": lab.backups})]
    p = subprocess.run(argv, cwd=lab.base, env=lab.env, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    recap = {m.group(1): int(m.group(2)) for m in
             re.finditer(r"^(\S+)\s*:\s*ok=\d+\s+changed=\d+\s+unreachable=\d+\s+failed=(\d+)", p.stdout, re.M)}
    return p.returncode, out, recap


def _task(out, name):
    """The output lines of the task `name`, up to the next TASK or PLAY header."""
    at = out.index("TASK [%s]" % name)
    ends = [i for i in (out.find("\nTASK [", at + 1), out.find("\nPLAY ", at + 1)) if i != -1]
    return out[at:min(ends) if ends else len(out)]


@pytest.mark.parametrize("limit", [GROUP, "ap-one", "all"])
def test_a_limited_run_commits_the_limited_hosts_captures(tmp_path, limit):
    """A real run under `--limit <limit>` (the group, as a schedule runs it; one host; `all`, which the implicit
    localhost is no member of): it passes, the history gains one commit holding each limited host's new capture and no
    other host's (beside the .gitignore the play renders), the commit ran once, for the first limited host, on the
    controller (`[ap-one -> localhost]`), the recap names no localhost, and the key directory is gone. Guards the three
    controller plays' shape (I-90): as `hosts: localhost` plays they printed `skipping: no hosts matched` under each of
    these limits, so the capture was written and never committed (the history kept its one commit) while the run
    passed."""
    lab = _lab(tmp_path)
    rc, out, recap = _play(lab, "--limit", limit)
    assert rc == 0, out
    limited = ["ap-one"] if limit == "ap-one" else ["ap-one", "ap-two"]
    assert recap == {host: 0 for host in limited}, (recap, out)
    assert "skipping: no hosts matched" not in out, out
    assert "changed: [ap-one -> localhost]" in _task(out, COMMIT), out
    assert _git(lab.backups, "rev-list", "--count", "HEAD").strip() == "2", out
    changed = _git(lab.backups, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert sorted(n for n in changed if n.endswith(".cfg")) == sorted(_capture(host) for host in limited), changed
    for host, (_address, text) in HOSTS.items():
        with open(os.path.join(lab.backups, _capture(host)), encoding="utf-8") as fh:
            assert fh.read() == (text if host in limited else GOOD), host
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


def test_an_unlimited_run_commits_on_localhost_as_before(tmp_path):
    """A real run with no limit: localhost is the controller plays' first host, so the commit runs there
    (`changed: [localhost]`), as it did when the plays were `hosts: localhost`, and the recap names localhost; both
    captures are committed. Guards the plays' host pattern order: `all:localhost` would run them for the first
    device instead, which a fleet run never did, and its recap would lose localhost."""
    lab = _lab(tmp_path)
    rc, out, recap = _play(lab)
    assert rc == 0, out
    assert recap == {"ap-one": 0, "ap-two": 0, "localhost": 0}, (recap, out)
    assert "changed: [localhost]" in _task(out, COMMIT), out
    assert _git(lab.backups, "rev-list", "--count", "HEAD").strip() == "2", out


def test_a_limited_run_with_a_retention_window_prunes_older_history(tmp_path):
    """A real run under `--limit <group>` with `kontroll_backup_retention=30d` (what a schedule with a retention
    passes), over a history whose one commit is 40 days old: the prune drops it (`PRUNE_DONE dropped=1 kept=2`),
    leaving the synthetic baseline and the run's snapshot, and the run passes. Guards the prune under the schedule's
    limit (I-90): only the schedules pass a window, and each passes a limit, so no prune ever ran."""
    lab = _lab(tmp_path, aged=True)
    rc, out, _recap = _play(lab, "--limit", GROUP, "-e", "kontroll_backup_retention=30d")
    assert rc == 0, out
    assert "PRUNE_DONE dropped=1 kept=2 window=30d" in out, out
    subjects = _git(lab.backups, "log", "--format=%s").splitlines()
    assert len(subjects) == 2 and subjects[1].startswith("backup history baseline"), subjects
    assert "backup before the run" not in subjects, subjects


def test_a_limited_run_with_no_capture_dir_is_refused_on_the_controller(tmp_path):
    """A real run under `--limit <group>` with no `config_backup_dir`: the capture dir's check refuses it on the
    controller (`[ap-one -> localhost]`), naming the remedy, and the run fails. Guards the check under a schedule's
    limit (I-90): skipped there, a box with no capture dir reached the captures with nothing to say where they go; and
    its message (I-91): the assert templated its success message, which names the undefined var, before it tested, so
    the run failed on an undefined-variable error, the remedy unprinted."""
    lab = _lab(tmp_path)
    rc, out, _recap = _play(lab, "--limit", GROUP, capture_dir=False)
    assert rc != 0, out
    check = _task(out, "Refuse without a capture directory, naming the remedy")
    assert "fatal: [ap-one -> localhost]" in check and REFUSED in check, out
