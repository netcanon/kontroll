"""The capture writes of the raw-SSH and RouterOS backup entrypoints render in a --check run.

WHY. Both read the device with a task check mode skips (`raw` always; `community.routeros.command`, which has no
check mode), and a skipped task's result has no `stdout`. Their `Write capture` read `_cfg.stdout` and
`ros_export.stdout[0]` regardless, so `backup-configs.yml --check` failed every openwrt and RouterOS host there, on a
real fleet as on the mock one (tests/fresh-node-check.sh's backup step, T27's first real run). Each write now reads
an empty capture in a dry-run, and the device's output exactly as before in a real run.

The `content` expressions are read from the role files and rendered with core Jinja and StrictUndefined, as Ansible
fails on an undefined attribute; the skipped results are the shape Ansible registers for a skipped task.
"""
import os

import jinja2
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIPPED = {"changed": False, "skipped": True, "skip_reason": "check mode"}
ROLES = {  # role: (the read's register, a real run's result of that read, the capture it writes)
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
    """In check mode, with the read's result a skipped task's (no `stdout`), the write renders an empty capture
    (the RouterOS one keeps its trailing newline). Guards `backup-configs.yml --check` failing every host of these
    classes at the write, as it did before."""
    assert _render(role, SKIPPED, True) == ("\n" if role == "routeros" else "")


@pytest.mark.parametrize("role", sorted(ROLES))
def test_a_real_run_writes_the_device_output_and_still_needs_it(role):
    """Outside check mode the write renders the read's output exactly as before, and a result with no output still
    fails the render. Guards the dry-run's default leaking into a real run, where an empty capture would overwrite
    a good one without an error."""
    register, real, capture = ROLES[role]
    assert _render(role, real, False) == capture
    with pytest.raises(jinja2.UndefinedError):
        _render(role, SKIPPED, False)
