"""No play-level or task-level `vars:` entry may define a variable in terms of itself.

WHY (the failure this guards — 2026-10-08 review, finding 11): `reload-observability.yml` declared
`prometheus_url: "{{ prometheus_url | default('http://prometheus:9090') }}"` as a PLAY var. That is a recursive
template: resolving `prometheus_url` means templating a value that asks for `prometheus_url`. Older ansible-core
happened to resolve it (the extra-var or an undefined won the lookup); the 2.19 templating engine detects the loop
and fails the play before its first task — and this was the only human reload verb for a freshly promoted scrape
target. The repo already documents the shape as fatal; nothing enforced it.

Scope, deliberately: play `vars:` and task `vars:` (both are lazily templated in the scope they define, so a
self-reference recurses), in every playbook AND in every role task/handler file — plus the top-level keys of a
role's `defaults/` and `vars/` files and of the playbook-adjacent `group_vars`, where a variable defined in terms
of itself is the same recursion. `set_fact`
keys are OUT of scope — a set_fact templates against the PRE-task context, which
is exactly what makes `kontroll_storage_root: "{{ kontroll_storage_root | default('/var/lib/kontroll') }}"` a
correct idiom there. A sweep that flagged those would be disabled within the week, so it has to be precise.
"""
import glob
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = sorted(glob.glob(os.path.join(ROOT, "ansible", "playbooks", "**", "*.yml"), recursive=True))


def _role_files(*kinds):
    """Every YAML file under a role's <kind>/ directory (tasks, handlers, defaults, vars), at any depth, with the
    dot-directory role TEMPLATE included — os.walk, because a `*` glob skips `.role-template`."""
    roles, found = os.path.join(ROOT, "ansible", "roles"), []
    for base, _dirs, files in os.walk(roles):
        parts = os.path.relpath(base, roles).replace(os.sep, "/").split("/")
        if len(parts) >= 2 and parts[1] in kinds:
            found += [os.path.join(base, f) for f in files if f.endswith((".yml", ".yaml"))]
    return sorted(found)


ROLE_TASK_FILES = _role_files("tasks", "handlers")
ROLE_VAR_FILES = _role_files("defaults", "vars")
# Role subdirectories that may hold YAML which is neither a task list nor a variable mapping. `meta/` is role
# metadata (dependencies, argument specs); `templates/` and `files/` are payload the role ships, not Ansible.
_NOT_SWEPT_ROLE_KINDS = ("meta", "templates", "files")


def _self_referencing_vars(doc):
    """Yield (key, value) for every `vars:` entry — play-level, task-level, block-level — whose value names
    its own key inside a Jinja expression. Pure over a parsed document, so the planted-shape test can feed it.
    Takes a playbook (a list of plays), a role's tasks/handlers file (a bare list of tasks) and a role's
    defaults/vars file (a mapping whose top-level keys are the variables)."""
    def _walk_tasks(tasks):
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            yield from _check(t.get("vars"))
            for k in ("block", "rescue", "always"):
                if isinstance(t.get(k), list):
                    yield from _walk_tasks(t[k])

    def _check(vars_):
        if not isinstance(vars_, dict):
            return
        for key, value in vars_.items():
            if re.search(r"\{\{[^}]*\b%s\b" % re.escape(str(key)), str(value)):
                yield key, value

    if isinstance(doc, dict):                     # a role's defaults/ or vars/ file: the keys ARE the variables
        yield from _check(doc)
        return
    sections = ("pre_tasks", "tasks", "post_tasks", "handlers")
    for item in doc if isinstance(doc, list) else []:
        if not isinstance(item, dict):
            continue
        if "hosts" in item or any(s in item for s in sections):          # a play
            yield from _check(item.get("vars"))
            for section in sections:
                yield from _walk_tasks(item.get(section))
        else:                                     # a bare task: a role's tasks/handlers file is a list of them
            yield from _walk_tasks([item])


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_no_playbook_var_is_defined_in_terms_of_itself():
    """THE SWEEP. Every playbook under ansible/playbooks/, every role task/handler file and every role
    defaults/vars file is parsed, and every play/task/block `vars:` entry — or role default — whose value templates
    its own name is reported with its file. Empty today; the reload play's `prometheus_url` was the one offender
    and now reads the override through a private name. The role files are in the sweep because they are what every
    device play executes: the same recursion there is fatal on the same ansible-core, on a live fleet run."""
    assert PLAYBOOKS, "no playbooks found — the sweep proves nothing"
    assert ROLE_TASK_FILES and any(".role-template" in p for p in ROLE_TASK_FILES), \
        "no role task files (or not the role template's) — the role half of the sweep proves nothing"
    assert ROLE_VAR_FILES, "no role defaults/vars files found — that half of the sweep proves nothing"
    offenders = []
    for path in PLAYBOOKS + ROLE_TASK_FILES + ROLE_VAR_FILES:
        try:
            doc = _load(path)
        except yaml.YAMLError:
            continue                       # syntax is yamllint's job; a non-parsing file cannot define a var
        for key, value in _self_referencing_vars(doc):
            offenders.append("%s: %s: %r" % (os.path.relpath(path, ROOT), key, str(value)[:80]))
    assert offenders == [], "a play/task var names itself (a recursive template, fatal on ansible-core >= 2.19):\n  " \
        + "\n  ".join(offenders)


def test_the_detector_fires_on_the_exact_shape_that_shipped():
    """Planted shape: the literal line the reload play carried. The detector must flag it at play level AND at task
    level, and must flag it even when wrapped in `default()` — that wrapper is precisely what made it look safe. It
    must flag the same shape in the two forms a ROLE holds it: a task's `vars:` in a bare task list (inside a block
    too), and a top-level key of a defaults/vars mapping."""
    play_level = [{"hosts": "localhost", "vars": {"prometheus_url": "{{ prometheus_url | default('http://x') }}"},
                   "tasks": []}]
    task_level = [{"hosts": "localhost", "tasks": [
        {"name": "t", "vars": {"url": "{{ url | default('x') }}"}, "debug": {"msg": "{{ url }}"}}]}]
    nested = [{"hosts": "localhost", "tasks": [{"block": [
        {"name": "t", "vars": {"deep": "{{ (deep | default([])) + [1] }}"}, "debug": {"msg": "x"}}]}]}]
    assert [k for k, _ in _self_referencing_vars(play_level)] == ["prometheus_url"]
    assert [k for k, _ in _self_referencing_vars(task_level)] == ["url"]
    assert [k for k, _ in _self_referencing_vars(nested)] == ["deep"]
    # the two role shapes: a bare task list (with a block), and a defaults/vars mapping
    role_tasks = [{"name": "t", "vars": {"cmd": "{{ cmd | default('show version') }}"}, "debug": {"msg": "x"}},
                  {"block": [{"name": "b", "vars": {"deep": "{{ deep }}"}, "debug": {"msg": "x"}}]}]
    role_defaults = {"backup_capable": "{{ backup_capable | default(true) }}", "other": "{{ backup_capable }}"}
    assert [k for k, _ in _self_referencing_vars(role_tasks)] == ["cmd", "deep"]
    assert [k for k, _ in _self_referencing_vars(role_defaults)] == ["backup_capable"]


def test_the_detector_ignores_set_fact_self_defaults_and_mere_substrings():
    """Precision: a `set_fact` key defaulting itself is the correct idiom (pre-task context) and must NOT be flagged;
    neither may a var whose name is a substring of another var it references (`url` vs `base_url`). A noisy sweep
    gets disabled, so these two must stay quiet."""
    set_fact = [{"hosts": "localhost", "tasks": [
        {"name": "derive", "ansible.builtin.set_fact": {"root": "{{ root | default('/var/lib') }}"}}]}]
    substring = [{"hosts": "localhost", "vars": {"url": "{{ base_url }}/api", "base_url": "http://x"}, "tasks": []}]
    assert list(_self_referencing_vars(set_fact)) == []
    assert list(_self_referencing_vars(substring)) == []


def test_the_reload_play_reads_the_override_through_a_private_name():
    """The specific fix: `reload-observability.yml` keeps `-e prometheus_url=…` as the operator override but its
    play var is `_reload_prometheus_url`, and every task reads that. Guards the one-line revert that reintroduces
    the recursion (it reads naturally and nothing else in the gate evaluates a play var)."""
    path = os.path.join(ROOT, "ansible", "playbooks", "reload-observability.yml")
    doc = _load(path)
    play = next(p for p in doc if isinstance(p, dict) and p.get("tasks"))
    assert "prometheus_url" not in (play.get("vars") or {}), "the play must not define prometheus_url (recursive)"
    assert "prometheus_url" in str(play["vars"].get("_reload_prometheus_url", "")), \
        "the private name must still honour the -e prometheus_url override"
    text = open(path, encoding="utf-8").read()
    assert "{{ prometheus_url }}" not in text, "tasks read the private name, never the raw override"


def test_every_role_yaml_file_is_of_a_kind_the_sweeps_read():
    """Every YAML file under ansible/roles/ (the dot-directory role template included) lies in a subdirectory the
    hygiene sweeps read — tasks/, handlers/, defaults/, vars/ — or in one named above as holding no tasks and no
    variables. Guards a new role layout escaping both sweeps without anyone deciding so: a task file dropped at a
    role's root, or under a directory name the walk does not know, would otherwise be executed by every device play
    and read by no gate. The fix for a failure here is to teach the sweeps the new kind, not to move the file."""
    roles = os.path.join(ROOT, "ansible", "roles")
    swept = {os.path.normpath(p) for p in ROLE_TASK_FILES + ROLE_VAR_FILES}
    unread = []
    for base, _dirs, files in os.walk(roles):
        parts = os.path.relpath(base, roles).replace(os.sep, "/").split("/")
        kind = parts[1] if len(parts) >= 2 else "(the role's root)"
        for f in files:
            path = os.path.normpath(os.path.join(base, f))
            if f.endswith((".yml", ".yaml")) and path not in swept and kind not in _NOT_SWEPT_ROLE_KINDS:
                unread.append(os.path.relpath(path, ROOT).replace(os.sep, "/"))
    assert not unread, "role YAML that no hygiene sweep reads: %s" % ", ".join(sorted(unread))
    assert any(".role-template" in p for p in ROLE_VAR_FILES), "the role template's defaults are not swept"
