"""scripts/fetch-dashboards.py — curated Grafana.com community dashboards, declared per device module.

The dashboard analogue of gen-observability.py: a class declares its data-relevant Grafana.com dashboard ids
(modules/<key>/module.yml `dashboards:`), and the fetcher pulls them from the public registry, pins the
Prometheus datasource, and provisions them — no hand-authored JSON. These tests pin the OFFLINE half (the
module join + the datasource munge); the network fetch itself is exercised live (not in the hermetic suite).
The script name is hyphenated, so it is loaded by path (not importable).
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


def _fd():
    spec = importlib.util.spec_from_file_location(
        "fetch_dashboards", os.path.join(ROOT, "scripts", "fetch-dashboards.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _fd()


def test_declared_dashboards_join_and_dedup():
    """declared_dashboards collects every enabled module's `dashboards:` ids and DEDUPS by gnet — so the
    Node Exporter dashboard declared by BOTH proxmox and docker_host is fetched once. Proves the
    'declare per class, fetch the union' join over the real modules+fleet."""
    want = fd.declared_dashboards(fd._load(fd.paths.resolve("config/fleet.yml")))
    assert want.get(1860) == "node" and want.get(10347) == "proxmox"


def test_pin_datasource_rewrites_input_and_strips_import_meta():
    """pin_datasource makes a downloaded community dashboard self-provisioning: the ${DS_*} datasource
    import is replaced with our fixed uid, __inputs/__requires are dropped, id is nulled, and a datasource
    TEMPLATE VARIABLE is defaulted to our datasource — so it renders without an import prompt or a stale
    datasource reference (the two datasource styles community dashboards use)."""
    raw = {"__inputs": [{"name": "DS_PROMETHEUS", "type": "datasource", "pluginId": "prometheus"}],
           "__requires": [{"type": "grafana"}], "id": 99,
           "templating": {"list": [{"type": "datasource", "name": "ds", "current": {"value": "stale-uid"}}]},
           "panels": [{"datasource": "${DS_PROMETHEUS}"}]}
    out = fd.pin_datasource(raw)
    assert "__inputs" not in out and "__requires" not in out and out["id"] is None
    assert out["panels"][0]["datasource"] == "prometheus"                      # input rewritten to the uid
    assert out["templating"]["list"][0]["current"]["value"] == "prometheus"    # ds template var defaulted


@pytest.mark.parametrize("item", [{"gnet": 1860, "name": "../../etc/x"}, {"gnet": 1860, "name": "Node"},
                                  {"gnet": "1860", "name": "node"}, {"gnet": -1, "name": "node"},
                                  {"gnet": 10 ** 12, "name": "node"}, {"gnet": True, "name": "node"},
                                  {"name": "node"}, "node"])
def test_fetch_refuses_a_declared_item_that_is_not_an_id_and_one_path_component(tmp_path, monkeypatch, item):
    """`declared_dashboards` fails closed — before anything is fetched or written — when a module's `dashboards:`
    item is not a positive board id plus a name that is one path component, and the message names the module file.
    The name is the FILE this script writes and the id chooses what an internet download puts in it, so a promoted
    line is not trusted to carry either (SECURITY.md C22: the check at the join). Guards a line that reached `main`
    choosing where a third-party download is written."""
    (tmp_path / "modules" / "demo").mkdir(parents=True)
    (tmp_path / "modules" / "demo" / "module.yml").write_text(
        fd.yaml.safe_dump({"key": "demo", "dashboards": [{"gnet": 1, "name": "ok"}, item]}), encoding="utf-8")
    monkeypatch.setattr(fd, "ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        fd.declared_dashboards({"enabled_modules": ["demo"]})
    assert "modules/demo/module.yml" in str(e.value) and "etc/x" not in str(e.value)


def test_fetch_refuses_one_name_declared_with_two_board_ids(tmp_path, monkeypatch):
    """Two enabled classes that declare the SAME dashboard name with DIFFERENT board ids make `declared_dashboards`
    fail closed, naming both module files; the same name with the same id (how `node` is shared by the shipped
    classes) is fine. A name is one file under dashboards/grafana/dashboards/: the script writes in ascending id
    order, so the higher id would silently replace the other class's curated board. Guards a line promoted on one
    class overwriting a dashboard that belongs to another."""
    for key, gnet in (("alpha", 1860), ("beta", 1860), ("gamma", 99999)):
        (tmp_path / "modules" / key).mkdir(parents=True)
        (tmp_path / "modules" / key / "module.yml").write_text(
            fd.yaml.safe_dump({"key": key, "dashboards": [{"gnet": gnet, "name": "node", "derived": True}]}),
            encoding="utf-8")
    monkeypatch.setattr(fd, "ROOT", str(tmp_path))
    assert fd.declared_dashboards({"enabled_modules": ["alpha", "beta"]}) == {1860: "node"}
    with pytest.raises(SystemExit) as e:
        fd.declared_dashboards({"enabled_modules": ["alpha", "beta", "gamma"]})
    assert "modules/alpha/module.yml" in str(e.value) and "modules/gamma/module.yml" in str(e.value)


def test_fetch_refuses_a_dashboards_block_that_is_not_a_list(tmp_path, monkeypatch):
    """A module whose `dashboards:` is a number (or any non-list) makes `declared_dashboards` fail closed, naming
    the file — it used to end in a traceback — while a class key that is not a key at all is skipped and the
    valid classes' boards are still returned. Guards the fetch dying without saying which file is wrong, and a
    malformed fleet line being joined into a path."""
    for key, body in (("good", {"key": "good", "dashboards": [{"gnet": 1860, "name": "node"}]}),
                      ("broken", {"key": "broken", "dashboards": 5})):
        (tmp_path / "modules" / key).mkdir(parents=True)
        (tmp_path / "modules" / key / "module.yml").write_text(fd.yaml.safe_dump(body), encoding="utf-8")
    monkeypatch.setattr(fd, "ROOT", str(tmp_path))
    assert fd.declared_dashboards({"enabled_modules": [None, "../x", 123, "good"]}) == {1860: "node"}
    with pytest.raises(SystemExit) as e:
        fd.declared_dashboards({"enabled_modules": ["good", "broken"]})
    assert "modules/broken/module.yml" in str(e.value)


def test_fetch_never_writes_a_download_over_a_file_that_is_not_that_board(tmp_path, monkeypatch):
    """Before anything is fetched, the run refuses when a target `<name>.json` already exists and does not say it
    is the declared board (`gnetId`): a hand-authored dashboard (the shipped logs one has no `gnetId`), a file of
    another board, a file that is not JSON. A target that IS that board, and one that does not exist, are fine.
    Guards a module line — promoted on any class — replacing a hand-authored dashboard with an internet
    download: a name declared by no class was accepted with any board id."""
    out = tmp_path / "boards"
    out.mkdir()
    (out / "logs.json").write_text('{"uid": "kontroll-logs"}', encoding="utf-8")
    (out / "node.json").write_text('{"uid": "n", "gnetId": 1860}', encoding="utf-8")
    (out / "other.json").write_text('{"gnetId": 4242}', encoding="utf-8")
    (out / "junk.json").write_text("not json", encoding="utf-8")
    (out / "boolfile.json").write_text('{"gnetId": true}', encoding="utf-8")        # True == 1 in Python
    (out / "listfile.json").write_text('[1, 2]', encoding="utf-8")                  # JSON, and not a dashboard
    want = {999999: "logs", 1860: "node", 5: "other", 6: "junk", 7: "absent", 1: "boolfile", 2: "listfile"}
    assert fd.foreign_targets(want, str(out)) == [
        "boolfile.json (declared as board 1)", "listfile.json (declared as board 2)",
        "other.json (declared as board 5)", "junk.json (declared as board 6)", "logs.json (declared as board 999999)"]
    assert fd.foreign_targets({1860: "node", 7: "absent"}, str(out)) == []
    monkeypatch.setattr(fd, "OUT_DIR", str(out))
    monkeypatch.setattr(fd, "_load", lambda rel: {})          # main() reads the fleet first: not this checkout's
    monkeypatch.setattr(fd, "declared_dashboards", lambda fleet: {999999: "logs"})
    monkeypatch.setattr(fd, "fetch", lambda gnet: pytest.fail("a download was started for a refused run"))
    with pytest.raises(SystemExit) as e:
        fd.main([])
    assert "logs.json" in str(e.value)
    assert (out / "logs.json").read_text(encoding="utf-8") == '{"uid": "kontroll-logs"}'


def test_a_fetched_board_says_which_board_it_is(monkeypatch):
    """`fetch()` returns a dashboard whose `gnetId` is the id it was fetched as: unchanged — byte for byte — when
    the download says so itself, and set when it does not. Guards the rule above having something to decide by
    for every file this script writes, without changing the bytes of any board already committed."""
    import json
    def served(with_id):
        raw = {"title": "T", "uid": "u", "panels": []}
        if with_id:
            raw = {"gnetId": 1860, **raw}
        return lambda url: json.dumps({"revision": 3, "name": "T"} if url.endswith("/1860") else raw)
    monkeypatch.setattr(fd, "_get", served(True))
    rev, title, dash = fd.fetch(1860)
    assert (rev, title, dash["gnetId"]) == (3, "T", 1860)
    assert json.dumps(dash) == json.dumps(fd.pin_datasource({"gnetId": 1860, "title": "T", "uid": "u", "panels": []}))
    monkeypatch.setattr(fd, "_get", served(False))
    assert fd.fetch(1860)[2]["gnetId"] == 1860
