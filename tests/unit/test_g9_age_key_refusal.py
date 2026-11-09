"""The G9 onboard-gui age-key gate accepts exactly the states that actually work, and its refusal is actionable.

G9 is deliberately fail-closed: deploy-stack never mints the GUI's age key, because a service that can create its
own decryption key is not a trust boundary. That part is right and these tests do not challenge it.

What was wrong was everything around it (live-caught 2026-07-28, fresh-box spin-up):

  1. The gate was a bare `failed_when: not stat.exists` with no `fail_msg`, so the operator got Ansible's generic
     failure plus a stat dict — a correct refusal that does not say what to put there. Spin-up friction is not a
     cosmetic problem for a "floating" instance whose whole premise is being rebuilt often.
  2. "Exists" was never the real precondition. The GUI runs as uid 1001 and mounts the key `:ro`, so a root-owned
     0600 key PASSES an exists-check and then fails at runtime as an opaque SOPS decrypt error, one deploy later
     and far from the cause.
  3. Nor was "readable" (I-22, 2026-11-08): one `up` of onboard-gui without the key leaves Docker's root:root 0755
     DIRECTORY at the path, which exists and is other-readable, so the gate passed it, and its own remedy then
     installed the key into the directory. And the stat described a link rather than the file Docker mounts. And an
     empty file passed (its two readers, 2026-11-08), on which the GUI fails as on a missing key.

Rather than string-match the playbook, these evaluate the gate's real Jinja against synthetic stat results — so
they test the condition's BEHAVIOUR, including the deliberate other-readable escape hatch that keeps a box which
already works that way from being newly broken.
"""
import os
import re

import jinja2
import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEPLOY = os.path.join(_ROOT, "ansible", "playbooks", "deploy-stack.yml")
_GUI_DIR = "/var/lib/kontroll/onboard-gui"


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _gate():
    with open(_DEPLOY, encoding="utf-8") as fh:
        for task in _tasks(yaml.safe_load(fh.read())):
            if "the actual remedy" in str(task.get("name", "")):
                return task["ansible.builtin.assert"]
    raise AssertionError("the G9 remedy assert is gone — if the gate moved, move this test with it")


def _evaluate(stat):
    """Run the gate's real `that:` expressions against a synthetic stat result; return (passed, message). Like
    `ansible.builtin.assert` it stops at the first false leg. Two ways it is laxer, neither of which the shipped gate
    meets (the integration test's pass rows run the real assert): Ansible renders `fail_msg` even when the assert
    passes, and 2.19 refuses a leg whose result is not a boolean."""
    gate = _gate()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx = {"_gui_age_key": {"stat": stat}, "kontroll_gui_dir": _GUI_DIR}
    for expr in gate["that"]:
        if not env.compile_expression(expr)(**ctx):
            return False, env.from_string(gate["fail_msg"]).render(**ctx)
    return True, ""


# The fields ansible.builtin.stat returns that the gate reads. A path that is not there gives `exists` alone; one that
# is gives every type flag and its size (tests/integration/test_g9_age_key_gate_real_stat.py runs the real stat on
# each shape and holds these to it). A regular file's size is the stand-in key's there; the gate reads a size only
# after the regular-file leg, so the other types carry none.
_FILE = {"exists": True, "isreg": True, "isdir": False, "islnk": False, "isfifo": False, "size": 34}
_DIR = {"exists": True, "isreg": False, "isdir": True, "islnk": False, "isfifo": False}
_FIFO = {"exists": True, "isreg": False, "isdir": False, "islnk": False, "isfifo": True}
_MISSING = {"exists": False}


def _mode(uid, gid, octal):
    """The owner/group/mode fields a stat returns for a path with this owner, group and mode."""
    return {"uid": uid, "gid": gid, "rusr": bool(octal & 0o400), "rgrp": bool(octal & 0o040),
            "roth": bool(octal & 0o004)}


# (label, stat, should_pass)
_CASES = [
    ("missing entirely", _MISSING, False),
    ("present and owned by the runtime uid", dict(_FILE, **_mode(1001, 1001, 0o600)), True),
    ("present but root-owned 0600 — the runtime uid cannot read it", dict(_FILE, **_mode(0, 0, 0o600)), False),
    ("present, root-owned, but other-readable", dict(_FILE, **_mode(0, 0, 0o644)), True),
    ("root-owned, group 1001, 0640 — the group bits let the GUI read it", dict(_FILE, **_mode(0, 1001, 0o640)), True),
    ("root-owned, group 1001, 0604 — the group bits apply, and give no read", dict(_FILE, **_mode(0, 1001, 0o604)),
     False),
    ("owned by the runtime uid without its owner read bit (0044)", dict(_FILE, **_mode(1001, 1001, 0o044)), False),
    ("a root:root 0755 directory, as Docker makes one at an `up` without the key", dict(_DIR, **_mode(0, 0, 0o755)),
     False),
    ("a directory the runtime uid owns", dict(_DIR, **_mode(1001, 1001, 0o700)), False),
    ("a named pipe the runtime uid owns", dict(_FIFO, **_mode(1001, 1001, 0o600)), False),
    ("an empty file the runtime uid owns and can read", dict(_FILE, size=0, **_mode(1001, 1001, 0o600)), False),
    ("an empty other-readable file", dict(_FILE, size=0, **_mode(0, 0, 0o644)), False),
]


@pytest.mark.parametrize("label,stat,should_pass", _CASES, ids=[c[0] for c in _CASES])
def test_the_gate_admits_exactly_the_states_that_work(label, stat, should_pass):
    """A key uid 1001 can actually read is the real precondition — not merely a file that exists. The root-owned,
    other-readable case is the deliberate escape hatch: other-readable is sloppy but it genuinely works, and a
    hardening check that breaks a running box on the next deploy teaches operators to skip deploys. The rest are
    I-22 (2026-11-08): a directory exists and its 0755 is other-readable, so the first two legs passed one, and the
    GUI started with its key file a directory, an opaque SOPS failure; only a regular file is a key, and only a
    non-empty one. And readability is the mode bits as the kernel reads them for a process of uid and gid 1001 with
    no other group: owner bits when uid 1001 owns it, group bits when gid 1001 does, else other's, so a 1001-owned
    key without its owner read bit is refused and a root:1001 0640 one passes (ownership alone decided both, the
    wrong way)."""
    passed, _ = _evaluate(stat)
    assert passed is should_pass, "gate %s for: %s" % ("passed" if passed else "refused", label)


def test_the_refusal_distinguishes_missing_from_unreadable():
    """Two different problems with two different fixes (put a key there vs. chown the one that is there). One
    generic message would send the operator down the wrong path for whichever case they hit."""
    _, missing = _evaluate(_MISSING)
    _, unreadable = _evaluate(dict(_FILE, **_mode(0, 0, 0o600)))
    assert "MISSING" in missing
    assert "not readable" in unreadable
    assert missing != unreadable


def test_the_refusal_of_a_directory_says_so_and_how_to_clear_it():
    """A directory needs a third remedy: the install the message gives, run against a directory, copies the key
    INTO it and leaves the gate's path a directory still, which is how I-22's box stayed broken through its own
    fix. So the refusal names the directory, says where it comes from, and gives `sudo rmdir` on the real path,
    which removes only an empty directory: one a key was installed into is refused, not deleted with the key in
    it. When it refuses, the message says to move the copy out first, and for a link to a directory, which `rmdir`
    will not remove, to `sudo rm` the link (the two shapes its readers found the remedy did not clear). A path that is
    neither a directory nor a regular file (a pipe, a socket) gets its own plain wording, and an empty file its
    own."""
    _, directory = _evaluate(dict(_DIR, **_mode(0, 0, 0o755)))
    assert "is a DIRECTORY" in directory and "starts without the key" in directory, directory
    assert "`sudo rmdir %s/age.key`" % _GUI_DIR in directory, directory
    assert "move that out first" in directory and "`sudo rm` the link" in directory, directory
    assert "install -m 0600 -o 1001 -g 1001" in directory, "the install still follows the rmdir"
    _, fifo = _evaluate(dict(_FIFO, **_mode(1001, 1001, 0o600)))
    assert "is not a regular file." in fifo and "rmdir" not in fifo, fifo
    _, empty = _evaluate(dict(_FILE, size=0, **_mode(1001, 1001, 0o600)))
    assert "is EMPTY." in empty and "rmdir" not in empty, empty


def test_the_gate_reads_the_file_a_container_would_mount_and_none_of_its_bytes():
    """The stat follows a link (`follow: true`): Docker binds a link's target, and without `follow` the stat
    describes the link, whose own mode is 0777, so a link to a 0600 root-owned key passed the other-readable leg
    (I-22). And it computes no checksum and no MIME type: the gate needs the type, owner and mode, and a digest of
    the GUI's private key in the registered result is one a verbose run would print. The real stat, run on a link,
    is in tests/integration/test_g9_age_key_gate_real_stat.py."""
    with open(_DEPLOY, encoding="utf-8") as fh:
        stat = next(t["ansible.builtin.stat"] for t in _tasks(yaml.safe_load(fh.read()))
                    if str(t.get("name", "")).startswith("Stat the onboard-gui age key"))
    assert stat.get("follow") is True, stat
    assert stat.get("get_checksum") is False and stat.get("get_mime") is False, stat


def test_the_refusal_carries_a_remedy_that_runs():
    """The whole point of the change. The message must contain a concrete command with the real target path — not
    a pointer to a doc — because this fires mid-deploy on a box the operator is standing up for the first time."""
    _, msg = _evaluate(_MISSING)
    assert "%s/age.key" % _GUI_DIR in msg, "the remedy must name the actual destination path"
    assert "install -m 0600 -o 1001 -g 1001" in msg, "the remedy must give a key the gate passes"
    assert "~/.config/sops/age/keys.txt" in msg, "and name the source: the control key (the accepted C10 residual)"


def test_the_refusal_says_the_re_run_restarts_a_running_gui():
    """The printed install puts a new file at the path, and a running onboard-gui keeps the one its bind started
    with. The re-run's stale-config check read bytes only, so a key put back with the same bytes was no change, and
    the deploy ended green on a GUI that still could not read its key (reader A's S-1 on I-22); the refusal asked
    the operator to restart it by hand, which they had to read to do. Since I-31 the check reads a storage-root
    file's mode and owner too, and the gate drops the GUI's record when it refuses (the record is of a key the gate
    passed, which the printed install puts back exactly: I-31's two reads), so the first deploy past the gate
    restarts it and the refusal says so. Held here: the sentence, no by-hand restart beside it in any spelling
    (`docker restart`, `docker container restart`, `docker compose restart`: I-31's read B, N-4), and the fact it
    rests on — the GUI binds its key read-only from a FILE under the storage root, the bind the check reads."""
    for stat in (_MISSING, dict(_FILE, **_mode(1001, 1001, 0o044))):
        _, msg = _evaluate(stat)
        assert "If onboard-gui is already running, the re-run restarts it" in msg, msg
        assert "dropped the record of the config the GUI last loaded" in msg, msg
        assert not re.search(r"restart\b[^`]*onboard-gui", msg) and "docker restart" not in msg, msg
    with open(os.path.join(_ROOT, "docker", "services", "onboard-gui.yaml"), encoding="utf-8") as fh:
        volumes = yaml.safe_load(fh)["services"]["onboard-gui"]["volumes"]
    assert ("${KONTROLL_STORAGE_ROOT:-/var/lib/kontroll}/onboard-gui/age.key:/home/semaphore/.config/sops/age/keys.txt"
            ":ro") in volumes, volumes


def _forget_task():
    with open(_DEPLOY, encoding="utf-8") as fh:
        tasks = list(_tasks(yaml.safe_load(fh.read())))
    names = [str(t.get("name", "")) for t in tasks]
    at = [i for i, n in enumerate(names) if n.startswith("Forget what onboard-gui last loaded")]
    assert len(at) == 1, "the G9 forget moved — move this test with it"
    stat = next(i for i, n in enumerate(names) if n.startswith("Stat the onboard-gui age key"))
    gate = next(i for i, n in enumerate(names) if "the actual remedy" in n)
    return tasks[at[0]], (stat, at[0], gate)


def test_a_refused_key_drops_the_guis_record_so_the_run_past_the_gate_restarts_it():
    """The record of what the GUI last loaded is always of a key this gate passed (the refusal stops the play before
    the record), and the install it prints puts that key's mode and owner back, so a GUI left holding a key it cannot
    read matched the record and was never restarted (I-31's two reads, measured). So before the assert, on the same
    stat, a task drops the GUI's record (`stale-config.py --forget onboard-gui`, the stamp the deploy's check reads),
    and a service the stamp does not hold is stale. Held here: its condition holds for exactly the shapes the gate
    refuses, every hand-written one above (the forget repeats the assert's legs, negated, and a leg added to one and
    not the other fails here); it runs only when onboard-gui is brought up, between the stat and the assert; it runs
    in a dry-run too (`check_mode: false`: the mandated dry-run is the run that refuses first, and dropping a record
    can only add a restart); and it reports changed only when it dropped one. The real stat on each shape, and the
    stamp it leaves, are in tests/integration/test_g9_age_key_gate_real_stat.py."""
    task, (stat_at, at, gate_at) = _forget_task()
    assert stat_at < at < gate_at, (stat_at, at, gate_at)
    whens = task["when"]
    assert isinstance(whens, list) and len(whens) == 2 and whens[0] == "'onboard-gui' in stack_services", whens
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for label, stat, should_pass in _CASES:
        forgets = env.compile_expression(whens[1])(_gui_age_key={"stat": stat})
        assert forgets is (not should_pass) and _evaluate(stat)[0] is should_pass, label
    cmd = " ".join(task["ansible.builtin.command"]["cmd"].split())
    assert cmd == ("{{ ansible_playbook_python }} -B {{ playbook_dir }}/../../scripts/stale-config.py "
                   "--stamp {{ kontroll_storage_root }}/config-loaded.json --forget onboard-gui"), cmd
    assert task.get("check_mode") is False, "the refusal a dry-run makes must drop the record too"
    assert task.get("register") == "_gui_record_forgotten"
    assert task["changed_when"] == "_gui_record_forgotten.stdout == 'forgotten'"
    with open(_DEPLOY, encoding="utf-8") as fh:
        found = [chain for t, chain in _with_parents(yaml.safe_load(fh.read()))
                 if str(t.get("name", "")).startswith("Forget what onboard-gui last loaded")]
    assert len(found) == 1
    assert all(section in ("block", "tasks") for section, _ in found[0])
    whens_above = [n["when"] for _, n in found[0] if "when" in n]
    assert set(whens_above) <= {"'onboard-gui' in stack_services"}, whens_above
    assert not any("tags" in n for _, n in found[0]) and "tags" not in task


def _with_parents(node, chain=()):
    """Every task with the chain of (section key, block) it sits in, the play first."""
    if isinstance(node, list):
        for item in node:
            yield from _with_parents(item, chain)
    elif isinstance(node, dict):
        yield node, chain
        for section in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if section in node:
                yield from _with_parents(node[section], chain + ((section, node),))


def test_the_gate_runs_whenever_onboard_gui_is_brought_up():
    """The integration test runs the stat and the assert as they are, but not the blocks around them, so a `when`
    on an enclosing block that never held would switch the whole gate off with every row here green (reader B's
    mutants B10 and B11 on I-22 survived all four test files). So: the two tasks sit in `block:` sections only (not
    a rescue or an always), and every `when` on them and on each block above them is the one condition the gate is
    for, onboard-gui being brought up; no block above them carries `tags`."""
    with open(_DEPLOY, encoding="utf-8") as fh:
        found = [(task, chain) for task, chain in _with_parents(yaml.safe_load(fh.read()))
                 if str(task.get("name", "")).startswith("Stat the onboard-gui age key")
                 or "the actual remedy" in str(task.get("name", ""))]
    assert len(found) == 2, "the G9 stat or assert moved — move this test with it"
    for task, chain in found:
        assert all(section in ("block", "tasks") for section, _ in chain), (task["name"], [s for s, _ in chain])
        whens = [node["when"] for node in [n for _, n in chain] + [task] if "when" in node]
        assert whens and set(whens) == {"'onboard-gui' in stack_services"}, (task["name"], whens)
        assert not any("tags" in node for _, node in chain) and "tags" not in task, task["name"]


def test_the_doc_pointer_in_the_refusal_actually_resolves():
    """The message cites a SETUP.md section, and the first draft cited the wrong one (§5 is secrets; the
    onboard-gui prerequisite is §6) — a pointer that sends the operator to an unrelated section is worse than no
    pointer, and section numbers move. Assert the cited heading exists verbatim in the doc."""
    _, msg = _evaluate(_MISSING)
    cited = re.search(r'docs/SETUP\.md "([^"]+)"', msg)
    assert cited, "the refusal should cite a SETUP.md section by NAME (numbers renumber, headings survive)"
    with open(os.path.join(_ROOT, "docs", "SETUP.md"), encoding="utf-8") as fh:
        setup = fh.read()
    assert ("## %s" % cited.group(1)) in setup, \
        "the refusal points at SETUP.md %r, which has no such heading" % cited.group(1)


def test_the_setup_doc_and_the_refusal_agree_on_the_remedy():
    """Two places now tell the operator how to provision this key. If they drift, one of them teaches a command
    that does not produce a key the gate accepts — and the doc is the one people find first."""
    with open(os.path.join(_ROOT, "docs", "SETUP.md"), encoding="utf-8") as fh:
        setup = fh.read()
    assert "install -m 0600 -o 1001 -g 1001" in setup, \
        "SETUP.md must carry the same ownership the gate requires and the refusal prints"


def test_the_refusal_still_refuses_to_mint_the_key():
    """The boundary this gate exists for. Making the refusal friendlier must never slide into auto-provisioning:
    a deploy that can create the GUI's decryption key has dissolved the separation the key represents."""
    with open(_DEPLOY, encoding="utf-8") as fh:
        text = fh.read()
    block = text.split("Stat the onboard-gui age key", 1)[1].split("Generate a self-signed TLS cert", 1)[0]
    assert "age-keygen" not in block.replace("`age-keygen` a", ""), \
        "the gate must not mint a key — age-keygen may appear only as advice inside the refusal text"
    _, msg = _evaluate(_MISSING)
    assert "never creates it" in msg, "the message should say plainly that this is a boundary, not a bug"
