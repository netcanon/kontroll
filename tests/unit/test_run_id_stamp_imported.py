"""Which playbooks print the run id, and how the stamp prints it once (I-15 read B N-5; I-41; I-52).

WHY. The stamp prints `kontroll_run_id=…`, the key that correlates one operation across the ansible log, the Semaphore
job and the script run-logs (docs/logging-architecture.md, Layer 0). It prints only in a run of a playbook that
imports it. With deploy-stack.yml's or promote.yml's import dropped, the whole non-e2e suite passed on both cores
(the I-15 read B), and bootstrap.yml and local-canonical.yml imported it not at all, so their runs carried no id
(I-41). These rows read each playbook's entries as YAML, judged by the path each import runs, the same walk the
key-cleanup pin uses (test_onboard_ssh_key.py): every playbook a run can start with imports the stamp or the fleet
preamble first, with no list to keep; each import is first, once and bare; deploy-stack.yml's later import of
local-canonical.yml, the one run that reaches the stamp twice; and the stamp's own shape, which prints once, on the
controller, in a `--tags` run too. The runs that show what a run prints are integration/test_run_id_stamp_runs.py
and, for the real local-canonical.yml, integration/test_local_canonical_as_root.py.
"""
import glob
import os

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(ROOT, "ansible", "playbooks")
STAMP = "_log-run-id.yml"
PREAMBLE = "_fleet-preamble.yml"
FLEET = {"ping.yml", "backup-configs.yml", "wire-logging.yml", "install-node-exporter.yml"}
IMPORT_KEYS = {"name", "import_playbook", "ansible.builtin.import_playbook"}


def _imported(entry):
    """The playbook an entry imports, or None for a play."""
    if not isinstance(entry, dict):
        return None
    return next((str(v) for k, v in entry.items() if k == "import_playbook" or k.endswith(".import_playbook")), None)


def _entries(name):
    """The entries of ansible/playbooks/<name>."""
    with open(os.path.join(PLAYBOOKS, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _playbooks():
    """{path under ansible/playbooks/: (its entries, the path each entry's import runs, or None)} for every playbook."""
    found = {}
    for directory, _dirs, files in os.walk(PLAYBOOKS):
        for name in files:
            if not name.endswith((".yml", ".yaml")):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            if not isinstance(doc, list):
                continue
            resolved = [os.path.relpath(os.path.realpath(os.path.join(directory, i)), os.path.realpath(PLAYBOOKS))
                        .replace(os.sep, "/") if i else None for i in map(_imported, doc)]
            found[os.path.relpath(os.path.join(directory, name), PLAYBOOKS).replace(os.sep, "/")] = (doc, resolved)
    return found


def test_every_playbook_a_run_can_start_with_stamps_first():
    """Each top-level playbook under ansible/playbooks/, `.yml` or `.yaml`, whose name does not start with `_` (the
    underscore files are only imported) has, as its FIRST entry, an import of the stamp or of the fleet preamble, and
    the preamble's own first entry is the stamp. Guards the gap I-41 closed (bootstrap.yml and local-canonical.yml
    started runs with no id) and the next playbook added without the import: the row reads every top-level file, so
    there is no list to keep up. A playbook in a subdirectory is refused by test_group_dispatch_refuses_local.py's
    rule that the tree holds nothing its scan reads past."""
    entry = sorted(os.path.basename(p) for ext in ("*.yml", "*.yaml") for p in glob.glob(os.path.join(PLAYBOOKS, ext))
                   if not os.path.basename(p).startswith("_"))
    assert {"bootstrap.yml", "local-canonical.yml", "deploy-stack.yml", "promote.yml"} <= set(entry), entry
    found = _playbooks()
    for name in entry:
        assert found[name][1][0] in (STAMP, PREAMBLE), "%s does not import the run-id stamp first" % name
    assert found[PREAMBLE][1][0] == STAMP


def test_each_importer_of_the_stamp_imports_it_first_and_once():
    """Every playbook that imports the stamp itself imports it as its first entry, once, and the import carries only
    its name: a `when:`, `tags: [never]` or `vars:` on it would skip or rename the stamp with every row green. First,
    so the id is printed before anything the run does; once, since a second import in one playbook is a second line
    (the stamp's guard makes it print nothing, but nothing needs it). The preamble and the stack playbooks are among
    them."""
    found = _playbooks()
    importers = {rel for rel, (_doc, resolved) in found.items() if STAMP in resolved}
    assert {PREAMBLE, "deploy-stack.yml", "promote.yml", "reload-observability.yml", "bootstrap.yml",
            "local-canonical.yml"} <= importers, sorted(importers)
    for rel in importers:
        doc, resolved = found[rel]
        assert resolved[0] == STAMP and resolved.count(STAMP) == 1, (rel, resolved)
        assert set(doc[0]) <= IMPORT_KEYS, (rel, doc[0])


def test_each_fleet_playbook_is_stamped_through_the_preamble_first():
    """Every playbook that imports the fleet preamble imports it as its first entry and once, with nothing but its
    name on it, and does not import the stamp itself as well (the preamble's first entry already does). The fleet
    playbooks the preamble serves are among them."""
    found = _playbooks()
    fleet = {rel for rel, (_doc, resolved) in found.items() if PREAMBLE in resolved and rel != PREAMBLE}
    assert FLEET <= fleet, sorted(fleet)
    for rel in fleet:
        doc, resolved = found[rel]
        assert resolved[0] == PREAMBLE and resolved.count(PREAMBLE) == 1, (rel, resolved)
        assert set(doc[0]) <= IMPORT_KEYS, (rel, doc[0])
        assert STAMP not in resolved, (rel, resolved)


def test_deploy_stack_imports_local_canonical_after_its_own_stamp():
    """deploy-stack.yml imports the stamp, and later local-canonical.yml, whose own first entry is the stamp: the one
    shipped run that reaches the stamp twice. Guards the premise of the stamp's guard: if this pair changes
    (local-canonical.yml no longer imported, or imported first), the guard's reason and its run rows should be
    revisited."""
    imports = [_imported(e) for e in _entries("deploy-stack.yml")]
    assert imports[0] == STAMP
    assert "local-canonical.yml" in imports[1:]
    assert _imported(_entries("local-canonical.yml")[0]) == STAMP


def test_the_stamp_prints_once_on_the_controller_and_leaves_the_id_alone():
    """The stamp is one play on `all:localhost`: its debug of `kontroll_run_id=` runs once, and only while
    `_kontroll_run_stamped` is unset, and registers what it printed; the task after it sets that fact, once, and
    `_kontroll_run_id`, the printed id (kept where an earlier import set it), and nothing else. Both run on the
    controller (`delegate_to: localhost`) and carry `tags: [always]`. Guards each part: `all` alone misses a run with
    no inventory hosts and `localhost` alone misses `--limit <host>`; a debug without the guard prints again under
    deploy-stack.yml; a stamp that set `kontroll_run_id` as a fact would move the key directory group_vars/all.yml
    names by it (`default('manual', true)`) for the rest of the run; without the delegation a first host whose
    connection plugin is not installed fails the stamp and ends the run, bootstrap's included (I-52); without the tag
    a `--tags` run prints no id. `_kontroll_run_id` is the id the log carries, for the backup summary's per-device
    line (I-65): an unset id is `now()`, so any later read of `kontroll_run_id` would mint another, and a second
    import that took its skipped debug's (empty) answer would lose the first's."""
    plays = _entries(STAMP)
    assert len(plays) == 1 and plays[0]["hosts"] == "all:localhost" and plays[0]["connection"] == "local"
    debug, note = plays[0]["tasks"]
    assert debug["ansible.builtin.debug"]["msg"].startswith("kontroll_run_id={{ kontroll_run_id")
    assert debug["run_once"] is True and debug["when"] == "not (_kontroll_run_stamped | default(false))"
    assert debug["register"] == "_kontroll_run_stamp"
    assert note["ansible.builtin.set_fact"] == {"_kontroll_run_stamped": True, "_kontroll_run_id": (
        "{{ _kontroll_run_id if _kontroll_run_id is defined\n"
        "   else (_kontroll_run_stamp.msg | default('') | regex_replace('^kontroll_run_id=', '')) }}")}
    assert note["run_once"] is True
    for task in (debug, note):
        assert task["delegate_to"] == "localhost" and task["tags"] == ["always"], task
