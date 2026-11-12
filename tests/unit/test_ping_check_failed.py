"""ping.yml tells a down host (UNREACHABLE, tolerated) from a reachable one whose class check failed (CHECK FAILED,
which fails the run after the key cleanup), says when a dry run did not probe, and fails an UNREACHABLE host too when
the caller requires it, as galaxy.py's onboard verify does (I-84, I-85).

WHY. The probe and the class check share one block, whose rescue reported any failure as UNREACHABLE, and an
unreachable host leaves the run passing: a device that answered but rejected its credentials read as down, the smoke
gate passed such a fleet, and galaxy.py's verify printed "reachable & managed" on ping's rc 0 for a host that did not
answer or whose check failed. A dry run skips the probe, and its summary said OK for a host whose port was closed.
These rows hold the shape; tests/integration/test_ping_check_failed_runs.py runs it (and galaxy's own arguments).
"""
import inspect
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import galaxy  # noqa: E402

PLAYBOOK = os.path.join(ROOT, "ansible", "playbooks", "ping.yml")
REQUIRE = "kontroll_ping_require_reachable"


def _plays():
    with open(PLAYBOOK, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _probe_block():
    """The reach play's block that holds the probe and the class include."""
    found = [t for play in _plays() for t in play.get("tasks", [])
             if "block" in t and any("ansible.builtin.include_role" in b for b in t["block"])]
    assert len(found) == 1, found
    return found[0]


def test_the_rescue_tells_the_probes_failure_from_the_checks():
    """The probe (`wait_for`) is the block's first task and registers `_kontroll_probe`; the rescue's first task sets
    `kontroll_reachable: false`, `kontroll_check_failed` true where the probe's register is not failed (the probe
    passed, or a dry run skipped it), and `kontroll_check_failed_at` to the failed task's name alone. A host that
    reached the rescue with no failed task (its check could not be loaded) is failed by the rescue's next task and
    reaches no summary, so the record need not tell it apart. Guards I-84's record: without the register, or reading
    another var, a down host and one whose check failed are one word again (both UNREACHABLE, the run passing, or both
    CHECK FAILED, a down host failing the run); and the failed task's result (the device's answer, which a check that
    handles a credential may hold) kept in a fact the summary prints."""
    block = _probe_block()
    probe = block["block"][0]
    assert "ansible.builtin.wait_for" in probe and probe.get("register") == "_kontroll_probe", probe
    record = block["rescue"][0]["ansible.builtin.set_fact"]
    assert record["kontroll_reachable"] is False, record
    assert record["kontroll_check_failed"] == "{{ _kontroll_probe | default({}) is not failed }}", record
    assert record["kontroll_check_failed_at"] == "{{ ansible_failed_task.name | default('') }}", record
    with open(PLAYBOOK, encoding="utf-8") as fh:
        assert "ansible_failed_result" not in fh.read()


def test_the_summary_names_each_outcome_and_a_dry_run():
    """The summary play's first task sets `kontroll_ping_line`: REFUSED for a recorded refusal, then CHECK FAILED
    with the task its check stopped at (the record read through `| bool`, as the last play reads it), then OK or
    UNREACHABLE, and `(dry run: the port was not probed)` after any but REFUSED when `ansible_check_mode`; its second
    prints the host and that line. Guards I-84's words (a failed check summarised UNREACHABLE, as a down host) and
    I-85's (a dry run's OK for a host nothing was probed on), and the summary reading the record otherwise than the
    last play does. The order is pinned as text only: the conditions exclude one another (a refused host has no probe
    record, a failed check is not reachable), so no reorder gives a host two words; the real-run rows hold the words
    each host gets."""
    summary = next(p for p in _plays() if p.get("name") == "Reachability summary")
    word, show = summary["tasks"]
    line = word["ansible.builtin.set_fact"]["kontroll_ping_line"]
    marks = ["REFUSED", "kontroll_class_refused", "CHECK FAILED: its check stopped at the task: ",
             "kontroll_check_failed_at", "kontroll_check_failed", "'OK'", "kontroll_reachable", "'UNREACHABLE'",
             "(dry run: the port was not probed)", "ansible_check_mode", "not kontroll_class_refused"]
    at = [line.find(m) for m in marks]
    assert -1 not in at and at == sorted(at), (list(zip(marks, at)), line)
    assert "if kontroll_check_failed | default(false) | bool" in line, line
    assert show["ansible.builtin.debug"]["msg"] == "{{ inventory_hostname }}: {{ kontroll_ping_line }}", show


def test_the_last_play_fails_a_refused_host_a_failed_check_and_where_required_an_unreachable_one():
    """The last play (after the key cleanup) first refuses, by name, a dry run given `kontroll_ping_require_reachable`
    true (read through `| bool`), then asserts, for each host: not refused; a dry run, or no failed check; and, when
    that var is true, reachable; its message is the host's summary line, defaulted (an assert renders its message
    before it tests: I-91). Guards I-84: a failed check passing the run (the smoke gate's false PASS); a dry run
    failed for a check it cannot run on every device (the fresh-node check's ping step); an unreachable host failing
    every run, or never failing galaxy's verify; a dry run asked to prove each host reachable, which probes no port,
    passing a closed one as OK; and the message failing on its own."""
    plays = _plays()
    assert plays[-2].get("ansible.builtin.import_playbook") == "_cleanup-ssh-keys.yml", plays[-2]
    refuse, last = plays[-1]["tasks"]
    assert refuse["when"] == "ansible_check_mode and (%s | default(false) | bool)" % REQUIRE, refuse
    said = refuse["ansible.builtin.fail"]["msg"]
    assert REQUIRE in said and "without --check" in said, said
    check = last["ansible.builtin.assert"]
    assert check["that"] == [
        "not (kontroll_class_refused | default(false))",
        "ansible_check_mode or not (kontroll_check_failed | default(false) | bool)",
        "not (%s | default(false) | bool) or kontroll_reachable | default(false)" % REQUIRE], check
    assert check["fail_msg"] == (
        "{{ inventory_hostname }}: {{ kontroll_ping_line | default('no summary') }} (see the summary)."), check


def test_galaxys_verify_requires_the_host_reachable_and_says_ok_only_for_rc_0():
    """galaxy.py's onboard verify runs VERIFY_PING (ping.yml with `-e kontroll_ping_require_reachable=true`, the var
    the last play reads) and `--limit <host>` under VERIFY_ENV (a limit that matches no host fails the run), and
    prints `verify_line`: OK only for rc 0, otherwise PENDING with the rc, pointing at the summary's words; the
    onboard's commit message names the require var. Guards I-84: the verify ran ping.yml bare, so rc 0 came back for
    a host that did not answer, and printed "reachable & managed"; a run that read no inventory, which skips every
    play and passes; its PENDING reason, which named missing credentials for every failure, a refused class among
    them; and the hand-off telling the operator to verify with the bare run, which passes a down host."""
    assert galaxy.VERIFY_PING == ["ansible-playbook", "playbooks/ping.yml", "-e", REQUIRE + "=true"]
    assert galaxy.VERIFY_ENV == {"ANSIBLE_HOST_PATTERN_MISMATCH": "error"}
    source = inspect.getsource(galaxy.cmd_onboard)
    assert ('gitio._run(VERIFY_PING + ["--limit", host_name], cwd=os.path.join(paths.ROOT, "ansible"),\n'
            '                             env=VERIFY_ENV)') in source, source
    assert "-e %s=true (a bare run passes a down " % REQUIRE in source, source
    assert 'print("\\n" + verify_line(host_name, vrc))' in source, source
    assert galaxy.verify_line("h1", 0).startswith("OK h1 — reachable & managed")
    for rc in (1, 2, 4):
        said = galaxy.verify_line("h1", rc)
        assert said.startswith("PENDING h1 — not verified (ping rc=%d)" % rc), said
        assert all(w in said for w in ("UNREACHABLE", "CHECK FAILED", "REFUSED", "ping.yml --limit h1")), said


def test_gitio_run_adds_an_extra_environment_to_the_callers_for_that_command_alone(monkeypatch):
    """`gitio._run(cmd, env={...})` runs the command with the caller's environment plus those variables, and without
    `env` passes none (the child inherits the caller's), leaving the caller's own environment as it was. Guards the
    verify's VERIFY_ENV (I-84): a run given the variables alone would lose PATH and the operator's Ansible
    settings, and one that set them in the caller's environment would carry them into every later command."""
    from kontroll import gitio
    seen = []
    monkeypatch.setattr(gitio.subprocess, "run", lambda cmd, cwd=None, env=None: seen.append(env) or
                        type("P", (), {"returncode": 3})())
    monkeypatch.setenv("KONTROLL_ROW_MARK", "kept")
    assert gitio._run(["x"], cwd="/", env={"ANSIBLE_HOST_PATTERN_MISMATCH": "error"}) == 3
    assert gitio._run(["x"], cwd="/") == 3
    assert seen[0]["ANSIBLE_HOST_PATTERN_MISMATCH"] == "error" and seen[0]["KONTROLL_ROW_MARK"] == "kept", seen[0]
    assert seen[0]["PATH"] == os.environ["PATH"] and seen[1] is None, seen
    assert "ANSIBLE_HOST_PATTERN_MISMATCH" not in os.environ
