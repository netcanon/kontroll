"""The request boundary: every request field that becomes a path component or an inventory key is a closed charset,
and every repository write is confined to write_root() (SECURITY.md C22; 2026-10-08 review, finding 1).

WHY: the onboard planner validated only that the collection was installed. `key`, `group`, `host`, `host_name` and
`secrets` were bare strings that went straight into `modules/<key>/module.yml`, `onboarded-<key>.yml`,
`ansible/secrets/<domain>.sops.yml` and the rendered inventory YAML — one `../` or `;` away from a write outside the
repository or a YAML/argv injection, on the privileged API and the GUI alike. CodeQL's 45 `py/path-injection` alerts
on the public tree are this single class of join. The fix is a closed charset at the seam AND at every join (so a new
caller cannot forget), plus `confined()` as the last check before any write. These tests are the accept/reject
tables, the "refused before the probe is reached" property, and the write confinement against real paths.
"""
import os

import pytest

from kontroll import catalog, gitio, paths, probe
from kontroll.service import actuation, backup, capability, hostvars, identity, logsvc, observe, onboard, refresh

pytestmark = pytest.mark.unit


def _load_script(modname, filename):
    """Load a hyphenated scripts/<filename> by path (not importable as a module name)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(modname, os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("value", ["cisco_ios", "edge-fw", "a", "x9", "a" * 64, "docker_host"])
def test_component_accepts_the_names_kontroll_actually_uses(value):
    """Device-class keys, unit keys, inventory groups and secret domains as they exist in the tree — lowercase, digits,
    `_`, `-`, up to 64 chars — pass unchanged. Guards the validator growing stricter than the data it protects."""
    assert paths.component(value) == value


@pytest.mark.parametrize("value", ["../x", "a/b", "a\\b", ".", "..", "", "A", "Edge", "a b", "a;b", "a\nb", "a\x00b",
                                   "x.y", "-a", "_a", "a" * 65, "cisco_ios\n", None, 7, ["a"]])
def test_component_rejects_separators_dots_metacharacters_case_and_length(value):
    """A separator, a dot (so no `..`), a NUL, a newline, a shell metacharacter, an uppercase letter, a leading `-`/`_`,
    a 65th char, an empty string or a non-string are all refused with the FIELD named. So is a valid name followed by
    ONE newline: `$` matches before a final newline, so an anchored pattern used with `match()` said yes to
    `cisco_ios\n` — the validator is applied with `fullmatch()`. Each is a path or YAML/argv hazard the old
    bare-string planner would have joined."""
    with pytest.raises(ValueError) as e:
        paths.component(value, "device-class key")
    assert "device-class key" in str(e.value)


@pytest.mark.parametrize("value", ["cisco.ios", "acme.edgeos", "community.docker", "a.b", "n_s.na_me"])
def test_collection_fqcn_accepts_namespace_dot_name(value):
    """Exactly `namespace.name` in Galaxy's charset passes — the form every `ansible_collections/<ns>/<name>` join
    and every `ansible-doc`/`ansible-galaxy` argv word expects."""
    assert paths.collection_fqcn(value) == value


@pytest.mark.parametrize("value", ["cisco", "cisco.ios.extra", "../acme.edgeos", "acme/edgeos", "Cisco.IOS", "a..b",
                                   ".ios", "cisco.", "a b.c", "", None, "cisco.ios\n", "--version", "-M..",
                                   "--playbook-dir=.."])
def test_collection_fqcn_rejects_anything_else(value):
    """One part, three parts, a path, uppercase, an empty label, a space, a non-string, a valid name followed by a
    newline or an option-shaped word are refused. The probe walks `base_path/<ns>/<name>` and hands the name to
    ansible-doc, so this is both a path and an argv guard."""
    with pytest.raises(ValueError):
        paths.collection_fqcn(value)


@pytest.mark.parametrize("value", ["192.0.2.10", "198.51.100.7", "2001:db8::1", "sw-core-1", "sw.lab.example",
                                   "edgeos-1", "a", "host.", "x" * 63])
def test_hostname_accepts_addresses_and_rfc1123_names(value):
    """IPv4/IPv6 literals and RFC-1123 hostnames (labels of letters/digits/hyphens joined by single dots) pass — the
    values onboard actually receives for `host` and `host_name`."""
    assert paths.hostname(value) == value


@pytest.mark.parametrize("value", ["../x", "a b", "a..b", "-a", "a-", "a/b", "", "192.0.2.50; rm -rf /", "a\nb",
                                   "x" * 64, ("a" * 63 + ".") * 4 + "b", "a_b", None, "sw-1\n", "192.0.2.9\n",
                                   "2001:db8::1%eth0", "fe80::1%1", "2001:db8::1%x\n  a: b",
                                   "2001:db8::1%}} {{ 7*7 }}"])
def test_hostname_rejects_paths_metacharacters_bad_labels_and_length(value):
    """A `..`, a space, a separator, a leading/trailing hyphen, a label over 63 chars, a name over 253 chars, an
    underscore (not RFC-1123), a newline (also as the LAST character), a shell payload or a non-string are refused —
    and so is an IPv6 literal with a ZONE ID, whatever follows the `%`. The standard library's address parser
    accepts any text there (a newline, braces, quotes), so the parser alone is not the charset. A host name becomes
    an inventory KEY, an inventory variable Ansible templates and part of a lookup expression, so the closed charset
    is load-bearing even though it is not a path."""
    with pytest.raises(ValueError):
        paths.hostname(value, "host")


def test_confined_accepts_repository_paths_and_refuses_climbing_out(tmp_path, monkeypatch):
    """`confined()` returns the absolute target for a repo-relative path and refuses an absolute path, a `..` that
    climbs out, a path that climbs out and back through a sibling, and a NUL — resolved against the REAL
    write_root(), so a symlink pointing outside is refused too. The last check before every drop-in write."""
    root = tmp_path / "repo"
    (root / "modules").mkdir(parents=True)
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(root))
    inside = paths.confined("modules/x/module.yml")
    assert inside == os.path.realpath(os.path.join(str(root), "modules", "x", "module.yml"))
    assert paths.confined("modules/../modules/y.yml").endswith("y.yml")          # climbs but stays inside
    for bad in ("../outside.yml", "modules/../../outside.yml", str(tmp_path / "abs.yml"), "a\x00b", "", None):
        with pytest.raises(ValueError):
            paths.confined(bad)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    try:
        os.symlink(str(outside), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this host")
    with pytest.raises(ValueError):
        paths.confined("escape/evil.yml")


def test_write_new_and_write_inventory_host_go_through_confined(tmp_path, monkeypatch):
    """The two drop-in writers refuse a climbing path and write NOTHING — the confinement is in the writer, not only
    in the planner, so a future caller that skips the planner is still stopped."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(root))
    with pytest.raises(ValueError):
        gitio._write_new("../evil.yml", "x: 1\n", "# banner\n")
    with pytest.raises(ValueError):
        gitio.write_inventory_host("../../evil.yml", "g", {"h": {}}, "# banner\n")
    assert not (tmp_path / "evil.yml").exists()
    assert gitio._write_new("modules/ok/module.yml", "x: 1\n", "# banner\n") is True
    assert (root / "modules" / "ok" / "module.yml").exists()


def test_module_file_refuses_a_path_shaped_key():
    """`paths.module_file` is the ONE resolver every module read goes through (onboard, identity, capability,
    configurable); it re-checks the key at the join so a caller that never saw the planner is still confined."""
    for bad in ("../x", "a/b", "..", "Key"):
        with pytest.raises(ValueError):
            paths.module_file(bad)
    assert paths.module_file("cisco_ios").endswith(os.path.join("modules", "cisco_ios", "module.yml"))


def test_the_planner_refuses_before_the_probe_is_reached(monkeypatch):
    """`build_onboard_plan` validates every field FIRST: with a bad key the probe (which shells ansible-doc with the
    collection) is never called and no catalog is loaded. Guards the order — a validator after the probe would still
    have handed the request's bytes to a subprocess."""
    def must_not_be_reached(*a, **k):
        raise AssertionError("the probe must not run for a refused request")
    monkeypatch.setattr(probe, "deep_probe", must_not_be_reached)
    with pytest.raises(ValueError) as e:
        onboard.build_onboard_plan("acme.edgeos", "../etc", "edge_router", "192.0.2.50", backends=[], vectors=[])
    assert "device-class key" in str(e.value) and "../etc" not in str(e.value)   # the field, never the value
    for kwargs in ({"group": "edge router"}, {"host": "192.0.2.1; id"}, {"host_name": "../h"},
                   {"secrets": "../network"}, {"collection": "acme/edgeos"}):
        args = dict(collection="acme.edgeos", key="edgeos", group="edge_router", host="192.0.2.50")
        args.update(kwargs)
        with pytest.raises(ValueError):
            onboard.build_onboard_plan(args["collection"], args["key"], args["group"], args["host"],
                                       host_name=args.get("host_name"), secrets=args.get("secrets", "network"),
                                       backends=[], vectors=[])


def test_probe_dir_walks_refuse_an_unvalidated_collection_name(tmp_path):
    """`shallow_from_local` and `units_in_collection` join `base_path/<ns>/<name>`; both re-validate the name so a
    `../` can never be walked even when a caller forgets the SEC-3 contract."""
    for fn in (lambda: probe.units_in_collection("../x", str(tmp_path)),
               lambda: probe.shallow_from_local("acme/edgeos", None, str(tmp_path))):
        with pytest.raises(ValueError):
            fn()


def test_a_refusal_names_its_field_and_is_still_a_value_error():
    """Every validator raises `paths.RequestBoundaryError`: a ValueError (so a handler written for the planner's
    other guards still maps it to 422 before any write) that also carries `.field`, which is what the audit line records —
    the FIELD, never the refused value. Guards a validator regressing to a bare ValueError (no field to audit) or
    to some other exception class (a 500 instead of a refusal)."""
    with pytest.raises(paths.RequestBoundaryError) as e:
        paths.component("../x", "device-class key")
    assert isinstance(e.value, ValueError) and e.value.field == "device-class key"
    with pytest.raises(paths.RequestBoundaryError) as e:
        paths.hostname("a b", "host")
    assert e.value.field == "host"
    with pytest.raises(paths.RequestBoundaryError) as e:
        paths.collection_fqcn("acme/edgeos")
    assert e.value.field == "collection"
    with pytest.raises(paths.RequestBoundaryError) as e:
        paths.confined("../out")
    assert e.value.field == "path"


MARKER = "Zz-refused-value-zZ"


@pytest.mark.parametrize("refuse, field", [
    (lambda v: paths.component(v, "device-class key"), "device-class key"),
    (lambda v: paths.collection_fqcn(v), "collection"),
    (lambda v: paths.hostname(v + " x", "host"), "host"),
    (lambda v: paths.confined("../" + v), "path"),
], ids=["component", "collection_fqcn", "hostname", "confined"])
def test_a_refusal_names_the_field_and_never_the_value(refuse, field):
    """The message of a refusal carries the FIELD and not the refused value, in any form. Guards the
    value travelling on in the message — to a service log that is shipped off the box, to an API client that logs
    error bodies (a credential pasted into the wrong field), to a second surface that paints it. The exception's own
    contract always said so; until 2026-10-12 every message quoted a forty-character repr of the value."""
    with pytest.raises(paths.RequestBoundaryError) as e:
        refuse(MARKER)
    assert e.value.field == field
    assert "refused-value" not in str(e.value)
    assert field in str(e.value) or field == "path"


def test_a_refusal_carries_a_code_derived_from_the_field():
    """`RequestBoundaryError.code` is the machine code the API and the GUI put beside the message: `invalid_` plus
    the field name with every run of other characters folded to `_`. Guards a client keying its next-step copy on a
    code that silently changed spelling, or on one built from the value."""
    for field, code in (("collection", "invalid_collection"), ("device-class key", "invalid_device_class_key"),
                        ("host_name", "invalid_host_name"), ("secrets domain", "invalid_secrets_domain")):
        assert paths.RequestBoundaryError(field, "x").code == code


@pytest.mark.parametrize("value", ["--version", "-M..", "--playbook-dir=..", "a b.c", "cisco.ios;x", "cisco.ios\n",
                                   "cisco", ""])
def test_deep_probe_refuses_a_name_that_is_not_a_collection_before_ansible_doc_runs(monkeypatch, value):
    """`probe.deep_probe` puts the collection name on `ansible-doc`'s command line several times, and some of its
    callers are routes that need no token (GET /probe, GET /classify). It validates the name itself, at the join: an
    option-shaped or otherwise malformed name raises before any process is started. Guards a request value becoming
    an argv word of a subprocess — on the open routes the validator at the join is the only control there is."""
    def must_not_run(args):
        raise AssertionError("ansible-doc must not be started for a refused name: %r" % (args,))
    monkeypatch.setattr(probe, "_ad", must_not_run)
    with pytest.raises(paths.RequestBoundaryError) as e:
        probe.deep_probe(value)
    assert e.value.field == "collection"


BUILDERS = {
    "telemetry": lambda key: observe.build_telemetry_plan(key, "host_node"),
    "logging": lambda key: logsvc.build_logging_plan(key, "syslog"),
    "backup": lambda key: backup.build_plan(key, {"params": {"schedule": "daily"}}),
    "identity": lambda key: identity.build_plan(key, {"values": {}}),
    "host vars": lambda key: hostvars.build_plan(key, "sw1", {"values": {}}),
    "suggest": lambda key: capability.suggest_view("telemetry", key),
}


@pytest.mark.parametrize("builder", sorted(BUILDERS))
@pytest.mark.parametrize("key", ["../modules/cisco_ios", "zz/qq", "QqUpper", "qq_key\n"])
def test_every_plan_builder_refuses_a_bad_key_before_it_touches_the_filesystem(monkeypatch, builder, key):
    """Every plan builder that joins a request `key` into a path refuses a malformed key as its FIRST act: the
    refusal names the field, and the filesystem is never asked about a path that holds the value. Guards a builder
    that spells the join itself and is saved only by the order of two later calls — three of them tested
    `modules/<key>/module.yml` for existence first, which told a caller whether some other directory holds a module
    file and answered `not_onboarded`, `unknown_method` or a 500 depending on what came next."""
    real_exists = os.path.exists

    def exists(path):
        assert key not in str(path).replace(os.sep, "/"), "the join was reached with the refused key: %r" % (path,)
        return real_exists(path)
    monkeypatch.setattr(os.path, "exists", exists)
    with pytest.raises(paths.RequestBoundaryError) as e:
        BUILDERS[builder](key)
    assert e.value.field == "device-class key"


def test_a_unit_key_followed_by_a_newline_matches_no_unit(monkeypatch):
    """The actuation preview refuses a unit key that is a valid key plus a trailing newline WITHOUT consulting the
    registry: the key pattern is applied with `fullmatch()`. Guards the `$`-before-a-final-newline trap in the one
    key validator that is not `paths.component` — with `match()` the value went on to the registry lookup and would
    have gone on to the values-file path."""
    def must_not_run(key):
        raise AssertionError("the registry must not be asked about a malformed key: %r" % (key,))
    monkeypatch.setattr(catalog, "actuation_unit", must_not_run)
    assert actuation.build_actuation_plan("backup-ios\n", {})["error"] == "no_unit"


@pytest.mark.parametrize("value", [True, 8443, "8443", "192.0.2.9", "fw.lab.example", "2001:db8::1", "true"])
def test_an_inline_host_var_accepts_plain_hosts_ports_and_flags(value):
    """A backend's or a class's `inline: true` credential field is written into the inventory as a literal host
    var: a bool, an int, or one word of letters, digits, `.`, `_`, `:` and `-` passes unchanged. Guards the check
    growing stricter than the values such a field exists for (a host, a port, a flag) — an operator who cannot
    enter `8443` would work around the form."""
    assert onboard._inline_literal(value, "api_port") == value


@pytest.mark.parametrize("value", ["{{ lookup('pipe', 'id') }}", "a b", "x;y", "8443\n", "", None, ["8443"],
                                   "{% raw %}", "a" * 254])
def test_an_inline_host_var_refuses_anything_ansible_could_template(value):
    """Anything else is refused with the FIELD named: Ansible templates inventory variables, so a string with braces
    in a literal host var is an expression that runs on the runner when a play touches the host. No shipped backend
    declares an inline field today; this pins the boundary for the first one that does."""
    with pytest.raises(paths.RequestBoundaryError) as e:
        onboard._inline_literal(value, "api_port")
    assert e.value.field == "api_port" and "lookup" not in str(e.value)


def test_refresh_skips_an_installed_entry_that_is_not_a_collection_name(monkeypatch, tmp_path):
    """The matrix refresh probes every installed collection. An installed entry whose name is not `namespace.name`
    is skipped — it never becomes an argv word, and it does not fail the refresh for every other collection. Guards
    the join in `deep_probe` turning one stray entry of the installed list into a refused request."""
    probed = []

    def fake_ad(args):
        probed.append(args)
        return ""
    monkeypatch.setattr(probe, "_ad", fake_ad)
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0", "--version": "1"})
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))     # the cache is written under the write root
    out = refresh.service_refresh(vectors=[], overrides=[], backends=[])
    assert out["count"] == 1 and out["records"][0]["collection"] == "cisco.ios"
    assert probed and not any("--version" in args for args in probed)


@pytest.mark.parametrize("key", ["null", "true", "no", "on", "123", "0x10", "2026-10-12", "0x_", "2026-02-30"])
def test_a_class_key_that_yaml_reads_as_another_type_is_refused(tmp_repo, monkeypatch, key):
    """`null`, `true`, `123`, `2026-10-12`: every character is inside the charset, and none of them is a string once
    it stands unquoted in the fleet's `enabled_modules` list — the readers of that list would hold None, a bool, a
    number or a date for a class key. The planner refuses such a key with the field named, before the probe, and
    the fleet writer refuses it at the join. Guards a class that can be onboarded and then breaks every route that
    enumerates the fleet. (Under `tmp_repo`: if the fleet writer's own check ever regressed, the call below would
    otherwise append the line to the fleet file of the checkout the suite runs in.)"""
    monkeypatch.setattr(probe, "deep_probe", lambda *a, **k: pytest.fail("the probe ran for a refused key"))
    with pytest.raises(paths.RequestBoundaryError) as e:
        onboard.build_onboard_plan("acme.edgeos", key, "edge_router", "192.0.2.50", backends=[], vectors=[])
    assert e.value.field == "device-class key"
    # the message is one fixed sentence: nothing of the request is interpolated into it
    assert str(e.value) == "device-class key is a word YAML reads as another type (null, a boolean, a number, a date)"
    with pytest.raises(paths.RequestBoundaryError):
        onboard.enable_in_fleet(key)


def test_enumerators_skip_an_entry_that_is_not_a_key(monkeypatch):
    """A reader that enumerates classes skips an entry that is not a class key instead of raising: `enabled_keys`
    over a parsed fleet file (a `- null`, a number, a mapping, a name with a newline or an uppercase letter), and
    `module_keys` over the modules directories. Such an entry is data the tool read back, not a request field —
    handed to `module_file()` it raised a request-boundary refusal, so one bad line in a fleet file (or one stray
    directory) made every route that enumerates classes answer 422 for a field nobody sent."""
    fleet = {"enabled_modules": ["cisco_ios", None, True, 123, {"a": 1}, "bad\n", "Upper", "", "edge-fw"]}
    assert paths.enabled_keys(fleet) == ["cisco_ios", "edge-fw"]
    for not_a_fleet in ({}, {"enabled_modules": None}, {"enabled_modules": "cisco_ios"}, None, []):
        assert paths.enabled_keys(not_a_fleet) == []
    monkeypatch.setattr(os, "listdir", lambda p: ["ok", "bad\n", "README.md", "_core.yml"])
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    assert paths.module_keys() == ["ok"]


def test_suggest_degrades_when_a_module_file_names_something_that_is_not_a_collection(tmp_repo, monkeypatch):
    """`suggest_view` reads the class's collection from its module file and probes it. When that name is not
    `namespace.name` — data in a file, not a request field — the view comes back with no suggestion instead of a
    refusal, and `ansible-doc` is not started. Guards the join in `deep_probe` answering a well-formed request with
    a 422 that names a field the caller never sent."""
    d = tmp_repo / "modules" / "demo_sw"
    d.mkdir(parents=True)
    (d / "module.yml").write_text("key: demo_sw\ncollections:\n  - name: \"--version\"\ninventory_group: core_switch\n",
                                  encoding="utf-8")
    monkeypatch.setattr(probe, "_ad", lambda args: pytest.fail("ansible-doc was started: %r" % (args,)))
    view = capability.suggest_view("telemetry", "demo_sw")
    assert view["error"] is None and view["suggestion"] is None


def test_the_cli_prints_a_refusal_as_one_line(monkeypatch):
    """`galaxy.py classify <not a collection>` exits with the refusal's message — the field and the rule, not the
    value — instead of a traceback. Guards the operator's own typo at the command line reading like a crash."""
    import galaxy
    monkeypatch.setattr(probe, "_ad", lambda args: pytest.fail("ansible-doc was started: %r" % (args,)))
    monkeypatch.setattr(galaxy.sys, "argv", ["galaxy.py", "classify", "Cisco.IOS"])
    with pytest.raises(SystemExit) as e:
        galaxy.main()
    assert "collection" in str(e.value.code) and "Cisco" not in str(e.value.code)


@pytest.mark.parametrize("field,kwargs", [
    ("inventory group", {"group": "null"}), ("inventory group", {"group": "123"}),
    ("inventory group", {"group": "true"}), ("secrets domain", {"secrets": "no"}),
    ("secrets domain", {"secrets": "2026-10-12"}), ("backend", {"backend": "null"}), ("backend", {"backend": "on"}),
    # shaped like a number or a date, and the YAML reader cannot build either: still a refusal of the FIELD
    ("inventory group", {"group": "0x_"}), ("inventory group", {"group": "2026-13-45"}),
    ("secrets domain", {"secrets": "0b_"}), ("backend", {"backend": "2026-02-30"}),
])
def test_every_onboard_field_that_becomes_a_name_must_be_a_word_yaml_reads_as_a_string(monkeypatch, field, kwargs):
    """The inventory group, the secrets domain and the backend get the same rule as the class key: one path
    component AND a word YAML reads as a string. Refused before the probe, with the field named and one fixed
    sentence. Guards the field next to the one that was fixed: a group named `null` was stored (quoted) in the
    module file and then written bare into the generated Prometheus targets and backup schedules, where it reads
    as no group at all."""
    monkeypatch.setattr(probe, "deep_probe", lambda *a, **k: pytest.fail("the probe ran for a refused request"))
    args = dict(group="edge_router", secrets="network", backend=None)
    args.update(kwargs)
    with pytest.raises(paths.RequestBoundaryError) as e:
        onboard.build_onboard_plan("acme.edgeos", "edgeos", args["group"], "192.0.2.50", secrets=args["secrets"],
                                   backend=args["backend"], backends=[], vectors=[])
    assert e.value.field == field
    assert str(e.value) == "%s is a word YAML reads as another type (null, a boolean, a number, a date)" % field


@pytest.mark.parametrize("host_name", ["true", "null", "123", "no", "2026-02-30", "0000-00-00"])
def test_a_host_name_may_be_a_word_yaml_reads_as_another_type(monkeypatch, host_name):
    """A host named `true`, `null` or `123` passes the request boundary (it reaches the probe): each is a valid
    RFC-1123 name, the inventory drop-in is serialized, and the generators quote a host name that is not a plain
    word. Guards the keyword rule being applied to the one field where it would refuse real host names."""
    reached = []
    monkeypatch.setattr(probe, "deep_probe", lambda *a, **k: reached.append(1) or {"modules": [], "plugins": []})
    plan = onboard.build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50", host_name=host_name,
                                      backends=[], vectors=[])
    assert reached and plan == {"error": "not_installed"}


def test_no_boundary_pattern_accepts_a_trailing_newline_even_with_match():
    """Every pattern of the request boundary ends in the end-of-string anchor (backslash-Z): `.match()` on a
    valid value followed by one newline is None for each of them. They are applied with `fullmatch()` today, so a
    `$` would behave the same — until the next caller writes `.match()`, which is how the first version of this
    boundary accepted a key followed by a newline. Guards the convention being one a reader can rely on by looking
    at the pattern."""
    from kontroll.service import _actuation_schema as schema
    from kontroll.service import _validate, actuation, settings
    genreq = _load_script("gen_requirements_patterns", "gen-requirements.py")
    samples = [
        (paths._COMPONENT_RE, "cisco_ios"), (paths._FQCN_RE, "cisco.ios"), (paths._HOSTNAME_RE, "sw1.example.test"),
        (paths._IP_LITERAL_RE, "192.0.2.10"), (onboard._INLINE_RE, "host.example"), (onboard._STEM_RE, "api_token"),
        (settings._REMOTE_NAME, "offsite"), (_validate._HOSTNAME_LABEL, "sw1"), (schema.KEY_RE, "my-unit"),
        (schema.KNOB_KEY_RE, "port"), (schema.PIN_VERSION_RE, "8.0.4"), (schema.SHA256_RE, "a" * 64),
        (actuation._KEY_RE, "my-unit"), (actuation._NAME_RE, "ios_facts"), (genreq._LOCK_NAME_RE, "cisco.ios"),
        (genreq._LOCK_VERSION_RE, ">=8.0.0"), (genreq._LOCK_DIGEST_RE, "a" * 64),
    ]
    for pattern, valid in samples:
        assert pattern.match(valid) is not None, pattern.pattern
        assert pattern.match(valid + "\n") is None, pattern.pattern
        assert pattern.pattern.endswith("\\Z"), pattern.pattern
