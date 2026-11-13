"""The unified root SSH-auth seam (G6 — onboarding accepts an SSH PRIVATE key as an alternative/fallback to a
password, materialized to a 0600 file at fleet-play time). These pins guard the load-bearing no-leak + backward-compat
contract the adversary flagged (docs/reviews/2026-06-18-ssh-auth-unification/). The Ansible materialization seam
itself (_render-ssh-keys.yml) is proven by a scratch-VM dogfood (Windows can't run ansible); here we pin the
data-write + the static no_log contract that keep the key off git, the inventory, and any log.
"""
import json
import os
import re
import stat
import subprocess
import sys

import pytest
import yaml

from kontroll import authspec, catalog, gitio, probe
from kontroll.service import onboard

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A FAKE PEM-shaped fixture. The BEGIN/END header literals are SPLIT so the contiguous
# `-----BEGIN ... PRIVATE KEY-----` never appears in this source (the secret-scanner must not flag a test
# fixture) — the runtime value is a faithful multi-line PEM the round-trip/normalize tests need.
_H = "-----BEGIN OPENSSH " "PRIVATE KEY-----"
_PEM = _H + "\nb3BlbnNzaC1rZXktZmFrZQ\nDEADBEEFc4\n" + _H.replace("BEGIN", "END")


def _plan(monkeypatch, **kw):
    """Build a real onboard plan for a cliconf class with a canned probe (no lab, no install)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "5.0.0", "local", "deep")
        f["plugins"] = {"cliconf": ["ios"]}
        f["modules"] = ["ios_command"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)
    return onboard.build_onboard_plan("cisco.ios", "cisco_ios", "core_switch", "192.0.2.10",
                                      host_name="sw1", secrets="network", **kw)


def test_key_onboard_emits_path_and_domain_never_the_pem(monkeypatch):
    """A key-auth onboard emits `ansible_ssh_private_key_file` (a kontroll_device_key_dir path), the NON-secret
    `kontroll_ssh_key_domain` hostvar (the render play keys on it to materialize the key), and a
    `<host>_ssh_private_key` SOPS field — but the PEM VALUE is NEVER in the inventory drop-in text (it lives only in
    creds_to_set, destined for SOPS). Guards the key leaking into a git-tracked inventory file (C1)."""
    plan = _plan(monkeypatch, ssh_private_key=_PEM)
    hv = plan["host_block"]["core_switch"]["hosts"]["sw1"]
    assert hv["ansible_ssh_private_key_file"] == "{{ kontroll_device_key_dir }}/sw1.key"
    assert hv["kontroll_ssh_key_domain"] == "network"
    assert plan["creds_to_set"]["sw1_ssh_private_key"] == _PEM + "\n"     # stored with a trailing newline
    assert "BEGIN OPENSSH" not in plan["host_text"]                       # the PEM is NEVER in the drop-in YAML


def test_password_only_onboard_has_no_key_hostvars(monkeypatch):
    """BACKWARD-COMPAT golden: a password-only onboard's drop-in carries NO key hostvars — the key branch is purely
    ADDITIVE, so a host passing no key is structurally unchanged from before the seam. Guards a silent inventory
    drift that would break every existing host's auth on the next fleet run (the one guard the cut refuses to defer)."""
    plan = _plan(monkeypatch, username="admin", password="pw")
    hv = plan["host_block"]["core_switch"]["hosts"]["sw1"]
    assert "ansible_ssh_private_key_file" not in hv and "kontroll_ssh_key_domain" not in hv
    assert hv["ansible_user"].endswith("sw1_username }}") and "ansible_password" in hv   # password path untouched


def test_crlf_paste_is_normalized_with_trailing_newline(monkeypatch):
    """MF-7: a Windows textarea paste (CRLF) is normalized to LF + a trailing newline is ensured before the
    SOPS-store, so paramiko (netcommon_cli) accepts the key and SSH doesn't reject a newline-stripped key. Guards
    a CRLF/short key silently failing the connection."""
    plan = _plan(monkeypatch, ssh_private_key="-----BEGIN-----\r\nLINE\r\n-----END-----")
    stored = plan["creds_to_set"]["sw1_ssh_private_key"]
    assert "\r" not in stored and stored.endswith("\n") and stored == "-----BEGIN-----\nLINE\n-----END-----\n"


def test_apply_routes_creds_through_stdin_never_argv(monkeypatch):
    """MF-3: apply_onboard_plan routes creds (incl. the multi-line PEM) through gitio.sops_write_domain (which
    transits sops' STDIN), NEVER gitio.sops_set (which puts the value on a ps-/proc-visible argv). Pins the
    decrypt->merge->write so a second host's onboard never drops the first's fields. The single most
    security-load-bearing test of this seam. Every WRITER apply_onboard_plan reaches is stubbed — including the
    inventory drop-in writer (gitio.write_inventory_host): with only _write_new stubbed, this test wrote a real
    `instance/inventory/onboarded-cisco_ios.yml` (host `sw1`, TEST-NET) into the checkout on every run — the
    "fossil" the 2026-10-08 review flagged as an operator artefact (S03). It was this test."""
    plan = _plan(monkeypatch, ssh_private_key=_PEM)
    monkeypatch.setattr(gitio, "_write_new", lambda *a, **k: True)
    monkeypatch.setattr(gitio, "write_inventory_host", lambda *a, **k: True)
    monkeypatch.setattr(onboard, "enable_in_fleet", lambda key: False)
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {"existing_field": "keep-me"})
    captured = {}
    monkeypatch.setattr(gitio, "sops_write_domain",
                        lambda d, m: captured.update(domain=d, map=dict(m)) or True)
    set_calls = []
    monkeypatch.setattr(gitio, "sops_set", lambda *a, **k: set_calls.append(a) or True)
    onboard.apply_onboard_plan(plan)
    assert not set_calls, "the PEM (and all creds) must go via sops_write_domain stdin, NEVER sops_set argv"
    assert captured["map"]["sw1_ssh_private_key"] == _PEM + "\n"          # the PEM transited the stdin write
    assert captured["map"]["existing_field"] == "keep-me"                # merged: a prior host's field survives


def test_render_play_keeps_no_log_on_the_key():
    """MF-2: the _render-ssh-keys.yml tasks that read the device key — the decrypt probe, whose text holds the
    community.sops lookup, and the key write, whose argv runs `sops --decrypt` — MUST carry `no_log: true`: without
    it a `--check --diff` or the Semaphore job log (Vector-tailed into Loki) would print what the task read (the
    probe's templated lookup; the write's result, sops' errors with it). The one line the whole no-leak story rests on;
    read from the parsed play, so a task that reads the key is found wherever its text sits (this row once searched
    500 characters after the key file's path)."""
    tasks, write = _render_tasks()
    readers = [t for t in tasks if "community.sops.sops" in str(t)] + [write]
    assert len(readers) == 2 and "sops" in write["ansible.builtin.command"]["argv"][2], readers
    assert all(t.get("no_log") is True for t in readers), "the decrypt probe AND the key write must both be no_log"


def test_loud_fail_when_a_key_host_is_undecryptable():
    """MF-1 (the blocker's unconditional half): a key-auth host whose SOPS domain is not decryptable in the runner
    context (e.g. `compute` is control-node-only — option B) FAILS LOUDLY via an assert, never silently leaves no
    key (which would surface as a misleading 'Permission denied (publickey)'). Pins the loud-fail can't be dropped."""
    src = open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8").read()
    assert "ansible.builtin.assert" in src and "could NOT be decrypted" in src
    assert "_ssh_key_present | bool" in src, "the loud-fail asserts the key was actually decryptable"


def test_render_and_cleanup_force_local_delegation():
    """REGRESSION GUARD (scratch-VM dogfood, 2026-06-18): every materialization/cleanup task MUST carry
    `delegate_to: localhost`. A fleet host's `ansible_connection` (network_cli/ssh/httpapi, set by its
    backend+group_vars) OVERRIDES a play's `connection: local` keyword (Ansible var-beats-keyword precedence), so
    without delegation these LOCAL file ops route over the DEVICE connection — a switch can't run `file`/`command`, and
    the key NEVER materializes for ANY real host (the dogfood caught exactly this: UNREACHABLE on the dir-create
    task). Read from the parsed play (every task but the key-dir guard's import, whose own tasks each delegate), so a
    newly-added render task that forgets to delegate fails CI, not the next live run."""
    render = open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8").read()
    cleanup = open(os.path.join(ROOT, "ansible", "playbooks", "_cleanup-ssh-keys.yml"), encoding="utf-8").read()
    (play,) = yaml.safe_load(render)
    loose = [t["name"] for t in play["tasks"]
             if "ansible.builtin.import_tasks" not in t and t.get("delegate_to") != "localhost"]
    assert not loose, "every render task must delegate_to: localhost: %s" % loose
    assert "delegate_to: localhost" in cleanup, "the cleanup rm must delegate_to: localhost (run_once host hijack)"


# The playbooks whose summary plays follow the key cleanup (I-9, I-64, I-83); each such play connects to nothing.
_SUMMARY_AFTER_CLEANUP = ("backup-configs.yml", "install-node-exporter.yml", "wire-logging.yml", "ping.yml")


def test_every_playbook_that_renders_device_keys_removes_them_last():
    """Every playbook under ansible/playbooks/ that imports `_fleet-preamble.yml` (which renders each key-auth host's
    private key into the run's key dir) or `_render-ssh-keys.yml` itself, judged by the path the import runs (fold 7's
    read B, N3: a playbook that imported the renderer directly, with no cleanup, passed; a templated or linked import is
    refused tree-wide by the dispatch scan, `imports a playbook the scan does not read`: fold 8's read A, N1; the pin
    reads YAML only, and a JSON-wrapped import is refused at playbook level by the gate (2.19's syntax-check,
    ansible-lint): fold 8's read B, S2), imports the shipped `ansible/playbooks/_cleanup-ssh-keys.yml` once, as its last
    entry; in `backup-configs.yml`, `install-node-exporter.yml`, `wire-logging.yml` and `ping.yml` alone, plays may
    follow it, and no import: their summaries run after the cleanup so that a run whose every capture, install or wiring
    is recorded as failed (or, in wire-logging.yml, whose request is refused) still removes its keys (I-9, I-64), and
    ping.yml's last play, which fails a host whose class has no `check` entrypoint installed, so that a run whose every
    host is refused does (I-83); `tests/unit/test_backup_summary_is_last.py` and `tests/unit/test_fleet_play_rescue.py`
    hold that each of those plays connects to nothing. Guards a fleet play whose cleanup import is dropped, or moved
    above a play that still connects: no shipped caller passes `kontroll_run_id`, so every run shares
    `kontroll-ssh-keys.manual`, and its keys would then stay there after every run, not only after one an earlier play
    stops (SECURITY.md's accepted-risk row assumes the cleanup runs last; local-refusal fold 2's read B, N3: nothing
    pinned it outside backup-configs.yml). The import entry carries only its name: a `when:`, a `tags: [never]` or a
    `vars:` that renames the key dir on it left the dir in place with every other test green (fold 5's read A, N2; I-9
    reads the cleanup's own entry, not the last), and it names the shipped file itself, not one of its name elsewhere
    (fold 6's read A, N2, and read B, N5: a no-op namesake under `files/` passed every test)."""
    def imported(entry):
        return next((str(v) for k, v in entry.items() if k == "import_playbook" or k.endswith(".import_playbook")),
                    None) if isinstance(entry, dict) else None

    found = {}
    playbooks = os.path.join(ROOT, "ansible", "playbooks")
    for directory, _dirs, files in os.walk(playbooks):
        for name in files:
            if not name.endswith((".yml", ".yaml")):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            imports = [imported(e) for e in doc] if isinstance(doc, list) else []
            resolved = [os.path.relpath(os.path.realpath(os.path.join(directory, i)),
                                        os.path.realpath(playbooks)).replace(os.sep, "/") if i else None
                        for i in imports]
            rel = os.path.relpath(os.path.join(directory, name), playbooks).replace(os.sep, "/")
            if rel != "_fleet-preamble.yml" and any(r in ("_fleet-preamble.yml", "_render-ssh-keys.yml")
                                                    for r in resolved):   # the renderer, by the path it runs
                found[rel] = resolved
                assert resolved.count("_cleanup-ssh-keys.yml") == 1, (rel, resolved)
                at = resolved.index("_cleanup-ssh-keys.yml")
                assert set(doc[at]) <= {"name", "import_playbook", "ansible.builtin.import_playbook"}, (rel, doc[at])
                after = resolved[at + 1:]   # None = a play, a string = an import
                assert not after or (rel in _SUMMARY_AFTER_CLEANUP and not any(after)), (rel, after)
    assert {"ping.yml", "backup-configs.yml", "wire-logging.yml", "install-node-exporter.yml"} <= set(found), found


def test_the_key_cleanup_removes_the_run_key_dir_on_the_runner():
    """`_cleanup-ssh-keys.yml` is one play of two tasks: the import of tasks/refuse-foreign-key-dir.yml (with no
    other keyword), then `ansible.builtin.file` on `{{ kontroll_device_key_dir }}` with `state: absent`, delegated to
    localhost, run in a dry run too (`check_mode: false`), in a play tagged `always`, and neither carries any other
    keyword. Guards a `check_mode: true`, a `when:` or an `ignore_errors` on the task or the play, which would leave
    every run's keys on disk (local-refusal fold 4's read A, N4: fold 3's mutant X6 survived every test); the
    `check_mode: false` dropped, under which a dry run removes nothing, a stale key an earlier run left included
    (I-87); the tag dropped, under which a `--tags` run skips the cleanup (I-93); and the key-dir guard dropped, moved
    after the delete or given a `when:`, under which the delete removes a dir that holds anything else, with all it
    holds (I-95). tests/integration/test_ssh_key_render_runs.py runs it in each mode, and over each refused dir."""
    with open(os.path.join(ROOT, "ansible", "playbooks", "_cleanup-ssh-keys.yml"), encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    assert len(plays) == 1 and len(plays[0]["tasks"]) == 2, plays
    play, (guard, task) = plays[0], plays[0]["tasks"]
    assert guard == {"name": guard["name"], "ansible.builtin.import_tasks": "tasks/refuse-foreign-key-dir.yml"}, guard
    assert set(play) == {"name", "hosts", "gather_facts", "connection", "run_once", "tags", "tasks"}, play
    assert play["tags"] == ["always"], play
    assert set(task) == {"name", "ansible.builtin.file", "delegate_to", "check_mode"}, task
    assert task["ansible.builtin.file"] == {"path": "{{ kontroll_device_key_dir }}", "state": "absent"}, task
    assert task["delegate_to"] == "localhost" and task["check_mode"] is False, task


def test_the_key_render_writes_the_keys_in_every_run_mode():
    """`_render-ssh-keys.yml` is one play tagged `always`, whose two writes (the key dir, the key file) carry
    `check_mode: false`, and no task carries a `check_mode` or `tags` of its own otherwise; the import of
    tasks/refuse-foreign-key-dir.yml comes before the decrypt probe and either write, for each key-auth host alone (its
    `when` the writes' own), and the dir is made after every assert. Guards I-87: skipped in a dry run, the dir
    and the copy wrote no key, so every key-auth host's connection named a key file that did not exist, could not log
    in, and counted as an ignored unreachable host: the `--check --diff` dry-run CLAUDE.md requires passed having
    reached no key-auth device (measured on ansible-core 2.20.10 against a key-only SSH server). I-93: untagged, a
    `--tags` run rendered no key for a tagged device task. I-95: a key dir that holds anything else, chmodded and
    written into before the guard could refuse it. And a dry run whose every key-auth host fails the decrypt assert
    leaving an empty dir the cleanup never reaches (the I-87 reads, N2). The decrypt probe (`set_fact`) and the asserts
    run in a dry run as they are. Every task runs for a key-auth host only; the last, which names a failed key write,
    only when that write failed too. tests/integration/test_ssh_key_render_runs.py runs it in each mode."""
    with open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    (play,) = plays
    assert play["tags"] == ["always"], play
    tasks = play["tasks"]
    writes = {t["name"]: t for t in tasks if "ansible.builtin.file" in t or "ansible.builtin.command" in t}
    assert len(writes) == 2 and all(t["check_mode"] is False for t in writes.values()), writes
    assert writes["Ensure the per-run ephemeral key dir exists (0700, run-uid-owned)"]["ansible.builtin.file"] == {
        "path": "{{ kontroll_device_key_dir }}", "state": "directory", "mode": "0700"}, writes
    others = [t for t in tasks if t["name"] not in writes]
    assert others and not any({"check_mode", "tags"} & set(t) for t in others), others
    at = {kind: next(i for i, t in enumerate(tasks) if kind in t) for kind in (
        "ansible.builtin.import_tasks", "ansible.builtin.set_fact", "ansible.builtin.file", "ansible.builtin.command")}
    guard = tasks[at["ansible.builtin.import_tasks"]]
    assert guard == {"name": guard["name"], "ansible.builtin.import_tasks": "tasks/refuse-foreign-key-dir.yml",
                     "when": "kontroll_ssh_key_domain is defined"}, guard
    assert at["ansible.builtin.import_tasks"] < at["ansible.builtin.set_fact"] < at["ansible.builtin.file"], at
    last_assert = max(i for i, t in enumerate(tasks) if "ansible.builtin.assert" in t)
    assert last_assert < at["ansible.builtin.file"] < at["ansible.builtin.command"], (last_assert, at)
    assert all(t.get("when") == "kontroll_ssh_key_domain is defined" for t in tasks[:-1]), tasks
    assert tasks[-1]["when"].startswith("kontroll_ssh_key_domain is defined and "), tasks[-1]


def test_the_key_dir_guard_refuses_anything_but_regular_key_files():
    """tasks/refuse-foreign-key-dir.yml reads the key dir path with `stat` (a link not followed), lists everything the
    dir holds with `find` (any kind of entry, hidden ones too, only when the path is a directory), and asserts that the
    path does not exist, or is a directory whose every entry is a regular file named `*.key`; the message names the
    path and the foreign entries; every task is delegated to localhost, none skips in a dry run or ignores an error.
    Guards I-95: the cleanup removes the key dir whole, so a `KONTROLL_SSH_KEY_DIR` naming an operator's dir was
    removed with all it held (and chmodded 0700 by the render); and each half of the test, where one dropped lets a
    shape through (`hidden: false` a dotfile; `follow: true` a link to a dir, whose target the delete leaves holding
    keys; the regular-file test a dir or a link named `*.key`; the name test a file of another name).
    tests/integration/test_ssh_key_render_runs.py runs each shape, in the render and in the cleanup."""
    guard_file = os.path.join(ROOT, "ansible", "playbooks", "tasks", "refuse-foreign-key-dir.yml")
    with open(guard_file, encoding="utf-8") as fh:
        stat_task, find_task, check = yaml.safe_load(fh)
    assert stat_task["ansible.builtin.stat"] == {"path": "{{ kontroll_device_key_dir }}", "follow": False,
                                                 "get_checksum": False}, stat_task
    assert stat_task["register"] == "_kontroll_key_dir", stat_task
    assert find_task["ansible.builtin.find"] == {"paths": "{{ kontroll_device_key_dir }}", "file_type": "any",
                                                 "hidden": True}, find_task
    assert find_task["register"] == "_kontroll_key_dir_held", find_task
    assert find_task["when"] == "_kontroll_key_dir.stat.isdir | default(false)", find_task
    assert check["ansible.builtin.assert"]["that"] == [
        "not _kontroll_key_dir.stat.exists or (_kontroll_key_dir.stat.isdir and _kontroll_key_foreign | length == 0)"]
    foreign = " ".join(check["vars"]["_kontroll_key_foreign"].split())
    assert foreign == ("{{ (_kontroll_key_dir_held.files | default([]) | rejectattr('isreg') | map(attribute='path') "
                       "| list) + (_kontroll_key_dir_held.files | default([]) | map(attribute='path') "
                       "| reject('search', '[.]key$') | list) }}"), foreign
    said = check["ansible.builtin.assert"]["fail_msg"]
    assert said.startswith(
        "kontroll_device_key_dir ({{ kontroll_device_key_dir }}) {{ _kontroll_key_dir_said }}."), said
    assert "_kontroll_key_foreign | map('basename')" in check["vars"]["_kontroll_key_dir_said"], check
    for task in (stat_task, find_task, check):
        assert task["delegate_to"] == "localhost", task
        assert not {"check_mode", "ignore_errors", "failed_when", "tags", "run_once"} & set(task), task
    assert "when" not in stat_task and "when" not in check, (stat_task, check)


@pytest.mark.parametrize("env,run_id,check,want", [
    ({}, None, False, "/tmp/kontroll-ssh-keys.manual"),
    ({}, None, True, "/tmp/kontroll-ssh-keys.manual.check"),
    ({}, None, None, "/tmp/kontroll-ssh-keys.manual"),
    ({"XDG_RUNTIME_DIR": "/run/user/1000"}, "r1", False, "/run/user/1000/kontroll-ssh-keys.r1"),
    ({"XDG_RUNTIME_DIR": "/run/user/1000"}, "r1", True, "/run/user/1000/kontroll-ssh-keys.r1.check"),
    ({"KONTROLL_SSH_KEY_DIR": "/srv/keys"}, "r1", False, "/srv/keys"),
    ({"KONTROLL_SSH_KEY_DIR": "/srv/keys//"}, None, False, "/srv/keys"),
    ({"KONTROLL_SSH_KEY_DIR": "/srv/keys/"}, None, True, "/srv/keys.check"),
    ({"KONTROLL_SSH_KEY_DIR": "/"}, None, False, "/"),
], ids=["default", "default-check", "no-check-var", "xdg-run-id", "xdg-run-id-check", "override", "override-slashes",
        "override-slash-check", "root-kept"])
def test_the_key_dir_is_a_dry_runs_own_and_drops_a_trailing_slash(env, run_id, check, want):
    """group_vars/all.yml's `kontroll_device_key_dir`, rendered by Jinja with Ansible's `lookup('env', …)` (an unset
    variable reads empty), `regex_replace` and `bool` stood in for: `$KONTROLL_SSH_KEY_DIR`, else `$XDG_RUNTIME_DIR` or
    `/tmp` with `kontroll-ssh-keys.<run id or manual>`, its trailing `/` dropped (a lone `/` kept, which the key-dir
    guard refuses), and `.check` appended in a dry run (`ansible_check_mode`; none where it is not set). Guards I-87's
    reads: one dir for both modes, so a dry run's cleanup removed the keys of a real run it overlapped, which ended
    green with its key-auth host not run; and a trailing `/` that made a dry run's dir a hidden `.check` inside the
    real run's. tests/integration/test_ssh_key_render_runs.py runs it in Ansible."""
    import jinja2
    with open(os.path.join(ROOT, "ansible", "playbooks", "group_vars", "all.yml"), encoding="utf-8") as fh:
        expr = yaml.safe_load(fh)["kontroll_device_key_dir"]
    jenv = jinja2.Environment(undefined=jinja2.Undefined)
    jenv.filters["regex_replace"] = lambda value, pattern, repl: re.sub(pattern, repl, value)
    jenv.filters["bool"] = lambda value: value if isinstance(value, bool) else str(value).lower() in ("1", "true")
    given = {"lookup": lambda kind, name: env.get(name, "") if kind == "env" else pytest.fail(kind)}
    if run_id is not None:
        given["kontroll_run_id"] = run_id
    if check is not None:
        given["ansible_check_mode"] = check
    assert jenv.from_string(expr).render(**given) == want


def _api_plan(monkeypatch, **kw):
    """Build a real onboard plan for an httpapi (REST) device → the `api` backend (no lab, no install)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"fortinet.fortios": "2.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "2.0.0", "local", "deep")
        f["plugins"] = {"httpapi": ["fortios"]}        # httpapi-only → classifies to the api backend
        f["modules"] = ["fortios_configuration_fact"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)
    return onboard.build_onboard_plan("fortinet.fortios", "fortigate", "edge_firewall", "192.0.2.1",
                                      host_name="fw1", secrets="network", **kw)


def test_onboard_plan_exposes_derived_cred_fields_without_values(monkeypatch):
    """F1: a cliconf onboard's plan carries `cred_fields` (the DERIVED descriptors for THIS backend — SSH login,
    not a token) and `cred_source`, and NO descriptor holds a value. Guards the form being able to render the right
    fields from the plan while the no-leak contract (names-only on every surface) is preserved."""
    plan = _plan(monkeypatch, username="admin", password="pw")
    names = {cf["field"] for cf in plan["cred_fields"]}
    assert {"username", "password", "ssh_private_key"} <= names and "api_token" not in names
    assert plan["cred_source"] == "shallow"
    for cf in plan["cred_fields"]:
        assert "value" not in cf and "secret_value" not in cf      # descriptors are pure schema


def test_api_device_onboard_offers_a_token_not_a_dead_ssh_box(monkeypatch):
    """THE HEADLINE WIN through the planner: onboarding a REST/httpapi device derives an `api_token` field and NO
    `ssh_private_key` — the form stops showing dead SSH boxes for a REST device (the operator's exact gap)."""
    plan = _api_plan(monkeypatch)
    assert plan["error"] is None and plan["backend"] == "api"
    names = {cf["field"] for cf in plan["cred_fields"]}
    assert "api_token" in names and "ssh_private_key" not in names


def test_domain_confinement_refuses_a_cross_domain_secret(monkeypatch):
    """MF-S2: if a derived SECRET field is stamped for a DIFFERENT SOPS domain than the onboard targets, the planner
    RAISES its designed refusal, `OnboardRefusal`, BEFORE any cred is mapped — so a malformed/adversarial descriptor
    can never smuggle a secret into a domain the operator didn't choose. The new data-driven loop's load-bearing
    safety rail (today's hardcoded if-ladder couldn't cross domains; a data-driven one could). The type is pinned too:
    the routes answer `OnboardRefusal` with a 422, and since T20 a plain ValueError with a 500 (T20's read B, S2)."""
    monkeypatch.setattr(authspec, "derive_auth", lambda bdef, domain, **k: {
        "source": "shallow",
        "fields": [{"field": "evil", "kind": "password", "secret": True, "sops_stem": "evil",
                    "maps_to": "ansible_password", "domain": "OTHER_DOMAIN"}]})
    with pytest.raises(onboard.OnboardRefusal, match="refusing to cross domains"):
        _plan(monkeypatch, creds={"evil": "leak-me"})           # onboard targets 'network', field claims 'OTHER_DOMAIN'


def _render_play():
    """The render play, as it parses."""
    with open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8") as fh:
        (play,) = yaml.safe_load(fh)
    return play


def _render_tasks():
    """The render play's tasks, and its key write (its one `command` task), as the play parses."""
    play = _render_play()
    (write,) = [t for t in play["tasks"] if "ansible.builtin.command" in t]
    return play["tasks"], write


# A stand-in `sops` for the key write's script: it adds its arguments to FAKE_SOPS_ARGV, one line per call, and prints
# the field FAKE_SOPS_OUT holds, its bytes as the environment holds them (a field that is not UTF-8 too).
_FAKE_SOPS = """#!%s
import json, os, sys
with open(os.environ["FAKE_SOPS_ARGV"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.buffer.write(os.environb[b"FAKE_SOPS_OUT"])
sys.exit(int(os.environ["FAKE_SOPS_RC"]))
"""
_KEY = _PEM + "\n"
_POSIX = pytest.mark.skipif(os.name != "posix", reason="runs the key write's script with a stand-in sops on PATH, "
                                                       "as the control node does (a POSIX executable and file modes)")


# Put before the key write's script when a row runs it: a Python audit hook that records, in KEY_WRITE_AUDIT, each
# process the script starts (its argv), each file it opens to write (`os.open` or `open`, not by fd; os.devnull aside),
# each link it makes and each rename; with KEY_WRITE_INTERRUPT set, the rename raises KeyboardInterrupt (as a Ctrl-C
# in the write's window does).
_AUDIT = """import json as _json, os as _os, sys as _sys
_audit = open(_os.environ["KEY_WRITE_AUDIT"], "w")
def _say(*what):
    _audit.write(_json.dumps(list(what)) + "\\n")
    _audit.flush()
def _hook(event, args):
    if event == "open" and isinstance(args[0], str) and args[0] != _os.devnull:
        mode, flags = args[1], args[2]
        if (mode and set(mode) & set("wax+")) or (mode is None and flags & (_os.O_WRONLY | _os.O_RDWR | _os.O_CREAT)):
            _say("write", args[0])
    elif event == "subprocess.Popen":
        argv = args[1]
        _say("popen", [_os.fsdecode(a) for a in argv] if not isinstance(argv, (str, bytes)) else [_os.fsdecode(argv)])
    elif event in ("os.link", "os.symlink"):
        _say(event, _os.fsdecode(args[0]), _os.fsdecode(args[1]))
    elif event == "os.rename":
        _say("rename", _os.fsdecode(args[0]), _os.fsdecode(args[1]))
        if _os.environ.get("KEY_WRITE_INTERRUPT"):
            raise KeyboardInterrupt
_sys.addaudithook(_hook)
"""


def _run_key_write(tmp_path, stored, rc=0, cred="dev_1", interrupt=False):
    """Run the key write's script as its argv does (`python -c <script> <key path> <domain> <field>`), after the audit
    hook, the key path `keys/<cred>.key` under `tmp_path` and the field `<cred>_ssh_private_key`, with the stand-in sops
    first on PATH printing `stored` and exiting `rc` (and the rename interrupted, with `interrupt`); return the finished
    process, the arguments sops was given at each call, and what the audit hook recorded, in order."""
    bin_dir, keys = tmp_path / "bin", tmp_path / "keys"
    bin_dir.mkdir(exist_ok=True)
    keys.mkdir(mode=0o700, exist_ok=True)
    (bin_dir / "sops").write_text(_FAKE_SOPS % sys.executable, encoding="utf-8")
    os.chmod(bin_dir / "sops", 0o755)
    said, audit = tmp_path / "sops-argv.json", tmp_path / "audit.jsonl"
    for old in (said, audit):
        if old.exists():
            old.unlink()
    script = _render_tasks()[1]["ansible.builtin.command"]["argv"][2]
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), FAKE_SOPS_ARGV=str(said),
               FAKE_SOPS_OUT=stored, FAKE_SOPS_RC=str(rc), KEY_WRITE_AUDIT=str(audit), PYTHONDONTWRITEBYTECODE="1")
    env.pop("KEY_WRITE_INTERRUPT", None)
    if interrupt:
        env["KEY_WRITE_INTERRUPT"] = "1"
    p = subprocess.run([sys.executable, "-c", _AUDIT + script, str(keys / ("%s.key" % cred)),
                        str(tmp_path / "lab.sops.yml"), "%s_ssh_private_key" % cred],
                       env=env, capture_output=True, text=True, timeout=60)
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()] if audit.exists() else []
    calls = [json.loads(line) for line in said.read_text(encoding="utf-8").splitlines()] if said.exists() else []
    return p, calls, events


@_POSIX
@pytest.mark.parametrize("stored", [_KEY, _PEM, _KEY + "\n", "  " + _KEY + " \n\n", "\u00a0" + _KEY + "\u00a0",
                                    _KEY + "\u00a0"],
                         ids=["one-final-newline", "no-final-newline", "two-final-newlines", "padded", "nbsp-wrapped",
                              "trailing-nbsp"])
def test_the_key_write_writes_the_field_sops_extracts_with_one_final_newline(tmp_path, stored):
    r"""REGRESSION GUARD (scratch-VM dogfood, 2026-06-18): the key write's script starts one process, sops, once, asking
    for the host's one field (`--decrypt --extract '["dev_1_ssh_private_key"]' <domain>`), and writes the field
    stripped, with exactly one final newline, to a 0600 file at the key path, leaving nothing else in the key dir —
    whether the field holds the key with one final newline, none, two, ASCII whitespace around it, or a no-break space
    (U+00A0) around it or after it; the one file it opens to write is a new one in the key dir, named `.`, mkstemp's
    random characters and `.key`, which it renames over the key path, and it makes no link. OpenSSH rejects a key with
    no final newline ('error in libcrypto' -> publickey denied): with the original `content: >-` the dogfood's key-auth
    SSH FAILED (byte-verified: the materialized key was 398 bytes / 6 lines vs the valid 399 / 7), and a field may hold
    a key with none or with more. A pasted key wrapped in a no-break space is what the GUI sends untrimmed, and OpenSSH
    refuses it: `bytes.strip()` kept it where the copy's `| trim` did not (I-96 re-read A, S2). The file it writes
    guards I-96: a run killed mid-write leaves that file, which must be in the key dir (the cleanup removes the dir; a
    key outside it is what I-96 was), pass the key-dir guard (a regular `*.key`) and name no host's key (none starts
    with `.`). One sops call and no link: a second call writing the field to a file of its own, or a link to the new
    file, puts the key outside the key dir with every other row green (I-96 re-read B, N2: M8, M12).
    tests/integration/test_ssh_key_render_runs.py runs it with the real sops."""
    p, calls, events = _run_key_write(tmp_path, stored)
    assert p.returncode == 0, p.stderr
    argv = ["--decrypt", "--extract", '["dev_1_ssh_private_key"]', str(tmp_path / "lab.sops.yml")]
    assert calls == [argv], calls
    keys = tmp_path / "keys"
    key = keys / "dev_1.key"
    assert key.read_bytes() == _KEY.encode() and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert os.listdir(keys) == ["dev_1.key"]
    (started, run), (opened, new), (moved, src, dst) = events
    assert (started, run) == ("popen", ["sops"] + argv), events
    assert (opened, moved, src, dst) == ("write", "rename", new, str(key)), events
    name = os.path.basename(new)
    assert os.path.dirname(new) == str(keys) and name.startswith(".") and name.endswith(".key"), new


@_POSIX
@pytest.mark.parametrize("cred", ["a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 59, "sw\u00e9_1"],
                         ids=["251-characters", "not-ascii"])
def test_the_key_write_renders_a_host_name_of_any_length_or_script(tmp_path, cred):
    """A host whose cred key is 251 characters (its key file's name, 255, the most a file system's name holds; the
    onboard accepts a host name of 253) or holds a character outside ASCII (`swé_1`, which a hand-written inventory
    can name): the script asks sops for the field by its name as it stands (`["swé_1_ssh_private_key"]`, not JSON's
    `\\u00e9` escape) and writes the key to `<cred>.key`, 0600, the key dir holding it alone. Guards I-96 re-read A, S1:
    the new file was named after the host (`<cred>-` and mkstemp's characters), so a name of 243 characters or more
    overflowed 255 and failed the write (`File name too long`), and sops reads the text between the quotes as it
    stands, so the escaped name named no field — both hosts the `copy` it replaced rendered. The integration rows run
    both through the render with the real sops."""
    p, calls, events = _run_key_write(tmp_path, _KEY, cred=cred)
    assert p.returncode == 0, p.stderr
    assert calls == [["--decrypt", "--extract", '["%s_ssh_private_key"]' % cred, str(tmp_path / "lab.sops.yml")]], calls
    key = tmp_path / "keys" / ("%s.key" % cred)
    assert key.read_bytes() == _KEY.encode() and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert os.listdir(tmp_path / "keys") == ["%s.key" % cred]


@_POSIX
@pytest.mark.parametrize("earlier", ["hard-link", "link", "dangling-link", "read-only-key"])
def test_the_key_write_replaces_an_earlier_file_at_the_key_path_never_writing_through_it(tmp_path, earlier):
    """An earlier entry at the key path — a hard link to an operator's 0644 file, a link to it, a link to nothing, or
    an earlier run's 0400 key — is replaced: the path is a regular 0600 file holding the key, the operator's file keeps
    its text and its mode, the dangling link's target is not made, and the key dir holds the key alone. Guards I-96's
    write: a write into the path keeps an existing file's mode (0644, which ssh refuses) and writes through a hard
    link or a link into the file it names; the `copy` it replaced moved a new file over the path, as the rename does.
    In a run the key-dir guard refuses a link or a directory at the path before any write (I-96 re-read A, N3), so a
    run meets the regular-file cases alone: tests/integration/test_ssh_key_render_runs.py runs the hard-link case
    through the render."""
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    notes = tmp_path / "notes.txt"
    notes.write_text("an operator's file the key write must not touch\n", encoding="utf-8")
    os.chmod(notes, 0o644)
    key = keys / "dev_1.key"
    if earlier == "hard-link":
        os.link(notes, key)
    elif earlier == "link":
        os.symlink(notes, key)
    elif earlier == "dangling-link":
        os.symlink(tmp_path / "nowhere", key)
    else:
        key.write_text("an earlier run's key\n", encoding="utf-8")
        os.chmod(key, 0o400)
    p, _said, _events = _run_key_write(tmp_path, _KEY)
    assert p.returncode == 0, p.stderr
    assert not key.is_symlink() and key.read_bytes() == _KEY.encode(), earlier
    assert stat.S_IMODE(key.stat().st_mode) == 0o600, oct(key.stat().st_mode)
    assert notes.read_text(encoding="utf-8") == "an operator's file the key write must not touch\n"
    assert stat.S_IMODE(notes.stat().st_mode) == 0o644 and not (tmp_path / "nowhere").exists()
    assert os.listdir(keys) == ["dev_1.key"]


@_POSIX
def test_the_key_write_fails_and_leaves_no_file_when_sops_or_the_rename_fails(tmp_path):
    """sops failing (exit 1, after printing part of a field) fails the script and writes nothing in the key dir; the
    rename failing (the key path is a directory) fails it and removes the new file it made there, leaving the dir as it
    was; a field that is not UTF-8 fails it the same way; and a KeyboardInterrupt at the rename (a Ctrl-C in the write's
    window) fails it and removes the new file too. Guards the write's loud failure (the task fails, never a silent
    short key) and its cleanup of the new file: a file left beside the key path would hold a key the cleanup removes
    only when the run completes. The interrupt is not an OSError: an `except OSError` there survived every other row
    (I-96 re-read B, N3: M6), and leaves the 0600 file to the next run's cleanup."""
    p, calls, events = _run_key_write(tmp_path, "part of a field", rc=1)
    assert p.returncode != 0 and len(calls) == 1 and [e[0] for e in events] == ["popen"], (p.stderr, events)
    assert os.listdir(tmp_path / "keys") == []
    p, _calls, events = _run_key_write(tmp_path, _KEY, interrupt=True)
    assert p.returncode != 0 and "KeyboardInterrupt" in p.stderr, p.stderr
    assert [e[0] for e in events] == ["popen", "write", "rename"], events
    assert os.listdir(tmp_path / "keys") == []
    p, _calls, events = _run_key_write(tmp_path, "\udcff" + _KEY)
    assert p.returncode != 0 and "UnicodeDecodeError" in p.stderr, p.stderr
    assert os.listdir(tmp_path / "keys") == []
    (tmp_path / "keys" / "dev_1.key").mkdir()
    p, _calls, _events = _run_key_write(tmp_path, _KEY)
    assert p.returncode != 0, p.stderr
    assert os.listdir(tmp_path / "keys") == ["dev_1.key"] and os.listdir(tmp_path / "keys" / "dev_1.key") == []


def test_the_key_write_takes_no_key_as_an_argument():
    """The key write is `ansible.builtin.command` whose one argument is its argv: the playbook's Python, `-c`, the
    script the rows above run, then the key file's path, the host's SOPS domain and its field's name; it has no `vars`
    and no `environment`, and is delegated to localhost. The one task whose text holds the community.sops lookup is
    the decrypt probe, which sets a boolean; no task of the play is a `copy` or a `template` or passes a `content`.
    Guards I-96: a `copy` with `content` staged the key in temp files under the controller's `~/.ansible/tmp`, outside
    the key dir, which a run killed mid-copy leaves (seen in flight on 2.18.19 and 2.19.4); a `command` given the key on
    `stdin` put it in its module's arguments, which an unpipelined run writes into the module file (kept under
    keep_remote_files, as under a `su` become) and ANSIBLE_DEBUG prints whatever `no_log` says; a task env var holding
    it is in -vvv's EXEC line, and an argv holding it in every local user's `ps`. Nor can the key reach the write
    another way: the write has no `args` or `module_defaults`, and the play no `module_defaults` or `environment`
    and no `vars` that hold a lookup (a play's `module_defaults` giving the command a `stdin` from such a var passed
    this row: I-96 re-read B, N4: M9). tests/integration/test_ssh_key_render_runs.py runs the env, stdin, argv and
    `copy` ways and sees the first three in its output or kept module file; the `copy` it sees only by its kept
    module file's name (its temp files are gone when the run ends), so this row is that way's guard."""
    play = _render_play()
    assert not {"module_defaults", "environment"} & set(play), sorted(play)
    assert "lookup(" not in str(play.get("vars", {})) and "query(" not in str(play.get("vars", {})), play.get("vars")
    tasks, write = _render_tasks()
    staged = [t["name"] for t in tasks if {"ansible.builtin.copy", "ansible.builtin.template"} & set(t)
              or any(isinstance(v, dict) and "content" in v for v in t.values())]
    assert not staged, staged
    args = write["ansible.builtin.command"]
    assert set(args) == {"argv"}, args
    assert args["argv"][:2] == ["{{ ansible_playbook_python }}", "-c"], args
    assert args["argv"][3:] == ["{{ kontroll_device_key_dir }}/{{ inventory_hostname | replace('-', '_') }}.key",
                                "{{ inventory_dir ~ '/../secrets/' ~ kontroll_ssh_key_domain ~ '.sops.yml' }}",
                                "{{ inventory_hostname | replace('-', '_') ~ '_ssh_private_key' }}"], args
    assert "{{" not in args["argv"][2] and not {"vars", "environment", "args", "module_defaults"} & set(write), write
    assert write["delegate_to"] == "localhost", write
    (probe,) = [t for t in tasks if "community.sops.sops" in str(t)]
    assert set(probe["ansible.builtin.set_fact"]) == {"_ssh_key_present"}, probe
    assert probe["ansible.builtin.set_fact"]["_ssh_key_present"].rstrip().endswith("| length > 0 }}"), probe


def test_a_failed_key_write_is_failed_by_the_next_task_naming_its_exit_code_never_its_output():
    """The key write registers `_ssh_key_write` with `failed_when: false`, and the task right after it is an
    `ansible.builtin.fail`, delegated to localhost, run when the host is key-auth and the write's `rc` is not 0 (a
    write with no `rc` counts as failed), whose message reads the write's result for its `rc` only (never its stdout,
    stderr or msg) and names the field and the domain in the sops command to run by hand, its output sent to
    /dev/null; no other task reads `_ssh_key_write`. Guards I-96 re-read A, N2: the write's own failure printed only
    `censored` (no_log) after a probe that passed — sops off PATH, a key the lookup alone is given, a name the write
    could not render, another run's cleanup in its window — with nothing to go on; and a message that read the result
    whole could print what the write's sops said. The integration row runs a write that fails where the probe passed."""
    tasks, write = _render_tasks()
    assert write["register"] == "_ssh_key_write" and write["failed_when"] is False, write
    after = tasks[tasks.index(write) + 1]
    assert set(after) == {"name", "ansible.builtin.fail", "vars", "delegate_to", "when"}, sorted(after)
    assert after["delegate_to"] == "localhost", after
    assert " ".join(after["when"].split()) == ("kontroll_ssh_key_domain is defined and _ssh_key_write is defined and "
                                               "(_ssh_key_write.rc | default(1)) != 0"), after["when"]
    said = after["ansible.builtin.fail"]["msg"]
    assert set(re.findall(r"_ssh_key_write[.\w]*", said)) == {"_ssh_key_write.rc"}, said
    assert "--decrypt --extract {{ ('[\"%s\"]' % _field) | quote }} {{ _domain | quote }} > /dev/null" in said, said
    assert after["vars"] == {"_field": write["ansible.builtin.command"]["argv"][5],
                             "_domain": write["ansible.builtin.command"]["argv"][4]}, after["vars"]
    readers = [t["name"] for t in tasks if "_ssh_key_write" in str(t)]
    assert readers == [write["name"], after["name"]], readers


def test_a_dotted_host_name_gets_a_lookup_that_resolves_one_key(monkeypatch):
    """The host var that reads a credential out of the decrypted SOPS domain names ONE key. For a host named
    `sw.lab.example` (a name the boundary accepts on purpose) the key is `sw.lab.example_password`, and written as
    `(…).sw.lab.example_password` it is an attribute chain that can never resolve — so that name gets the bracket
    form, while an identifier-shaped name keeps the dotted form byte for byte. Guards both halves: a host whose
    credentials silently cannot be read, and a rewrite of every existing drop-in (a second onboard must still
    report no change)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "5.0.0", "local", "deep")
        f["plugins"] = {"cliconf": ["ios"]}
        f["modules"] = ["ios_command"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)

    def host_vars(host_name):
        plan = onboard.build_onboard_plan("cisco.ios", "cisco_ios", "core_switch", "192.0.2.10",
                                          host_name=host_name, secrets="network", username="admin", password="pw")
        return plan["host_block"]["core_switch"]["hosts"][host_name], plan["creds_to_set"]

    plain, _ = host_vars("sw-core-1")
    assert plain["ansible_password"].endswith(").sw_core_1_password }}")          # unchanged: the dotted form
    dotted, creds = host_vars("sw.lab.example")
    assert dotted["ansible_password"].endswith(")['sw.lab.example_password'] }}")   # one key, in brackets
    assert "sw.lab.example_password" in creds
    assert onboard._sops_ref("X", "a_b") == "{{ (X).a_b }}" and onboard._sops_ref("X", "9a") == "{{ (X)['9a'] }}"


@pytest.mark.parametrize("stem", ["pw }} {{ lookup('pipe', 'id')", "pw']", "pw.x", "pw-x", "", "pw\n", None])
def test_a_credential_field_whose_name_is_not_an_identifier_is_refused(monkeypatch, stem):
    """A credential field's stem is formatted into the host var's lookup expression, so it is an identifier or the
    onboard is refused before anything is rendered — as REPOSITORY DATA (nothing the caller sent is wrong: the
    backend registry or the class's module file declared the name; an installed module's option with such a name
    is not offered as a field at all, tests/unit/test_authspec.py), saying where to look and not echoing the name.
    Guards a declared field called `pw }} {{ … }}` becoming a Jinja expression in a host's inventory variables the
    moment an operator fills that field in — and the refusal being answered as a field the operator got wrong."""
    monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"fields": [
        {"field": "password", "kind": "password", "label": "password", "maps_to": "ansible_password",
         "sops_stem": stem, "secret": True, "required": False}]})
    from kontroll import paths
    with pytest.raises(paths.RepositoryDataError) as e:
        _plan(monkeypatch, password="pw")
    assert e.value.what == "credential field" and "lookup" not in str(e.value)
    assert "device class" in str(e.value) and "registry entry" in str(e.value)      # where to look
    assert not isinstance(e.value, ValueError)


def test_an_inline_credential_field_reaches_the_inventory_only_as_a_typed_value(monkeypatch):
    """For a credential field marked `inline: true` the PLANNER writes the value itself into the host's inventory
    variables — and refuses a value that is not a port, with the field named, while `8443` lands as the int 8443.
    The helper is pinned on its own elsewhere; this pins the CALL SITE for a field authspec.derive_auth hands the
    planner (the shipped SSH port's call site is pinned in tests/unit/test_onboard_port.py), so the check cannot be
    bypassed on either. Guards `{{ … }}` in a literal host var, which Ansible evaluates on the runner when a play
    touches the host, and the request's text being written where a typed value belongs (S28)."""
    from kontroll import paths
    monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"fields": [
        {"field": "api_port", "kind": "port", "label": "port", "maps_to": "ansible_httpapi_port",
         "sops_stem": "api_port", "inline": True, "secret": False, "required": False}], "source": "shallow"})
    with pytest.raises(paths.RequestBoundaryError) as e:
        _plan(monkeypatch, creds={"api_port": "{{ 7*7 }}"})
    assert e.value.field == "api_port"
    plan = _plan(monkeypatch, creds={"api_port": "8443"})
    written = plan["host_block"]["core_switch"]["hosts"]["sw1"]["ansible_httpapi_port"]
    assert written == 8443 and type(written) is int
    assert "api_port" not in str(plan["creds_to_set"])                   # an inline value is not a SOPS secret
