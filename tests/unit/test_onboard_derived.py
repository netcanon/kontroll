"""F2 onboard wiring: a BLIND onboard of a fresh collection now writes the AUTO-DERIVED metrics:/logs: floor into
the new module (so gen-observability/gen-logging generate its scrape targets + Vector drop-in with zero further
operator action — the empty-Grafana/empty-Loki defect closed) and carries the HONEST capability_gap in the plan.

WHY (the failure these guard): pre-F2, a blind onboard produced a module with EMPTY capability blocks by design —
a managed-but-unmonitored device. These pin that a cliconf onboard now ships a derived floor, that the floor is
marked `derived: true` (so gen-class-capabilities owns it), and — the load-bearing one — that a derivation bug
DEGRADES to no floor + a logged warning, NEVER a failed onboard (INVARIANT D*: a secondary capability can never
gate onboarding). The derivation reads the REAL registries (no paths.ROOT repoint here), so it exercises the
shipped confer maps + derive_defaults end-to-end through the planner.
"""
import pytest

from kontroll import catalog, paths, probe
from kontroll.service import onboard

pytestmark = pytest.mark.unit


def _blind_plan(monkeypatch, plugins, modules, collection="arista.eos", key="arista_eos_auto", options=None, **kw):
    """A real onboard plan for a FRESH collection/key (no existing module ⇒ the non-reuse derive path), with a canned
    deep-probe (`options`: its module options, for a module_option signal). No paths.ROOT repoint ⇒
    derive_class_capabilities reads the SHIPPED telemetry/logging/backend registries (the point: prove the wiring
    against the real confer maps)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {collection: "1.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "1.0.0", "local", "deep")
        f["plugins"] = plugins
        f["modules"] = modules
        if options is not None:
            f["module_options"] = options
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)
    return onboard.build_onboard_plan(collection, key, "core_switch", "192.0.2.30",
                                      host_name="sw9", secrets="network", **kw)


def test_blind_cliconf_onboard_emits_the_derived_metrics_and_logs_floor(monkeypatch):
    """A blind network_cli onboard writes metrics [snmp(if_mib), blackbox(icmp)] + logs [syslog_push(tcp)] into the
    NEW module, each marked `derived: true`, plus the class-level `derived: true` flag — so the device is monitored
    + log-shippable on day one with zero curation. Guards the empty-Grafana/empty-Loki defect re-appearing."""
    plan = _blind_plan(monkeypatch, {"cliconf": ["eos"]}, ["eos_command"])
    mod = plan["module"]
    assert mod.get("derived") is True
    assert [m["method"] for m in mod["metrics"]] == ["snmp", "blackbox"]
    assert all(m.get("derived") for m in mod["metrics"])
    assert mod["metrics"][0]["params"] == {"module": "if_mib"}
    assert [m["method"] for m in mod["logs"]] == ["syslog_push"]
    assert mod["logs"][0]["params"] == {"transport": "tcp"} and mod["logs"][0].get("derived") is True
    assert plan["capability_gap"] == []                       # cisco-class cliconf gear is full-parity


def test_blind_proxmox_shaped_onboard_surfaces_the_pve_gap_non_gating(monkeypatch):
    """A proxmox-shaped device onboarded with `backend: raw_ssh` (module proxmox_kvm carries no signal a backend
    classifies on, so the backend is named — S24) derives the universal floor (blackbox + host_node) but the vendor
    `pve` exporter is NOT faked — it is named in the NON-GATING capability_gap (declare it to enable VM/LXC metrics).
    Guards both the honest gap surfacing and the never-fake invariant on the onboard path."""
    plan = _blind_plan(monkeypatch, {}, ["proxmox_kvm"], collection="community.proxmox", key="proxmox_auto",
                       backend="raw_ssh")
    assert plan["error"] is None and plan["backend"] == "raw_ssh"
    assert {m["method"] for m in plan["module"]["metrics"]} == {"blackbox", "host_node"}
    assert any(g["method"] == "pve" for g in plan["capability_gap"])


@pytest.mark.parametrize("plugins, modules, types", [
    ({"connection": ["kubectl"]}, ["k8s", "k8s_info"], "connection"),
    ({"connection": ["aws_ssm"], "inventory": ["aws_ec2"]}, ["ec2_instance"], "connection, inventory"),
    ({}, ["win_ping"], "none"),
], ids=["kubernetes", "cloud-sdk", "winrm"])
def test_blind_onboard_of_an_unrecognised_shape_is_refused_with_its_signals(monkeypatch, plugins, modules, types):
    """A blind onboard of a collection that carries no signal a backend classifies on is answered `no_backend` with
    its signals — the plugin TYPES it ships, its module count, no backend it classifies to — and the one sentence
    every surface answers with names those types and the remedy, never a plugin's NAME (third-party bytes).

    Guards the S24 misroute returning: such a collection (Kubernetes, a cloud SDK, WinRM) was planned as raw_ssh —
    SSH-as-root with a node_exporter floor — and the onboard "succeeded". And a refusal that names only a code."""
    plan = _blind_plan(monkeypatch, plugins, modules, collection="acme.paradigm", key="paradigm_auto")
    assert plan["error"] == "no_backend"
    assert plan["signals"] == {"plugin_types": types.split(", ") if types != "none" else [],
                               "modules": len(modules), "unrunnable": []}
    assert plan["reused_class"] is None and plan["declaring_class"] is None
    said = onboard.no_backend_message(plan, "acme.paradigm", "REMEDY")
    assert said.startswith("no backend kontroll can run fits acme.paradigm: nothing in it is a signal")
    assert "plugin types: %s; %d modules" % (types, len(modules)) in said and said.endswith("— REMEDY")
    for names in plugins.values():
        assert not any(n in said for n in names), "a plugin's name is third-party bytes — never in the answer"


def test_a_collection_a_class_declares_is_pointed_at_that_class(monkeypatch):
    """A `no_backend` onboard of a collection a shipped class declares WITH a backend, under another key, names that
    class: community.proxmox classifies to nothing, but the proxmox class declares `backend: raw_ssh`, and a reuse
    onboard of it adopts that backend. Guards the operator who searched "proxmox" being sent to the SSH card (or to
    `--backend`) when the one-click path is the class key."""
    plan = _blind_plan(monkeypatch, {}, ["proxmox_kvm"], collection="community.proxmox", key="proxmox_auto")
    assert plan["error"] == "no_backend" and plan["declaring_class"] == "proxmox"
    said = onboard.no_backend_message(plan, "community.proxmox", "REMEDY")
    assert said == ("no backend kontroll can run fits community.proxmox on its own, but the proxmox class declares it "
                    "with a backend — onboard it as device-class key proxmox")


def test_the_declaring_class_hint_survives_malformed_module_files(monkeypatch, tmp_repo):
    """The `no_backend` answer's declaring-class lookup reads past a module file that is a list, whose `collections` is
    not a list, that is not UTF-8, that holds a date no calendar has, or that nests past the recursion limit — named to
    sort before every other class, so the scan opens each — and names no class for a collection none declares (a full
    scan), nor for one declared by a file whose `key` is not a class key or is a word YAML reads as another type (the
    class `null`), whose `key` names a class whose own file declares another collection, or whose `key` names a class
    whose own file is not UTF-8. Guards every refusal turning into a 500 (or a 422 naming the codec) because one
    onboarded class's file is malformed — the lookup runs on each `no_backend` plan; a list file's first `.get` was an
    AttributeError — and the hint naming a key a request could not send, or one whose onboard would not reuse the class
    that declares the collection (it would answer `no_backend` and point at that key again)."""
    mods = tmp_repo / "modules"
    for key, text in (("aa_listy", "- a\n- b\n"), ("aa_odd", "key: aa_odd\ncollections: 5\n"),
                      ("aa_cp1252", b"key: aa_cp1252\ndescription: Caf\xe9\n"),
                      ("aa_baddate", "key: aa_baddate\nadded: 2026-02-30\n"),
                      ("aa_deep", "key: aa_deep\nx: " + "[" * 5000 + "]" * 5000 + "\n"),
                      ("aa_spaced", "key: a b\ncollections:\n  - {name: acme.spaced}\nbackend: raw_ssh\n"),
                      ("null", "key: 'null'\ncollections:\n  - {name: acme.null}\nbackend: raw_ssh\n"),
                      ("aa_elsewhere", "key: acme_other\ncollections:\n  - {name: acme.other}\nbackend: raw_ssh\n"),
                      ("acme_other", "key: acme_other\ncollections:\n  - {name: acme.z}\nbackend: raw_ssh\n"),
                      ("aa_pointer", "key: acme_garbled\ncollections:\n  - {name: acme.ptr}\nbackend: raw_ssh\n"),
                      ("acme_garbled", b"key: acme_garbled\ndescription: Caf\xe9\n")):
        (mods / key).mkdir()
        if isinstance(text, bytes):
            (mods / key / "module.yml").write_bytes(text)
        else:
            (mods / key / "module.yml").write_text(text, encoding="utf-8")
    for collection in ("acme.nothing", "acme.spaced", "acme.null", "acme.other", "acme.ptr"):
        plan = _blind_plan(monkeypatch, {}, ["thing_info"], collection=collection, key="thing_auto")
        assert plan["error"] == "no_backend" and plan["declaring_class"] is None, collection


_ACME = "key: acme_thing\ncollections:\n  - {name: acme.thing}\n"


_ROLELESS_SIGNAL = {"thing_config": ["backup"]}     # a module_option signal: classifies to vendor_config alone


@pytest.mark.parametrize("extra, named, reuse, options", [
    ("backend: raw_ssh\n", "acme_thing", "plans", None),
    ("backend: raw_ssh\nrole: thing\n", "acme_thing", "plans", None),
    ("backend: vendor_config\nrole: thing\n", "acme_thing", "plans", None),
    ("role: thing\n", None, "no_backend", None),
    ("backend: vendor_config\n", None, "repository_data", None),
    ("backend: raw_ssh\nrole: backend_vendor_config\n", None, "repository_data", None),
    ("backend: Bad\nrole: thing\n", None, "repository_data", None),
    ("backend: raw_ssh\nsecrets_domain: ../x\n", None, "repository_data", None),
    ("backend: raw_ssh\ninventory_group: all\n", None, "repository_data", None),
    ("backend: raw_ssh\nrole: Bad Role\n", None, "repository_data", None),
    ("backend: zzz\nrole: thing\n", None, "plans", None),
    ("role: thing\n", None, "plans", _ROLELESS_SIGNAL),
], ids=["runnable-backend", "own-role", "own-role-roleless-backend", "no-backend", "no-role-roleless-backend",
        "made-up-role", "adopted-backend-fails", "adopted-secrets-domain-fails", "adopted-inventory-group-fails",
        "adopted-role-fails", "unshipped-backend", "no-backend-own-role-roleless-match"])
def test_the_declaring_class_is_named_only_when_its_reuse_would_plan(monkeypatch, tmp_repo, extra, named, reuse,
                                                                     options):
    """A `no_backend` answer under another key names the class that declares the collection when an onboard under
    the class's key would plan — its backend runs, or it brings its own role (a role-less backend included) — and
    names none when that onboard is answered no_backend again (no backend), or as repository data (no role from
    anyone, a made-up role, any of the four names the reuse adopts breaking its rule). Two shapes whose reuse plans
    are not named, so the hint fails closed: a backend this install does not ship (the reuse plans with the class's
    own role — a fault the planner does not check yet), and no backend with the class's own role where the
    collection's one match is a backend that declares no role (the reuse takes that match, S26). Guards the hint
    sending the operator to a refusal of its own, and withholding a key that would plan without saying so."""
    (tmp_repo / "modules" / "acme_thing").mkdir()
    (tmp_repo / "modules" / "acme_thing" / "module.yml").write_text(_ACME + extra, encoding="utf-8")
    plan = _blind_plan(monkeypatch, {}, ["thing_info"], collection="acme.thing", key="thing_auto", options=options)
    assert plan["error"] == "no_backend" and plan["declaring_class"] == named
    try:
        again = _blind_plan(monkeypatch, {}, ["thing_info"], collection="acme.thing", key="acme_thing",
                            options=options)
        said = "plans" if again["error"] is None and again["module_reuse"] else again["error"]
    except paths.RepositoryDataError:
        said = "repository_data"
    assert said == reuse


def test_a_reused_class_without_a_backend_is_told_to_declare_one(monkeypatch, tmp_repo):
    """A second device of a class whose module file declares the collection but no backend, when the collection
    carries no signal, is refused with the sentence that names the class's file and the key to add — and not the
    surface's remedy (the Generic SSH host card under that key would meet the class's collection as a conflict).
    Guards the one refusal whose fix is a file edit being sent elsewhere, and the branch that says so going
    unnoticed (it had no test)."""
    (tmp_repo / "modules" / "acme_thing").mkdir()
    (tmp_repo / "modules" / "acme_thing" / "module.yml").write_text(
        "key: acme_thing\ncollections:\n  - {name: acme.thing}\nrole: thing\n", encoding="utf-8")
    plan = _blind_plan(monkeypatch, {}, ["thing_info"], collection="acme.thing", key="acme_thing")
    assert plan["error"] == "no_backend" and plan["reused_class"] == "acme_thing"
    said = onboard.no_backend_message(plan, "acme.thing", "REMEDY")
    assert said == ("no backend kontroll can run fits acme.thing: nothing in it is a signal a backend classifies on "
                    "(plugin types: none; 1 modules); the acme_thing class's module file declares no backend — add "
                    "one (`backend:` in modules/acme_thing/module.yml; `raw_ssh` if it is an SSH host)")


def test_the_cli_onboard_of_an_unrecognised_shape_exits_with_the_sentence(monkeypatch):
    """`galaxy onboard` of a collection that carries no signal exits non-zero with the refusal's sentence (its own
    remedy: `--backend raw_ssh` only for an SSH host). Guards the CLI printing the refusal and exiting 0 — a script
    or a pipeline would read a refused onboard as done."""
    import argparse
    import galaxy
    monkeypatch.setattr(catalog, "local_installed", lambda: {"acme.thing": "1.0.0"})
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: dict(
        probe._facts(coll, "1.0.0", "local", "deep"), plugins={}, modules=["thing_info"]))
    args = argparse.Namespace(collection="acme.thing", key="thing_auto", group="core_switch", host="192.0.2.31",
                              host_name=None, secrets="network", backend=None, username=None, password=None,
                              api_token=None, apply=False, commit=False, bootstrap=False, push=False)
    with pytest.raises(SystemExit) as stop:
        galaxy.cmd_onboard(args)
    assert isinstance(stop.value.code, str), "sys.exit(<sentence>) exits 1; an int 0 or None would be success"
    assert stop.value.code.startswith("no backend kontroll can run fits acme.thing: nothing in it is a signal")
    assert stop.value.code.endswith("pass --backend raw_ssh only if the target is an SSH host, or add a backend")


def test_onboard_derivation_failure_degrades_to_no_floor_never_fails(monkeypatch):
    """INVARIANT D* (the load-bearing pin): a derivation EXCEPTION must NOT fail the onboard — the module ships with
    no derived floor + a logged warning, and the plan is otherwise complete. Guards a derivation bug ever gating
    onboarding (a secondary capability is strictly best-effort)."""
    def _boom(*a, **k):
        raise RuntimeError("derivation blew up")
    monkeypatch.setattr(onboard, "derive_class_capabilities", _boom)
    plan = _blind_plan(monkeypatch, {"cliconf": ["eos"]}, ["eos_command"])
    assert plan["error"] is None
    assert "metrics" not in plan["module"] and "logs" not in plan["module"]
    assert "derived" not in plan["module"] and plan["capability_gap"] == []
