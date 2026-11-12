"""_log-run-id.yml run by ansible-playbook: where it stamps, and one `kontroll_run_id=` line per run (I-15, I-41, I-52).

WHY. Every playbook a run can start with imports _log-run-id.yml first (directly or through the fleet preamble), so
its run prints `kontroll_run_id=…`, the key that
correlates one operation across the ansible log, the Semaphore job and the script run-logs. The stamp played on
`hosts: all`. With an inventory that has no hosts (`all: {}`), `all` matches nothing, and the implicit localhost is
not in it, so nothing was stamped. A play on localhost still ran on the implicit localhost:
reload-observability.yml's real run POSTed `/-/reload` with nothing to correlate it by, by the installer and by
Semaphore alike, on both cores (the I-8 read's probe). The stamp now plays on `all:localhost`.

`run_once` takes the first host of the play, which is the first inventory host when there is one, so a fleet's stamp
lands where it did and its recap gains no localhost. A `--limit <host>` still stamps on that host (the reason the
pattern starts from `all`, not localhost: the onboard's verify runs `ping.yml --limit <host>`).

Both of the stamp's tasks run on the controller (`delegate_to: localhost`), so its line reads `[<host> -> localhost]`
for an inventory host, and a first host whose `ansible_connection` names a plugin that is not installed (network_cli
before bootstrap.yml installs ansible.netcommon) no longer fails the stamp and ends the run (I-52): bootstrap.yml now
imports the stamp, and it is the playbook that installs that plugin.

bootstrap.yml and local-canonical.yml printed no id; they now import the stamp first (I-41). local-canonical.yml is
also imported by deploy-stack.yml after deploy-stack's own stamp, and an unset id is `now()`, so the stamp prints only
once per run. Only a run can say what a run prints: the I-41 rows run small playbooks that import the real stamp,
shaped as the real ones are (an entry playbook: the stamp, then its own play on localhost; an outer one: the stamp, a
play, then the entry playbook, as deploy-stack.yml imports local-canonical.yml), against no inventory hosts, two
hosts, two hosts limited to one, and two whose first names a connection plugin that is not installed; other rows give
the run a caller's `-e` id, or `--tags`. Limited to one, the implicit localhost is outside the limit, so a localhost
play runs on no host, as the real ones' plays would: there the stamp is all that runs.

It needs ansible-playbook: where it is missing every row is skipped (the gate's images and CI's validate job have it).
Each row runs the repository's own _log-run-id.yml with an Ansible home, configuration, vars-plugin path and
inventory of its own. Every task is a controller-side action (debug, set_fact): nothing connects anywhere.
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
# a first host whose connection plugin is not installed (a name no collection has needs none to be missing)
NOSUCH = {"all": {"children": {"core_switch": {"hosts": {"sw-a": {"ansible_connection": "kontroll_nosuch"},
                                                         "sw-b": dict(LOCAL)}}}}}


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
    stamped = re.findall(r'^ok: \[([^\]\s]+)(?: -> localhost)?\] => \{\n\s+"msg": "kontroll_run_id=', p.stdout,
                         re.M)
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


def test_a_first_host_whose_connection_plugin_is_not_installed_is_still_stamped(tmp_path):
    """A fleet whose first host names a connection plugin no installed collection has: the stamp prints one line for
    that host, run on the controller (`[sw-a -> localhost]`), and the run passes. Without `delegate_to: localhost`
    the host's plugin was loaded for the debug, the stamp failed (rc 2) and the run ended there: in bootstrap.yml,
    before the task that installs the collection the plugin is in (the I-41 read A's B1; I-52)."""
    rc, out, stamped, ids, hosts = _run(tmp_path, NOSUCH)
    assert rc == 0, out
    assert stamped == ["sw-a"] and len(ids) == 1, out
    assert "ok: [sw-a -> localhost] =>" in out, out


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


ID = re.compile(r"kontroll_run_id=([^\s\"]+)")
SEEN = re.compile(r"seen=([^\s\"]+)")
# each: the inventory, the extra arguments, and how many times a localhost play runs (once, unless the limit leaves
# the implicit localhost out)
SHAPED = {
    "no inventory hosts": ("# no hosts\n", [], 1),
    "two hosts": ("h1 ansible_connection=local\nh2 ansible_connection=local\n", [], 1),
    "two hosts, --limit h2": ("h1 ansible_connection=local\nh2 ansible_connection=local\n", ["--limit", "h2"], 0),
    "a first host whose plugin is not installed": ("h1 ansible_connection=kontroll_nosuch\n"
                                                   "h2 ansible_connection=local\n", [], 1),
}


def _shaped_playbooks(d):
    """entry.yml: the stamp, then a localhost play that prints what `kontroll_run_id` it sees (the ssh key directory
    in group_vars/all.yml keys on it, `default('manual', true)`); outer.yml: the stamp, a play, then entry.yml."""
    work = ("- name: {name}\n  hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
            "    - name: Report\n      ansible.builtin.debug:\n"
            "        msg: \"seen={{{{ kontroll_run_id | default('manual', true) }}}}\"\n")
    stamp = "- name: Stamp the run correlation id\n  ansible.builtin.import_playbook: %s\n" % STAMP
    (d / "entry.yml").write_text(stamp + work.format(name="The entry playbook's own play"), encoding="utf-8")
    (d / "outer.yml").write_text(stamp + work.format(name="The outer playbook's play")
                                 + "- name: The entry playbook\n  ansible.builtin.import_playbook: entry.yml\n",
                                 encoding="utf-8")


def _run_shaped(tmp_path, playbook, inventory, *args):
    """Run `playbook` against `inventory` (the SHAPED key); return (ids printed, `seen=` values, output)."""
    text, extra, _ = SHAPED[inventory]
    home = tmp_path / "ansible-home"
    home.mkdir(exist_ok=True)
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n", encoding="utf-8")
    hosts = tmp_path / "hosts.ini"
    hosts.write_text(text, encoding="utf-8")
    _shaped_playbooks(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_VARS_PLUGINS=str(home / "plugins" / "vars"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(["ansible-playbook", "-i", str(hosts), str(tmp_path / playbook), *extra, *args],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    assert p.returncode == 0, "ansible-playbook rc=%d\n%s" % (p.returncode, out)
    return ID.findall(p.stdout), SEEN.findall(p.stdout), out


@pytest.mark.parametrize("inventory", sorted(SHAPED))
def test_a_playbook_run_alone_prints_one_id(tmp_path, inventory):
    """An entry playbook run alone (bootstrap.yml, or local-canonical.yml by a recovery path) prints exactly one
    `kontroll_run_id=` line, with no inventory hosts, with two, limited to one of them, and with a first host whose
    connection plugin is not installed; the play after it, where it runs (the limited run leaves out the localhost it
    runs on), still sees no `kontroll_run_id` (`manual`). Guards `all:localhost` (with `all` the run with no hosts
    prints none, with `localhost` the limited run prints none), the debug's `run_once` (two hosts would print two),
    the delegation (without it the last case ends at the stamp, rc 2, and its play never runs: bootstrap.yml's own
    play is the one that installs the plugin) and a stamp that set the id as a fact."""
    ids, seen, out = _run_shaped(tmp_path, "entry.yml", inventory)
    assert len(ids) == 1, out
    assert seen == ["manual"] * SHAPED[inventory][2], out


@pytest.mark.parametrize("inventory", sorted(SHAPED))
def test_a_playbook_imported_after_the_stamp_prints_no_second_id(tmp_path, inventory):
    """The outer playbook (shaped as deploy-stack.yml, which imports local-canonical.yml after its own stamp) prints
    exactly one `kontroll_run_id=` line for the whole run, the entry playbook's import of the stamp saying nothing.
    Guards the guard: without it, or without the fact it reads, the run printed two lines, and with no `-e` id two
    `now()` stamps can differ, so one run would carry two ids. Both localhost plays, where they run, see no id."""
    ids, seen, out = _run_shaped(tmp_path, "outer.yml", inventory)
    assert len(ids) == 1, out
    assert seen == ["manual", "manual"] * SHAPED[inventory][2], out


def test_an_id_the_caller_gives_is_the_one_printed_once(tmp_path):
    """With `-e kontroll_run_id=run-abc` (how a caller may pass one; no shipped caller does yet, api/audit.py), the
    outer playbook prints `kontroll_run_id=run-abc` once and no other id, and both plays see that id. Guards the
    caller's id: the guard must not mint, repeat or replace it."""
    ids, seen, out = _run_shaped(tmp_path, "outer.yml", "two hosts", "-e", "kontroll_run_id=run-abc")
    assert ids == ["run-abc"], out
    assert seen == ["run-abc", "run-abc"], out


@pytest.mark.parametrize("playbook", ["entry.yml", "outer.yml"])
def test_a_run_given_tags_is_stamped_once(tmp_path, playbook):
    """A run given `--tags` that names none of the stamp's tasks (here a tag no task carries, so every play's own task
    is skipped) still prints one `kontroll_run_id=` line, alone and under another stamp. Guards the stamp's
    `tags: [always]`: without it a `--tags` run, an Ansible run like any other, printed no id (the I-41 read A's N2,
    measured on both cores)."""
    ids, seen, out = _run_shaped(tmp_path, playbook, "two hosts", "--tags", "kontroll_no_such_tag")
    assert len(ids) == 1, out
    assert seen == [], out
