"""The Generic SSH host search card (S24) — the commonest Ansible target, a plain SSH host, has no collection to find.

WHY: raw_ssh used to be the catch-all for any collection nothing else claimed, which onboarded a Kubernetes, cloud or
WinRM target as SSH-as-root. It is now never auto-matched, and a plain SSH host is reached instead through a card the
raw_ssh backend declares (`search_card` in ansible/backends/raw_ssh/backend.yml), produced in the one search seam. These
rows pin what that card must and must not do: be offered for an SSH-ish query and not for others, never be probed,
onboard without pinning a collection Galaxy does not have (`ansible.builtin` ships inside ansible-core — a pin of it
would abort the next bootstrap), and reuse its class for host two even after the operator curated it.
"""
import importlib.util
import logging
import os

import pytest
import yaml

from kontroll import catalog, probe
from kontroll.service import onboard, scaffold
from kontroll.service.search import service_search

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY = catalog.load_backends()
RAW_SSH = next(b for b in REGISTRY if b["name"] == "raw_ssh")


def _gen_requirements():
    """scripts/gen-requirements.py, loaded by path (its name has a hyphen)."""
    spec = importlib.util.spec_from_file_location("gen_requirements_card", os.path.join(ROOT, "scripts",
                                                                                        "gen-requirements.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def no_probe(monkeypatch):
    """No installed collection matches, and any probe is a failure: a card is declared data, never probed."""
    monkeypatch.setattr(catalog, "local_shallow", lambda keywords, limit: [])

    def _refuse(*a, **k):
        raise AssertionError("a search card's collection must never be probed")
    monkeypatch.setattr(probe, "deep_probe", _refuse)


@pytest.mark.parametrize("query", [["linux"], ["SSH"], ["debian", "server"], ["ansible.builtin"]])
def test_generic_ssh_card_is_offered_for_a_linux_query(no_probe, query):
    """A local search for an SSH-ish word (a card keyword, any case) or the card's collection returns the card's one
    record: `ansible.builtin`, suggested and only candidate `raw_ssh`, provenance "declared", the card's title as the
    description and its key as `suggested_key`. Guards the commonest target having nothing to click once raw_ssh
    stopped catching every collection."""
    recs = service_search(query, origin="local")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["collection"] == "ansible.builtin" and rec["origin"] == "local"
    assert rec["suggested_backend"] == "raw_ssh" and rec["backend_candidates"] == ["raw_ssh"]
    assert rec["provenance"] == "declared" and rec["suggested_key"] == "ssh_host"
    assert rec["meta"]["description"] == RAW_SSH["search_card"]["title"]


@pytest.mark.parametrize("query", [["cisco"], ["fortios"], ["backup"], ["metrics"], ["reachability"]])
def test_card_is_not_offered_for_an_unrelated_query(no_probe, query):
    """A query that matches neither the card's collection nor one of its keywords gets no card — including a word
    that is only in its title. Guards the card polluting every search (its title's prose, "backup" and "metrics",
    would otherwise offer an SSH host to anyone looking for a backup-capable switch)."""
    assert service_search(query, origin="local") == []


def test_card_is_never_deep_probed(no_probe):
    """A DEEP search (the GUI's deep toggle) re-probes the installed matches, never the card: with every probe
    failing loudly, the card still comes back. Guards a slow `ansible-doc ansible.builtin` call on every SSH search,
    and the card's floor depending on what ansible-core's doc output says today."""
    recs = service_search(["ssh"], origin="local", deep=True)
    assert [r["collection"] for r in recs] == ["ansible.builtin"]


def test_an_installed_collection_wins_over_the_card_of_the_same_name(monkeypatch):
    """When the local search already returned the card's collection as an installed one, the card adds no second
    record of it. Guards two cards of one collection, one probed and one declared, on the same page."""
    monkeypatch.setattr(catalog, "local_shallow", lambda keywords, limit: [
        probe._facts("ansible.builtin", "2.17.0", "local", "shallow")])
    recs = service_search(["ansible.builtin"], origin="local")
    assert [(r["collection"], r["provenance"]) for r in recs] == [("ansible.builtin", "derived")]


def test_generic_ssh_onboard_pins_no_collection_and_probes_nothing(tmp_repo, no_probe):
    """An onboard of the card's collection with the backend the card names plans a raw_ssh class that pins NO
    collection — `collections: []` — with SSH connection vars, and probes nothing; the scaffold does the same. With no
    backend, it is `no_backend` like any other collection (raw_ssh is manual; the card sends it). Guards a
    non-installable `ansible.builtin >=0.0.0` reaching the install lockfile and aborting the next bootstrap, and the
    card's onboard depending on an `ansible-doc` run."""
    plan = onboard.build_onboard_plan("ansible.builtin", "ssh_host", "linux_hosts", "192.0.2.61", backend="raw_ssh",
                                      vectors=[])
    assert plan["error"] is None and plan["backend"] == "raw_ssh" and plan["role"] == "backend_raw_ssh"
    assert plan["module"]["collections"] == []
    host = plan["host_block"]["linux_hosts"]["hosts"][plan["host_name"]]
    assert host["ansible_connection"] == "ssh"
    made = scaffold.build_scaffold("ansible.builtin", "ssh_box", "linux_hosts", "linux", backend="raw_ssh")
    assert made["error"] is None and made["module"]["collections"] == []
    refused = onboard.build_onboard_plan("ansible.builtin", "ssh_host", "linux_hosts", "192.0.2.61", vectors=[])
    assert refused["error"] == "no_backend" and refused["signals"] == {
        "plugin_types": [], "modules": 0, "unrunnable": []}


@pytest.mark.parametrize("text", [
    "{}\n", "key: ssh_host\ncollections: 5\n", "key: ssh_host\n", "key: ssh_host\ncollections: [x]\n",
    "key: ssh_host\ncollections:\n  - {version: '1'}\n", "key: ssh_host\ncollections:\n  - {name: 5}\n",
    "key: ssh_host\ncollections:\n  - {name: acme.other}\nrole: other_role\n",
], ids=["empty-mapping", "collections-not-a-list", "no-collections-key", "a-bare-word", "an-entry-without-a-name",
        "a-name-not-a-string", "another-collection"])
def test_the_card_reuses_only_a_class_that_declares_an_empty_collection_list(tmp_repo, no_probe, text):
    """An onboard from the card under a key whose module file is an empty mapping, holds a `collections` that is not
    a list, has no `collections` at all, lists entries that name no collection, or declares another collection is
    NOT a reuse — the plan is a new class's, and the apply meets the existing file as a conflict — while exactly
    `collections: []` (openwrt's shape, and what the card writes) is. Guards the collection-less reuse capturing a
    class that never said it had no collection: before S24 each was a conflict, and a reuse would adopt that file's
    backend and role for every SSH host onboarded under the key."""
    (tmp_repo / "modules" / "ssh_host").mkdir()
    (tmp_repo / "modules" / "ssh_host" / "module.yml").write_text(text, encoding="utf-8")
    plan = onboard.build_onboard_plan("ansible.builtin", "ssh_host", "linux_hosts", "192.0.2.62", backend="raw_ssh",
                                      vectors=[])
    assert plan["error"] is None and plan["module_reuse"] is False
    (tmp_repo / "modules" / "ssh_host" / "module.yml").write_text(
        "key: ssh_host\ncollections: []\nrole: backend_raw_ssh\nbackend: raw_ssh\ninventory_group: linux_hosts\n"
        "secrets_domain: linux\n", encoding="utf-8")
    assert onboard.build_onboard_plan("ansible.builtin", "ssh_host", "linux_hosts", "192.0.2.62", backend="raw_ssh",
                                      vectors=[])["module_reuse"] is True


def test_every_shipped_card_is_well_formed_bundled_and_skipped_by_gen_requirements():
    """Every card the shipped registry declares is served (well formed, on a backend with a role), names a
    collection gen-requirements knows ships inside ansible-core, and proposes a key that is a device-class key — and
    there is at least one. Guards a card whose collection a pin would send to Galaxy (the belt would not catch a new
    name), and a card silently dropped by its own shape check."""
    cards = catalog.search_cards(REGISTRY)
    declared = [b["name"] for b in REGISTRY if b.get("search_card") is not None]
    assert cards and [b["name"] for b, _ in cards] == declared
    bundled = _gen_requirements().ANSIBLE_CORE_BUNDLED
    for _, card in cards:
        assert card["collection"] in bundled, card["collection"]
        assert all(k == k.lower() for k in card["keywords"]), "a query is case-folded; a keyword must be too"


@pytest.mark.parametrize("card, role", [
    ({"collection": "ansible.builtin", "title": "x", "key": "k"}, "backend_raw_ssh"),                  # no keywords
    ({"collection": "not a name", "title": "x", "key": "k", "keywords": ["ssh"]}, "backend_raw_ssh"),
    ({"collection": "ansible.builtin", "title": " ", "key": "k", "keywords": ["ssh"]}, "backend_raw_ssh"),
    ({"collection": "ansible.builtin", "title": "x", "key": "../k", "keywords": ["ssh"]}, "backend_raw_ssh"),
    ({"collection": "ansible.builtin", "title": "x", "key": "k", "keywords": ["ssh", 3]}, "backend_raw_ssh"),
    ({"collection": "ansible.builtin", "title": "x", "key": "k", "keywords": ["ssh"]}, None),           # no role
    ("ansible.builtin", "backend_raw_ssh"),                                                            # not a mapping
    ({"collection": "cisco.ios", "title": "x", "key": "k", "keywords": ["ssh"]}, "backend_raw_ssh"),  # a Galaxy one
], ids=["no-keywords", "bad-collection", "blank-title", "bad-key", "bad-keyword", "no-role", "scalar",
        "galaxy-collection"])
def test_a_malformed_card_is_skipped_with_a_warning(caplog, card, role):
    """A card that is not well formed, that names a collection ansible-core does not ship, or that sits on a backend
    with no execution role, is not served — it is skipped with a warning naming the backend — and the cards'
    collections do not include it. Guards a malformed card reaching the GUI (a key that is a path, a keyword the
    matcher cannot fold), offering an onboard the planner would refuse, and a card naming a Galaxy collection: the
    planner neither probes nor pins a card's collection, so that one would be onboarded unpinned and unprobed."""
    registry = [dict(name="odd", classify={"manual": True}, role=role, search_card=card)]
    with caplog.at_level(logging.WARNING, logger="kontroll.catalog"):
        assert catalog.search_cards(registry) == [] and catalog.bundled_collections(registry) == set()
    assert "'odd'" in caplog.text


def test_gen_requirements_drops_a_pin_of_a_collection_ansible_core_ships(tmp_path, monkeypatch, capsys):
    """A module that pins `ansible.builtin` (written by the pre-fix CLI onboard) is resolved WITHOUT that pin, the
    other pins kept, and the generator says so on stderr — offline. Guards the next bootstrap's `ansible-galaxy
    collection install` aborting on a collection Galaxy does not have."""
    genreq = _gen_requirements()
    (tmp_path / "modules" / "ssh_old").mkdir(parents=True)
    (tmp_path / "modules" / "_core.yml").write_text(
        yaml.safe_dump({"collections": [{"name": "ansible.posix", "version": ">=1.5.0"}]}), encoding="utf-8")
    (tmp_path / "modules" / "ssh_old" / "module.yml").write_text(yaml.safe_dump({"key": "ssh_old", "collections": [
        {"name": "ansible.builtin", "version": ">=0.0.0"}]}), encoding="utf-8")
    monkeypatch.setattr(genreq, "ROOT", str(tmp_path))
    out, _ = genreq.compute(["ssh_old"], str(tmp_path / "actuation"))
    assert [c["name"] for c in out] == ["ansible.posix"]
    assert "ansible.builtin ships inside ansible-core" in capsys.readouterr().err
