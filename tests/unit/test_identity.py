"""identity — the module-IDENTITY reconfigure instance (Phase 4b, #139).

These pin the heaviest non-platform reconfigure: a diff-able, comment-preserving change to a device-class's
top-level identity keys (secrets_domain / inventory_group / backend / role / status) in modules/<key>/module.yml.
WHY (the failures they guard): before 4b an identity change had no surgical seam and fell off into a whole-file
Overwrite (design 22 §5.4 G7) — silently flattening a hand-written module. And an identity re-point RE-HOMES
hosts / ORPHANS creds, so it MUST carry the heaviest gate: each test asserts the change is classified
`severity: identity` (not a plain modify), that a proposal may change AT MOST ONE identity key at a time (so the
type-to-confirm value is unambiguous), that the comment-preserving write keeps the module's annotations, that the
config-injection guard rejects a crafted value BEFORE the write, and that the plan_token gates the exact bytes.
The regen-against-the-real-tree is out of scope (the plan stages module.yml only); these run under tmp_repo.
"""
import pytest
import yaml

from kontroll.service import identity, promote

pytestmark = pytest.mark.unit

# A hand-written module WITH comments + all five identity keys — the shape the write must never flatten.
_MODULE = """# Device-class module: a network switch (hand-written, with comments).
key: demo_sw
description: demo switch
status: active
collections:
  - name: cisco.ios
role: backend_network_cli
backend: network_cli
inventory_group: core_switch   # the dispatch seam — this comment must survive
secrets_domain: network
metrics:
  - {method: snmp, params: {module: if_mib}}
"""

# The full declared identity set the renderer submits, pre-filled from current (one value changed per test).
_CURRENT = {"status": "active", "role": "backend_network_cli", "backend": "network_cli",
            "inventory_group": "core_switch", "secrets_domain": "network"}


def _write_module(tmp_repo, key, text):
    d = tmp_repo / "modules" / key
    d.mkdir(parents=True)
    (d / "module.yml").write_text(text, encoding="utf-8")
    return d / "module.yml"


def _sel(**overrides):
    return {"values": dict(_CURRENT, **overrides)}


def test_propose_is_pure(tmp_repo):
    """build_plan is PURE — computing an identity plan leaves module.yml byte-identical. Guards the propose/
    promote boundary: reviewing an identity change must never mutate the repo (only promote writes)."""
    p = _write_module(tmp_repo, "demo_sw", _MODULE)
    before = p.read_text(encoding="utf-8")
    plan = identity.build_plan("demo_sw", _sel(inventory_group="edge_firewall"))
    assert plan["error"] is None
    assert p.read_text(encoding="utf-8") == before


def test_identity_change_is_modify_upgraded_to_identity_and_preserves_comments(tmp_repo):
    """Changing inventory_group is a `modify` UPGRADED to `severity: identity` (the descriptor knob_meta), the
    write swaps the value in place keeping the inline comment + siblings, and the plan_token gates the exact
    bytes. THE load-bearing case: a re-home must fire the type-to-confirm, not a plain overwrite-confirm, and
    must not flatten the module."""
    p = _write_module(tmp_repo, "demo_sw", _MODULE)
    plan = identity.build_plan("demo_sw", _sel(inventory_group="edge_firewall"))
    assert plan["error"] is None and plan["severity"] == "identity"
    chg = [c for c in plan["changes"] if c["path"] == "inventory_group"]
    assert chg and chg[0]["kind"] == "modify" and chg[0]["severity"] == "identity"
    assert chg[0]["before"] == "core_switch" and chg[0]["after"] == "edge_firewall"
    assert "# the dispatch seam" in plan["text_after"] and "secrets_domain: network" in plan["text_after"]
    # the token verifies against the bytes it will write, and fails for any drift (anti-drift + no-quiet-upgrade)
    assert promote.verify_token(plan["plan_token"], *plan["token_parts"]) is True
    assert promote.verify_token(plan["plan_token"], plan["text_after"] + "x", plan["token_parts"][1]) is False
    identity.apply_plan(plan)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["inventory_group"] == "edge_firewall"


def test_no_drop_struct_is_the_full_module_not_the_edited_key(tmp_repo):
    """build_plan feeds verify_no_drop the FULL module top-level keys (collections / metrics / every identity
    key), NOT just the edited key — so a future writer dropping a co-owner key fails closed at promote. Guards
    review SHOULD-FIX 1: upsert_top_level_key only replaces/appends (a legit edit keeps every key → passes), but
    the gate can now SEE a drop instead of being fed a single-key struct in which a drop is inexpressible."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    plan = identity.build_plan("demo_sw", _sel(inventory_group="edge_firewall"))
    assert {"collections", "metrics", "secrets_domain"} <= set(plan["current"])   # the FULL module, not {inventory_group}
    from kontroll.service._reconfig import verify_no_drop
    dropped = {k: v for k, v in plan["proposed"].items() if k != "secrets_domain"}  # a vanished co-owner identity key…
    assert verify_no_drop(plan["current"], dropped, plan["changes"]) is False        # …now fails closed
    assert verify_no_drop(plan["current"], plan["proposed"], plan["changes"]) is True   # the real edit passes


def test_same_values_is_a_noop(tmp_repo):
    """Submitting the current values unchanged is an idempotent no-op: error None, empty change-set, severity
    None, text_after == text_before (writes nothing). Guards a re-submit of an unchanged identity form being
    treated as a clobber or duplicating a key."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    plan = identity.build_plan("demo_sw", _sel())
    assert plan["error"] is None and plan["changes"] == [] and plan["severity"] is None
    assert plan["text_after"] == plan["text_before"]


def test_two_identity_keys_at_once_is_refused(tmp_repo):
    """Changing TWO identity-severity keys in one proposal is `multi_identity` (refused) — so the
    type-to-confirm value stays unambiguous (one re-home/re-classify at a time). Guards a single GUI action
    re-homing AND re-crediting a class behind one confirm."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    plan = identity.build_plan("demo_sw", _sel(inventory_group="edge_firewall", secrets_domain="semaphore"))
    assert plan["error"] == "multi_identity"


def test_a_status_modify_may_ride_with_one_identity_change(tmp_repo):
    """A `status` flip (severity `modify`) MAY accompany a single identity change — two changes, max severity
    `identity`. Guards the rule being over-strict (blocking a benign status flip alongside the re-home)."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    plan = identity.build_plan("demo_sw", _sel(inventory_group="edge_firewall", status="staged"))
    assert plan["error"] is None and plan["severity"] == "identity"
    assert {c["path"] for c in plan["changes"]} == {"inventory_group", "status"}


def test_a_crafted_value_is_rejected_before_the_write(tmp_repo):
    """A value outside the knob's pattern/allow-list (here a YAML-metacharacter injection into a slug key) is
    `bad_value` — the P0a config-injection guard, server-side, BEFORE any byte is written. Guards a crafted
    identity value ever reaching the rendered module.yml."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    plan = identity.build_plan("demo_sw", _sel(secrets_domain="network\ninjected: pwned"))
    assert plan["error"] == "bad_value" and plan["field"] == "secrets_domain"


def test_unknown_field_and_not_onboarded(tmp_repo):
    """A submitted key that is not a declared identity knob is `unknown_field`; a class with no module.yml is
    `not_onboarded`. Guards the dialog proposing a write for a typo'd/forged field or an un-onboarded class."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    assert identity.build_plan("demo_sw", {"values": {"bogus": "x"}})["error"] == "unknown_field"
    assert identity.build_plan("ghost", _sel())["error"] == "not_onboarded"


def test_apply_is_idempotent(tmp_repo):
    """Applying the same identity plan twice writes once — a second apply is changed=False. Guards a
    non-idempotent identity write (the 0-changed rule)."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    plan = identity.build_plan("demo_sw", _sel(role="backend_ssh"))
    assert identity.apply_plan(plan)["changed"] is True
    # a re-build sees the new value already current → no change; re-applying the SAME plan writes nothing.
    assert identity.apply_plan(plan)["changed"] is False
    re_plan = identity.build_plan("demo_sw", _sel(role="backend_ssh"))
    assert re_plan["changes"] == [] and re_plan["text_after"] == re_plan["text_before"]


def test_a_writer_s_own_refusal_is_a_bad_value_not_an_exception(tmp_repo, monkeypatch):
    """With the knob validator replaced by one that accepts anything — a future identity knob whose validator is
    looser than the scalar writer — a value the writer refuses comes back as `bad_value` and nothing is raised.
    Guards the writers' fail-closed check reaching the identity route (which has no `try`) as an uncaught error."""
    _write_module(tmp_repo, "demo_sw", _MODULE)
    monkeypatch.setattr(identity, "validate_value", lambda knob, value: None)
    plan = identity.build_plan("demo_sw", _sel(role="null"))
    assert plan["error"] == "bad_value" and "plain YAML" in plan["detail"]
    assert identity.build_plan("demo_sw", _sel(role="opnsense_firewall"))["error"] is None


def test_the_enact_hint_stages_only_generator_output():
    """No enact command the identity planner hands the operator contains a blanket add (`add -A`, `add --all`,
    `add .`): the regenerate step stages exactly the directories the two generators it runs write — read here from
    the generators' own source, so the hint cannot drift from them. Guards the GUI telling an operator to stage
    their whole worktree by hand, with no gate in front of it: the hint used to end in `git add -A && git
    commit`."""
    import importlib.util
    import os
    import re
    for changes in ([{"kind": "modify", "path": "inventory_group"}], [{"kind": "modify", "path": "status"}], []):
        for step in identity.identity_enact_commands("demo_sw", changes):
            assert not re.search(r"\badd\s+(-A|--all|\.)(\s|$)", step["cmd"]), step["cmd"]
    regen = identity.identity_enact_commands("demo_sw", [])[-1]["cmd"]
    staged = regen.split("git add -- ", 1)[1].split(" && ", 1)[0].split()
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _load(name):
        spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(root, "scripts", name))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    logging_gen = _load("gen-logging.py")
    observability_source = io_open(os.path.join(root, "scripts", "gen-observability.py"))
    assert '"prometheus/targets/%s/%s.generated.yml"' in observability_source
    assert sorted(staged) == sorted(["prometheus/targets", logging_gen.GEN_DIR,
                                     os.path.dirname(logging_gen.LOKI_OVERRIDES_REL)])
    assert "gen-observability.py" in regen and "gen-logging.py" in regen


def io_open(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("stored", ["all", "lab-switches", "a" * 80])
def test_a_value_read_back_unchanged_is_not_judged(tmp_repo, stored):
    """A class whose module file holds a group the knob would not accept as a NEW value — a name Ansible reserves,
    a hyphen the onboard field allows, a name longer than the bound — can still have ANOTHER identity key edited:
    the dialog submits every knob pre-filled, and the unchanged group is read back, not chosen. The same word
    chosen for a class that does not hold it is a `bad_value`.

    Guards the tightened knob making an existing class un-editable (every edit refused over a field the operator
    did not touch), without loosening what the knob will store."""
    _write_module(tmp_repo, "demo_sw", _MODULE.replace("inventory_group: core_switch", "inventory_group: %s" % stored))
    plan = identity.build_plan("demo_sw", {"values": dict(_CURRENT, inventory_group=stored, role="backend_other")})
    assert plan["error"] is None and [c["path"] for c in plan["changes"]] == ["role"]
    _write_module(tmp_repo, "other_sw", _MODULE.replace("demo_sw", "other_sw"))
    chosen = identity.build_plan("other_sw", _sel(inventory_group=stored))
    assert chosen["error"] == "bad_value" and chosen["field"] == "inventory_group"


@pytest.mark.parametrize("stored,text", [(123, "123"), ({"a": 1}, "{a: 1}")], ids=["a-number", "a-mapping"])
def test_a_stored_value_that_is_not_a_string_is_still_judged(tmp_repo, stored, text):
    """A module file that holds a number, or a mapping, for an identity key, and a submission of that same value,
    is a `bad_value` for that key: only a STRING the file already holds is read back unjudged.

    Guards the read-back rule widening into "whatever equals the file passes": a value that is not a string is a
    damaged file, the validator must still be what answers it — with the field named — and a client that echoes
    it must not get a plan for it."""
    _write_module(tmp_repo, "demo_sw", _MODULE.replace("inventory_group: core_switch", "inventory_group: %s" % text))
    plan = identity.build_plan("demo_sw", {"values": {"inventory_group": stored}})
    assert plan["error"] == "bad_value" and plan.get("field") == "inventory_group"


@pytest.mark.parametrize("text", ["- key: demo_sw\n", "just a word\n"])
def test_a_module_file_that_is_not_a_mapping_is_repository_data(tmp_repo, text):
    """An identity plan for a class whose module file parses to a list or a bare word stops with
    `paths.RepositoryDataError` naming the class and the file — the onboard's answer for the same file.

    Guards the 500 it was (an AttributeError out of the first `.get`): the second reader of a module file must
    not answer the same damaged file differently from the first."""
    from kontroll import paths
    _write_module(tmp_repo, "demo_sw", text)
    with pytest.raises(paths.RepositoryDataError) as stop:
        identity.build_plan("demo_sw", _sel(role="backend_other"))
    assert stop.value.what == "module file" and "modules/demo_sw/module.yml" in str(stop.value)


@pytest.mark.parametrize("text", ["[]\n", "false\n", "0\n", "''\n"])
def test_a_module_file_that_is_an_empty_non_mapping_is_repository_data_too(tmp_repo, text):
    """A module file that parses to an empty list, `false`, zero or an empty string is not a mapping either: the
    identity plan stops with `RepositoryDataError`, as the onboard does for the same file.

    Guards `or {}` turning a document that is not a mapping into an empty class: the writer then appended a key
    to text that is not a mapping, and the plan ended in a parser error (a 500)."""
    from kontroll import paths
    from kontroll.service import onboard
    _write_module(tmp_repo, "demo_sw", text)
    with pytest.raises(paths.RepositoryDataError) as stop:
        identity.build_plan("demo_sw", _sel(role="backend_other"))
    assert stop.value.what == "module file"
    with pytest.raises(paths.RepositoryDataError):
        onboard._existing_module("demo_sw")


def test_an_empty_module_file_is_still_an_empty_class(tmp_repo):
    """An empty module file, or one that holds only comments, is a class with no keys yet: the plan ADDS every key
    the dialog submits, and none of them is a refusal.
    Guards the non-mapping refusal swallowing the one falsy document that IS "no keys yet"."""
    _write_module(tmp_repo, "demo_sw", "# nothing yet\n")
    plan = identity.build_plan("demo_sw", _sel(role="backend_other"))
    assert plan["error"] is None
    assert {c["kind"] for c in plan["changes"]} == {"add"}
    assert {c["path"]: c["after"] for c in plan["changes"]} == dict(_CURRENT, role="backend_other")
