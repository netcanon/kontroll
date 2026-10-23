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
import os
import re

import pytest
import yaml

from kontroll import authspec, catalog, paths, probe
from kontroll.service import onboard, scaffold

pytestmark = pytest.mark.unit

# Every shape of inventory host name the request boundary accepts (paths.inventory_host), with its credential key —
# the last the longest (253 characters: its `<cred key>_api_token` is 263, inside catalog.MAPS_TO_RE's bound).
LONG_NAME = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
HOST_NAMES = ["sw1", "SW-Core-1", "sw.lab.example", "192.0.2.9", "2001:db8::9", LONG_NAME]
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


@pytest.mark.parametrize("backend", [b["name"] for b in catalog.load_backends() if catalog.backend_role(b)])
def test_every_runnable_shipped_backend_stores_in_network_by_default(probed, backend):
    """With no domain in the request, a class of every shipped backend that declares a role stores its credentials
    in `network` — the template's operational domain, whose `.sops.yaml` rule carries the runner's key. The backends
    are read from the registry, so one added later is covered. Guards the default drifting per backend in the
    move."""
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


# --- the defaults as registry data (S27): what the oracle above now runs on ------------------------------------- #
REGISTRY = catalog.load_backends()
CODE_SITES = ["scripts/kontroll/service/onboard.py", "scripts/kontroll/service/scaffold.py",
              "scripts/kontroll/authspec.py", "api/routes/onboard.py", "gui/app.py", "gui/templates/index.html",
              "scripts/galaxy.py"]


def _lab(**kw):
    """A backend descriptor the test owns: a role, an SSH password, and whatever the row adds."""
    b = {"name": "lab_cli", "role": "backend_netcommon_cli", "requires": [],
         "connection": {"ansible_connection": "network_cli"},
         "auth": [{"field": "password", "kind": "password", "maps_to": "ansible_password"}]}
    b.update(kw)
    return b


def _lab_plan(bdef, **kw):
    kw.setdefault("creds", {"password": "p"})
    return _plan("h1", backend=bdef["name"], backends=[bdef], **kw)


def _curated(tmp_repo, **fields):
    """A curated `edgeos` class already in the repository, declaring `acme.edgeos` — what a second device reuses."""
    target = tmp_repo / "modules" / "edgeos"
    target.mkdir(parents=True)
    (target / "module.yml").write_text(yaml.safe_dump(dict({
        "key": "edgeos", "collections": [{"name": "acme.edgeos", "version": ">=1.0.0"}],
        "inventory_group": "edge_router"}, **fields)), encoding="utf-8")


def test_every_derived_param_is_a_required_param_and_renders():
    """Each shipped backend's `derive_params` names only params its `requires:` lists, and each template renders,
    with the planner's closed context, to a value in the param charset. Guards a typo'd placeholder or a param no
    class reads shipping in the registry — the planner would refuse every onboard of that backend."""
    ctx = {"collection": "acme.edgeos", "namespace": "acme", "name": "edgeos"}
    derived = [b for b in REGISTRY if b.get("derive_params") is not None]
    assert derived, "netcommon_cli derives network_os — the registry must still say so"
    for b in derived:
        assert set(b["derive_params"]) <= set(b.get("requires") or []), b["name"]
        for tmpl in b["derive_params"].values():
            assert catalog.PARAM_RE.fullmatch(catalog.render_template(tmpl, ctx, catalog.PARAM_RE))


def test_every_shipped_backend_names_a_default_domain():
    """Every shipped backend declares `default_secrets_domain`, as a valid name. Guards a backend shipped without
    one: its onboards would be refused until the request names a domain."""
    assert REGISTRY, "no shipped backend was read"
    for b in REGISTRY:
        assert onboard.default_secrets_domain(b, b["name"]), b["name"]


@pytest.mark.parametrize("host_name", HOST_NAMES)
def test_every_templated_maps_to_renders_for_every_host_name_shape(host_name):
    """Every `maps_to` template the registry or the generic union ships renders, for the credential key of each
    shape of host name the boundary accepts, to `<cred key>_api_token`. Guards MF-03: an output rule narrower than
    the host names would refuse an onboard that works today."""
    ck = host_name.replace("-", "_")
    templates = [e["maps_to"] for b in REGISTRY for e in (b.get("auth") or []) if "{" in str(e.get("maps_to"))]
    templates.append(authspec.union_field("api_token")["maps_to"])
    assert len(templates) == 2, "the api backend's token and the generic union's: a new one needs its own row"
    for tmpl in templates:
        assert catalog.render_template(tmpl, {"cred_key": ck}, catalog.MAPS_TO_RE) == ck + "_api_token"


@pytest.mark.parametrize("tmpl, ctx", [
    ("{host}", {"cred_key": "h1"}),                              # a placeholder the context does not fill
    ("{collection.__class__}", {"collection": "a.b"}),           # attribute access: str.format's door
    ("{{collection}}", {"collection": "a.b"}),                   # a Jinja opener
    ("{collection", {"collection": "a.b"}),                      # a brace left over
    ("collection}", {"collection": "a.b"}),
    ("{% if x %}", {}),
    ("{collection} x", {"collection": "a.b"}),                   # a space in the result
    ("{collection}.IOS", {"collection": "a.b"}),                 # a param is lower case (an FQCN's charset)
    ("{Collection}", {"Collection": "a.b"}),                     # placeholders are lower case
    ("{collection}", {"collection": 1}),                         # a context value that is not text
    (None, {}),                                                  # a template that is not text
    (["{collection}"], {"collection": "a.b"}),
    ("", {}),                                                    # renders to nothing
])
def test_template_renderer_refuses_unknown_placeholders_and_bad_output(tmpl, ctx):
    """catalog.render_template refuses (ValueError) every template outside the closed `{name}` grammar or whose
    result leaves the charset. Guards a descriptor template becoming an expression language — the value lands in a
    module file and in a host var Ansible templates."""
    with pytest.raises(ValueError):
        catalog.render_template(tmpl, ctx, catalog.PARAM_RE)


@pytest.mark.parametrize("tmpl", ["{{collection}}", "{collection", "collection}", "{% if x %}", "{Collection}"])
def test_template_grammar_refuses_a_stray_brace_whatever_the_charset(tmpl):
    """The `{name}` grammar refuses a brace that is not a placeholder even under a charset that would let it
    through. Guards the grammar resting on today's charsets: both refuse a brace, so a template check that relied on
    them alone would pass a Jinja opener the day one charset admits braces."""
    with pytest.raises(ValueError):
        catalog.render_template(tmpl, {"collection": "a.b", "Collection": "a.b"}, re.compile(r".{1,256}\Z"))


def test_template_renderer_fills_each_placeholder():
    """The positive half: each placeholder is replaced, as often as it appears, and the rest is kept as written.
    Guards a renderer that refuses everything passing the refusal rows above."""
    ctx = {"collection": "acme.edgeos", "namespace": "acme", "name": "edgeos"}
    assert catalog.render_template("{namespace}.{name}.{name}_x", ctx, catalog.PARAM_RE) == "acme.edgeos.edgeos_x"
    assert catalog.render_template("plain", ctx, catalog.PARAM_RE) == "plain"


def test_no_code_names_a_secrets_domain():
    """None of the planner, the scaffold, authspec, the routes, the form or the CLI holds the word `network` as a
    quoted literal. Guards an eleventh copy of the default the registry now owns (S27). A tripwire, not a proof: a
    default spelled another way passes it."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel in CODE_SITES:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            text = fh.read()
        assert '"network"' not in text and "'network'" not in text, rel


def test_onboard_without_a_domain_uses_the_backend_default(probed, tmp_repo):
    """With no domain in the request, a new class stores its credentials in the backend's `default_secrets_domain`:
    the plan, the module file and the drop-in's lookup all name it. A request's domain wins over it, and a reused
    class's own `secrets_domain` wins over both, as before. Guards the resolution order and a silent fallback to a
    literal in code."""
    probed(**CLICONF)
    lab = _lab(default_secrets_domain="lab")
    plan = _lab_plan(lab)
    assert plan["secrets"] == "lab" and plan["module"]["secrets_domain"] == "lab"
    assert "/../secrets/lab.sops.yml" in plan["host_text"] and "network.sops.yml" not in plan["host_text"]
    assert _lab_plan(lab, secrets="ops")["secrets"] == "ops"
    _curated(tmp_repo, role="backend_netcommon_cli", backend="lab_cli", secrets_domain="curated")
    assert _lab_plan(lab, secrets="ops")["secrets"] == "curated"
    assert _lab_plan(lab)["secrets"] == "curated"


def test_a_backend_with_no_default_domain_requires_one(probed):
    """A backend that declares no `default_secrets_domain` plans only when the request names a domain; without one
    the plan is refused as the `secrets domain` field (422, `invalid_secrets_domain`), never filled from code. Guards
    the former `network` fallback coming back for a backend an operator adds."""
    probed(**CLICONF)
    with pytest.raises(paths.RequestBoundaryError) as refused:
        _lab_plan(_lab())
    assert refused.value.code == "invalid_secrets_domain" and "required" in str(refused.value)
    assert _lab_plan(_lab(), secrets="ops")["secrets"] == "ops"


def test_the_scaffold_takes_the_backend_default_domain(probed):
    """The scaffold, given no domain, writes the backend's default into the class it scaffolds, and refuses for a
    backend that declares none. Guards the scaffold's own copy of the fallback."""
    probed(**CLICONF)
    res = scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", backend="lab_cli",
                                  backends=[_lab(default_secrets_domain="lab")])
    assert res["error"] is None and res["module"]["secrets_domain"] == "lab"
    assert scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router")["module"]["secrets_domain"] == "network"
    with pytest.raises(paths.RequestBoundaryError):
        scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", backend="lab_cli", backends=[_lab()])


@pytest.mark.parametrize("change", [
    {"default_secrets_domain": "../x"},                                                     # not a path component
    {"default_secrets_domain": True},                                                       # not text
    {"default_secrets_domain": "null"},                                                     # YAML reads it back as None
    {"requires": ["network_os"], "derive_params": {"network_os": "{collection}.{nmae}"}},   # a typo'd placeholder
    {"requires": ["network_os"], "derive_params": {"network_os": "{{ collection }}"}},      # Jinja
    {"requires": ["network_os"], "derive_params": ["network_os"]},                          # not a map
    {"requires": ["a b"], "derive_params": {"a b": "{name}"}},                              # a param name that is not one
    {"requires": ["network_os"],                                                            # …and one it does not require
     "derive_params": {"network_os": "{collection}.{name}", "bad-name": "{collection}"}},
    {"auth": [{"field": "password", "kind": "password", "maps_to": "{cred_key}-pw"}]},      # renders to no var name
    {"auth": [{"field": "password", "kind": "password", "maps_to": "{host}_pw"}]},          # a placeholder it lacks
], ids=["domain-path", "domain-bool", "domain-null", "param-typo", "param-jinja", "param-list", "param-name",
        "param-name-unrequired", "maps-to-dash", "maps-to-unknown"])
def test_a_registry_default_that_does_not_render_is_repository_data(probed, change):
    """A default domain that is not a valid name, a `derive_params` template that does not render or names a param
    that is not an identifier (one the backend requires or not), and a `maps_to` template that does not render to a
    variable name are the registry's fault: RepositoryDataError (409 `repository_data`), never a 422 blaming the
    request and never a written var. Guards a descriptor typo reaching the module file or the drop-in (a param's name
    is a key of the module file's `backend_params`, matched against `connection:` holes); the unrequired row guards
    the name check moving behind the `requires` filter (mutant M4 of T18's fold read), where the CHANGELOG says every
    name is held."""
    probed(**CLICONF)
    bdef = _lab(**dict({"default_secrets_domain": "lab"}, **change))
    with pytest.raises(paths.RepositoryDataError):
        _lab_plan(bdef)


def test_a_required_param_the_backend_does_not_derive_is_left_out(probed):
    """A param the backend requires but does not derive, and a derived param it does not require, are not written —
    the class's `backend_params` holds only what is both. Guards the move widening what a new class is written
    with."""
    probed(**CLICONF)
    plan = _lab_plan(_lab(default_secrets_domain="lab", requires=["network_os", "other"],
                          derive_params={"network_os": "{namespace}_{name}", "extra": "{name}"}))
    assert plan["module"]["backend_params"] == {"network_os": "acme_edgeos"}


def test_an_api_token_names_its_own_var_when_its_descriptor_does(probed, tmp_repo):
    """A class whose own `auth:` block gives `api_token` a literal `maps_to` has the token written under that var.
    The former branch on the field's name wrote `<cred key>_api_token` whatever the descriptor said; a declared
    var now holds. Pins a deliberate difference this change introduces (CHANGELOG [Unreleased]; brief 14 §5.5
    does not list it), which no shipped descriptor reaches."""
    probed(**HTTPAPI)
    _curated(tmp_repo, backend="api", secrets_domain="network",
             auth=[{"field": "api_token", "kind": "token", "maps_to": "edgeos_token"}])
    plan = _plan("h1", creds={"api_token": "t"})
    assert ("edgeos_token", _former_lookup("h1_api_token")) in _host_vars(plan, "h1")
    assert "h1_api_token" not in dict(_host_vars(plan, "h1"))


def test_an_api_token_with_no_var_on_a_backend_without_the_template_is_stored_only(probed, tmp_repo):
    """On a backend whose registry entry has no `api_token` template, a class's own `api_token` with no `maps_to` is
    stored in SOPS and given no host var — what any other field with no `maps_to` gets. Pins the second deliberate
    difference brief 14 §5.5 accepts: no shipped module declares such a field."""
    probed(**CLICONF)
    _curated(tmp_repo, backend="netcommon_cli", secrets_domain="network",
             auth=[{"field": "api_token", "kind": "token"}])
    plan = _plan("h1", creds={"api_token": "t"})
    assert plan["creds_to_set"] == {"h1_api_token": "t"}
    assert "h1_api_token" not in dict(_host_vars(plan, "h1"))


@pytest.mark.parametrize("maps_to", ["{cred_key}", "ansible_{cred_key}", "{cred_key}_password",
                                     "{cred_key}_api_token"])
def test_a_module_files_auth_entry_may_not_hold_a_template(probed, tmp_repo, maps_to):
    """A curated class whose own `auth:` entry gives `maps_to` a template its backend does not declare for that field
    is refused as repository data saying so, before any var is written — `api_token`'s own template too, on the
    `password` field; the same entry repeating the backend's own template for `api_token` plans as the backend's
    would. (`{cred_key}_api_token` on `password` and the refusal's wording are T18's fold-2 read, N3: X1, X3.)

    Guards the request's host name choosing WHICH host var a lookup lands in (T18's read, F1): rendered, such a
    template put a SOPS lookup of a value the request supplied under `ansible_python_interpreter` or
    `ansible_connection` for a host named for it. Only the registry and the generic union declare a template."""
    probed(**HTTPAPI)
    _curated(tmp_repo, backend="api", secrets_domain="network",
             auth=[{"field": "password", "kind": "password", "maps_to": maps_to}])
    with pytest.raises(paths.RepositoryDataError) as refused:
        _plan("h1", creds={"password": "p"})
    assert "template" in str(refused.value) and "does not declare" in str(refused.value)
    (tmp_repo / "modules" / "edgeos" / "module.yml").unlink()
    (tmp_repo / "modules" / "edgeos").rmdir()
    _curated(tmp_repo, backend="api", secrets_domain="network",
             auth=[{"field": "api_token", "kind": "token", "maps_to": "{cred_key}_api_token"}])
    plan = _plan("h1", creds={"api_token": "t"})
    assert ("h1_api_token", _former_lookup("h1_api_token")) in _host_vars(plan, "h1")


def test_a_required_connection_param_the_backend_does_not_derive_is_repository_data(probed):
    """A backend whose `connection:` block reads a param it requires and does not derive (an operator's copy of
    netcommon_cli written before S27, with no `derive_params`) is refused as repository data naming the param; the
    same backend deriving it plans with the param rendered into the host var. The refusal names the connection
    var, and the edits that clear it for a value set at run time: dropping the param from `requires:`, or moving the
    var out of the block.

    Guards an unrendered `{{ network_os }}` reaching the drop-in (T18's read, F12): unless a group var happened to
    define it, the plan succeeded and the first play failed far from the cause. The remedy is pinned because, as
    first worded, it named only the param and left the plan refused when followed (fold-2 read, S2)."""
    probed(**CLICONF)
    copy = _lab(default_secrets_domain="lab", requires=["network_os"],
                connection={"ansible_connection": "network_cli", "ansible_network_os": "{{ network_os }}"})
    with pytest.raises(paths.RepositoryDataError) as refused:
        _lab_plan(copy)
    assert "network_os" in str(refused.value) and "derive_params" in str(refused.value)
    assert "requires:" in str(refused.value) and "ansible_network_os" in str(refused.value)
    assert "out of its connection block" in str(refused.value)
    plan = _lab_plan(dict(copy, derive_params={"network_os": "{collection}.{name}"}))
    assert dict(_host_vars(plan, "h1"))["ansible_network_os"] == "acme.edgeos.edgeos"


_THROUGH = ["{{ network_os | lower }}", "{{network_os|lower}}", "{{ network_os }}_x", "{{ 'acme.' ~ network_os }}",
            "x{{ network_os }}", "{{- network_os -}}", "{{ y }}{{ network_os }}", "{{ (network_os) }}",
            "{{ x | default(network_os) }}", "{% if true %}{{ network_os }}{% endif %}",
            "{% if network_os %}cisco.ios.ios{% endif %}", "{{ hostvars[inventory_hostname]['network_os'] }}",
            ["{{ network_os }}"], {"a": "{{ network_os }}"}, {"{{ network_os }}": 1}, [["{{ network_os }}"]],
            {"a": {"b": "{{ network_os }}"}}, {"{{ network_os }}"}, "{{ x |\n network_os }}", "{{ network_os",
            "{{ network_os }", "{{ '}}' ~ network_os }}",
            "#jinja2:variable_start_string:'[[',variable_end_string:']]'\n[[ network_os ]]"]
_THROUGH_IDS = ["filter", "filter-unspaced", "text-after", "concat", "text-before", "trim-markers", "second-expression",
                "parenthesised", "default-arg", "statement", "statement-only", "hostvars", "list", "map", "map-key",
                "nested-list", "nested-map", "set", "multi-line", "unclosed", "single-close", "literal-close",
                "override-header"]


@pytest.mark.parametrize("value", _THROUGH, ids=_THROUGH_IDS)
@pytest.mark.parametrize("derive", [None, {"network_os": "{collection}.{name}"}], ids=["underived", "derived"])
def test_a_required_connection_param_read_through_an_expression_is_repository_data(probed, derive, value):
    """A backend whose `connection:` block names a required param anywhere in a templated value but as a bare
    `{{ network_os }}` — a filter (spaced or not), text beside it, an expression it is not first in, whitespace
    control, a `{% %}` block (alone too), a `hostvars` lookup, a line break inside the braces, an unclosed or
    single-closed `{{`, a `}}` inside a string literal, a `#jinja2:` header that changes the delimiters, or a list, a
    set or a map (a key too) at any depth — is refused as repository data naming the param and the bare form, whether
    the backend derives it or not; for an underived param the refusal also names `derive_params` and the run-time
    edit (drop it from `requires:`).

    Guards the forms escaping F12's check: only a bare `{{ param }}` is filled from the derived params, so the
    drop-in would hold the expression unrendered and the first play would fail (T18's fold read, N5). The check
    first read only a value's first name, from a value that began with `{{`, and ten of these shapes passed it
    (fold-2 read, S1; its mutants X4 and X5 survived every suite); then it read whole `{{ }}`/`{% %}` blocks and
    walked lists and maps but not a set or a tuple, and the unclosed, single-close, literal-close, header and set
    shapes passed (fold-3 read, S1). The map-key, nested and multi-line rows pin a walk that its mutants Y1-Y4
    narrowed unseen (its N2). The bare form is asserted because the refusal is the author's only route to the fix
    (fold-4 read, N5: its mutant Z11 dropped it unseen)."""
    probed(**CLICONF)
    bdef = _lab(default_secrets_domain="lab", requires=["network_os"],
                connection={"ansible_connection": "network_cli", "ansible_network_os": value})
    if derive:
        bdef["derive_params"] = derive
    with pytest.raises(paths.RepositoryDataError) as refused:
        _lab_plan(bdef)
    assert "network_os" in str(refused.value) and "templated value" in str(refused.value)
    assert "whole value `{{ network_os }}`" in str(refused.value)
    assert ("derive_params" in str(refused.value)) == (derive is None)
    assert ("value set at run time" in str(refused.value)) == (derive is None)


@pytest.mark.parametrize("value", ["{{ network_os }}", "{{network_os}}", "  {{ network_os }}  ", "{{  network_os  }}",
                                   "{{\tnetwork_os\t}}"], ids=["spaced", "unspaced", "padded", "wide", "tabbed"])
def test_a_bare_required_connection_param_is_filled_in_any_spacing(probed, value):
    """A derived required param read bare, whatever the spacing inside or around its braces (a tab too), plans with
    the derived value as the host var.

    Guards the bare form's match narrowing: the expression rule refuses any read of a required param but the bare
    one, so a bare form it failed to recognise would refuse netcommon_cli's every onboard as repository data (T18's
    fold-4 read, N5: its mutant Z5, spaces only, survived every suite)."""
    probed(**CLICONF)
    plan = _lab_plan(_lab(default_secrets_domain="lab", requires=["network_os"],
                          derive_params={"network_os": "{collection}.{name}"},
                          connection={"ansible_connection": "network_cli", "ansible_network_os": value}))
    assert dict(_host_vars(plan, "h1"))["ansible_network_os"] == "acme.edgeos.edgeos"


@pytest.mark.parametrize("requires", ["network_os", ["network_os", 5], {"network_os": 1}, ["r\u00e9seau"], False, "",
                                      [True], [None], ["a" * 65]],
                         ids=["string", "non-string-entry", "map", "non-identifier-entry", "false", "empty-string",
                              "bool-entry", "null-entry", "len65"])
def test_a_requires_that_is_not_a_list_of_names_is_repository_data(probed, requires):
    """A backend whose `requires:` is not a list of param names — a string (empty too), `false`, a map, an entry that
    is not a string (a YAML `true` or `~` too) or not an identifier, or one longer than the 64 characters a
    `derive_params` key may hold — is refused as repository data naming `requires`, by the planner and by the
    scaffold's `backend_params` alike; only an absent or `null` one, or `[]`, means none.

    Guards a YAML string turning every `param in requires` test into a substring test (T18's fold-2 read, N4):
    `requires: network_os` refused a backend reading `{{ os }}` as if it required `os`, and would derive any param
    whose name is a piece of the string. An entry that is not an identifier was accepted and could never be derived
    or read bare (fold-3 read, S1), and a falsy `requires:` read as none would hide the same fault (its mutant Y7).
    The bool, null and long entries are rows because the name rule applied to the entry's text, or with no length
    bound, let each by unseen (fold-4 read, N5: its mutants Z7 and Z6)."""
    probed(**CLICONF)
    bdef = _lab(default_secrets_domain="lab", requires=requires, derive_params={"network_os": "{collection}.{name}"},
                connection={"ansible_connection": "network_cli", "ansible_network_os": "{{ os }}"})
    with pytest.raises(paths.RepositoryDataError) as refused:
        _lab_plan(bdef)
    assert "requires" in str(refused.value)
    with pytest.raises(paths.RepositoryDataError):
        onboard.backend_params(bdef, "acme.edgeos", "lab_cli")


@pytest.mark.parametrize("value", ["{{ site_os }}", "{{ site_os | lower }}"], ids=["bare", "filter"])
def test_a_connection_param_the_backend_does_not_require_is_left_for_run_time(probed, value):
    """A backend whose `connection:` block reads a param it does not require, bare or through a filter, plans, the
    value left as written for a group var to define at run time; only a required param must be derived and read bare.

    Guards F12's refusal widening to every `{{ … }}` in a `connection:` block (mutant M3 of T18's fold read), or the
    expression rule to every templated value (mutant Y13 of its fold-3 read): a backend that reads a site's own group
    var through its connection block would then be refused."""
    probed(**CLICONF)
    plan = _lab_plan(_lab(default_secrets_domain="lab", requires=[],
                          connection={"ansible_connection": "network_cli", "ansible_network_os": value}))
    assert dict(_host_vars(plan, "h1"))["ansible_network_os"] == value


@pytest.mark.parametrize("value", ["network_os", "{# network_os #}", "x #jinja2: network_os"],
                         ids=["plain", "comment-only", "header-not-first"])
@pytest.mark.parametrize("derive", [None, {"network_os": "{collection}.{name}"}], ids=["underived", "derived"])
def test_a_connection_value_that_reads_no_var_plans_as_written(probed, derive, value):
    """A backend whose `connection:` value names a required param but reads no var — plain text, a Jinja comment
    alone, a `#jinja2:` that does not open the value — plans with the value as written, whether the backend derives
    the param or not.

    Guards the expression rule widening past what ansible-core templates: every string scanned, the comment marker
    read as a template opener, or the override header matched anywhere would refuse such a backend as repository
    data (T18's fold-4 read, N5: its mutants Z1, Z2 and Z3 survived every suite)."""
    probed(**CLICONF)
    bdef = _lab(default_secrets_domain="lab", requires=["network_os"],
                connection={"ansible_connection": "network_cli", "ansible_network_os": value})
    if derive:
        bdef["derive_params"] = derive
    assert dict(_host_vars(_lab_plan(bdef), "h1"))["ansible_network_os"] == value


@pytest.mark.parametrize("looped", ["v: &a [*a]", "v: &a [[*a]]", "v: &a {k: *a}", "v: [x, &a [*a]]"],
                         ids=["self-list", "two-level", "self-map", "below-root"])
@pytest.mark.parametrize("requires", [["network_os"], []], ids=["required", "not-required"])
def test_a_connection_value_that_holds_itself_is_repository_data(probed, requires, looped):
    """A backend whose `connection:` value holds itself (a recursive YAML alias: a container met again anywhere on
    its own path — one or two levels down, through a map, or below the root) is refused as repository data naming
    the connection var, whether the backend requires a param or not; a value that holds one list twice (an alias
    used twice, no cycle) plans.

    Guards the planner's template walk recursing until Python stops it: the API answered 500 where a malformed
    registry answers 409, for a backend whose params the rule never refuses too (T18's fold-4 read, N7). The
    two-level, map and below-root rows are rows because a walk that remembered only the parent, checked lists
    only, or checked the root only still caught `&a [*a]` (its fold-5 read, N3: mutants W1, W2 and W3)."""
    probed(**CLICONF)
    looped = yaml.safe_load(looped)["v"]
    bdef = _lab(default_secrets_domain="lab", requires=requires,
                connection={"ansible_connection": "network_cli", "ansible_network_os": looped})
    with pytest.raises(paths.RepositoryDataError) as refused:
        _lab_plan(bdef)
    assert "ansible_network_os" in str(refused.value) and "refers to itself" in str(refused.value)
    twice = yaml.safe_load("v: [&a [x], *a]")["v"]
    plan = _lab_plan(dict(bdef, connection={"ansible_connection": "network_cli", "ansible_network_os": twice}))
    assert dict(_host_vars(plan, "h1"))["ansible_network_os"] == [["x"], ["x"]]


def test_a_param_name_of_64_characters_is_a_param_name(probed):
    """A backend that requires and derives a param whose name is 64 characters long, the most a `derive_params`
    key may hold, plans with the derived value in the connection var that reads it bare.

    Guards the `requires:` name rule drifting one shorter than the `derive_params` key rule: only a 65-character
    entry was pinned (refused), so a cap of 63 made such a param un-requirable, unseen (T18's fold-5 read, N3:
    its mutant R1 survived every suite)."""
    probed(**CLICONF)
    name = "a" * 64
    plan = _lab_plan(_lab(default_secrets_domain="lab", requires=[name],
                          derive_params={name: "{collection}.{name}"},
                          connection={"ansible_connection": "network_cli", "ansible_network_os": "{{ " + name + " }}"}))
    assert dict(_host_vars(plan, "h1"))["ansible_network_os"] == "acme.edgeos.edgeos"


def test_a_legacy_api_token_on_a_backend_without_the_template_gets_the_unions_var(probed):
    """An onboard of a cliconf class that passes an `api_token` (a legacy caller's field the backend does not
    declare) writes the token's lookup under `<cred key>_api_token`, the generic union's template — what the former
    code wrote on any backend.

    Guards the union arm of the module-file template check (T18's fold read, N1, mutant M1): checking a template
    against the backend registry alone refuses every such onboard as repository data."""
    probed(**CLICONF)
    plan = _plan("sw1", creds={"username": "u", "api_token": "t"})
    assert ("sw1_api_token", _former_lookup("sw1_api_token")) in _host_vars(plan, "sw1")


def test_a_module_file_may_repeat_its_backends_own_template(probed, tmp_repo):
    """A curated class whose own `auth:` entry repeats the template its backend's registry entry declares for that
    field (an operator's registry, `password` -> `{cred_key}_pw`) plans, the lookup of the host's password written
    under the rendered var.

    Guards the registry arm of the module-file template check (T18's fold read, N1, mutant M2): checking a template
    against the generic union alone refuses a class that repeats its own backend's template."""
    probed(**CLICONF)
    field = {"field": "password", "kind": "password", "maps_to": "{cred_key}_pw"}
    bdef = _lab(default_secrets_domain="lab", auth=[field])
    _curated(tmp_repo, backend="lab_cli", secrets_domain="lab", auth=[field])
    plan = _plan("h1", backends=[bdef], creds={"password": "p"})
    assert ("h1_pw", _former_lookup("h1_password", "lab")) in _host_vars(plan, "h1")


def test_a_backend_this_install_does_not_ship_is_named_as_such_in_the_refusal():
    """With no domain from the request or a class, a backend this install does not ship (no registry entry) is
    refused as the `secrets domain` field with a message saying so, not one blaming a registry entry that does not
    exist. Guards the misattributed refusal T18's read found (F5): the fault is the class's module file."""
    with pytest.raises(paths.RequestBoundaryError) as refused:
        onboard._secrets_domain(None, {}, "nope")
    assert "not one this install ships" in str(refused.value)
    with pytest.raises(paths.RequestBoundaryError) as refused:
        onboard._secrets_domain(None, {"name": "lab_cli"}, "lab_cli")
    assert "registry entry declares no default" in str(refused.value)


def test_only_a_templated_backend_maps_to_is_a_default_by_field_name(probed, monkeypatch):
    """A TIER-B field that brings no `maps_to` and is named like a backend `auth:` entry whose `maps_to` is a LITERAL
    (`password` → `ansible_password` on the api backend) is stored in SOPS with no host var; only a templated entry
    (`api_token`) is a default by name.

    Guards the by-name default widening from the registry's templates to every literal it declares (mutant A of
    T18's read, D14): a field read from a module's argument spec would then reach a connection var."""
    probed(**HTTPAPI)
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.edgeos": "1.0.0"})
    monkeypatch.setattr(authspec, "auth_fields_from_argspec", lambda coll, modules, **kw: [
        {"field": "password", "kind": "password", "label": "password", "sops_stem": "password", "secret": True,
         "required": True, "derivation": "no_log", "auth_set": "edgeos"}])
    plan = _plan("h1", creds={"password": "p"}, deep=True)
    assert plan["cred_source"] == "deep"
    assert plan["creds_to_set"] == {"h1_password": "p"}
    assert "ansible_password" not in dict(_host_vars(plan, "h1"))
