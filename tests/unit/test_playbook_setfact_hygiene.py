"""No `set_fact` key may reference another key set by the SAME task.

Ansible templates every key of a single `set_fact` against the context as it was BEFORE the task ran. So this
looks entirely reasonable and fails at runtime:

    ansible.builtin.set_fact:
      _requested_services: "{{ stack_services | reject(...) | list }}"
      _up_services:        "{{ _requested_services + [...] }}"   # '_requested_services' is undefined

Live-caught 2026-07-28 on the floating-prod build, in exactly that shape. What makes it worth a guard rather
than a lesson is how far it travelled: `ansible-playbook --syntax-check` passed, `ansible-lint` passed at
production profile, and the unit tests covering the expressions passed too — because they evaluated the two
Jinja strings with the intermediate injected, which is precisely what Ansible does not do. Everything that
parses rather than evaluates was happy. Only the deploy failed.

The fix is always the same and always cheap: make the intermediate a task `vars:` entry, which IS in scope for
module args. This sweeps every playbook AND every role task/handler file (the copy-me role template included) —
the role files are what every device play executes, and a role is where the next one will be written — so it is
caught in CI instead of on a control node mid-deploy.
"""
import os
import re

import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PLAYBOOKS = os.path.join(_ROOT, "ansible", "playbooks")
_ROLES = os.path.join(_ROOT, "ansible", "roles")


def _playbooks():
    """Every YAML file under ansible/playbooks/, at any depth (a playbook moved into a subdirectory stays swept;
    the playbook-adjacent group_vars file is a mapping with no tasks, which the walker passes over)."""
    found = []
    for base, _dirs, files in os.walk(_PLAYBOOKS):
        rel = os.path.relpath(base, _ROOT).replace(os.sep, "/")
        found += ["%s/%s" % (rel, f) for f in files if f.endswith((".yml", ".yaml"))]
    return sorted(found)


def _role_task_files():
    """Every YAML file under a role's tasks/ or handlers/ directory, at any depth, dot-directories included (the
    role TEMPLATE lives in `.role-template`, which a `*` glob would skip)."""
    found = []
    for base, _dirs, files in os.walk(_ROLES):
        parts = os.path.relpath(base, _ROLES).replace(os.sep, "/").split("/")
        if len(parts) >= 2 and parts[1] in ("tasks", "handlers"):
            found += ["ansible/roles/%s/%s" % ("/".join(parts), f) for f in files if f.endswith((".yml", ".yaml"))]
    return sorted(found)


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers"):
            if key in node:
                yield from _tasks(node[key])


def _self_references(task):
    """(key, sibling) pairs where a set_fact key's Jinja names another key of the same set_fact."""
    fact = task.get("ansible.builtin.set_fact") or task.get("set_fact")
    if not isinstance(fact, dict):
        return []
    # cacheable is a set_fact OPTION, not a fact being defined
    keys = [k for k in fact if k != "cacheable"]
    found = []
    for key in keys:
        text = str(fact[key])
        for other in keys:
            if other == key:
                continue
            if re.search(r"(?<![\w.])%s\b" % re.escape(other), text):
                found.append((key, other))
    return found


@pytest.mark.parametrize("name", _playbooks() + _role_task_files())
def test_no_set_fact_key_references_a_sibling_key(name):
    """The regression, swept across every playbook and every role task/handler file. A sibling reference is
    undefined at render time, so this is always a bug — not a style preference — and the failure only ever shows up
    on a real run. Role files are bare task lists; the walker takes them as they are."""
    with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh.read())
    offenders = []
    for task in _tasks(doc):
        for key, other in _self_references(task):
            offenders.append("%s: set_fact '%s' references sibling '%s' (undefined at render — make '%s' a "
                             "task var)" % (task.get("name", "<unnamed>"), key, other, other))
    assert not offenders, "%s\n  %s" % (name, "\n  ".join(offenders))


def test_the_scanner_actually_catches_a_planted_sibling_reference():
    """A sweep that has never been observed to fail proves nothing — this plants the exact live-caught shape and
    requires the detector to flag it."""
    planted = {
        "name": "planted",
        "ansible.builtin.set_fact": {
            "_requested_services": "{{ stack_services | reject('equalto', 'x') | list }}",
            "_up_services": "{{ _requested_services + ['caddy'] }}",
        },
    }
    assert ("_up_services", "_requested_services") in _self_references(planted)


def test_the_scanner_does_not_flag_an_unrelated_name_or_a_substring():
    """Guards the guard against noise, which is how a sweep gets disabled: a key that merely CONTAINS another
    key's name as a substring (`_up_services_extra`) is not a reference, and neither is a same-named var read
    from outside this task."""
    ok = {
        "name": "fine",
        "ansible.builtin.set_fact": {
            "_up": "{{ stack_services }}",
            "_up_extra": "{{ something_else }}",
        },
    }
    assert _self_references(ok) == []
    substring = {
        "name": "substring only",
        "ansible.builtin.set_fact": {
            "_svc": "{{ a }}",
            "_svc_list": "{{ _svc_list_from_elsewhere | default([]) }}",
        },
    }
    assert not any(pair[1] == "_svc" for pair in _self_references(substring)), \
        "'_svc_list_from_elsewhere' merely starts with '_svc' — the word boundary must not treat it as a hit"


def test_the_sweep_reaches_the_role_files_and_the_role_template():
    """The swept set holds playbooks, role task files and the copy-me role template's task files — found by walking
    the roles tree, not by a one-level glob. Guards the sweep silently covering nothing of a kind: a `*` glob skips
    the dot-directory the template lives in, and a role that nests its task files would escape a one-level
    pattern; an empty category would let this sweep pass while proving nothing about it."""
    roles = _role_task_files()
    assert any(f.count("/") == 2 for f in _playbooks()), "no playbooks found"
    assert any("/.role-template/tasks/" in f for f in roles), "the role template's task files are not swept"
    assert any("/tasks/" in f and "/.role-template/" not in f for f in roles), "no real role's task files are swept"
    assert all(os.path.exists(os.path.join(_ROOT, f)) for f in roles)
