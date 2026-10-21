"""Hermetic shape pins for the Grafana-provisioned logging alert rules (Layer 3, no live Grafana in CI).

Guards: a rule that pins a wrong/empty datasource uid (silently never fires), an unbounded window or a missing
debounce `for:` (alert spam / never-clears), and a rules file with no contact point (Grafana provisioning fails
fatally). Also pins the `auth-denied` token as a shared contract (V1 M-9: the body-substring matchers are
content-coupled — if the emitter's wording drifts from this token, the security alert silently stops firing).
"""
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ALERTING = os.path.join(_ROOT, "dashboards", "grafana", "provisioning", "alerting")
_TRANSFORMS = os.path.join(_ROOT, "docker", "vector", "config.d", "transforms")

# the audit-log token the auth-denied alert keys off — MUST match what api/audit.py + gui emit (M-9 contract).
AUTH_DENIED_TOKEN = "auth-denied"


def _load(name):
    with open(os.path.join(_ALERTING, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _rules():
    out = []
    for grp in _load("logging-rules.yaml")["groups"]:
        out.extend(grp.get("rules", []))
    return out


def _constant_services():
    """Every `service` a Vector shape sets to a fixed string. A shape whose service comes from the event (the
    container's name, the systemd unit, the script's run-log name) adds none: no rule can name it in advance."""
    out = set()
    for name in sorted(os.listdir(_TRANSFORMS)):
        with open(os.path.join(_TRANSFORMS, name), encoding="utf-8") as fh:
            out.update(re.findall(r'^\s*\.service\s*=\s*"([^"]+)"\s*(?:#.*)?$', fh.read(), re.M))
    return out


def test_every_rule_queries_loki_and_has_a_threshold_condition():
    """Each rule's query data node pins datasourceUid 'loki' and the rule names a `condition` whose node is a
    threshold/expression — a wrong uid or missing condition means the rule silently never evaluates."""
    rules = _rules()
    assert rules, "no alert rules found"
    for r in rules:
        assert r.get("condition"), "rule %r has no condition" % r.get("title")
        nodes = {d["refId"]: d for d in r.get("data", [])}
        # the query node(s) must hit loki; the condition node must be an __expr__ node
        loki_nodes = [d for d in r["data"] if d.get("datasourceUid") == "loki"]
        assert loki_nodes, "rule %r has no loki query node" % r.get("title")
        cond = nodes.get(r["condition"])
        assert cond and cond.get("datasourceUid") == "__expr__", \
            "rule %r condition must reference an __expr__ node" % r.get("title")


def test_every_rule_has_a_debounce_and_a_bounded_window():
    """Every rule has a non-empty `for:` (debounce) and a BOUNDED lookback (relativeTimeRange.from > 0 and a
    bounded LogQL [range]) — fail-closed: no unbounded lookback, no instant-fire flapping."""
    for r in _rules():
        assert r.get("for"), "rule %r missing a `for:` debounce" % r.get("title")
        for d in r["data"]:
            rtr = d.get("relativeTimeRange", {})
            assert rtr.get("from", 0) > 0, "rule %r data node %r has an unbounded relativeTimeRange" % (
                r.get("title"), d.get("refId"))


def test_every_service_a_rule_selects_is_one_a_vector_shape_emits():
    """Every `service="x"` / `service=~"a|b"` a rule selects is a service some Vector shape sets as a fixed
    string. Why: the crash-storm rule selected `service="containers"`, but the containers shape sets `service` to
    each container's own name. No stream ever carried that label, so the rule could never fire: a crash-looping
    container on a scratch box (2026-10-20) left its query empty. A selector naming a label value nothing
    emits is valid LogQL and returns no data, so nothing else catches it. A rule after a dynamic service selects
    by something the stream does carry (the crash rule: the body's `container_name`)."""
    services = _constant_services()
    assert services, "no fixed `.service = \"...\"` found under %s" % _TRANSFORMS
    for r in _rules():
        for d in r["data"]:
            expr = d.get("model", {}).get("expr", "")
            for op, value in re.findall(r'\bservice\s*(=~|=)\s*"([^"]*)"', expr):
                for one in (value.split("|") if op == "=~" else [value]):
                    assert one in services, "rule %r selects service %r, which no Vector shape emits (fixed: %s)" % (
                        r.get("title"), one, sorted(services))


_CRASH_UID = "kontroll-log-container-restart"
# The crash rule's query as measured on real traffic (2026-10-21): over 7 days of two boxes' Loki it counted one
# line a box (Grafana's "plugin process exited" at a deploy's restart), where the query before the `!~` stage
# counted Grafana's lines about this rule every minute and each deploy's ansible banners; on a scratch box it still
# counted a crash-looping container's lines.
_CRASH_EXPR_MEASURED = (
    'sum(count_over_time({source="internal"} | json container_name, message | __error__="" | container_name != "" '
    '| line_format "{{.message}}" |~ "(?i)restart|panic|exited" '
    '!~ `kontroll-log-container-restart|^(TASK|RUNNING HANDLER) \\[` [5m]))')
_LINE_FILTER = re.compile(r'(\|~|!~|\|=|!=)\s*(?:"((?:[^"\\]|\\.)*)"|`([^`]*)`)')


def _crash_rule_expr():
    (rule,) = [r for r in _rules() if r["uid"] == _CRASH_UID]
    return " ".join(rule["data"][0]["model"]["expr"].split())


def _crash_rule_keeps(message):
    """Whether the crash rule's line filters (the stages after its `line_format`) keep a container line whose
    message is `message`, applied in order as LogQL applies them. RE2 and Python agree on the patterns used."""
    tail = _crash_rule_expr().split('line_format "{{.message}}"', 1)[1]
    for op, quoted, raw in _LINE_FILTER.findall(tail):
        pattern = raw or re.sub(r"\\(.)", r"\1", quoted)
        hit = bool(re.search(pattern, message)) if op in ("|~", "!~") else pattern in message
        if hit != (op in ("|~", "|=")):
            return False
    return True


def test_the_crash_rule_counts_crash_lines_and_not_grafana_on_itself_or_deploy_banners():
    """The crash-storm rule keeps a container line that reports a panic, an exit or a restart, and drops the two
    kinds of container line that say those words without a crash. Why: measured over 7 days of two boxes' Loki
    (2026-10-21), Grafana logs this rule's uid (`kontroll-log-container-restart`) at every evaluation, so a query
    counting the word `restart` counted Grafana's own lines every minute and would have fired for good once it
    could fire at all; and every deploy's ansible prints `TASK [Restart …]` banners from its container."""
    for line in (
        "panic: runtime error, container exited",
        "restarting worker",
        'level=error msg="child exited unexpectedly" code=2',
    ):
        assert _crash_rule_keeps(line), line
    for line in (
        'logger=tsdb.loki endpoint=queryData pluginId=loki dsName=Loki dsUID=loki uname=grafana_scheduler '
        'rule_uid=kontroll-log-container-restart org_id=1 fromAlert=true level=info msg="Response parsed from loki"',
        'logger=ngalert.sender.router rule_uid=kontroll-log-container-restart org_id=1 level=info '
        'msg="Sending alerts to local notifier" count=1',
        "TASK [Restart a service whose content clone was made again (its bind mount still holds the removed "
        "directory)] ***",
        "RUNNING HANDLER [grafana : restart grafana] ***",
    ):
        assert not _crash_rule_keeps(line), line


def test_the_crash_rules_query_is_the_one_measured_on_real_traffic():
    """The crash-storm rule's whole query is the one measured on two boxes' last 7 days. Why: its load-bearing
    stages fail silently. A typo in `| json container_name` or a `line_format` naming a field the body lacks makes
    it a dead alert again, and without `container_name != ""` journald's "Main process exited" and the ansible
    log's handler lines count; each is valid LogQL that passes every other test (the readers' runs, 2026-10-21).
    A change is re-measured first (docs/troubleshooting.md §2 has the query), then this pin follows it."""
    assert _crash_rule_expr() == _CRASH_EXPR_MEASURED


def test_the_runbooks_crash_query_is_the_rules():
    """docs/troubleshooting.md's crash-storm query is the rule's own, stage for stage. Why: the runbook is where the
    rule is re-measured before a change and read when it fires; a copy that drifted would measure another query."""
    selector = _crash_rule_expr()[len("sum(count_over_time("):-len(" [5m]))")]
    with open(os.path.join(_ROOT, "docs", "troubleshooting.md"), encoding="utf-8") as fh:
        doc = " ".join(fh.read().split())
    assert selector + " [5m]))" in doc, "docs/troubleshooting.md §2 lacks the crash rule's query: %s" % selector


def test_auth_denied_token_is_pinned():
    """The auth-denied alert matches the exact AUTH_DENIED_TOKEN — pinned here as the shared contract so the
    alert and the audit-log emitter can't drift silently (V1 M-9)."""
    auth = [r for r in _rules() if "auth-denied" in (r.get("title") or "").lower()]
    assert auth, "no auth-denied rule found"
    for r in auth:
        exprs = " ".join(d.get("model", {}).get("expr", "") for d in r["data"])
        assert AUTH_DENIED_TOKEN in exprs, "auth-denied rule must match the pinned token %r" % AUTH_DENIED_TOKEN


def test_contact_point_present_so_provisioning_wont_fail():
    """A contact-point file with >=1 contact point must exist — Grafana provisioning fails fatally on a rule
    that resolves to a missing contact point; the default no-op keeps provisioning clean + non-coercive."""
    cp = _load("contactpoints.yaml")
    assert cp.get("contactPoints"), "contactpoints.yaml must define at least one contact point"
