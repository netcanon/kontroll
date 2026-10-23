"""A name the server takes on is held to its field's rule — and a file that fails is not a refused request.

WHY (the failures these guard): the onboard planner checked every name the REQUEST carried, then, on the reuse
path, replaced three of them with values read from the class's module file that nothing had checked, and took a
fourth (the role) from there without the request ever having one. The adopted secrets domain is formatted into a
lookup expression in every credential host var; the role is a host var Ansible templates; the group is the
drop-in's top-level key. A weaker-checked value a request could store — an identity knob had no length bound, the
path join stops at sixty-four characters — ended in a 422 naming a field nobody sent, after the inventory drop-in
had been written (and the fleet line, where the class was not enabled yet); the role a request's backend derives
was the same fault one field along. And where the repository's own data or layout stopped a request (a module
file, a credential field a registry declares, a write target outside the write root), both apps answered as if a
request field had been refused. SECURITY.md C22.
"""
import argparse
import glob
import os

import pytest
import yaml

from kontroll import authspec, catalog, gitio, paths, probe
from kontroll.service import identity, onboard, openapi, scaffold

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LINE_SEPARATOR = chr(0x2028)
HOSTILE = ["x') ~ lookup('pipe','id') ~ ('", "a" * 80, 123, True, ["network"], "", "Null", "net" + LINE_SEPARATOR + "work",
           "../network", "net work", "null", 0, False, {}, 1.5]
RESERVED = ["all", "ungrouped", "localhost"]
ABSENT = object()                     # for the `reuse` fixture: the module file does not carry the key at all


@pytest.fixture
def reuse(tmp_repo, monkeypatch, make_facts):
    """A throwaway tree with ONE onboarded class (`demo_sw`, cisco.ios) whose identity keys the test chooses, and
    the planner's two I/O seams canned — so a plan for a second device of that class takes the REUSE path."""
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "1.0.0"})

    def write(**identity_keys):
        module = {"key": "demo_sw", "collections": [{"name": "cisco.ios", "version": ">=1.0.0"}],
                  "role": "backend_netcommon_cli", "backend": "netcommon_cli",
                  "inventory_group": "core_switch", "secrets_domain": "network"}
        module.update(identity_keys)
        for name in [k for k, v in module.items() if v is ABSENT]:
            del module[name]
        target = tmp_repo / "modules" / "demo_sw"
        target.mkdir(parents=True, exist_ok=True)
        (target / "module.yml").write_text(yaml.safe_dump(module), encoding="utf-8")
        return tmp_repo
    return write


def _plan(**kw):
    return onboard.build_onboard_plan("cisco.ios", "demo_sw", "core_switch", "192.0.2.40", host_name="sw2", **kw)


# --- what the reuse path adopts -------------------------------------------------------------------------------------

def test_a_healthy_class_is_adopted_as_before(reuse):
    """A second device of a class whose module file holds ordinary names gets that class's group, domain, backend
    and role — whatever the request said — and the plan is a reuse. Guards the re-check changing a healthy
    onboard: the adopted strings are returned unchanged."""
    reuse(inventory_group="lab_switches", secrets_domain="lab")
    plan = _plan(password="pw")
    assert plan["error"] is None and plan["module_reuse"] is True
    assert (plan["group"], plan["secrets"], plan["backend"], plan["role"]) == (
        "lab_switches", "lab", "netcommon_cli", "backend_netcommon_cli")
    assert list(plan["host_block"]) == ["lab_switches"]


@pytest.mark.parametrize("field", onboard._ADOPTED)
@pytest.mark.parametrize("value", HOSTILE, ids=repr)
def test_reuse_refuses_a_module_file_whose_adopted_name_is_not_a_name(reuse, monkeypatch, field, value):
    """For each name the reuse path takes from the module file, a value that is not one path
    component YAML reads as a string — a quote-and-parenthesis string, eighty letters, a number, a boolean, a
    list, an empty string, a capital letter, a line separator, a path, a space, a YAML keyword, and the values
    that used to mean "not stated" (zero, false, an empty mapping) — stops the PLAN with
    `paths.RepositoryDataError`, which is not a ValueError.

    Guards a file value reaching a lookup expression, a templated host var or a path unread; and a refusal that
    arrives only at apply, after the inventory drop-in had been written (and the fleet line, where the class was
    not enabled yet) — the eighty-letter domain did exactly that. The type and the place are the guard: a plan
    writes nothing, refused or not (the patched writers only show that)."""
    for writer in ("_write_new", "write_inventory_host", "sops_decrypt_domain", "sops_write_domain"):
        monkeypatch.setattr(gitio, writer, lambda *a, **k: pytest.fail("a writer ran for a refused plan"))
    monkeypatch.setattr(onboard, "enable_in_fleet", lambda *a, **k: pytest.fail("the fleet writer ran"))
    reuse(**{field: value})
    with pytest.raises(paths.RepositoryDataError) as stop:
        _plan(password="pw")
    assert stop.value.what == "module file"
    assert not isinstance(stop.value, ValueError), "a planner-guard handler must not be able to relabel it"


@pytest.mark.parametrize("field", onboard._ADOPTED)
def test_reuse_refusal_names_the_file_and_never_the_value(reuse, field):
    """The refusal names the class, the key of the module file and the file to correct — and none of the refused
    text. Guards the value travelling on in a response body and a log; the operator is told where to look."""
    marker = "Zz-refused') ~ value"
    reuse(**{field: marker})
    with pytest.raises(paths.RepositoryDataError) as stop:
        _plan()
    message = str(stop.value)
    assert "demo_sw" in message and field in message and "modules/demo_sw/module.yml" in message
    assert "Zz-refused" not in message and "~" not in message


@pytest.mark.parametrize("field,value,rule", [
    ("inventory_group", "Core_Switch", "1-64 chars"), ("role", "backend.cli", "1-64 chars"),
    ("inventory_group", "localhost", "Ansible reserves"), ("secrets_domain", "true", "YAML reads"),
    ("backend", "a" * 65, "1-64 chars"),
])
def test_reuse_refusal_says_which_rule_the_file_breaks(reuse, field, value, rule):
    """The refusal carries the RULE the value breaks — the charset and length of a name, a name Ansible reserves,
    a word YAML reads as another type — beside the class and the file, and not the value.

    Guards a refusal that names the file and not what is wrong with it: `localhost` and `null` are well-formed
    names, and an operator told only "not a valid name" looks for a typo that is not there."""
    reuse(**{field: value})
    with pytest.raises(paths.RepositoryDataError) as stop:
        _plan()
    message = str(stop.value)
    assert rule in message and field in message and "modules/demo_sw/module.yml" in message
    assert value not in message or value == "localhost"        # `localhost` is in the fixed list the rule prints


@pytest.mark.parametrize("stated", [None, ABSENT], ids=["null", "absent"])
@pytest.mark.parametrize("field", onboard._ADOPTED)
def test_an_absent_or_null_adopted_name_means_use_the_requests(reuse, field, stated):
    """A module file that does not carry one of the adopted keys, or carries it as null, is not refused: the
    request's own value (or the registry's, for the role) is used. Guards the re-check turning "not stated" into a
    fault — only a value that IS there and is not a name is one."""
    reuse(**{field: stated})
    plan = _plan()
    assert plan["error"] is None and plan["module_reuse"] is True


@pytest.mark.parametrize("text", ["- key: demo_sw\n- collections: []\n", "just a word\n", "42\n"])
def test_a_module_file_that_is_not_a_mapping_is_repository_data(reuse, tmp_repo, text):
    """A module file that parses to a list, a bare word or a number stops the plan with `RepositoryDataError`
    naming the class and the file. Guards the 500 it used to be on both apps (an AttributeError out of the first
    `.get`): it is a file that fails, and it is answered as one."""
    reuse()
    (tmp_repo / "modules" / "demo_sw" / "module.yml").write_text(text, encoding="utf-8")
    with pytest.raises(paths.RepositoryDataError) as stop:
        _plan()
    assert stop.value.what == "module file" and "modules/demo_sw/module.yml" in str(stop.value)
    assert "not a mapping" in str(stop.value)


def _maps_to_passes(maps_to, template_ok):
    """The planner's rule for a credential field's `maps_to`: none, an identifier, or — in the backend registry
    alone (`template_ok`) — a template that renders, for a representative credential key, inside
    catalog.MAPS_TO_RE. A module file's entry names its var as written (T18's read, F1)."""
    if template_ok and isinstance(maps_to, str) and "{" in maps_to:
        return bool(catalog.render_template(maps_to, {"cred_key": "sw2"}, catalog.MAPS_TO_RE))
    return maps_to is None or bool(onboard._STEM_RE.fullmatch(maps_to))


def test_every_shipped_class_and_backend_passes_the_adoption_rule():
    """Every shipped module file passes the adoption rule for the names it holds, every credential field a shipped
    module declares maps to nothing or to an identifier, one a shipped backend declares also to a template (S27)
    that renders to a host var name, every role a shipped backend declares passes the adopted role's rule, and its default domain passes the
    request field's. Guards the re-check refusing a healthy shipped class: this is the corpus the rule was measured
    against, kept in the suite."""
    seen = 0
    for path in sorted(glob.glob(os.path.join(ROOT, "modules", "*", "module.yml"))):
        with open(path, encoding="utf-8") as fh:
            module = yaml.safe_load(fh) or {}
        for field in onboard._ADOPTED:
            onboard._adopted(module, field, module.get("key") or "x")
        for entry in module.get("auth") or []:
            assert _maps_to_passes(entry.get("maps_to"), template_ok=False), path
        seen += 1
    for backend in catalog.load_backends():
        for entry in backend.get("auth") or []:
            assert _maps_to_passes(entry.get("maps_to"), template_ok=True), backend.get("name")
        onboard.default_secrets_domain(backend, backend["name"])         # raises RepositoryDataError if it fails
        if catalog.backend_role(backend) is not None:                    # one with none is never chosen (S26)
            assert onboard._role_of(backend, "x", False)
        assert paths.is_component(backend["name"]) and onboard.plain_scalar(backend["name"]), backend.get("name")
    assert seen > 0, "no shipped module file was read"


@pytest.mark.parametrize("maps_to", ["ansible_connection }}", "ansible_connection\n", "a b", 7, "", ["ansible_user"]])
def test_a_credential_fields_host_var_name_is_an_identifier(reuse, monkeypatch, maps_to):
    """A credential field whose `maps_to` — the NAME of the host var it is written under — is not an identifier (a
    brace, a line break, a space, a number, an empty string, a list) is refused as repository data, before any
    host var is built, with the class and the backend named; `ansible_user` is accepted, and so is
    `ansible_connection`, for a field stored in SOPS.

    Guards a host-var KEY that is not a name reaching the inventory drop-in. It does NOT guard which host var a
    SOPS-stored field names: `maps_to: ansible_connection` is an identifier and its lookup is still written there
    (asserted below, so the limit is pinned and not implied closed). An INLINE field's var is narrower since S28 —
    a port only into a `*_port` var (authspec.inline_target) — and that is asserted last."""
    reuse()
    stored = {"field": "mode", "kind": "password", "label": "mode", "sops_stem": "mode", "secret": True}
    monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"source": "shallow", "fields": [
        dict(stored, maps_to=maps_to)]})
    with pytest.raises(paths.RepositoryDataError) as stop:              # the planner's own check: a SOPS field's
        _plan(creds={"mode": "local"})                                   # var is not narrowed by inline_target
    message = str(stop.value)
    assert stop.value.what == "credential field" and "ansible_connection" not in message
    assert "demo_sw" in message and "netcommon_cli" in message          # where to look
    field = {"field": "mode", "kind": "port", "label": "mode", "sops_stem": "mode", "inline": True}
    for name in ("ansible_user", "ansible_connection"):
        monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"source": "shallow", "fields": [
            dict(stored, maps_to=name)]})
        plan = _plan(creds={"mode": "local"})
        written = plan["host_block"]["core_switch"]["hosts"]["sw2"][name]
        assert "lookup(" in written and "local" not in written, name     # a SOPS lookup, never the value
    monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"source": "shallow", "fields": [
        dict(field, maps_to="ansible_connection")]})
    with pytest.raises(paths.RepositoryDataError):
        _plan(creds={"mode": "22"})


@pytest.mark.parametrize("maps_to", [None, ABSENT], ids=["null", "absent"])
def test_an_inline_credential_field_names_the_host_var_it_is_written_under(reuse, monkeypatch, maps_to):
    """An `inline` credential field that names no host var is refused as repository data once a value is given
    for it; without a value the plan is returned and nothing is written under it.

    Guards a drop-in holding the line `null: <value>`: an inline value is not stored in SOPS, so the host var is
    the only place it can go, and the planner used to index the host block with whatever `maps_to` was."""
    reuse()
    field = {"field": "mode", "kind": "port", "label": "mode", "sops_stem": "mode", "inline": True}
    if maps_to is not ABSENT:
        field["maps_to"] = maps_to
    monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"source": "shallow", "fields": [dict(field)]})
    with pytest.raises(paths.RepositoryDataError) as stop:
        _plan(creds={"mode": "22"})
    assert stop.value.what == "credential field" and "inline credential field" in str(stop.value)
    assert "demo_sw" in str(stop.value)                                  # where to look
    plan = _plan()
    assert plan["error"] is None and None not in plan["host_block"]["core_switch"]["hosts"]["sw2"]


@pytest.mark.parametrize("name", ["raw_thing", "b" * 56, "b" * 57, "b" * 64])
def test_no_request_stores_a_role_the_next_onboard_refuses(reuse, name):
    """A `backend` the registry does not ship is refused as the `backend` field, by the planner and by the scaffold,
    whatever its length; the value is not in the message. Nothing is derived from a request's backend any more, so
    no request can write a module file whose role the next onboard refuses.

    Guards the role as a request-made name. The planner made `backend_<name>` up for any backend a request named
    (S26), and the earlier rule here refused only a name too long to make one from: `raw_thing` (or any name up to
    fifty-six letters) still wrote a role no play could include into the module file and every host's
    `device_role`."""
    for call in (lambda: onboard.build_onboard_plan("cisco.ios", "newcls", "core_switch", "192.0.2.41", backend=name),
                 lambda: scaffold.build_scaffold("cisco.ios", "scaf", "core_switch", "network", name, backends=[])):
        with pytest.raises(paths.RequestBoundaryError) as stop:
            call()
        assert stop.value.field == "backend" and name not in str(stop.value)


@pytest.mark.parametrize("backend", ["b" * 60, "vendor_config", "napalm"])
def test_a_role_the_module_file_does_not_give_is_the_files_fault(reuse, backend):
    """A class whose module file names no role and a backend that declares none — one this install does not ship,
    or a shipped one with no execution role — stops the plan with `RepositoryDataError` naming the class and what to
    add, not with a refusal of a `backend` field the request never carried; the backend is not in the message.

    Guards answering a file's fault as a field's, at the place the role is taken: the request sent no backend, so
    a 422 would tell the caller to correct a value they never typed."""
    reuse(backend=backend, role=ABSENT)
    with pytest.raises(paths.RepositoryDataError) as stop:
        _plan()
    assert stop.value.what == "backend" and "demo_sw" in str(stop.value) and "role:" in str(stop.value)
    assert backend not in str(stop.value)


@pytest.mark.parametrize("backend", ["b" * 60, "napalm"])
def test_a_backend_the_request_sent_is_the_requests_fault_on_a_reuse_too(reuse, backend):
    """A second device of a class whose module file names no role, onboarded with a `backend` the registry does not
    ship or one that declares no role, is refused as the `backend` field — the request sent that backend, whatever
    the file holds.

    Guards the fault being decided by the PATH (reuse or not) instead of by who supplied the backend: a caller
    would be told to correct a file for a value they typed."""
    reuse(role=ABSENT)
    with pytest.raises(paths.RequestBoundaryError) as stop:
        _plan(backend=backend)
    assert stop.value.field == "backend"


_CLI = {"name": "netcommon_cli", "requires": [], "classify": {"plugin": "cliconf"},    # a registry entry that
        "role": "backend_netcommon_cli", "default_secrets_domain": "network"}          # runs (it declares a role)


@pytest.mark.parametrize("name", ["r" * 60, "Weird.Name", "x y", "netcommon_cli"])
@pytest.mark.parametrize("role", [None, ""], ids=["no-role", "empty-role"])
def test_a_matched_backend_that_declares_no_role_is_passed_over(reuse, name, role):
    """With no `backend` asked for, a registry entry the classifier matches that declares no role (none, or an empty
    one) is passed over by the planner and by the scaffold: alone, the answer is `no_backend`, naming it as the match
    that cannot run; nothing is made up from its name, whatever that name is.

    Guards the auto path writing a class nobody named a backend for: a collection whose only match was
    `vendor_config` was planned with the made-up role `backend_vendor_config` (S26), and a match whose name no role
    could be made from stopped the plan as repository data instead of being passed over."""
    registry = [dict(_CLI, name=name, role=role)]
    plan = onboard.build_onboard_plan("cisco.ios", "newcls", "core_switch", "192.0.2.41", backends=registry, vectors=[])
    made = scaffold.build_scaffold("cisco.ios", "scaf", "core_switch", "network", backends=registry)
    assert plan["error"] == made["error"] == "no_backend"
    assert plan["signals"]["unrunnable"] == made["signals"]["unrunnable"] == [name]
    assert "role" not in plan and "module" not in made


@pytest.mark.parametrize("role", [123, ["x"], "Bad Role }}", "r" * 80, "null", 0, False], ids=repr)
def test_a_registry_role_that_is_not_a_name_is_repository_data(reuse, role):
    """A backend registry entry whose own `role` is not a name — a number, a list, a string with a brace, eighty
    letters, a YAML keyword, zero, false — stops the plan of a NEW class and the scaffold with
    `RepositoryDataError` (`what == "backend"`), naming the class and not the role.

    Guards the one name that reached a new module file unread: the planner wrote the registry's role into the
    file and into the `device_role` host var, and the next onboard of the class then refused the file the tool
    itself had written."""
    registry = [dict(_CLI, role=role)]
    with pytest.raises(paths.RepositoryDataError) as stop:
        onboard.build_onboard_plan("cisco.ios", "newcls", "core_switch", "192.0.2.41", backends=registry, vectors=[])
    assert stop.value.what == "backend" and "newcls" in str(stop.value)
    assert not (isinstance(role, str) and role in str(stop.value))
    with pytest.raises(paths.RepositoryDataError):
        scaffold.build_scaffold("cisco.ios", "scaf", "core_switch", "network", backends=registry)


@pytest.mark.parametrize("registry,backend", [
    ([dict(_CLI, role="my_role")], None),
    ([dict(_CLI, name="meta_only", order=0, role=None), dict(_CLI, role="my_role")], None),
    ([dict(_CLI, name="meta_only", order=0, role=None), dict(_CLI, role="my_role")], "netcommon_cli"),
], ids=["registry-role", "passed-over", "request-backend"])
def test_what_a_new_class_plan_writes_the_next_onboard_adopts(reuse, registry, backend):
    """The role the planner writes into a NEW class's module file — and the other adopted keys — passes the adoption
    rule, and is the registry entry's own, whether the classifier chose the backend (past a match that declares no
    role) or the request named it. The scaffold's module passes it too, with the same role.

    Guards the invariant behind the role rule, for every way a backend is chosen: a request must not be able to write
    a module file that the next onboard of the class answers 409 for."""
    plan = onboard.build_onboard_plan("cisco.ios", "newcls", "core_switch", "192.0.2.41", backend=backend,
                                      backends=registry, vectors=[])
    assert plan["error"] is None and plan["module_reuse"] is False
    made = scaffold.build_scaffold("cisco.ios", "scaf", "core_switch", "network", backend, backends=registry)
    for field in onboard._ADOPTED:
        assert onboard._adopted(plan["module"], field, "newcls") == plan["module"][field]
        assert onboard._adopted(made["module"], field, "scaf") == made["module"][field]
    assert plan["module"]["role"] == made["module"]["role"] == "my_role"    # the two writers agree on the role


def test_no_identity_knob_stores_a_name_the_adoption_rule_refuses(reuse, monkeypatch):
    """Every word the identity planner accepts as a NEW value of an adopted key passes the adoption rule — over
    every string of one or two characters from a small alphabet (a letter, a digit, `_`, `-`, `.`, a capital, a
    space), a handful of three-character shapes, the YAML keywords, the reserved names and the two lengths around
    the bound — and each knob accepts the shortest name and the longest, so the comparison is not empty.

    Guards the fault this change is named for, as a property instead of one example: a request storing, through
    the identity knob, a name the next onboard of that class then refuses to adopt — the adoption rule, and for
    `role` the made-up-role rule too (the knob refuses `backend_vendor_config` and `backend_napalm`, which the next
    onboard would answer 409). It does NOT cover a stored backend that declares no role: on a class whose module file
    names no role either, a backend the registry does not ship, or one with no role, is accepted here and answered 409
    at the next onboard that names no backend (SECURITY.md C22, limits)."""
    import itertools
    reuse()
    descriptors = catalog.load_knob_descriptors()          # read once: the planner re-reads them on every call
    monkeypatch.setattr(catalog, "load_knob_descriptors", lambda *a, **k: descriptors)
    words = ["".join(t) for n in (1, 2) for t in itertools.product("a0_-.A ", repeat=n)]
    words += ["a-b", "a.b", "a b", "0ab", "_ab", "ab-", "a0_", "A01"]
    words += ["null", "true", "no", "yes", "on", "off", "1e3", "all", "ungrouped", "localhost", "a" * 64, "a" * 65]
    registry = catalog.load_backends()
    made_up = catalog.made_up_roles(registry)
    words += made_up
    for field in onboard._ADOPTED:
        accepted = []
        for word in words:
            plan = identity.build_plan("demo_sw", {"values": {field: word}})
            if plan["error"] is None and plan["changes"]:
                accepted.append(word)
                onboard._adopted({field: word}, field, "demo_sw")       # raises if the next onboard would refuse it
                if field == "role":
                    onboard._no_made_up_role(word, "demo_sw", registry)
        assert "a" in accepted and "a" * 64 in accepted, "the %s knob accepted nothing worth comparing" % field
    assert made_up, 'the registry ships no role-less backend: the made-up-role words compare nothing'


# --- one type for "the repository's own data stopped the request" ---------------------------------------------------

def test_a_write_target_outside_the_root_is_repository_data_not_a_field(tmp_path, monkeypatch):
    """`confined()` refuses with `RepositoryDataError` — not a ValueError, not a field refusal — carrying status
    409, the code `repository_data`, `what == "path"` and one fixed sentence without the path.

    Guards both apps answering a server-built path that does not confine (a symlinked overlay, a bug) as 422
    `invalid_path`: nothing the caller sent is wrong."""
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))
    with pytest.raises(paths.RepositoryDataError) as stop:
        paths.confined("../Zz-outside.yml")
    exc = stop.value
    assert not isinstance(exc, (ValueError, paths.RequestBoundaryError))
    assert (exc.status, exc.code, exc.what) == (409, "repository_data", "path")
    assert "Zz-outside" not in str(exc) and "layout" in str(exc)


def test_drop_refused_still_skips_a_path_it_cannot_confine(tmp_path, monkeypatch):
    """`gitio._drop_refused` returns without raising for a path that does not confine, and hands only the file
    that is inside the write root to `git clean`.

    Guards the one behavioural trap of the type change: the clean-up used to rely on `confined()` raising a
    ValueError; with the new type and the old `except`, a path the clean-up skips today would become an exception
    out of `commit_and_push`."""
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))
    (tmp_path / "ok.yml").write_text("x: 1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    gitio._drop_refused(["../x.yml", "ok.yml", "absent.yml"])
    assert len(calls) == 1 and calls[0][-1] == "ok.yml" and "../x.yml" not in calls[0]
    assert paths.confined_file("../x.yml") is False and paths.confined_file("ok.yml") is True


@pytest.mark.parametrize("second_device", [False, True], ids=["new-class", "reuse"])
def test_apply_confines_the_targets_it_will_write_before_its_first_write(reuse, monkeypatch, second_device):
    """`apply_onboard_plan` asks `confined()` about every drop-in target it is going to write before any writer
    runs — the module target and the inventory target for a new class, the inventory target alone for a second
    device of an existing one — so a layout the confinement refuses leaves nothing behind.

    Guards the partial write: the module file used to be written first, and the inventory write then refused —
    "before any write" was not true of a symlinked inventory directory. And guards the opposite fault: a reuse
    refused over a module target it never writes (a tree whose `modules/` resolves elsewhere took second devices
    before this check existed)."""
    if second_device:
        reuse()
        plan = _plan()
    else:
        plan = onboard.build_onboard_plan("cisco.ios", "newcls", "core_switch", "192.0.2.41", host_name="sw9")
    assert plan["error"] is None and plan["module_reuse"] is second_device
    asked, wrote = [], []
    real = paths.confined

    def confined(rel):
        asked.append(rel)
        if rel == plan["inv_path"]:
            raise paths.RepositoryDataError("path", "stop")
        return real(rel)
    monkeypatch.setattr(paths, "confined", confined)
    for writer in ("_write_new", "write_inventory_host"):
        monkeypatch.setattr(gitio, writer, lambda *a, **k: wrote.append(a) or True)
    monkeypatch.setattr(onboard, "enable_in_fleet", lambda *a, **k: wrote.append(a) or True)
    with pytest.raises(paths.RepositoryDataError):
        onboard.apply_onboard_plan(plan)
    assert asked == ([plan["inv_path"]] if second_device else [plan["mod_path"], plan["inv_path"]]) and wrote == []


# --- a group Ansible reserves ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("word", RESERVED)
def test_a_reserved_group_is_refused_wherever_a_group_is_chosen(reuse, monkeypatch, tmp_repo, word):
    """`all`, `ungrouped` and `localhost` are refused as the group of a device class at every seam that chooses
    one: the onboard field (a field refusal, before the probe), the scaffold's argument, the identity knob (a
    `bad_value`), and a group adopted from a module file (repository data).

    Guards a class addressing every host wherever its group is a play's `hosts:` pattern (the logging wiring
    play, the backup schedule's limit, a unit's play), and a group named `localhost` capturing the plays written
    for the control node."""
    with pytest.raises(paths.RequestBoundaryError) as stop:
        paths.inventory_group(word)
    assert stop.value.field == "inventory group" and "reserves" in str(stop.value)
    reuse(inventory_group=word)                       # adopted from a module file: the file's fault
    with pytest.raises(paths.RepositoryDataError):
        _plan()
    reuse()                                           # the identity knob: a bad value, nothing planned
    plan = identity.build_plan("demo_sw", {"values": {"inventory_group": word}})
    assert plan["error"] == "bad_value" and plan["field"] == "inventory_group" and "reserves" in plan["detail"]
    probed = []                                       # the onboard field and the scaffold: before the probe
    monkeypatch.setattr(probe, "deep_probe", lambda *a, **k: probed.append(1) or {"modules": [], "plugins": []})
    with pytest.raises(paths.RequestBoundaryError):
        onboard.build_onboard_plan("acme.edgeos", "edgeos", word, "192.0.2.50", backends=[], vectors=[])
    with pytest.raises(paths.RequestBoundaryError):
        scaffold.build_scaffold("acme.edgeos", "edgeos", word, "network", backends=[])
    assert probed == [], "a refused group must not reach the probe"


@pytest.mark.parametrize("group", ["core_switch", "lab_switches", "hosts", "children", "vars"])
def test_an_operators_own_group_is_accepted(group):
    """A functional group of the tree, a new group of the operator's, and the three words that are keys of a group
    mapping (which Ansible and every reader of this tree treat as ordinary group names) all pass. Guards the
    deny-list growing into an allow-list, or into a rule with no failure behind it."""
    assert paths.inventory_group(group) == group


def test_no_shipped_class_uses_a_reserved_group():
    """No shipped module's group, and no group of the shipped example inventory a class could be attached to, is a
    reserved name. Guards the rule refusing a shipped tree."""
    for path in sorted(glob.glob(os.path.join(ROOT, "modules", "*", "module.yml"))):
        with open(path, encoding="utf-8") as fh:
            group = (yaml.safe_load(fh) or {}).get("inventory_group")
        assert group is None or paths.inventory_group(group) == group, path
    with open(os.path.join(ROOT, "instance.example", "inventory", "hosts.yml"), encoding="utf-8") as fh:
        children = ((yaml.safe_load(fh) or {}).get("all") or {}).get("children") or {}
    assert children and not set(children) & paths.RESERVED_NAMES


@pytest.mark.parametrize("word", RESERVED + ["127.0.0.1", "::1"])
def test_a_reserved_name_is_refused_as_a_hosts_inventory_name(monkeypatch, word):
    """`all`, `ungrouped`, `localhost`, `127.0.0.1` and `::1` are refused as the NAME a host is given in the
    inventory, as the `host_name` field and before the probe; the same word as the ADDRESS (`host`) is not a name
    of the inventory and is accepted, and an ordinary address may still be a host's name.

    Guards the capture through the field next to the group: an inventory host named `localhost` — or by either
    loopback address, which are the same host to Ansible — replaces the implicit control node, so every play
    written for the control node runs against the device; and with a host named `all` the pattern `all`
    addresses that one host (each measured with ansible-core)."""
    probed = []
    monkeypatch.setattr(probe, "deep_probe", lambda *a, **k: probed.append(1) or {"modules": [], "plugins": []})
    with pytest.raises(paths.RequestBoundaryError) as stop:
        onboard.build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50", host_name=word,
                                   backends=[], vectors=[])
    assert stop.value.field == "host_name" and "reserves" in str(stop.value) and probed == []
    assert paths.hostname(word, "host") == word                       # as an address it is only an address
    plan = onboard.build_onboard_plan("acme.edgeos", "edgeos", "edge_router", word, backends=[], vectors=[])
    assert plan["error"] == "not_installed", "an address is not a name: it reaches the probe"
    for name in ("sw1", "192.0.2.7", "2001:db8::1", "Localhost"):     # the rule is these exact names, no more
        assert paths.inventory_host(name) == name
    assert not {"127.0.0.1", "::1"} & paths.RESERVED_NAMES, "a group cannot carry an address; the set is the host's"


# --- the scaffold CLI and the recipe name ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"key": "../x"}, {"key": "a/b"}, {"key": "null"}, {"key": "edgeos\n"}, {"group": "edge router"},
    {"group": "all"}, {"group": "123"}, {"secrets": "x') ~ y"}, {"secrets": "../network"}, {"secrets": "null"},
    {"backend": "no"},
    {"backend": "raw ssh"}, {"collection": "acme/edgeos"},
], ids=repr)
def test_scaffold_refuses_what_the_planner_refuses(monkeypatch, kwargs):
    """`build_scaffold` refuses a key, group, secrets domain, backend or collection the onboard planner would
    refuse, with the planner's field name, before the probe is reached.

    Guards a second writer of a class's identity keys with a looser rule than the first: the file the scaffold
    writes is what a later onboard adopts, and its `--key` used to be joined into a path unread — `../../x` wrote
    outside the repository."""
    monkeypatch.setattr(probe, "deep_probe", lambda *a, **k: pytest.fail("the probe ran for a refused argument"))
    args = dict(collection="acme.edgeos", key="edgeos", group="edge_router", secrets="network", backend=None)
    args.update(kwargs)
    with pytest.raises(paths.RequestBoundaryError):
        scaffold.build_scaffold(args["collection"], args["key"], args["group"], args["secrets"], args["backend"],
                                backends=[])


def test_the_cli_prints_a_repository_refusal_as_one_line(tmp_repo, monkeypatch, make_facts):
    """`galaxy.py scaffold` whose write target the confinement refuses exits with the refusal's one sentence, not
    with a traceback.

    Guards the command line treating a refusal that is the repository's own like a crash: `main()` catches the
    request refusal and the write conflict, and this type is neither — it is deliberately not a ValueError."""
    import galaxy
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "1.0.0"})
    monkeypatch.setattr(galaxy, "build_scaffold", lambda *a, **k: scaffold.build_scaffold(
        *a, backends=[{"name": "netcommon_cli", "role": "backend_netcommon_cli", "requires": [],
                       "default_secrets_domain": "network", "match": {"any": [{"plugin": "cliconf"}]}}]))

    def refuse(rel_path):
        raise paths.RepositoryDataError("path", "a write target does not resolve inside the repository")
    monkeypatch.setattr(paths, "confined", refuse)
    monkeypatch.setattr(galaxy.sys, "argv", ["galaxy.py", "scaffold", "cisco.ios", "--key", "ios_x",
                                             "--group", "core_switch", "--backend", "netcommon_cli"])
    with pytest.raises(SystemExit) as stop:
        galaxy.main()
    assert "does not resolve inside the repository" in str(stop.value.code)


def test_cmd_scaffold_writes_under_the_write_root(tmp_path, monkeypatch, make_facts, capsys):
    """`galaxy.py scaffold` writes the module file under the write root, through the confined module target, and
    creates nothing under the code root; and a dry-run for a key no class has says nothing about a refusal.

    Guards the write to the code root: on a baked install that is the read-only image tree, and it was the one
    module write that passed no confinement."""
    import galaxy
    code_root = tmp_path / "code"
    write_root = tmp_path / "write"
    (code_root / "modules").mkdir(parents=True)
    write_root.mkdir()
    monkeypatch.setattr(paths, "ROOT", str(code_root))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "1.0.0"})
    monkeypatch.setattr(galaxy, "build_scaffold", lambda *a, **k: scaffold.build_scaffold(
        *a, backends=[{"name": "netcommon_cli", "role": "backend_netcommon_cli", "requires": [],
                       "default_secrets_domain": "network", "match": {"any": [{"plugin": "cliconf"}]}}]))
    galaxy.cmd_scaffold(argparse.Namespace(collection="cisco.ios", key="ios_x", group="core_switch",
                                           secrets="network", backend="netcommon_cli", dry_run=True))
    assert "a real run refuses" not in capsys.readouterr().out      # the note is only true when the class exists
    galaxy.cmd_scaffold(argparse.Namespace(collection="cisco.ios", key="ios_x", group="core_switch",
                                           secrets="network", backend="netcommon_cli", dry_run=False))
    assert (write_root / "modules" / "ios_x" / "module.yml").exists()
    assert not (code_root / "modules" / "ios_x").exists()


def test_cmd_scaffold_with_a_blank_domain_takes_the_backend_default(tmp_path, monkeypatch, make_facts, capsys):
    """`galaxy.py scaffold --secrets ""` writes the class with the backend's `default_secrets_domain`, as an omitted
    flag does: blank means omitted, as in `onboard`, the API and the GUI.

    Guards the scaffold half of T18's read F10 (mutant M6 of its fold read passed the blank through, and the
    scaffold refused it as a domain that is not a name)."""
    import galaxy
    (tmp_path / "modules").mkdir()
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(tmp_path))
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "1.0.0"})
    monkeypatch.setattr(galaxy, "build_scaffold", lambda *a, **k: scaffold.build_scaffold(
        *a, backends=[{"name": "netcommon_cli", "role": "backend_netcommon_cli", "requires": [],
                       "default_secrets_domain": "lab", "match": {"any": [{"plugin": "cliconf"}]}}]))
    for secrets in ("", None):
        galaxy.cmd_scaffold(argparse.Namespace(collection="cisco.ios", key="ios_x", group="core_switch",
                                               secrets=secrets, backend="netcommon_cli", dry_run=True))
        assert "secrets_domain: lab" in capsys.readouterr().out


def test_cmd_scaffold_does_not_shadow_a_class_the_code_root_ships(tmp_path, monkeypatch, make_facts, capsys):
    """`galaxy.py scaffold` for a key whose class exists under the CODE root only — a shipped class on an install
    whose write root is elsewhere — refuses to overwrite, and creates nothing under the write root.

    Guards a stub written beside a shipped class: the write root's copy shadows the shipped one for every reader,
    so its `metrics:`, `backup:` and `logs:` blocks would vanish from every plan (the defect #124 was about). The
    no-clobber test used to look where the file is WRITTEN; a reader looks in two places."""
    import galaxy
    code_root = tmp_path / "code"
    write_root = tmp_path / "write"
    (code_root / "modules" / "ios_x").mkdir(parents=True)
    (code_root / "modules" / "ios_x" / "module.yml").write_text("key: ios_x\nmetrics: []\n", encoding="utf-8")
    write_root.mkdir()
    monkeypatch.setattr(paths, "ROOT", str(code_root))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "1.0.0"})
    monkeypatch.setattr(galaxy, "build_scaffold", lambda *a, **k: scaffold.build_scaffold(
        *a, backends=[{"name": "netcommon_cli", "role": "backend_netcommon_cli", "requires": [],
                       "default_secrets_domain": "network", "match": {"any": [{"plugin": "cliconf"}]}}]))
    with pytest.raises(SystemExit) as stop:
        galaxy.cmd_scaffold(argparse.Namespace(collection="cisco.ios", key="ios_x", group="core_switch",
                                               secrets="network", backend="netcommon_cli", dry_run=False))
    assert "refusing to overwrite" in str(stop.value.code)
    assert not (write_root / "modules").exists()
    capsys.readouterr()
    galaxy.cmd_scaffold(argparse.Namespace(collection="cisco.ios", key="ios_x", group="core_switch",
                                           secrets="network", backend="netcommon_cli", dry_run=True))
    assert "a real run refuses" in capsys.readouterr().out          # the dry-run says what the real run does
    assert not (write_root / "modules").exists()


def test_the_cli_onboard_names_the_group_and_the_domain_the_plan_adopted(reuse, monkeypatch, capsys):
    """`galaxy.py onboard --apply --commit` for a second device of a class, with another group and another domain
    on the command line, prints the ADOPTED secrets file and commits with the ADOPTED group in the body.

    Guards the third seat of the commit-body word (the two routes were corrected first): a commit message, and a
    step of the printed plan, that say a host and its credentials went somewhere they did not go."""
    import galaxy
    reuse(inventory_group="lab_switches", secrets_domain="lab")
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {})
    monkeypatch.setattr(gitio, "sops_write_domain", lambda d, m: True)
    monkeypatch.setattr(galaxy.sys, "argv", [
        "galaxy.py", "onboard", "cisco.ios", "--key", "demo_sw", "--group", "some_other_group", "--host",
        "192.0.2.40", "--host-name", "sw2", "--secrets", "other", "--password", "pw", "--apply", "--commit"])
    galaxy.main()
    out = capsys.readouterr().out
    assert "instance/secrets/lab.sops.yml" in out and "other.sops.yml" not in out
    commit = next(c for c in calls if c[:2] == ["git", "commit"])
    assert any("in group lab_switches" in word for word in commit)
    assert not any("some_other_group" in word for word in commit)


@pytest.mark.parametrize("name", ["../x", "a/b", "My Recipe", "x\n"])
def test_openapi_recipe_name_is_one_component(name):
    """An explicit recipe name that is not one path component is refused; a derived name and a plain one pass.
    Guards a recipe file written outside the recipes directory by `galaxy.py openapi --name`."""
    spec = {"info": {"title": "Demo API"}, "paths": {}}
    with pytest.raises(paths.RequestBoundaryError):
        openapi.build_openapi_recipe(spec, name=name)
    assert openapi.build_openapi_recipe(spec)["name"] == "demo_api"
    assert openapi.build_openapi_recipe(spec, name="demo")["name"] == "demo"


@pytest.mark.parametrize("overwrite", [False, True])
def test_apply_does_not_shadow_a_class_the_code_root_ships(tmp_path, monkeypatch, make_facts, overwrite):
    """On an install whose write root is not its code root, onboarding under the key of a class the code root
    ships for ANOTHER collection is a write conflict at apply — nothing is written under the write root — unless
    the caller passes `overwrite`, which writes it.

    Guards a request shadowing a shipped class: the write root's copy is what every reader then reads, so the
    class's role, backend and group would change for the hosts it already has and its curated blocks would be
    gone (#124). A one-root tree already answered the conflict, and so did a write root that holds the class;
    this is the write root that does not — a clone older than its image."""
    code_root, write_root = tmp_path / "code", tmp_path / "write"
    (code_root / "modules" / "cisco_ios").mkdir(parents=True)
    (code_root / "modules" / "cisco_ios" / "module.yml").write_text(yaml.safe_dump({
        "key": "cisco_ios", "collections": [{"name": "cisco.ios", "version": ">=1.0.0"}], "role": "backend_x",
        "backend": "netcommon_cli", "inventory_group": "core_switch", "secrets_domain": "network",
        "backup": [{"method": "config_capture"}]}), encoding="utf-8")
    (write_root / "ansible" / "inventory").mkdir(parents=True)
    (write_root / "config").mkdir()
    (write_root / "config" / "fleet.yml").write_text("enabled_modules:\n  - docker_host\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ROOT", str(code_root))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, plugins={"cliconf": ["ios"]}, modules=["edge_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.edgeos": "1.0.0"})
    plan = onboard.build_onboard_plan("acme.edgeos", "cisco_ios", "edge_router", "192.0.2.60", backends=[dict(_CLI)],
                                      vectors=[])
    assert plan["error"] is None and plan["module_reuse"] is False
    target = write_root / "modules" / "cisco_ios" / "module.yml"
    if not overwrite:
        with pytest.raises(gitio.WriteConflict):
            onboard.apply_onboard_plan(plan)
        assert not target.exists() and not (write_root / "ansible" / "inventory" / "onboarded-cisco_ios.yml").exists()
        return
    onboard.apply_onboard_plan(plan, overwrite=True)
    assert "acme.edgeos" in target.read_text(encoding="utf-8")


def test_apply_takes_a_second_device_of_a_class_only_the_code_root_holds(tmp_path, monkeypatch, make_facts):
    """On an install whose write root is not its code root and does not hold the class, a SECOND device of a
    class the code root ships — the same collection, so a reuse — is applied: the inventory drop-in is written
    under the write root, no module file is, and nothing is a conflict.

    Guards the reader's-view conflict being asked of a reuse as well: a reuse writes no module file, so there is
    nothing to shadow, and asking would refuse every second device of a shipped class on a clone older than its
    image — with a message that tells the operator to pick another key."""
    code_root, write_root = tmp_path / "code", tmp_path / "write"
    (code_root / "modules" / "cisco_ios").mkdir(parents=True)
    (code_root / "modules" / "cisco_ios" / "module.yml").write_text(yaml.safe_dump({
        "key": "cisco_ios", "collections": [{"name": "cisco.ios", "version": ">=1.0.0"}], "role": "backend_x",
        "backend": "netcommon_cli", "inventory_group": "core_switch", "secrets_domain": "network"}),
        encoding="utf-8")
    (write_root / "ansible" / "inventory").mkdir(parents=True)
    (write_root / "config").mkdir()
    (write_root / "config" / "fleet.yml").write_text("enabled_modules:\n  - cisco_ios\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ROOT", str(code_root))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "1.0.0"})
    plan = onboard.build_onboard_plan("cisco.ios", "cisco_ios", "core_switch", "192.0.2.61", host_name="sw2",
                                      backends=[dict(_CLI)], vectors=[])
    assert plan["error"] is None and plan["module_reuse"] is True
    onboard.apply_onboard_plan(plan)
    assert not (write_root / "modules").exists()
    assert "sw2" in (write_root / plan["inv_path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("keys", [{"collections": 5}, {"collections": [{"name": ["x"]}]},
                                  {"auth": [{"field": "u", "kind": "identity", "auth_set": ["s"], "required": True}]}],
                         ids=["collections-a-number", "a-collection-name-a-list", "auth-set-a-list"])
def test_a_module_file_with_a_malformed_block_is_not_a_crash(reuse, keys):
    """A module file whose `collections` block is not a list, or holds a name that is not a string, declares no
    collection — the plan is built as for a class of another collection — and an `auth:` entry whose `auth_set`
    is a list is used without it. None of them raises.

    Guards the 500 each of these files was on both apps (a TypeError where the planner collects names into a
    set): data the tool reads back from a file must degrade, not crash a request."""
    reuse(**keys)
    plan = _plan(creds={"u": "admin"}) if "auth" in keys else _plan()
    assert plan["error"] is None
    assert plan["module_reuse"] is ("auth" in keys)
