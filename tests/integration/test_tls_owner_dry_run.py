"""deploy-stack.yml's self-signed TLS tasks, run for real by ansible-playbook, in a dry-run and in an apply.

WHY: in self_signed mode the play makes each service's cert with `openssl req` (skipped under `--check`), then gives
the cert and its key to uid 1001. Until 2026-11-08 the owner task was skipped in a dry-run only on a node with no
canonical yet (`_fresh_preview`), so on a node that HAS a canonical and first enables the API or onboard-gui in
self_signed mode the mandated `--check --diff` failed on the owner task, "file (…/tls/api.key) is absent, cannot
continue", where the apply would have made the cert and owned it (I-27, found by the I-19 read). Each owner task
now gates on a `stat` of its own cert, read before the cert is made, and only in a dry-run; a note says what was not
previewed. These rows take the four tasks of each service from the playbook as they are (the probe, the generator,
the owner task, the note), in the playbook's order, and run them against scratch service directories.

A byo_proxy/acme row holds the other half of each gate: there kontroll makes no cert, so all four tasks are skipped,
in a dry-run and in an apply, and an operator's own files in tls/ are left as they are.

The apply row needs root (the owner task gives the files to uid 1001) and openssl, and is skipped without them; the
dry-run rows and the byo_proxy/acme row run as any user, since none of them changes an owner. The public CI runs
the suite as uid 1001, so tests/validate.sh's as-root step runs this file too, where a skipped row fails the gate.
Needs POSIX and ansible-playbook; without them every row is skipped.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix" or not shutil.which("ansible-playbook"),
                       reason="needs POSIX and ansible-playbook (it runs deploy-stack.yml's own tasks)"),
]

# Each service: its block, its four tasks by name, the variable that names its directory, its cert's file names.
SERVICES = {
    "api": {
        "block": "Provision the API host artifacts",
        "tasks": ("Look for the API's self-signed cert", "Generate a self-signed TLS cert for the API",
                  "Own the TLS material as the runtime uid", "cannot preview owning the API's cert"),
        "dir_var": "kontroll_api_dir", "crt": "api.crt", "key": "api.key",
    },
    "gui": {
        "block": "Provision the onboard-gui host artifacts",
        "tasks": ("Look for onboard-gui's self-signed cert", "Generate a self-signed TLS cert for onboard-gui",
                  "Own the onboard-gui TLS material", "cannot preview owning onboard-gui's cert"),
        "dir_var": "kontroll_gui_dir", "crt": "gui.crt", "key": "gui.key",
    },
}
ROLES = ("probe", "make", "own", "note")


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _service_tasks(svc):
    """The service's four TLS tasks, in the order its block has them, each registered as `_<svc>_<role>` (the probe
    keeps its own register, which the owner task reads); returns [(role, task)]."""
    with open(DEPLOY, encoding="utf-8") as fh:
        blocks = [t for t in _tasks(yaml.safe_load(fh.read()))
                  if SERVICES[svc]["block"] in str(t.get("name", "")) and "block" in t]
    assert len(blocks) == 1, "the %s block moved — move this test with it" % svc
    picked = []
    for task in blocks[0]["block"]:
        hits = [r for r, frag in zip(ROLES, SERVICES[svc]["tasks"]) if frag in str(task.get("name", ""))]
        if hits:
            role = hits[0]
            picked.append((role, dict(task) if role == "probe" else dict(task, register="_%s_%s" % (svc, role))))
    assert sorted(r for r, _ in picked) == sorted(ROLES), "a TLS task of %s moved or was renamed: %r" % (svc, picked)
    return picked


def _run(tmp_path, check, fresh_node=False, needs_cert="yes"):
    """Run both services' TLS tasks in a play shaped as deploy-stack.yml's (become when root, no facts) with the
    resolver's answer (`yes` is self_signed, `no` is byo_proxy/acme) and `_fresh_preview` as a node with (default)
    or without a canonical would have it; return (rc, output, {"<svc>_<role>": the task's registered result})."""
    result = tmp_path / "result.json"
    tasks, regs = [], {}
    for svc in SERVICES:
        for role, task in _service_tasks(svc):
            tasks.append(task)
            regs["%s_%s" % (svc, role)] = task["register"]
    record = "{{ {%s} | to_json }}" % ", ".join("'%s': %s | default({})" % (k, v) for k, v in regs.items())
    play = [{
        "name": "The self-signed TLS tasks, as deploy-stack.yml runs them",
        "hosts": "localhost",
        "gather_facts": False,
        "become": os.geteuid() == 0,
        "vars": {"kontroll_api_dir": str(tmp_path / "api"), "kontroll_gui_dir": str(tmp_path / "gui"),
                 "_needs_cert": {"stdout": needs_cert}, "_fresh_preview": check and fresh_node},
        "tasks": tasks + [{"name": "Write down the results",
                           "ansible.builtin.copy": {"content": record, "dest": str(result), "mode": "0600"},
                           "check_mode": False}],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    home_a = tmp_path / "ansible-home"
    home_a.mkdir(exist_ok=True)
    cfg = home_a / "ansible.cfg"
    cfg.write_text("[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n", encoding="utf-8")
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
        "ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home_a), ANSIBLE_LOCAL_TEMP=str(home_a / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1")
    argv = ["ansible-playbook", "-i", str(tmp_path / "hosts.yml"), str(tmp_path / "play.yml")]
    if check:
        argv += ["--check", "--diff"]
    p = subprocess.run(argv, cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    return p.returncode, out, (json.loads(result.read_text(encoding="utf-8")) if result.exists() else None)


def _tls_dirs(tmp_path):
    """Each service's tls/ directory, as the play's dir task leaves it (uid 1001's 0750 when root runs this)."""
    for svc in SERVICES:
        tls = tmp_path / svc / "tls"
        tls.mkdir(parents=True)
        if os.geteuid() == 0:
            os.chown(str(tls), 1001, 1001)
        os.chmod(str(tls), 0o750)


def _skipped(res):
    """A task that did not run: skipped, or a loop whose every item was."""
    return res.get("skipped") is True or bool(res.get("results")) and all(i.get("skipped") for i in res["results"])


def _owned(path):
    st = os.lstat(path)
    return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)


@pytest.mark.parametrize("fresh_node", [False, True], ids=["node-with-a-canonical", "fresh-node"])
def test_a_dry_run_before_the_certs_are_made_passes_says_so_and_makes_nothing(tmp_path, fresh_node):
    """`--check --diff` on a node whose certs are not made yet, with a canonical (I-27: a service first enabled in
    self_signed mode on a node applied before) and without one (a fresh node, the 2026-07-28 case), passes: each
    service's owner task is skipped, its note says NOT PREVIEWED and names the cert, and nothing is made in tls/.
    Guards the owner task being gated on the node being new again (the first row then fails "is absent, cannot
    continue", as I-27 did) or on nothing (both rows fail), and a skip nothing reports."""
    _tls_dirs(tmp_path)
    rc, out, res = _run(tmp_path, check=True, fresh_node=fresh_node)
    assert rc == 0 and res, out
    for svc, spec in SERVICES.items():
        assert _skipped(res["%s_own" % svc]), (svc, res["%s_own" % svc], out)
        note = res["%s_note" % svc]
        assert not _skipped(note) and "NOT PREVIEWED" in note.get("msg", "") and spec["crt"] in note["msg"], (svc, note)
        assert os.listdir(str(tmp_path / svc / "tls")) == [], (svc, os.listdir(str(tmp_path / svc / "tls")))


def test_a_dry_run_with_the_certs_there_previews_owning_them_and_changes_nothing(tmp_path):
    """With each cert and key there already, but not uid 1001's 0640/0644 (an operator's restore, say), the dry-run
    runs the owner task and reports that it would change them, says nothing about a skip, and leaves both files as
    they were. Guards a gate that skips the owner task in every dry-run (it would hide the very change this preview
    exists to show) and a note that fires when nothing was skipped."""
    _tls_dirs(tmp_path)
    before = {}
    for svc, spec in SERVICES.items():
        for name in (spec["key"], spec["crt"]):
            path = tmp_path / svc / "tls" / name
            path.write_text("not a real cert\n", encoding="utf-8")
            os.chmod(str(path), 0o600)
            before[str(path)] = _owned(str(path))
    rc, out, res = _run(tmp_path, check=True)
    assert rc == 0 and res, out
    for svc in SERVICES:
        own = res["%s_own" % svc]
        assert not _skipped(own) and own.get("changed") is True, (svc, own, out)
        assert _skipped(res["%s_note" % svc]), (svc, res["%s_note" % svc])
    assert {p: _owned(p) for p in before} == before, out


@pytest.mark.parametrize("check", [True, False], ids=["dry-run", "apply"])
@pytest.mark.parametrize("certs", [False, True], ids=["no-certs", "operators-own-certs"])
def test_byo_proxy_or_acme_runs_no_tls_task_and_leaves_tls_as_it_is(tmp_path, check, certs):
    """With the resolver answering `no` (byo_proxy or acme: TLS ends at the operator's proxy or Caddy), all four TLS
    tasks of each service are skipped, in a dry-run and in an apply, with no certs and with an operator's own files
    in tls/ (0600, the runner's), and those files keep their owner and mode. Guards an owner gate (or a note) that
    loses its `_needs_cert` term: the probe is skipped there, so the dry-run fails evaluating `<probe>.stat`, an apply
    with no certs fails "is absent, cannot continue", and an apply with the operator's certs gives them to uid 1001."""
    _tls_dirs(tmp_path)
    before = {}
    if certs:
        for svc, spec in SERVICES.items():
            for name in (spec["key"], spec["crt"]):
                path = tmp_path / svc / "tls" / name
                path.write_text("the operator's own\n", encoding="utf-8")
                os.chmod(str(path), 0o600)
                before[str(path)] = _owned(str(path))
    listing = {svc: sorted(os.listdir(str(tmp_path / svc / "tls"))) for svc in SERVICES}
    rc, out, res = _run(tmp_path, check=check, needs_cert="no")
    assert rc == 0 and res, out
    ran = sorted(k for k, v in res.items() if not _skipped(v))
    assert ran == [], (ran, out)
    assert {svc: sorted(os.listdir(str(tmp_path / svc / "tls"))) for svc in SERVICES} == listing, out
    assert {p: _owned(p) for p in before} == before, out


@pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0 or not shutil.which("openssl"),
                    reason="the apply gives the certs to uid 1001 (needs root) and makes them with openssl")
def test_an_apply_makes_each_cert_owns_it_and_a_second_apply_changes_nothing(tmp_path):
    """An apply on a node with no certs yet makes each service's cert and key, gives both to uid 1001 and gid 1001,
    the key 0640 and the cert 0644, and says nothing about a skip; a second apply reports no task changed. Guards a
    gate that skips owning a cert the apply just made (the probe reads "absent" in an apply too, so only the
    check-mode term keeps the owner task running; the service would then crash on a key it cannot read) and a role
    that is not idempotent."""
    _tls_dirs(tmp_path)
    rc, out, res = _run(tmp_path, check=False)
    assert rc == 0 and res, out
    for svc, spec in SERVICES.items():
        assert not _skipped(res["%s_own" % svc]) and _skipped(res["%s_note" % svc]), (svc, res, out)
        tls = tmp_path / svc / "tls"
        assert _owned(str(tls / spec["key"])) == (1001, 1001, 0o640), out
        assert _owned(str(tls / spec["crt"])) == (1001, 1001, 0o644), out
    rc, out, again = _run(tmp_path, check=False)
    assert rc == 0 and again, out
    changed = sorted(k for k, v in again.items() if v.get("changed"))
    assert changed == [], (changed, out)


def test_the_rows_that_need_root_run_in_the_gate_as_root():
    """The apply row needs root and is skipped elsewhere, and the public CI runs the suite as uid 1001, so the one
    row that proves an apply owns the certs it makes ran nowhere but a builder's root run. tests/validate.sh's
    as-root step runs the files that need root and fails `--strict` on any skip; it must name this one."""
    with open(os.path.join(ROOT, "tests", "validate.sh"), encoding="utf-8") as fh:
        text = fh.read()
    step = text.split("step canon-drop-as-root", 1)[1].split("\n  fi\n", 1)[0]
    assert "tests/integration/test_tls_owner_dry_run.py" in step, step
