"""The capture writes of the raw-SSH, RouterOS, API and CLI backup entrypoints render in a --check run.

WHY. Each reads the device with a task check mode skips (`raw` always; `community.routeros.command` and
`ansible.builtin.uri`, which have no check mode), and a skipped task's result has no `stdout` or `content`. The
raw-SSH and RouterOS `Write capture` read `_cfg.stdout` and `ros_export.stdout[0]` regardless, so
`backup-configs.yml --check` failed every openwrt and RouterOS host there, on a real fleet as on the mock one
(tests/fresh-node-check.sh's backup step, T27's first real run). The API write was not reached in a dry-run: its
failure record read the skipped pull as a failed answer first (I-6; test_backend_api_check_mode.py holds when each
task runs). Each write now reads an empty capture in a dry-run, and the device's output exactly as before in a real
run.

The `content` expressions are read from the role files and rendered with core Jinja and StrictUndefined, as Ansible
fails on an undefined attribute; the skipped results are the shape Ansible registers for a skipped task. And no
role's capture write is forced to run in a dry-run, where it would write that empty preview over a good capture.
"""
import glob
import os

import jinja2
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A check-mode skip's keys (its message names the module): no stdout or content, nor the `skip_reason` a `when` adds.
SKIPPED = {"changed": False, "skipped": True, "msg": "This action does not support check mode."}
ROLES = {  # role: (the read's register, a real run's result of that read, the capture it writes)
    "backend_api": ("_api_resp", {"status": 200, "content": "config system global\n"}, "config system global\n"),
    # the CLI backend's read is skipped in a dry-run only for a command that is not a `show` (I-11)
    "backend_netcommon_cli": ("_cfg", {"changed": False, "stdout": "hostname sw1\n!"}, "hostname sw1\n!\n"),
    "backend_raw_ssh": ("_cfg", {"rc": 0, "stdout": "config system\n", "stderr": ""}, "config system\n"),
    "routeros": ("ros_export", {"stdout": ["/ip address\nadd address=192.0.2.5/24"]},
                 "/ip address\nadd address=192.0.2.5/24\n"),
}


def _write(role):
    """The `copy` of the role's one `Write capture` task, wherever in the backup entrypoint it sits."""
    def walk(tasks):
        for t in tasks or []:
            yield t
            for k in ("block", "rescue", "always"):
                yield from walk(t.get(k))
    tasks = yaml.safe_load(open(os.path.join(ROOT, "ansible", "roles", role, "tasks", "backup.yml"), encoding="utf-8"))
    found = [t for t in walk(tasks) if t.get("name") == "Write capture"]
    assert len(found) == 1, role
    return found[0]["ansible.builtin.copy"]


def _render(role, result, check_mode):
    register = ROLES[role][0]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string(_write(role)["content"]).render({register: result, "ansible_check_mode": check_mode})


@pytest.mark.parametrize("role", sorted(ROLES))
def test_a_dry_run_writes_an_empty_capture_where_the_read_was_skipped(role):
    """In check mode, with the read's result a skipped task's (no `stdout` or `content`), the write renders an empty
    capture (the RouterOS and CLI ones keep their trailing newline). Guards `backup-configs.yml --check` failing every
    host of these classes at the write, as the raw-SSH and RouterOS ones did before T27, and a CLI class whose backup
    command is not a `show` (I-11), whose dry-run now skips the read."""
    assert _render(role, SKIPPED, True) == ("\n" if role in ("routeros", "backend_netcommon_cli") else "")


@pytest.mark.parametrize("role", sorted(ROLES))
def test_a_real_run_writes_the_device_output_and_still_needs_it(role):
    """Outside check mode the write renders the read's output exactly as before, and a result with no `stdout` or
    `content` (a skipped read's) still fails the render. Guards the dry-run's default leaking into a real run, where
    a read that left no result would be written as an empty capture without an error. A read that returns an empty
    `stdout` renders as one here; since I-5 the write does not run for it (test_backup_empty_capture_refused.py)."""
    register, real, capture = ROLES[role]
    assert _render(role, real, False) == capture
    with pytest.raises(jinja2.UndefinedError):
        _render(role, SKIPPED, False)


def _capture_writes():
    """(role, task name, check_mode) for every task in a role's task files whose module writes a `dest` under
    `config_backup_dir`, with the `check_mode` of the blocks around it where it sets none."""
    out = []

    def walk(role, tasks, forced):
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            here = t.get("check_mode", forced)
            if any(isinstance(t.get(k), list) for k in ("block", "rescue", "always")):
                for k in ("block", "rescue", "always"):
                    walk(role, t.get(k), here)
                continue
            if any(isinstance(v, dict) and "config_backup_dir" in str(v.get("dest", "")) for v in t.values()):
                out.append((role, t.get("name"), here))

    for path in sorted(glob.glob(os.path.join(ROOT, "ansible", "roles", "*", "tasks", "*.yml"))):
        with open(path, encoding="utf-8") as fh:
            walk(os.path.basename(os.path.dirname(os.path.dirname(path))), yaml.safe_load(fh), None)
    return out


def test_no_capture_write_is_forced_to_run_in_check_mode():
    """No role task that writes under `config_backup_dir` carries `check_mode: false`, on itself or on a block around
    it; and the walk finds the writes of the backends these tests render, and OPNsense's. A forced write runs in the
    mandated `--check` and writes its preview, the empty capture, over the device's last good one. T27's fold-2 read
    planted it on the raw-SSH write and the fresh-node check passed (N6); that check now fails on a written capture,
    but it reaches only the writes a local connection lets a class reach, and the classes whose read stops there
    (OPNsense and the netcommon classes) never get to theirs, so it is held here for every role."""
    writes = _capture_writes()
    names = {(role, name) for role, name, _ in writes}
    assert {("backend_raw_ssh", "Write capture"), ("routeros", "Write capture"), ("backend_api", "Write capture"),
            ("opnsense", "Write capture")} <= names, sorted(names)
    assert [(r, n) for r, n, cm in writes if str(cm).strip().lower() == "false"] == []
