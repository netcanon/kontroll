"""_log-run-id.yml run by ansible-playbook on an empty inventory, a fleet, a `--limit` and an injected id (I-15).

WHY. The fleet and stack playbooks import _log-run-id.yml first, so their run prints `kontroll_run_id=…`, the key that
correlates one operation across the ansible log, the Semaphore job and the script run-logs. The stamp played on
`hosts: all`. With an inventory that has no hosts (`all: {}`), `all` matches nothing, and the implicit localhost is
not in it, so nothing was stamped. A play on localhost still ran on the implicit localhost:
reload-observability.yml's real run POSTed `/-/reload` with nothing to correlate it by, by the installer and by
Semaphore alike, on both cores (the I-8 read's probe). The stamp now plays on `all:localhost`.

`run_once` takes the first host of the play, which is the first inventory host when there is one, so a fleet's stamp
lands where it did and its recap gains no localhost. A `--limit <host>` still stamps on that host (the reason the
pattern starts from `all`, not localhost: the onboard's verify runs `ping.yml --limit <host>`).

It needs ansible-playbook: where it is missing every row is skipped (the gate's images and CI's validate job have it).
Each row runs the repository's own _log-run-id.yml with an Ansible home, configuration, vars-plugin path and
inventory of its own.
"""
import datetime
import os
import re
import shutil
import subprocess

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                       reason="needs ansible-playbook (it runs _log-run-id.yml for real)"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STAMP = os.path.join(ROOT, "ansible", "playbooks", "_log-run-id.yml")
LOCAL = {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}
FLEET = {"all": {"children": {"core_switch": {"hosts": {"sw-a": dict(LOCAL), "sw-b": dict(LOCAL)}}}}}


def _run(tmp_path, inventory, *extra, tz=None):
    """Run _log-run-id.yml against `inventory` (with `TZ=tz` when given); return its exit code, its output, the hosts
    its stamp line was printed for, the ids printed, and the hosts its recap lists. The vars-plugin path is the row's
    own too: the default path includes the site-wide /usr/share/ansible/plugins/vars, which ANSIBLE_HOME does not
    move (the I-15 read B's N-10)."""
    home = tmp_path / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n", encoding="utf-8")
    inv = tmp_path / "hosts.yml"
    inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_VARS_PLUGINS=str(home / "plugins" / "vars"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", PYTHONDONTWRITEBYTECODE="1")
    if tz is not None:
        env["TZ"] = tz
    p = subprocess.run(["ansible-playbook", "-i", str(inv), STAMP] + list(extra), cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    stamped = re.findall(r'^ok: \[([^\]]+)\] => \{\n\s+"msg": "kontroll_run_id=', p.stdout, re.M)
    ids = re.findall(r'"msg": "kontroll_run_id=([^"]*)"', p.stdout)
    recap = p.stdout[p.stdout.find("PLAY RECAP"):] if "PLAY RECAP" in p.stdout else ""
    hosts = re.findall(r"^(\S+)\s*:\s*ok=", recap, re.M)
    return p.returncode, out, stamped, ids, hosts


def _is_utc_now(run_id, before, after):
    """True when `run_id` is a `%Y%m%dT%H%M%SZ` stamp of a UTC time between `before` and `after` (to the second)."""
    try:
        at = datetime.datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return False
    return before.replace(microsecond=0) <= at <= after


def test_an_inventory_with_no_hosts_is_stamped_on_the_implicit_localhost(tmp_path):
    """The I-15 case: `all: {}`. The stamp is printed once, for localhost, with a UTC id, and the run passes. On
    `hosts: all` nothing was printed, while a localhost play of the same run still acted. The run's TZ is five hours
    off UTC and the id must be the UTC time of the run, so a local time printed with a literal `Z` fails here, not
    only on a controller whose clock is not UTC: the API's run ids are UTC (the I-15 read B's N-2)."""
    before = datetime.datetime.now(datetime.timezone.utc)
    rc, out, stamped, ids, hosts = _run(tmp_path, {"all": {}}, tz="ABC+5")
    after = datetime.datetime.now(datetime.timezone.utc)
    assert rc == 0, out
    assert stamped == ["localhost"], out
    assert len(ids) == 1 and _is_utc_now(ids[0], before, after), out
    assert hosts == ["localhost"], out


def test_a_fleet_is_stamped_once_on_its_first_host_and_its_recap_gains_no_localhost(tmp_path):
    """With hosts in the inventory, the stamp lands on the first of them, once, as it did on `hosts: all`. The
    recap lists that host alone, so a fleet run's recap (which the GUI and Semaphore show) does not gain a localhost
    it never named."""
    rc, out, stamped, ids, hosts = _run(tmp_path, FLEET)
    assert rc == 0, out
    assert stamped == ["sw-a"], out
    assert len(ids) == 1, out
    assert hosts == ["sw-a"], out


def test_a_limit_to_one_host_is_still_stamped_on_it(tmp_path):
    """`--limit <host>` (the onboard's verify runs `ping.yml --limit <host>`): the stamp is printed for that host. A
    pattern of localhost alone would be limited away here, which is why the stamp starts from `all`."""
    rc, out, stamped, ids, hosts = _run(tmp_path, FLEET, "--limit", "sw-b")
    assert rc == 0, out
    assert stamped == ["sw-b"], out
    assert hosts == ["sw-b"], out


@pytest.mark.parametrize("inventory,host", [({"all": {}}, "localhost"), (FLEET, "sw-a")], ids=["no-hosts", "fleet"])
def test_an_injected_id_is_the_one_stamped(tmp_path, inventory, host):
    """A caller's `-e kontroll_run_id=…` (the playbook's header says a caller may inject one) is the id printed, once,
    on an inventory with no hosts and on a fleet, so the run is found by the id its caller logged (the fleet case
    was probed and pinned by nothing: the I-15 read B's N-9)."""
    rc, out, stamped, ids, hosts = _run(tmp_path, inventory, "-e", "kontroll_run_id=probe-i15-run")
    assert rc == 0, out
    assert stamped == [host] and ids == ["probe-i15-run"], out


def test_an_empty_injected_id_falls_back_to_a_utc_stamp(tmp_path):
    """`-e kontroll_run_id=` (a caller that templates an unset id) prints one UTC id, not an empty one: the stamp's
    `default(…, true)` treats an empty id as none. Without the `true` the run printed `kontroll_run_id=` and nothing
    after it, an id that correlates nothing, and every other row passed (the I-15 read B's N-3)."""
    before = datetime.datetime.now(datetime.timezone.utc)
    rc, out, stamped, ids, hosts = _run(tmp_path, {"all": {}}, "-e", "kontroll_run_id=")
    after = datetime.datetime.now(datetime.timezone.utc)
    assert rc == 0, out
    assert stamped == ["localhost"] and len(ids) == 1 and _is_utc_now(ids[0], before, after), out
