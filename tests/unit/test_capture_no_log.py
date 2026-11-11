"""Every backup capture's write, and the read it writes, carries `no_log: true`, but for a named exemption whose reason
is a fact about the file read (I-13).

WHY: a capture is the device's own configuration, and most carry its secrets: password hashes, PSKs, keys, a
cloud-init password. Its write is a `copy` of the read's answer, so `--check --diff` prints a capture whole as the
diff (the new one where the read runs in a dry-run, the last one where it does not), into the ansible log and the
Semaphore job's output, and a `-v` run prints the read's answer. Every class's write and read carried `no_log` but
one: docker_host's container manifest, whose `docker ps -a` prints each container's command line (truncated), where a
credential passed as an argument can show. A rule kept by each role's author, with no check, let that one through
(CLAUDE.md: never log a secret; SECURITY.md C2).

What the guard holds, as it finds them (tests/unit/test_backup_empty_capture_refused.py finds the writes the same
way): every task of a role's task files (.yml or .yaml), at any depth, whose arguments name `config_backup_dir` or a
fact a set_fact in the same file builds from it, is a write; a task in the same file whose register the write's own
arguments name is its read. Each is checked on its own, so two tasks of one name are two tasks (the I-13 reads' S1: a
second write named like the first escaped the check). `no_log` counts as the literal `true` on the task itself, or,
for an include, in its `apply:`: a `no_log` on an include_tasks does not reach the tasks it includes (measured, both
cores). Not held here, and so not claimed: a path built in role defaults, a block's vars or a fact of a fact, and a
read whose answer reaches the write through a set_fact or a template.
"""
import glob
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INCLUDES = ("include_tasks", "ansible.builtin.include_tasks", "include_role", "ansible.builtin.include_role")

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


def _hidden(task):
    """Whether the task's output is hidden: `no_log` the literal true on the task, or, for an include, in its
    `apply:` (an include's own `no_log` does not reach the tasks it includes)."""
    include = next((task[k] for k in INCLUDES if k in task), None)
    if include is not None:
        return isinstance(include, dict) and (include.get("apply") or {}).get("no_log") is True
    return task.get("no_log") is True


def _captures():
    """[(role, the write, [each task whose register the write's arguments read])] for every role task that names
    `config_backup_dir` or a fact a set_fact in the same file builds from it, but the set_facts and the
    `file: state=directory` that makes the directory. A list, so a second task of a name is never folded into the
    first; every task that registers a name the write reads counts as its read."""
    out = []
    pattern = os.path.join(ROOT, "ansible", "roles", "*", "tasks", "**", "*.y*ml")
    for path in sorted(glob.glob(pattern, recursive=True)):
        role = os.path.relpath(path, os.path.join(ROOT, "ansible", "roles")).split(os.sep)[0]
        with open(path, encoding="utf-8") as fh:
            tasks = [t for t in _walk(yaml.safe_load(fh)) if isinstance(t, dict)]
        facts = {k for t in tasks for k, v in (t.get("ansible.builtin.set_fact") or {}).items()
                 if "config_backup_dir" in str(v)}
        registers = {}
        for t in tasks:
            if t.get("register"):
                registers.setdefault(t["register"], []).append(t)
        for t in tasks:
            if "block" in t or "ansible.builtin.set_fact" in t:
                continue
            if (t.get("ansible.builtin.file") or {}).get("state") == "directory":
                continue
            args = {k: v for k, v in t.items() if k not in ("name", "when", "register", "delegate_to", "no_log")}
            text = list(_strings(args))
            if any("config_backup_dir" in s or any(f in s for f in facts) for s in text):
                reads = [r for name, rs in registers.items() for r in rs
                         if r is not t and any(re.search(r"\b%s\b" % re.escape(name), s) for s in text)]
                out.append((role, t, reads))
    return out


def _exempt(role, task, reads=None):
    """Whether EXEMPT covers this task: its (role, name) is listed and the file it reads is the one listed (for a
    write, the file every read it writes reads). Judged per task, so a task that borrows a listed name to read another
    file is not covered."""
    entry = EXEMPT.get((role, task.get("name")))
    if entry is None:
        return False
    if reads is not None:
        return bool(reads) and all(_reads_file(r) == entry[0] for r in reads)
    return _reads_file(task) == entry[0]


def test_every_capture_write_and_its_read_carry_no_log_or_are_exempt():
    """Each capture's write, and each task whose register the write reads (the read whose answer it writes), hides its
    output (`no_log: true` on the task, or in an include's `apply:`), or is covered by EXEMPT for the file it reads.
    Every task is checked, a second of the same name too. Guards a capture printed whole by `--check --diff` or a `-v`
    run, as docker_host's container manifest was (I-13: its read and write had no `no_log`); a new class's backup
    added without it; a second capture written under a name an earlier write already has, or a read registering a
    name a later read reuses (the I-13 reads' S1, both of which escaped a check keyed by name); and a `no_log` on an
    include_tasks, which does not hide the write it includes."""
    found = _captures()
    assert found, "no capture write found: the discovery is broken"
    bare = []
    for role, write, reads in found:
        if not _hidden(write) and not _exempt(role, write, reads):
            bare.append((role, write.get("name")))
        for read in reads:
            if not _hidden(read) and not _exempt(role, read):
                bare.append((role, read.get("name")))
    assert not bare, sorted(set(bare))


def test_each_exemption_names_a_task_that_exists_and_the_file_it_reads():
    """Each EXEMPT entry is a capture write or its read that exists, and every task under that (role, name) lacks
    `no_log` and reads the file named (a `slurp`'s `src`, or, for a write, the file every read it writes reads).
    Guards an exemption outliving its task (a renamed task the exemption no longer reaches, or one now `no_log`
    anyway), and a reason that stopped being true: a role that comes to read another file under the same task name, a
    second task taking a listed name among them, keeps no exemption. The file is read from the task's `src`, never its
    name, which names the file whatever it reads."""
    found = _captures()
    tasks = {}
    for role, write, reads in found:
        tasks.setdefault((role, write.get("name")), []).append((write, reads))
        for read in reads:
            tasks.setdefault((role, read.get("name")), []).append((read, None))
    for key, (named, _why) in EXEMPT.items():
        assert key in tasks, key
        for task, reads in tasks[key]:
            assert not _hidden(task), (key, "no longer needs the exemption")
            assert _exempt(key[0], task, reads), (key, named, _reads_file(task), [_reads_file(r) for r in reads or []])
