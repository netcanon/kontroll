"""Every backup capture's write, and the read it writes, carries `no_log: true`, but for a named exemption whose reason
is a fact about the file read (I-13).

WHY: a capture is the device's own configuration, and most carry its secrets: password hashes, PSKs, keys, a
cloud-init password. Its write is a `copy` of the read's answer, so `--check --diff` prints the whole capture as the
diff, into the ansible log and the Semaphore job's output, and a `-v` run prints the read's answer. Every class's
write and read carried `no_log` but one: docker_host's container manifest, whose `docker ps -a` prints each
container's command line (truncated), where a credential passed as an argument shows. A rule kept by each role's
author, with no check, let that one through (CLAUDE.md: never log a secret; SECURITY.md C2). The writes are found as
tests/unit/test_backup_empty_capture_refused.py finds them: any task of a role's task files, at any depth, whose
arguments name `config_backup_dir` or a fact built from it; a new class's write is held without being listed here.
"""
import glob
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (role, task name) -> (the file the task reads, or whose read it writes; why that file's text may be shown).
EXEMPT = {
    ("logging_journald_remote", "Read the current journald.conf (rollback reference)"):
        ("journald.conf", "journald's own settings, which name no credential (the upload's are journal-upload.conf's)"),
    ("logging_journald_remote", "Write the journald.conf capture"): ("journald.conf", "as its read"),
    ("logging_file_tail_ssh", "Read authorized_keys"):
        ("authorized_keys", "public keys and their options, no private half (its write is no_log all the same)"),
    ("openwrt_discovery_key", "Read authorized_keys"):
        ("authorized_keys", "public keys and their options, no private half (its write is no_log all the same)"),
}


def _walk(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always"):
            if key in node:
                yield from _walk(node[key])


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def _reads_file(task):
    """The base name of the file a `slurp` reads, or None for any other task."""
    src = (task.get("ansible.builtin.slurp") or {}).get("src")
    return os.path.basename(str(src)) if src else None


def _captures():
    """{(role, write's name): (the write, [each task whose register the write's arguments read])} for every role
    task that names `config_backup_dir` or a fact a set_fact in the same file builds from it, but the set_facts and
    the `file: state=directory` that makes the directory."""
    out = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "ansible", "roles", "*", "tasks", "**", "*.yml"), recursive=True)):
        role = os.path.relpath(path, os.path.join(ROOT, "ansible", "roles")).split(os.sep)[0]
        with open(path, encoding="utf-8") as fh:
            tasks = [t for t in _walk(yaml.safe_load(fh)) if isinstance(t, dict)]
        facts = {k for t in tasks for k, v in (t.get("ansible.builtin.set_fact") or {}).items()
                 if "config_backup_dir" in str(v)}
        registers = {t["register"]: t for t in tasks if t.get("register")}
        for t in tasks:
            if "block" in t or "ansible.builtin.set_fact" in t:
                continue
            if (t.get("ansible.builtin.file") or {}).get("state") == "directory":
                continue
            args = {k: v for k, v in t.items() if k not in ("name", "when", "register", "delegate_to", "no_log")}
            text = list(_strings(args))
            if any("config_backup_dir" in s or any(f in s for f in facts) for s in text):
                reads = [registers[r] for r in registers
                         if r != t.get("register") and any(re.search(r"\b%s\b" % re.escape(r), s) for s in text)]
                out[(role, t.get("name"))] = (t, reads)
    return out


def test_every_capture_write_and_its_read_carry_no_log_or_are_exempt():
    """Each capture's write, and each task whose register the write reads (the read whose answer it writes), has
    `no_log: true`, or is named in EXEMPT. Guards a capture printed whole by `--check --diff` or a `-v` run, as
    docker_host's container manifest was (I-13: its read and write had no `no_log`), and a new class's backup
    added without it."""
    found = _captures()
    assert found, "no capture write found: the discovery is broken"
    bare = []
    for (role, name), (write, reads) in sorted(found.items()):
        for task in [write] + reads:
            if task.get("no_log") is not True and (role, task.get("name")) not in EXEMPT:
                bare.append((role, task.get("name")))
    assert not bare, bare


def test_each_exemption_names_a_task_that_exists_and_the_file_it_reads():
    """Each EXEMPT entry is a capture write or its read that exists and lacks `no_log`, and the file it names is the
    file the task reads (a `slurp`'s `src`), or, for a write, the file every read it writes reads. Guards an
    exemption outliving its task (a renamed task the exemption no longer reaches, or one now `no_log` anyway), and a
    reason that stopped being true: a role that comes to read another file under the same task name keeps no
    exemption. The file is read from the task's `src`, never its name, which names the file whatever it reads."""
    found = _captures()
    reads_of = {(role, w.get("name")): rs for (role, _), (w, rs) in found.items()}
    tasks = {(role, t.get("name")): t for (role, _), (write, reads) in found.items() for t in [write] + reads}
    for key, (named, _why) in EXEMPT.items():
        assert key in tasks, key
        task = tasks[key]
        assert task.get("no_log") is not True, (key, "no longer needs the exemption")
        if key in reads_of:
            assert reads_of[key] and all(_reads_file(r) == named for r in reads_of[key]), (key, named)
        else:
            assert _reads_file(task) == named, (key, named, _reads_file(task))
