"""The mock inventory (tests/mock-inventory.yml) covers every shipped device class, and every host in it dispatches.

WHY — tests/fresh-node-check.sh runs the fleet plays (ping.yml, backup-configs.yml) against this file in check mode,
so the classes it names are the classes whose `check` and `backup` entrypoints anything evaluates. Until T27 two of
its hosts had no `device_role` (`include_role: name: "{{ device_role }}"` is then undefined: ping.yml's rescue
reported it as UNREACHABLE, backup-configs.yml failed the host), the edge still said `cisco_ios`, and three classes
had no host at all, so a class could ship with an entrypoint nothing had ever evaluated. The coverage is derived from
modules/*/module.yml, as the collections are: a new class with no mock host fails here, not in a deploy.
"""
import glob
import ipaddress
import os

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def _groups(group_vars=None):
    """{group: {host: vars}} for every group of the mock inventory that holds hosts (children walked); with a dict as
    `group_vars`, each group's own `vars` (all's included) are put in it too, wherever the group is defined."""
    with open(os.path.join(ROOT, "tests", "mock-inventory.yml"), encoding="utf-8") as fh:
        inv = yaml.safe_load(fh)
    out = {}

    def walk(name, node):
        node = node or {}
        if group_vars is not None:          # merged: a group named again under `children:` carries no vars there
            group_vars.setdefault(name, {}).update(node.get("vars") or {})
        if node.get("hosts"):
            out.setdefault(name, {}).update({h: (v or {}) for h, v in node["hosts"].items()})
        for child, sub in (node.get("children") or {}).items():
            walk(child, sub)

    walk("all", inv["all"])
    return out


def _hosts():
    return {h: v for hosts in _groups().values() for h, v in hosts.items()}


def _modules():
    """Each shipped device class's module.yml, parsed."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "modules", "*", "module.yml"))):
        with open(path, encoding="utf-8") as fh:
            out.append(yaml.safe_load(fh))
    return out


def test_every_mock_host_has_a_device_role_that_is_a_real_role():
    """Each host in the mock inventory names a `device_role` whose role directory holds both fleet entrypoints
    (tasks/check.yml for ping.yml, tasks/backup.yml for backup-configs.yml). Guards a host the fleet plays cannot
    dispatch: an undefined or misspelt role is a template error the check-mode run reports as an unreachable host
    (ping.yml's rescue) or a failed one, so the step that runs the plays against this file would test nothing for it."""
    hosts = _hosts()
    assert hosts, "the mock inventory names no host"
    for host, hv in hosts.items():
        role = hv.get("device_role")
        assert role, "%s has no device_role" % host
        for entry in ("check", "backup"):
            assert os.path.isfile(os.path.join(ROOT, "ansible", "roles", role, "tasks", entry + ".yml")), \
                "%s: roles/%s has no tasks/%s.yml" % (host, role, entry)


def test_every_shipped_class_has_a_mock_host():
    """For each modules/<key>/module.yml, a mock host in that class's `inventory_group` carries its `role` as its
    `device_role` (and, for a class run by a generic backend, its `backend_params.network_os` as
    `ansible_network_os`, which that backend reads). Guards a class shipping with no evaluated entrypoint: the list is
    derived from modules/, so adding a class without a mock host fails here."""
    groups = _groups()
    mods = _modules()
    assert mods, "no modules/*/module.yml found"
    for m in mods:
        members = groups.get(m["inventory_group"], {})
        match = [h for h, hv in members.items() if hv.get("device_role") == m["role"]]
        assert match, "class %s: no host in %s with device_role %s" % (m["key"], m["inventory_group"], m["role"])
        nos = (m.get("backend_params") or {}).get("network_os")
        if nos:
            assert any(members[h].get("ansible_network_os") == nos for h in match), \
                "class %s: its mock host must set ansible_network_os: %s" % (m["key"], nos)


def _fits(m, hv):
    """Whether a mock host's vars are the class's: its role as the `device_role` and, for a class run by a generic
    backend, its `network_os` as `ansible_network_os`."""
    nos = (m.get("backend_params") or {}).get("network_os")
    return hv.get("device_role") == m["role"] and (not nos or hv.get("ansible_network_os") == nos)


def test_every_shipped_class_has_a_mock_host_of_its_own():
    """The classes can each be given a different mock host that fits them (in the class's `inventory_group`, with
    its role and network_os): as many hosts as classes, one per class key. Guards a class added under a new key that
    reuses another class's role and group (a variant of one): the rule above finds the other class's host and passes,
    so the new class is counted as covered while one host stands for two (T27's fold-1 read, N5), and the header's
    "ONE host per shipped device class" is no longer what the file holds. And there are no more hosts than classes:
    a host left from a removed class whose role still exists would otherwise pass every rule here (T27's fold-2
    read, N3)."""
    groups = _groups()
    mods = _modules()
    fit = {m["key"]: [h for h, hv in groups.get(m["inventory_group"], {}).items() if _fits(m, hv)] for m in mods}
    owner = {}

    def place(key, seen):                     # Kuhn's augmenting path: give `key` a host, moving others if need be
        for h in fit[key]:
            if h not in seen:
                seen.add(h)
                if h not in owner or place(owner[h], seen):
                    owner[h] = key
                    return True
        return False

    unplaced = [k for k in sorted(fit) if not place(k, set())]
    assert not unplaced, "classes with no mock host of their own (add one each, in its inventory_group): %s; " \
        "the hosts that fit them: %s" % (unplaced, {k: fit[k] for k in unplaced})
    hosts = sorted(_hosts())
    assert len(hosts) == len(mods), "the mock inventory holds %d hosts for %d classes (one per class; a host no " \
        "class needs, left from a removed one, is %s)" % (len(hosts), len(mods), sorted(set(hosts) - set(owner)))


def test_every_mock_host_is_a_documentation_address_reached_locally():
    """Every mock host's `ansible_host` is in an RFC-5737 documentation range, and the file sets
    `ansible_connection: local` for all of them, with no group or host setting another. Guards the seam's one safety
    property: a check-mode run against this file, or a hand run that forgets `--check`, reaches no real device; a
    group's own vars outrank all's, so `ansible_connection: ssh` on one group would send its hosts out (T27's fold-2
    read, N3)."""
    gv = {}
    _groups(gv)
    assert gv["all"].get("ansible_connection") == "local", gv["all"]
    assert not [g for g, v in gv.items() if g != "all" and "ansible_connection" in v], gv
    for host, hv in _hosts().items():
        assert "ansible_connection" not in hv, "%s overrides ansible_connection" % host
        addr = ipaddress.ip_address(hv["ansible_host"])
        assert any(addr in n for n in DOC_NETS), "%s: %s is not a documentation address" % (host, addr)
