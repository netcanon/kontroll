"""A device class is planned only with a backend some play can run: the role comes from declared data, not from a name.

WHY (S26, the 2026-10-12 plan's brief 14 §3): the onboard planner and the scaffold made the role up
(`backend_<name>`) for any backend whose registry entry declares no `role:` — `vendor_config` and `napalm` declare
none — and for any backend name a request sent that the registry does not ship at all. The made-up name was written
into the module file and into every host's `device_role`; `ping.yml` then included a role that does not exist, its
failure rescued into a bare UNREACHABLE, and `backup-configs.yml` failed the same way. The auto path reached it
without anyone naming a backend: a collection with a `*_config` module that takes a `backup` option and no cliconf
plugin classifies to `vendor_config` first, and the GUI's onboard form sent the search record's first match as the
backend. These rows hold the role to the registry: a role a backend declares names a role a play can include; the
planner and the scaffold plan a new class only with such a backend, and refuse a request that names any other
before probing; a reused class keeps the role its own module file declares (and only such a class can run with a
backend that declares none); and the suggestion the form sends is what the planner would choose.
"""
import os

import pytest
import yaml

from kontroll import catalog, paths, probe, record
from kontroll.service import classify, onboard, scaffold

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY = catalog.load_backends()
ROLE_LESS = [b["name"] for b in REGISTRY if catalog.backend_role(b) is None]
# A collection with a vendor `*_config` module that takes a `backup` option: what `vendor_config` classifies on.
VENDOR_CONFIG = dict(modules=["x_config"], module_options={"x_config": ["backup"]})


@pytest.fixture
def probed(tmp_repo, monkeypatch, make_facts):
    """The planner's two I/O seams canned: `probed(**facts)` sets what the probe returns for any collection, and
    returns the list of collections probed — so a row can tell a refusal before the probe from one after it."""
    seen = []

    def canned(**facts):
        monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: seen.append(coll) or make_facts(
            collection=coll, **facts))
        return seen
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    return canned


def _plan(**kw):
    return onboard.build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50", vectors=[], **kw)


def _curated(tmp_repo, **fields):
    """A curated `edgeos` class already in the repository, declaring `acme.edgeos` — what a second device reuses."""
    target = tmp_repo / "modules" / "edgeos"
    target.mkdir(parents=True)
    (target / "module.yml").write_text(yaml.safe_dump(dict({
        "key": "edgeos", "collections": [{"name": "acme.edgeos", "version": ">=1.0.0"}],
        "inventory_group": "edge_router", "secrets_domain": "network"}, **fields)), encoding="utf-8")


def test_every_backend_role_names_a_role_with_check_and_backup():
    """Every role a shipped backend declares names a directory under `ansible/roles/` that has `tasks/check.yml` and
    `tasks/backup.yml`, and at least one backend declares one. Guards the linkage the planner now trusts: it writes a
    backend's declared role as every host's `device_role`, and `ping.yml` / `backup-configs.yml` include that role's
    check and backup — a typo'd or missing role would pass every other row and fail only at play time."""
    declared = [(b["name"], catalog.backend_role(b)) for b in REGISTRY if catalog.backend_role(b) is not None]
    assert declared, "no shipped backend declares a role"
    for name, role in declared:
        for entry in ("check.yml", "backup.yml"):
            assert os.path.isfile(os.path.join(ROOT, "ansible", "roles", role, "tasks", entry)), (name, role, entry)


def test_the_capability_matrix_says_which_backends_run():
    """docs/capability-matrix.md §5.1's "runs today?" column agrees with the registry: "yes" for each backend that
    declares a role, "classifier only" for each that ships with none, "not built" for a name with no directory —
    and every shipped backend has a row. Guards the doc a reader picks a backend from going stale the day a role
    ships for `vendor_config` or `napalm`, or a new backend lands without a row (CLAUDE.md: a fact in prose needs a
    check in the same commit)."""
    import re
    text = open(os.path.join(ROOT, "docs", "capability-matrix.md"), encoding="utf-8").read()
    rows = dict(re.findall(r"^\| `([a-z_]+)`[^|]*\|[^|]*\|[^|]*\| ([^|]+?) \|$", text, re.M))
    shipped = {b["name"]: catalog.backend_role(b) for b in REGISTRY}
    assert set(shipped) <= set(rows), "a shipped backend has no §5.1 row: %s" % sorted(set(shipped) - set(rows))
    for name, says in rows.items():
        if name not in shipped:
            assert says == "not built", (name, says)
            assert not os.path.isdir(os.path.join(ROOT, "ansible", "backends", name)), name
        elif shipped[name] is None:
            assert says.startswith("classifier only"), (name, says)
        else:
            assert says == "yes", (name, says)


@pytest.mark.parametrize("backend", ROLE_LESS)
def test_the_planner_refuses_every_backend_without_a_role(probed, backend):
    """A request that names a shipped backend declaring no role — every one in the real registry, so the row cannot
    go stale — is refused as the `backend` field, the value not in the message, and nothing is planned. Guards a class
    being written with a `device_role` no play can include (the planner made `backend_<name>` up) — and the refusal
    coming after the probe, so an uninstalled collection answered `not_installed` to a request whose own field was
    the fault (the docs say the field is refused first)."""
    seen = probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    with pytest.raises(paths.RequestBoundaryError) as stop:
        _plan(backend=backend)
    assert stop.value.field == "backend" and backend not in str(stop.value)
    assert seen == [], "a refused request field is answered before anything is probed"


@pytest.mark.parametrize("backend", ["zzz", "backend_api", "netcommon"])
def test_the_planner_refuses_a_backend_the_registry_does_not_ship_before_probing(probed, backend):
    """A `backend` that names no registry entry is refused as the `backend` field before the collection is probed,
    the value not in the message. Guards a request string becoming a role name: `zzz` was planned as
    `backend_zzz`, with no connection vars and the generic credential union, and written."""
    seen = probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    with pytest.raises(paths.RequestBoundaryError) as stop:
        _plan(backend=backend)
    assert stop.value.field == "backend" and backend not in str(stop.value)
    assert "not one this install ships" in str(stop.value), "the registry check, not the role check, answers it"
    assert seen == [], "a refused request field is answered before anything is probed"


@pytest.mark.parametrize("backend", ["zzz", "backend_api"])
def test_a_reused_class_with_its_own_role_still_refuses_a_backend_the_registry_does_not_ship(probed, tmp_repo,
                                                                                             backend):
    """A request that names a backend the registry does not ship is refused as the `backend` field before the probe
    even for a second device of a curated class with its own role — where the role check is skipped, so the registry
    check is the only guard. Guards a reuse planning with `backend: zzz` and no connection vars, against the 422 the
    API documents for any unshipped name."""
    seen = probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    _curated(tmp_repo, role="edgeos_own")
    with pytest.raises(paths.RequestBoundaryError) as stop:
        _plan(backend=backend)
    assert stop.value.field == "backend" and "not one this install ships" in str(stop.value) and seen == []


@pytest.mark.parametrize("facts, chosen", [
    (VENDOR_CONFIG, None),
    (dict(VENDOR_CONFIG, plugins={"httpapi": ["x"]}), "api"),
    (dict(VENDOR_CONFIG, plugins={"cliconf": ["x"]}), "netcommon_cli"),
], ids=["only-vendor-config", "vendor-config-then-api", "cliconf-first"])
def test_the_auto_path_passes_over_a_match_without_a_role(probed, facts, chosen):
    """With no `backend` asked for, the planner and the scaffold choose the first match that declares a role, in
    the classifier's order, and answer `no_backend` when none does — naming the match that cannot run. A collection
    that classifies only to `vendor_config` gets no plan; one that classifies to `vendor_config` and then `api` is planned with `api` and its
    role. Guards the auto path writing a broken class without anyone naming a backend: `vendor_config` (order 2) was
    the first match for such a collection, and it was chosen. `galaxy classify`'s `best` (service_classify) names
    the same backend, so the CLI does not recommend one the next step refuses."""
    probed(**facts)
    plan = _plan()
    made = scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", "network")
    assert classify.service_classify("acme.edgeos", backends=REGISTRY)["best"] == chosen
    if chosen is None:
        assert plan["error"] == made["error"] == "no_backend"
        assert plan["signals"]["unrunnable"] == made["signals"]["unrunnable"] == ["vendor_config"]
        return
    role = catalog.backend_role(next(b for b in REGISTRY if b["name"] == chosen))
    assert plan["error"] is None and (plan["backend"], plan["role"]) == (chosen, role)
    assert plan["host_block"]["edge_router"]["hosts"]["edgeos-1"]["device_role"] == role
    assert (made["backend"], made["module"]["role"]) == (chosen, role)


def test_the_scaffold_takes_the_role_from_the_registry(probed):
    """`build_scaffold` writes the role the named backend's registry entry declares, and refuses a backend that
    declares none (or that the registry does not ship) as the `backend` field. Guards the scaffold's role, which was
    the format string `backend_<name>` whatever the registry said."""
    probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    for entry in REGISTRY:
        role = catalog.backend_role(entry)
        if role is None:
            with pytest.raises(paths.RequestBoundaryError) as stop:
                scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", "network", entry["name"])
            assert stop.value.field == "backend"
        else:
            made = scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", "network", entry["name"])
            assert made["module"]["role"] == role, entry["name"]
    with pytest.raises(paths.RequestBoundaryError):
        scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", "network", "zzz")


@pytest.mark.parametrize("backend", ["zzz"] + ROLE_LESS)
def test_the_scaffold_refuses_a_backend_it_cannot_run_before_probing(probed, backend):
    """`build_scaffold` refuses a `backend` the registry does not ship, or one that declares no role, as the
    `backend` field before the collection is probed. Guards the order the docs state (a refused field is answered
    first), which nothing pinned: moving either check after the probe answered `not_installed` for an uninstalled
    collection, and deep-probed a collection for a request that was already refused."""
    seen = probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    with pytest.raises(paths.RequestBoundaryError) as stop:
        scaffold.build_scaffold("acme.edgeos", "edgeos", "edge_router", "network", backend)
    assert stop.value.field == "backend" and seen == []
    # each refusal says its own reason: an unshipped name is the registry check's, a role-less one the role check's
    assert ("not one this install ships" in str(stop.value)) == (backend == "zzz")


def test_a_reused_class_whose_file_names_a_backend_without_a_role_is_answered_before_probing(probed, tmp_repo):
    """A second device of a class whose module file names a backend that declares no role, and no role of its own,
    is repository data answered before the collection is probed — like every other fault of a reused class's file.
    Guards the one repository fault that still waited for the probe: an uninstalled collection answered
    `not_installed`, and the operator installed it only to meet the 409."""
    seen = probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    _curated(tmp_repo, backend=ROLE_LESS[0])
    with pytest.raises(paths.RepositoryDataError):
        _plan()
    assert seen == []


@pytest.mark.parametrize("facts, backend", [
    (VENDOR_CONFIG, "vendor_config"),
    (dict(VENDOR_CONFIG, plugins={"httpapi": ["x"]}), "api"),
], ids=["only-vendor-config", "vendor-config-then-api"])
def test_a_reused_class_with_its_own_role_plans_whatever_its_collection_classifies_to(probed, tmp_repo, facts,
                                                                                        backend):
    """A second device of a curated class whose module file declares its own role (and no backend) plans with that
    role even when the class's collection classifies only to a backend that declares none — with that match, the
    only one there is — and, when a match that declares a role follows it, with that one: the backend the search
    record suggests, so the GUI's form and the auto path agree. Guards the refusal reaching too far (a curated class
    brings its own execution role, so only "no role from anywhere" stops a plan), and the reuse choosing
    `vendor_config` over `api` where the form would have sent `api`."""
    probed(**facts)
    _curated(tmp_repo, role="edgeos_own")
    plan = _plan()
    assert plan["error"] is None and plan["module_reuse"] is True
    assert (plan["backend"], plan["role"]) == (backend, "edgeos_own")


@pytest.mark.parametrize("backend", ["netcommon_cli"] + ROLE_LESS)
def test_a_reused_class_keeps_its_own_role_whatever_backend_the_request_names(probed, tmp_repo, backend):
    """A request that names a shipped backend for a second device of a curated class with its own role plans with that
    backend and the CLASS's role — the host's `device_role` is the class's, whether the named backend declares a
    role (`netcommon_cli`) or none. Guards two things: the request's backend replacing the role every other host of
    the class runs (a class split across two roles), and a role-less backend being refused for a class that does
    not need its role (brief 14 §3.2)."""
    probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    _curated(tmp_repo, role="edgeos_own")
    plan = _plan(backend=backend)
    assert plan["error"] is None and (plan["backend"], plan["role"]) == (backend, "edgeos_own")
    assert plan["host_block"]["edge_router"]["hosts"]["edgeos-1"]["device_role"] == "edgeos_own"


@pytest.mark.parametrize("role", ["backend_%s" % name for name in ROLE_LESS])
def test_a_reused_class_with_a_made_up_backend_role_is_repository_data(probed, tmp_repo, role):
    """A curated class whose module file names the role the planner made up for a shipped backend that declares
    none — `backend_<name>` for every such backend in the real registry — stops the reuse as repository data, the
    role not in the message, nothing probed. Guards a class the old planner wrote (the auto path chose
    `vendor_config`) being adopted silently: every new host would get that `device_role`, which no play can include
    — with no backend named, and with one named, as the GUI's form always does. A backend role the registry declares
    (ios_xe_auto's `backend_netcommon_cli`) and an operator's own role that
    carries the prefix still plan — whether a role exists is the linker's question, not this guard's."""
    seen = probed(plugins={"cliconf": ["edgeos"]}, modules=["edgeos_command"])
    _curated(tmp_repo, role=role)
    with pytest.raises(paths.RepositoryDataError) as stop:
        _plan()
    assert role not in str(stop.value) and "modules/edgeos/module.yml" in str(stop.value) and seen == []
    with pytest.raises(paths.RepositoryDataError):
        _plan(backend="netcommon_cli")     # the GUI's shape: its form always sends the suggestion as `backend`
    for own in ("backend_netcommon_cli", "backend_site_own"):
        (tmp_repo / "modules" / "edgeos" / "module.yml").unlink()
        (tmp_repo / "modules" / "edgeos").rmdir()
        _curated(tmp_repo, role=own)
        assert _plan()["role"] == own


@pytest.mark.parametrize("facts, suggested", [
    (VENDOR_CONFIG, None),
    (dict(VENDOR_CONFIG, plugins={"httpapi": ["x"]}), "api"),
    (dict(VENDOR_CONFIG, plugins={"cliconf": ["x"]}), "netcommon_cli"),
], ids=["only-vendor-config", "vendor-config-then-api", "cliconf-first"])
def test_the_suggested_backend_is_the_one_the_planner_chooses(probed, make_facts, facts, suggested):
    """A search record's `suggested_backend` is the backend the planner chooses for the same facts (None where it
    chooses none) — named here, not only compared, so the two cannot drift together back to `vendor_config` — while
    `backend_candidates` keeps every match. Guards the GUI's onboard form, which sends the suggestion as the
    request's `backend` and loads its credential fields for it: suggesting `vendor_config` sent a backend the
    planner now refuses, and before that planned a class no play could run."""
    probed(**facts)
    rec = record.build_record(make_facts(collection="acme.edgeos", **facts), [], backends=REGISTRY)
    plan = _plan()
    assert rec["suggested_backend"] == plan.get("backend") == suggested
    assert "vendor_config" in rec["backend_candidates"]
