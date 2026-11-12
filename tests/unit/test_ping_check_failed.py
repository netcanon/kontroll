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
    I-85's (a dry run's OK for a host nothing was probed on), and the order: a refused host named anything but
    REFUSED, or a failed check named OK; and the summary reading the record otherwise than the last play does."""
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
    """The last play (after the key cleanup) asserts, for each host: not refused; a dry run, or no failed check; and,
    when `kontroll_ping_require_reachable` is true, reachable; its message is the host's summary line, defaulted (an
    assert renders its message before it tests: I-91). Guards I-84: a failed check passing the run (the smoke gate's
    false PASS); a dry run failed for a check it cannot run on every device (the fresh-node check's ping step);
    an unreachable host failing every run, or never failing galaxy's verify; and the message failing on its own."""
    plays = _plays()
    assert plays[-2].get("ansible.builtin.import_playbook") == "_cleanup-ssh-keys.yml", plays[-2]
    check = plays[-1]["tasks"][0]["ansible.builtin.assert"]
    assert check["that"] == [
        "not (kontroll_class_refused | default(false))",
        "ansible_check_mode or not (kontroll_check_failed | default(false) | bool)",
        "not (%s | default(false) | bool) or kontroll_reachable | default(false)" % REQUIRE], check
    assert check["fail_msg"] == (
        "{{ inventory_hostname }}: {{ kontroll_ping_line | default('no summary') }} (see the summary)."), check


def test_galaxys_verify_requires_the_host_reachable_and_says_ok_only_for_rc_0():
    """galaxy.py's onboard verify runs VERIFY_PING (ping.yml with `-e kontroll_ping_require_reachable=true`, the var
    the last play reads) and `--limit <host>`, and prints `verify_line`: OK only for rc 0, otherwise PENDING with the
    rc, pointing at the summary's words. Guards I-84: the verify ran ping.yml bare, so rc 0 came back for a host that
    did not answer, and printed "reachable & managed"; and its PENDING reason, which named missing credentials for
    every failure, a refused class among them."""
    assert galaxy.VERIFY_PING == ["ansible-playbook", "playbooks/ping.yml", "-e", REQUIRE + "=true"]
    source = inspect.getsource(galaxy.cmd_onboard)
    assert 'gitio._run(VERIFY_PING + ["--limit", host_name]' in source, source
    assert 'print("\\n" + verify_line(host_name, vrc))' in source, source
    assert galaxy.verify_line("h1", 0).startswith("OK h1 — reachable & managed")
    for rc in (1, 2, 4):
        said = galaxy.verify_line("h1", rc)
        assert said.startswith("PENDING h1 — not verified (ping rc=%d)" % rc), said
        assert all(w in said for w in ("UNREACHABLE", "CHECK FAILED", "REFUSED", "ping.yml --limit h1")), said
