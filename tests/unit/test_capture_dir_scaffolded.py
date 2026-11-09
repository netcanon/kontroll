"""A freshly scaffolded instance can actually run backup-configs.yml.

F-BACKUPDIR (2026-07-28 fleet-restore review). `config_backup_dir` is referenced throughout
ansible/playbooks/backup-configs.yml — starting with the directory it writes captures into — and its only inventory
default was `instance/inventory/group_vars/all.yml`. But `instance.example/inventory/` shipped only `hosts.yml`, so
**every** box built by `kontroll-init --fresh` had it undefined and the nightly config backup could never run there.

The defect was invisible for the worst possible reason: the workstation checkout HAS the file (the homelab
tine commits a real instance/ overlay), so it works everywhere a developer looks and fails only on a real
freshly-installed box — the same shape as the fresh-node dry-run bug found the same day. Both are cases where
the development environment carries state that a new install does not.

Why the default lives in instance.example rather than ansible/playbooks/group_vars/: playbook-adjacent
group_vars OUTRANK inventory group_vars for `all`, so defining it there would silently override whatever an
instance set — the opposite of an overridable default. That precedence is the load-bearing reason for the
file's location, so it is asserted here.
"""
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_EXAMPLE_GV = os.path.join(_ROOT, "instance.example", "inventory", "group_vars", "all.yml")
_PLAYBOOK_GV = os.path.join(_ROOT, "ansible", "playbooks", "group_vars", "all.yml")
_BACKUP_PLAY = os.path.join(_ROOT, "ansible", "playbooks", "backup-configs.yml")


def test_the_scaffold_ships_a_capture_dir_default():
    """The fix. Without this file a fresh-init'd box has no config_backup_dir at all."""
    assert os.path.isfile(_EXAMPLE_GV), \
        "instance.example must ship inventory/group_vars/all.yml or a fresh box cannot run backup-configs"
    doc = yaml.safe_load(open(_EXAMPLE_GV, encoding="utf-8"))
    assert "config_backup_dir" in doc, "the scaffolded default must define config_backup_dir"
    assert str(doc["config_backup_dir"]).strip(), "and it must be non-empty"


def test_the_scaffolder_will_actually_copy_it():
    """kontroll-init walks instance.example with os.walk and skips the `secrets` subtree. A nested
    inventory/group_vars/ path must not be caught by that skip, or the file ships but never lands."""
    src = open(os.path.join(_ROOT, "scripts", "kontroll-init.py"), encoding="utf-8").read()
    assert "os.walk(src)" in src, "the scaffold must recurse, or a nested default never reaches instance/"
    assert 'parts[0] == "secrets"' in src, \
        "only the secrets subtree may be skipped — a broader skip would silently drop inventory/group_vars"
    assert "os.makedirs(os.path.dirname(target), exist_ok=True)" in src, \
        "intermediate dirs must be created or the nested copy fails"


def test_the_default_is_NOT_in_playbook_group_vars():
    """THE PRECEDENCE TRAP. Playbook-adjacent group_vars beat inventory group_vars for `all`, so putting
    config_backup_dir here would override every instance's own setting — turning an overridable default into
    an unoverridable one. The tempting "just define it next to the playbook" fix is the wrong fix."""
    doc = yaml.safe_load(open(_PLAYBOOK_GV, encoding="utf-8")) or {}
    assert "config_backup_dir" not in doc, \
        "playbook group_vars OUTRANK inventory group_vars — defining it here silently overrides the instance"


def test_the_playbook_refuses_with_a_remedy_when_it_is_missing():
    """Boxes built BEFORE this fix still lack the file, and an undefined-variable error names a template
    rather than an action. The refusal must fire as the first task — failing part-way through a fleet capture
    is worse — and must tell the operator where to put it."""
    doc = yaml.safe_load(open(_BACKUP_PLAY, encoding="utf-8"))
    play = next(p for p in doc if str(p.get("name", "")).startswith("Ensure backup dir"))
    first = play["tasks"][0]
    assert "ansible.builtin.assert" in first, "the guard must be the FIRST task in the play"
    that = str(first["ansible.builtin.assert"]["that"])
    assert "is defined" in that and "length > 0" in that, "undefined AND empty must both be refused"
    fail_msg = str(first["ansible.builtin.assert"]["fail_msg"])
    assert "group_vars/all.yml" in fail_msg, "the message must name the file to create"
    assert "-e config_backup_dir" in fail_msg, "and offer the one-off override"
