"""logsvc — the logging instance (#3) of the secondary-capability seam: the `logs:` write + the enact builder.
Guards the add-only idempotent write, that the enact reloads Vector (NOT Prometheus — the enact builder stays
instance-local so enact_kind: vector_reload is data, not a spine edit), and the capability-neutral entrypoints.
"""
import pytest
import yaml

from kontroll import catalog, paths
from kontroll.service import logsvc

pytestmark = pytest.mark.unit


def _method(name):
    return next(m for m in catalog.load_logging() if m["name"] == name)


def test_logging_enact_emits_vector_reload_not_prometheus():
    """logsvc.logging_enact_commands returns steps that reload VECTOR (+ a device-side wire play for a push
    method), never a Prometheus kill -HUP — guards the enact content being copy-pasted from telemetry (the
    enact builder stays instance-local so enact_kind: vector_reload is descriptive data, not a spine edit)."""
    cmds = logsvc.logging_enact_commands("cisco_ios", _method("syslog_push"))
    blob = " ".join(c["cmd"] for c in cmds)
    assert "gen-logging.py" in blob and "prometheus" not in blob.lower() and "HUP" not in blob
    assert any("wire-logging.yml" in c["cmd"] for c in cmds)   # push method -> a device-side wiring step


def test_pull_method_enact_is_vector_reload_only():
    """A PULL method (rest_pull) has NO device-side wiring step — Vector reaches out, so the enact is the
    Vector reload alone. Guards a wiring play being emitted for a method with wiring_role: null."""
    cmds = logsvc.logging_enact_commands("demo", _method("rest_pull"))
    assert not any("wire-logging.yml" in c["cmd"] for c in cmds)


def test_apply_logging_plan_is_idempotent_add_only(tmp_repo):
    """Promoting the same logs selection twice lands the block once (add-only line-surgery via the lifted
    insert_into_block) — a second apply is a no-op. Guards a non-idempotent logs: write that would duplicate a
    source on re-promote (the 0-changed rule). (regenerate is the self-protecting no-op under tmp_repo.)"""
    (tmp_repo / "modules" / "demo").mkdir(parents=True)
    (tmp_repo / "modules" / "demo" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo", "collections": [{"name": "ns.demo"}], "role": "demo"}), encoding="utf-8")
    plan = logsvc.build_logging_plan("demo", "journald_remote")
    assert plan["error"] is None
    out1 = logsvc.apply_logging_plan(plan)
    assert out1["changed"] is True
    # a second build sees it declared (idempotent contract); re-applying the SAME plan writes nothing.
    out2 = logsvc.apply_logging_plan(plan)
    assert out2["changed"] is False
    # re-proposing the SAME method+value is now an idempotent reconfigure NO-OP (Phase 4a replaced the
    # `already_declared` wall): error None, empty change-set, text_after == text_before (writes nothing).
    re_plan = logsvc.build_logging_plan("demo", "journald_remote")
    assert re_plan["error"] is None and re_plan["changes"] == [] and re_plan["text_after"] == re_plan["text_before"]


def test_build_plan_neutral_signature_unpacks_selection(tmp_repo):
    """logsvc.build_plan(key, selection) — the capability-neutral entrypoint the spine calls — unpacks
    {method, params} into build_logging_plan with the same signature observe.build_plan has, so the spine
    never branches on the capability. Guards a divergent entrypoint shape."""
    (tmp_repo / "modules" / "demo").mkdir(parents=True)
    (tmp_repo / "modules" / "demo" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo", "collections": [{"name": "ns.demo"}]}), encoding="utf-8")
    plan = logsvc.build_plan("demo", {"method": "file_tail_ssh", "params": {"retention": "30d"}})
    assert plan["error"] is None and plan["method"] == "file_tail_ssh"
    assert any(p.endswith("module.yml") for p in plan["paths"])


def test_planned_generated_paths_match_generator_output():
    """build_logging_plan's regen_paths MUST be exactly the files gen-logging.py actually writes for that
    (method, key): the per-(method, key) fragment + the aggregate _capability_sink, under the generator's
    GEN_DIR. Single-sourced from gen-logging (not a hard-coded literal), pinned against the committed cisco_ios
    worked example whose fragment exists on disk. Guards the shipped drift where logsvc named
    docker/vector/config.d/sources|transforms/generated/ (paths the generator never writes), so a capability
    promote committed phantom paths and never staged the real Vector config — the bug a tmp_repo test (regen is
    a no-op there) could not catch and the capability path was never dogfooded to expose."""
    import os
    from kontroll import paths as kpaths
    gen = logsvc._gen_logging()
    planned = logsvc._generated_paths("cisco_ios", "syslog_push")
    assert planned == ["%s/syslog_push_cisco_ios.generated.yaml" % gen.GEN_DIR,
                       "%s/_capability_sink.generated.yaml" % gen.GEN_DIR]
    # the planned fragment + sink are real files the generator wrote (the committed worked example) — proving
    # logsvc points at gen-logging's real output, not a path the generator never emits.
    for rel in planned:
        assert os.path.exists(os.path.join(kpaths.ROOT, rel)), "%s is a phantom path" % rel
    # and gen-logging's own fan over the real tree produces that exact fragment key (the two agree end-to-end).
    fleet = {"enabled_modules": ["cisco_ios"]}
    inv = {"all": {"children": {gen._load("modules/cisco_ios/module.yml").get("inventory_group"):
                                {"hosts": {"sw": {"ansible_host": "192.0.2.1"}}}}}}
    produced = set(gen.logging_files(fleet, inv))
    assert planned[0] in produced and planned[1] in produced


def test_a_logging_plan_whose_text_would_not_read_back_is_refused(tmp_repo):
    """Planning a first `journald_remote` source for a class whose module file writes its `logs` key in quotes
    raises the repository's refusal instead of returning a plan, and the file is as it was.

    Guards the logging planner's read-back: the line writer does not see a quoted key, so it appends a second
    `logs:` block — YAML reads the last one only, and the source the class already declared was gone from the
    plan's text while the plan said one source was added."""
    d = tmp_repo / "modules" / "demo"
    d.mkdir(parents=True)
    text = 'key: demo\ncollections:\n  - name: ns.demo\nrole: demo\n"logs":\n  - {method: file_tail}\n'
    (d / "module.yml").write_text(text, encoding="utf-8")
    with pytest.raises(paths.RepositoryDataError) as refused:
        logsvc.build_logging_plan("demo", "journald_remote")
    assert "`logs`" in str(refused.value) and (d / "module.yml").read_text(encoding="utf-8") == text


def test_a_logging_reconfigure_reads_back_as_the_entry_replaced_where_it_stands(tmp_repo):
    """Planning `syslog_push` over udp for a class that declares it over tcp, first of two sources, returns a plan
    whose text YAML reads as the same two sources in the same order with only the transport changed.

    Guards the logging planner telling the read-back WHICH entry the plan replaces: asked without the method, the
    read-back expects the entry appended, and every reconfigure of a declared logging method is answered 409."""
    d = tmp_repo / "modules" / "demo"
    d.mkdir(parents=True)
    (d / "module.yml").write_text(
        "key: demo\ncollections:\n  - name: ns.demo\nrole: demo\nlogs:\n"
        "  - {method: syslog_push, params: {transport: tcp}}   # the floor\n  - {method: journald_remote}\n",
        encoding="utf-8")
    plan = logsvc.build_logging_plan("demo", "syslog_push", params={"transport": "udp"})
    assert plan["error"] is None
    assert yaml.safe_load(plan["text_after"])["logs"] == [
        {"method": "syslog_push", "params": {"transport": "udp"}}, {"method": "journald_remote"}]
