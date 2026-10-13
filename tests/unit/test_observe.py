"""observe — the telemetry instance (#1) of the secondary-capability seam: the metrics/dashboards write.

These pin the telemetry-specific half of Phase 7 (docs/observability/secondary-capability-dialog.md §2.1,
master §6 acceptance): PROPOSE writes nothing; the module-block write PRESERVES a hand-written module.yml's
comments (never a safe_dump flatten); the config-injection guard rejects an out-of-allow-list param BEFORE it
is written; ADD is idempotent (an already-declared method is refused, not duplicated); the plan_token gates
the exact declared block being written; and the enact step is hand-off STRINGS only (the API runs no play).
The regen-against-the-real-tree step is self-protecting (a no-op under tmp_repo), so these never touch the
real repo.
"""
import os

import pytest

import yaml

from kontroll import catalog
from kontroll.service import observe, promote

pytestmark = pytest.mark.unit

METHODS = catalog.load_telemetry()

# A hand-written module.yml WITH comments + an existing metrics block — the proxmox/cisco shape the write
# must never flatten.
_COMMENTED = """# Device-class module: a network switch (hand-written, with comments).
key: demo_sw
description: demo switch
status: active
collections:
  - name: cisco.ios
role: backend_network_cli
inventory_group: core_switch   # a comment that must survive the write
metrics:
  - {method: snmp, params: {module: if_mib}}   # interface counters
"""


def _write_module(tmp_repo, key, text):
    d = tmp_repo / "modules" / key
    d.mkdir(parents=True)
    (d / "module.yml").write_text(text, encoding="utf-8")
    return d / "module.yml"


def test_propose_writes_nothing(tmp_repo):
    """build_telemetry_plan is PURE — after computing a plan, the module.yml on disk is byte-identical. Guards
    the propose/promote boundary: reviewing a plan must never mutate the repo (only promote writes)."""
    p = _write_module(tmp_repo, "demo_sw", _COMMENTED)
    before = p.read_text(encoding="utf-8")
    plan = observe.build_telemetry_plan("demo_sw", "host_node", methods=METHODS)
    assert plan["error"] is None
    assert p.read_text(encoding="utf-8") == before


def test_add_preserves_comments_and_existing_block(tmp_repo):
    """Adding host_node to a commented module appends to the metrics list and leaves every comment + the
    prior entry intact — the comment-preserving line-surgery, not a yaml round-trip. Guards the regression
    that flattens a hand-written module.yml (losing its access-chain/rationale comments)."""
    p = _write_module(tmp_repo, "demo_sw", _COMMENTED)
    plan = observe.build_telemetry_plan("demo_sw", "host_node", methods=METHODS)
    observe.apply_telemetry_plan(plan)
    after = p.read_text(encoding="utf-8")
    assert "# a comment that must survive the write" in after
    assert "- {method: snmp, params: {module: if_mib}}" in after   # the prior entry kept
    assert "- {method: host_node}" in after                        # the new entry added
    assert after.count("metrics:") == 1                            # appended INTO the block, not a 2nd block


def test_add_creates_block_when_absent(tmp_repo):
    """A class with NO metrics block gets a fresh `metrics:` block appended (the common case — onboard never
    writes a metrics block, INVARIANT D*). Guards the absent-block branch of the insert."""
    text = "key: demo_host\ndescription: a host\ninventory_group: hypervisors\n"
    p = _write_module(tmp_repo, "demo_host", text)
    plan = observe.build_telemetry_plan("demo_host", "host_node", methods=METHODS)
    observe.apply_telemetry_plan(plan)
    after = p.read_text(encoding="utf-8")
    assert "metrics:\n  - {method: host_node}" in after


def test_reconfigure_same_value_is_a_noop(tmp_repo):
    """Re-proposing a method the class already declares WITH THE SAME value is an idempotent no-op (Phase 4a
    replaced the `already_declared` wall with a reconfigure path): error None, an empty change-set, severity
    None, and text_after == text_before (writes nothing). Guards a same-value re-submit being treated as a
    clobber or duplicated as a second entry."""
    _write_module(tmp_repo, "demo_sw", _COMMENTED)
    plan = observe.build_telemetry_plan("demo_sw", "snmp", params={"module": "if_mib"}, methods=METHODS)
    assert plan["error"] is None and plan["changes"] == [] and plan["severity"] is None
    assert plan["text_after"] == plan["text_before"]   # the no-op writes nothing


def test_bad_param_is_rejected_before_write(tmp_repo):
    """A param value outside the method's allow-list (here a YAML-metacharacter injection) is `bad_param` —
    the config-injection guard, enforced in the propose path as defense-in-depth with the generator. Guards a
    crafted value ever reaching the rendered metrics entry."""
    _write_module(tmp_repo, "demo_sw2", _COMMENTED.replace("demo_sw", "demo_sw2").replace(
        "  - {method: snmp, params: {module: if_mib}}   # interface counters\n", ""))
    plan = observe.build_telemetry_plan("demo_sw2", "snmp",
                                        params={"module": "if_mib\ninjected: pwned"}, methods=METHODS)
    assert plan["error"] == "bad_param"


def test_unknown_method_and_not_onboarded(tmp_repo):
    """An unregistered method is `unknown_method`; a class with no module.yml is `not_onboarded`. Guards the
    dialog from proposing a write for a typo'd method or an un-onboarded class (the route turns each into the
    right 4xx)."""
    _write_module(tmp_repo, "demo_sw", _COMMENTED)
    assert observe.build_telemetry_plan("demo_sw", "nope", methods=METHODS)["error"] == "unknown_method"
    assert observe.build_telemetry_plan("ghost", "host_node", methods=METHODS)["error"] == "not_onboarded"


def test_plan_token_gates_the_written_block(tmp_repo):
    """The plan's token verifies against the text it will write, and FAILS for any other text — the
    propose→promote anti-drift gate applied to the telemetry write. Guards a promote landing a block the
    operator never reviewed."""
    _write_module(tmp_repo, "demo_host", "key: demo_host\ninventory_group: hypervisors\n")
    plan = observe.build_telemetry_plan("demo_host", "host_node", methods=METHODS)
    assert promote.verify_token(plan["plan_token"], *plan["token_parts"]) is True   # token_parts = [text_after, severity_decision]
    assert promote.verify_token(plan["plan_token"], plan["text_after"] + "drift", plan["token_parts"][1]) is False


def test_enact_commands_carry_kind_and_never_actuate(tmp_repo):
    """telemetry_enact_commands returns `{kind, why, cmd, …}` steps — the operator's hand-off, NEVER executed by
    the API (the PROPOSE/ENACT boundary). Each step declares its `kind`: 'semaphore' (a one-click Tier-1 task,
    naming a `task`) or 'operator' (a control-node command, Tier-2), and keeps a `cmd` CLI fallback + a `why`.
    The universal Prometheus reload is now a Semaphore task (HTTP reload), not a `docker exec kill -HUP` (item F)."""
    _write_module(tmp_repo, "demo_sw", _COMMENTED)
    plan = observe.build_telemetry_plan("demo_sw", "blackbox", params={"probe": "icmp"}, methods=METHODS)
    assert plan["error"] is None
    assert plan["enact"]
    for c in plan["enact"]:
        assert c["kind"] in ("semaphore", "operator")
        assert c["cmd"] and c["why"]                              # a CLI fallback + a rationale, always
        if c["kind"] == "semaphore":
            assert c["task"]                                      # a Semaphore task name to run
    reload = [c for c in plan["enact"] if c.get("task") == "reload-observability"]
    assert reload and reload[0]["kind"] == "semaphore"           # the reload is a Semaphore task, not a HUP
    assert not any("kill -HUP" in c["cmd"] for c in plan["enact"])


def test_grafana_links_deep_link_to_committed_uid(tmp_repo, monkeypatch):
    """The telemetry plan carries Grafana `links`: an 'Open Grafana' base + a `/d/<uid>` deep-link for each
    curated dashboard whose committed JSON pins a uid. WHY (#122): after observability onboarding the operator
    asked for a clickable jump to the device's Grafana page; the uid is read from the committed dashboard JSON so
    the deep-link is reproducible, the base comes from the deploy env, and links are EMPTY when no base is
    resolvable (never a fabricated host)."""
    _write_module(tmp_repo, "demo_host",
                  "key: demo_host\ninventory_group: hypervisors\ndashboards:\n  - {gnet: 1860, name: node}\n")
    dd = tmp_repo / "dashboards" / "grafana" / "dashboards"
    dd.mkdir(parents=True)
    (dd / "node.json").write_text('{"uid": "rYdddlPWk", "title": "Node Exporter Full"}', encoding="utf-8")
    # no mgmt base in env -> no links (never invent a host)
    monkeypatch.delenv("KONTROLL_MGMT_IP", raising=False)
    monkeypatch.delenv("KONTROLL_DOMAIN", raising=False)
    assert observe.build_telemetry_plan("demo_host", "host_node", methods=METHODS)["links"] == []
    # by-IP base -> an 'Open Grafana' link + the node dashboard /d/<uid> deep-link (HTTP :3002, like the Homepage tile)
    monkeypatch.setenv("KONTROLL_MGMT_IP", "203.0.113.9")
    urls = {l["url"] for l in observe.build_telemetry_plan("demo_host", "host_node", methods=METHODS)["links"]}
    assert "http://203.0.113.9:3002" in urls
    assert "http://203.0.113.9:3002/d/rYdddlPWk" in urls
    # by-domain fallback (no IP) -> the reverse-proxied grafana host
    monkeypatch.delenv("KONTROLL_MGMT_IP", raising=False)
    monkeypatch.setenv("KONTROLL_DOMAIN", "lab.example")
    base = observe.build_telemetry_plan("demo_host", "host_node", methods=METHODS)["links"][0]["url"]
    assert base == "https://grafana.lab.example"


_SHIPPED_SHAPE = """key: demo_sw
description: demo switch
collections:
  - name: cisco.ios
role: backend_network_cli
inventory_group: core_switch
metrics:
  - {method: snmp, params: {module: if_mib}}
dashboards:
  - {gnet: 13659, name: snmp_if}
"""


@pytest.mark.parametrize("dashboards", [
    [{"gnet": 1, "name": "x}\ninventory_group: edge_firewall\nrole: opnsense_firewall\n_pad: {a: 1"}],
    [{"gnet": 1, "name": "a, derived: true, gnet: 99999"}],
    [{"gnet": 1, "name": "../../../../tmp/x"}],
    [{"gnet": 1, "name": "Node"}],
    [{"gnet": 1, "name": "node\n"}],
    [{"gnet": 1, "name": "node", "derived": True}],
    [{"gnet": True, "name": "node"}],
    [{"gnet": "1860", "name": "node"}],
    [{"gnet": 0, "name": "node"}],
    [{"gnet": 10 ** 12, "name": "node"}],
    [{"gnet": 1860.0, "name": "node"}],
    [{"name": "node"}],
    ["node"],
    {"gnet": 1, "name": "node"},
    "node",
    [{"gnet": 5, "name": "null"}],
    [{"gnet": 5, "name": "true"}],
    [{"gnet": 5, "name": "123"}],
    [{"gnet": 5, "name": "2026-10-12"}],
    [{"gnet": 5, "name": "fresh"}, {"gnet": 6, "name": "fresh"}],
    [{"gnet": 5, "name": "fresh"}, {"gnet": 5, "name": "fresh"}],
    [{"gnet": 99999, "name": "snmp_if"}],
    [{"gnet": 99999, "name": "node"}],
    [{"gnet": 1, "name": "n%d" % i} for i in range(40)],
    [{"gnet": 999999, "name": "logs"}],
    [{"gnet": 31337, "name": "other"}],
    [{"gnet": 1860, "name": "mynode"}],
    [{"gnet": 7, "name": "p"}, {"gnet": 7, "name": "q"}],
    [{"gnet": 31338, "name": "junk"}],
    [{"gnet": 31339, "name": "listfile"}],
    [{"gnet": 1, "name": "boolfile"}],
], ids=["newline-rewrites-role", "one-line-flow-mapping", "path-traversal", "uppercase", "trailing-newline",
        "extra-key", "boolean-id", "string-id", "zero-id", "huge-id", "float-id", "missing-id", "non-mapping-item",
        "not-a-list", "a-string", "yaml-null", "yaml-bool", "yaml-number", "yaml-date", "repeated-name",
        "repeated-item", "own-name-other-id", "another-class-name-other-id", "too-many-items",
        "existing-file-no-class-declares", "existing-file-of-another-board", "declared-board-second-name",
        "one-board-two-names", "existing-file-unreadable", "existing-file-not-a-board", "existing-file-boolean-id"])
def test_a_dashboard_selection_outside_the_closed_shape_is_refused_before_anything_is_rendered(
        tmp_repo, monkeypatch, dashboards):
    """A `dashboards` selection is a REQUEST value that becomes a hand-formatted YAML line, a file name when the
    plan reads the board's uid, and the file `fetch-dashboards.py` later writes an internet download to. Anything
    but a bounded list of exactly `{gnet: <positive int>, name: <one path component>}` is `bad_param`, and neither
    the renderer nor the uid read is reached. So is a name YAML reads as another type (`null`, `true`, `123`, a
    date — inside the charset, and not a string once written unquoted), a name repeated within the selection, and
    a name this class or ANOTHER class already declares with a different board id (a name is one file: the later
    download would replace the curated board). So is a name that is already a FILE under the dashboards directory
    and does not say it is that board — the hand-authored `logs` dashboard, which no class declares, or a file of
    another board — and a board id given a second name (the fetch keeps one name per board, so the second file
    would never be written). Guards one request field rewriting a class's ROLE and INVENTORY GROUP with a newline,
    rewriting its own item — the board id, the `derived` mark — with none, choosing a path outside the dashboards
    directory, and overwriting another class's board or a hand-authored one. The refusal names the item and the
    rule, never the value."""
    _write_module(tmp_repo, "demo_sw", _SHIPPED_SHAPE)
    _write_module(tmp_repo, "demo_hv", "key: demo_hv\ndashboards:\n  - {gnet: 1860, name: node}\n")
    boards = tmp_repo / "dashboards" / "grafana" / "dashboards"
    boards.mkdir(parents=True)
    (boards / "logs.json").write_text('{"uid": "kontroll-logs", "title": "Logs"}', encoding="utf-8")   # hand-authored
    (boards / "other.json").write_text('{"uid": "x", "gnetId": 4242}', encoding="utf-8")              # another board
    (boards / "junk.json").write_text("not json at all", encoding="utf-8")                            # unreadable
    (boards / "listfile.json").write_text("[1, 2]", encoding="utf-8")                                 # not a board
    (boards / "boolfile.json").write_text('{"gnetId": true}', encoding="utf-8")                       # True == 1

    def must_not_run(*a, **k):
        raise AssertionError("a refused selection reached the renderer or the uid read")
    monkeypatch.setattr(observe, "_render_dashboard_entry", must_not_run)
    monkeypatch.setattr(observe, "_dashboard_uid", must_not_run)
    plan = observe.build_telemetry_plan("demo_sw", "host_node", dashboards=dashboards, methods=METHODS)
    assert plan["error"] == "bad_param" and plan["detail"].startswith("dashboards")
    assert "edge_firewall" not in plan["detail"] and "tmp/x" not in plan["detail"]


def test_a_valid_dashboard_selection_adds_one_item_and_changes_no_other_key(tmp_repo):
    """With `{gnet: 1860, name: node}` the proposed module text parses to the same mapping as before plus exactly
    that one `dashboards` item and the one `metrics` entry the method adds — every other top-level key is equal
    before and after, and the item reads back as the two values that were sent (a name another class declares with
    the SAME board id is allowed: it is the same file). Guards the write path, which had no test at all: a change
    to the renderer that touches another key of the class (its role, its inventory group) would not otherwise be
    seen."""
    _write_module(tmp_repo, "demo_sw", _SHIPPED_SHAPE)
    _write_module(tmp_repo, "demo_hv", "key: demo_hv\ndashboards:\n  - {gnet: 1860, name: node}\n")
    plan = observe.build_telemetry_plan("demo_sw", "host_node", dashboards=[{"gnet": 1860, "name": "node"}],
                                        methods=METHODS)
    assert plan["error"] is None
    before, after = yaml.safe_load(plan["text_before"]), yaml.safe_load(plan["text_after"])
    assert after["dashboards"] == before["dashboards"] + [{"gnet": 1860, "name": "node"}]
    assert after["metrics"] == before["metrics"] + [{"method": "host_node"}]
    assert {k: v for k, v in after.items() if k not in ("dashboards", "metrics")} == \
           {k: v for k, v in before.items() if k not in ("dashboards", "metrics")}
    assert after["role"] == "backend_network_cli" and after["inventory_group"] == "core_switch"


def test_dashboard_uid_reads_only_inside_the_dashboards_directory(tmp_repo, monkeypatch):
    """`_dashboard_uid` re-checks the name at the join: a name that is not one path component returns None — also
    for a name that reached `main` in a module file some other way — while a real board's uid is still read.
    Guards a propose reflecting the `uid` field of an arbitrary JSON file the service can read."""
    (tmp_repo / "secret.json").write_text('{"uid": "LEAKED"}', encoding="utf-8")
    dd = tmp_repo / "dashboards" / "grafana" / "dashboards"
    dd.mkdir(parents=True)
    (dd / "node.json").write_text('{"uid": "rYdddlPWk"}', encoding="utf-8")
    assert observe._dashboard_uid("node") == "rYdddlPWk"
    for name in ("../../../secret", "..", "a/b", "Node", "node\n", ""):
        assert observe._dashboard_uid(name) is None, name


def test_a_name_whose_existing_file_is_that_board_is_accepted(tmp_repo):
    """A selection whose name is already a file under the dashboards directory is accepted when the file says it
    is that board (`gnetId`) — whether a class still declares it or not. Guards the existing-file rule refusing the
    ordinary cases: re-proposing a fetched board, and declaring again a board whose class was removed while its
    file stayed."""
    _write_module(tmp_repo, "demo_sw", _SHIPPED_SHAPE)
    boards = tmp_repo / "dashboards" / "grafana" / "dashboards"
    boards.mkdir(parents=True)
    (boards / "snmp_if.json").write_text('{"uid": "a", "gnetId": 13659}', encoding="utf-8")   # declared, fetched
    (boards / "orphan.json").write_text('{"uid": "b", "gnetId": 4242}', encoding="utf-8")     # no class declares it
    for selection in ([{"gnet": 13659, "name": "snmp_if"}], [{"gnet": 4242, "name": "orphan"}],
                      [{"gnet": 777, "name": "brand_new"}]):
        plan = observe.build_telemetry_plan("demo_sw", "host_node", dashboards=selection, methods=METHODS)
        assert plan["error"] is None, (selection, plan.get("detail"))


def test_one_damaged_module_file_does_not_break_every_class_s_dashboards(tmp_repo):
    """`_declared_dashboards()` reads every class's module file on a propose that carries dashboards. A file that
    is not UTF-8, is not YAML, is a list, or whose `dashboards:` is a number or holds a boolean id contributes what
    it validly declares and nothing else — and the propose for another class is still built. Guards one damaged
    class turning every class's telemetry propose into an uncaught error, and `gnet: true` being taken for
    board 1."""
    _write_module(tmp_repo, "demo_sw", _SHIPPED_SHAPE)
    _write_module(tmp_repo, "bad_number", "key: bad_number\ndashboards: 5\n")
    _write_module(tmp_repo, "bad_list_doc", "- a\n- b\n")
    _write_module(tmp_repo, "bad_yaml", "key: [unterminated\n")
    _write_module(tmp_repo, "bad_bool", "key: bad_bool\ndashboards:\n  - {gnet: true, name: boolboard}\n  - 7\n")
    (tmp_repo / "modules" / "bad_bytes").mkdir()
    (tmp_repo / "modules" / "bad_bytes" / "module.yml").write_bytes(b"key: \xff\xfe\n")
    assert observe._declared_dashboards() == {"snmp_if": {13659}}
    plan = observe.build_telemetry_plan("demo_sw", "host_node", dashboards=[{"gnet": 1, "name": "boolboard"}],
                                        methods=METHODS)
    assert plan["error"] is None


def test_the_dashboard_name_check_knows_the_name_goes_into_a_flow_mapping(tmp_repo, monkeypatch):
    """With the path-component charset switched off, a dashboard name holding a `,` or a `}` is still refused: the
    name is rendered inside `{gnet: N, name: <name>}`, where those are structure although the same word is plain
    after `key: `. Today the charset refuses them first, so this is a second layer — pinned because the charset is
    a rule about FILE NAMES, and the day it is widened nothing else would stand between a name and the item's
    other fields."""
    _write_module(tmp_repo, "demo_sw", _SHIPPED_SHAPE)
    monkeypatch.setattr(observe.paths, "component", lambda value, field="x": value)
    for name in ("a,b", "a}", "x, derived: true", "0x_", "2026-13-45"):
        cleaned, bad = observe._clean_dashboards([{"gnet": 5, "name": name}], {})
        assert cleaned is None and bad.startswith("dashboards[0].name"), name
    assert observe._clean_dashboards([{"gnet": 5, "name": "plain_name"}], {}) == \
        ([{"gnet": 5, "name": "plain_name"}], None)


def test_a_reconfigure_of_the_shipped_docker_host_class_keeps_its_other_method(tmp_repo):
    """Planning a changed `blackbox` probe for the shipped `docker_host` class — whose `metrics:` line carries a
    comment — gives a module text that YAML reads with BOTH of the class's methods, the changed one changed, under
    one `metrics:` key.

    Guards the planner end of the block writer's fix on the real file: this plan used to append a second
    `metrics:` block holding only `blackbox`, and the class's `host_node` target then disappeared from the next
    regeneration while the proposal's diff showed an added block and nothing removed."""
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "modules", "docker_host", "module.yml")
    with open(real, encoding="utf-8") as fh:
        _write_module(tmp_repo, "docker_host", fh.read())
    plan = observe.build_telemetry_plan("docker_host", "blackbox", params={"probe": "tcp_connect"}, methods=METHODS)
    assert plan["error"] is None and plan["changes"], plan.get("detail")
    after = yaml.safe_load(plan["text_after"])
    assert [m["method"] for m in after["metrics"]] == ["blackbox", "host_node"]
    assert after["metrics"][0]["params"] == {"probe": "tcp_connect"}
    assert sum(1 for ln in plan["text_after"].splitlines() if ln.startswith("metrics:")) == 1
