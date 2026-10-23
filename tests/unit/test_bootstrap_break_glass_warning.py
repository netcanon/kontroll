"""The bootstrap play's break-glass warning counts age recipients per creation rule, not over the whole file.

WHY. ansible/playbooks/bootstrap.yml warns when an instance has no break-glass recipient. Its predicate counted the
distinct `age1…` strings in the whole of `instance/.sops.yaml`, so a control key plus a scoped Semaphore key — which
is on the operational domains only — made two, and the warning was silent while every other domain had one recipient
and a lost control key would destroy them (T11 / S12). Now the play parses the file and names each rule with fewer
than two distinct recipients in any of its key groups. It stays a warning, not a gate: a `--no-break-glass` scratch
instance is allowed.

The template is rendered here with core Jinja on fixtures — the task's comment says it uses no Ansible-only filter,
which is what makes that possible — and the task's own `when:` and message are read from the play.
"""
import os

import jinja2
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CTL = "age1controlkeytestpub000000000000000000000000000000000000"
BG = "age1breakglasskeytestpub0000000000000000000000000000000000"
SEM = "age1semaphorekeytestpub00000000000000000000000000000000000"


def _task():
    """The warning task of the bootstrap play."""
    plays = yaml.safe_load(open(os.path.join(ROOT, "ansible", "playbooks", "bootstrap.yml"), encoding="utf-8"))
    found = [t for p in plays for t in p.get("tasks") or [] if "break-glass" in (t.get("name") or "")]
    assert len(found) == 1, [t.get("name") for t in found]
    return found[0]


def thin(rules):
    """The rules the task names, rendered from its own `_sops_thin` template with `rules` as the parsed file."""
    return jinja2.Environment().from_string(_task()["vars"]["_sops_thin"]).render(_sops_rules=rules)


def _rule(name, *groups):
    return {"path_regex": "(^|/)%s\\.sops\\.ya?ml$" % name, "key_groups": [{"age": list(g)} for g in groups]}


def test_the_warning_reads_the_instance_file_and_fires_on_any_thin_rule():
    """The task parses `instance/.sops.yaml` (the instance's own file, never the template) into `_sops_rules`, fires
    when `_sops_thin` names any rule, puts those rules in its message, and stays a `debug` — a warning, never a
    failed task. Guards the predicate drifting back to a whole-file count, or the warning becoming a gate that
    fails the bootstrap of a `--no-break-glass` scratch instance."""
    task = _task()
    assert "instance/.sops.yaml" in task["vars"]["_sops_rules"] and "from_yaml" in task["vars"]["_sops_rules"]
    assert task["when"] == "_sops_thin | length > 0"
    assert "{{ _sops_thin }}" in task["ansible.builtin.debug"]["msg"]
    assert "docs/break-glass.md" in task["ansible.builtin.debug"]["msg"]
    assert "fail" not in task and "assert" not in str(task)


def test_the_warning_counts_recipients_per_rule():
    """A control key plus a scoped Semaphore key on the operational domains only still warns, naming exactly the
    domains with one recipient; two distinct recipients in every rule is silent. Guards the whole-file count — two
    distinct keys in the file — being satisfied by a key that is not on every domain."""
    ops = (CTL, SEM)
    scoped = [_rule("network", ops), _rule("proxmox", ops), _rule("compute", (CTL,)), _rule("dashboards", (CTL,))]
    assert thin(scoped) == "(^|/)compute\\.sops\\.ya?ml$, (^|/)dashboards\\.sops\\.ya?ml$"
    full = [_rule("network", (CTL, BG, SEM)), _rule("proxmox", (CTL, BG, SEM)), _rule("compute", (CTL, BG))]
    assert thin(full) == ""


@pytest.mark.parametrize("rules,names", [
    ([_rule("compute", (CTL, CTL))], ["compute"]),                                  # one key, listed twice
    ([{"path_regex": "x", "key_groups": [{"age": CTL + "," + BG}]}], []),           # sops's comma-separated string
    ([{"path_regex": "x", "key_groups": [{"age": CTL + ", " + CTL}]}], ["x"]),      # … naming one key twice
    ([{"path_regex": "x", "age": CTL + "," + BG}], []),                             # recipients on the rule itself
    ([_rule("compute", (CTL,), (BG,))], ["compute"]),                                # a key group of one: needed too
    ([_rule("compute", (CTL, BG), (SEM,))], ["compute"]),                            # … after a group of two
    ([{"path_regex": "x", "key_groups": [{"pgp": ["F00"]}]}], ["x"]),               # no age recipient at all
    ([{"key_groups": [{"age": [CTL]}]}], ["(a rule with no path_regex)"]),
    ([], []),
], ids=["duplicate", "comma-string", "comma-duplicate", "rule-level-age", "two-groups", "thin-second-group", "no-age",
        "no-path-regex", "no-rules"])
def test_the_count_reads_each_recipient_form_sops_accepts(rules, names):
    """The count de-duplicates a key listed twice, reads sops's comma-separated `age:` string and an `age:` on the
    rule itself, needs two in EACH key group (with several groups sops needs a key from every one, so a group of one
    is a single point of loss, wherever in the rule that group stands), and names a rule with no age recipient or no
    path_regex. Guards a recipient form the count misreads either silencing the warning on a one-key domain or
    naming a domain that has two."""
    got = thin(rules)
    assert [n for n in got.split(", ") if n] == [
        n if n.startswith("(a rule") or n == "x" else "(^|/)%s\\.sops\\.ya?ml$" % n for n in names]
