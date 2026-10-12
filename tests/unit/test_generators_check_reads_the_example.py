"""`--check` of every fleet-driven generator validates the COMMITTED file against the shipped example.

WHY: the committed generated files (the Semaphore backup-schedule spec, the Prometheus proxy jobs, the exporter
compose fragments, the secret-env manifest) are the public, example-derived reference. Four generators still read
`paths.resolve("config/fleet.yml")` under `--check` — the instance overlay when one exists — so on a tree with a
private fleet the product gate compared a public file with private data: green by coincidence, and red (with a
"stale" committed file nobody could regenerate from a public clone) the day that fleet changed. `--check` and the
new `--example` now read `instance.example/`; the bare run, which is what a deploy calls on a node, still reads
that node's overlay.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

GENERATORS = ["gen-backup", "gen-prometheus-jobs", "gen-exporters", "gen-secret-env"]


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(ROOT, "scripts", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", GENERATORS)
def test_check_never_asks_for_the_instance_overlay(name, monkeypatch):
    """`main(["--check"])` passes on the committed files without resolving ANY instance-overlay path. Guards an
    operator's fleet edit turning a public product gate red, or making a committed file "stale" against data a
    public clone does not have."""
    gen = _load(name)

    def resolve(rel, write=False):
        raise AssertionError("--check must not resolve the instance overlay (asked for %s)" % rel)
    monkeypatch.setattr(gen.paths, "resolve", resolve)
    assert gen.main(["--check"]) == 0


@pytest.mark.parametrize("name", GENERATORS)
def test_a_bare_run_reads_the_nodes_own_fleet(name, monkeypatch):
    """The bare run — what deploy-stack and the capability regen call on a node — still asks the resolver for the
    fleet, before it writes anything. Guards the fix overshooting: a deploy that generates a live node's schedules,
    scrape jobs and exporters from the EXAMPLE fleet."""
    gen = _load(name)

    class AskedForTheFleet(Exception):
        pass

    def resolve(rel, write=False):
        if rel == "config/fleet.yml":
            raise AskedForTheFleet()
        raise AssertionError("unexpected overlay read before the fleet: %s" % rel)
    monkeypatch.setattr(gen.paths, "resolve", resolve)
    with pytest.raises(AskedForTheFleet):
        gen.main([])


def test_exporters_check_reads_the_examples_trust_decisions_not_the_nodes(monkeypatch):
    """`gen-exporters.py --check` takes the per-class trust decisions (`device_trust`) from
    `instance.example/instance.yml` and never opens `instance/instance.yml`. Guards the second, hard-coded overlay
    read in that generator: a CA pin an operator set on their own instance changing the verdict of a public gate."""
    gen = _load("gen-exporters")
    asked = []
    real = gen._load_optional

    def load_optional(rel):
        asked.append(rel.replace(os.sep, "/"))
        return real(rel)
    monkeypatch.setattr(gen, "_load_optional", load_optional)
    assert gen.main(["--check"]) == 0
    assert asked == ["instance.example/instance.yml"], asked
