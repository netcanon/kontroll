"""What an onboard writes when the request leaves it to the backend: the secrets domain, the derived params, the token var.

WHY (S27, the 2026-10-12 plan's brief 14 §5): three onboarding defaults lived in code instead of in the backend
registry. The SOPS domain `network` was a literal in ten places (the planner, both apps' routes, the form, the CLI);
`network_os` was a code rule keyed on that word appearing in `requires`; and the api backend's token var was a branch
on a field NAME (`api_token` → `<host>_api_token`) beside a `token_var` half nothing ever filled. Moving them into the
backend descriptors is a refactor of the critical path, so the first row here is the BYTE-IDENTITY ORACLE: what an
onboard writes today, spelled out by the former rules for each shape of host name the request boundary accepts
(lower case, upper case, dotted, IPv4, IPv6 — the upper-case and dotted names are the ones a lower-case-only output
rule would refuse, MF-03). It is written against the code rules and must stay green, unchanged, under the data.
"""
import pytest
import yaml

from kontroll import authspec, catalog, probe
from kontroll.service import onboard, scaffold

pytestmark = pytest.mark.unit

# Every shape of inventory host name the request boundary accepts (paths.inventory_host), with its credential key.
HOST_NAMES = ["sw1", "SW-Core-1", "sw.lab.example", "192.0.2.9", "2001:db8::9"]
CLICONF = dict(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
HTTPAPI = dict(plugins={"httpapi": ["edgeos"]}, modules=["edgeos_api"])


def _former_lookup(sops_key, domain="network"):
    """The host var the planner has written for one key of a SOPS domain since N1 — spelled out here, not imported,
    so the oracle does not move with the code it checks."""
    expr = "lookup('community.sops.sops', inventory_dir ~ '/../secrets/%s.sops.yml') | from_yaml" % domain
    return ("{{ (%s).%s }}" if sops_key.isidentifier() else "{{ (%s)['%s'] }}") % (expr, sops_key)


@pytest.fixture
def probed(tmp_repo, monkeypatch, make_facts):
    """`probed(**facts)`: the probe returns these facts for any collection, and nothing is installed (no TIER-B)."""
    def canned(**facts):
        monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(collection=coll, **facts))
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    return canned


def _plan(host_name, **kw):
    return onboard.build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50", host_name=host_name,
                                      vectors=[], **kw)


def _host_vars(plan, host_name):
    return list(plan["host_block"]["edge_router"]["hosts"][host_name].items())


@pytest.mark.parametrize("host_name", HOST_NAMES)
def test_backend_data_reproduces_the_former_code_rules_for_a_cliconf_device(probed, host_name):
    """A cliconf collection onboarded with no domain and no backend named: the class's `backend_params` is
    `{network_os: <collection>.<name>}`, `ansible_network_os` is rendered from it, the domain is `network`, and the
    host block — every var, in order, and the serialized text — is what the former rules wrote, the SSH key's file
    path and its decrypt domain included. Guards the move of the `network_os` rule and the `network` default into
    the registry changing one byte of what an SSH-login onboard writes."""
    probed(**CLICONF)
    creds = {"username": "u", "password": "p", "ssh_private_key": "k\n", "enable_password": "e", "port": "2222"}
    plan = _plan(host_name, creds=creds)
    ck = host_name.replace("-", "_")
    expected = [("ansible_host", "192.0.2.50"), ("device_role", "backend_netcommon_cli"),
                ("ansible_connection", "network_cli"), ("ansible_network_os", "acme.edgeos.edgeos"),
                ("ansible_become", False),
                ("ansible_user", _former_lookup(ck + "_username")),
                ("ansible_password", _former_lookup(ck + "_password")),
                ("ansible_ssh_private_key_file", "{{ kontroll_device_key_dir }}/%s.key" % ck),
                ("kontroll_ssh_key_domain", "network"),
                ("ansible_become_password", _former_lookup(ck + "_enable_password")),
                ("ansible_port", 2222)]
    assert plan["error"] is None and plan["backend"] == "netcommon_cli"
    assert _host_vars(plan, host_name) == expected
    assert plan["host_text"] == yaml.safe_dump({"edge_router": {"hosts": {host_name: dict(expected)}}},
                                               sort_keys=False, allow_unicode=True, width=4096)
    assert plan["creds_to_set"] == {ck + "_username": "u", ck + "_password": "p", ck + "_ssh_private_key": "k\n",
                                    ck + "_enable_password": "e"}
    assert plan["secrets"] == "network"
    assert plan["module"]["backend_params"] == {"network_os": "acme.edgeos.edgeos"}
    assert plan["module"]["secrets_domain"] == "network"


@pytest.mark.parametrize("host_name", HOST_NAMES)
def test_backend_data_reproduces_the_former_code_rules_for_an_api_device(probed, host_name):
    """An httpapi collection with a token: the host var `<cred key>_api_token` (the cred key is the host name with
    `-` as `_`) bound to the SOPS lookup of the same key, `creds_to_set` keyed the same, the basic-auth pair beside
    it, no `backend_params`, and the domain `network`. Guards the token var's move from a branch on the field's
    name to the descriptor's `maps_to` template: the upper-case, dotted and IP-literal names are the ones a template
    whose output rule were an identifier would refuse."""
    probed(**HTTPAPI)
    plan = _plan(host_name, creds={"api_token": "t", "username": "u", "password": "p"})
    ck = host_name.replace("-", "_")
    expected = [("ansible_host", "192.0.2.50"), ("device_role", "backend_api"), ("ansible_connection", "local"),
                ("kontroll_probe_port", 443),
                (ck + "_api_token", _former_lookup(ck + "_api_token")),
                ("ansible_user", _former_lookup(ck + "_username")),
                ("ansible_password", _former_lookup(ck + "_password"))]
    assert plan["error"] is None and plan["backend"] == "api"
    assert _host_vars(plan, host_name) == expected
    assert plan["host_text"] == yaml.safe_dump({"edge_router": {"hosts": {host_name: dict(expected)}}},
                                               sort_keys=False, allow_unicode=True, width=4096)
    assert plan["creds_to_set"] == {ck + "_api_token": "t", ck + "_username": "u", ck + "_password": "p"}
    assert plan["secrets"] == "network" and plan["module"]["secrets_domain"] == "network"
    assert "backend_params" not in plan["module"]


@pytest.mark.parametrize("backend", ["netcommon_cli", "api", "raw_ssh"])
def test_every_runnable_shipped_backend_stores_in_network_by_default(probed, backend):
    """With no domain in the request, a class of every shipped backend that declares a role stores its credentials
    in `network` — the template's operational domain, whose `.sops.yaml` rule carries the runner's key. Guards the
    default drifting per backend in the move."""
    probed(**CLICONF)
    plan = _plan("h1", backend=backend, creds={"password": "p"})
    assert plan["error"] is None and plan["secrets"] == "network"
    assert plan["module"]["secrets_domain"] == "network"
    assert plan["host_text"].count("/../secrets/network.sops.yml") == 1


def test_the_scaffold_reproduces_the_former_network_os_rule(probed):
    """The scaffold writes the same `backend_params` the planner does for a cliconf collection (`<collection>.<name>`)
    and the domain it was given. Guards the scaffold's copy of the rule (now the shared helper) drifting."""
    probed(**CLICONF)
    res = scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", "network")
    assert res["error"] is None and res["module"]["backend_params"] == {"network_os": "acme.edgeos.edgeos"}
    assert res["module"]["secrets_domain"] == "network"


def test_a_tier_b_api_token_still_gets_its_host_var(probed, monkeypatch):
    """A credential field named `api_token` derived from a module's argument spec (TIER-B, which never sets a
    `maps_to`) on the api backend still lands as `<cred key>_api_token`, bound to its SOPS key. The one behaviour of
    the former name branch a plain move of the template onto the descriptor would drop: the planner applies the
    backend's templated `maps_to` by field name to a field that brings none."""
    probed(**HTTPAPI)
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.edgeos": "1.0.0"})
    monkeypatch.setattr(authspec, "auth_fields_from_argspec", lambda coll, modules, **kw: [
        {"field": "api_token", "kind": "token", "label": "api token", "sops_stem": "api_token", "secret": True,
         "required": True, "derivation": "no_log", "auth_set": "edgeos_api"}])
    plan = _plan("SW-Core-1", creds={"api_token": "t"}, deep=True)
    assert plan["cred_source"] == "deep"
    assert ("SW_Core_1_api_token", _former_lookup("SW_Core_1_api_token")) in _host_vars(plan, "SW-Core-1")
    assert plan["creds_to_set"] == {"SW_Core_1_api_token": "t"}
