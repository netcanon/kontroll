"""backup-configs.yml run for real: a device that answers nothing keeps its last good capture, and the run fails (I-5).

WHY. tests/unit/test_backup_empty_capture_refused.py evaluates each backup's conditions; it cannot say what a run
does with them: that the refusal runs before the write, that its failure reaches the summary and fails the run there,
that the other hosts' captures are still written and committed to the capture history, and that a dry-run writes
nothing and refuses nothing it did not read. T27's runtime read found the fault this way: a raw-SSH `cmd=true`
against a seeded good capture left a 0-byte capture, and the run passed. These rows run the real play against three
hosts of the generic raw-SSH backend on a local connection, the one backend whose read a local connection makes for
real, with local commands for backup commands: one prints a config, one prints nothing, one prints only blank lines.
A fourth host is unreachable (a connection plugin of the row's own fails every connection, as ssh's exit 255 does):
the play tolerates it, and it is neither refused nor written. Each host has a good capture in place and in the
history before the run.

The Proxmox and Docker backups print a `### ` header before each read, so a host whose reads all answer nothing
answers its section headers alone and exits 0 (I-10). Their rows run each against a host on the same local connection,
beside the good raw-SSH host, so the history play runs and its commit can be read. The Docker host's `docker` is the
row's own, first on `PATH`, printing nothing and exiting 0: those rows run on any machine. The Proxmox host is this
machine: where `pveversion`, /etc/network/interfaces and /etc/pve are all missing (the gate's image) its script answers
its headers alone; where any of them is there, the Proxmox rows are skipped (the answer would have a line of the
machine's own), and a skip fails no gate, so they are proved in the gate's image only.

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
PVE = "pve-none"   # the Proxmox rows' host, at 192.0.2.41
DKR = "dk-silent"  # the Docker rows' host, at 192.0.2.51: its `docker` answers nothing
NO_PROXMOX = not (shutil.which("pveversion") or os.path.exists("/etc/network/interfaces") or os.path.exists("/etc/pve"))
HOSTS = {  # host: (address, its backup command)
    "ap-good": ("192.0.2.31", "printf 'config system\\n'"),
    "ap-empty": ("192.0.2.32", "true"),
    "ap-blank": ("192.0.2.33", "printf '\\n \\n'"),
    "ap-down": ("192.0.2.34", "printf 'config system\\n'"),
}
DOWN = "ap-down"   # unreachable: its connection is the row's own `down` plugin
DOWN_PLUGIN = '''from ansible.errors import AnsibleConnectionFailure
from ansible.plugins.connection import ConnectionBase

DOCUMENTATION = """
name: down
short_description: a host that cannot be reached
description: Every use fails as an ssh connection to a host that is down does.
author: kontroll tests
options: {}
"""


class Connection(ConnectionBase):
    transport = "down"
    has_pipelining = False

    def _connect(self):
        raise AnsibleConnectionFailure("down: the host cannot be reached")

    def exec_command(self, cmd, in_data=None, sudoable=True):
        self._connect()

    def put_file(self, in_path, out_path):
        self._connect()

    def fetch_file(self, in_path, out_path):
        self._connect()

    def close(self):
        self._connected = False
'''
GOOD = "config wifi-iface 'default_radio0'\n"   # each host's capture before the run


def _capture(host):
    return "backend_raw_ssh_%s.cfg" % HOSTS[host][0].replace(".", "-")


def _git(backups, *argv):
    return subprocess.run(["git", "-C", backups, *argv], check=True, capture_output=True, text=True).stdout


def _lab(tmp_path, hosts, captures):
    """The inventory of `hosts`, an Ansible home and configuration of the row's own (with the unreachable host's
    connection plugin), and a captures directory holding each of `captures`, good, committed as the history play
    commits."""
    home = tmp_path / "ansible-home"
    (home / "connection_plugins").mkdir(parents=True)
    (home / "connection_plugins" / "down.py").write_text(DOWN_PLUGIN, encoding="utf-8")
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nroles_path = %s\nfilter_plugins = %s\nconnection_plugins = %s\n"
                   "retry_files_enabled = False\ninterpreter_python = auto_silent\n"
                   % (os.path.join(ROOT, "ansible", "roles"), os.path.join(ROOT, "ansible", "filter_plugins"),
                      home / "connection_plugins"), encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "hosts": hosts}}), encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    for name in captures:
        (backups / name).write_text(GOOD, encoding="utf-8")
    _git(str(backups), "init", "-q")
    _git(str(backups), "add", "-A")
    _git(str(backups), "-c", "user.email=kontroll-backup@localhost", "-c", "user.name=kontroll backup",
         "commit", "-q", "-m", "backup before the run")
    # The fleet plays' key directory is the row's own: the key-cleanup play deletes it, and its default is a fixed path
    # under /tmp. No bytecode is written next to the tree's filter plugins.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(tmp_path / "keys"), PYTHONDONTWRITEBYTECODE="1")
    return type("Lab", (), {"base": str(tmp_path), "inventory": str(inventory), "backups": str(backups),
                            "env": env})()


def _raw_ssh(host):
    """The inventory entry of one of the raw-SSH hosts."""
    address, command = HOSTS[host]
    return dict({"ansible_host": address, "device_role": "backend_raw_ssh", "backend_backup_command": command},
                **({"ansible_connection": "down"} if host == DOWN else {}))


@pytest.fixture
def lab(tmp_path, isolated_git):
    """The four raw-SSH hosts, each with its good capture."""
    return _lab(tmp_path, {host: _raw_ssh(host) for host in HOSTS}, [_capture(host) for host in HOSTS])


PVE_CAPTURE = "Proxmox_%s_node-and-guests.txt" % PVE
DKR_CAPTURE = "Docker_%s_manifest.txt" % DKR


@pytest.fixture
def pve_lab(tmp_path, isolated_git):
    """One Proxmox host, on this machine, and the good raw-SSH host beside it, each with its good capture."""
    if not NO_PROXMOX:
        pytest.skip("this machine has pveversion, /etc/network/interfaces or /etc/pve: its answer is not headers alone")
    return _lab(tmp_path, {PVE: {"ansible_host": "192.0.2.41", "device_role": "proxmox"},
                           "ap-good": _raw_ssh("ap-good")}, [PVE_CAPTURE, _capture("ap-good")])


@pytest.fixture
def dkr_lab(tmp_path, isolated_git):
    """One Docker host opted in to the backup, on this machine, whose `docker` is the row's own (first on `PATH`: it
    prints nothing and exits 0, whatever it is asked), and the good raw-SSH host beside it, each with its good
    capture."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    lab = _lab(tmp_path, {DKR: {"ansible_host": "192.0.2.51", "device_role": "docker_host", "backup_capable": True},
                          "ap-good": _raw_ssh("ap-good")}, [DKR_CAPTURE, _capture("ap-good")])
    lab.env = dict(lab.env, PATH=str(bindir) + os.pathsep + lab.env.get("PATH", os.defpath))
    return lab


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


def _summary(out, host, role="backend_raw_ssh"):
    """The host's line of the play's summary, as its `debug` printed it (a JSON string)."""
    found = re.findall(r'"msg": "(%s \(%s\): .*)"' % (re.escape(host), re.escape(role)), out)
    assert len(found) == 1, (host, out)
    return json.loads('"%s"' % found[0]).split(": ", 1)[1]


def _stamped(out):
    """The fleet preamble's run id line: the one the summary's lines are correlated by."""
    return re.search(r'"msg": "kontroll_run_id=[0-9]{8}T[0-9]{6}Z"', out) is not None


def test_a_real_run_refuses_the_empty_answers_keeps_their_captures_and_fails(lab):
    """A real run writes and commits ap-good's new capture, and nothing else: ap-empty's and ap-blank's summary lines
    say FAILED, naming the command that answered nothing, their good captures are kept byte for byte, the run fails
    at its summary for those two hosts only, and the history play still runs; ap-down, unreachable, is tolerated,
    not refused, and its summary says it was not run; and the run id is stamped. Guards I-5: before it, both empty
    answers were written over the good captures, recorded captured, and committed as a change, and the run passed;
    and an unreachable host reported as a read that answered nothing (I-5's runtime read, S-1)."""
    rc, out, recap = _play(lab)
    assert rc != 0, out
    assert recap == {"ap-good": 0, "ap-empty": 1, "ap-blank": 1, "ap-down": 0, "localhost": 0}, (recap, out)
    assert _stamped(out), out
    assert _summary(out, "ap-good") == "captured -> %s" % _capture("ap-good")
    assert _summary(out, DOWN) == "not run — unreachable or skipped"
    assert _summary(out, "ap-empty").startswith("FAILED: `true` answered nothing, so no capture was written")
    assert _summary(out, "ap-blank").startswith("FAILED: `printf '\\n \\n'` answered nothing")
    for host in ("ap-empty", "ap-blank", DOWN):
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
    recorded captured (ap-down too: its read is skipped before any connection), no capture or commit changes, and
    the run id is stamped. Guards the refusal reaching a read the dry-run skipped: the mandated dry-run would then
    fail every raw-SSH host (OpenWrt's class) at its summary, as T27's fresh-node check found the write's own
    template did before it was fixed."""
    rc, out, recap = _play(lab, check=True)
    assert rc == 0 and set(recap.values()) == {0}, (rc, recap, out)
    assert _stamped(out), out
    for host in HOSTS:
        assert _summary(out, host) == "captured -> %s" % _capture(host)
        with open(os.path.join(lab.backups, _capture(host)), encoding="utf-8") as fh:
            assert fh.read() == GOOD, host
    assert _git(lab.backups, "rev-list", "--count", "HEAD").strip() == "1"


def _capture_commits(lab, name):
    """How many commits of the capture history touched the capture."""
    return len(_git(lab.backups, "log", "--format=%H", "--", name).split())


def _committed_beside(lab, kept):
    """The run's one new commit holds the good raw-SSH host's new capture, and nothing of the capture `kept`: the
    history play ran (a host beside the refused one kept the play going), and the refused host's capture was not
    in it."""
    assert _git(lab.backups, "rev-list", "--count", "HEAD").strip() == "2"
    changed = _git(lab.backups, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert _capture("ap-good") in changed and kept not in changed, changed   # and the play's .gitignore
    assert _capture_commits(lab, kept) == 1
    assert _git(lab.backups, "status", "--porcelain", "--untracked-files=no").strip() == ""


def test_a_real_run_refuses_a_proxmox_answer_of_headers_alone_and_keeps_the_capture(pve_lab):
    """A real run against a host with no Proxmox on it fails at its summary for that host, whose line says FAILED,
    the node having answered only the capture's section headers; its good capture is kept byte for byte; the good
    raw-SSH host beside it is captured, so the history play runs, and its new commit holds that host's capture and
    nothing of the Proxmox host's; and the run id is stamped. Guards I-10: the script's `### ` headers made that
    answer non-empty, so it was written over the good capture, recorded captured and committed, and the run passed
    (the Proxmox and Docker backups were exempt from I-5's refusal as never empty). With the Proxmox host alone,
    "no commit touches it" could not fail: a play whose every host has failed ends the playbook before the history
    play (the I-10 read A, N-2)."""
    rc, out, recap = _play(pve_lab)
    assert rc != 0, out
    assert recap == {PVE: 1, "ap-good": 0, "localhost": 0}, (recap, out)
    assert _stamped(out), out
    assert _summary(out, PVE, "proxmox").startswith(
        "FAILED: the node answered only the capture's section headers (no pveversion, network or /etc/pve), so no "
        "capture was written and any earlier one is kept."), out
    assert _summary(out, "ap-good") == "captured -> %s" % _capture("ap-good")
    with open(os.path.join(pve_lab.backups, PVE_CAPTURE), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    _committed_beside(pve_lab, PVE_CAPTURE)


def test_a_dry_run_of_a_proxmox_host_previews_its_capture_writes_nothing_and_passes(pve_lab):
    """`--check --diff` against the same hosts passes: `shell` does not run its script under `--check`, so the
    capture is previewed and recorded captured, and nothing is written or committed. Guards the refusal reaching the
    read a dry-run skips: the mandated dry-run would then fail every Proxmox node at its summary."""
    rc, out, recap = _play(pve_lab, check=True)
    assert rc == 0 and recap == {PVE: 0, "ap-good": 0, "localhost": 0}, (rc, recap, out)
    assert _summary(out, PVE, "proxmox") == "captured -> %s" % PVE_CAPTURE
    with open(os.path.join(pve_lab.backups, PVE_CAPTURE), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    assert _git(pve_lab.backups, "rev-list", "--count", "HEAD").strip() == "1"


def test_a_docker_host_whose_docker_answers_nothing_dry_runs_clean_then_keeps_its_manifest_and_fails(dkr_lab):
    """A Docker host opted in to the backup, whose `docker` answers nothing and exits 0, beside the good raw-SSH
    host. `--check --diff` passes: the script does not run, both captures are previewed and recorded captured, and
    nothing is written or committed. The real run then fails at its summary for the Docker host, whose line says
    FAILED, the host having answered only the manifest's section headers; its good manifest is kept byte for byte;
    the other host is captured, and the history's new commit holds that host's capture and nothing of the Docker
    host's; and the run id is stamped.

    Guards I-10 for Docker on every machine: such a docker's answer was its headers alone, written over the good
    manifest, recorded captured and committed, and the run passed (measured on the role before the fix, the I-10
    read B). The Proxmox rows need a machine with no Proxmox piece on it and are skipped anywhere else; this row needs
    only its own `docker`, so a header renamed out of the `### ` form is caught here too (the I-10 reads' survivors)."""
    rc, out, recap = _play(dkr_lab, check=True)
    assert rc == 0 and recap == {DKR: 0, "ap-good": 0, "localhost": 0}, (rc, recap, out)
    assert _summary(out, DKR, "docker_host") == "captured -> %s (manifest)" % DKR_CAPTURE
    assert _git(dkr_lab.backups, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(dkr_lab.backups, "status", "--porcelain", "--untracked-files=no").strip() == ""
    rc, out, recap = _play(dkr_lab)
    assert rc != 0, out
    assert recap == {DKR: 1, "ap-good": 0, "localhost": 0}, (recap, out)
    assert _stamped(out), out
    assert _summary(out, DKR, "docker_host").startswith(
        "FAILED: the host answered only the manifest's section headers (no docker version, containers, images or "
        "stacks), so no capture was written and any earlier one is kept."), out
    assert _summary(out, "ap-good") == "captured -> %s" % _capture("ap-good")
    with open(os.path.join(dkr_lab.backups, DKR_CAPTURE), encoding="utf-8") as fh:
        assert fh.read() == GOOD
    _committed_beside(dkr_lab, DKR_CAPTURE)
