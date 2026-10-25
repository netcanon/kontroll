"""classify(facts, backends) — the single dispatch seam's auto-matcher.

Pins each backend's structural signature against synthetic facts, that every `manual`
backend (napalm, raw_ssh) is NEVER auto-matched, and that the absence of every signal matches
NO backend (S24 — it used to fall to raw_ssh). Asserts membership + the
lowest-`order` winner (suggested backend), not full-list equality, so adding a new
backend doesn't break these. Mirrors the real ansible/backends/*/backend.yml rules.
"""
import pytest

import galaxy

pytestmark = pytest.mark.unit


def _suggested(facts, backends):
    """The first (lowest-order) backend classify() matches, or None — the 'suggested' backend."""
    matches = galaxy.classify(facts, backends)
    return matches[0] if matches else None


def test_cliconf_maps_to_netcommon_cli(backends, make_facts):
    """A cliconf plugin auto-classifies to netcommon_cli, and it wins as the suggested backend
    (order 1) — the common network_cli device path."""
    f = make_facts(plugins={"cliconf": ["ios"]}, modules=["ios_command"])
    matches = galaxy.classify(f, backends)
    assert "netcommon_cli" in matches
    assert _suggested(f, backends) == "netcommon_cli"   # order 1 — the winner


def test_httpapi_only_maps_to_api(backends, make_facts):
    """An httpapi-only collection (no cliconf) auto-classifies to the api backend, not cli."""
    f = make_facts(plugins={"httpapi": ["fortios"]})
    matches = galaxy.classify(f, backends)
    assert "netcommon_cli" not in matches
    assert _suggested(f, backends) == "api"


@pytest.mark.parametrize("facts", [
    dict(modules=["weird_thing"]),                                                  # a bare module list
    dict(plugins={"connection": ["kubectl"]}, modules=["k8s", "k8s_info"]),         # a Kubernetes shape
    dict(plugins={"connection": ["aws_ssm"]}, modules=["ec2_instance"]),            # a cloud-SDK shape
    dict(modules=["win_ping", "win_shell", "win_service"]),                         # a WinRM shape
    dict(modules=["alias", "rule"]),                                                # the opnsense pinned shape
    dict(),                                                                         # nothing at all (openwrt)
], ids=["bare-modules", "kubernetes", "cloud-sdk", "winrm", "opnsense-pinned", "empty"])
def test_absence_of_signals_matches_no_backend(backends, make_facts, facts):
    """A collection with no cliconf/httpapi/netconf plugin and no `*_config` module matches NO backend, whatever
    else it carries — a connection plugin no predicate reads, Windows or cloud modules, or nothing.

    Guards the S24 misroute: raw_ssh's `none_of` matched the ABSENCE of every signal, so a Kubernetes, cloud or WinRM
    collection was onboarded "successfully" as SSH-as-root with a node_exporter floor. Absence is not evidence; such
    a collection must be answered `no_backend`."""
    assert galaxy.classify(make_facts(**facts), backends) == []


def test_config_module_with_backup_option_maps_to_vendor_config(backends, make_facts):
    """A vendor *_config module that exposes a `backup:` option auto-classifies to vendor_config —
    and is NOT treated as bespoke (a _config module is structured, not raw)."""
    f = make_facts(modules=["ios_config"], module_options={"ios_config": ["backup", "lines"]})
    matches = galaxy.classify(f, backends)
    assert "vendor_config" in matches
    assert "raw_ssh" not in matches                     # raw_ssh is manual (S24): never a match


def test_manual_backend_never_autoclassified(backends, make_facts):
    """No backend whose `classify` is `manual` in the shipped registry — napalm (select-by-driver) and raw_ssh
    (chosen only by declaration or its search card) — is ever auto-picked, whatever the facts; and both are manual.
    Read from the registry, so a new manual backend is covered without a new row. Guards raw_ssh regaining a
    structural predicate (S24) — the catch-all that onboarded any unrecognised collection as SSH-as-root."""
    manual = {b["name"] for b in backends if (b.get("classify") or {}).get("manual")}
    assert {"napalm", "raw_ssh"} <= manual
    for f in (make_facts(plugins={"cliconf": ["ios"]}),
              make_facts(plugins={"httpapi": ["x"]}),
              make_facts(plugins={"netconf": ["x"]}),
              make_facts(modules=["thing"]),
              make_facts()):
        assert manual.isdisjoint(galaxy.classify(f, backends))


def test_galaxy_classify_exits_with_the_sentence_for_a_requires_that_is_not_a_list_of_names(monkeypatch,
                                                                                             make_facts):
    """`galaxy classify` of a collection whose best backend's registry `requires:` is not a list of param names exits
    non-zero with the repository-data sentence naming `requires`, as `GET /classify` answers 409 for it.

    Guards the CLI half of T18's fold-3 read, N3: the command printed a string `requires:` letter by letter and raised
    a TypeError for a non-string entry, and nothing ran it against a malformed registry, so a `RepositoryDataError`
    escaping it as a traceback survived every suite (fold-4 read, N5: its mutant Z9)."""
    import sys

    from kontroll import catalog, probe
    real = catalog.load_backends
    monkeypatch.setattr(catalog, "load_backends", lambda *a, **k: [
        dict(b, requires="network_os") if b.get("name") == "netcommon_cli" else b for b in real(*a, **k)])
    monkeypatch.setattr(probe, "deep_probe", lambda coll, version=None: make_facts(
        collection=coll, version="5.0.0", plugins={"cliconf": ["ios"]}, modules=["ios_command"]))
    monkeypatch.setattr(sys, "argv", ["galaxy.py", "classify", "cisco.ios"])
    with pytest.raises(SystemExit) as stop:
        galaxy.main()
    assert isinstance(stop.value.code, str), "sys.exit(<sentence>) exits 1; an int 0 or None would be success"
    assert "netcommon_cli backend's requires is not a list of param names" in stop.value.code
