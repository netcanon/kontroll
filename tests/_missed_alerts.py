"""Reads the two per-run alerts' LogQL as Loki reads it, for the tests of the per-device backup alert and the failed-job
alert (I-65). No Loki runs in the suite, so this reads each rule's pipeline in Python, and refuses any form it does not
know: a stage added, dropped or changed without this reader learning it fails the tests instead of passing unread.

- `pipeline(uid)` holds the rule's expression to its one known form (a stream selector, one `|=` line filter, the
  JSON body's fields with `__error__=""`, for the job rule a `container_name` match, `line_format "{{.message}}"`,
  then `| regexp` stages, all under one `sum by (...) (count_over_time(... [window]))`), and holds each regexp to what
  Python's `re` and Loki's RE2 read alike (named groups, `\\s`, `\\S`, classes and `+`; no lookaround, backreference
  or counted repeat).
- `read(uid, event)` is the series a Vector event (a dict: the body's `message`, `container_name`, and the stream's
  labels) adds to: its `by` labels, or None where the rule does not count the line.
- `events(text)` splits an ansible log into the events Vector's internal_ansible source makes (its multiline
  `start_pattern`: a new event at each line that starts with an ISO date); `run_id(message)` is the internal_ansible
  shape's extraction of the stream's `run_id` label, read from its VRL.
"""
import json
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(ROOT, "dashboards", "grafana", "provisioning", "alerting", "logging-rules.yaml")
SOURCE = os.path.join(ROOT, "docker", "vector", "config.d", "sources", "internal_ansible.yaml")
SHAPE = os.path.join(ROOT, "docker", "vector", "config.d", "transforms", "internal_ansible_shape.yaml")
BACKUP = "kontroll-log-backup-failure"
JOB = "kontroll-log-job-failed"

_TAIL = r'(?P<regexps>(?: \| regexp `[^`]+`)+) \[(?P<window>[0-9]+[smh])\]\)\)$'
_FORMS = {
    BACKUP: re.compile(
        r'^sum by \((?P<by>[a-z_]+(?:, [a-z_]+)*)\) \(count_over_time\('
        r'(?P<selector>\{source="internal", service="ansible"\}) '
        r'\|= "(?P<token>[A-Z-]+)" \| json message \| __error__="" \| line_format "\{\{\.message\}\}"' + _TAIL),
    JOB: re.compile(
        r'^sum by \((?P<by>[a-z_]+(?:, [a-z_]+)*)\) \(count_over_time\((?P<selector>\{source="internal"\}) '
        r'\|= "(?P<token>[A-Za-z ]+)" \| json container_name, message \| __error__="" '
        r'\| container_name = "(?P<container>[a-z-]+)" \| line_format "\{\{\.message\}\}"' + _TAIL),
}
# What RE2 and Python's re read alike, as these regexps use it: literals, named groups, \s \S, classes, `+`.
_PORTABLE = re.compile(r'(?:[A-Za-z0-9_=-]|\\[sS]|\(\?P<[a-z_]+>|\)|\[[a-z0-9-]+\]|\+)+')


def rule(uid):
    """The rule of logging-rules.yaml whose uid is `uid` (exactly one)."""
    with open(RULES, encoding="utf-8") as fh:
        groups = yaml.safe_load(fh)["groups"]
    (found,) = [r for g in groups for r in g["rules"] if r["uid"] == uid]
    return found


def expression(uid):
    """The rule's one Loki query, its whitespace as Loki reads it (a run of it is one space)."""
    (query,) = [d for d in rule(uid)["data"] if d.get("datasourceUid") == "loki"]
    return " ".join(query["model"]["expr"].split())


def pipeline(uid):
    """The rule's stages, held to the known form: a dict of `by` (the label names), `token` (the line filter),
    `selector` (the stream's label matchers), `container` (the job rule's), `regexps` (in order) and `window`.
    Raises AssertionError on any other form."""
    expr = expression(uid)
    m = _FORMS[uid].match(expr)
    assert m, "%s: a form this reader does not read: %r" % (uid, expr)
    regexps = re.findall(r"\| regexp `([^`]+)`", m.group("regexps"))
    for one in regexps:
        assert _PORTABLE.fullmatch(one), "%s: a regexp Python and RE2 may read differently: %r" % (uid, one)
        re.compile(one)
    selector = dict(re.findall(r'([a-z_]+)="([^"]*)"', m.group("selector")))
    return {"by": m.group("by").split(", "), "token": m.group("token"), "regexps": regexps, "selector": selector,
            "container": m.groupdict().get("container"), "window": m.group("window")}


def read(uid, event):
    """The labels of the series `event` adds to under the rule, or None where the rule does not count it. `event` is a
    Vector event as the Loki sink sends it: its JSON is the line, and its stream labels are its `labels` key."""
    p = pipeline(uid)
    labels = event.get("labels", {})
    if any(labels.get(name) != value for name, value in p["selector"].items()):
        return None
    body = {k: v for k, v in event.items() if k != "labels"}
    if p["token"] not in json.dumps(body, ensure_ascii=False):
        return None
    if p["container"] is not None and body.get("container_name") != p["container"]:
        return None
    line = str(body.get("message", ""))
    found = {}
    for one in p["regexps"]:
        m = re.search(one, line)
        if m:
            found.update({k: v for k, v in m.groupdict().items() if v is not None})
    series = dict(labels, **found)
    return {name: series.get(name, "") for name in p["by"]}


def events(text):
    """`text` (an ansible log) as Vector's internal_ansible source cuts it: a new event at each line its multiline
    `start_pattern` matches, the lines between joined with a newline."""
    with open(SOURCE, encoding="utf-8") as fh:
        start = re.compile(yaml.safe_load(fh)["multiline"]["start_pattern"])
    out = []
    for line in text.split("\n"):
        if start.match(line) or not out:
            out.append(line)
        else:
            out[-1] += "\n" + line
    return out


def run_id(message):
    """The `run_id` label the internal_ansible shape gives an event of `message` ("" where it finds none)."""
    with open(SHAPE, encoding="utf-8") as fh:
        (pattern,) = re.findall(r"parse_regex\(\.message, r'([^']+)'\)", fh.read())
    m = re.search(pattern, message)
    return m.group("rid") if m else ""
