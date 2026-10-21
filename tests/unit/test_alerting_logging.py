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


def _load_sink():
    with open(os.path.join(_ROOT, "docker", "vector", "config.d", "sinks", "loki.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


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


def test_every_rule_reads_no_lines_as_ok_and_a_failed_query_as_an_error():
    """Every rule sets `noDataState: OK` and `execErrState: Error`. Why: each rule is a `sum(count_over_time(...))`
    over matching lines, and when nothing matches Loki returns no series at all, not a 0. Under Grafana's default
    (NoData) all four rules sat in NoData on two boxes (2026-10-21) and sent a DatasourceNoData alert every minute, so
    the Alerting page showed every logging alert as live whether anything had happened or not. A failed query (Loki
    down) counts nothing either: under the default (Alerting) each rule fired under its own name once its `for` ran
    out, so a Loki outage read as a crash storm (Grafana 13.0.2, datasource on a closed port, scratch box,
    2026-10-21); with Error the rule shows Error, which is what happened."""
    for r in _rules():
        assert r.get("noDataState") == "OK", "rule %r must set noDataState: OK (no lines matched is not an alert)" % (
            r.get("title"))
        assert r.get("execErrState") == "Error", (
            "rule %r must set execErrState: Error (a failed query is not a crash storm)" % r.get("title"))


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


def _selectors(expr):
    """The stream selectors of a LogQL expression: the `{…}` a range function opens on (not a `{{.x}}` template)."""
    return re.findall(r"(?:count_over_time|rate|bytes_over_time|bytes_rate)\(\s*\{([^{}]*)\}", expr)


def test_every_label_a_rule_selects_is_one_the_sink_sets():
    """Every matcher in a rule's stream selector names a label the internal Loki sink sets, and a `source` matcher
    names the sink's own fixed value. Why: since each rule reads no matching lines as OK, a selector that matches
    no stream reads Normal for good, and a typo in a label name (`servce="ansible"`) or a value no stream carries
    (`source="internals"`) is valid LogQL that returns nothing. A round-2 reader's mutants of both kinds passed every
    other test (2026-10-21); a wrong `service` value is held by the test above."""
    sink = _load_sink()
    labels = set(sink["labels"])
    for r in _rules():
        for d in r["data"]:
            expr = " ".join(d.get("model", {}).get("expr", "").split())
            if not expr:
                continue
            selectors = _selectors(expr)
            assert selectors, "rule %r: no stream selector found in %r" % (r.get("title"), expr)
            for sel in selectors:
                matchers = re.findall(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"', sel)
                assert matchers and re.sub(r'\w+\s*(=~|!~|!=|=)\s*"(?:[^"\\]|\\.)*"|[\s,]', "", sel) == "", (
                    "rule %r: a selector this test cannot read: {%s}" % (r.get("title"), sel))
                for name, _op, value in matchers:
                    assert name in labels, "rule %r selects label %r, which the Loki sink never sets (sets: %s)" % (
                        r.get("title"), name, sorted(labels))
                    if name == "source":
                        assert value == sink["labels"]["source"], "rule %r selects source=%r; the sink sets %r" % (
                            r.get("title"), value, sink["labels"]["source"])


def _levels_by_service():
    """For each fixed `service` a Vector shape sets, the levels that shape sets: its fixed `.level = "x"` strings, or
    None when it also sets `level` from the event (any value can arrive then)."""
    out = {}
    for name in sorted(os.listdir(_TRANSFORMS)):
        with open(os.path.join(_TRANSFORMS, name), encoding="utf-8") as fh:
            text = fh.read()
        values = re.findall(r"^\s*\.level\s*=\s*(.+?)\s*(?:#.*)?$", text, re.M)
        sets = [re.fullmatch(r'"([^"]+)"', v) for v in values]
        levels = {m.group(1) for m in sets} if sets and all(sets) else None
        for service in re.findall(r'^\s*\.service\s*=\s*"([^"]+)"\s*(?:#.*)?$', text, re.M):
            prior = out.get(service, set())
            out[service] = None if levels is None or prior is None else prior | levels
    return out


def _matches(op, value, candidate):
    """Whether a LogQL label matcher `<op> "value"` holds for a label equal to `candidate` (regexes are anchored)."""
    hit = candidate == value if op in ("=", "!=") else bool(re.fullmatch(value, candidate))
    return hit == (op in ("=", "=~"))


def test_every_level_a_rule_selects_is_one_its_services_emit():
    """A `level` matcher on a rule whose selected services each set `level` only to fixed strings matches one of
    those strings. Why: the audit shapes set `level="audit"` and the ansible shape `level="info"`, so an added
    `level="error"` on the auth-denied or the backup rule matches no stream, and since each rule reads no matching
    lines as OK it reads Normal for good. A round-2 reader asked for this clause; a round-4 reader added that matcher
    to the auth-denied rule and every test passed (2026-10-21). A rule that selects a service whose level comes from
    the event (containers, journald) is not held here: any level can arrive on it."""
    levels_by_service = _levels_by_service()
    assert levels_by_service.get("api-audit") == {"audit"}, levels_by_service
    for r in _rules():
        for d in r["data"]:
            for sel in _selectors(" ".join(d.get("model", {}).get("expr", "").split())):
                matchers = re.findall(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"', sel)
                services = [one for name, op, value in matchers if name == "service"
                            for one in (value.split("|") if op == "=~" else [value])]
                if not services or any(levels_by_service.get(one) is None for one in services):
                    continue
                emitted = set().union(*(levels_by_service[one] for one in services))
                for name, op, value in matchers:
                    if name == "level":
                        assert any(_matches(op, value, one) for one in emitted), (
                            "rule %r selects level %s%r, but %s only emit %s" % (
                                r.get("title"), op, value, services, sorted(emitted)))


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
