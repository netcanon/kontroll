"""Which playbooks print the run id: each importer of `_log-run-id.yml` imports it first, and once (I-15 read B, N-5).

WHY. The stamp prints `kontroll_run_id=…`, the key that correlates one operation across the ansible log, the Semaphore
job and the script run-logs (docs/logging-architecture.md, Layer 0). It prints only in a run of a playbook that
imports it. The integration rows run it through backup-configs.yml (by way of the fleet preamble) and
reload-observability.yml, so dropping either import fails a row. With deploy-stack.yml's or promote.yml's import
dropped, the whole non-e2e suite passed on both cores, while Layer 0 names both as importers. This reads each
playbook's entries as YAML, judged by the path each import runs, the same walk the key-cleanup pin uses
(test_onboard_ssh_key.py).
"""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOKS = os.path.join(ROOT, "ansible", "playbooks")
STAMP = "_log-run-id.yml"
PREAMBLE = "_fleet-preamble.yml"
DIRECT = {PREAMBLE, "deploy-stack.yml", "promote.yml", "reload-observability.yml"}
FLEET = {"ping.yml", "backup-configs.yml", "wire-logging.yml", "install-node-exporter.yml"}
IMPORT_KEYS = {"name", "import_playbook", "ansible.builtin.import_playbook"}


def _imported(entry):
    """The playbook an entry imports, or None for a play."""
    if not isinstance(entry, dict):
        return None
    return next((str(v) for k, v in entry.items() if k == "import_playbook" or k.endswith(".import_playbook")), None)


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


def test_each_importer_of_the_stamp_imports_it_first_and_once():
    """The preamble, deploy-stack.yml, promote.yml and reload-observability.yml import the stamp, as their first entry,
    once, and the import carries only its name: a `when:`, `tags: [never]` or `vars:` on it would skip or rename the
    stamp with every row green. First, so the id is printed before anything the run does; once, since a second import
    prints a second id, which can differ by a second (read B's N-4 measured two in one reload run)."""
    found = _playbooks()
    importers = {rel for rel, (_doc, resolved) in found.items() if STAMP in resolved}
    assert DIRECT <= importers, sorted(importers)
    for rel in importers:
        doc, resolved = found[rel]
        assert resolved[0] == STAMP and resolved.count(STAMP) == 1, (rel, resolved)
        assert set(doc[0]) <= IMPORT_KEYS, (rel, doc[0])


def test_each_fleet_playbook_is_stamped_through_the_preamble_first():
    """Every playbook that imports the fleet preamble imports it as its first entry and once, with nothing but its
    name on it, and does not import the stamp itself as well (the preamble's first entry already does, so a fleet run
    would print two ids). The fleet playbooks the preamble serves are among them."""
    found = _playbooks()
    fleet = {rel for rel, (_doc, resolved) in found.items() if PREAMBLE in resolved and rel != PREAMBLE}
    assert FLEET <= fleet, sorted(fleet)
    for rel in fleet:
        doc, resolved = found[rel]
        assert resolved[0] == PREAMBLE and resolved.count(PREAMBLE) == 1, (rel, resolved)
        assert set(doc[0]) <= IMPORT_KEYS, (rel, doc[0])
        assert STAMP not in resolved, (rel, resolved)
