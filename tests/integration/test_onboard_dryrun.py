"""onboard as a black box — dry-run plans nothing; --apply mutates a tmp repo idempotently.

deep_probe/local_installed (the ansible-doc/ansible-galaxy shell-outs) are mocked, so
this runs with no ansible + no lab. kontroll.paths.ROOT is repointed at a throwaway repo
(tmp_repo), so --apply writes there, never the real tree. Credentials are deliberately
omitted (no --username/--password/--api-token) so the SOPS shell-out is never invoked —
the cred path is covered by the live VM e2e, not here.
"""
import os
import types

import pytest

import galaxy
from kontroll import catalog, gitio, probe
from kontroll.service.onboard import apply_onboard_plan, build_onboard_plan

pytestmark = pytest.mark.integration


def _args(**over):
    """Build the onboard args namespace (dry-run defaults), overriding per test."""
    base = dict(collection="acme.edgeos", key="edgeos", group="edge_router",
                host="192.0.2.50", host_name=None, secrets="network", backend=None,
                username=None, password=None, api_token=None,
                apply=False, commit=False, push=False, bootstrap=False)
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.fixture
def mock_probe(monkeypatch, make_facts):
    """Make every collection probe as a cliconf device -> netcommon_cli backend."""
    monkeypatch.setattr(probe, "deep_probe",
                        lambda coll, version=None: make_facts(
                            collection=coll, plugins={"cliconf": ["edgeos"]},
                            modules=["edgeos_command"]))
    monkeypatch.setattr(catalog, "local_installed", lambda: {})


def test_dryrun_writes_nothing(tmp_repo, mock_probe, capsys):
    """A bare onboard prints the full plan (module + drop-in host + fleet-enable) but writes
    NOTHING — the dry-run-by-default safety, asserted by an inert tmp repo."""
    galaxy.cmd_onboard(_args())
    out = capsys.readouterr().out
    assert "ONBOARD acme.edgeos" in out
    assert "netcommon_cli" in out               # the auto-chosen backend
    assert "[1] Device-class declaration" in out and "[2] Drop-in inventory host" in out
    # dry-run is inert: no module, no drop-in inventory, fleet unchanged
    assert not (tmp_repo / "modules" / "edgeos").exists()
    assert not (tmp_repo / "ansible" / "inventory" / "onboarded-edgeos.yml").exists()
    assert "- edgeos" not in (tmp_repo / "config" / "fleet.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("backend", ["napalm", "zzz"])
def test_cli_onboard_a_backend_nothing_can_run_exits_with_one_line(tmp_repo, mock_probe, monkeypatch, backend):
    """`galaxy.py onboard --backend <b> --apply`, where `b` declares no execution role or is not in the registry,
    exits non-zero with the refusal's one sentence (not a traceback, not quoting the value) and writes nothing.
    Guards CLI parity with the planner's refusal (S26): the command applied a class whose `device_role` no play can
    include."""
    monkeypatch.setattr(galaxy.sys, "argv", ["galaxy.py", "onboard", "acme.edgeos", "--key", "edgeos", "--group",
                                             "edge_router", "--host", "192.0.2.50", "--backend", backend, "--apply"])
    with pytest.raises(SystemExit) as stop:
        galaxy.main()
    assert isinstance(stop.value.code, str) and "backend" in stop.value.code and backend not in stop.value.code
    assert not (tmp_repo / "modules" / "edgeos").exists()
    assert "- edgeos" not in (tmp_repo / "config" / "fleet.yml").read_text(encoding="utf-8")


def test_apply_writes_module_inventory_and_enables_fleet(tmp_repo, mock_probe, capsys):
    """--apply writes the three artifacts — the device-class module, a drop-in inventory host
    (with the backend + mgmt IP), and the fleet-enable — the full self-completing onboard."""
    galaxy.cmd_onboard(_args(apply=True))
    capsys.readouterr()

    mod = tmp_repo / "modules" / "edgeos" / "module.yml"
    inv = tmp_repo / "ansible" / "inventory" / "onboarded-edgeos.yml"
    assert mod.exists() and inv.exists()
    assert "backend_netcommon_cli" in mod.read_text(encoding="utf-8")
    inv_text = inv.read_text(encoding="utf-8")
    assert "edgeos-1" in inv_text                          # default host name
    assert "192.0.2.50" in inv_text                        # the supplied mgmt IP
    assert "- edgeos" in (tmp_repo / "config" / "fleet.yml").read_text(encoding="utf-8")


def test_apply_is_idempotent(tmp_repo, mock_probe, capsys):
    """A second --apply with identical inputs is a no-op (files already current, fleet already
    enabled) — re-running onboard never duplicates or errors."""
    galaxy.cmd_onboard(_args(apply=True))
    capsys.readouterr()
    galaxy.cmd_onboard(_args(apply=True))                  # second apply
    out = capsys.readouterr().out
    assert "already current" in out                        # writer detected identical content
    assert "already enabled" in out                        # fleet insert was a no-op
    # still exactly one fleet entry
    assert (tmp_repo / "config" / "fleet.yml").read_text(encoding="utf-8").count("- edgeos") == 1


def _seed_class(tmp_repo, key="edgeos", collection="acme.edgeos"):
    """Pre-place a RICH curated device-class module (metrics:/backup: blocks, like a shipped class) so the
    next onboard of `collection` hits the reuse path. Returns the module file's path."""
    d = tmp_repo / "modules" / key
    d.mkdir(parents=True)
    mp = d / "module.yml"
    mp.write_text(
        "# a shipped, curated class\nkey: %s\ndescription: curated\nstatus: active\n"
        "collections:\n  - name: %s\n    version: \">=1.0.0\"\n"
        "role: %s\ninventory_group: edge_router\nsecrets_domain: network\n"
        "metrics:\n  - {method: snmp}\nbackup:\n  capable: true\n  schedule: \"0 2 * * *\"\n"
        % (key, collection, key), encoding="utf-8")
    return mp


def test_onboard_reuses_existing_class_not_clobbers_it(tmp_repo, mock_probe, capsys):
    """Onboarding a device whose CLASS already exists REUSES the curated module — it is left byte-for-byte
    unchanged (its metrics:/backup: blocks survive) and only the inventory host + fleet-enable are written.

    WHY (the live defect this guards): the operator's Overwrite replaced the rich shipped cisco_ios module with
    a 13-line minimal stub on .50 main, silently stripping its telemetry/backup capability and breaking the
    per-class backup auto-enroll (#124). Reuse means a 2nd device of a class never regenerates/clobbers it."""
    mp = _seed_class(tmp_repo)
    before = mp.read_text(encoding="utf-8")
    plan = build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50")
    assert plan["module_reuse"] is True
    apply_onboard_plan(plan)
    after = mp.read_text(encoding="utf-8")
    assert after == before                                  # the curated module is untouched
    assert "metrics:" in after and "backup:" in after       # its capability blocks survive
    inv = (tmp_repo / "ansible" / "inventory" / "onboarded-edgeos.yml").read_text(encoding="utf-8")
    assert "192.0.2.50" in inv                              # the new host landed
    assert "- edgeos" in (tmp_repo / "config" / "fleet.yml").read_text(encoding="utf-8")


def test_onboard_second_host_merges_keeps_first(tmp_repo, mock_probe, capsys):
    """A 2nd device of an existing class MERGES into the drop-in inventory — both hosts are kept, the 1st is
    never dropped. Guards the multi-host facet of #124 (an overwrite would have replaced host #1 with host #2)."""
    _seed_class(tmp_repo)
    apply_onboard_plan(build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50", host_name="edge-1"))
    apply_onboard_plan(build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.51", host_name="edge-2"))
    inv = (tmp_repo / "ansible" / "inventory" / "onboarded-edgeos.yml").read_text(encoding="utf-8")
    assert "edge-1" in inv and "edge-2" in inv              # both hosts present (no clobber)
    assert "192.0.2.50" in inv and "192.0.2.51" in inv


def test_second_generic_host_reuses_the_class_even_after_curation(tmp_repo, monkeypatch):
    """Hosts two and three onboarded through the Generic SSH host card (`ansible.builtin`, backend raw_ssh) REUSE
    the class host one created, even after the operator curated its module (a `backup:` block): the module stays
    byte-for-byte, all three hosts land in the drop-in, nothing is probed. Guards "one click" failing on host two: a
    collection-less class declares no collection to match, so the second onboard regenerated the module and hit a
    WriteConflict as soon as it had been curated (S24)."""
    def _refuse(*a, **k):
        raise AssertionError("the card's collection is never probed")
    monkeypatch.setattr(probe, "deep_probe", _refuse)

    def _plan(n):
        return build_onboard_plan("ansible.builtin", "ssh_host", "linux_hosts", "192.0.2.%d" % (70 + n),
                                  host_name="box-%d" % n, backend="raw_ssh")
    first = _plan(1)
    assert first["module_reuse"] is False and first["module"]["collections"] == []
    apply_onboard_plan(first)
    mp = tmp_repo / "modules" / "ssh_host" / "module.yml"
    mp.write_text(mp.read_text(encoding="utf-8") + "backup:\n  capable: true\n", encoding="utf-8")   # curated
    curated = mp.read_text(encoding="utf-8")
    for n in (2, 3):
        plan = _plan(n)
        assert plan["module_reuse"] is True, n
        apply_onboard_plan(plan)
    assert mp.read_text(encoding="utf-8") == curated
    inv = (tmp_repo / "ansible" / "inventory" / "onboarded-ssh_host.yml").read_text(encoding="utf-8")
    assert all(h in inv for h in ("box-1", "box-2", "box-3"))


def test_onboard_different_collection_on_same_key_still_conflicts(tmp_repo, mock_probe):
    """A DIFFERENT collection colliding on an existing key is NOT reuse (reuse keys on the collection matching):
    it still hard-stops with WriteConflict so the operator picks a fresh key, and only a deliberate overwrite
    replaces it — the loudly-warned escape hatch. Guards reuse from being mistaken for blanket no-clobber."""
    _seed_class(tmp_repo, key="edgeos", collection="other.os")   # existing module is a DIFFERENT collection
    plan = build_onboard_plan("acme.edgeos", "edgeos", "edge_router", "192.0.2.50")
    assert plan["module_reuse"] is False
    with pytest.raises(gitio.WriteConflict):
        apply_onboard_plan(plan)
    assert apply_onboard_plan(plan, overwrite=True)["changed"] is True   # deliberate replace


def test_apply_refuses_to_clobber_divergent_file(tmp_repo, mock_probe, capsys):
    """If the operator hand-edited a drop-in, a re-apply HARD-STOPS rather than overwriting their change — the
    no-clobber guarantee that protects local edits. The hard stop is a catchable `gitio.WriteConflict` (NOT a
    `sys.exit`/SystemExit): in-process the GUI/API turn it into a clean 409 instead of a worker-killing crash; the
    CLI's dispatch catches it and exits cleanly. (Same guard that fires when a key collides with a shipped module.)"""
    galaxy.cmd_onboard(_args(apply=True))
    capsys.readouterr()
    # operator hand-edits the drop-in; a re-apply must hard-stop, never overwrite
    inv = tmp_repo / "ansible" / "inventory" / "onboarded-edgeos.yml"
    inv.write_text("# hand-edited\nall: {}\n", encoding="utf-8")
    with pytest.raises(gitio.WriteConflict):
        galaxy.cmd_onboard(_args(apply=True))


def test_a_reuse_onboard_reports_the_group_it_adopted(tmp_repo, mock_probe, capsys):
    """A reuse-onboard whose request names another group than the class's own returns the ADOPTED group in the
    plan — the value both routes and the command line write into the commit body.

    Pins what their fix relies on, not the fix: the plan has always carried the adopted group (this passes on
    the tree before the change too). The fault was in the callers, which formatted the REQUEST's group into
    "Host … in group …"; the route tests and the command line's test hold that."""
    _seed_class(tmp_repo)
    plan = build_onboard_plan("acme.edgeos", "edgeos", "some_other_group", "192.0.2.50")
    assert plan["module_reuse"] is True
    adopted = next(iter(plan["host_block"]))
    assert plan["group"] == adopted and adopted != "some_other_group"
