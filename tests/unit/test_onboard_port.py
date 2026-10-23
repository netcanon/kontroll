"""S28 (T17) — reachability as data, at the planner: what an onboard writes for the reachability probe's port.

Three writes are pinned here. An `api` host carries its backend's `probe_port` as `kontroll_probe_port` (an int),
so ping.yml probes a REST device on its API port instead of the SSH port, where it read UNREACHABLE and its check
never ran (L12 F-5). An SSH host carries the port the operator typed as `ansible_port` (an int). An SSH host with
no port typed is byte-identical to the drop-in written before this change.

And the boundary this opens: the SSH port is the first request value that lands in an inventory host var, where
Ansible templates it on the control node. authspec types it — an int, or a 422 naming the field and never the
value — so the request never chooses the bytes that reach the drop-in (SECURITY.md C22). The descriptor's half (the
kinds that may be inline, and the var a port may land in) is repository data, refused as such.
"""
import pytest

from kontroll import authspec, catalog, paths, probe
from kontroll.service import onboard

pytestmark = pytest.mark.unit

# The drop-ins a cliconf and a raw-SSH onboard wrote BEFORE S28, captured from public main 6599484 with the same
# inputs as _plan below. An SSH backend declares no probe_port and its port field writes nothing when blank, so
# both must still come out byte for byte.
_GOLDEN_CLICONF = (
    "core_switch:\n  hosts:\n    sw1:\n      ansible_host: 192.0.2.10\n      device_role: backend_netcommon_cli\n"
    "      ansible_connection: network_cli\n      ansible_network_os: cisco.ios.ios\n      ansible_become: false\n"
    "      ansible_user: '{{ (lookup(''community.sops.sops'', inventory_dir ~ ''/../secrets/network.sops.yml'')"
    " | from_yaml).sw1_username }}'\n"
    "      ansible_password: '{{ (lookup(''community.sops.sops'', inventory_dir ~ ''/../secrets/network.sops.yml'')"
    " | from_yaml).sw1_password }}'\n")
_GOLDEN_RAW_SSH = (
    "linux_hosts:\n  hosts:\n    box1:\n      ansible_host: 192.0.2.61\n      device_role: backend_raw_ssh\n"
    "      ansible_connection: ssh\n"
    "      ansible_user: '{{ (lookup(''community.sops.sops'', inventory_dir ~ ''/../secrets/linux.sops.yml'')"
    " | from_yaml).box1_username }}'\n"
    "      ansible_ssh_private_key_file: '{{ kontroll_device_key_dir }}/box1.key'\n"
    "      kontroll_ssh_key_domain: linux\n")


@pytest.fixture
def canned(tmp_repo, monkeypatch):
    """A cliconf collection (cisco.ios) and an httpapi-only one (acme.rest) installed, each probing as its kind; no
    lab, no install, no write outside tmp_repo."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0", "acme.rest": "1.0.0"})
    monkeypatch.setattr(catalog, "local_shallow", lambda keywords, limit: [])

    def _fake(coll, version=None):
        f = probe._facts(coll, "5.0.0" if coll == "cisco.ios" else "1.0.0", "local", "deep")
        if coll == "cisco.ios":
            f["plugins"], f["modules"] = {"cliconf": ["ios"]}, ["ios_command"]
        else:
            f["plugins"], f["modules"] = {"httpapi": ["rest"]}, ["rest_info"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)
    return tmp_repo


def _cliconf(creds, **kw):
    return onboard.build_onboard_plan("cisco.ios", "cisco_ios", "core_switch", "192.0.2.10", host_name="sw1",
                                      secrets="network", creds=creds, vectors=[], **kw)


def _raw_ssh(creds):
    return onboard.build_onboard_plan("ansible.builtin", "ssh_host", "linux_hosts", "192.0.2.61", host_name="box1",
                                      secrets="linux", backend="raw_ssh", creds=creds, vectors=[])


def _api(creds=None, **kw):
    return onboard.build_onboard_plan("acme.rest", "acme_rest", "services", "192.0.2.80", host_name="rest1",
                                      secrets="network", creds=creds or {"api_token": "t"}, vectors=[], **kw)


def _with(name, **changes):
    """The shipped backend registry with `name`'s entry changed — a synthetic descriptor, the real rest."""
    out = []
    for b in catalog.load_backends():
        if b.get("name") == name:
            b = {**b, **changes}
        out.append(b)
    return out


def test_api_onboard_writes_the_backend_probe_port_as_an_int(canned):
    """An `api` onboard's drop-in carries `kontroll_probe_port: 443` — an int, unquoted in the YAML — from the
    backend's `probe_port`, placed after the connection vars. Guards a REST device being probed on the SSH port: it
    read UNREACHABLE and its class check never ran (L12 F-5)."""
    plan = _api()
    assert plan["error"] is None and plan["backend"] == "api"
    host = plan["host_block"]["services"]["hosts"]["rest1"]
    assert host["kontroll_probe_port"] == 443 and type(host["kontroll_probe_port"]) is int
    assert "      ansible_connection: local\n      kontroll_probe_port: 443\n" in plan["host_text"]
    assert "ansible_port" not in host                    # the API's port is the recipe's, never an operator's box


@pytest.mark.parametrize("creds", [{}, {"port": ""}], ids=["no-port-field", "a-blank-port"])
def test_ssh_onboards_without_a_port_are_byte_identical(canned, creds):
    """A cliconf and a raw-SSH onboard with no port typed — absent, or the blank string an API caller may send —
    write exactly the drop-in they wrote before S28 (goldens captured from public main). Guards new bytes in every
    SSH host's drop-in, and a blank port being refused or written where the form's label promises "blank = 22"
    (MF-02)."""
    assert _cliconf({"username": "admin", "password": "pw", **creds})["host_text"] == _GOLDEN_CLICONF
    assert _raw_ssh({"username": "ops", "ssh_private_key": "K\n", **creds})["host_text"] == _GOLDEN_RAW_SSH


@pytest.mark.parametrize("build", [_cliconf, _raw_ssh], ids=["netcommon_cli", "raw_ssh"])
def test_ssh_onboard_with_a_port_writes_ansible_port_as_an_int(canned, build):
    """`creds={"port": "2222"}` writes `ansible_port: 2222` — an int, unquoted — and stores nothing in SOPS for it
    (a port is not a secret). Guards the port being written as the request's text (which Ansible would template) or
    sent to the secrets domain, and the typed port being ignored as it was before S28 (no field declared it)."""
    host_name = "sw1" if build is _cliconf else "box1"
    plan = build({"username": "admin", "port": "2222"})
    host = next(iter(plan["host_block"].values()))["hosts"][host_name]
    assert host["ansible_port"] == 2222 and type(host["ansible_port"]) is int
    assert "      ansible_port: 2222\n" in plan["host_text"]
    assert not [k for k in plan["creds_to_set"] if k.endswith("_port")]


@pytest.mark.parametrize("value", [
    "{{ lookup('pipe', 'id') }}", "22; id", "0", "65536", "99999", "-1", "+22", " 22", "22 ", "22\n", "0x16",
    "22.0", "1e3", "２２", "٢٢", "port", "22:22",
], ids=["jinja", "shell", "zero", "65536", "five-nines", "negative", "plus", "leading-space", "trailing-space",
        "trailing-newline", "hex", "decimal", "exponent", "full-width-digits", "arabic-indic-digits", "a-word",
        "two-ports"])
def test_inline_port_refuses_anything_but_a_port(canned, value):
    """A typed port that is not one to five ASCII digits naming 1..65535 is refused as a request field: a
    RequestBoundaryError on `port` (the 422 `invalid_port`), whose message never carries the value, and no plan.
    Guards a request value reaching the inventory as text, where Ansible would template it on the control node —
    the Jinja row is the attack, the newline and the other digit scripts are the `re.match` and `\\d` traps."""
    with pytest.raises(paths.RequestBoundaryError) as e:
        _cliconf({"username": "admin", "port": value})
    assert e.value.field == "port" and e.value.code == "invalid_port"
    assert value.strip() not in str(e.value) or value.strip() == "port"


@pytest.mark.parametrize("entry", [
    {"field": "alias", "kind": "host", "inline": True, "maps_to": "kontroll_alias"},
    {"field": "secret_flag", "kind": "password", "inline": True, "maps_to": "kontroll_flag"},
    {"field": "port", "kind": "port", "inline": True, "maps_to": "ansible_user"},
    {"field": "port", "kind": "port", "inline": True},
    {"field": "port", "kind": "port", "inline": True, "maps_to": "Ansible_Port"},
], ids=["a-kind-with-no-validator", "a-secret-kind", "a-port-aimed-at-a-non-port-var", "no-target",
        "a-target-not-lower-case"])
def test_an_inline_field_the_descriptor_cannot_write_is_repository_data(canned, entry):
    """A backend descriptor that marks a field `inline` of a kind with no validator, or aims an inline port at a var
    that is not a `*_port`, or at no var, refuses the plan as repository data (409 `repository_data`, the file's
    fault), not as the request's. Guards the next inline kind being added without a validator — its value would be
    written as the request's text — and a valid int landing in the wrong var."""
    shipped = next(b for b in catalog.load_backends() if b["name"] == "netcommon_cli")
    auth = [e for e in shipped["auth"] if e.get("field") != entry["field"]] + [entry]   # replaces the shipped port
    backends = _with("netcommon_cli", auth=auth)
    with pytest.raises(paths.RepositoryDataError):
        _cliconf({"username": "admin", entry["field"]: "2222"}, backends=backends)


@pytest.mark.parametrize("probe_port", ["443", "{{ api_port }}", 0, 65536, -1, True, 443.0],
                         ids=["quoted", "templated", "zero", "too-high", "negative", "a-bool", "a-float"])
def test_a_probe_port_that_is_not_a_yaml_integer_port_refuses_the_plan(canned, probe_port):
    """A backend `probe_port` that is not a YAML integer in 1..65535 refuses every plan on that backend as
    repository data. Guards a quoted or templated value in a descriptor reaching every host's drop-in, where Ansible
    would template it, and a bool (an int in Python) passing as port 1."""
    with pytest.raises(paths.RepositoryDataError):
        _api(backends=_with("api", probe_port=probe_port))


def test_port_number_is_one_to_five_ascii_digits_naming_a_port():
    """authspec.port_number, the one parser both halves use: an int or a digit string naming 1..65535 is that int;
    a bool, a float, a sign, a space, a newline or a non-ASCII digit is None. Guards the C22 `re.match` trap
    ("22\\n" accepted) and `str.isdigit` / `\\d` accepting other scripts' digits."""
    assert [authspec.port_number(v) for v in (22, "22", "65535", "00022", 65535)] == [22, 22, 65535, 22, 65535]
    for v in (True, False, 0, 65536, "0", "65536", "22\n", "２２", "+22", 22.0, None, [22], ""):
        assert authspec.port_number(v) is None, v


def test_inline_bool_is_one_of_two_exact_words():
    """An inline `bool` writes True or False, from a bool or exactly "true" / "false"; "True", "yes", "1" and the
    rest are a 422 on the field. Guards the second validated kind accepting a word Ansible or YAML would read some
    other way."""
    cf = {"field": "verify", "kind": "bool", "inline": True, "maps_to": "kontroll_verify"}
    assert [authspec.inline_value(cf, v) for v in ("true", "false", True, False)] == [True, False, True, False]
    for v in ("True", "yes", "1", "on", " true", "true\n", 1, None):
        with pytest.raises(paths.RequestBoundaryError):
            authspec.inline_value(cf, v)
