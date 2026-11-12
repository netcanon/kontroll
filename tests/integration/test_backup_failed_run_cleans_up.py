"""backup-configs.yml run for real, every capture failed: the run still removes its SSH keys, and still fails (I-9,
I-12).

WHY. The summary play's assert fails each host whose capture was recorded as failed, and when that is every host
Ansible ends the playbook at that play. The history, the prune and the per-run SSH-key cleanup came after it, so a
run whose every capture was recorded as failed left the run's keys in their per-run directory. Since I-5 one device
that answers nothing is enough: a one-host scheduled backup. A capture that fails as a task (a read that exits
non-zero) failed its host at the capture play instead: no summary line, and when it was every host, the run ended
there, before the cleanup (I-12); the capture play's rescue now records it as a failed capture. A class whose backup
entrypoint is not installed could not be loaded, which is no task failure: Ansible dropped its host from every later
play or ended the whole run, so where that was every host the run's keys stayed (I-66); the capture block's first
task now refuses it as a task, which the rescue records.
tests/unit/test_backup_summary_is_last.py holds the order and tests/unit/test_backup_capture_rescue.py the rescue;
these rows run them. Hosts on a local connection: of the generic raw-SSH backend, one whose backup command (`true`)
answers nothing, one whose command exits 3, and one that prints a config; and one whose `device_role` names no
installed role, one whose role has no tasks/backup.yml, one with no `device_role`, and one whose role's backup
includes a file that is missing. Each has its good capture in place and in the history, and a stale key file in the
run's key directory before the run.

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
HOSTS = {  # host: (address, its backup command)
    "ap-empty": ("192.0.2.51", "true"),               # answers nothing: recorded as failed by the role (I-5, I-9)
    "ap-erring": ("192.0.2.52", "echo partial; exit 3"),  # fails as a task: recorded by the capture play (I-12)
    "ap-good": ("192.0.2.53", "printf 'config system\\n'"),
    "ap-missing": ("192.0.2.54", "true"),             # its class is not installed: refused as a task (I-66)
    "ap-noentry": ("192.0.2.55", "true"),             # its class's role has no tasks/backup.yml: refused (I-66)
    "ap-norole": ("192.0.2.56", "true"),              # it names no device_role: refused (I-66)
    "ap-broken": ("192.0.2.57", "true"),              # its class's backup includes a missing file: failed in the
}                                                     # rescue, as it cannot be loaded (I-12)
ROLE = {"ap-missing": "zz_no_such_class", "ap-noentry": "zz_no_backup", "ap-norole": None,
        "ap-broken": "zz_broken_include"}           # every other host: backend_raw_ssh; None: no device_role
EXTRA_ROLES = {  # the lab's second roles_path entry: a role with no backup entry, one whose entry cannot be loaded
    "zz_no_backup/tasks/main.yml": "- ansible.builtin.debug:\n    msg: not a backup\n",
    "zz_broken_include/tasks/backup.yml": "- ansible.builtin.include_tasks: no_such_file.yml\n",
}
HOST = "ap-empty"
GOOD = "config wifi-iface 'default_radio0'\n"
PLAYS = ("Snapshot the captures into local history (git)", "Prune capture history older than the retention window (git)",
         "Remove the per-run ephemeral SSH key dir", "Backup run summary")


def _capture(host):
    return "backend_raw_ssh_%s.cfg" % HOSTS[host][0].replace(".", "-")


CAPTURE = _capture(HOST)


def _git(backups, *argv):
    return subprocess.run(["git", "-C", backups, *argv], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def lab(tmp_path, isolated_git):
    """ap-empty alone (see _lab)."""
    return _lab(tmp_path, [HOST])


@pytest.fixture
def erring_lab(tmp_path, isolated_git):
    """ap-erring alone (see _lab)."""
    return _lab(tmp_path, ["ap-erring"])


@pytest.fixture
def erring_good_lab(tmp_path, isolated_git):
    """ap-erring and ap-good (see _lab)."""
    return _lab(tmp_path, ["ap-erring", "ap-good"])


def _lab(tmp_path, hosts):
    """The inventory of `hosts`, an Ansible home and configuration of the row's own (its roles path the repo's roles,
    then EXTRA_ROLES), a captures directory holding each host's good capture, committed as the history play commits,
    and the run's key directory holding a stale key for each."""
    home = tmp_path / "ansible-home"
    home.mkdir(parents=True)
    extra = tmp_path / "extra-roles"
    for rel, content in EXTRA_ROLES.items():
        (extra / rel).parent.mkdir(parents=True, exist_ok=True)
        (extra / rel).write_text(content, encoding="utf-8")
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = %s:%s\nfilter_plugins = %s\nretry_files_enabled = False\n"
                   "interpreter_python = auto_silent\n"
                   % (os.path.join(ROOT, "ansible", "roles"), extra, os.path.join(ROOT, "ansible", "filter_plugins")),
                   encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    entries = {host: {"ansible_host": HOSTS[host][0], "device_role": ROLE.get(host, "backend_raw_ssh"),
                      "backend_backup_command": HOSTS[host][1]} for host in hosts}
    for entry in entries.values():
        if entry["device_role"] is None:
            del entry["device_role"]
    inventory.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "hosts": entries}}), encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    for host in hosts:
        (backups / _capture(host)).write_text(GOOD, encoding="utf-8")
    _git(str(backups), "init", "-q")
    _git(str(backups), "add", "-A")
    _git(str(backups), "-c", "user.email=kontroll-backup@localhost", "-c", "user.name=kontroll backup",
         "commit", "-q", "-m", "backup before the run")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    for host in hosts:
        (keys / ("%s.key" % host)).write_text("not a key: a stale file the cleanup must remove\n", encoding="utf-8")
        os.chmod(keys / ("%s.key" % host), 0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(keys), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "backups": str(backups),
                            "keys": str(keys), "env": env})()


def _play(lab, *extra):
    """Run backup-configs.yml against the lab's hosts (with `extra` arguments); return its exit code, its output and
    each host's recap."""
    argv = ["ansible-playbook", "-i", lab.inventory, PLAYBOOK, "-e", json.dumps({"config_backup_dir": lab.backups}),
            *extra]
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


STOPPED = (r'"msg": "%s \(backend_raw_ssh\): FAILED: the capture stopped at the task '
           r'\\"Pull config via raw command\\" \(a no_log task prints only that it failed\)\."')
STAMP = r'"msg": "kontroll_run_id=[0-9]{8}T[0-9]{6}Z"'
REFUSED = ('"msg": "%s (%s): FAILED: the capture stopped at the task \\"Refuse a host whose class has no entrypoint '
           'for this play installed\\" (a no_log task prints only that it failed)."')
BROKEN = ('"msg": "ap-broken: the backup entrypoint of the class zz_broken_include could not be loaded (a file or role '
          'it includes is missing, or the collection it names is not installed), so the run fails here (I-12)."')


def test_a_run_whose_every_capture_fails_as_a_task_still_removes_its_keys_and_names_it(erring_lab):
    """A real run whose only host's backup command exits 3 (after printing a line) fails for that host at its
    summary, whose line says FAILED and names the task the capture stopped at, with the run id stamped; the history,
    prune and key-cleanup plays run before the summary, and the run's key directory, stale key and all, is gone after
    it; the good capture is kept byte for byte (the partial answer is not written), and no commit touches it. Guards
    I-12: the read failed its host at the capture play, so the summary printed no line for it, and with every host
    failed so the playbook ended there, before the history and the key cleanup (the I-9 reads' S1)."""
    rc, out, recap = _play(erring_lab)
    assert rc != 0, out
    assert recap == {"ap-erring": 1, "localhost": 0}, (recap, out)
    assert re.search(r'"msg": "kontroll_run_id=[0-9]{8}T[0-9]{6}Z"', out), out
    assert re.search(STOPPED % "ap-erring", out), out
    at = [out.find("PLAY [%s]" % play) for play in PLAYS]
    assert -1 not in at and at == sorted(at), (at, out)
    assert not os.path.exists(erring_lab.keys), os.listdir(erring_lab.keys)
    with open(os.path.join(erring_lab.backups, _capture("ap-erring")), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    assert len(_git(erring_lab.backups, "log", "--format=%H", "--", _capture("ap-erring")).split()) == 1
    assert _git(erring_lab.backups, "status", "--porcelain", "--untracked-files=no").strip() == ""


def test_a_capture_that_fails_as_a_task_beside_a_good_one_is_named_and_the_good_one_committed(erring_good_lab):
    """A real run against ap-erring (its command exits 3) and ap-good (it prints a config): ap-good is captured, and
    the history's new commit holds its capture and nothing of ap-erring's; ap-erring's summary line says FAILED,
    naming the task, with the run id stamped, its good capture is kept byte for byte, and the run fails at its summary
    for it alone; the key directory is gone. Guards I-12 where one host of several fails as a task: it had no summary
    line, so the run's output named no host for the failure the recap counted (the I-9 read B's M3)."""
    lab = erring_good_lab
    rc, out, recap = _play(lab)
    assert rc != 0, out
    assert recap == {"ap-erring": 1, "ap-good": 0, "localhost": 0}, (recap, out)
    assert re.search(STAMP, out), out
    assert re.search(STOPPED % "ap-erring", out), out
    good = r'"msg": "ap-good \(backend_raw_ssh\): captured -> %s"' % re.escape(_capture("ap-good"))
    assert re.search(good, out), out
    with open(os.path.join(lab.backups, _capture("ap-erring")), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    with open(os.path.join(lab.backups, _capture("ap-good")), encoding="utf-8") as fh:
        assert fh.read() == "config system\n"
    assert _git(lab.backups, "rev-list", "--count", "HEAD").strip() == "2"
    changed = _git(lab.backups, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert _capture("ap-good") in changed and _capture("ap-erring") not in changed, changed
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


def test_a_callers_any_errors_fatal_leaves_the_good_host_captured_beside_one_that_fails_as_a_task(erring_good_lab):
    """The run of the row above under `ANSIBLE_ANY_ERRORS_FATAL=True` ends the same: ap-good is captured and committed
    and its summary line printed, ap-erring is named FAILED by the task, the run fails for it, and the key directory is
    gone. Guards the capture play's `any_errors_fatal: false` (the I-12 reads' O1): a caller's setting failed every
    other host once ap-erring's read failed, rescue or not, so ap-good, whose read had answered, was moved into the
    rescue and dropped from every later play: its capture was never written, it had no summary line, and its recap
    read failed=0."""
    lab = erring_good_lab
    lab.env["ANSIBLE_ANY_ERRORS_FATAL"] = "True"
    rc, out, recap = _play(lab)
    assert rc != 0, out
    assert recap == {"ap-erring": 1, "ap-good": 0, "localhost": 0}, (recap, out)
    assert re.search(STOPPED % "ap-erring", out), out
    good = r'"msg": "ap-good \(backend_raw_ssh\): captured -> %s"' % re.escape(_capture("ap-good"))
    assert re.search(good, out), out
    with open(os.path.join(lab.backups, _capture("ap-good")), encoding="utf-8") as fh:
        assert fh.read() == "config system\n"
    changed = _git(lab.backups, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert _capture("ap-good") in changed and _capture("ap-erring") not in changed, changed
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


@pytest.mark.parametrize("host", ["ap-missing", "ap-noentry", "ap-norole"])
def test_a_class_whose_backup_entrypoint_is_not_installed_is_named_failed_and_its_keys_removed(tmp_path, host):
    """A dry run, then a real run, of one host whose class's backup entrypoint is not installed (its `device_role`
    names no installed role; names a role with no tasks/backup.yml; or is missing) each fail with rc 2 at the summary,
    whose line says FAILED and names the entrypoint check as the task the capture stopped at; the real run runs the
    history, prune and key-cleanup plays before the summary, and the run's key directory, stale key and all, is gone
    after it; the good capture is kept byte for byte, and no commit touches it. Guards I-66: such an include could not
    be loaded, which is no task failure; Ansible dropped the host from every later play (no role), or ended the whole
    run (a role with no entry, rc 4; no `device_role` on 2.18, rc 1), so the run ended before the key cleanup and left
    the run's keys on disk, with no summary line."""
    lab = _lab(tmp_path, [host])
    for extra in (("--check", "--diff"), ()):
        rc, out, recap = _play(lab, *extra)
        assert rc == 2, (extra, rc, out)
        assert recap == {host: 1, "localhost": 0}, (extra, recap, out)
        assert REFUSED % (host, ROLE[host] or "?") in out, (extra, out)
    assert re.search(STAMP, out), out
    at = [out.find("PLAY [%s]" % play) for play in PLAYS]
    assert -1 not in at and at == sorted(at), (at, out)
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)
    with open(os.path.join(lab.backups, _capture(host)), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    assert len(_git(lab.backups, "log", "--format=%H", "--", _capture(host)).split()) == 1
    assert _git(lab.backups, "status", "--porcelain", "--untracked-files=no").strip() == ""


@pytest.mark.parametrize("host", ["ap-missing", "ap-noentry"])
def test_a_class_not_installed_beside_a_good_host_leaves_the_good_one_captured(tmp_path, host):
    """A real run against a host whose class is not installed (no such role; a role with no tasks/backup.yml) and
    ap-good fails with rc 2: ap-good is captured and committed (the other's file is not in the run's commit), both get
    a summary line (the other's FAILED, naming the entrypoint check), and the key directory is gone. Guards I-66 where
    the class is one host's among several: a role with no entry ended the whole run (rc 4), ap-good's capture, the
    history and the key cleanup with it, and a missing role left its host with no summary line."""
    lab = _lab(tmp_path, [host, "ap-good"])
    rc, out, recap = _play(lab)
    assert rc == 2, (rc, out)
    assert recap == {host: 1, "ap-good": 0, "localhost": 0}, (recap, out)
    assert REFUSED % (host, ROLE[host]) in out, out
    good = r'"msg": "ap-good \(backend_raw_ssh\): captured -> %s"' % re.escape(_capture("ap-good"))
    assert re.search(good, out), out
    with open(os.path.join(lab.backups, _capture("ap-good")), encoding="utf-8") as fh:
        assert fh.read() == "config system\n"
    changed = _git(lab.backups, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert _capture("ap-good") in changed and _capture(host) not in changed, changed
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)


def test_a_class_whose_entrypoint_cannot_be_loaded_fails_the_run_in_the_rescue(tmp_path):
    """A dry run and a real run whose only host's class is installed but whose backup entry includes a file that is
    missing each fail with rc 2, the rescue's second task naming the host and its class, and neither runs the summary
    play; the run's keys stay (the I-66 residue: the cleanup is a later play). Guards the I-12 reads' S1 for the load
    failure the entrypoint check cannot see: an include that cannot be loaded is no task failure; Ansible moved the host
    into the rescue with no failed task and out of every later play, so with the record alone the run's iterator no
    longer counted it failed, and Ansible ended the run at the capture play with rc 0, a capture that failed reported
    as passed to a scheduler that reads the exit code. The host's recap counts two failures, the load and the rescue's
    `fail`."""
    lab = _lab(tmp_path, ["ap-broken"])
    for extra in (("--check", "--diff"), ()):
        rc, out, recap = _play(lab, *extra)
        assert rc == 2, (extra, rc, out)
        assert recap == {"ap-broken": 2, "localhost": 0}, (extra, recap, out)
        assert BROKEN in out, (extra, out)
        assert "PLAY [Backup run summary]" not in out, (extra, out)
    assert os.path.exists(lab.keys), out


def test_a_class_that_cannot_be_loaded_beside_a_good_host_leaves_the_good_one_captured(tmp_path):
    """A real run against ap-broken (its class's backup includes a missing file) and ap-good fails with rc 2: ap-good
    is captured and committed (ap-broken's file is not in the run's commit) and its summary line printed, the key
    directory is gone, and ap-broken gets the rescue's failure (its recap counts the load and that `fail`) and no
    summary line. Guards the load failure's `fail` against stopping the other hosts: it fails its own host only, and
    the good host's capture, the history, the prune and the key cleanup still run."""
    lab = _lab(tmp_path, ["ap-broken", "ap-good"])
    rc, out, recap = _play(lab)
    assert rc == 2, (rc, out)
    assert recap == {"ap-broken": 2, "ap-good": 0, "localhost": 0}, (recap, out)
    assert BROKEN in out, out
    good = r'"msg": "ap-good \(backend_raw_ssh\): captured -> %s"' % re.escape(_capture("ap-good"))
    assert re.search(good, out), out
    assert not re.search(r'"msg": "ap-broken \(', out), out
    with open(os.path.join(lab.backups, _capture("ap-good")), encoding="utf-8") as fh:
        assert fh.read() == "config system\n"
    changed = _git(lab.backups, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert _capture("ap-good") in changed and _capture("ap-broken") not in changed, changed
    assert not os.path.exists(lab.keys), os.listdir(lab.keys)
