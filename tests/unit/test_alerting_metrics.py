"""Hermetic pins for the alerts that reach someone (T32): the metrics rules, the heartbeat, the policy tree, the one
contact point, and the wiring that hands that contact point the operator's URL.

WHY (the failure this guards). Before T32 the logging rules fired into a placeholder contact point that no policy
named, so Grafana's default policy routed every alert nowhere useful, and no rule watched a scrape target at all: a
dead device read as a quiet dashboard (docs/logging-architecture.md G6). The pins below hold:
- every alert routes to the one contact point, whose URL is the operator's secret and never a literal in the tree;
- the URL travels the SOPS domain → deploy-stack's no_log `.env` render → Grafana's environment, an unset URL falls
  back to a closed loopback port and is announced, and no secret_env_map drop-in may shadow the name;
- a down rule fires only for a target that answered within its window, so a placeholder target never pages and a
  decommissioned one stops; the blackbox targets, whose `up` reads the exporter, are watched by `probe_success`;
- the heartbeat always fires, on a route of its own, and nothing else carries its label.
The two down expressions run through `promtool test rules` where promtool is installed (the validate image has it).
"""
import glob
import json
import os
import re
import shutil
import subprocess

import jinja2
import pytest
import yaml
from _crash_filter import compile_re2, stages
from _deploy_walk import Walk, args, load, module_words, strings

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ALERTING = os.path.join(_ROOT, "dashboards", "grafana", "provisioning", "alerting")
_CRASH_UID = "kontroll-log-container-restart"
# The down rules' shape: the metric is 0 now, and was 1 at some point in the bounded window.
_DOWN = re.compile(r"^\((\w+) == bool 0\) and on \(job, instance\) \(max_over_time\((\w+)\[(\d+[smhdw])\]\) == 1\)$")


def _load(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _rules(name):
    return [r for g in _load("dashboards", "grafana", "provisioning", "alerting", name)["groups"] for r in g["rules"]]


def _alerting_docs(where=_ALERTING):
    """(file name, document) for every entry under provisioning/alerting/, a hidden one included: Grafana's alerting
    provisioner reads them all, and refuses the directory on an entry without a `.yaml`, `.yml` or `.json` suffix
    (reasoned from its config reader), so each entry must be a file with one. A JSON file is read as JSON, which
    Grafana reads and YAML refuses when it is tab-indented."""
    out = []
    for name in sorted(os.listdir(where)):
        path = os.path.join(where, name)
        assert os.path.isfile(path) and name.endswith((".yaml", ".yml", ".json")), (
            "Grafana's alerting provisioner reads every entry here and refuses one that is not a .yaml, .yml or "
            ".json file: %s" % name)
        with open(path, encoding="utf-8") as fh:
            out.append((name, json.load(fh) if name.endswith(".json") else yaml.safe_load(fh)))
    return out


def _groups():
    return [g for _, doc in _alerting_docs() for g in (doc or {}).get("groups") or []]


def _all_rules():
    return [r for g in _groups() for r in g.get("rules") or []]


def _crash_words(rules):
    """What the crash-storm rule counts, read from its own line filters: each `|~` stage's pattern and each `|=`
    stage's text, compiled (`_crash_filter.stages` fails on a stage it cannot read)."""
    (crash,) = [r for r in rules if r["uid"] == _CRASH_UID]
    return [compile_re2(p if op == "|~" else re.escape(p)) for op, p in stages(_query(crash)["model"]["expr"])
            if op in ("|~", "|=")]


def _query(rule):
    (node,) = [d for d in rule["data"] if d.get("datasourceUid") != "__expr__"]
    return node


def _deploy_tasks():
    return [t for play in _load("ansible", "playbooks", "deploy-stack.yml") for t in play.get("tasks") or []]


def test_every_alert_routes_to_the_one_contact_point():
    """The policy tree's root and every route send to `kontroll-default`, the only contact point provisioned, and the
    old placeholder is deleted by its org and uid. The root groups by folder and rule, flushes a group every 5m and
    re-sends a firing one every 12h, and the receiver sends resolves. Guards an alert routed to a contact point that
    does not exist (Grafana refuses the whole provisioning), the pre-T32 shape (a contact point no policy names, so
    nothing is sent), and the timings the docs promise: a `repeat_interval` of a minute passed every other test (an
    alert a minute to a phone), as did a lost resolve (T32 read A, S4)."""
    points = _load("dashboards", "grafana", "provisioning", "alerting", "contactpoints.yaml")
    names = {cp["name"] for cp in points["contactPoints"]}
    assert names == {"kontroll-default"}
    (tree,) = _load("dashboards", "grafana", "provisioning", "alerting", "policies.yaml")["policies"]
    assert tree["receiver"] == "kontroll-default"
    assert {r["receiver"] for r in tree.get("routes", [])} <= names
    assert points["deleteContactPoints"] == [{"orgId": 1, "uid": "kontroll-logging-noop"}]
    assert (tree["group_by"], tree["group_interval"], tree["repeat_interval"]) == (
        ["grafana_folder", "alertname"], "5m", "12h")
    (receiver,) = points["contactPoints"][0]["receivers"]
    assert receiver["disableResolveMessage"] is False


def test_the_notifier_url_comes_from_the_environment_never_the_tree():
    """The contact point's URL is `$KONTROLL_ALERT_URL`, and no provisioned alerting file holds a URL of its own.
    Guards the operator's notifier URL — the credential of every URL-shaped notifier (an ntfy topic, a webhook
    token) — being written into a tracked file, and a hard-coded receiver that sends every box's alerts to one
    place."""
    (receiver,) = _load("dashboards", "grafana", "provisioning", "alerting", "contactpoints.yaml")[
        "contactPoints"][0]["receivers"]
    assert receiver["type"] == "webhook" and receiver["settings"]["url"] == "$KONTROLL_ALERT_URL"
    for name, doc in _alerting_docs():
        urls = [s for _, s in _paths(doc) if "://" in s]
        assert urls == [], (name, urls)


def _paths(value, at=()):
    """(path, string) for every key and string value in a parsed document, a `!!binary` value decoded as text
    (Grafana's YAML reader hands its bytes to a string field)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield at + (k,), str(k)
            yield from _paths(v, at + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _paths(v, at + (i,))
    elif isinstance(value, (str, bytes)):
        yield at, value if isinstance(value, str) else value.decode("utf-8", "replace")


def test_the_only_environment_reference_in_alerting_is_the_notifier_url():
    """Across every file under provisioning/alerting/, the only `$NAME` or `${…}` reference anywhere in a string (a `{{
    … }}` template action included; a `$$` literal is none) is the contact point's `url: $KONTROLL_ALERT_URL`, in a key
    or a value. Why: Grafana interpolates such a reference from its own environment when it provisions, and that
    environment holds `GF_SECURITY_ADMIN_PASSWORD` and the URL itself; one in a rule's label or annotation would send
    the admin password with every alert, or the URL in the daily heartbeat, with every other test green (T32 read B,
    S3). Its interpolation does not read template syntax (a template variable is written `$$labels`), so a reference
    inside a template action is expanded too: skipping those actions passed one (fold 1 read B, S1: E4). One of Go's
    one-character names (`$1`, `$*`, `$#`, `$@`, `$!`, `$?`, `$-`) counts too: Grafana expands it to nothing, so a
    `label_replace` replacement `$1` silently becomes `""` (write `$$1`); and a hidden file and a `!!binary` value are
    read, as Grafana reads them (fold 2 read B, N3: E5 to E7)."""
    refs = []
    for name, doc in _alerting_docs():
        for at, text in _paths(doc):
            text = text.replace("$$", "")
            refs += [(name, at, m) for m in re.findall(r"\$(?:[A-Za-z_{]\w*|[0-9*#@!?-])", text)]
    url = ("contactPoints", 0, "receivers", 0, "settings", "url")
    assert refs == [("contactpoints.yaml", url, "$KONTROLL_ALERT_URL")], refs


def test_the_url_travels_from_the_secret_through_the_no_log_render():
    """The URL is an optional `password` field of the dashboards secret form, shaped `http_url`; deploy-stack checks
    that shape in a no_log task (a bool) and refuses a URL out of it in an assert that prints only its own words, before
    it renders the URL single-quoted into `.env` (mode 0600) inside the no_log task; it reduces it to a bool in another
    no_log task and says so when it is unset; the domain is decrypted once, as `_dash`; every task or handler that reads
    it (`_dash`, any key of it, `ansible_failed_result` or `ansible_failed_task`, or a fact, register, add_host var or
    include `vars:` set from a task that reads it), in the play, in a task file it imports or includes or in a role it
    runs, each read in the places `_deploy_walk` lists (a role's meta dependencies and its `tasks_from`/`handlers_from`
    included, a role named in the k=v form too), and every task a reading include brings in, is no_log, each template a
    task renders (under `action:`, `local_action:` or `args:` too, or in a `with_template` loop under any prefix) or a
    template lookup reads, with the templates it includes, read as the task's own text (a template task that is not
    no_log prints the rendered file under the mandated `--check --diff`), and no task's name, nor an assert's or a
    fail's message, templates it, nor does any task hand it to a lookup, a `with_<lookup>` loop's terms included
    (Ansible prints a name, no_log or not, and 2.19 a failing task's message, a lookup's error naming its term, under
    no_log); no task that is not no_log prints every var: no templated string of it or of a template it renders, nor a
    debug's `var:` expression, reads `vars`, `hostvars`, `ansible_facts` or a `vars` lookup (a `copy` content or a
    `lineinfile` line prints under `--check --diff`); no play var, `vars_files` or include_vars var, role defaults or
    vars, meta dependency param, var of an inventory file at any depth, its group_vars or host_vars as shipped, or
    playbook group_vars or host_vars var reads a tainted name, a template it looks up read as its text; the two bools
    are exempt on their one setter only, each setter's value pinned as written; and no play key but its vars and task
    lists, nor any task's or include's `apply:` `environment:`, nor a task's `delegate_to`, `notify` or `raw` or
    `script` argument, reads it, as `-vvv` prints an environment, a `module_defaults` invocation and a raw command even
    under no_log, and Ansible prints a delegated host and a missing handler's name (a debug of a derived var, or a
    reader in an imported file, went unseen: fold 1 read B, N1, R1 and R2; a role's task, a derived fact and those keys:
    fold 2 read B, N1; a role's meta dependency, defaults or `handlers_from`, an include's `vars:` or loop, whose no_log
    does not reach the tasks it includes, an include_vars or group_vars file, a second decrypt and a bool re-set
    elsewhere: fold 3 read B, N1; a templated name, an `apply:` environment and the files the walk did not resolve as
    Ansible does: fold 4's reads 90, N6 and 100, N3; a failing no_log assert's message, a template, the inventory's
    group_vars, a file Ansible finds before the one the walk read, a debug of `vars`, a dependency's params,
    `delegate_to`, `notify` and `raw`: fold 5's reads 111, S1, S2, N1 and 110, S2; a k=v debug or a template of `vars`,
    `ansible_facts`, a rescue that prints the failed result, a lookup handed the URL, an add_host var, a template
    lookup, a template task under `action:` or `args:`, and the files the walk found in one of Ansible's places where
    Ansible read another: fold 6's reads 120, S1, S2, S3, N1, N2 and 121, S1, S2, S3, N1-N5; a template under a prefixed
    `with_template`, a lookup with a space before its parenthesis, a no_log loop over a lookup handed the URL, a debug
    `var:` that prints every var past its start, a rescue's `ansible_failed_task`, an exempt bool's setter set to the
    URL, an inventory subdirectory's file, and files Ansible loaded from places the walk did not search: fold 7's reads
    130, S1, S2, S4, S5 and 131, S1, S2, S5, S6, S7; and a form the walk does not model, which now fails it,
    `Unfollowed`: 131, S3, S4, S5; and an include or a lookup a quoted `'{#'` hid from a pattern, a name or a failure
    message that templates every var, `ansible_facts.items()`, a role's include under a nested `tasks/`, an import
    entry's tags, and a `.env` writer no test held no_log: fold 8's reads 140, S1, S2, S3, N1 and 141, N2). Every task
    that writes the stack's `.env` (`_env_writers`: a content-writing module under any prefix, by any name for its
    file, written `{{ stack_dir }}/.env`; a writer that names a `.env` another way fails the pin: fold 9's reads 151,
    N2 and 150, N-C; and so does one whose file or src ends in no name written as it is, whose src names a `.env`,
    that has no file or whose `args:` is no mapping: fold 11's reads 171, S2 and 170, S-5), is no_log, the digest
    pin's blockinfile too, as `--check --diff` prints the whole file; no other task names the `.env` but as compose's
    `--env-file` before `up`, `restart` or `ps` or in a message's words (`_env_readers`: 171, S3), nor reads the
    stack's env with no `.env` named (`_reads_the_stack`: compose's `config`, `exec` or `run`, `docker inspect`,
    `exec` or `top`, a `cp`, a written `commit`, a `logs` or an `attach` of Grafana's, its `start` attached, a compose
    `up` neither detached nor waiting, the Engine API over a unix socket by `uri` or curl; the command by its path,
    quoted, or written by a Jinja block in a command's place, past a redirect, a line's continuation, a comment or sh's
    quoting of an option's value, nested too, in a quoted run, each read as a command of its own (as a shell's `-c`, `su
    -c` or `eval` runs one), or in a lookup that writes no space: fold 14's read 200; fold 15's reads 210 and 211; fold
    16's reads 220 and 221), no var or play key holds the file's path or a lookup of it (`_env_vars`) and no link under
    docker/, nor any in the tree, may name it (`_link_leaks`, over `_links_under` and `_tree_links`: fold 12's read 181,
    S4, S5; fold 13's reads 190, S-3, S-4, S-5 and 191, S-c, S-e; fold 15's read 211, N-4); no file the walk read sets
    `module_defaults` (171, S2); and no task sets stats, nor does ansible.cfg set `show_custom_stats`, which prints a
    run's custom stats at its end, no_log or not (151, N5). It reads all of it over `_deploy_walk.Walk`, the playbooks
    deploy-stack imports included, through `_url_leaks`, which test_the_url_pin_finds_each_leak_shape holds to each
    shape; grafana.yaml passes it to the container with a non-empty loopback default, and no other service fragment
    names it; and the name is reserved from secret_env_map drop-ins. Guards the URL, or a password beside it in the
    domain, leaking into a deploy log (a `debug: var=_dash` named no key, T32 read B, N1); a URL that ends the `.env`
    line's quote, which compose then prints, or stops the redaction of Grafana's failed-delivery line (read B, S1; fold
    2 read A, N1); a `.env` other users can read, or another container handed the URL (read B, N6); an unset URL failing
    the provisioning (an empty webhook URL is invalid) or passing in silence; and a drop-in that shadows the operator's
    notifier."""
    form = _load("secret-forms", "dashboards.yml")
    (field,) = [f for f in form["fields"] if f["key"] == "alert_notifier_url"]
    assert field["type"] == "password" and field.get("required") is False and field.get("shape") == "http_url"
    tasks = _deploy_tasks()
    (env,) = [t for t in tasks if "KONTROLL_ALERT_URL=" in str(t.get("ansible.builtin.copy", {}).get("content", ""))]
    assert env.get("no_log") is True and env["ansible.builtin.copy"]["mode"] == "0600"
    content = env["ansible.builtin.copy"]["content"]
    assert "KONTROLL_ALERT_URL='{{ _dash.alert_notifier_url | default('', true) }}'" in content
    (shape,) = [t for t in tasks if "_alert_url_ok" in (t.get("ansible.builtin.set_fact") or {})]
    assert shape.get("no_log") is True and "when" not in shape
    # each bool's value as written: `_url_leaks` exempts a bool's one setter whatever it sets (fold 7's read 131, S7)
    assert shape["ansible.builtin.set_fact"] == {"_alert_url_ok": (
        "{{ _u == '' or (_u is match('https?://.') and _u.isprintable()\n"
        "   and (_refused | select('in', _u) | list | length) == 0) }}")}
    assert shape["vars"] == {"_u": "{{ _dash.alert_notifier_url | default('', true) | string }}",
                             "_refused": [" ", '"', "'", "\\", "$", "#"]}
    (refuse,) = [t for t in tasks if "ansible.builtin.assert" in t and "_alert_url_ok" in str(t)]
    assert refuse["ansible.builtin.assert"]["that"] == "_alert_url_ok | bool" and "when" not in refuse
    assert set(refuse) == {"name", "ansible.builtin.assert"}, sorted(refuse)   # no ignore_errors: it stops the run
    assert "{{" not in refuse["ansible.builtin.assert"]["fail_msg"] and not refuse.get("no_log")
    assert tasks.index(shape) < tasks.index(refuse) < tasks.index(env)
    w = Walk(_ROOT)
    found = _url_leaks(w)
    every = found["every"]
    (decide,) = [t for t in every if "_has_alert_url" in (args(t, "set_fact") or {})]
    (setter,) = [t for t in every if "_alert_url_ok" in (args(t, "set_fact") or {})]
    assert setter == shape and decide.get("no_log") is True
    assert decide["ansible.builtin.set_fact"] == {
        "_has_alert_url": "{{ (_dash.alert_notifier_url | default('', true) | string | trim | length) > 0 }}"}
    assert not found["tainted"] & {"_alert_url_ok", "_has_alert_url"}, found["tainted"]
    for leak in ("unlogged", "named", "message", "outside", "derived"):
        assert found[leak] == [], (leak, found[leak])
    # `_dash` once; `_device_domains` is the device-credential domains, each by `kontroll_sops_domain`, which refuses
    # a platform-core name such as dashboards (test_kontroll_sops.py)
    assert found["decrypts"] == ["_dash", "_device_domains"], found["decrypts"]
    assert env in found["readers"] and shape in found["readers"]
    # every task that writes the stack's `.env` (`_env_writers`) is no_log, the digest pin's blockinfile too, which
    # reads no tainted name: under the mandated --check --diff it prints the whole file, the URL's line in it (fold 8's
    # read 141, N2)
    writers = _env_writers(every)
    assert env in writers and any("ansible.builtin.blockinfile" in t for t in writers), [t.get("name") for t in writers]
    assert all(t.get("no_log") is True for t in writers), [t.get("name") for t in writers]
    # no other task reads the `.env` but the compose commands that name it (fold 11's read 171, S3), or reads the
    # stack's env with no `.env` named; no var or play key holds the file's path or a lookup of it, and no link under
    # the stack's directory, nor any in the tree (`_link_leaks`: the links git's index holds, or, in a copy without
    # its `.git` or one untracked inside another work tree, each in it: fold 14's read 200, N-1; fold 15's read 211,
    # N-4), names it another way (fold 12's read 181, S4, S5; fold 13's read 190, S-3, S-5); and no file the walk read
    # sets `module_defaults`, which may hand a writer the file as its own (171, S2)
    assert _env_readers(w, every) == []
    assert _env_vars(w) == []
    assert _link_leaks(_ROOT) == []
    assert _module_defaults(w) == []
    assert any("_has_alert_url" in str(t.get("when", "")) and "ansible.builtin.debug" in t for t in tasks)
    # custom stats print at the run's end, no_log or not, where ansible.cfg shows them: no task sets any (151, N5)
    assert [t.get("name") for t in every if args(t, "set_stats") is not None] == []
    with open(os.path.join(_ROOT, "ansible", "ansible.cfg"), encoding="utf-8") as fh:
        assert not re.search(r"(?m)^\s*show_custom_stats\s*[=:]", fh.read())
    grafana = _load("docker", "services", "grafana.yaml")["services"]["grafana"]["environment"]
    default = re.fullmatch(r"\$\{KONTROLL_ALERT_URL:-(http://127\.0\.0\.1:\d+/\S+)\}", grafana["KONTROLL_ALERT_URL"])
    assert default, grafana["KONTROLL_ALERT_URL"]
    with open(os.path.join(_ROOT, "scripts", "gen-secret-env.py"), encoding="utf-8") as fh:
        reserved = fh.read().split("RESERVED_PLATFORM_CORE_ENV = frozenset({", 1)[1].split("})")[0]
    assert '"KONTROLL_ALERT_URL"' in reserved
    holders = sorted(os.path.basename(f) for f in glob.glob(os.path.join(_ROOT, "docker", "services", "*.y*ml"))
                     if "KONTROLL_ALERT_URL" in open(f, encoding="utf-8").read())
    assert holders == ["grafana.yaml"], holders


_DECRYPTS = re.compile(r"dashboards\.sops|kontroll_sops_domain|load_vars")


def _spans(text):
    """The Jinja code of `text`: each `{{ }}` and `{% %}` block's tokens, as Jinja's lexer reads them, so a quoted
    `}}` or a nested mapping's close stays inside its block (a pattern that ended at the first `}}` read past
    `{{ '}}' ~ x }}` and `{{ {'a': {'b': 1}} | combine({'v': vars}) }}`: fold 8's read 140, S3); a comment, a raw
    block and the text between blocks are none. Text Jinja's lexer refuses is read whole."""
    try:
        tokens = list(jinja2.Environment().lex(text))
    except jinja2.TemplateSyntaxError:
        return [text]
    out, block = [], None
    for _, kind, value in tokens:
        if kind in ("variable_begin", "block_begin"):
            block = []
        elif kind in ("variable_end", "block_end"):
            out.append("".join(block))
            block = None
        elif block is not None:
            block.append(value)
    return out


# What holds every var at once, and so each tainted name: `vars`, `hostvars` or one host's `hostvars[…]` (fold 5's
# read 111, N1), `ansible_facts` whole, by a key that is not a literal, or called on (`ansible_facts.items()`: a
# cacheable set_fact's fact is in it: fold 6's read 121, N2; fold 8's read 140, S3), and a `vars` lookup, which reads
# any var by its name, a space before its parenthesis too (Jinja's parser skips it); not an attribute of another name
# (`x.vars`).
_EVERY_VAR = (r"(?<![.\w])(?:vars|hostvars|ansible_facts(?!\s*(?:\.\s*\w+\b(?!\s*\()|\[\s*['\"])))\b"
              r"|\b(?:lookup|query|q)\s*\(\s*['\"](?:[\w.]*\.)?vars['\"]")
_EVERY_VAR_IN = re.compile(_EVERY_VAR)
# The messages Ansible prints for a failing assert or fail, on 2.19 under no_log too (fold 5's read 111, S1), and a
# pause's prompt, which both cores print on a tty under no_log (fold 12's read 181, S6; Semaphore's closed stdin
# prints none); a `success_msg` is none, a passing no_log task printing nothing (fold 6's read 120, N1).
_MESSAGES = {"assert": ("fail_msg", "msg"), "fail": ("msg",), "pause": ("prompt",)}
# A lookup call, whose failure prints its terms, on 2.19 under no_log too (fold 6's read 121, S1), a space before
# its parenthesis too (fold 7's reads 130, S4 and 131, S2).
_LOOKUP_CALL = re.compile(r"\b(?:lookup|query|q)\s*\(")

# The modules that write a file's content, which the mandated --check --diff prints, under any prefix
# (`community.general.ini_file` too), and the names a module takes for the file (lineinfile's, replace's and
# blockinfile's `path` is also `dest`, `destfile` and `name`: fold 9's reads 151, N2 and 150, N-C).
_WRITERS = ("copy", "template", "blockinfile", "lineinfile", "replace", "ini_file", "assemble")
_FILE_KEYS = ("dest", "path", "destfile", "name")
_STACK_ENV = re.compile(r"\{\{\s*stack_dir\s*\}\}/\.env")


def _named(t):
    """The module names task `t` gives: its keys, and its `action:` or `local_action:` (a string's first word, or a
    mapping's `module:`'s, as Ansible splits it: `module_words`; fold 13's read 191, S-b)."""
    out = {str(k) for k in t}
    for key in ("action", "local_action"):
        a = t.get(key)
        if isinstance(a, str) and a.split():
            out.add(a.split()[0])
        elif isinstance(a, dict) and module_words(a.get("module"))[0] is not None:
            out.add(module_words(a["module"])[0])
    return out


def _unplaced(f):
    """Whether path `f`'s last part, past the last `/` written as it is (of a path with none, the whole), may name the
    directory or the stack's `.env`: the path holds a raw block anywhere, whose text the lexer reads as written, so a
    `/` in it may cut the last part (`{{ stack_dir }}{% raw %}/.e{% endraw %}nv`, the stack's `.env`, was placed as a
    name written as it is: fold 14's read 200, S-C); the last part is the empty name, `.` or `..`; it holds a `{% %}`
    statement or a `{# #}` comment, which may make its written text another (a filter's, a trimmed line break's: fold
    13's read 191, S-d, W1-W3); or it holds a `{{ }}` block (as Jinja's lexer reads it, so a quoted close stays inside)
    and the text written after its last block is a suffix of `.env` or of `..`, the empty one too, as a block may write
    any text, a `/` too, so only that text is the name's own (`{{ stack_dir }}/{{ item }}`, `{{ stack_dir }}/.e{{ 'nv'
    }}`, `{{ _f }}`, and `{{ stack_dir }}/vector{{ '/../.e' }}nv`, which a pattern of the whole part placed: 191, S-d,
    W4). A path the lexer refuses is unplaced too. A name written as it is (`.env`, `x.key`) is placed, as is one whose
    last block a suffix no `.env` ends in follows (`{{ item }}.conf`, `tail_{{ h }}.key`; fold 12's reads 181, N2 and
    180, N-D: such per-item writers were refused, though sound)."""
    try:
        tokens = list(jinja2.Environment().lex(f))
    except jinja2.TemplateSyntaxError:
        return True
    if any(kind == "raw_begin" for _, kind, _ in tokens):
        return True
    parts = []                                      # (None, text written) or (the block's opening token, "")
    for _, kind, value in tokens:
        if kind == "data":
            parts.append((None, value))
        elif kind.endswith("_begin"):
            parts.append((kind, ""))
    last = max([i for i, (block, text) in enumerate(parts) if block is None and "/" in text], default=None)
    if last is not None:
        parts = [(None, parts[last][1].rsplit("/", 1)[1])] + parts[last + 1:]
    blocks = [i for i, (block, _) in enumerate(parts) if block is not None]
    if not blocks:
        return "".join(text for _, text in parts) in ("", ".", "..")
    if any(parts[i][0] != "variable_begin" for i in blocks):
        return True
    tail = "".join(text for _, text in parts[blocks[-1] + 1:])
    return ".env".endswith(tail) or "..".endswith(tail)


def _env_writers(every):
    """Each task of `every` that writes the stack's `.env`: a `_WRITERS` module, under any prefix, named by its key
    or by `action:` or `local_action:` (`_named`), whose file, by any of its names, is written `{{ stack_dir
    }}/.env`, the tree's one spelling. A writer the pin cannot place fails it, as it may write the stack's file and
    print it: one whose file names a `.env` another way (`{{ stack_dir ~ '/.env' }}`, `{{ stack_dir }}/./.env`: a
    lineinfile under `name:`, an ini_file and a joined dest each passed it, fold 9's read 150, N-C, and a prefixed
    ini_file under `action:`, fold 10's read 161, N3); one whose file or `src` may name a `.env` or the directory
    past its last written `/` (`_unplaced`: a copy into `{{ stack_dir }}/`, which takes its src's name, a dest of `{{
    stack_dir }}/.e{{ 'nv' }}` or of `{{ stack_dir }}/{{ item }}` over the two env files, which `--check --diff`
    printed, the stack's old URL in it, on each core, and a `{% %}` or `{# #}` block in the name, fold 12's reads
    181, N2 and 180, N-D); one whose `src` names a `.env`; one with no file at all, which a play's
    `module_defaults` may hand it; and one whose `args:` is not a mapping, which may (fold 11's reads 171, S2 and 170,
    S-5)."""
    out = []
    for t in every:
        mods = set(_WRITERS) | {n for n in _named(t) if n.rsplit(".", 1)[-1] in _WRITERS}
        found = [a for m in sorted(mods) for a in [args(t, m)] if a is not None]
        if not found:
            continue
        assert t.get("args") is None or isinstance(t["args"], dict), ("a writer whose args: is no mapping",
                                                                      t.get("name"))
        files = [str(a[k]) for a in found if isinstance(a, dict) for k in _FILE_KEYS if k in a]
        srcs = [str(a["src"]) for a in found if isinstance(a, dict) and a.get("src") is not None]
        assert files, ("a writer whose file the pin cannot read", t.get("name"))
        odd = [f for f in files if re.search(r"\.env(?![\w.])", f) and not _STACK_ENV.fullmatch(f)]
        odd += [f for f in files + srcs if _unplaced(f)]
        odd += [s for s in srcs if re.search(r"\.env(?![\w.])", s)]
        assert not odd, ("a writer of a .env the pin cannot place", t.get("name"), odd)
        if any(_STACK_ENV.fullmatch(f) for f in files):
            out.append(t)
    return out


# A compose command that names the stack's `.env` as its `--env-file`, as the deploy runs it: `up`, `restart` and
# `ps` print no value of it (`config` prints the file as compose reads it).
_COMPOSE_ENV = re.compile(r"docker compose(?: --progress plain)? --project-directory \{\{ stack_dir \}\}"
                          r"(?: --file \{\{ stack_dir \}\}/compose\.yaml)? --env-file \{\{ stack_dir \}\}/\.env"
                          r" (?:up|restart|ps)\b")
# What reads the stack's env with no `.env` named (fold 12's reads 181, S4 and 180, N-B): compose's `config` and
# `convert`, which print the file as compose reads it (the project directory's `.env` by default), its `exec` and
# `run`, which run a command in a service whose environment holds it; `docker inspect`, `docker exec` and `docker top`
# (whose `e` prints each process's environment) of a container, an image's inspect too (a container committed to one
# keeps its environment: fold 13's read 191, S-e), and so a written `commit`, docker's or compose's (fold 15's read 210,
# N-3; fold 16's read 221, N-8); a `cp`, compose's or docker's, which copies a file out of a container, its
# `/proc/1/environ` among them (fold 13's own find); a `logs` of Grafana's, which prints its failed-delivery line, the
# URL in it (fold 13's read 190, N-4), an `attach`, which prints it as it comes (fold 15's own, after read 201's lead),
# a docker `start` of Grafana's that attaches (`-a`, `-i`, `--attach`, `--interactive`) and a compose `up` neither
# detached nor waiting, which print it so too (fold 15's read 211, N-1: `_prints_grafanas_log`); and the modules that do
# the same, `docker_container` too, whose result holds the container's inspect. The options each takes, with a value or
# alone, so the subcommand is the first word past them: one neither names is read as a read, as it may take the
# subcommand for its value (fold 13's read 190, S-4: compose's hidden `--workdir`, v1's `--context`).
_COMPOSE_READS = frozenset({"config", "convert", "exec", "run", "cp"})
_DOCKER_READS = frozenset({"inspect", "exec", "cp", "top"})
_COMPOSE_OPTS = frozenset({"--project-directory", "--workdir", "--file", "-f", "--env-file", "--progress",
                           "--project-name", "-p", "--profile", "--ansi", "--parallel", "--context", "-c", "--host",
                           "-H", "--log-level", "--tlscacert", "--tlscert", "--tlskey"})
_COMPOSE_FLAGS = frozenset({"--compatibility", "--dry-run", "--all-resources", "--verbose", "--no-ansi",
                            "--skip-hostname-check", "--tls", "--tlsverify", "--version", "-v"})
_DOCKER_OPTS = frozenset({"--config", "--context", "-c", "--host", "-H", "--log-level", "-l", "--tlscacert",
                          "--tlscert", "--tlskey"})
_DOCKER_FLAGS = frozenset({"--debug", "-D", "--tls", "--tlsverify", "--version", "-v"})
_LOGS_OPTS = frozenset({"--tail", "-n", "--since", "--until", "--index"})
_READ_MODULES = frozenset({"docker_container_info", "docker_container_exec", "docker_compose_v2_exec",
                           "docker_compose_v2_run", "docker_container"})
# The words that may stand before a command in its place on a line: a command that runs the words after it as a
# command (`eval` too: fold 16's read 220, N-2), or a shell keyword a command follows (fold 15's reads 210, S-3 and
# 211, S-2).
_PREFIX_CMDS = frozenset({"sudo", "doas", "runuser", "env", "timeout", "nice", "nohup", "ionice", "stdbuf", "setsid",
                          "exec", "command", "time", "eval", "if", "then", "else", "elif", "while", "until", "do", "!",
                          "{"})
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
# A number or a duration, as timeout takes one ("a floating point number with an optional suffix": `30`, `30s`, `2.5`,
# `1m`), where the pin read digits alone (fold 16's read 220, S-2).
_DURATION = re.compile(r"[0-9.]+[smhd]?")
# A redirection, its operator and its target (`2>/dev/null`, `2> /dev/null`, `2>&1`, `>|x`, bash's `&>x`; `>>` and
# `<<` are read as two, each dropped; `<<-` is read whole, as its `-` would otherwise be read as the second `<`'s
# target and a blank after it would keep the heredoc's word: fold 17's read 231, S-1), which a reading of its own
# drops (`_unredirected`): before a block in a command's place, or between docker and its subcommand, it moved the
# read out of the words the pin read, and the `&` of `2>&1` cut the line there (fold 16's read 220, S-2; what fold 13
# named a residual, "a redirect comes between").
_REDIRECT = re.compile(r"[0-9]*(?:&>|[<>]&|>\||<<-|[<>])[ \t]*[^\s;&|()<>]*")
# What opens a substitution sh holds in one word (`_sh_masked`).
_SH_OPENS = ("$(", "${", "<(", ">(")


def _subcommand(words, opts, flags):
    """The first of `words` past the options (each of `opts` with its value, apart, after a `=` or, for a short one,
    attached; each of `flags` alone or with its `=value`), and the words after it; None when there is none, and `-`
    when an option neither set names comes first: it may take the subcommand for its value, so the caller reads it as
    a read (fold 13's read 190, S-4). An attached value and a flag's `=value` are read so as the docker and compose
    CLIs read them (`-pkontroll`, `-f{{ x }}`, `-Hunix://…`, `--dry-run=false`: each passed a block's compose read, or
    a block's docker one, which the CLI ran: fold 15's reads 210, S-2 and 211, S-2), and so is a cluster of short
    options: its flags, then at most one option, whose value is the rest of the word or the next word (`-Dc default`,
    `-DH unix://…`: past a block, each passed a docker read the CLI ran, fold 16's read 220, N-3)."""
    i = 0
    while i < len(words) and words[i].startswith("-"):
        name, eq, _ = words[i].partition("=")
        if name in opts:
            i += 1 if eq else 2
        elif name in flags:
            i += 1
        elif name.startswith("--") or len(name) < 2:
            return "-", []
        else:
            for k in range(1, len(name)):
                if "-" + name[k] in opts:
                    i += 1 if k + 1 < len(name) or eq else 2
                    break
                if "-" + name[k] not in flags:
                    return "-", []
            else:
                i += 1
    return (words[i], words[i + 1:]) if i < len(words) else (None, [])


def _in_command_place(words, i):
    """Whether `words[i]` stands where sh runs a command: first on its line, or past a `_PREFIX_CMDS` word or an
    assignment that is first, with only such words, options, the word after an option (it may be the option's value),
    numbers or durations (`_DURATION`) and Jinja blocks between (`sudo -u kontroll {{ _compose }} config`, `X=1 {{
    _compose }} config`, `if {{ _compose }} config`, `timeout 30 …`: each passed the pin, which read a block as `docker
    compose` first on its line alone, and compose printed the stack's `.env`: fold 15's reads 210, S-3 and 211, S-2;
    `timeout 30s …`, `timeout 2.5 …`: fold 16's read 220, S-2); a prefix word is read by its last path part, as the
    docker word is (`/usr/bin/sudo`: fold 16's read 221, N-1). Nor is a block past a first word of another kind, a
    block's among them: `{{ _git }} -C {{ bare }} config` is no read, as the deploy's `git -C {{ bare }} config` is
    not; past a prefix, the word after an option is read as its value, so `sudo -n git -C {{ bare }} config` and
    `sudo {{ _git }} -C {{ bare }} config` are read as a block in a command's place, sound refusals R-ALERT-1 names
    (fold 16's reads 221, N-3 and 220, N-4)."""
    if i == 0:
        return True
    if not (words[0].rsplit("/", 1)[-1] in _PREFIX_CMDS or _ASSIGNMENT.match(words[0])):
        return False
    return all(v.rsplit("/", 1)[-1] in _PREFIX_CMDS or v.startswith("-") or _ASSIGNMENT.match(v)
               or _DURATION.fullmatch(v) or "\0" in v
               or words[j - 1].startswith("-") for j, v in enumerate(words[1:i], 1))


def _prints_grafanas_log(sub, rest, compose):
    """Whether subcommand `sub`, handed `rest`, prints Grafana's log, its failed-delivery line with the URL among it:
    a `logs` or an `attach` of Grafana's (`_logs_of_grafana`); docker's `start` of Grafana's that attaches, by `-a`,
    `--attach`, `-i` or `--interactive`, alone, with a value or in a cluster (the CLI attaches for either; `-ai`,
    `-a=true`: fold 16's read 220, S-3); a compose `up` neither detached (`-d`, `--detach`) nor waiting (`--wait`),
    which prints every service's log as it comes (fold 15's read 211, N-1: each printed it as the `attach` fold 15
    read does)."""
    if sub in ("logs", "attach"):
        return _logs_of_grafana(rest)
    if sub == "start" and not compose:
        return (any(re.fullmatch(r"--(?:attach|interactive)(?:=.*)?|-[ai]+(?:=.*)?", w) for w in rest)
                and _logs_of_grafana(rest))
    if sub == "up" and compose:
        return not any(w in ("-d", "--detach", "--detach=true", "--wait", "--wait=true") for w in rest)
    return False


def _sh_masked(s):
    """`s` with each run sh holds in one word made one word: its whitespace, separators, parentheses and backquotes
    each `_`, so the cut at separators and the split at whitespace read sh's own words. A run is a quoted one, a
    `$(…)`, `$((…))`, `${…}`, `<(…)` or backquoted substitution, or an escaped character, read by a scan that nests as
    sh does: a quote inside a substitution, a substitution inside double quotes, a `)` inside a quote, and a `case`
    inside a substitution, held to its `esac` (a variable named `case` inside an arithmetic expansion is read so too,
    as `$((` is read as a `$(` holding a `(`: fold 17's read 231, N-1). A quoted space, line break or separator in an
    option's value, or a substitution there, moved the subcommand out of the words the pin read (`docker --config
    '/tmp/a b' inspect`, `--config 'a;b'`, `-p "$(basename {{ stack_dir }})" config`), while docker ran the read and
    printed the URL or the `.env` (fold 15's reads 210, S-1 and 211, S-1); and fold 16's pattern, which paired a `$(`
    with the first `)` after it, held no `$((`, `${` or `<(`, and ended a double-quoted run at a quote nested inside
    it, did so again (`--config $(echo ')')`, `--parallel $((1+1))`, `--config ${X:-a;b}`, `--config "$(dirname "/tmp/a
    b/c")"`: fold 16's read 220, S-1)."""
    out, stack, i = [], [], 0
    while i < len(s):
        c, top, step, closes = s[i], (stack[-1] if stack else None), 1, False
        at_word = not (i and (s[i - 1].isalnum() or s[i - 1] == "_"))
        if top == "'":
            closes = c == "'"
        elif c == "\\" and i + 1 < len(s):
            step = 2
        elif top == '"' and c == '"' or top == "`" and c == "`" or top == "${" and c == "}":
            closes = True
        elif top in ("$(", "<(", "(") and c == ")":
            closes = True
        elif top == "case" and at_word and re.match(r"esac\b", s[i:i + 5]):
            closes, step = True, 4
        elif top in ("$(", "<(", "(", "case") and at_word and re.match(r"case\b", s[i:i + 5]):
            stack.append("case")
            step = 4
        elif s.startswith(_SH_OPENS if top != '"' else _SH_OPENS[:2], i):
            opened = next(o for o in _SH_OPENS if s.startswith(o, i))
            stack.append("<(" if opened == ">(" else opened)
            step = len(opened)
        elif c == "`" or c in "'\"" and top != '"':
            stack.append(c)
        elif top not in (None, '"') and c == "(":
            stack.append("(")
        chunk = s[i:i + step]
        out.append(re.sub(r"[\s;&|`()]", "_", chunk) if stack or step == 2 and c == "\\" else chunk)
        if closes:
            stack.pop()
        i += step
    return "".join(out)


def _jinja_strings(text):
    """Each Jinja string literal's text in `text`, as written (Ansible keeps a literal's backslashes): a `pipe`
    lookup's command, read as sh reads it (`_sh_masked`), where its quotes are the literal's own (fold 15's read 210,
    S-1)."""
    try:
        return [v[1:-1] for _, kind, v in jinja2.Environment().lex(text) if kind == "string"]
    except jinja2.TemplateSyntaxError:
        return []


def _joined(text):
    """Each Jinja block's code in `text` with its string literals joined across `~` or `+` as Jinja joins them, any
    name one block-written word and any other token a line's end: a `pipe` lookup's command built in pieces (`'docker
    compose --project-directory ' ~ stack_dir ~ ' config'`, `'docker' ~ ' inspect …'`, `'docker' + ' inspect …'`)
    passed the pin, and printed the `.env` or the URL (fold 15's read 210, N-2; fold 16's read 221, N-2)."""
    try:
        tokens = list(jinja2.Environment().lex(text))
    except jinja2.TemplateSyntaxError:
        return []
    out, cur, inside = [], "", False
    for _, kind, value in tokens:
        if kind in ("variable_begin", "block_begin"):
            inside, cur = True, ""
        elif kind in ("variable_end", "block_end"):
            out.append(cur)
            inside = False
        elif not inside or kind == "whitespace" or kind == "operator" and value in ("~", "+", "."):
            continue
        elif kind == "string":
            cur += value[1:-1]
        elif kind in ("name", "integer", "float"):
            cur += "\0"
        else:
            cur += "\n"
    return out


def _logs_of_grafana(words):
    """Whether a `logs` handed `words` (its options, then its services or its container) may print Grafana's: it
    names none, so every service's, or one that names Grafana or that a Jinja block writes."""
    names, i = [], 0
    while i < len(words):
        if words[i].startswith("-"):
            i += 2 if words[i] in _LOGS_OPTS else 1
        else:
            names, i = names + [words[i]], i + 1
    return not names or any("grafana" in n or "\0" in n for n in names)


def _unworded(t):
    """Task `t` as the readers' checks read it: its name and an assert's, a fail's or a debug's message reduced to
    their Jinja (`_spans`: the words outside it run and print nothing that reads the file), and the tasks of its
    `block:`, `rescue:` and `always:` left out, each read as a task of its own (fold 13's reads 190, N-2 and 191,
    N-c: a block whose task's name, and a message whose words, spelled a `docker inspect` each failed the pin)."""
    out = {}
    for k, v in t.items():
        bare = re.sub(r"^ansible\.(?:builtin|legacy)\.", "", str(k))
        if k in ("block", "rescue", "always"):
            continue
        if k == "name":
            v = " ".join(_spans(str(v)))
        elif bare in ("assert", "fail", "debug") and isinstance(v, dict):
            v = {a: " ".join(_spans(str(b))) if a in ("msg", "fail_msg", "success_msg") else b for a, b in v.items()}
        out[k] = v
    return out


def _held(flat, text, literals):
    """The texts the masked reading reads (`_sh_masked`): `flat` (the text with each Jinja block one word), each Jinja
    string's of `text` (`_jinja_strings`) and each of `literals`; each again with its `#` comments dropped, as sh
    drops them (a comment's apostrophe paired with an option value's quote and moved the subcommand out, `# don't`
    then `docker --config '/tmp/a b' inspect …`: fold 16's read 221, S-2); and each quoted run's text, and each run's
    inside that, read as a command of its own, as a shell's `-c`, `su -c` and `eval` run it (`sh -c "docker --config
    '/tmp/a b' inspect …"`, `sh -c '{{ _compose }} config'`: fold 16's reads 221, S-2 and 220, N-2; `sh -c 'su -c
    "…"'`: fold 17's own; a run deeper still holds an escaped quote, which the pin does not undo). Each only adds a
    reading, so it can only add a refusal."""
    held = [flat] + _jinja_strings(text) + literals
    held += [re.sub(r"(?m)(^|[\s;&|()])#.*$", r"\1", x) for x in held]
    for _ in range(2):
        held += [r for x in held for r in [m.group(0)[1:-1] for m in re.finditer(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", x)]
                 if r not in held]
    return held


def _unredirected(texts):
    """Each of `texts`, and again with each redirection dropped (`_REDIRECT`)."""
    return [v for x in texts for v in (x, _REDIRECT.sub(" ", x))]


def _reads_the_stack(t):
    """Whether task `t` reads the stack's env without naming its `.env`: it runs a `_READ_MODULES` module (`_named`,
    under any prefix), its words outside a name's or a message's Jinja (`_unworded`) spell `unix_socket` or
    `unix-socket` (`uri`'s, `curl --unix-socket`: the Engine API over docker's socket printed Grafana's `Config.Env`,
    fold 15's read 210, N-4; another client on the socket is a residual R-ALERT-1 names), or a string of it
    (`_unworded`; a command's `argv` joined, and again with each element one word; the command module's string and
    expect's `command` with their line breaks read as spaces, as each splits it; each Jinja block's code with its
    literals joined across `~` or `+`, `_joined`), with a shell's line continuation removed (a backslash that ends a
    line between `docker` and `inspect`, or inside either: fold 14's read 200, S-A; fold 15's read 210, N-1) and again
    read as a space (a backslash sh does not read as one, an escaped one or a comment's, glued the next line to this
    one when removed: fold 16's read 221, S-1), as written with each Jinja block one word or as each block's own code
    (a `pipe` lookup's command), and again as sh holds its words (`_sh_masked`), over the texts `_held` names (fold
    15's reads 210 and 211, S-1; fold 16's reads 220, S-1 and 221, S-2), each reading again with each redirection
    dropped (`_unredirected`: fold 16's read 220, S-2), each literal of each of the task's strings as written read
    alone too, a message's among them (`_unworded` keeps a message's blocks unbraced, where no literal lexes, and
    `_joined` puts a block's literals in one text, where a `'` in one pairs with a `'` in another and hides a `pipe`
    lookup's command between them: `{{ "'" ~ lookup('pipe', 'docker --config "a;b" inspect …') ~ "'" }}`, fold 16's own
    run), its words read at whitespace and, again, at quotes and commas too (`lookup('pipe','docker inspect …')` writes
    no space: 200, S-B; read at commas alone, `docker --host=a,b inspect` would pass), holds a docker command, to the
    end of its line, a `;`, `&`, `|`, a parenthesis or a backquote (a `$(…)` too), whose subcommand is a `_DOCKER_READS`
    one or a written `commit` (an image committed from Grafana's container keeps its environment: 210, N-3), or a
    compose one (`docker compose`, `docker-compose`) whose subcommand is a `_COMPOSE_READS` one or a written `commit`
    (221, N-8), or one that prints Grafana's log (`_prints_grafanas_log`). A word is a docker command by its last path
    part, its quotes stripped (`/usr/bin/docker`, `"docker`), or as a Jinja block handed a subcommand written as it is
    (`{{ _docker }} inspect`: fold 13's reads 190, S-4 and 191, S-e); a block in a command's place
    (`_in_command_place`), which may write `docker compose` whole, reads it handed a compose read or one that prints
    Grafana's log, past compose's options (`{{ _compose }} config`, `{{ _compose }} -f x config`, `sudo {{ _compose }}
    config`: 200, S-D; 210, S-3; 211, S-2), though not past a first word of another kind, as the deploy's `git -C {{
    bare }} config` is no read; a subcommand a block writes (`docker compose {{ verb }}`), which may be either, reads it
    too, as does a written docker's past an option `_subcommand` does not know (a block's, past one, does not: the
    deploy's `{{ ansible_playbook_python }} -B …` is no read)."""
    if any(n.rsplit(".", 1)[-1] in _READ_MODULES for n in _named(t)):
        return True
    if re.search(r"unix[_-]socket", str(_unworded(t))):
        return True
    texts = list(strings(_unworded(t)))
    argvs = [a["argv"] for m in ("command", "shell") for a in [args(t, m)]
             if isinstance(a, dict) and isinstance(a.get("argv"), list)]
    texts += [" ".join(map(str, v)) for v in argvs]
    texts += [" ".join(re.sub(r"[\s;&|`()]", "_", str(x)) or "_" for x in v) for v in argvs]
    texts += [" ".join(a[k].split()) for a in [args(t, "command")] if isinstance(a, dict)
              for k in ("cmd", "_raw_params") if isinstance(a.get(k), str)]
    texts += [" ".join(a["command"].split()) for a in [args(t, "expect")]
              if isinstance(a, dict) and isinstance(a.get("command"), str)]
    texts += ["\n".join(j for s in strings(t) for j in _joined(str(s)))]
    literals = [x for s in strings(t) for c in ("", " ") for x in _jinja_strings(re.sub(r"\\\n", c, str(s)))]
    for text in [re.sub(r"\\\n", c, x) for x in texts for c in ("", " ")]:
        flat = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", "\0", text, flags=re.S)
        lines = re.split(r"[;&|\n`()]", "\n".join(_unredirected([flat] + _spans(text))))
        lines += re.split(r"[;&|\n`()]", "\n".join(_unredirected(_sh_masked(x) for x in _held(flat, text, literals))))
        for words in [w for line in lines for w in ([word.strip("'\"") for word in line.split()],
                                                    [word for word in re.split(r"[\s'\",]+", line) if word])]:
            for i, word in enumerate(words):
                name = word.rsplit("/", 1)[-1]
                if name == "docker-compose":
                    sub, rest = "compose", words[i + 1:]
                elif name == "docker" or "\0" in name:
                    if "\0" in name and _in_command_place(words, i):     # it may write `docker compose` whole
                        sub, rest = _subcommand(words[i + 1:], _COMPOSE_OPTS, _COMPOSE_FLAGS)
                        if sub in _COMPOSE_READS or _prints_grafanas_log(sub, rest, True):
                            return True
                    sub, rest = _subcommand(words[i + 1:], _DOCKER_OPTS, _DOCKER_FLAGS)
                    if "\0" in name and (sub is None or sub == "-" or "\0" in sub):
                        continue        # a block's word is a docker command only before a subcommand as written
                else:
                    continue
                if sub in ("container", "image"):
                    sub, rest = (rest[0], rest[1:]) if rest else (None, [])
                reads, compose = _DOCKER_READS, False
                if sub == "compose":
                    sub, rest = _subcommand(rest, _COMPOSE_OPTS, _COMPOSE_FLAGS)
                    reads, compose = _COMPOSE_READS, True
                if sub is not None and (sub == "-" or "\0" in sub or sub in reads
                                        or sub == "commit" and "\0" not in name
                                        or _prints_grafanas_log(sub, rest, compose)):
                    return True
    return False


def _env_vars(w):
    """Each place walk `w` reads that a task may read past `_env_readers`, which reads tasks, whose text, with each
    template a lookup in it renders (`Walk.expand`), names a `.env` (a lookup of the file, or its path): each key of
    a play but its task lists (its vars, each `roles:` entry's params and vars, its environment; its name's Jinja, as
    the PLAY banner prints the name) and of an import entry, each var of a vars file and each param of a dependency
    (fold 12's read 181, S4: a play var and a role default that look the file up each passed the pin; fold 13's reads
    190, S-3 and 191, S-c: a role entry's param and vars, a play's environment and its name each printed the file on
    both cores)."""
    def names(text):
        return re.search(r"\.env(?![\w.])", text)

    out = [(p.get("name"), k) for p in w.plays + w.imports for k, v in p.items()
           if k not in ("pre_tasks", "tasks", "post_tasks", "handlers")
           and names(" ".join(_spans(str(v))) + w.rendered(v) if k == "name" else w.expand(v))]
    out += [(meta, k) for meta, dep in w.deps for k, v in dep.items()
            if k not in ("role", "name") and names(w.expand(v))]
    for f in w.var_files:
        if not re.search(r"\.sops\.(ya?ml|json)$", f):
            out += [(f, k) for k, v in (load(f) or {}).items() if names(w.expand(v))]
    return out


def _env_readers(w, every):
    """Each task of `every` but an `_env_writers` one that reads the stack's env with no `.env` named
    (`_reads_the_stack`: compose's `config`, `exec` or `run`, `docker inspect`, `exec` or `top`, a `cp`, a written
    `commit`, docker's or compose's, a `logs` or an `attach` of Grafana's, its `docker start` attached, a compose `up`
    neither detached nor waiting, a task that spells `unix_socket` or `unix-socket`, `docker_container_info`), or that
    names a `.env` (not `.env.homepage`, the portal's own) in its text or in a template it renders, but as compose's
    `--env-file` before `up`, `restart` or `ps` (`_COMPOSE_ENV`, once), or in the words of a task's name or an assert's,
    a fail's or a debug's message outside their Jinja (`_unworded`): such a task reads the file another way, a debug of
    a file lookup of it printing the URL, a slurp registering it where the pin follows no taint, no_log or not (fold
    11's read 171, S3). Some it refuses are sound (compose's `pull` with the `--env-file`, `config --quiet`, a `stat` of
    the file, a `file:` setting its mode, a docker command past an option the reader does not know, a `logs` of every
    service; a block-written word before a docker read's word, `sudo -u {{ u }} cp a b`, `install -d -o {{ uid }} logs`
    or compose's `top` past a block-written value; a block in a command's place handed a compose read or an attached
    `up`, `{{ npm }} run build`, `{{ vagrant }} up`; a path whose last part is `docker` before an option, `find
    /srv/docker -name x`; a `docker cp` into a container; a debug's k=v message or a pause's prompt that spells a docker
    read: fold 14's read 200, N-2; a list, YAML or Jinja, whose items are `docker` and a read's word, `['docker',
    'exec']`; a written `commit`, docker's or compose's, or an attached compose `up` of another service, and a task
    naming another daemon's unix socket: fold 15's reads 210 and 211; a `start --attach=false`, an `up` detached by
    another spelling (`--detach=1`, `-dV`) or `up --no-start`, a block past a prefix's option and the word after it or
    past another block's option (`sudo -n git -C {{ bare }} config`, `sudo {{ _git }} -C {{ bare }} config`), a
    message's Jinja literal that spells a read, and a line sh continues after an escaped backslash: fold 16's reads 220
    and 221; a quoted argument a block opens, handed a read's word, `echo "{{ svc }} up"`, `git commit -m "{{ version }}
    run"`; and a shell comment, an echo's text or an assignment's value that spells a read: fold 17's read 231, N-2,
    N-3): each is written another way or the pin learns it. A `.env` path built in pieces or matched by a glob, a
    command or a script handed the stack's directory, a command a Jinja block writes whole, a block in a command's place
    past an option neither set names or past another block (`{{ _compose }} --x config`, `{{ _sudo }} {{ _compose }}
    config`: a block handed an option neither set names is read as no read, as the deploy's `{{ ansible_playbook_python
    }} -B …` is none), a prefix word the pin does not list (`chronic`, `flock /x`, `chroot /`, a shell's `-c` handed a
    command unquoted), a docker word or subcommand that a shell expansion, quoting inside a word, an escape or `xargs`
    writes, or that a block joins (`D=docker; $D inspect`, `do'ck'er inspect`, `docker{{ ' ' }}inspect`), a lookup's
    command built in pieces another way (an inline `if`, a filter or a format, `'docker %s' | format('inspect')`: 200,
    N-3; 210, N-2; 221, N-2), a read after a heredoc whose body holds a quote, past bash's `$'…'` holding an escaped
    quote, or after a variable named `case` inside an arithmetic expansion (220, S-1; 221, S-2; 231, N-1), a log of
    Grafana's container named by its id, a read of its environment another way (its `/proc/<pid>/environ` on the host, a
    task over a docker connection, a `docker run` handed its volumes or its pid namespace, the Engine API over a TCP
    port, or over docker's socket by another client, `nc -U`, `socat` or a python socket: 220, N-1), and an image
    committed from it by a block-written docker or compose (`{{ _docker }} commit`, `{{ _compose }} commit`: a block
    before `commit` is as often git's value, `git -C {{ dir }} commit`), read by `docker_image_info`, `docker save` or a
    `docker run` of it (200, N-4; 210, N-3: the module reads the deploy's own images), it does not see (SECURITY.md
    R-ALERT-1)."""
    writers = {id(t) for t in _env_writers(every)}
    out = []
    for t in every:
        if id(t) in writers:
            continue
        if _reads_the_stack(t):
            out.append(t.get("name"))
            continue
        if not re.search(r"\.env(?![\w.])", w.text(t)):
            continue
        rest = {}
        for k, v in _unworded(t).items():
            bare = re.sub(r"^ansible\.(?:builtin|legacy)\.", "", str(k))
            if bare in ("command", "shell") and isinstance(v, dict) and isinstance(v.get("cmd"), str):
                cmd = " ".join(v["cmd"].split())
                if _COMPOSE_ENV.match(cmd):
                    v = dict(v, cmd=cmd.replace(" --env-file {{ stack_dir }}/.env ", " ", 1))
            rest[k] = v
        text = str(rest) + "".join(w.template_text(p) for p in w.templates.get(id(t), []))
        if re.search(r"\.env(?![\w.])", text):
            out.append(t.get("name"))
    return out


def _links_under(path):
    """Each symbolic link under directory `path`, at any depth: a link in the stack's directory may name its `.env`
    by another name, which `_env_readers` reads for none (fold 12's read 181, S5)."""
    return sorted(os.path.join(d, n) for d, dirs, files in os.walk(path) for n in dirs + files
                  if os.path.islink(os.path.join(d, n)))


def _tracked_links(root):
    """Each symbolic link git's index holds under `root` (mode 120000), as validate's tree-holds-regular-files-only
    step lists them; None where `root` is not the top of a git work tree: a copy without its `.git`, or one that lies
    inside another repository's work tree, whose index lists none of the copy's files (fold 15's read 211, N-4). A link
    anywhere in the tree may name the stack's `.env` by another name, which `_env_readers` reads for none (fold 13's
    read 190, S-5: a link outside docker/ to docker/.env, read by a lineinfile under `--check --diff` and by a file
    lookup, passed a pin that read docker/ alone)."""
    git = ["git", "-c", "safe.directory=*", "-C", root]
    try:
        top = subprocess.run(git + ["rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             check=True).stdout.strip()
        if os.path.realpath(top) != os.path.realpath(root):
            return None
        listed = subprocess.run(git + ["ls-files", "-s"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return sorted(line.split("\t", 1)[1] for line in listed.splitlines() if line.startswith("120000 "))


def _tree_links(root):
    """Each link of the tree at `root`: those git's index holds (`_tracked_links`), or, where `root` is not the top of
    a git work tree (a copy without its `.git`, or one untracked inside another work tree), each under it
    (`_links_under`), which in such a copy refuses a virtualenv's or `node_modules`' links too, loudly. Why: the pin's
    `assert not _tracked_links(_ROOT)` passed on its None, so a tree with no `.git` was checked for no link at all,
    where validate's step at least says it checked nothing (fold 14's read 200, N-1); and a copy inside another work
    tree read that tree's index, which lists none of the copy's links, and passed (fold 15's read 211, N-4)."""
    links = _tracked_links(root)
    return links if links is not None else _links_under(root)


def _link_leaks(root):
    """Each link that may name the stack's `.env` by another name, which the URL's pin holds empty: under the stack's
    directory (`_links_under`) and anywhere in the tree (`_tree_links`). One function, so the test that plants a link
    runs what the pin runs: a pin that went back to `assert not _tracked_links(_ROOT)` passed every row (fold 15's
    read 211, N-4)."""
    return _links_under(os.path.join(root, "docker")) + _tree_links(root)


def _module_defaults(w):
    """Each file walk `w` read that sets `module_defaults`, at any depth (a play's, a block's): it may hand a writer
    that names no file the stack's `.env` as its own, past `_env_writers` (fold 11's read 171, S2)."""
    return [f for f in w.opened if "module_defaults" in set(_keys(load(f)))]


def _keys(value):
    """Every mapping key in a parsed document, at any depth."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def _messages(t):
    """The failure messages the task's assert or fail carries: each message key's value. A free word is none: under
    no_log neither core prints it (2.19 refuses it as a raw param, 2.18 hides the result), and a task whose free words
    read the URL is a reader all the same."""
    out = []
    for name, keys in _MESSAGES.items():
        a = args(t, name)
        if isinstance(a, dict):
            out += [str(a.get(k, "")) for k in keys]
    return out


def _lookup_terms(text):
    """The argument text of each lookup, query or q call in `text`, to its closing parenthesis."""
    out = []
    for m in _LOOKUP_CALL.finditer(text):
        depth, i, quote = 1, m.end(), None
        while i < len(text) and depth:
            c = text[i]
            if quote:
                quote = None if c == quote else quote
            elif c in "'\"":
                quote = c
            else:
                depth += {"(": 1, ")": -1}.get(c, 0)
            i += 1
        out.append(text[m.end():i - 1])
    return out


def _prints_every_var(w, t):
    """Whether the task prints every var when it is not no_log: a debug whose `var`, an expression, reads one
    anywhere in it, or any templated string of the task, or of a template it renders, that does (a debug's `msg`; a
    name; the template, copy and lineinfile actions' diffs under the mandated `--check --diff`; a loop's items; a
    template lookup's text in a debug)."""
    a = args(t, "debug")
    if isinstance(a, dict) and _EVERY_VAR_IN.search(str(a.get("var", ""))):
        return True
    texts = [w.template_text(f) for f in w.templates.get(id(t), [])] + list(strings(t))
    return any(_EVERY_VAR_IN.search(x) for text in texts for x in _spans(text))


def _url_leaks(w):
    """What the URL's no_log pin reads in `w`, a `_deploy_walk.Walk`, each task read with the templates it renders
    (`Walk.text`): `every` task; `tainted`, the names that hold the URL (`_dash`, and each fact, register, include var
    or `apply:` var a task that reads one, or prints every var, sets, an add_host's vars included, but the two bools on
    their one setter; and a rescue's `ansible_failed_result` and `ansible_failed_task`); `readers`, each task that reads
    a tainted name or that a reading include brings in; and the leaks, each empty on a sound tree: `unlogged` (a reader,
    or a task that prints every var — a debug whose `var:` reads `vars`, `hostvars`, `ansible_facts` or a `vars` lookup
    anywhere in it, or a templated string of the task or of a template it renders that does — that is not no_log),
    `named` (a task whose name templates a tainted name), `message` (an assert's or a fail's failure message, or a
    pause's prompt, that templates one, or a lookup, a `with_<lookup>` loop's included, handed one, whose failure prints
    its terms; 2.19 prints both under no_log, and both cores a pause's prompt on a tty), each read with the templates
    the task renders where it calls a lookup, as a name, message or term that looks up a reader prints what it reads
    (fold 12's read 181, S1, S6), `outside` (a play key but its vars and task lists, a task's or an include's `apply:`
    environment, or a task's `delegate_to`, `notify` or `raw` or `script` argument, that reads one), `derived` (a play
    var, a vars-file var or a meta dependency's param that reads one) and `decrypts` (each play var, vars-file var,
    dependency param, task or SOPS vars file that decrypts the dashboards domain). A var is read with each template a
    lookup in it renders (`Walk.expand`), one a task, an include's `apply:` or an add_host hands on too (fold 13's reads
    190, S-2 and 191, S-a: a no_log name, an assert's message and an included task's name each read a handed var that
    looked up a reader, and printed it on both cores), and a module's arguments in each form Ansible takes
    (`_deploy_walk.args`)."""
    every = w.tasks
    setters = {b: [t for t in every if b in (args(t, "set_fact") or {})] for b in ("_alert_url_ok", "_has_alert_url")}
    exempt = {id(ts[0]): {b} for b, ts in setters.items() if len(ts) == 1}

    def applied(t):                 # an include's or import's `apply:`, whose keys are the tasks' it brings in
        return [ref["apply"] for key in ("include_tasks", "import_tasks", "include_role", "import_role")
                for ref in [args(t, key)] if isinstance(ref, dict) and isinstance(ref.get("apply"), dict)]

    def names(t, pat):
        facts = args(t, "set_fact") or {}
        out = set(facts) - {"cacheable", "_raw_params"} if isinstance(facts, dict) else set()
        out |= {t["register"]} if t.get("register") else set()
        host = args(t, "add_host") or {}               # a new host's vars, read by its name in a later play (121, N3)
        out |= {k for k, v in host.items() if k not in ("name", "host", "hostname", "groups", "group", "groupname")
                and pat.search(w.expand(v))} if isinstance(host, dict) else set()
        handed = dict(t.get("vars") or {})
        for a in applied(t):
            handed.update(a.get("vars") or {})
        out |= {k for k, v in handed.items() if pat.search(w.expand(v))}   # handed on, with what it looks up
        return out - exempt.get(id(t), set())

    def reading(pat):               # a task that reads a tainted name, or that a reading import or include brought in
        ids = set()
        for t in every:
            if pat.search(w.text(t)) or w.via.get(id(t)) in ids:
                ids.add(id(t))
        return [t for t in every if id(t) in ids]

    def looked_up(t, value):        # `value`'s text and, where it calls a lookup, each template task `t` renders
        text = str(value or "")     # (a name or message that looks up a reader prints what it reads: 181, S1)
        return [text] + ([w.template_text(p) for p in w.templates.get(id(t), [])] if _LOOKUP_CALL.search(text) else [])

    def templates(pat, t, value):   # whether `value`, or what it looks up, templates a tainted name or every var
        return any(pat.search(x) or _EVERY_VAR_IN.search(x) for text in looked_up(t, value) for x in _spans(text))

    def handed(pat, t):             # whether a lookup's terms, past its plugin's name, hold one or every var, or
        terms = _lookup_terms(w.text(t))     # look up a template that does
        return any(pat.search(x) or _EVERY_VAR_IN.search(re.sub(r"^\s*(['\"])[^'\"]*\1", "", x)) for x in terms) or \
            any(templates(pat, t, x) for x in terms if _LOOKUP_CALL.search(x))

    # `_dash`, and a rescue's `ansible_failed_result` and `ansible_failed_task`, which hold the failed task's result
    # and the task, its arguments templated (fold 6's read 121, S3; fold 7's read 131, S1)
    tainted, grew = {"_dash", "ansible_failed_result", "ansible_failed_task"}, True
    # a fact, register or var set by a task that reads a tainted name, or prints every var (which holds each:
    # fold 9's read 150, S-D), is tainted too
    while grew:
        pat = re.compile(r"\b(?:%s)\b" % "|".join(sorted(map(re.escape, tainted))))
        new = set().union(*(names(t, pat) for t in reading(pat) + [t for t in every if _prints_every_var(w, t)]))
        grew, tainted = not new <= tainted, tainted | new
    readers = reading(pat)
    read = {id(t) for t in readers}
    outside = [(p.get("name"), k) for p in w.plays + w.imports for k, v in p.items()   # an import's tags and when too
               if k not in ("vars", "pre_tasks", "tasks", "post_tasks", "handlers") and pat.search(w.expand(v))]
    outside += [(t.get("name"), "environment") for t in every
                if pat.search(w.expand([t.get("environment")] + [a.get("environment") for a in applied(t)]))]
    outside += [(t.get("name"), k) for t in every    # printed by default, on a failure or at -vvv (111, N1)
                for k, v in (("delegate_to", t.get("delegate_to")), ("notify", t.get("notify")),
                             ("raw", args(t, "raw")), ("script", args(t, "script")))
                if v is not None and pat.search(w.expand(v))]
    derived = [(p.get("name"), k) for p in w.plays for k, v in (p.get("vars") or {}).items()
               if k != "_dash" and pat.search(w.expand(v))]
    derived += [(meta, k) for meta, dep in w.deps for k, v in dep.items()   # a dependency sees its params as vars
                if k not in ("role", "name") and pat.search(w.expand(v))]
    decrypts = [k for p in w.plays for k, v in (p.get("vars") or {}).items() if _DECRYPTS.search(w.expand(v))]
    decrypts += [(meta, k) for meta, dep in w.deps for k, v in dep.items() if _DECRYPTS.search(w.expand(v))]
    decrypts += [t.get("name") for t in every if _DECRYPTS.search(w.text(t))]
    for f in w.var_files:
        if re.search(r"\.sops\.(ya?ml|json)$", f):     # ansible.cfg's community.sops vars plugin decrypts it
            decrypts.append(f)
            continue
        for k, v in (load(f) or {}).items():         # the walk refuses a file that is not a mapping (140, S1)
            derived += [(f, k)] if pat.search(w.expand(v)) else []
            decrypts += [(f, k)] if _DECRYPTS.search(w.expand(v)) else []
    return {"every": every, "tainted": tainted, "readers": readers, "derived": derived, "decrypts": decrypts,
            "unlogged": [t.get("name") for t in every
                         if (id(t) in read or _prints_every_var(w, t)) and t.get("no_log") is not True],
            "named": [t.get("name") for t in every if templates(pat, t, t.get("name"))],
            "message": [t.get("name") for t in every if any(templates(pat, t, m) for m in _messages(t))
                        or handed(pat, t)
                        or any(templates(pat, t, v) for k, v in t.items() if str(k).startswith("with_"))],
            "outside": outside}


_FACT = {"name": "keep a key", "no_log": True, "ansible.legacy.set_fact": {"_f": "{{ _dash.alert_notifier_url }}"}}
_DEBUG_DASH = [{"name": "h", "ansible.builtin.debug": {"var": "_dash"}}]
_URL = "{{ _dash.alert_notifier_url }}"
_CHECK_H = [{"name": "check {{ _h }}", "no_log": True, "ansible.builtin.debug": {"msg": "hi"}}]
_DECRYPT = "{{ lookup('community.sops.sops', _secrets_dir ~ '/dashboards.sops.yml') }}"
# Two cacheable set_facts in the sound tree: `cacheable` names no fact (fold 5's read 110, N3).
_CACHED = [{"name": "cache a key", "no_log": True, "ansible.builtin.set_fact": {"_c": _URL, "cacheable": True}},
           {"name": "cache a word", "ansible.builtin.set_fact": {"_w": "x", "cacheable": True}}]
# A debug of two facts by a literal key, which prints those facts, not every var (fold 7's own mutants).
_BY_KEY = [{"name": "a fact by its key", "ansible.builtin.debug": {
    "msg": "{{ ansible_facts.hostname }} {{ ansible_facts['os_family'] }}"}}]
# A no_log assert whose `success_msg` reads the URL: a passing no_log task prints nothing (fold 6's read 120, N1).
_PASSES = [{"name": "passes", "no_log": True, "ansible.builtin.assert": {"that": "true", "success_msg": _URL}}]
# Three debugs that print no var: the word in plain text, a fact's attribute of that name (fold 7's read 130, N3), and
# a name that ends in it (fold 8's read 140, N2: W10).
_PLAIN = [{"name": "a word", "ansible.builtin.debug": {"msg": "the vars are set"}},
          {"name": "an attribute", "ansible.builtin.debug": {"msg": "{{ _w.vars | default('') }}"}},
          {"name": "a name so", "ansible.builtin.debug": {"msg": "{{ _my_vars | default('') }}"}}]
# A no_log `vars` lookup of a literal name, whose failure prints that name alone (fold 9's own mutants).
_BY_NAME = [{"name": "a var by its name", "no_log": True, "ansible.builtin.set_fact": {
    "_v": "{{ lookup('vars', '_w') }}"}}]
_SOUND = _CACHED + _BY_KEY + _PASSES + _PLAIN + _BY_NAME   # the sound tree's tasks after its one render
_READER = {"ansible/playbooks/templates/plain.j2": "plain\n", "ansible/playbooks/templates/reader.j2": _URL + "\n"}
_LOOKS_UP = "{{ lookup('template', 'reader.j2') }}"
# A template that decrypts the domain, and a lookup of it.
_DECRYPTER = {"ansible/playbooks/templates/dec.j2": _DECRYPT + "\n"}
_LOOKS_UP_DEC = "{{ lookup('template', 'dec.j2') }}"
_LEAK_SHAPES = {    # (tasks, play keys (`vars` joins the play's), {file under the tree: its document}, an imported
    #               playbook's plays, the leak)
    "P12-a-no-log-reader-named-from-it": ([{"name": "check {{ _dash.alert_notifier_url }}", "no_log": True,
                                            "ansible.builtin.debug": {"msg": "hi"}}], {}, {}, None, "named"),
    "P5-an-include-applies-it-as-environment": (
        [{"name": "inc", "no_log": True, "ansible.builtin.include_tasks": {
            "file": "t.yml", "apply": {"no_log": True, "environment": {"X": "{{ _dash.alert_notifier_url }}"}}}}],
        {}, {"ansible/playbooks/t.yml": [{"name": "t", "no_log": True, "ansible.builtin.debug": {"msg": "hi"}}]},
        None, "outside"),
    "P6-a-legacy-set-fact-then-a-debug": ([_FACT, {"name": "show", "ansible.builtin.debug": {"var": "_f"}}], {}, {},
                                          None, "unlogged"),
    "P1-a-play-var-from-a-fact": ([_FACT], {"vars": {"_g": "{{ _f }}"}}, {}, None, "derived"),
    "P2-a-role-default-from-a-fact": ([_FACT], {}, {"ansible/roles/r/defaults/main.yml": {"_g": "{{ _f }}"}}, None,
                                      "derived"),
    "T3-role-vars-main-yaml": ([], {}, {"ansible/roles/r/vars/main.yaml": {"_g": "{{ _dash }}"}}, None, "derived"),
    "T3c-role-vars-main-dir": ([], {}, {"ansible/roles/r/vars/main/a.yml": {"_g": "{{ _dash }}"}}, None, "derived"),
    "P4-vars-from-a-yaml-file": ([{"ansible.builtin.include_role": {"name": "r", "vars_from": "fb4"}}], {},
                                 {"ansible/roles/r/vars/fb4.yaml": {"_g": "{{ _dash }}"}}, None, "derived"),
    "T3b-handlers-from-a-yaml-file": ([{"ansible.builtin.include_role": {"name": "r", "handlers_from": "fb4"}}], {},
                                      {"ansible/roles/r/handlers/fb4.yaml": _DEBUG_DASH}, None, "unlogged"),
    "P8-include-vars-dir": ([{"ansible.builtin.include_vars": {"dir": "fb4"}}], {},
                            {"ansible/playbooks/fb4/a.yml": {"_g": "{{ _dash }}"}}, None, "derived"),
    "P9-host-vars-json": ([], {}, {"ansible/playbooks/host_vars/localhost.json": {"_g": "{{ _dash }}"}}, None,
                          "derived"),
    "P9b-host-vars-with-no-extension": ([], {}, {"ansible/playbooks/host_vars/localhost": {"_g": "{{ _dash }}"}},
                                        None, "derived"),
    "P3-a-second-decrypt-in-role-defaults": ([], {}, {"ansible/roles/r/defaults/main.yml": {
        "_d2": "{{ lookup('community.sops.sops', _secrets_dir ~ '/dashboards.sops.yml') }}"}}, None, "decrypts"),
    "a-sops-vars-file-beside-the-playbook": ([], {}, {"ansible/playbooks/group_vars/all.sops.yml": {"x": "enc"}},
                                             None, "decrypts"),
    "P7-an-imported-playbook-prints-it": ([], {}, {}, [{"hosts": "localhost", "tasks": _DEBUG_DASH}], "unlogged"),
    "F1-a-k-v-set-fact-then-a-debug": ([{"name": "kv", "no_log": True, "ansible.builtin.set_fact": "_k=" + _URL},
                                        {"name": "show", "ansible.builtin.debug": {"var": "_k"}}], {}, {}, None,
                                       "unlogged"),
    "F2-a-register-of-a-reader-then-a-debug": (
        [{"name": "r", "no_log": True, "ansible.builtin.debug": {"msg": _URL}, "register": "_r"},
         {"name": "show", "ansible.builtin.debug": {"var": "_r"}}], {}, {}, None, "unlogged"),
    "F3-an-include-var-an-included-name-reads": (
        [{"name": "inc", "no_log": True, "ansible.builtin.include_tasks": "t.yml", "vars": {"_h": _URL}}], {},
        {"ansible/playbooks/t.yml": _CHECK_H}, None, "named"),
    "F4-an-apply-var-an-included-name-reads": (
        [{"name": "inc", "no_log": True, "ansible.builtin.include_tasks": {
            "file": "t.yml", "apply": {"no_log": True, "vars": {"_h": _URL}}}}], {},
        {"ansible/playbooks/t.yml": _CHECK_H}, None, "named"),
    "F5-an-include-loops-over-it": (
        [{"name": "inc", "no_log": True, "ansible.builtin.include_tasks": "t.yml", "loop": [_URL]}], {},
        {"ansible/playbooks/t.yml": [{"name": "t", "ansible.builtin.debug": {"var": "item"}}]}, None, "unlogged"),
    "F6-a-play-environment": ([], {"environment": {"X": _URL}}, {}, None, "outside"),
    "F7-a-task-decrypts-it-again": (
        [{"name": "again", "no_log": True, "ansible.builtin.set_fact": {
            "_d2": "{{ lookup('community.sops.sops', _secrets_dir ~ '/dashboards.sops.yml') }}"}}], {}, {}, None,
        "decrypts"),
    "F8-load-vars-of-a-domain-by-a-var": (
        [{"name": "load", "no_log": True, "community.sops.load_vars": {
            "file": "{{ _secrets_dir }}/{{ _which }}.sops.yml", "name": "_d3"}}], {}, {}, None, "decrypts"),
    "F9-a-bool-set-twice-once-from-it": (
        [{"name": "shape", "no_log": True, "ansible.builtin.set_fact": {"_alert_url_ok": "{{ _dash is mapping }}"}},
         {"name": "again", "no_log": True, "ansible.builtin.set_fact": {"_alert_url_ok": _URL}},
         {"name": "show", "ansible.builtin.debug": {"var": "_alert_url_ok"}}], {}, {}, None, "unlogged"),
    "S1-a-no-log-assert-fails-with-it": ([{"name": "check", "no_log": True, "ansible.builtin.assert": {
        "that": "_dash is mapping", "fail_msg": "bad " + _URL}}], {}, {}, None, "message"),
    "S1b-a-no-log-fail-with-it": ([{"name": "refuse", "no_log": True, "ansible.builtin.fail": {"msg": "no " + _URL},
                                    "when": "false"}], {}, {}, None, "message"),
    "S1c-a-k-v-fail-with-it": ([{"name": "refuse", "no_log": True, "fail": "msg='no " + _URL + "'", "when": "false"}],
                               {}, {}, None, "message"),
    "S2-a-template-reads-it": ([{"name": "extras", "ansible.builtin.template": {"src": "extras.j2", "dest": "/x"}}],
                               {}, {"ansible/playbooks/templates/extras.j2": "u=" + _URL + "\n"}, None, "unlogged"),
    "S2b-a-template-includes-a-reader": (
        [{"name": "extras", "ansible.builtin.template": {"src": "outer.j2", "dest": "/x"}}], {},
        {"ansible/playbooks/templates/outer.j2": "{% include 'inner.j2' %}\n",
         "ansible/playbooks/templates/inner.j2": "u=" + _URL + "\n"}, None, "unlogged"),
    "S2c-a-template-lookup-reads-it": ([{"name": "show", "ansible.builtin.debug": {
        "msg": "{{ lookup('template', 'lt.j2') }}"}}], {}, {"ansible/playbooks/templates/lt.j2": _URL + "\n"}, None,
        "unlogged"),
    "S2d-a-role-template-before-the-playbooks": (
        [{"ansible.builtin.include_role": {"name": "r", "tasks_from": "t2"}}], {},
        {"ansible/roles/r/tasks/t2.yml": [{"name": "rt", "ansible.builtin.template": {"src": "rt.j2", "dest": "/x"}}],
         "ansible/roles/r/templates/rt.j2": _URL + "\n", "ansible/playbooks/templates/rt.j2": "plain\n"}, None,
        "unlogged"),
    "S2e-an-inventory-group-var": ([], {}, {"instance.example/inventory/group_vars/all.yml": {"_g": _URL}}, None,
                                   "derived"),
    "S2f-a-role-beside-the-playbook": ([], {}, {"ansible/playbooks/roles/r/tasks/main.yml": _DEBUG_DASH}, None,
                                       "unlogged"),
    "S2g-include-vars-from-vars-first": (
        [{"ansible.builtin.include_vars": "iv.yml"}], {},
        {"ansible/playbooks/iv.yml": {"_g": "plain"}, "ansible/playbooks/vars/iv.yml": {"_g": _URL}}, None, "derived"),
    "S2h-a-role-include-vars-from-its-vars": (
        [{"ansible.builtin.include_role": {"name": "r", "tasks_from": "t3"}}], {},
        {"ansible/roles/r/tasks/t3.yml": [{"ansible.builtin.include_vars": {"file": "rv.yml"}}],
         "ansible/roles/r/tasks/rv.yml": {"_g": "plain"}, "ansible/roles/r/vars/rv.yml": {"_g": _URL}}, None,
        "derived"),
    "S2i-vars-files-from-vars-first": ([], {"vars_files": ["vf.yml"]},
                                       {"ansible/playbooks/vf.yml": {"_g": "plain"},
                                        "ansible/playbooks/vars/vf.yml": {"_g": _URL}}, None, "derived"),
    "N1-a-debug-of-vars": ([{"name": "all", "ansible.builtin.debug": {"var": "vars"}}], {}, {}, None, "unlogged"),
    "N1b-a-k-v-debug-of-hostvars": ([{"name": "all", "debug": "var=hostvars[inventory_hostname]"}], {}, {}, None,
                                    "unlogged"),
    "N1c-a-debug-msg-of-hostvars": ([{"name": "all", "ansible.builtin.debug": {"msg": "{{ hostvars }}"}}], {}, {},
                                    None, "unlogged"),
    "N1d-a-meta-dependency-param": ([], {}, {
        "ansible/roles/r/meta/main.yml": {"dependencies": [{"role": "d", "_x": _URL}]},
        "ansible/roles/d/tasks/main.yml": [{"name": "d", "ansible.builtin.debug": {"var": "_x"}}]}, None, "derived"),
    "N1e-delegated-to-it": ([{"name": "to", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
                             "delegate_to": _URL}], {}, {}, None, "outside"),
    "N1f-notifies-a-name-from-it": ([{"name": "n", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
                                      "notify": "tell " + _URL}], {}, {}, None, "outside"),
    "N1g-a-raw-argument": ([{"name": "raw", "no_log": True, "ansible.builtin.raw": "echo " + _URL}], {}, {}, None,
                           "outside"),
    "L-no-log-a-string": ([{"name": "show", "no_log": "false", "ansible.builtin.debug": {"var": "_dash"}}], {}, {},
                          None, "unlogged"),
    "L-a-sops-json-vars-file": ([], {}, {"ansible/playbooks/group_vars/all.sops.json": {"x": "enc"}}, None,
                                "decrypts"),
    "L-a-name-statement-reads-it": ([{"name": "c {% for c in _dash.alert_notifier_url %}{{ c }}{% endfor %}",
                                      "no_log": True, "ansible.builtin.debug": {"msg": "hi"}}], {}, {}, None, "named"),
    "L-a-bool-from-it-then-reset": (
        [{"name": "ok", "no_log": True, "ansible.builtin.set_fact": {"_alert_url_ok": _URL}},
         {"name": "show", "ansible.builtin.debug": {"var": "_alert_url_ok"}},
         {"name": "reset", "no_log": True, "ansible.builtin.set_fact": {"_alert_url_ok": "{{ false }}"}}], {}, {}, None,
        "unlogged"),
    "M-a-script-argument": ([{"name": "script", "no_log": True, "ansible.builtin.script": "run.sh " + _URL}], {}, {},
                            None, "outside"),
    "M-a-dependency-param-decrypts": ([], {}, {
        "ansible/roles/r/meta/main.yml": {"dependencies": [{"role": "d", "_y": _DECRYPT}]},
        "ansible/roles/d/tasks/main.yml": [{"name": "d", "ansible.builtin.debug": {"msg": "hi"}}]}, None, "decrypts"),
    "M-a-template-decrypts": ([{"name": "tpl", "no_log": True,
                                "ansible.builtin.template": {"src": "d.j2", "dest": "/x"}}],
                              {}, {"ansible/playbooks/templates/d.j2": _DECRYPT + "\n"}, None, "decrypts"),
    "M-a-two-line-name-reads-it": ([{"name": "c {{\n _dash.alert_notifier_url }}", "no_log": True,
                                     "ansible.builtin.debug": {"msg": "hi"}}], {}, {}, None, "named"),
    "S3-a-k-v-debug-of-vars-quoted": ([{"name": "all", "debug": 'msg="{{ vars }}"'}], {}, {}, None, "unlogged"),
    "S3b-a-template-of-vars": ([{"name": "tv", "ansible.builtin.template": {"src": "v.j2", "dest": "/x"}}], {},
                               {"ansible/playbooks/templates/v.j2": "{{ vars }}\n"}, None, "unlogged"),
    "S3c-a-rescue-prints-the-failed-result": (
        [{"block": [{"name": "fails", "ansible.builtin.include_tasks": "t7.yml"}],
          "rescue": [{"name": "why", "ansible.builtin.debug": {"var": "ansible_failed_result"}}]}], {},
        {"ansible/playbooks/t7.yml": [{"name": "r", "no_log": True, "ansible.builtin.fail": {"msg": "no"},
                                       "when": "_dash is mapping"}]}, None, "unlogged"),
    "S3d-a-no-log-lookup-handed-it": ([{"name": "f", "no_log": True, "ansible.builtin.set_fact": {
        "_x": "{{ lookup('ansible.builtin.file', _dash.alert_notifier_url) }}"}}], {}, {}, None, "message"),
    "S1d-a-no-log-assert-msg-alias": ([{"name": "check", "no_log": True, "ansible.builtin.assert": {
        "that": "_dash is mapping", "msg": "bad " + _URL}}], {}, {}, None, "message"),
    "N2-a-debug-of-ansible-facts": ([{"name": "facts", "ansible.builtin.debug": {"var": "ansible_facts"}}], {}, {},
                                    None, "unlogged"),
    "N2b-a-debug-of-a-vars-lookup": ([{"name": "v", "ansible.builtin.debug": {"msg": "{{ lookup('vars', _w) }}"}}],
                                     {}, {}, None, "unlogged"),
    "N3-an-add-host-var-from-it": ([{"name": "add", "no_log": True, "ansible.builtin.add_host": {"name": "h2",
                                                                                             "_u": _URL}},
                                    {"name": "show", "ansible.builtin.debug": {"var": "_u"}}], {}, {}, None,
                                   "unlogged"),
    "C6-a-play-var-looks-up-a-reader": ([], {"vars": {"_g": _LOOKS_UP}}, _READER, None, "derived"),
    "C6b-a-vars-file-looks-up-a-reader": ([], {"vars_files": ["vf7.yml"]},
                                          dict(_READER, **{"ansible/playbooks/vf7.yml": {"_g": _LOOKS_UP}}), None,
                                          "derived"),
    "Q5-a-group-var-looks-up-a-reader": (
        [], {}, dict(_READER, **{"instance.example/inventory/group_vars/all.yml": {"_g": _LOOKS_UP}}), None, "derived"),
    "Q6-a-template-looks-up-a-reader": ([{"name": "outer", "ansible.builtin.template": {"src": "o.j2", "dest": "/x"}}],
                                        {}, dict(_READER, **{"ansible/playbooks/templates/o.j2": _LOOKS_UP + "\n"}),
                                        None, "unlogged"),
    "Q7-with-template-a-reader": ([{"name": "wt", "ansible.builtin.debug": {"var": "item"},
                                    "with_template": ["reader.j2"]}], {}, _READER, None, "unlogged"),
    "Q13-a-second-lookup-term": ([{"name": "two", "ansible.builtin.debug": {
        "msg": "{{ lookup('template', 'plain.j2', 'reader.j2') }}"}}], {}, _READER, None, "unlogged"),
    "C17-a-legacy-template-lookup": ([{"name": "legacy", "ansible.builtin.debug": {
        "msg": "{{ lookup('ansible.legacy.template', 'reader.j2') }}"}}], {}, _READER, None, "unlogged"),
    "Q14-a-template-under-action": ([{"name": "act", "action": "ansible.builtin.template src=reader.j2 dest=/x"}], {},
                                    _READER, None, "unlogged"),
    "Q14b-a-template-under-local-action": ([{"name": "la", "local_action": {"module": "template", "src": "reader.j2",
                                                                           "dest": "/x"}}], {}, _READER, None,
                                           "unlogged"),
    "C11-a-template-under-args": ([{"name": "ar", "ansible.builtin.template": None,
                                    "args": {"src": "reader.j2", "dest": "/x"}}], {}, _READER, None, "unlogged"),
    "Q11-a-lookups-include-in-its-own-directory": (
        [{"name": "own", "ansible.builtin.debug": {"msg": "{{ lookup('template', 'own/o.j2') }}"}}], {},
        {"ansible/playbooks/templates/own/o.j2": "{% include 'oi.j2' %}\n",
         "ansible/playbooks/templates/own/oi.j2": _URL + "\n", "ansible/playbooks/templates/oi.j2": "plain\n"},
        None, "unlogged"),
    "C18-an-include-in-the-top-templates-directory": (
        [{"name": "top", "ansible.builtin.template": {"src": "{{ playbook_dir }}/files/top.j2", "dest": "/x"}}], {},
        {"ansible/playbooks/files/top.j2": "{% include 'fsub/n.j2' %}\n",
         "ansible/playbooks/files/fsub/n.j2": "{% include 't-only.j2' %}\n",
         "ansible/playbooks/files/t-only.j2": _URL + "\n"}, None, "unlogged"),
    "C7-a-dependency-renders-its-dependents-template": ([], {}, {
        "ansible/roles/r/meta/main.yml": {"dependencies": ["d"]},
        "ansible/roles/d/tasks/main.yml": [{"name": "dt", "ansible.builtin.template": {"src": "p.j2", "dest": "/x"}}],
        "ansible/roles/r/templates/p.j2": _URL + "\n"}, None, "unlogged"),
    "C13-an-include-role-from-a-role": (
        [{"ansible.builtin.include_role": {"name": "r", "tasks_from": "t5"}}], {},
        {"ansible/roles/r/tasks/t5.yml": [{"ansible.builtin.include_role": {"name": "q"}}],
         "ansible/roles/q/tasks/main.yml": [{"ansible.builtin.include_vars": "qv.yml"}],
         "ansible/roles/r/vars/qv.yml": {"_g": _URL}}, None, "derived"),
    "C8-a-roles-nested-include-from-its-tasks": (
        [{"ansible.builtin.include_role": {"name": "r", "tasks_from": "sub/n1"}}], {},
        {"ansible/roles/r/tasks/sub/n1.yml": [{"ansible.builtin.include_tasks": "n2.yml"}],
         "ansible/roles/r/tasks/n2.yml": _DEBUG_DASH,
         "ansible/roles/r/tasks/sub/n2.yml": [{"name": "plain", "ansible.builtin.debug": {"msg": "hi"}}]}, None,
        "unlogged"),
    "C12-an-imported-playbooks-own-role": ([], {}, {
        "ansible/playbooks/sub/p.yml": [{"hosts": "localhost", "roles": ["s"]}],
        "ansible/playbooks/sub/roles/s/tasks/main.yml": _DEBUG_DASH},
        [{"ansible.builtin.import_playbook": "sub/p.yml"}], "unlogged"),
    "C9-the-inventory-files-own-vars": ([], {}, {"instance.example/inventory/hosts.yml": {"all": {"vars": {
        "_g": _URL}}}}, None, "derived"),
    "C25-an-include-vars-dirs-extensions": ([{"ansible.builtin.include_vars": {"dir": "ed", "extensions": ["conf"]}}],
                                            {}, {"ansible/playbooks/ed/a.conf": {"_g": _URL}}, None, "derived"),
    "C21-a-first-found-vars-files-list": ([], {"vars_files": [["nothere.yml", "ff.yml"]]},
                                          {"ansible/playbooks/ff.yml": {"_g": _URL}}, None, "derived"),
    "N5-a-k-v-include-vars": ([{"ansible.builtin.include_vars": "file=kvv.yml"}], {},
                              {"ansible/playbooks/kvv.yml": {"_g": _URL}}, None, "derived"),
    "F7-a-template-hands-it-to-a-lookup": (
        [{"name": "tl", "no_log": True, "ansible.builtin.template": {"src": "lk.j2", "dest": "/x"}}], {},
        {"ansible/playbooks/templates/lk.j2": "{{ lookup('ansible.builtin.env', _dash.alert_notifier_url) }}\n"}, None,
        "message"),
    "F7-a-no-log-query-handed-it": ([{"name": "f", "no_log": True, "ansible.builtin.set_fact": {
        "_x": "{{ query('ansible.builtin.env', _dash.alert_notifier_url) }}"}}], {}, {}, None, "message"),
    "F7-a-lookup-term-after-a-parenthesis": ([{"name": "f", "no_log": True, "ansible.builtin.set_fact": {
        "_x": "{{ lookup('ansible.builtin.env', ('HOME'), _dash.alert_notifier_url) }}"}}], {}, {}, None, "message"),
    "F7-a-debug-var-of-a-vars-lookup": ([{"name": "v", "ansible.builtin.debug": {"var": "lookup('vars', _w)"}}], {}, {},
                                        None, "unlogged"),
    "F7-a-play-environment-looks-up-a-reader": ([], {"environment": {"X": _LOOKS_UP}}, _READER, None, "outside"),
    "F7-a-task-environment-looks-up-a-reader": ([{"name": "e", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
                                                  "environment": {"X": _LOOKS_UP}}], {}, _READER, None, "outside"),
    "F7-delegated-to-a-lookup-of-a-reader": ([{"name": "to", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
                                               "delegate_to": _LOOKS_UP}], {}, _READER, None, "outside"),
    "F7-a-dependency-param-looks-up-a-reader": ([], {}, dict(_READER, **{
        "ansible/roles/r/meta/main.yml": {"dependencies": [{"role": "d", "_x": _LOOKS_UP}]},
        "ansible/roles/d/tasks/main.yml": [{"name": "d", "ansible.builtin.debug": {"msg": "hi"}}]}), None, "derived"),
    "F7-a-play-var-looks-up-a-decrypt": ([], {"vars": {"_g": _LOOKS_UP_DEC}}, _DECRYPTER, None, "decrypts"),
    "F7-a-vars-file-looks-up-a-decrypt": ([], {"vars_files": ["vf8.yml"]}, dict(_DECRYPTER, **{
        "ansible/playbooks/vf8.yml": {"_g": _LOOKS_UP_DEC}}), None, "decrypts"),
    "F7-a-bare-include-applies-it-as-environment": (
        [{"name": "inc", "no_log": True, "include_tasks": {
            "file": "t.yml", "apply": {"no_log": True, "environment": {"X": _URL}}}}],
        {}, {"ansible/playbooks/t.yml": [{"name": "t", "no_log": True, "ansible.builtin.debug": {"msg": "hi"}}]},
        None, "outside"),
    # Fold 8, from fold 7's reads: a template under a prefixed `with_…template` (130, S1; 131, S2); each place Ansible
    # loads a file from that the walk did not search, beside a decoy the walk read (130, S2; 131, S6); a no_log loop
    # handed the URL by a lookup's terms (130, S4; 131, S2); a lookup with a space before its parenthesis (131, S2); an
    # inventory subdirectory's file (131, S5); a rescue's `ansible_failed_task` (131, S1); each expression that prints
    # every var (130, S5); and the rows that kill fold 7's surviving mutants (130, N3).
    "U1-with-a-builtin-template-a-reader": ([{"name": "wtb", "ansible.builtin.debug": {"msg": "{{ item }}"},
                                              "with_ansible.builtin.template": "reader.j2"}], {}, _READER, None,
                                            "unlogged"),
    "U1b-with-a-legacy-template-a-reader": ([{"name": "wtl", "ansible.builtin.debug": {"msg": "{{ item }}"},
                                              "with_ansible.legacy.template": ["reader.j2"]}], {}, _READER, None,
                                            "unlogged"),
    "U3-a-nested-playbooks-own-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost", "tasks": [{"name": "pd", "ansible.builtin.template": {
            "src": "{{ playbook_dir }}/pd.j2", "dest": "/x"}}]}],
        "ansible/playbooks/sub/pd.j2": _URL + "\n", "ansible/playbooks/pd.j2": "plain\n"},
        [{"ansible.builtin.import_playbook": "sub/o.yml"}], "unlogged"),
    "U4-a-roles-include-in-the-working-directorys-tasks": ([], {"roles": ["r", "q"]}, {
        "ansible/roles/q/tasks/main.yml": [{"name": "inc", "ansible.builtin.include_tasks": "x.yml"}],
        "ansible/tasks/x.yml": [{"name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}],
        "ansible/playbooks/tasks/x.yml": [{"name": "decoy", "ansible.builtin.debug": {"msg": "hi"}}]}, None,
        "unlogged"),
    "U5-a-nested-include-under-its-parents-directory": (
        [{"name": "outer", "ansible.builtin.include_tasks": "tasks/o.yml"}], {},
        {"ansible/playbooks/tasks/o.yml": [{"name": "mid", "ansible.builtin.include_tasks": "in/m.yml"}],
         "ansible/playbooks/tasks/in/m.yml": [{"name": "leaf", "ansible.builtin.include_tasks": "l.yml"}],
         "ansible/playbooks/in/l.yml": [{"name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}],
         "ansible/playbooks/tasks/in/l.yml": [{"name": "decoy", "ansible.builtin.debug": {"msg": "hi"}}]}, None,
        "unlogged"),
    "U6-a-roles-lookup-below-its-tasks": ([], {"roles": ["r", "q"]}, {
        "ansible/roles/q/tasks/main.yml": [{"name": "sub", "ansible.builtin.include_tasks": "sub/s.yml"}],
        "ansible/roles/q/tasks/sub/s.yml": [{"name": "show", "ansible.builtin.debug": {
            "msg": "{{ lookup('template', 'x.j2') }}"}}],
        "ansible/roles/q/tasks/x.j2": _URL + "\n", "ansible/playbooks/templates/x.j2": "plain\n"}, None, "unlogged"),
    "U8-a-no-log-with-url-loop": ([{"name": "reach", "no_log": True, "ansible.builtin.debug": {"msg": "{{ item }}"},
                                    "with_url": [_URL]}], {}, {}, None, "message"),
    "U8b-a-no-log-builtin-file-loop": ([{"name": "reach", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
                                         "with_ansible.builtin.file": _URL}], {}, {}, None, "message"),
    "F9-a-no-log-with-url-loop-of-a-multi-line-item": (
        [{"name": "reach", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
          "with_url": ["{{ _dash.alert_notifier_url\n}}"]}], {}, {}, None, "message"),
    "U11-a-roles-handler-include-in-the-playbooks-handlers": ([], {"roles": ["r", "q"]}, {
        "ansible/roles/q/tasks/main.yml": [{"name": "go", "ansible.builtin.debug": {"msg": "go"}, "changed_when": True,
                                            "notify": "h"}],
        "ansible/roles/q/handlers/main.yml": [{"name": "h", "ansible.builtin.include_tasks": "hx.yml"}],
        "ansible/playbooks/handlers/hx.yml": [{"name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}],
        "ansible/playbooks/hx.yml": [{"name": "decoy", "ansible.builtin.debug": {"msg": "hi"}}]}, None, "unlogged"),
    "A1-a-spaced-template-lookup-in-a-debug": ([{"name": "show", "ansible.builtin.debug": {
        "msg": "{{ lookup ('ansible.builtin.template', 'reader.j2') }}"}}], {}, _READER, None, "unlogged"),
    "A2-a-play-var-spaced-template-lookup": ([], {"vars": {"_g": "{{ lookup ('template', 'reader.j2') }}"}}, _READER,
                                             None, "derived"),
    "A3-a-no-log-spaced-lookup-handed-it": ([{"name": "f", "no_log": True, "ansible.builtin.set_fact": {
        "_x": "{{ lookup ('ansible.builtin.file', _dash.alert_notifier_url) }}"}}], {}, {}, None, "message"),
    "A4-a-debug-of-a-spaced-builtin-vars-lookup": ([{"name": "v", "ansible.builtin.debug": {
        "msg": "{{ lookup ('ansible.builtin.vars', '_da' ~ 'sh') }}"}}], {}, {}, None, "unlogged"),
    "H3-an-inventory-subdirectorys-file": ([], {}, {"instance.example/inventory/site/hosts.yml": {"all": {"vars": {
        "_g": _URL}}}}, None, "derived"),
    "I1-a-nested-import-in-the-working-directorys-tasks": (
        [{"name": "a", "ansible.builtin.import_tasks": "tasks/a.yml"}], {},
        {"ansible/playbooks/tasks/a.yml": [{"name": "b", "ansible.builtin.import_tasks": "b.yml"}],
         "ansible/tasks/b.yml": [{"name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}],
         "ansible/playbooks/b.yml": [{"name": "decoy", "ansible.builtin.debug": {"msg": "hi"}}]}, None, "unlogged"),
    "I2-an-included-roles-include-in-the-working-directorys-tasks": (
        [{"name": "run q", "ansible.builtin.include_role": {"name": "q"}}], {},
        {"ansible/roles/q/tasks/main.yml": [{"name": "x", "ansible.builtin.include_tasks": "x.yml"}],
         "ansible/tasks/x.yml": [{"name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}],
         "ansible/playbooks/tasks/x.yml": [{"name": "decoy", "ansible.builtin.debug": {"msg": "hi"}}]}, None,
        "unlogged"),
    "R2-a-rescue-prints-the-failed-task": (
        [{"block": [{"name": "post", "ansible.builtin.include_tasks": "post.yml"}],
          "rescue": [{"name": "why", "ansible.builtin.debug": {"var": "ansible_failed_task"}}]}], {},
        {"ansible/playbooks/post.yml": [{"name": "post a test alert", "no_log": True,
                                         "ansible.builtin.uri": {"url": _URL, "method": "POST"}}]}, None, "unlogged"),
    "V1-a-debug-var-of-vars-in-a-list": ([{"name": "v", "ansible.builtin.debug": {"var": "[vars]"}}], {}, {}, None,
                                         "unlogged"),
    "V2-a-debug-var-templating-vars": ([{"name": "v", "ansible.builtin.debug": {"var": "{{ vars }}"}}], {}, {}, None,
                                       "unlogged"),
    "V3-a-copy-of-vars-under-diff": ([{"name": "v", "ansible.builtin.copy": {"content": "{{ vars | to_nice_json }}",
                                                                           "dest": "/x"}}], {}, {}, None, "unlogged"),
    "V4-a-lineinfile-of-vars-under-diff": ([{"name": "v", "ansible.builtin.lineinfile": {"path": "/x",
                                                                                       "line": "{{ vars }}"}}],
                                           {}, {}, None, "unlogged"),
    "V5-a-debug-var-defaulting-to-vars": ([{"name": "v", "ansible.builtin.debug": {"var": "_nope | default(vars)"}}],
                                          {}, {}, None, "unlogged"),
    "V6-a-debug-var-of-a-vars-lookup-in-a-list": ([{"name": "v", "ansible.builtin.debug": {
        "var": "[lookup('vars', _w)]"}}], {}, {}, None, "unlogged"),
    "K2-a-lookup-with-a-space-inside": ([{"name": "show", "ansible.builtin.debug": {
        "msg": "{{ lookup( 'template', 'reader.j2') }}"}}], {}, _READER, None, "unlogged"),
    "K3-a-play-var-looks-up-a-second-term": ([], {"vars": {"_g": "{{ lookup('template', 'plain.j2', 'reader.j2') }}"}},
                                             _READER, None, "derived"),
    "K4-a-yaml-inventory-file": ([], {}, {"instance.example/inventory/hosts.yaml": {"all": {"vars": {"_g": _URL}}}},
                                 None, "derived"),
    "K7-a-debug-of-a-builtin-vars-lookup": ([{"name": "v", "ansible.builtin.debug": {
        "msg": "{{ lookup('ansible.builtin.vars', _w) }}"}}], {}, {}, None, "unlogged"),
    "K8-a-debug-of-a-builtin-vars-query": ([{"name": "v", "ansible.builtin.debug": {
        "msg": "{{ query('ansible.builtin.vars', _w) }}"}}], {}, {}, None, "unlogged"),
    "K9-a-bare-add-host-var-from-it": ([{"name": "add", "no_log": True, "add_host": {"name": "h2", "_u": _URL}},
                                        {"name": "show", "ansible.builtin.debug": {"var": "_u"}}], {}, {}, None,
                                       "unlogged"),
    "K10-a-lookup-term-after-a-quoted-parenthesis": ([{"name": "f", "no_log": True, "ansible.builtin.set_fact": {
        "_x": "{{ lookup('ansible.builtin.file', ')' ~ _dash.alert_notifier_url) }}"}}], {}, {}, None, "message"),
    "G1b-a-templates-include-behind-a-quoted-comment-open": (
        [{"name": "t", "ansible.builtin.template": {"src": "q.j2", "dest": "/x"}}], {},
        dict(_READER, **{"ansible/playbooks/templates/q.j2": "{{ '{#' }}{% include 'reader.j2' %}{{ '#}' }}\n"}), None,
        "unlogged"),
    "G1d-a-templates-lookup-behind-a-quoted-comment-open": (
        [{"name": "t", "ansible.builtin.template": {"src": "q.j2", "dest": "/x"}}], {},
        dict(_READER, **{"ansible/playbooks/templates/q.j2": "{{ '{#' }}" + _LOOKS_UP + "\n"}), None, "unlogged"),
    "G4-an-import-entrys-tags-from-it": ([], {}, {"ansible/playbooks/sub/o.yml": [{"hosts": "localhost"}]},
                                         [{"ansible.builtin.import_playbook": "sub/o.yml", "tags": _URL}], "outside"),
    "G4b-an-import-entrys-when-looks-up-a-reader": ([], {}, dict(_READER, **{
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost"}]}), [{
            "ansible.builtin.import_playbook": "sub/o.yml", "when": "lookup('template', 'reader.j2') | length > 0"}],
        "outside"),
    "G5-a-roles-include-under-x-tasks-in-its-tasks-x": ([], {"roles": ["r", "q"]}, {
        "ansible/roles/q/tasks/main.yml": [{"name": "a", "ansible.builtin.include_tasks": "x/tasks/a.yml"}],
        "ansible/roles/q/tasks/x/tasks/a.yml": [{"name": "b", "ansible.builtin.include_tasks": "b.yml"}],
        "ansible/roles/q/tasks/x/b.yml": [{"name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}],
        "ansible/playbooks/x/tasks/b.yml": [{"name": "decoy", "ansible.builtin.debug": {"msg": "hi"}}]}, None,
        "unlogged"),
    "G6-a-set-facts-raw-block-looks-up-a-reader": ([{"name": "r", "ansible.builtin.set_fact": {
        "_r": "{% raw %}" + _LOOKS_UP + "{% endraw %}"}}], {}, _READER, None, "unlogged"),
    "G6b-a-set-facts-literal-looks-up-a-reader": ([{"name": "r", "ansible.builtin.set_fact": {
        "_r": "{{ '{{ lookup(\"template\", \"reader.j2\") }}' }}"}}], {}, _READER, None, "unlogged"),
    "G7-a-no-log-tasks-name-of-vars": ([{"name": "{{ vars }}", "no_log": True, "ansible.builtin.debug": {
        "msg": "hi"}}], {}, {}, None, "named"),
    "G7b-a-no-log-name-past-a-quoted-close": ([{"name": "{{ '}}' ~ _dash.alert_notifier_url }}", "no_log": True,
                                                "ansible.builtin.debug": {"msg": "hi"}}], {}, {}, None, "named"),
    "G8-a-no-log-asserts-fail-msg-of-vars": ([{"name": "a", "no_log": True, "ansible.builtin.assert": {
        "that": "false", "fail_msg": "{{ vars | string }}"}}], {}, {}, None, "message"),
    "G8b-a-no-log-fails-msg-of-hostvars": ([{"name": "f", "no_log": True, "ansible.builtin.fail": {
        "msg": "{{ hostvars | string }}"}}], {}, {}, None, "message"),
    "G8c-a-no-log-lookup-handed-vars": ([{"name": "f", "no_log": True, "ansible.builtin.set_fact": {
        "_x": "{{ lookup('ansible.builtin.file', vars | string) }}"}}], {}, {}, None, "message"),
    "G9-a-debug-of-ansible-facts-items": ([{"name": "f", "ansible.builtin.debug": {
        "msg": "{{ ansible_facts.items() | list }}"}}], {}, {}, None, "unlogged"),
    "G10-a-debug-of-vars-in-a-nested-mapping": ([{"name": "c", "ansible.builtin.debug": {
        "msg": "{{ {'a': {'b': 1}} | combine({'v': vars}) }}"}}], {}, {}, None, "unlogged"),
    # a play in sub/o.yml, which other.yml imports, under its own `{{ playbook_dir }}`, beside the top's decoy
    "W1-a-nested-playbooks-with-template-of-its-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost", "tasks": [{
            "name": "wt", "ansible.builtin.debug": {"msg": "{{ item }}"},
            "with_template": "{{ playbook_dir }}/wt.j2"}]}],
        "ansible/playbooks/sub/wt.j2": _URL + "\n", "ansible/playbooks/wt.j2": "plain\n"},
        [{"ansible.builtin.import_playbook": "sub/o.yml"}], "unlogged"),
    "W2-a-nested-playbooks-include-vars-dir-of-its-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost", "tasks": [{
            "name": "ivd", "ansible.builtin.include_vars": {"dir": "{{ playbook_dir }}/vd"}}]}],
        "ansible/playbooks/sub/vd/a.yml": {"_g": _URL}, "ansible/playbooks/vd/a.yml": {"_g": "x"}},
        [{"ansible.builtin.import_playbook": "sub/o.yml"}], "derived"),
    "W3-a-nested-playbooks-include-vars-of-its-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost", "tasks": [{
            "name": "ivf", "ansible.builtin.include_vars": "{{ playbook_dir }}/vf.yml"}]}],
        "ansible/playbooks/sub/vf.yml": {"_g": _URL}, "ansible/playbooks/vf.yml": {"_g": "x"}},
        [{"ansible.builtin.import_playbook": "sub/o.yml"}], "derived"),
    "W4-a-nested-playbooks-vars-files-of-its-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost", "vars_files": ["{{ playbook_dir }}/vv.yml"]}],
        "ansible/playbooks/sub/vv.yml": {"_g": _URL}, "ansible/playbooks/vv.yml": {"_g": "x"}},
        [{"ansible.builtin.import_playbook": "sub/o.yml"}], "derived"),
    "W5-a-nested-playbooks-first-found-of-its-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost", "vars_files": [["{{ playbook_dir }}/vff.yml"]]}],
        "ansible/playbooks/sub/vff.yml": {"_g": _URL}, "ansible/playbooks/vff.yml": {"_g": "x"}},
        [{"ansible.builtin.import_playbook": "sub/o.yml"}], "derived"),
    "W6-a-nested-playbooks-import-of-its-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"ansible.builtin.import_playbook": "{{ playbook_dir }}/n.yml"}],
        "ansible/playbooks/sub/n.yml": [{"hosts": "localhost", "tasks": [{
            "name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}]}],
        "ansible/playbooks/n.yml": [{"hosts": "localhost", "tasks": [{
            "name": "hi", "ansible.builtin.debug": {"msg": "hi"}}]}]},
        [{"ansible.builtin.import_playbook": "sub/o.yml"}], "unlogged"),
    "W7-a-template-of-an-unspaced-playbook-dir": ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost", "tasks": [{
            "name": "pd", "ansible.builtin.template": {"src": "{{playbook_dir}}/pd.j2", "dest": "/x"}}]}],
        "ansible/playbooks/sub/pd.j2": _URL + "\n"}, [{"ansible.builtin.import_playbook": "sub/o.yml"}], "unlogged"),
    "W8-a-roles-handler-nested-include-under-its-handlers": ([], {"roles": ["r", "q"]}, {
        "ansible/roles/q/tasks/main.yml": [{"name": "go", "ansible.builtin.debug": {"msg": "go"}, "changed_when": True,
                                            "notify": "h"}],
        "ansible/roles/q/handlers/main.yml": [{"name": "h", "ansible.builtin.include_tasks": "s1/a.yml"}],
        "ansible/roles/q/handlers/s1/a.yml": [{"name": "a", "ansible.builtin.include_tasks": "s2/c.yml"}],
        "ansible/roles/q/handlers/s1/s2/c.yml": [{"name": "c", "ansible.builtin.include_tasks": "d.yml"}],
        "ansible/roles/q/handlers/s2/d.yml": [{"name": "say", "ansible.builtin.debug": {"msg": "u=" + _URL}}]},
        None, "unlogged"),
    # fold 13: read 181's rows (fold 12's 6333ba9), a name, a message or a term that looks up a reader, and a pause
    "U1-a-no-log-name-looks-up-a-reader": ([{"name": "A1 " + _LOOKS_UP, "no_log": True,
                                             "ansible.builtin.debug": {"msg": "hi"}}], {}, _READER, None, "named"),
    "U2-a-no-log-asserts-message-looks-up-a-reader": ([{"name": "u2", "no_log": True, "ansible.builtin.assert": {
        "that": "false", "fail_msg": _LOOKS_UP}}], {}, _READER, None, "message"),
    "U2b-a-no-log-fails-message-looks-up-a-reader": ([{"name": "u2b", "no_log": True, "ansible.builtin.fail": {
        "msg": _LOOKS_UP}}], {}, _READER, None, "message"),
    "U3-a-no-log-file-lookup-handed-a-template-lookup": ([{"name": "u3", "no_log": True, "ansible.builtin.debug": {
        "msg": "{{ lookup('file', lookup('template', 'reader.j2')) }}"}}], {}, _READER, None, "message"),
    "U4-a-no-log-with-file-loop-looks-up-a-reader": ([{"name": "u4", "no_log": True,
                                                       "ansible.builtin.debug": {"msg": "hi"}, "with_file": _LOOKS_UP}],
                                                     {}, _READER, None, "message"),
    "P1-a-no-log-pauses-prompt-templates-it": ([{"name": "p1", "no_log": True, "ansible.builtin.pause": {
        "prompt": _URL, "seconds": 1}}], {}, {}, None, "message"),
    # fold 14: reads 190's and 191's rows (fold 13's 72f84b4), a var a task, an include or an add_host hands on that
    # looks up a reader
    "V1a-a-no-log-name-reads-a-task-var-that-looks-up-a-reader": ([{"name": "V1 {{ _r }}", "no_log": True,
                                                                    "vars": {"_r": _LOOKS_UP},
                                                                    "ansible.builtin.debug": {"msg": "hi"}}],
                                                                  {}, _READER, None, "named"),
    "V1b-a-no-log-asserts-message-reads-a-task-var-that-looks-up-a-reader": ([{
        "name": "v1b", "no_log": True, "vars": {"_r": _LOOKS_UP},
        "ansible.builtin.assert": {"that": "false", "fail_msg": "{{ _r }}"}}], {}, _READER, None, "message"),
    "V1e-an-include-var-that-looks-up-a-reader-an-included-name-reads": (
        [{"name": "inc", "no_log": True, "ansible.builtin.include_tasks": "t.yml", "vars": {"_h": _LOOKS_UP}}], {},
        dict(_READER, **{"ansible/playbooks/t.yml": _CHECK_H}), None, "named"),
    "V1h-an-add-host-var-that-looks-up-a-reader-then-printed": (
        [{"name": "v1h", "no_log": True, "ansible.builtin.add_host": {"name": "h2", "_r": _LOOKS_UP}},
         {"name": "say", "ansible.builtin.debug": {"msg": "{{ _r }}"}}], {}, _READER, None, "unlogged"),
}


def _leak_tree(root, shape):
    tasks, play_keys, files, imported, _ = _LEAK_SHAPES[shape] if shape else ([], {}, {}, None, None)
    keys = dict(play_keys)
    play = dict({"name": "render", "hosts": "localhost", "roles": ["r"],
                 "vars": dict({"_dash": "{{ lookup('community.sops.sops', _secrets_dir ~ '/dashboards.sops.yml') }}"},
                              **keys.pop("vars", {})),
                 "tasks": [{"name": "render the env", "no_log": True,
                            "ansible.builtin.copy": {"content": "U=" + _URL, "dest": "/x"}}] + _SOUND + tasks}, **keys)
    files = dict(files, **{"ansible/playbooks/deploy-stack.yml": [play] + (
        [{"ansible.builtin.import_playbook": "other.yml"}] if imported else []),
        "ansible/roles/r/tasks/main.yml": [{"name": "hello", "ansible.builtin.debug": {"msg": "hi"}}]})
    if imported:
        files["ansible/playbooks/other.yml"] = imported
    for rel, doc in files.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        # JSON indented by tabs, which YAML refuses, so a walk that reads a `.json` file as YAML fails here
        text = doc if isinstance(doc, str) else json.dumps(doc, indent="\t") if rel.endswith(".json") else \
            yaml.safe_dump(doc)                         # a string is a template's text
        path.write_text(text, encoding="utf-8")
    return Walk(str(root))


@pytest.mark.parametrize("shape", sorted(_LEAK_SHAPES))
def test_the_url_pin_finds_each_leak_shape(tmp_path, shape):
    """`_url_leaks` over a tree of its own reports nothing for a play that decrypts the dashboards domain once and reads
    it in one no_log task, and reports each shape that prints the URL or decrypts the domain again under the leak it is:
    a no_log reader whose name templates it (its banner prints at any verbosity), an include whose `apply:` carries it
    as an environment, an `ansible.legacy.set_fact` of it read by a debug, a play var or a role default derived from
    such a fact, a role's `vars/main.yaml`, `vars/main/` or `vars_from` file, a `handlers_from` `.yaml` file, an
    `include_vars` `dir:`, a host_vars file in JSON or with no extension, a second decrypt in role defaults, a SOPS vars
    file the vars plugin decrypts, and an imported playbook's debug; and, one per branch of `_url_leaks` no shape
    reached, a k=v set_fact or a register of a reader then read by a debug, a task's `vars:` or an `apply:` var handed
    to an include whose task's name reads it, an include looping over the URL whose task prints `item`, a play's
    `environment`, a task that decrypts the domain again or loads a SOPS file named by a var, and a bool set twice, once
    from the URL, then printed; and, from fold 5's reads, a no_log assert or fail (and its k=v form) whose message
    templates it, a template task that is not no_log whose template reads it, includes a reader, or is a role's found
    before the playbook's, a template lookup in a debug, an inventory group_var, a role beside the playbook, an
    include_vars file found in `vars/` before the bare name (in a role too) and a vars_files entry so, a debug of
    `vars`, of `hostvars[…]` in the k=v form or of `{{ hostvars }}`, a meta dependency's param, and a no_log task
    delegated to it, notifying a name from it or running it in `raw` or a `script`; and, one per rule no shape held, a
    `no_log: "false"`, a `.sops.json` vars file, a `{% %}` statement in a name, a bool set from the URL, printed, then
    reset, a dependency's param or a template that decrypts the domain, and a name that templates it over two lines. The
    sound tree holds two cacheable set_facts, one a reader, and a debug of two facts by a literal key (which prints
    those facts, not every var), and a no_log assert whose `success_msg` reads the URL (a passing no_log task prints
    nothing). Why: each passed the pin on the real tree, the walk or the check not reading the form, while Ansible
    printed the URL (fold 4's reads 90, N6, N7 and 100, N3); the pin had no self-test, so a weakened branch passed too,
    and with one, each branch above could be dropped with every shape still caught (fold 5's own mutants; a `.json` file
    read as YAML passed too, its JSON being YAML, so the tree's JSON is indented by tabs); fold 5's reads found each
    later shape Ansible prints with the pin green (111, S1, S2, N1; 110, S2), and four rules no row held, each with a
    weakening that passed, and a sound tree a `cacheable` read as a fact would fail (110, N3); and fold 6's own mutants
    dropped the `script` arm, a dependency's or a template's decrypt and the multi-line read of a name with every shape
    still caught. And, from fold 6's reads, a debug of `{{ vars }}` in a k=v `msg="…"`, a template task that is not
    no_log whose template templates `vars`, a rescue's debug of `ansible_failed_result`, a no_log task that hands the
    URL to a lookup, a no_log assert's `msg`, a debug of `ansible_facts` (the sound tree's cacheable reader puts the URL
    there) or of a `vars` lookup, an add_host var from it then printed; a template lookup in a play var, a vars file or
    the inventory's group_vars, in a template, in a `with_template`, as a second term or under `ansible.legacy.`, each
    rendering a reader; a template task under `action:`, `local_action:` or `args:`; a reader a lookup's template
    includes from its own directory, or a template includes from the top template's; a meta dependency that renders its
    dependent's template; an include_role from a role whose include_vars file is in the includer's `vars/`; a role's
    nested include found in its `tasks/`; an imported playbook's own role; the inventory file's own vars; an
    include_vars `dir:`'s `extensions:`; a first-found vars_files list; and a k=v `file=` include_vars. Why: each passed
    the pin while Ansible printed the URL or loaded a file that holds it (fold 6's reads 120, S1, S2, S3, N1 and 121,
    S1, S2, S3, N1-N5). And, one per branch fold 7's own mutants dropped with every shape still caught: a template that
    hands the URL to a lookup, a `query` handed it, a lookup term after a parenthesised one, a debug `var` of a `vars`
    lookup, a play's or a task's environment, a `delegate_to` or a dependency's param that looks up a reader, a play var
    or a vars file that looks up a template that decrypts the domain, and an `apply:` environment on a bare
    `include_tasks`; and a debug of a fact by its key in the sound tree, which the mutant that read `ansible_facts`
    whole there passed; and a `success_msg`, which fold 6 read as a failure message though Ansible prints none (read
    120, N1). And, from fold 7's reads, a template under `with_ansible.builtin.template` or
    `with_ansible.legacy.template` (130, S1; 131, S2); a nested playbook's `{{ playbook_dir }}`, a role's include in the
    working directory's `tasks/` (a play's role or an included one), an include under its parent's directory, a role's
    lookup below its `tasks/`, a role handler's include in the playbook's `handlers/` and a nested import in the working
    directory's `tasks/`, each beside a decoy the walk read where Ansible loads the other (130, S2; 131, S6); a no_log
    `with_url` or `with_ansible.builtin.file` loop over the URL (130, S4); a lookup with a space before its parenthesis,
    in a debug, a play var, a no_log lookup handed the URL and a `vars` lookup (131, S2); an inventory subdirectory's
    file (131, S5); a rescue's debug of `ansible_failed_task` after an included no_log reader (131, S1); a debug `var:`
    of `[vars]`, `{{ vars }}`, `_nope | default(vars)` or `[lookup('vars', _w)]`, and a `copy` content or `lineinfile`
    line of `vars` (130, S5); and, one per fold 7 mutant that survived with a row that kills it (130, N3), a lookup with
    a space after its parenthesis, a play var's second lookup term, a `hosts.yaml` inventory file, a `vars` lookup or
    query under `ansible.builtin.`, a bare `add_host` var and a lookup term after a quoted parenthesis; and the sound
    tree's three debugs that print no var (the word in plain text, a fact's attribute of that name, a name that ends
    in it). Why: each passed the pin while Ansible printed the URL (130, S1, S2, S4, S5; 131, S1, S2, S5, S6), or a
    mutant that dropped its branch passed every row (130, N3). And, from fold 8's reads, a template's include or lookup
    behind a quoted comment open, an import entry's `tags:` from the URL, a role's include under `x/tasks/` loaded from
    its `tasks/x/` beside a decoy, a set_fact's raw block or literal that looks up a reader (2.18 renders it again where
    the fact is read), a no_log name of `{{ vars }}` or past a quoted `'}}'`, a no_log assert's or fail's message of
    `vars` or `hostvars` as a string, a debug of `ansible_facts.items()` or of `vars` inside a nested mapping; and,
    from fold 9's own mutants, an import entry's `when:` that looks up a reader, a no_log lookup handed `vars`, a no_log
    `with_url` loop whose list item reads the URL across a line break (the check reads a list as Python prints it, whose
    escaped line break Jinja's lexer refuses, so it reads that text whole), and the sound tree's no_log `vars` lookup of
    a literal name; and, one per fold 8 mutant that survived with a row that kills it, a nested playbook's
    `with_template`, include_vars file and `dir:`, vars_files entry, first-found list and import under its own `{{
    playbook_dir }}` beside the top's decoy, a template under an unspaced `{{playbook_dir}}`, and a role handler's
    include nested under `handlers/`. Why: each passed the pin while Ansible printed the URL on both cores (140, S1, S2,
    S3, N1), or a mutant that dropped its branch passed every row (140, N2). And, from fold 12's reads, a no_log task
    whose name, assert or fail message, or `with_file` loop looks up a reader, a no_log file lookup handed a template
    lookup of one, and a no_log pause whose prompt templates the URL. Why: the pin read a name, a message or a term as
    written, not with the template its lookup renders, and a pause's prompt as no message, so each passed it while
    Ansible printed the URL (a pause's prompt on a tty, on both cores: 181, S1, S6). And, from fold 13's reads, a no_log
    name and a no_log assert's message that read a var their task hands itself, an included task's name that reads a
    var its include hands on, and an add_host var then printed, each var looking up a reader. Why: the pin read a
    handed var's text as written, not with the template its lookup renders, so each passed it while Ansible printed
    the URL on both cores (190, S-2; 191, S-a)."""
    base = _url_leaks(_leak_tree(tmp_path / "base", None))
    assert [base[k] for k in ("unlogged", "named", "message", "outside", "derived")] == [[], [], [], [], []]
    assert base["decrypts"] == ["_dash"]
    leak = _LEAK_SHAPES[shape][-1]
    found = _url_leaks(_leak_tree(tmp_path / "planted", shape))
    assert found[leak] != base[leak], (shape, leak, found[leak])


def test_a_no_log_lookup_of_a_reader_under_a_plain_name_is_no_leak(tmp_path, monkeypatch):
    """A no_log debug whose `msg:` looks up a template that reads the URL, under a name that calls no lookup, is no
    leak: no_log hides the message, and the banner prints the name as it is written. Why: a pin that read a task's
    templates into every name and message, not only into one that calls a lookup, failed this sound task with every
    row green (fold 13's own mutant)."""
    task = {"name": "plain", "no_log": True, "ansible.builtin.debug": {"msg": _LOOKS_UP}}
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([task], {}, _READER, None, "named"))
    found = _url_leaks(_leak_tree(tmp_path, "_row"))
    assert [found[k] for k in ("unlogged", "named", "message", "outside", "derived")] == [[], [], [], [], []]


def _ansible_jinja():
    """Jinja under the Ansible rules these templates use: None renders empty, `bool` reads a string, and `match` and
    `search` are Ansible's tests (re.match, anchored at the start; re.search, anywhere)."""
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(finalize=lambda v: "" if v is None else v)
    env.filters["bool"] = lambda v: str(v).strip().lower() in ("true", "yes", "on", "1")
    env.tests["match"] = lambda v, pattern: re.match(pattern, v) is not None
    env.tests["search"] = lambda v, pattern: re.search(pattern, v) is not None
    return env


def _shape_ok(env, dash):
    (shape,) = [t for t in _deploy_tasks() if "_alert_url_ok" in (t.get("ansible.builtin.set_fact") or {})]
    u = env.from_string(shape["vars"]["_u"]).render(_dash=dash)
    return env.from_string(shape["ansible.builtin.set_fact"]["_alert_url_ok"]).render(
        _u=u, _refused=shape["vars"]["_refused"]) == "True"


@pytest.mark.parametrize("dash, blank", [({}, True), ({"alert_notifier_url": ""}, True),
                                         ({"alert_notifier_url": None}, True),
                                         ({"alert_notifier_url": "http://192.0.2.10/alerts"}, False)],
                         ids=["absent", "empty", "null", "set"])
def test_the_deploy_warns_exactly_when_grafana_gets_no_url(dash, blank):
    """deploy-stack's shape check, its `.env` line, its `_has_alert_url` fact and its warning's `when`, rendered with
    Jinja under Ansible's own rules, agree for each shape the dashboards domain can hold: an absent, empty or null
    URL passes the check, renders Grafana's line empty and the warning fires; a set URL renders and the warning is
    quiet; no stack without grafana is warned. Guards the warning and the render disagreeing — a null URL
    (`alert_notifier_url:` with no value) once read as set (`None | string` is `"None"`), so Grafana posted to the
    closed port and the deploy said nothing — and a warning whose condition was inverted. A blank URL is refused by
    the shape check before any of this (the next test)."""
    env = _ansible_jinja()
    assert _shape_ok(env, dash)
    tasks = _deploy_tasks()
    (render,) = [t for t in tasks if "KONTROLL_ALERT_URL=" in str(t.get("ansible.builtin.copy", {}).get("content"))]
    (line,) = [ln.strip() for ln in render["ansible.builtin.copy"]["content"].splitlines()
               if ln.strip().startswith("KONTROLL_ALERT_URL=")]
    quoted = env.from_string(line).render(_dash=dash).split("=", 1)[1]
    assert quoted[0] == quoted[-1] == "'" and len(quoted) >= 2, quoted
    url = quoted[1:-1]
    (decide,) = [t for t in tasks if "_has_alert_url" in (t.get("ansible.builtin.set_fact") or {})]
    has = env.from_string(decide["ansible.builtin.set_fact"]["_has_alert_url"]).render(_dash=dash)
    (say,) = [t for t in tasks if "ansible.builtin.debug" in t and "_has_alert_url" in str(t.get("when", ""))]
    when = env.from_string("{{ %s }}" % say["when"])
    assert (url == "", when.render(stack_services=["grafana"], _has_alert_url=has)) == (blank, str(blank)), (url, has)
    assert url == (dash.get("alert_notifier_url") or "")
    assert when.render(stack_services=["api"], _has_alert_url=has) == "False"


# Each one the dialog's shape refuses (a `'`, a character that stops the redaction, `$`/`#`, a non-printable, or no
# http(s) URL); then two it must take.
_REFUSED_URLS = ["  ", "https://192.0.2.10/a b", "https://192.0.2.10/a$b", "https://192.0.2.10/a${b}",
                 "https://192.0.2.10/#x", 'https://192.0.2.10/"x', "https://192.0.2.10/'x",
                 "https://192.0.2.10/a\\b", "https://192.0.2.10/x\nKONTROLL_X=1", "https://192.0.2.10/x\t",
                 "https://192.0.2.10/x\x00", "https://192.0.2.10/x\x1b", "https://192.0.2.10/x\u00a0",
                 "https://192.0.2.10/x\u2028", " https://192.0.2.10/x", "ftp://192.0.2.10/x", "https://",
                 "192.0.2.10/x", "<https://192.0.2.10/x>", "xhttps://192.0.2.10/"]
_TAKEN_URLS = ["https://ntfy.example/kontroll-x7q", "http://192.0.2.10:8080/hook?a=1&tok=x%20y"]


@pytest.mark.parametrize("url", _REFUSED_URLS + _TAKEN_URLS)
def test_the_deploy_refuses_exactly_the_urls_the_secrets_dialog_refuses(url):
    """deploy-stack's shape check (`_alert_url_ok`), rendered under Ansible's rules, takes a URL exactly when the
    Secrets dialog's `http_url` shape does: a URL set with sops by hand meets the same rule at the deploy that the
    dialog holds at entry. Guards a hand-set URL reaching docker/.env that a `'` would let out of its single quotes
    (compose prints the line it cannot parse in the bring-up, which is not no_log), that a space, a quote, a
    backslash or a character Go escapes would cut Vector's redaction of Grafana's failed-delivery line short, or
    that a `$` or a `#` would change were the line unquoted; and the two checks drifting apart (T32 read B, S1).
    Measured on both ansible cores, not only this Jinja. Two refused URLs carry text before the scheme, which only
    the anchoring refuses: a `search` in place of the deploy's `match` passed every other row (T32 fold 1 read A,
    N5)."""
    from kontroll.service import secrets as secrets_service
    takes = secrets_service._SHAPES["http_url"][0]
    assert _shape_ok(_ansible_jinja(), {"alert_notifier_url": url}) is takes(url) is (url in _TAKEN_URLS)


def test_the_down_rules_fire_only_for_a_target_that_answered_in_their_window():
    """Each metrics rule reads `(<m> == bool 0) and on (job, instance) (max_over_time(<m>[<w>]) == 1)` for one
    metric, with one window for both rules (7 days), an instant query, `for: 10m`, `noDataState: OK` and
    `execErrState: Error`, and a `description` saying that a RESOLVED after the window is the window passing. Guards a
    rule that pages for a target that never answered (the shipped tree's TEST-NET placeholders, a class enabled before
    its device) or forever for a decommissioned one, a rule that reads NoData as an alert, a Prometheus outage hidden
    as Normal, the "stopped for 10 minutes" the docs promise (T32 read A, S4), and a RESOLVED a week after a device
    died read as "it is back" (read A, S3)."""
    windows = set()
    for rule in _rules("metrics-rules.yaml"):
        model = _query(rule)["model"]
        m = _DOWN.match(model["expr"])
        assert m and m.group(1) == m.group(2), model["expr"]
        windows.add(m.group(3))
        assert model.get("instant") is True and rule["noDataState"] == "OK" and rule["execErrState"] == "Error"
        assert rule["for"] == "10m", rule["uid"]
        assert "RESOLVED" in rule["annotations"]["description"] and "7-day window" in rule["annotations"]["description"]
    assert windows == {"7d"}, windows


def test_a_probed_target_is_watched_by_its_probe_not_its_scrape():
    """Every Prometheus job that scrapes through the blackbox exporter (`metrics_path: /probe`) has a metrics rule on
    `probe_success`, and `up` has one too. Guards the blackbox targets' silent gap: their scrape target is the
    exporter, so their `up` stays 1 while the device behind it is dead."""
    metrics = {_DOWN.match(_query(r)["model"]["expr"]).group(1) for r in _rules("metrics-rules.yaml")}
    assert "up" in metrics
    probes = [j["job_name"] for f in sorted(glob.glob(os.path.join(_ROOT, "prometheus", "jobs.d", "*.yml")))
              for j in (_load(os.path.relpath(f, _ROOT)) or {}).get("scrape_configs", [])
              if j.get("metrics_path") == "/probe"]
    assert probes and "probe_success" in metrics, probes


def test_every_rule_pins_a_provisioned_datasource_a_threshold_a_debounce_and_a_bounded_window():
    """Every metrics and heartbeat rule queries a datasource by a uid the provisioning defines, decides on an
    `__expr__` threshold node, and carries a `for:` and a bounded `relativeTimeRange`. Guards a wrong uid (the rule
    never evaluates), an alert without a debounce (flapping), and an unbounded lookback."""
    uids = {d["uid"] for f in glob.glob(os.path.join(_ROOT, "dashboards", "grafana", "provisioning", "datasources",
                                                     "*.yml"))
            for d in _load(os.path.relpath(f, _ROOT))["datasources"]}
    rules = _rules("metrics-rules.yaml") + _rules("heartbeat-rule.yaml")
    assert len(rules) >= 3
    for rule in rules:
        assert _query(rule)["datasourceUid"] in uids, rule["uid"]
        nodes = {d["refId"]: d for d in rule["data"]}
        cond = nodes[rule["condition"]]
        assert cond["datasourceUid"] == "__expr__" and cond["model"]["type"] == "threshold", rule["uid"]
        assert rule.get("for"), rule["uid"]
        assert all(d["relativeTimeRange"]["from"] > 0 for d in rule["data"]), rule["uid"]


def test_the_heartbeat_always_fires_on_its_own_route():
    """The heartbeat rule's query is `vector(1)` over a `gt 0` threshold, it alerts on NoData too, and its label is
    the matcher of a route that sends at once and re-sends daily; it fires a minute after it starts and shows a failed
    query as Error; no other rule carries that label. Guards a heartbeat that can stop by itself (then its absence
    proves nothing), one buried in the 12h grouping of real alerts, a real alert that rides the heartbeat's daily-only
    route, and the two states the docs promise: Prometheus down as an Error alert, and a first heartbeat within
    minutes of a deploy (T32 read A, S4)."""
    (beat,) = _rules("heartbeat-rule.yaml")
    assert _query(beat)["model"]["expr"] == "vector(1)" and beat["noDataState"] == "Alerting"
    assert beat["for"] == "1m" and beat["execErrState"] == "Error"
    cond = {d["refId"]: d for d in beat["data"]}[beat["condition"]]
    assert cond["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}
    assert beat["labels"]["kontroll"] == "heartbeat"
    (tree,) = _load("dashboards", "grafana", "provisioning", "alerting", "policies.yaml")["policies"]
    (route,) = [r for r in tree["routes"] if r.get("object_matchers") == [["kontroll", "=", "heartbeat"]]]
    assert route["group_wait"] == "0s" and route["repeat_interval"] == "24h"
    others = [r["uid"] for r in _all_rules() if r["uid"] != beat["uid"]
              and r.get("labels", {}).get("kontroll") == "heartbeat"]
    assert others == []


def test_no_string_in_the_alerting_files_trips_the_crash_rule():
    """No key or string in any file under provisioning/alerting/ (a hidden or a JSON one included) says what the
    crash rule's line filters count (read from all of the rule's stages: `restart`, `panic` or `exited` today), but
    the crash rule's own uid and query; and every rule's uid is unique. Why: the crash-storm rule counts container
    lines with those words and drops only its own uid (test_alerting_logging.py). Grafana logs each rule's uid at
    every evaluation, so a uid so named would count itself into a crash storm every minute; and a failed delivery
    logs, at every retry, the alert's group key (its title and folder, and the matchers of the policy route it
    took), any label that route groups by, and the contact point's and receiver's names, so a title so named (the
    crash rule's was, "Container restart / crash storm") keeps the crash rule firing on a box whose deliveries fail,
    the default (T32's reads). A label, an annotation or a notification template a line may quote is held as
    cheaply. Holding named fields let a folder, a rule file named otherwise and a word added to the filter through
    (fold 1 read A, S1); reading only the first `|~` stage, a double-quoted stage's escapes raw, the visible files
    and named fields let an `or` stage, a `\\b` word boundary, a hidden rule file, a policy route's matcher and a
    receiver's uid through (fold 2 read A, S1: G1 to G5); a stage the reader cannot read now fails here."""
    rules = _all_rules()
    words = _crash_words(rules)
    assert {"logging-rules.yaml", "metrics-rules.yaml", "heartbeat-rule.yaml"} <= {
        name for name, doc in _alerting_docs() if (doc or {}).get("groups")}
    assert all(any(w.search(s) for w in words) for s in ("restart", "panic", "exited")), [w.pattern for w in words]
    uids = [r["uid"] for r in rules]
    assert len(uids) == len(set(uids)), uids
    said = _said(_alerting_docs(), words)
    assert said == [], said


def _said(docs, words):
    """(file, path, text) for each key or string in `docs` that one of `words` matches, but the crash rule's uid and
    its `data`."""
    said = []
    for name, doc in docs:
        own = {("groups", g, "rules", r) for g, group in enumerate((doc or {}).get("groups") or [])
               for r, rule in enumerate(group.get("rules") or []) if rule.get("uid") == _CRASH_UID}
        said += [(name, at, text) for at, text in _paths(doc)
                 if not (at[:4] in own and at[4:5] in (("uid",), ("data",))) and any(w.search(text) for w in words)]
    return said


def test_the_crash_word_hold_reads_every_file_and_exempts_only_the_crash_rules_uid_and_query(tmp_path):
    """The hold's two halves on files of their own: `_alerting_docs` reads a hidden file and a tab-indented JSON
    file, and refuses an entry Grafana would refuse; `_said` exempts the crash rule's uid and `data` and nothing
    else (not its title or annotations, not another rule's `data`, not a word in a nested JSON string); and
    `_crash_words` reads a `|=` stage's text and fails on a crash rule with a second query. Why: the hold passes the
    shipped tree whether or not each of these holds, so without this test a reader that skipped hidden files, read
    JSON as YAML, exempted the whole crash rule or every rule's query, dropped `|=` stages or read only the first
    query would go unnoticed (T32 fold 3 read A, N3)."""
    words = [re.compile("(?i)restart")]
    (tmp_path / ".hidden.yaml").write_text("apiVersion: 1\ngroups: [{name: g, rules: [{uid: u1, title: Restart}]}]\n")
    (tmp_path / "tab.json").write_text('{\n\t"templates": [{"name": "n", "template": "a restart"}]\n}\n')
    crash = {"uid": _CRASH_UID, "title": "Restart", "annotations": {"summary": "restart"},
             "data": [{"model": {"expr": "restart"}}]}
    other = {"uid": "u2", "title": "t", "data": [{"model": {"expr": "restart"}}]}
    (tmp_path / "rules.yml").write_text(yaml.safe_dump({"groups": [{"name": "g", "rules": [crash, other]}]}))
    docs = _alerting_docs(str(tmp_path))
    assert [name for name, _ in docs] == [".hidden.yaml", "rules.yml", "tab.json"]
    assert sorted((name, at[-1]) for name, at, _ in _said(docs, words)) == [
        (".hidden.yaml", "title"), ("rules.yml", "expr"), ("rules.yml", "summary"), ("rules.yml", "title"),
        ("tab.json", "template")]
    for bad in ("x.YAML", "x.txt"):
        (tmp_path / bad).write_text("{}\n")
        with pytest.raises(AssertionError):
            _alerting_docs(str(tmp_path))
        (tmp_path / bad).unlink()
    query = {"datasourceUid": "loki", "model": {"expr": '{a="b"} | line_format "{{.message}}" |= "x" [5m]))'}}
    assert [w.pattern for w in _crash_words([dict(crash, data=[query])])] == ["x"]
    with pytest.raises(ValueError):
        _crash_words([dict(crash, data=[query, query])])


_PROMTOOL_CASES = """rule_files: [rules.yml]
evaluation_interval: 1h
tests:
  - interval: 1h
    input_series:
      - {series: 'up{job="node",instance="went-down"}', values: '1x2 0x200'}
      - {series: 'up{job="node",instance="never-up"}', values: '0x202'}
      - {series: 'up{job="node",instance="steady"}', values: '1x202'}
      - {series: 'probe_success{job="blackbox",instance="went-down"}', values: '1x2 0x200'}
      - {series: 'probe_success{job="blackbox",instance="never-up"}', values: '0x202'}
    alert_rule_test:
      - {eval_time: 10h, alertname: up, exp_alerts: [{exp_labels: {job: node, instance: went-down}}]}
      - {eval_time: 10h, alertname: probe_success, exp_alerts: [{exp_labels: {job: blackbox, instance: went-down}}]}
      - {eval_time: 190h, alertname: up, exp_alerts: []}
      - {eval_time: 190h, alertname: probe_success, exp_alerts: []}
"""


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed (the validate image has it)")
def test_the_down_expressions_behave_under_promtool(tmp_path):
    """The two down expressions, each wrapped in the rule's own `> 0` threshold, run through `promtool test rules`:
    a target up for three hours then down fires ten hours in and is quiet after the 7-day window passes; one never up
    and one steadily up never fire. Guards the expressions themselves — a `bool` dropped (an up target would read as
    no series, and NoData as OK hides nothing), a window that never lets a dead target go, or a join that matches no
    labels — which a shape pin cannot see."""
    groups = [{"alert": m.group(1), "expr": "(%s) > 0" % _query(r)["model"]["expr"]}
              for r in _rules("metrics-rules.yaml") for m in [_DOWN.match(_query(r)["model"]["expr"])]]
    (tmp_path / "rules.yml").write_text(yaml.safe_dump({"groups": [{"name": "t32", "rules": groups}]}),
                                        encoding="utf-8")
    (tmp_path / "cases.yml").write_text(_PROMTOOL_CASES, encoding="utf-8")
    run = subprocess.run(["promtool", "test", "rules", "cases.yml"], cwd=str(tmp_path), capture_output=True,
                         text=True)
    assert run.returncode == 0, run.stdout + run.stderr


# fold 10: read 150's rows for the URL pin (fold 9's 8631cb3)
_BS = chr(92)    # a backslash, kept out of the source's own escapes
_LEAK_KEYS = ("unlogged", "named", "message", "outside", "derived", "decrypts")


def _pin(tmp_path, monkeypatch, tasks, files=None):
    """The URL pin's leak lists for the leak-shape test's base tree, and for that tree with `tasks` added as a leak
    shape's, beside `files`."""
    monkeypatch.setitem(_LEAK_SHAPES, "_row", (tasks, {}, dict(files or {}), None, "unlogged"))
    return _url_leaks(_leak_tree(tmp_path / "base", None)), _url_leaks(_leak_tree(tmp_path / "shape", "_row"))


@pytest.mark.parametrize("leak,tasks", [
    ("message", [{"name": "show", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
                  "with_file": "{{ vars }}"}]),
    ("message", [{"name": "show", "no_log": True, "ansible.builtin.debug": {"msg": "hi"},
                  "with_file": "{{ lookup('vars', '_d' ~ 'ash') }}"}]),
    ("message", [{"name": "show", "no_log": True, "ansible.builtin.debug": {
        "msg": "{{ lookup('file', lookup('vars', '_d' ~ 'ash')) }}"}}]),
    ("unlogged", [{"name": "show", "ansible.builtin.debug": {"msg": "{{ ansible_facts[k] }}"}}]),
], ids=["a-no-log-with-file-loop-of-every-var", "a-no-log-with-file-loop-of-a-built-vars-lookup",
        "a-no-log-file-lookup-handed-a-vars-lookup", "a-debug-of-a-fact-by-a-var-key"])
def test_the_pin_reads_every_var_in_a_loop_a_handed_lookup_and_a_keyed_fact(tmp_path, monkeypatch, leak, tasks):
    """A no_log task's `with_file` loop over every var or over a vars lookup of a built name (2.19 prints the failing
    term under no_log: probe12 p18b), and a no_log file lookup handed a vars lookup of a built name (p19), are
    messages; a debug of `ansible_facts[k]`, a cacheable fact read by a var's key, prints every var (p20). Why: a
    mutant that read a `with_` loop for a tainted name alone, one that stripped every quoted literal from a lookup's
    terms (the vars lookup's name with them), and one that exempted any `ansible_facts[` read, each passed every row
    (fold 9's read 150, N-E: m19)."""
    base, found = _pin(tmp_path, monkeypatch, tasks)
    assert found[leak] != base[leak], (leak, found[leak])


def test_a_jinja_comment_naming_every_var_is_not_a_read(tmp_path, monkeypatch):
    """A debug whose message holds `{# vars #}`, a comment Jinja never runs, prints no var. Why: a mutant whose
    `_spans` read comments too flagged it (stricter only; fold 9's read 150, N-E: m19)."""
    base, found = _pin(tmp_path, monkeypatch, [{"name": "show", "ansible.builtin.debug": {"msg": "{# vars #}hi"}}])
    assert {k: found[k] for k in _LEAK_KEYS} == {k: base[k] for k in _LEAK_KEYS}


@pytest.mark.parametrize("value", ["{{ vars }}", "{{ lookup('vars', '_d' ~ 'ash') }}"])
def test_a_fact_set_from_every_var_is_tainted(tmp_path, monkeypatch, value):
    """A no_log set_fact of every var (`vars`, or a vars lookup of a built name), then a debug of the fact, is a leak:
    both cores print the dashboards domain, the URL in it (probe12 p5, p5b). Why: the pin tainted only the names a
    task reading a tainted name set, so the fact held the URL untainted and the debug passed (fold 9's read 150, S-D,
    which falsified SECURITY.md R-ALERT-1)."""
    base, found = _pin(tmp_path, monkeypatch, [
        {"name": "keep", "no_log": True, "ansible.builtin.set_fact": {"g": value}},
        {"name": "show", "ansible.builtin.debug": {"var": "g"}}])
    assert "show" in found["unlogged"] and "show" not in base["unlogged"], found["unlogged"]


_LIT = "{{ lookup(\"template\", \"reader.j2\") }}" + _BS + "x7b" + _BS + "x7b"


@pytest.mark.parametrize("tasks", [
    [{"name": "show", "ansible.builtin.debug": {"var": "lookup('template', 'reader.j2')"}}],
    [{"name": "keep", "ansible.builtin.set_fact": {"f": "{{ '" + _LIT + "' }}"}},
     {"name": "show", "ansible.builtin.debug": {"msg": "{{ f }}"}}],
    [{"name": "show", "ansible.builtin.debug": {"msg": "{{ lookup('ansible.builtin.file', '" + _LIT + "') }}"}}],
    [{"name": "show", "ansible.builtin.debug": {"msg": "hi"}, "when": "true, lookup('template', 'reader.j2')"}],
], ids=["a-debug-var-that-is-one-lookup", "a-literal-fact-with-backslash-escapes",
        "a-lookup-term-literal-with-backslash-escapes", "a-tuple-condition"])
def test_the_pin_reads_a_lookup_ansible_runs_where_the_walk_read_none(tmp_path, monkeypatch, tasks):
    """An unlogged debug whose `var:` is one template lookup of a template that reads the URL, a set_fact's literal or
    a file lookup's term holding that lookup and then an escaped brace (the backslash kept, as Ansible keeps it), and
    a tuple condition, are leaks: each renders the template on a core and prints the URL (probe12 p21, p1b, p1c, p4).
    Why: the walk read no lookup at an expression's top, decoded the escape so the literal did not parse and was
    skipped, and parsed a condition as one expression only, so each passed the pin (fold 9's read 150, S-E, S-B,
    S-A)."""
    base, found = _pin(tmp_path, monkeypatch, tasks, _READER)
    assert found["unlogged"] != base["unlogged"], found["unlogged"]


@pytest.mark.parametrize("module,key", [
    ("copy", "dest"), ("template", "dest"), ("ansible.builtin.blockinfile", "path"),
    ("ansible.builtin.lineinfile", "path"), ("lineinfile", "dest"), ("lineinfile", "destfile"), ("lineinfile", "name"),
    ("ansible.builtin.replace", "path"), ("replace", "name"), ("community.general.ini_file", "path"),
    ("community.general.ini_file", "dest"), ("ansible.builtin.assemble", "dest"),
])
def test_a_writer_of_the_stacks_env_is_found_under_each_module_and_name(module, key):
    """`_env_writers` finds a task that writes `{{ stack_dir }}/.env` under each content-writing module, prefixed or
    not, and each name the module takes for its file. Why: the pin read five modules' `dest` or `path` alone, so a
    lineinfile under `name:` or `destfile:`, a replace under `name:` and a community.general.ini_file wrote the file,
    not no_log, with every row green, and `--check --diff` prints it (fold 9's read 150, N-C: X19)."""
    task = {"name": "w", module: {key: "{{ stack_dir }}/.env", "line": "x"}}
    assert _env_writers([task, {"name": "other", "ansible.builtin.copy": {"dest": "/etc/x", "content": "y"}}]) == [task]


@pytest.mark.parametrize("task", [
    {"name": "w", "action": "community.general.ini_file path={{ stack_dir }}/.env section=s option=o value=v"},
    {"name": "w", "action": {"module": "community.general.ini_file", "path": "{{ stack_dir }}/.env", "section": "s"}},
    {"name": "w", "local_action": "community.general.ini_file path={{ stack_dir }}/.env section=s"},
    {"name": "w", "local_action": {"module": "community.general.ini_file", "dest": "{{ stack_dir }}/.env"}},
    {"name": "w", "action": {"module": "community.general.ini_file path={{ stack_dir }}/.env section=s"}},
], ids=["action-string", "action-mapping", "local-action-string", "local-action-mapping", "action-mapping-words"])
def test_a_prefixed_writer_of_the_stacks_env_is_found_under_action_and_local_action(task):
    """`_env_writers` finds a task that writes `{{ stack_dir }}/.env` through another collection's module named by
    `action:` or `local_action:`, as a string's first word or a mapping's `module:`, its arguments in it too. Why: the
    pin took a prefixed writer's name from the task's keys alone, so `action: "community.general.ini_file path=…/.env
    …"` wrote the file, not no_log, with every row green, and `--check --diff` prints it (fold 10's read 161, N3); and
    it took a mapping's whole `module:` for the name, though Ansible runs the module its first word names (fold 13's
    read 191, S-b)."""
    assert _env_writers([task, {"name": "other", "ansible.builtin.copy": {"dest": "/etc/x", "content": "y"}}]) == [task]


@pytest.mark.parametrize("dest", ["{{ stack_dir ~ '/.env' }}", "{{ stack_dir }}/./.env"], ids=["joined", "dotted"])
def test_a_writer_of_a_env_the_pin_cannot_place_fails_it(dest):
    """A writer whose file names a `.env` in another spelling than `{{ stack_dir }}/.env` fails the pin rather than
    pass unread: it may be the stack's file. Why: a copy to either spelling wrote the file, not no_log, with every
    row green (fold 9's read 150, N-C: X19)."""
    with pytest.raises(AssertionError):
        _env_writers([{"name": "w", "ansible.builtin.copy": {"dest": dest, "content": "y"}}])


@pytest.mark.parametrize("task", [
    {"name": "w", "ansible.builtin.copy": {"src": "files/x", "dest": "{{ stack_dir }}/"}},
    {"name": "w", "ansible.builtin.copy": {"src": "files/x", "dest": "{{ stack_dir }}"}},
    {"name": "w", "ansible.builtin.copy": {"src": "files/x", "dest": "{{ stack_dir }}/."}},
    {"name": "w", "ansible.builtin.copy": {"src": "files/x", "dest": "{{ stack_dir }}/sub/.."}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/.e{{ 'nv' }}", "content": "y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/.e{{ 'n' }}v", "content": "y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/{{ item }}", "content": "y"}, "loop": [".env"]},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ _f }}", "content": "y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/.e{% if true %}nv{% endif %}", "content": "y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/.e{# x #}nv", "content": "y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/{{ item }}.env", "content": "y"}, "loop": [""]},
    {"name": "w", "ansible.builtin.copy": {"src": "files/.env", "dest": "/etc/x"}},
    {"name": "w", "ansible.builtin.assemble": {"src": "{{ stack_dir }}", "dest": "/etc/x"}},
    {"name": "w", "ansible.builtin.lineinfile": {"line": "X=y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "/etc/x", "content": "y"}, "args": "{{ _env_args }}"},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/{{ x }", "content": "y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/{{ x }}.", "content": "y"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/{{ sub }}/{{ item }}", "content": "y"},
     "loop": ["a"]},
    {"name": "w", "ansible.builtin.lineinfile": {"path": "{{ stack_dir }}/{% filter lower %}.ENV{% endfilter %}",
                                                 "line": "X=1"}},
    {"name": "w", "ansible.builtin.lineinfile": {"path": "{{ stack_dir }}/.e{% if true %}\nnv{% endif %}",
                                                 "line": "X=1"}},
    {"name": "w", "ansible.builtin.lineinfile": {"path": "{{ stack_dir }}/.e{# c #}\nnv", "line": "X=1"}},
    {"name": "w", "ansible.builtin.lineinfile": {"path": "{{ stack_dir }}/vector{{ '/../.e' }}nv", "line": "X=1"}},
    {"name": "w", "ansible.builtin.copy": {"src": "files/x", "dest": "{{ stack_dir }}/{{ sub }}/"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/{{ a }}x{{ b }}", "content": "y"}},
    {"name": "w", "ansible.builtin.lineinfile": {"path": "{{ stack_dir }}{% raw %}/.e{% endraw %}nv", "line": "X=1"}},
    {"name": "w", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/vector{% raw %}/../.e{% endraw %}nv",
                                           "content": "y"}},
], ids=["into-the-directory", "into-the-directory-bare", "into-the-directory-dot", "into-a-parent", "templated-name",
        "a-name-split-by-a-block", "looped-name", "a-bare-variable", "a-name-split-by-a-statement",
        "a-name-split-by-a-comment", "a-looped-prefix-of-env", "a-env-src", "a-directory-src", "no-file",
        "args-not-a-mapping", "a-name-the-lexer-refuses", "a-block-then-a-dot", "a-loop-under-a-templated-directory",
        "a-filter-block", "a-statements-trimmed-line-break", "a-comments-line-break", "a-block-that-writes-a-slash",
        "into-a-directory-a-block-names", "text-between-two-blocks", "a-raw-block-holding-the-last-slash",
        "a-raw-block-through-a-parent"])
def test_a_writer_whose_file_the_pin_cannot_place_fails_it(task):
    """A writer whose file, or src, may name a `.env` or the directory (a copy into `{{ stack_dir }}/`, `{{ stack_dir
    }}`, `{{ stack_dir }}/.` or `{{ stack_dir }}/sub/..`, which takes its src's name; a dest of `{{ stack_dir }}/.e{{
    'nv' }}` or `{{ stack_dir }}/.e{{ 'n' }}v`, whose name a block splits, a `{% %}` statement or a `{# #}` comment too,
    of `{{ stack_dir }}/{{ item }}` or `{{ stack_dir }}/{{ item }}.env` in a loop, `{{ stack_dir }}/{{ x }}.` (`.` or
    `..` where `x` is empty or a dot), `{{ stack_dir }}/{{ sub }}/{{ item }}` (the stack's own `.env` where `sub` is
    `.`), a dest the lexer refuses, or a bare variable; an assemble of the stack's directory), whose src names a `.env`,
    that has no file (a play's `module_defaults` may hand it one), or whose `args:` is not a mapping, fails the pin: it
    may write or read the stack's `.env`, and `--check --diff` prints the file. Why: each of the copies into the
    directory, the templated name and the looped one printed the stack's old URL under `--check --diff` on both cores
    with every row green (fold 11's reads 171, S2 and 170, S-5); and a name a statement or a comment splits passed a
    placing that split on the expression's close alone (fold 12's reads 181, N2 and 180, N-D); and a dest a block then a
    dot ends, one under a templated directory and one the lexer refuses each passed a mutant of the placing (fold 13's
    own mutants); and a name a filter block writes, one a statement's or a comment's trimmed line break splits, and
    one past a block that writes a `/`, each printed the stack's file under `--check --diff` on both cores with every
    row green, as the placing kept the text before a block (fold 13's read 191, S-d); and a copy into a directory a
    block names (`{{ stack_dir }}/{{ sub }}/`, the stack's own where `sub` is `.`) and a name whose last block
    follows text another block follows (`{{ stack_dir }}/{{ a }}x{{ b }}`) are fold 14's own rows, against a
    placing cut at the first written `/` or read from the first block; and a name whose last `/` a raw block holds
    (`{{ stack_dir }}{% raw %}/.e{% endraw %}nv`) printed the stack's file under `--check --diff` on both cores with
    every row green, and the same through `vector/..` passed the pin, as the placing read the raw block's text as
    written (fold 14's read 200, S-C; fold 15's read 211, N-7)."""
    with pytest.raises(AssertionError):
        _env_writers([task])


def test_a_writer_of_a_name_written_as_it_is_under_a_templated_directory_is_placed():
    """A writer whose file is a name written as it is under a templated directory (`{{ kontroll_vector_dir
    }}/file_tail_ssh.key`, `{{ stack_dir }}/.env.homepage`), or a literal path, or whose src is a bare name, passes
    `_env_writers` and is no writer of the stack's `.env`; so does one whose name is a block before a written suffix
    (`/etc/x/{{ item }}.conf`, `{{ vdir }}/tail_{{ h }}.key`), which names no `.env`. Why: the shipped deploy's
    writers are such, and a pin that refused a templated directory or a src without a `/` would fail the shipped
    tree (fold 12's own rows); and per-item writers of the second kind were refused, though sound (fold 12's reads
    181, N2 and 180, N-D)."""
    tasks = [{"name": "k", "ansible.builtin.copy": {"dest": "{{ kontroll_vector_dir }}/file_tail_ssh.key",
                                                    "content": "k"}},
             {"name": "h", "ansible.builtin.copy": {"dest": "{{ stack_dir }}/.env.homepage", "content": "h"}},
             {"name": "u", "ansible.builtin.template": {"src": "unit.service.j2", "dest": "/etc/systemd/system/u"}},
             {"name": "c", "ansible.builtin.copy": {"dest": "/etc/x/{{ item }}.conf", "content": "c"}, "loop": ["a"]},
             {"name": "t", "ansible.builtin.copy": {"dest": "{{ vdir }}/tail_{{ h }}.key", "content": "t"}}]
    assert _env_writers(tasks) == []


@pytest.mark.parametrize("task", [
    {"name": "r", "ansible.builtin.debug": {"msg": "{{ lookup('file', stack_dir ~ '/.env') }}"}},
    {"name": "r", "ansible.builtin.debug": {"var": "lookup('file', stack_dir ~ '/.env')"}},
    {"name": "r", "no_log": True, "ansible.builtin.slurp": {"src": "{{ stack_dir }}/.env"}, "register": "_e"},
    {"name": "r", "ansible.builtin.command": {"cmd": "cat {{ stack_dir }}/.env"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} --env-file {{ stack_dir }}/.env config"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} --env-file {{ stack_dir }}/.env ps"
               " {{ stack_dir }}/.env"}},
    {"name": "r", "ansible.builtin.assert": {"that": "true", "fail_msg": "{{ lookup('file', stack_dir ~ '/.env') }}"}},
    {"name": "r {{ lookup('file', stack_dir ~ '/.env') }}", "ansible.builtin.debug": {"msg": "x"}},
    {"name": "r", "ansible.builtin.shell": {"cmd": "sh -c 'cat \"$5\"' docker compose --project-directory"
                                                " {{ stack_dir }} --env-file {{ stack_dir }}/.env up -d"}},
    {"name": "r", "ansible.builtin.shell": {
        "cmd": "docker compose --project-directory {{ stack_dir }} --env-file {{ stack_dir }}/.env up -d"
               " && docker run --rm --env-file {{ stack_dir }}/.env alpine env"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose --project-directory {{ stack_dir }} config"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} --file {{ stack_dir }}/compose.yaml config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker-compose --project-directory {{ stack_dir }} convert"}},
    {"name": "r", "ansible.builtin.command": {
        "argv": ["docker", "compose", "--project-directory", "{{ stack_dir }}", "exec", "grafana", "env"]}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose --project-directory {{ stack_dir }} {{ _verb }}"}},
    {"name": "r", "ansible.builtin.shell": {
        "cmd": "docker compose --project-directory {{ stack_dir }} ps -q | xargs docker inspect"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker container inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.shell": "docker exec kontroll-grafana-1 env"},
    {"name": "r", "community.docker.docker_container_info": {"name": "kontroll-grafana-1"}, "register": "_c"},
    {"name": "r", "action": "community.docker.docker_compose_v2_exec project_src={{ stack_dir }} service=grafana"},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} run --rm grafana env"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} {% if _show %}config{% endif %}"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker {{ _verb }} kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker --debug inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker -H unix:///run/docker.sock inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose -f /opt/stack/compose.yaml config"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} --env-file {{ stack_dir }}/.env pull"}},
    {"name": "r", "ansible.builtin.shell": "docker cp kontroll-grafana-1:/proc/1/environ -"},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} cp grafana:/proc/1/environ /tmp/e"}},
    {"name": "r", "community.docker.docker_container_exec": {"container": "kontroll-grafana-1", "command": "env"}},
    {"name": "r", "community.docker.docker_compose_v2_run": {"project_src": "{{ stack_dir }}", "service": "grafana",
                                                            "command": "env"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "/usr/bin/docker compose --project-directory {{ stack_dir }} config"}},
    {"name": "r", "ansible.builtin.command": {
        "argv": ["/usr/bin/docker", "compose", "--project-directory", "{{ stack_dir }}", "config"]}},
    {"name": "r", "ansible.builtin.shell": {
        "cmd": "sh -c \"docker compose --project-directory {{ stack_dir }} config\""}},
    {"name": "r", "ansible.builtin.shell": {"cmd": "docker compose --project-directory {{ stack_dir }} 'config'"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "{{ _docker }} compose --project-directory {{ stack_dir }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose --workdir . config", "chdir": "{{ stack_dir }}"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker-compose --context default config",
                                              "chdir": "{{ stack_dir }}"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose --file={{ stack_dir }}/compose.yaml config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker --foo bar inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "/usr/bin/docker inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.shell": "echo $(docker inspect kontroll-grafana-1)"},
    {"name": "r", "ansible.builtin.shell": "echo `docker inspect kontroll-grafana-1`"},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _d }} inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker top kontroll-grafana-1 e"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker -D inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker image inspect kontroll-grafana-snap"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} logs grafana"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} logs --tail 20"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker logs kontroll-grafana-1"}},
    {"name": "r", "community.docker.docker_container": {"name": "kontroll-grafana-1"}, "register": "_c"},
    {"name": "{{ lookup('pipe', 'docker inspect kontroll-grafana-1') }}", "ansible.builtin.debug": {"msg": "x"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose exec -T grafana env", "chdir": "{{ stack_dir }}"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose cp grafana:/proc/1/environ /tmp/e",
                                              "chdir": "{{ stack_dir }}"}},
    {"name": "r", "ansible.builtin.set_fact": {"_i": "{{ lookup('pipe', 'docker inspect kontroll-grafana-1') }}"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "docker compose --project-directory {{ stack_dir }} logs {{ _svc }}"}},
    {"name": "r", "ansible.builtin.shell": "docker " + _BS + "\n  inspect kontroll-grafana-1"},
    {"name": "r", "ansible.builtin.shell": {
        "cmd": "docker compose --project-directory {{ stack_dir }} " + _BS + "\n  config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker\n  inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": "docker\n  inspect kontroll-grafana-1"},
    {"name": "r", "ansible.builtin.set_fact": {"_i": "{{ lookup('pipe','docker inspect kontroll-grafana-1') }}"}},
    {"name": "r", "ansible.builtin.debug": {"var": "query(\"pipe\",\"docker inspect kontroll-grafana-1\")"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker --host=a,b inspect kontroll-grafana-1"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} -f {{ stack_dir }}/compose.yaml config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} -f {{ stack_dir }}/compose.yaml logs grafana"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker attach kontroll-grafana-1"}},
    {"name": "{{ lookup('pipe','docker inspect kontroll-grafana-1') }}", "ansible.builtin.debug": {"msg": "x"}},
    {"name": "r", "ansible.builtin.shell": "'/usr/bin/docker' --config /tmp/a,b inspect kontroll-grafana-1"},
    {"name": "r", "ansible.builtin.set_fact": {
        "_i": "{{ lookup('pipe', ['docker','inspect','kontroll-grafana-1'] | join(' ')) }}"}},
], ids=["a-debug-of-a-file-lookup", "a-debug-var-of-a-file-lookup", "a-no-log-slurp", "a-cat", "compose-config",
        "compose-ps-and-the-file-again", "an-assert-message-lookup", "a-name-lookup", "compose-words-handed-a-shell",
        "another-commands-env-file", "compose-config-with-no-env-named", "compose-config-under-a-file",
        "compose-v1-convert", "compose-exec-by-argv", "a-templated-subcommand", "a-piped-docker-inspect",
        "a-docker-container-inspect", "a-docker-exec", "the-container-info-module", "the-compose-exec-module",
        "compose-run", "a-subcommand-a-statement-writes", "a-templated-docker-subcommand", "an-option-with-no-value",
        "an-option-with-a-value", "compose-config-by-a-short-file", "a-pull-naming-the-env-file", "a-docker-cp",
        "a-compose-cp", "the-container-exec-module", "the-compose-run-module",
        "compose-config-by-its-path", "compose-config-by-argv-and-its-path", "compose-config-handed-to-a-shell",
        "a-quoted-subcommand", "a-docker-word-a-block-writes", "compose-config-under-workdir",
        "compose-v1-config-under-a-context", "compose-config-under-an-equals-file",
        "an-option-the-reader-does-not-know",
        "a-docker-inspect-by-its-path", "a-docker-inspect-in-a-substitution", "a-docker-inspect-in-backquotes",
        "a-docker-command-a-block-writes", "a-docker-top", "a-docker-inspect-past-a-flag", "an-image-inspect",
        "compose-logs-of-grafana",
        "compose-logs-of-every-service", "docker-logs-of-grafana", "the-container-module", "a-names-pipe-lookup",
        "compose-exec-in-its-directory", "compose-cp-in-its-directory", "a-facts-pipe-lookup",
        "compose-logs-of-a-service-a-block-names", "a-docker-inspect-past-a-line-continuation",
        "compose-config-past-a-line-continuation", "a-docker-inspect-over-two-lines-of-a-command",
        "a-free-form-docker-inspect-over-two-lines", "a-pipe-lookup-with-no-space", "a-debug-var-query-with-no-space",
        "a-docker-inspect-past-a-host-list", "compose-config-a-block-writes-at-a-lines-start",
        "compose-config-a-block-writes-past-a-file", "compose-logs-a-block-writes-past-a-file", "a-docker-attach",
        "a-names-pipe-lookup-with-no-space", "a-quoted-docker-path-past-a-comma-value",
        "a-pipe-lookup-of-joined-words"])
def test_a_task_that_reads_the_stacks_env_fails_the_pin(tmp_path, monkeypatch, task):
    """A task that reads the stack's `.env` other than as a compose command's `--env-file` (a debug of a file lookup
    of it, in its message or its `var:`, a slurp, no_log or not, a cat, a compose `config`, which prints the file as
    compose reads it, the file named again past the flag, or a file lookup of it in an assert's message or in the
    task's name, whose banner prints at any verbosity; a compose command's words handed as arguments to a shell
    that cats one of them, their `up` detached so that the cat alone reads (since fold 16 an `up` that is not prints
    every service's log: F12-readers-compose-anywhere passed the row through it), or a second command's `--env-file`,
    a `docker run` that prints its env), or that reads the stack's env with no `.env` named (compose's `config` or
    v1's `convert`, which read the project directory's `.env` by default, `exec` by `argv` too, a subcommand a Jinja
    block writes, `docker inspect` piped or under `container`, `docker exec`, `docker_container_info`,
    `docker_compose_v2_exec` under `action:`) is in `_env_readers`. Why: a debug of a file lookup of the `.env`
    printed the URL, and a slurp registered it where the pin follows no taint, with every row green (fold 11's read
    171, S3); the message's and the name's rows hold that only their words are passed, not their Jinja, and the rows
    after them that only a command that is the compose one, at its start, has its flag passed, and only once (fold
    12's own rows); and a compose `config` that named no `.env`, and an inspect of a stack container, printed the URL
    on both cores with every row green (fold 12's reads 181, S4 and 180, N-B); a compose `run`, a subcommand a
    statement writes, a docker subcommand a block writes, a docker option with no value or with one before `inspect`,
    a short `-f`, the container-exec and compose-run modules, and a compose `pull` that names the `.env` (sound, and
    refused: the `--env-file` is passed before `up`, `restart` and `ps` alone) each passed a mutant of the reader with
    every row green, and a `cp` out of a container, which copies its `/proc/1/environ`, passed the reader itself (fold
    13's own mutants and find); and a compose `config` or a `docker inspect` spelled by the command's path, handed to
    a shell, quoted, written by a Jinja block, in a `$(…)` or backquotes, past compose's hidden `--workdir` or v1's
    `--context`, or past an option the reader does not know, a `docker top` with `e`, an image's inspect and a `logs`
    of Grafana's each passed the reader, which read the command's word as written and took an unknown option for one
    with no value, while the readers' probes printed the URL or Grafana's environment (fold 13's reads 190, S-4, N-4
    and 191, S-e); the `=` form, a docker flag before `inspect`, a `logs` of every service, the `docker_container`
    module, whose result holds the container's inspect, and a name's `pipe` lookup are fold 14's own rows, as are
    compose's `exec` and `cp` run in the stack's directory, a fact's `pipe` lookup and a `logs` of a service a Jinja
    block names: each passed a mutant of the reader with every other row green, the first two since a block before the
    subcommand is read as a docker command of its own (fold 14's own mutants); and a `docker inspect` and a compose
    `config` past a shell's line continuation, a `pipe` lookup with no space after its comma (a fact's, a debug's
    `var:` query) and a compose `config` a Jinja block writes at a line's start, past a `-f` too, each passed the pin,
    and those run printed the URL or the stack's `.env` on both cores (fold 14's read 200, S-A, S-B, S-D); a `docker
    inspect` over two lines of a command module's string, mapped or free-form, a `docker --host=a,b inspect`, which a
    reading at commas alone passes, a compose `logs` of Grafana's that a block at a line's start writes past a `-f`, a
    `docker attach` of Grafana's, and a name's `pipe` lookup with no space, whose Jinja `_unworded` hands on without
    its braces, so a reading of block code alone passed it, are fold 15's own rows, after read 201's leads; and a
    quoted docker path past an option whose value holds a comma, and a `pipe` lookup of words written as a list and
    joined, are fold 15's rows for its own mutants: each passed a reading that kept the quotes at whitespace, or read
    at quotes and not commas, with every other row green."""
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([task], {}, {}, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert _env_readers(w, w.tasks) == [task["name"]]


_GC = "kontroll-grafana-1"
_DC = "docker compose --project-directory {{ stack_dir }}"


@pytest.mark.parametrize("task", [
    {"name": "r", "ansible.builtin.shell": "docker --config '/tmp/a b' inspect " + _GC},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker --config=\"/tmp/a b\" inspect " + _GC}},
    {"name": "r", "ansible.builtin.shell": "docker --config '/tmp/a;b' inspect " + _GC},
    {"name": "r", "ansible.builtin.command": "docker --config 'a;b' inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config \"/tmp/a|b\" inspect " + _GC},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker --config '/tmp/(a)' inspect " + _GC}},
    {"name": "r", "ansible.builtin.shell": "docker --config \"/tmp/a\nb\" inspect " + _GC},
    {"name": "r", "ansible.builtin.debug": {"msg": "{{ lookup('pipe', \"docker --config '/tmp/a;b' inspect "
                                                   + _GC + "\") }}"}},
    {"name": "r", "ansible.builtin.shell": _DC + " --profile 'x;y' config"},
    {"name": "r", "ansible.builtin.shell": _DC + " -p \"$(basename {{ stack_dir }})\" config"},
    {"name": "r", "ansible.builtin.shell": "docker --config $(mktemp -d) inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config `mktemp -d` inspect " + _GC},
    {"name": "r", "ansible.builtin.command": {"argv": ["docker", "--config", "/tmp/a\nb", "inspect", _GC]}},
    {"name": "r", "ansible.builtin.command": {"argv": ["docker", "--config", "/tmp/a b", "inspect", _GC]}},
    {"name": "r", "ansible.builtin.command": {"argv": ["docker", "--config", "", "inspect", _GC]}},
    {"name": "r", "ansible.builtin.command": {"argv": ["docker", "--config", "a;b&c|d`e(f)g", "inspect", _GC]}},
    {"name": "r", "ansible.builtin.shell": "docker --config $(dirname $(mktemp -d)) inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config a" + _BS + " b inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config 'a&b' inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config \"a" + _BS + "\" b\" inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config 'a`b' inspect " + _GC},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} -pkontroll config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} -f{{ stack_dir }}/compose.yaml config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} --all-resources=true config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} --dry-run=false config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _d }} -Hunix:///var/run/docker.sock inspect " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose -pkontroll config", "chdir": "{{ stack_dir }}"}},
    {"name": "r", "ansible.builtin.shell": "if true; then {{ _compose }} config; fi"},
    {"name": "r", "ansible.builtin.shell": "! {{ _compose }} config"},
    {"name": "r", "ansible.builtin.shell": "X=1 {{ _compose }} config"},
    {"name": "r", "ansible.builtin.command": {"cmd": "env {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "timeout 30 {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "nice -n 5 {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "sudo -u {{ kontroll_user }} {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "sudo -u kontroll {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "timeout {{ t }} {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "sudo env {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.shell": "env COMPOSE_X=1 {{ _compose }} config"},
    {"name": "r", "ansible.builtin.command": {"cmd": "sudo {{ _compose }} run --rm grafana env"}},
    {"name": "r", "ansible.builtin.shell": "if {{ _compose }} config; then :; fi"},
    {"name": "r", "ansible.builtin.shell": "nohup {{ _compose }} attach grafana"},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} --dry-run config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _compose }} -f {{ stack_dir }}/compose.yaml attach grafana"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker compose attach grafana", "chdir": "{{ stack_dir }}"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker start -a " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker container start --attach " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker start --attach=true " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": _DC + " up grafana"}},
    {"name": "r", "ansible.builtin.command": {"cmd": _DC + " --env-file {{ stack_dir }}/.env up grafana"}},
    {"name": "r", "ansible.builtin.expect": {"command": "docker\n  inspect " + _GC, "responses": {"x": "y"}}},
    {"name": "r", "ansible.builtin.shell": "docker ins" + _BS + "\npect " + _GC + "\n"},
    {"name": "r", "ansible.builtin.shell": "dock" + _BS + "\ner inspect " + _GC + "\n"},
    {"name": "r", "ansible.builtin.shell": "docker \"ins" + _BS + "\npect\" " + _GC + "\n"},
    {"name": "r", "ansible.builtin.shell": "docker" + _BS + "\n  inspect " + _GC},
    {"name": "r", "ansible.builtin.debug": {
        "msg": "{{ lookup('ansible.builtin.pipe','docker compose --project-directory ' ~ stack_dir ~ ' config') }}"}},
    {"name": "r", "ansible.builtin.debug": {"msg": "{{ lookup('pipe', 'docker' ~ ' inspect " + _GC + "') }}"}},
    {"name": "r", "ansible.builtin.debug": {
        "msg": "{{ \"'\" ~ lookup('pipe', 'docker --config \"a;b\" inspect " + _GC + "') ~ \"'\" }}"}},
    {"name": "r", "ansible.builtin.set_fact": {
        "_i": "{{ \"'\" ~ lookup('pipe', 'docker --config \"a;b\" inspect " + _GC + "') ~ \"'\" }}"}},
    {"name": "r", "ansible.builtin.debug": {
        "msg": "{{ \"'\" ~ lookup('pipe', 'docker --config \"a;b\" ins" + _BS + "\npect " + _GC + "') ~ \"'\" }}"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ 'docker --config \"a;b\"\ninspect " + _GC + "' }}"}},
    {"name": "r", "ansible.builtin.shell": "docker commit " + _GC + " snap && docker run --rm snap env"},
    {"name": "r", "ansible.builtin.uri": {"unix_socket": "/var/run/docker.sock", "return_content": True,
                                          "url": "http://localhost/v1.51/containers/" + _GC + "/json"}},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "curl -s --unix-socket /var/run/docker.sock http://localhost/containers/" + _GC + "/json"}},
    {"name": "r", "ansible.builtin.shell": "docker --config $(echo ')') inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config $(case x in x) echo y;; esac) inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": _DC + " --parallel $((1+1)) config"},
    {"name": "r", "ansible.builtin.shell": "docker --config ${X:-/tmp/a;b} inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config \"$(dirname \"/tmp/a b/c\")\" inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": {"cmd": "docker --config <(true) inspect " + _GC,
                                            "executable": "/bin/bash"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "timeout 30s {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "timeout -k 5s 2.5 {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.shell": "2>/dev/null {{ _compose }} config"},
    {"name": "r", "ansible.builtin.shell": "2> /dev/null {{ _compose }} config"},
    {"name": "r", "ansible.builtin.shell": "{{ _compose }} 2>/dev/null config"},
    {"name": "r", "ansible.builtin.shell": "docker 2>&1 inspect " + _GC},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker start -ai " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker start -a=true " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker start --interactive " + _GC}},
    {"name": "r", "ansible.builtin.shell": "sh -c '{{ _compose }} config'"},
    {"name": "r", "ansible.builtin.shell": "eval {{ _compose }} config"},
    {"name": "r", "ansible.builtin.command": {"cmd": "su kontroll -c \"{{ _compose }} config\""}},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _d }} -Dc default inspect " + _GC}},
    {"name": "r", "ansible.builtin.shell": "echo " + _BS * 2 + "\ndocker inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "# docker compose pull" + _BS + "\ndocker inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "# don't\ndocker --config '/tmp/a b' inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "# the \"x\ndocker --config \"/tmp/a b\" inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "sh -c \"docker --config '/tmp/a b' inspect " + _GC + "\""},
    {"name": "r", "ansible.builtin.command": {
        "cmd": "su kontroll -c \"docker --config '/tmp/a;b' inspect " + _GC + "\""}},
    {"name": "r", "ansible.builtin.command": {"cmd": "/usr/bin/sudo {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.command": {"cmd": "/usr/bin/timeout 30 {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.debug": {"msg": "{{ lookup('pipe', 'docker' + ' inspect " + _GC + "') }}"}},
    {"name": "r", "ansible.builtin.shell": _DC + " commit grafana snap && docker run --rm snap env"},
    {"name": "r", "ansible.builtin.shell": "{% set c = 'docker' ~ ' inspect " + _GC + "' %}{{ c }}"},
    {"name": "r", "ansible.builtin.debug": {
        "msg": "{{ lookup('pipe', 'docker ' ~ ' '.join(['inspect', '" + _GC + "'])) }}"}},
    {"name": "r", "ansible.builtin.debug": {
        "msg": "{{ lookup('pipe', 'docker compose --parallel ' ~ 4 ~ ' config') }}"}},
    {"name": "r", "ansible.builtin.shell": "no_proxy=192.0.2.1 {{ _compose }} config"},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker start --attach=1 " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": _DC + " up --detach=false grafana"}},
    {"name": "r", "ansible.builtin.command": {"cmd": _DC + " up --wait=false grafana"}},
    {"name": "r", "ansible.builtin.shell": {"cmd": "{{ _compose }} &>/dev/null config", "executable": "/bin/bash"}},
    {"name": "r", "ansible.builtin.shell": "docker >|/tmp/x inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": (
        "sh -c 'su kontroll -c \"docker --config $(echo /tmp/a) inspect " + _GC + "\"'")},
    {"name": "r", "ansible.builtin.command": {"cmd": "{{ _d }} -Dc=default inspect " + _GC}},
    {"name": "r", "ansible.builtin.command": {"cmd": "docker -Dq x inspect " + _GC}},
    {"name": "r", "ansible.builtin.shell": "{{ _compose }} </dev/null config"},
    {"name": "r", "ansible.builtin.shell": "true # don't\ndocker --config '/tmp/a b' inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config \"/tmp/it's a\" inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config $( (echo /tmp/a) ) inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "docker --config $(echo /tmp/showcase) inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": {"cmd": "docker --config >(cat) inspect " + _GC, "executable": "/bin/bash"}},
    {"name": "r", "ansible.builtin.shell": "docker --config \"/tmp/<(a\" inspect " + _GC},
    {"name": "r", "ansible.builtin.command": {"cmd": "sudo /usr/bin/env {{ _compose }} config"}},
    {"name": "r", "ansible.builtin.shell": "docker 2>/dev/null --config '/tmp/a b' inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": "cat <<EOF\nit's\nEOF\ndocker 2>/dev/null inspect " + _GC},
    {"name": "r", "ansible.builtin.shell": (
        "sh -c \"printf '%s" + _BS + "\"' x; docker --config '/tmp/a b' inspect " + _GC + "\"")},
], ids=["a-quoted-space-in-an-options-value", "a-quoted-space-past-an-equals", "a-quoted-semicolon-in-a-shell",
        "a-quoted-semicolon-in-a-free-form-command", "a-quoted-bar", "a-quoted-parenthesis", "a-quoted-line-break",
        "a-quoted-semicolon-in-a-pipe-lookup", "a-quoted-semicolon-before-compose-config",
        "a-substitution-as-composes-project-name", "a-substitution-as-an-options-value",
        "a-backquote-substitution-as-an-options-value", "an-argv-value-holding-a-line-break",
        "an-argv-value-holding-a-space", "an-argv-value-that-is-empty", "an-argv-value-holding-each-separator",
        "a-nested-substitution-as-an-options-value",
        "an-escaped-space-in-an-options-value", "a-quoted-ampersand", "an-escaped-quote-in-double-quotes",
        "a-quoted-backquote",
        "a-block-past-an-attached-project-name", "a-block-past-an-attached-file",
        "a-block-past-a-flags-equals-true", "a-block-past-dry-run-equals-false", "a-block-past-an-attached-host",
        "docker-compose-past-an-attached-project-name", "a-block-after-then", "a-block-after-a-bang",
        "a-block-after-an-assignment", "a-block-after-env", "a-block-after-timeout", "a-block-after-nice-n",
        "a-block-after-sudo-u", "a-block-after-sudo-u-and-a-name", "a-block-after-timeout-and-a-block",
        "a-block-after-sudo-env", "a-block-after-env-and-an-assignment", "a-blocks-compose-run-after-sudo",
        "a-block-after-if", "a-blocks-attach-after-nohup",
        "a-block-past-a-flag", "a-blocks-attach-past-a-file", "compose-attach-of-grafana", "a-docker-start-attached",
        "a-docker-container-start-attached", "a-docker-start-attach-equals-true", "compose-up-of-grafana-attached",
        "compose-up-attached-past-the-env-file",
        "an-expects-docker-inspect-over-two-lines", "a-continuation-inside-the-subcommand",
        "a-continuation-inside-the-docker-word", "a-continuation-inside-a-quoted-subcommand",
        "a-continuation-with-no-space-before-it", "a-pipe-lookup-joined-across-tildes",
        "a-pipe-lookup-with-docker-apart", "a-messages-pipe-lookup-between-two-quote-literals",
        "a-facts-pipe-lookup-between-two-quote-literals",
        "a-pipe-lookup-between-two-quote-literals-over-a-continuation", "a-command-modules-literal-over-two-lines",
        "a-docker-commit-then-a-run-of-it", "the-engine-api-by-uri", "the-engine-api-by-curl",
        "a-substitution-holding-a-quoted-parenthesis", "a-substitution-holding-a-case",
        "an-arithmetic-expansion-as-composes-value", "a-parameter-expansion-holding-a-semicolon",
        "a-quote-nested-in-a-quoted-substitution", "a-process-substitution-as-an-options-value",
        "a-block-after-timeout-and-a-duration", "a-block-after-timeout-k-and-a-fraction", "a-block-after-a-redirect",
        "a-block-after-a-redirect-apart-from-its-target", "a-redirect-between-a-block-and-its-read",
        "a-redirect-between-docker-and-its-read", "a-docker-start-ai", "a-docker-start-a-equals-true",
        "a-docker-start-interactive", "a-block-in-sh-c", "a-block-after-eval", "a-block-in-su-c",
        "a-block-past-a-cluster-of-short-options", "a-line-after-an-escaped-backslash",
        "a-line-after-a-comments-backslash", "a-comments-apostrophe-before-a-quoted-value",
        "a-comments-quote-before-a-double-quoted-value", "a-quoted-value-in-sh-c", "a-quoted-semicolon-in-su-c",
        "a-block-after-sudo-by-its-path", "a-block-after-timeout-by-its-path", "a-pipe-lookup-joined-by-a-plus",
        "a-written-compose-commit-then-a-run-of-it",
        "a-command-a-set-block-joins", "a-pipe-lookup-joined-across-a-method-call",
        "a-pipe-lookup-joined-across-a-number", "a-block-after-a-lower-case-assignment",
        "a-docker-start-attach-equals-1", "compose-up-detach-equals-false", "compose-up-wait-equals-false",
        "a-block-past-a-bash-amp-redirect", "a-clobbering-redirect-between-docker-and-its-read",
        "a-substitution-in-a-command-two-shells-deep", "a-block-past-a-cluster-whose-option-takes-an-equals-value",
        "a-docker-read-past-a-cluster-holding-an-unknown-option", "a-block-past-an-input-redirect",
        "a-trailing-comments-apostrophe-before-a-quoted-value", "an-apostrophe-in-a-double-quoted-value",
        "a-substitution-holding-a-subshell", "a-substitution-holding-a-word-ending-in-case",
        "a-process-substitution-that-writes-as-an-options-value", "a-double-quoted-value-holding-a-process-opener",
        "a-prefix-by-its-path-past-another", "a-quoted-value-after-a-redirect",
        "a-redirected-read-after-a-heredoc-holding-an-apostrophe", "a-quoted-command-holding-an-escaped-quote"])
def test_a_docker_read_sh_quoting_a_prefix_or_an_attached_value_hides_fails_the_pin(tmp_path, monkeypatch, task):
    """A docker or compose read the pin read past before fold 16 is in `_env_readers`: one past an option whose value
    holds a quoted space, line break or separator, an escaped space or quote, or a `$(…)` (nested too) or backquoted
    substitution, in a shell, a command module's string, a `pipe` lookup or an `argv` element, an empty one too or one
    holding each separator (`_sh_masked`, each element one word); a Jinja block that writes `docker compose` or
    `docker`, past an option with its value attached or a flag's `=value` (`_subcommand`), or past a prefix word, an
    assignment or a shell keyword, with their options, the word after an option, a number, an assignment or another
    block between (`_in_command_place`), past a flag too; an `attach` a block writes, and compose's; a docker `start
    -a` (or `--attach=true`) and a compose `up` neither detached nor waiting (`_prints_grafanas_log`); expect's
    command over two lines; a continuation inside a word, a quoted one and one with no space before it, which sh
    removes; a `pipe` lookup's command joined across `~`, and one between two literals that each hold a `'`, in a
    message or a fact, which `_joined` puts in one text where the two pair and hide the command (each literal is read
    alone, `_jinja_strings`, a message's too: before fold 16's last change the message's passed the pin), over a
    continuation inside a literal too, which sh removes (Ansible keeps a literal's backslashes); a command module's
    literal over two lines, which it reads as one (`_jinja_strings` of its text with its line breaks read as spaces); a
    written `docker commit` of Grafana's; and the Engine API over a unix socket. Fold 17's rows: a value sh holds past a
    `)` of its own, quoted or a `case` pattern's, an arithmetic, parameter or process substitution, a quote nested in a
    quoted substitution, a subshell or a word ending in `case` inside one, a process substitution that writes, and a
    `<(` or an apostrophe a double-quoted value holds (`_sh_masked`'s scan); a block past `timeout`'s duration
    (`_DURATION`), past a redirect, apart from its target too, or with one between it and its read, bash's `&>` and an
    input's among them, and docker with one between it and its read, a `2>&1` or a `>|` (`_unredirected`); a `start`
    attached by `-ai`, `-a=true` or `--interactive`; a block in a quoted `sh -c` or `su -c` command, or after `eval`; a
    block past a cluster of short options, its option's value after a `=` too, and docker past a cluster holding an
    option it does not know (`_subcommand`); the line after an escaped backslash or a comment's, which sh does not
    continue (the continuation read as a space too); a comment's apostrophe or quote before a quoted value, at a line's
    start or after a command, a quoted value inside a quoted `sh -c` or `su -c` command, one holding an escaped quote,
    and a substitution in a command two shells deep (`_held`); a prefix by its path, past another too; a redirect before
    a quoted value, and one in a read after a heredoc whose body holds an apostrophe (each reading's own); a lookup
    joined by `+`; a written compose `commit` of Grafana's service; and a command a `{% set %}` joins, one joined across
    a method call or a number, a block after a lower-case assignment, `--attach=1`, and an `up` with `--detach=false` or
    `--wait=false`. Why: fold 15's runtime read ran each docker row on both cores, against the docker CLI and a stand-in
    daemon, and each printed the URL or the stack's `.env` with every row green (210, S-1, S-2, S-3, N-1 … N-4); its
    adversarial read found each other row passing the pin (211, S-1, S-2, N-1, N-2, N-3), but for the empty value and
    the argv value's separators, the nested substitution, the escaped space and quote, the quoted `&` and backquote, the
    name after `sudo -u`, the block after `timeout`, the `env` after `sudo`, the assignment after `env` and
    `--attach=true`, fold 16's own rows: each failed a mutant of the reading that passed every other row (the quote
    literals: F16-jinja-strings-off and F16-masked-no-jinja-strings, which `_joined` let pass; the two lines:
    F16-masked-no-text-strings); the message's row and the continuation's no longer fail theirs
    (F16-masked-no-literals, F16-literals-no-continuation: each passes every row, as another reading refuses each row
    too, reads 230 and 240), so a name's or a message's Jinja literal, read alone, is held by no row;
    and fold 16's runtime read ran each of fold 17's rows it names on both cores, and each printed the URL or the `.env`
    with every row green (220, S-1, S-2, S-3, N-2, N-3), its adversarial read found the next ones passing the pin (221,
    S-1, S-2, N-1, N-2, N-8), and the rows `a-command-a-set-block-joins` … `compose-up-wait-equals-false` hold the six
    readings fold 16 added that no row held (221, N-4: a mutant of each passed every row); `--interactive` is fold 17's
    own (the docker CLI's `start` attaches for `-i` as for `-a`), as are the rows after them, each written for a
    reading of fold 17's own that no row then held (the subshell's nested `(` is now held by the arithmetic row too, as
    `$((` is read as a `$(` holding a `(`; the subshell row stays in case a `$((` opener comes back)."""
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([task], {}, {}, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert _env_readers(w, w.tasks) == [task["name"]]


# The options and flags `_subcommand` knows, written out, not read from its sets, so that a set that drops one
# fails a row (fold 15's read 210, N-5: 42 such mutants passed every row).
_COMPOSE_OPTS_ROWS = ["--project-directory", "--workdir", "--file", "-f", "--env-file", "--progress", "--project-name",
                      "-p", "--profile", "--ansi", "--parallel", "--context", "-c", "--host", "-H", "--log-level",
                      "--tlscacert", "--tlscert", "--tlskey"]
_COMPOSE_FLAGS_ROWS = ["--compatibility", "--dry-run", "--all-resources", "--verbose", "--no-ansi",
                       "--skip-hostname-check", "--tls", "--tlsverify", "--version", "-v"]
_DOCKER_OPTS_ROWS = ["--config", "--context", "-c", "--host", "-H", "--log-level", "-l", "--tlscacert", "--tlscert",
                     "--tlskey"]
_DOCKER_FLAGS_ROWS = ["--debug", "-D", "--tls", "--tlsverify", "--version", "-v"]
_OPTION_ROWS = ([("a-block-past-compose-option" + o, "{{ _compose }} %s v config" % o) for o in _COMPOSE_OPTS_ROWS]
                + [("a-block-past-compose-flag" + f, "{{ _compose }} %s config" % f) for f in _COMPOSE_FLAGS_ROWS]
                + [("a-block-past-docker-option" + o, "{{ _d }} %s v inspect %s" % (o, _GC)) for o in _DOCKER_OPTS_ROWS]
                + [("a-block-past-docker-flag" + f, "{{ _d }} %s inspect %s" % (f, _GC)) for f in _DOCKER_FLAGS_ROWS]
                + [("an-image-inspect-of-a-snapshot", "docker image inspect snap"),
                   ("a-docker-inspect-glued-past-a-semicolon", "true;docker inspect " + _GC),
                   ("a-docker-inspect-glued-past-an-and", "true&&docker inspect " + _GC),
                   ("a-docker-inspect-glued-past-a-pipe", "true|docker inspect " + _GC),
                   ("a-docker-inspect-glued-in-parentheses", "(docker inspect " + _GC + ")"),
                   ("a-docker-inspect-glued-past-a-substitution", "$(true)docker inspect " + _GC),
                   ("a-docker-inspect-in-glued-backquotes", "echo `docker inspect " + _GC + "`")])
# The words `_in_command_place` reads a command past, written out, not read from `_PREFIX_CMDS`.
_PREFIX_ROWS = ["sudo", "doas", "runuser", "env", "timeout", "nice", "nohup", "ionice", "stdbuf", "setsid", "exec",
                "command", "time", "if", "then", "else", "elif", "while", "until", "do", "!", "{"]
_OPTION_ROWS += [("a-block-after-prefix-" + {"!": "bang", "{": "brace"}.get(p, p), "%s {{ _compose }} config" % p)
                 for p in _PREFIX_ROWS]


@pytest.mark.parametrize("cmd", [c for _, c in _OPTION_ROWS], ids=[i for i, _ in _OPTION_ROWS])
def test_a_docker_read_past_each_option_the_reader_knows_fails_the_pin(tmp_path, monkeypatch, cmd):
    """A docker or compose read a Jinja block writes, past each option `_subcommand` knows with its value and each
    flag it knows alone, or after each prefix word `_in_command_place` knows, each listed here as written, is in
    `_env_readers`; so are an image's inspect and a docker inspect glued to a separator, a parenthesis, a substitution
    or a backquote. Why: the sets were read nowhere a row could tell one member from another, so a set that dropped
    one, and a cut that dropped a separator, passed every row (fold 15's read 210, N-5: 42 of its option-set mutants
    survived the suite), and a reader that dropped an option took its value for the subcommand and passed the read;
    the prefix words are fold 16's own, held so for the same reason."""
    task = {"name": "r", "ansible.builtin.shell": cmd}
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([task], {}, {}, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert _env_readers(w, w.tasks) == ["r"]


def test_the_words_fold_16s_readings_could_take_for_a_read_are_not_reads(tmp_path, monkeypatch):
    """The shapes the readings fold 16 adds could take for a read, and that read no env of the stack, are not in
    `_env_readers`: git's `config` past a block that writes git, past `sudo -u`, `timeout` or an assignment and past
    its `-C` value, and its `commit` past `-c` values, one quoting a space; a `cp` past `sudo` handed a block; a
    docker command past an attached host that lists containers; a task whose name says `unix_socket` but runs nothing;
    an `attach` of another service's container, and its `start -a`; a `start` not attached; a compose `up` detached or
    waiting, by each spelling; a check for docker and a pipe that runs another `exec`; an `up` handed to a block that
    is no command's place (`make -C {{ d }} up`, `ssh {{ h }} up`); git's `config` past `timeout`'s duration, and a
    block's past its `-C` value with a redirect after; a block past a cluster of short options handed `ps`; and a
    block handed `ps` past a tab-stripped heredoc whose word is `config`, written with a blank before it. Why: a
    block in a command's place is read as `docker compose`, a prefix word's options are passed, an attached value is
    skipped, a socket is read in a task's text, and a `start` or an `up` prints a log, so a rule that read a block
    anywhere, read past a prefix's non-option words, read a name's words for a socket, or read every `start` or `up`
    would refuse the deploy's own `git -C {{ bare }} config` and these with it (fold 15's reads 210, S-3 and 211,
    S-2, N-1, its FR rows; past a prefix's option the next word is read as its value, so `sudo -n git -C {{ bare }}
    config` is refused, a sound refusal R-ALERT-1 names: fold 16's read 221, N-3); `up -d` alone, `--detach` and
    their `=true` spellings each hold a member of the reading's set that a mutant drops (fold 16's own rows); an `up`
    read for docker too refused the first two (fold 16's read 220, N-5); and a duration, a redirect and a cluster are
    fold 17's readings, each of which a looser rule would take for a read; a redirect reading that took `<<-` for
    two operators kept the heredoc's word and read it as the subcommand (fold 17's read 231, S-1)."""
    tasks = [{"name": "g1", "ansible.builtin.command": {"cmd": "{{ _git }} -C {{ bare }} config a b"}},
             {"name": "g2", "ansible.builtin.command": {"cmd": "sudo -u {{ u }} git -C {{ bare }} config a b"}},
             {"name": "g3", "ansible.builtin.shell": "timeout 9 git -C {{ bare }} config a b"},
             {"name": "g4", "ansible.builtin.shell": "GIT_X=1 git -C {{ bare }} config a b"},
             {"name": "g5", "ansible.builtin.command": {
                 "cmd": "git -C {{ bare }} config receive.denyNonFastForwards true"}},
             {"name": "g6", "ansible.builtin.shell":
                 "git -c safe.directory={{ d }} -C {{ d }} -c user.name='k b' commit -q -m x"},
             {"name": "c1", "ansible.builtin.command": {"cmd": "sudo cp {{ a }} config"}},
             {"name": "h1", "ansible.builtin.command": {"cmd": "docker -Hunix:///run/docker.sock ps"}},
             {"name": "n1 the unix_socket is docker's", "ansible.builtin.debug": {"msg": "x"}},
             {"name": "a1", "ansible.builtin.command": {"cmd": "docker attach kontroll-prometheus-1"}},
             {"name": "s1", "ansible.builtin.command": {"cmd": "docker start " + _GC}},
             {"name": "s2", "ansible.builtin.command": {"cmd": "docker start -a kontroll-prometheus-1"}},
             {"name": "u1", "ansible.builtin.command": {"cmd": _DC + " up -d grafana"}},
             {"name": "u2", "ansible.builtin.command": {"cmd": _DC + " up --wait grafana"}},
             {"name": "u3", "ansible.builtin.command": {"cmd": _DC + " up --detach grafana"}},
             {"name": "u4", "ansible.builtin.command": {"cmd": _DC + " up --detach=true grafana"}},
             {"name": "u5", "ansible.builtin.command": {"cmd": _DC + " up --wait=true grafana"}},
             {"name": "v1", "ansible.builtin.shell": "command -v docker ; cp a b"},
             {"name": "v2", "ansible.builtin.shell": "docker ps -q | xargs echo; exec true"},
             {"name": "m1", "ansible.builtin.shell": "make -C {{ d }} up"},
             {"name": "m2", "ansible.builtin.command": {"cmd": "ssh {{ h }} up"}},
             {"name": "g7", "ansible.builtin.shell": "timeout 9s git -C {{ bare }} config a b"},
             {"name": "g8", "ansible.builtin.shell": "{{ _git }} -C {{ bare }} config a b 2>/dev/null"},
             {"name": "k1", "ansible.builtin.command": {"cmd": "{{ _d }} -Dc default ps"}},
             {"name": "r1", "ansible.builtin.shell": "{{ _d }} <<- config ps"}]
    monkeypatch.setitem(_LEAK_SHAPES, "_row", (tasks, {}, {}, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert _env_readers(w, w.tasks) == []


def test_a_template_that_reads_the_stacks_env_fails_the_pin(tmp_path, monkeypatch):
    """A task whose template names the stack's `.env` (a file lookup of it) is in `_env_readers`, as a task whose own
    text does. Why: the template renders where the task runs and, under `--check --diff`, prints what it rendered;
    a pin that read the task's keys alone passed it (fold 12's own row)."""
    task = {"name": "r", "ansible.builtin.template": {"src": "r.j2", "dest": "/etc/x"}}
    files = {"ansible/playbooks/templates/r.j2": "{{ lookup('file', stack_dir ~ '/.env') }}\n"}
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([task], {}, files, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert _env_readers(w, w.tasks) == ["r"]


def test_the_compose_commands_and_a_messages_words_naming_the_env_are_not_reads(tmp_path, monkeypatch):
    """The deploy's compose commands that name the `.env` as their `--env-file` (`up`, `restart`, `ps`), a task name
    and an assert's or a debug's message whose words name it, and a read of `.env.homepage`, are not in
    `_env_readers`; nor are a compose `logs` past `--progress` and `-f`, a `docker run` of another image, a systemd
    restart of docker, a compose `up` written over two lines, a shell whose `docker` ends a line before an `exec` and a
    task whose name spells a `docker inspect`, which read no env of the stack. Why: the shipped deploy names the `.env`
    so, and none of them prints a value of it (fold 12's own rows); and a reader of the stack's subcommands that took
    an option's value for the subcommand, or read `docker` in a unit's name as a command, would fail the shipped tree
    (fold 13's own rows), as one that read a command unjoined or past a line's end failed the two after them, and one
    that read a task's name, which runs nothing, the last (fold 13's own mutants); and a block whose task's name, a
    debug's, an assert's (its `success_msg` too) and a fail's message whose words spell a docker read, a compose
    option written with its `=` before a `logs` of another service, `docker --version`, compose's `--dry-run`, a
    Jinja block before `ps` and a docker option with its value before `ps` read no env of it either: a pin that read
    a block's tasks as its own, or a message's words, failed the first four (fold 13's reads 190, N-2 and 191, N-c),
    and the rest are fold 14's own rows, but for a `git -C {{ bare }} config`, the deploy's own, a block past a
    line's start before a compose read: a reader that read such a block as `docker compose` wherever it stood failed
    the shipped tree (fold 14's read 200, S-D), which this row holds."""
    env = " --env-file {{ stack_dir }}/.env "
    tasks = [{"name": "up", "ansible.builtin.command": {"cmd": "docker compose --progress plain --project-directory "
                                                               "{{ stack_dir }} --file {{ stack_dir }}/compose.yaml"
                                                               + env + "up -d x"}},
             {"name": "re", "ansible.builtin.command": {
                 "cmd": "docker compose --project-directory {{ stack_dir }}" + env + "restart x"}},
             {"name": "ps", "ansible.builtin.command": {
                 "cmd": "docker compose --project-directory {{ stack_dir }}" + env + "ps"}},
             {"name": "check docker/.env", "ansible.builtin.assert": {"that": "true", "fail_msg": "see docker/.env"}},
             {"name": "say", "ansible.builtin.debug": {"msg": "no .env yet: {{ x | default('') }}"}},
             {"name": "home", "ansible.builtin.debug": {"msg": "{{ lookup('file', stack_dir ~ '/.env.homepage') }}"}}]
    tasks += [{"name": "logs", "ansible.builtin.command": {
                  "cmd": "docker compose --progress plain --project-directory {{ stack_dir }} -f x.yaml logs vector"}},
              {"name": "run", "ansible.builtin.command": {"cmd": "docker run --rm --log-driver none -v x:/dst busybox"
                                                                 " chown -R 1001:1001 /dst"}},
              {"name": "svc", "ansible.builtin.systemd": {"name": "docker", "state": "restarted"}},
              {"name": "up2", "ansible.builtin.command": {"cmd": "docker compose --project-directory {{ stack_dir }}\n"
                                                                 "  --env-file {{ stack_dir }}/.env up -d x"}},
              {"name": "check", "ansible.builtin.shell": "command -v docker\nexec /usr/local/bin/check"},
              {"name": "docker inspect is for the operator", "ansible.builtin.debug": {"msg": "ok"}}]
    tasks += [{"name": "b", "block": [{"name": "docker inspect is the operator's",
                                       "ansible.builtin.debug": {"msg": "k"}}]},
              {"name": "nd1", "ansible.builtin.debug": {"msg": "run docker inspect grafana to look"}},
              {"name": "nd2", "ansible.builtin.assert": {"that": "true",
                                                         "fail_msg": "check with: docker compose config",
                                                         "success_msg": "or with: docker compose config"}},
              {"name": "nd3", "ansible.builtin.fail": {"msg": "use docker exec to look"}, "when": "false"},
              {"name": "eq", "ansible.builtin.command": {
                  "cmd": "docker compose --progress=plain --project-directory {{ stack_dir }} logs --tail 50 vector"}},
              {"name": "ver", "ansible.builtin.command": {"cmd": "docker --version"}},
              {"name": "fl", "ansible.builtin.command": {
                  "cmd": "docker compose --dry-run --project-directory {{ stack_dir }} ps"}},
              {"name": "tj", "ansible.builtin.command": {"cmd": "{{ _docker }} ps"}},
              {"name": "dh", "ansible.builtin.command": {"cmd": "docker -H unix:///run/docker.sock ps"}},
              {"name": "gc", "ansible.builtin.command": {
                  "cmd": "git -C {{ bare }} config receive.denyNonFastForwards true"}}]
    monkeypatch.setitem(_LEAK_SHAPES, "_row", (tasks, {}, {}, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert _env_readers(w, w.tasks) == []


@pytest.mark.parametrize("play_keys,files", [
    ({"vars": {"_e": "{{ stack_dir }}/.env"}}, {}),
    ({"vars": {"_e": "{{ lookup('file', stack_dir ~ '/.env') }}"}}, {}),
    ({}, {"ansible/roles/r/defaults/main.yml": {"_e": "{{ lookup('file', stack_dir ~ '/.env') }}"}}),
    ({}, {"instance.example/inventory/group_vars/all.yml": {"_e": "docker/.env"}}),
    ({"vars": {"_e": "{{ lookup('template', 'e.j2') }}"}},
     {"ansible/playbooks/templates/e.j2": "{{ stack_dir }}/.env\n"}),
    ({}, {"ansible/roles/r/meta/main.yml": {"dependencies": [{"role": "d", "_e": "{{ stack_dir }}/.env"}]},
          "ansible/roles/d/tasks/main.yml": [{"name": "d", "ansible.builtin.debug": {"msg": "hi"}}]}),
], ids=["a-play-var-of-its-path", "a-play-var-lookup", "a-role-default-lookup", "an-inventory-group-var",
        "a-play-var-that-looks-up-a-template-naming-it", "a-dependency-param-of-its-path"])
def test_a_var_that_holds_the_stacks_env_fails_the_pin(tmp_path, monkeypatch, play_keys, files):
    """A var the deploy reads, a play's, a role default, an inventory group var or a dependency's param, that holds the
    `.env`'s path or a lookup of it, or that looks up a template naming it, is in `_env_vars` (a play's under its key,
    `vars`), which the URL's pin holds empty. Why: a play var and a role default that looked the file up, read by a
    debug that named no `.env`, each printed the URL on both cores with every row green, as `_env_readers` reads tasks
    only (fold 12's read 181, S4); a dependency's param passed a pin that read no dependency's (fold 13's own
    mutant)."""
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([], play_keys, files, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert [k for _, k in _env_vars(w)] == [next(iter(play_keys), "_e")]


@pytest.mark.parametrize("play_keys,files,key", [
    ({"roles": [{"role": "r", "_e": "{{ lookup('file', stack_dir ~ '/.env') }}"}]}, {}, "roles"),
    ({"roles": [{"role": "r", "vars": {"_e": "{{ stack_dir }}/.env"}}]}, {}, "roles"),
    ({"environment": {"E": "{{ lookup('file', stack_dir ~ '/.env') }}"}}, {}, "environment"),
    ({"name": "render {{ lookup('file', stack_dir ~ '/.env') }}"}, {}, "name"),
    ({"name": "render {{ lookup('template', 'e.j2') }}"},
     {"ansible/playbooks/templates/e.j2": "{{ stack_dir }}/.env\n"}, "name"),
], ids=["a-role-entrys-param", "a-role-entrys-vars", "the-plays-environment", "a-lookup-in-the-plays-name",
        "a-template-the-plays-name-looks-up"])
def test_a_play_key_that_holds_the_stacks_env_fails_the_pin(tmp_path, monkeypatch, play_keys, files, key):
    """A play key but its task lists that holds the `.env`'s path or a lookup of it (a `roles:` entry's param or its
    `vars:`, the play's `environment:`, a lookup in the play's name, or a template the name looks up that names it) is
    in `_env_vars`, which the URL's pin holds empty. Why: a role entry's param and vars, a play's environment and its
    name each printed the file on both cores with every row green, as `_env_vars` read a play's `vars:` alone (fold
    13's reads 190, S-3 and 191, S-c)."""
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([], play_keys, files, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert [k for _, k in _env_vars(w)] == [key]


def test_an_import_entrys_condition_that_holds_the_stacks_env_fails_the_pin(tmp_path, monkeypatch):
    """An `import_playbook` entry's key that looks the `.env` up (its `when:`; an entry's `vars:` stops the walk,
    `Unfollowed`, 131 S4) is in `_env_vars`, which the URL's pin holds empty. Why: an entry's condition holds for each
    task of the plays it imports, and a pin that read the plays' keys alone passed the row (fold 14's own mutant)."""
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([], {}, {
        "ansible/playbooks/sub/o.yml": [{"hosts": "localhost",
                                         "tasks": [{"name": "d", "ansible.builtin.debug": {"msg": "hi"}}]}]},
        [{"ansible.builtin.import_playbook": "sub/o.yml",
          "when": "lookup('file', stack_dir ~ '/.env') | length > 0"}], "unlogged"))
    assert [k for _, k in _env_vars(_leak_tree(tmp_path, "_row"))] == ["when"]


def test_a_var_of_the_portals_env_is_not_the_stacks(tmp_path, monkeypatch):
    """A var that holds `.env.homepage`, the portal's own env, or names no `.env`, and a play whose name's words name
    the `.env`, are not in `_env_vars`. Why: the shipped deploy reads the portal's file so, and a pin that read `.env`
    as a prefix would fail it (fold 13's own row); and the PLAY banner prints a name's words, which read no file, so a
    pin that read a name whole would fail a play that only says what it writes (fold 14's own row)."""
    monkeypatch.setitem(_LEAK_SHAPES, "_row", ([], {"name": "render docker/.env",
                                                    "vars": {"_h": "{{ stack_dir }}/.env.homepage", "_p": "a.envx"}},
                                               {}, None, "unlogged"))
    assert _env_vars(_leak_tree(tmp_path, "_row")) == []


def test_a_link_under_the_stack_directory_fails_the_pin(tmp_path):
    """A symbolic link under the stack's directory, to a file or to a directory, at any depth, is in `_links_under`,
    which the URL's pin holds empty under docker/; a plain file is not. Why: a link to the `.env` by another name,
    read by a debug's file lookup, printed the URL with every row green, as the pin reads a `.env` by its name
    (fold 12's read 181, S5)."""
    (tmp_path / "sub").mkdir()
    (tmp_path / ".env").write_text("U=x\n", encoding="utf-8")
    (tmp_path / "plain.txt").write_text("x\n", encoding="utf-8")
    assert _links_under(str(tmp_path)) == []
    try:
        os.symlink(tmp_path / ".env", tmp_path / "sub" / "settings.txt")
        os.symlink(tmp_path / "sub", tmp_path / "again", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("no symbolic links here: %s" % type(exc).__name__)
    found = [p.replace("\\", "/").rsplit("/", 2)[-2:] for p in _links_under(str(tmp_path))]
    assert sorted(found) == sorted([[tmp_path.name, "again"], ["sub", "settings.txt"]]), found


def test_a_link_the_tree_tracks_anywhere_fails_the_pin(tmp_path):
    """A symbolic link git's index holds anywhere in the tree, outside docker/ too, is in `_tracked_links`, and so in
    `_tree_links` and `_link_leaks`, which the URL's pin holds empty over the repository; a tracked plain file is not;
    and an untracked link under docker/ is in `_link_leaks` too, first. Why: a link outside docker/ to docker/.env,
    read by a lineinfile under `--check --diff` and by a file lookup, printed the URL with every row green, as the pin
    read links under docker/ alone (fold 13's read 190, S-5); validate's tree-holds-regular-files-only step refuses
    one too, where validate runs (191); and the pin reads both halves through `_link_leaks`, so a body that drops
    either fails here (fold 15's read 211, N-4)."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("no git here")
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / ".env").write_text("U=x\n", encoding="utf-8")
    (tmp_path / "plain.txt").write_text("x\n", encoding="utf-8")
    try:
        os.symlink(os.path.join("docker", ".env"), tmp_path / "settings.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip("no symbolic links here: %s" % type(exc).__name__)
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run([git, "-C", str(tmp_path)] + argv, check=True, capture_output=True)
    assert _tracked_links(str(tmp_path)) == ["settings.txt"]
    assert _tree_links(str(tmp_path)) == ["settings.txt"]
    assert _link_leaks(str(tmp_path)) == ["settings.txt"]
    os.symlink(".env", tmp_path / "docker" / "again")
    assert [p.replace("\\", "/").rsplit("/", 1)[-1] for p in _link_leaks(str(tmp_path))] == ["again", "settings.txt"]


def test_tracked_links_reads_the_index_through_a_linked_path(tmp_path):
    """`_tracked_links` reads git's index of a work tree reached by a path that is a symbolic link (macOS's `/tmp`, a
    bind, a linked checkout), as it does by the tree's own path. Why: git prints the resolved top, so a top check
    without `realpath` returns None there, and `_tree_links` falls back to the files on disk, which list a venv's links
    too, a false refusal, and, where git checks a link out as a plain file (`core.symlinks` false), none of the tracked
    ones, a tracked link to docker/.env passing the pin; no row held the `realpath` (fold 16's read 220, N-5)."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("no git here")
    repo = tmp_path / "repo"
    (repo / "docker").mkdir(parents=True)
    (repo / "docker" / ".env").write_text("U=x\n", encoding="utf-8")
    try:
        os.symlink(os.path.join("docker", ".env"), repo / "settings.txt")
        os.symlink(repo, tmp_path / "alias", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("no symbolic links here: %s" % type(exc).__name__)
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run([git, "-C", str(repo)] + argv, check=True, capture_output=True)
    assert _tracked_links(str(tmp_path / "alias")) == ["settings.txt"]


@pytest.mark.parametrize("where", ["no-git", "inside-another"])
def test_a_link_in_a_tree_with_no_git_fails_the_pin(tmp_path, monkeypatch, where):
    """In a tree that is not the top of a git work tree, a copy without its `.git` or one untracked inside another
    repository's work tree, `_tracked_links` says None, and `_tree_links`, and so `_link_leaks`, which the URL's pin
    holds empty over the repository, list each symbolic link under it, as `_links_under` does. Why: the pin's `assert
    not _tracked_links(_ROOT)` passed on that None, so a link to docker/.env in a copy without its `.git` passed the
    pin, which said nothing, where validate's step says it checked nothing (fold 14's read 200, N-1); and in a copy
    inside another work tree, git read that tree's index, which lists none of the copy's files, and the pin passed
    (fold 15's read 211, N-4)."""
    git = shutil.which("git")
    if where == "inside-another" and git is None:
        pytest.skip("no git here")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    tree = tmp_path / "copy" if where == "inside-another" else tmp_path
    (tree / "docker").mkdir(parents=True)
    (tree / "docker" / ".env").write_text("U=x\n", encoding="utf-8")
    (tree / "plain.txt").write_text("x\n", encoding="utf-8")
    if where == "inside-another":
        subprocess.run([git, "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    try:
        os.symlink(os.path.join("docker", ".env"), tree / "settings.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip("no symbolic links here: %s" % type(exc).__name__)
    assert _tracked_links(str(tree)) is None
    assert [p.replace("\\", "/").rsplit("/", 1)[-1] for p in _tree_links(str(tree))] == ["settings.txt"]
    assert [p.replace("\\", "/").rsplit("/", 1)[-1] for p in _link_leaks(str(tree))] == ["settings.txt"]


@pytest.mark.parametrize("where", ["play", "block"])
def test_a_file_the_deploy_reads_that_sets_module_defaults_fails_the_pin(tmp_path, monkeypatch, where):
    """A file the walk reads that sets `module_defaults`, on a play or on a block, is in `_module_defaults`, which the
    URL's pin holds empty. Why: a `module_defaults` may hand a writer that names no file the stack's `.env` as its
    own, which `--check --diff` prints, past `_env_writers` (fold 11's read 171, S2)."""
    defaults = {"ansible.builtin.copy": {"dest": "{{ stack_dir }}/.env"}}
    block = {"name": "b", "module_defaults": defaults, "block": [{"name": "c", "ansible.builtin.debug": {"msg": "x"}}]}
    shape = ([], {"module_defaults": defaults}) if where == "play" else ([block], {})
    monkeypatch.setitem(_LEAK_SHAPES, "_row", shape + ({}, None, "unlogged"))
    w = _leak_tree(tmp_path, "_row")
    assert [f.replace("\\", "/").rsplit("/", 1)[-1] for f in _module_defaults(w)] == ["deploy-stack.yml"]
