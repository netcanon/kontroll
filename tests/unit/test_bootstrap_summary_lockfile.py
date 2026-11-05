"""The bootstrap play's summary reads the collection lockfile only where it is.

WHY. ansible/playbooks/bootstrap.yml generates `ansible/collections/requirements.generated.yml` with a `command`
task, which a `--check` run skips, and the file is gitignored, so a fresh tree has none. The summary read it with a
`file` lookup regardless, and `bootstrap.yml --check` on a fresh node failed there (tests/fresh-node-check.sh's
bootstrap step, T27's first real run). The play now stats the file first and looks it up only when it exists; the
summary says why the list is empty when it does not.

The two expressions are read from the play and evaluated here with core Jinja: `lookup` and `from_yaml` are stand-ins,
and the lookup fails the test if it is called for a file the stat did not find.
"""
import os

import jinja2
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCK = "{{ repo_root }}/ansible/collections/requirements.generated.yml"


def _tasks():
    """The bootstrap play's tasks, in order."""
    plays = yaml.safe_load(open(os.path.join(ROOT, "ansible", "playbooks", "bootstrap.yml"), encoding="utf-8"))
    assert len(plays) == 1
    return plays[0]["tasks"], plays[0]["vars"]


def _named(tasks, name):
    found = [i for i, t in enumerate(tasks) if t.get("name") == name]
    assert len(found) == 1, name
    return found[0]


def _expr(s):
    """The one Jinja expression a `"{{ … }}"` value holds."""
    s = s.strip()
    assert s.startswith("{{") and s.endswith("}}") and s.count("{{") == 1, s
    return s[2:-2]


def _env(lockfile_text):
    """Core Jinja with stand-ins for Ansible's `lookup('file', …)` and `from_yaml`. With `lockfile_text` None the
    lookup fails the test: the play must not read a file its stat did not find."""
    calls = []

    def lookup(kind, path):
        calls.append((kind, path))
        assert lockfile_text is not None, "the lockfile was looked up although the stat found none"
        return lockfile_text

    env = jinja2.Environment()
    env.globals["lookup"] = lookup
    env.filters["from_yaml"] = yaml.safe_load
    return env, calls


def _summary_line(tasks):
    msg = tasks[_named(tasks, "Summary")]["ansible.builtin.debug"]["msg"]
    found = [m for m in msg if m.startswith("collections ")]
    assert len(found) == 1, msg
    return found[0]


def test_the_lockfile_is_looked_for_before_the_summary_reads_it():
    """A `stat` of the play's own lockfile path, registered as `lockfile_stat`, runs before the task that computes
    the summary facts, and that task's lookup is guarded by it. Guards the stat checking another path, or running
    after the read, which would leave the lookup unguarded on a fresh tree."""
    tasks, play_vars = _tasks()
    assert play_vars["req_generated"] == LOCK
    i = _named(tasks, "Look for the collection lockfile")
    j = _named(tasks, "Compute summary facts (secret files present + the resolved collection names)")
    assert i < j
    assert tasks[i]["ansible.builtin.stat"] == {"path": "{{ req_generated }}"}
    assert tasks[i]["register"] == "lockfile_stat"
    assert "when" not in tasks[i] and "check_mode" not in tasks[i]


def test_a_fresh_tree_has_no_lockfile_and_the_summary_says_why():
    """With no lockfile (a `--check` run on a fresh tree), the collection names are an empty list, no lookup is
    made, and the summary's collections line says the dry-run does not generate the file. Guards the summary's
    `file` lookup failing `bootstrap.yml --check` on a fresh node, as it did before."""
    tasks, _ = _tasks()
    j = _named(tasks, "Compute summary facts (secret files present + the resolved collection names)")
    env, calls = _env(None)
    st = {"stat": {"exists": False}}
    colls = env.compile_expression(_expr(tasks[j]["ansible.builtin.set_fact"]["resolved_colls"]))(
        req_generated="/x/requirements.generated.yml", lockfile_stat=st)
    assert colls == [] and calls == []
    line = env.from_string(_summary_line(tasks)).render(resolved_colls=colls, lockfile_stat=st)
    assert line.startswith("collections     : ") and "--check" in line and "does not generate" in line, line


def test_a_lockfile_present_is_read_and_listed():
    """With the lockfile there, the names are its collections in order, read through one `file` lookup of the
    play's path, and the summary lists them. Guards the guard hiding the collections on a configured node."""
    tasks, _ = _tasks()
    j = _named(tasks, "Compute summary facts (secret files present + the resolved collection names)")
    env, calls = _env("---\ncollections:\n  - name: community.general\n    version: 1.0.0\n  - name: cisco.ios\n")
    st = {"stat": {"exists": True}}
    colls = env.compile_expression(_expr(tasks[j]["ansible.builtin.set_fact"]["resolved_colls"]))(
        req_generated="/x/requirements.generated.yml", lockfile_stat=st)
    assert colls == ["community.general", "cisco.ios"]
    assert calls == [("file", "/x/requirements.generated.yml")]
    line = env.from_string(_summary_line(tasks)).render(resolved_colls=colls, lockfile_stat=st)
    assert line == "collections     : community.general, cisco.ios", line
