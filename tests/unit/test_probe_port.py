"""S28 (T17) — reachability as data, at the play: which TCP port ping.yml's reachability probe uses for a host.

The probe's port is one Jinja expression in `ansible/playbooks/ping.yml`. These tests read that string out of the
play (so they test the file, not a copy) and render it with plain Jinja2 under StrictUndefined, whose `default`
filter is the one Ansible uses. A wrong expression flips a healthy host to UNREACHABLE and skips its class check,
fleet-wide; nothing here needs a device.

The safety argument is the second test: the two variables S28 adds (`ansible_port`, `kontroll_probe_port`) are set
by no host that exists before it, so for every such host the new expression gives exactly the old one's answer.
"""
import glob
import os

import jinja2
import pytest
import yaml

from kontroll import authspec, catalog

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OLD = "{{ mgmt_port | default(22) }}"          # the probe's port before S28
_NEW_VARS = ("ansible_port", "kontroll_probe_port")


def _probe_port_expression():
    """The `port:` of ping.yml's `wait_for` task, as written in the play."""
    with open(os.path.join(ROOT, "ansible", "playbooks", "ping.yml"), encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "ansible.builtin.wait_for" in node:
                found.append(node["ansible.builtin.wait_for"]["port"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(plays)
    assert len(found) == 1, "ping.yml should hold exactly one reachability probe"
    return found[0]


def _render(expr, hostvars):
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(expr).render(**hostvars)


@pytest.mark.parametrize("hostvars, port", [
    ({}, "22"),
    ({"kontroll_probe_port": 443}, "443"),
    ({"ansible_port": 2222}, "2222"),
    ({"ansible_port": 2222, "kontroll_probe_port": 443}, "2222"),
    ({"mgmt_port": 8443}, "8443"),
    ({"mgmt_port": 8443, "ansible_port": 2222, "kontroll_probe_port": 443}, "8443"),
    ({"ansible_port": None, "kontroll_probe_port": 443}, "443"),
    ({"mgmt_port": "", "ansible_port": 2222}, "2222"),
    ({"mgmt_port": None, "ansible_port": None, "kontroll_probe_port": None}, "22"),
], ids=["none", "backend-probe-port", "ssh-port", "ssh-port-wins-over-backend", "hand-override",
        "hand-override-wins", "null-falls-through", "empty-falls-through", "all-null"])
def test_probe_port_expression_truth_table(hostvars, port):
    """The probe uses the first of `mgmt_port` (a hand override), `ansible_port` (what Ansible connects on), the
    backend's `kontroll_probe_port`, then 22. Guards a precedence slip that probes a live host on the wrong port and
    reports it UNREACHABLE, and the old expression coming back (rows two to four gave 22: an `api` host was probed
    on the SSH port, L12 F-5). A var set to null or empty counts as unset (the last three rows): `wait_for` given no
    port only sleeps and reports ok, so a hand-written `ansible_port: ~` passed the probe with nothing listening
    (T17 review, N1)."""
    assert _render(_probe_port_expression(), hostvars) == port


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_probe_port_is_unchanged_for_a_host_that_sets_neither_new_var():
    """For any host that sets neither new variable, the new expression gives the old one's port, with or without
    `mgmt_port`; and no shipped inventory (the example overlay, the mock inventory) sets either, so no host that
    exists before S28 is probed on another port after it. Guards the change moving the probe of an already-onboarded
    host — the whole safety argument of S28, checkable by reading (the private overlay was checked the same way)."""
    for hostvars in ({}, {"mgmt_port": 8443}, {"mgmt_port": 22}, {"ansible_host": "192.0.2.1", "device_role": "x"}):
        assert _render(_probe_port_expression(), hostvars) == _render(_OLD, hostvars), hostvars
    files = glob.glob(os.path.join(ROOT, "instance.example", "inventory", "**", "*.yml"), recursive=True)
    files.append(os.path.join(ROOT, "tests", "mock-inventory.yml"))
    assert len(files) > 1
    for path in files:
        with open(path, encoding="utf-8") as fh:
            for doc in yaml.safe_load_all(fh):
                assert not set(_keys(doc)) & set(_NEW_VARS), path


def test_backend_probe_ports_are_integers_in_range():
    """Every backend's `probe_port`, where one is declared, is a YAML integer in 1..65535 (not a quoted string, not a
    bool), and `api` declares 443, the port its recipes' requests default to. Guards a quoted or templated value in a
    descriptor (the planner refuses it, so every onboard on that backend would fail) and the API probe port being
    dropped."""
    declared = {b["name"]: b["probe_port"] for b in catalog.load_backends() if "probe_port" in b}
    assert declared.get("api") == 443
    for name, value in declared.items():
        assert type(value) is int and authspec.port_number(value) == value, name


def _shipped_auth_blocks():
    for b in catalog.load_backends():
        yield "backend " + b["name"], b
    for path in sorted(glob.glob(os.path.join(ROOT, "modules", "*", "module.yml"))):
        with open(path, encoding="utf-8") as fh:
            yield path, yaml.safe_load(fh) or {}


def test_every_shipped_inline_field_is_a_validated_kind_aimed_at_a_var_of_its_kind():
    """Every `inline: true` credential field a shipped backend or module declares is of a kind authspec validates
    (a port, a flag) and is aimed at a var of that kind (a port at a `*_port` var), and the SSH backends offer the
    port the probe reads. Guards a descriptor that would refuse every onboard on its class, and the SSH port field
    being dropped from the form."""
    inline = {}
    for where, doc in _shipped_auth_blocks():
        for cf in authspec.auth_fields_for_backend(doc) or []:
            if cf.get("inline"):
                assert authspec.inline_target(cf) is not None, (where, cf["field"])
                inline.setdefault(where, []).append((cf["field"], cf["maps_to"]))
    for backend in ("netcommon_cli", "raw_ssh"):
        assert ("port", "ansible_port") in inline.get("backend " + backend, []), backend
    assert "backend api" not in inline              # the API's port is the recipe's: no operator box for it
