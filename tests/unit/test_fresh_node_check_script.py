"""tests/fresh-node-check.sh, run against a scratch checkout with fake tools: where it runs each step, with what
environment, and what makes it fail.

WHY — the script is the one gate that makes Ansible evaluate a fresh node's install (CI's validate job runs it), and
two ways it could go wrong would never show as a red run: run on a configured control node, it could mint a second
age key into the operator's ~/.config/sops/age or scaffold an instance/ into the checkout it was started from; and its
full-stack pass, which must FAIL, could degrade to "any red is fine", passing a play that breaks for a reason that
has nothing to do with the age key. The real tools are not needed to see either: a fake `ansible-playbook` records
where and how it was called and answers as the mode says, and a stub kontroll-init writes the two files the real one
does. Real git, real bash, the real script.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(os.name != "posix", reason="the script is bash's"),
              pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ALL = ["semaphore", "homepage", "prometheus", "grafana", "loki", "vector", "api", "onboard-gui"]

# The stub kontroll-init: records how it was run, writes the key and instance/.sops.yaml as the real one does, and
# ends with the real one's last line; FAKE_MODE leaves one of the three out.
_INIT = r'''#!/usr/bin/env python3
import json, os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
mode = os.environ.get("FAKE_MODE", "")
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": "kontroll-init", "cwd": os.getcwd(), "root": root, "argv": sys.argv[1:],
                         "env": {k: os.environ.get(k) for k in ("HOME", "SOPS_AGE_KEY_FILE", "KONTROLL_IN_CONTAINER")}})
             + "\n")
key = os.environ["SOPS_AGE_KEY_FILE"]
if mode != "init-no-key":
    os.makedirs(os.path.dirname(key), exist_ok=True)
    open(key, "w").write("# public key: age1fake\n")
if mode != "init-no-sops":
    os.makedirs(os.path.join(root, "instance"), exist_ok=True)
    open(os.path.join(root, "instance", ".sops.yaml"), "w").write("creation_rules: []\n")
if mode != "init-unfinished":
    print("\n==> kontroll-init done. Next:")
'''

# The fake ansible-playbook: records how it was called and what git reads of the tree above its cwd; answers by
# FAKE_MODE. backup-configs.yml answers as the real one does against the mock inventory (four hosts stopped at their
# device's read or credential gate, every host in the recap, exit 2), or as a `backup:<how>` mode bends that.
_PLAYBOOK = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
sys.stdout.reconfigure(encoding="utf-8")
args = sys.argv[1:]
def git(*a):
    r = subprocess.run(["git", "-C", os.path.dirname(os.getcwd())] + list(a), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": "ansible-playbook", "cwd": os.getcwd(), "argv": args,
                         "env": {k: os.environ.get(k) for k in ("HOME", "SOPS_AGE_KEY_FILE", "KONTROLL_IN_CONTAINER",
                                                                "ANSIBLE_COLLECTIONS_PATH")},
                         "git": {"top": git("rev-parse", "--show-toplevel"),
                                 "branch": git("symbolic-ref", "--short", "HEAD"),
                                 "files": git("ls-tree", "-r", "--name-only", "HEAD")}}) + "\n")
mode = os.environ.get("FAKE_MODE", "")
everything = any("onboard-gui" in a for a in args)
ok = "PLAY RECAP\nlocalhost : ok=3 changed=0 unreachable=0 failed=0 skipped=1 rescued=0 ignored=0"
bad = "PLAY RECAP\nlocalhost : ok=2 changed=0 unreachable=0 failed=1 skipped=0 rescued=0 ignored=0"
if everything and mode == "all-pass":
    print(ok); sys.exit(0)
if everything and mode == "other-red":
    print("fatal: [localhost]: FAILED! => something else broke\n" + bad); sys.exit(2)
if everything:
    print("onboard-gui's age key at /opt/kontroll/gui/age.key is MISSING. deploy-stack never creates it\n" + bad)
    sys.exit(2)
how, _, play = mode.partition(":")
if how == "no-recap" and play in args:
    sys.exit(0)
if how == "failed-host" and play in args:
    print(bad); sys.exit(0)
if how == "red" and play in args:
    print("ERROR! the playbook could not be read"); sys.exit(4)
if "playbooks/backup-configs.yml" in args:
    stops = list(STOPS)
    if mode == "backup:pass":
        stops = []
    elif mode == "backup:other-host":
        stops.append(("mock-ap", "backend_raw_ssh : Write capture"))
    elif mode == "backup:other-task":
        stops[0] = ("mock-edge", "opnsense : Resolve capture filename")
    elif mode == "backup:listed-passes":
        stops = stops[:-1]
    out = ["", "TASK [backend_netcommon_cli : A read whose failure is ignored] ****",
           'fatal: [mock-switch]: FAILED! => {"msg": "no answer"}', "...ignoring"]
    for h, t in stops:
        out += ["", "TASK [%s] ****" % t, 'fatal: [%s]: FAILED! => {"censored": "no_log"}' % h]
    if mode != "backup:no-summary":
        out += ["", "PLAY [Backup run summary] ****"]
    if mode != "backup:no-recap":
        out += ["", "PLAY RECAP ****"] + ["%-26s : ok=3    changed=0    unreachable=0    failed=%d    skipped=6"
                                          % (h, any(h == s for s, _ in stops)) for h in HOSTS]
    print("\n".join(out)); sys.exit(2 if stops else 0)
print(ok)
'''.replace("STOPS", repr([
    ("mock-edge", "opnsense : Read /conf/config.xml"),
    ("mock-fortigate", "backend_api : Refuse an unresolvable API credential (var NAMES only — never the value)"),
    ("mock-switch", "backend_netcommon_cli : Pull config via cli_command"),
    ("mock-xe", "backend_netcommon_cli : Pull config via cli_command")])).replace("HOSTS", repr(
        ["mock-ap", "mock-docker", "mock-edge", "mock-fortigate", "mock-hv", "mock-routeros", "mock-switch", "mock-xe"]))


def _exe(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path, mode="", extra_env=None):
    """A scratch checkout (the real script, the stub kontroll-init, one commit), fake tools first on PATH, an empty
    caller HOME and TMPDIR; runs the script from the checkout and returns (result, calls, paths)."""
    repo = tmp_path / "repo"
    for d in ("tests", "scripts", "ansible/playbooks"):
        (repo / d).mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "tests", "fresh-node-check.sh"), repo / "tests" / "fresh-node-check.sh")
    _exe(repo / "scripts" / "kontroll-init.py", _INIT)
    (repo / "ansible" / "playbooks" / "deploy-stack.yml").write_text("---\n")
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "s"], check=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _exe(bin_ / "ansible-playbook", _PLAYBOOK)
    for tool in ("sops", "age-keygen"):
        _exe(bin_ / tool, "#!/bin/sh\nexit 0\n")
    home, tmpdir, log = tmp_path / "caller-home", tmp_path / "tmpdir", tmp_path / "calls.jsonl"
    home.mkdir()
    tmpdir.mkdir()
    env = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]), HOME=str(home), TMPDIR=str(tmpdir),
               FAKE_LOG=str(log), FAKE_MODE=mode)
    env.pop("ANSIBLE_COLLECTIONS_PATH", None)
    env.pop("KONTROLL_FRESH_KEEP", None)
    env.update(extra_env or {})
    r = subprocess.run(["bash", str(repo / "tests" / "fresh-node-check.sh")], cwd=str(repo), env=env,
                       capture_output=True, text=True, timeout=120)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return r, calls, {"repo": repo, "home": home, "tmpdir": tmpdir}


def test_the_script_runs_every_step_in_a_throwaway_export_and_leaves_the_checkout_alone(tmp_path):
    """With every tool answering as a fresh node's would, the script passes; kontroll-init and every
    `ansible-playbook` ran inside an export under TMPDIR (kontroll-init's root and each play's cwd), with HOME and
    SOPS_AGE_KEY_FILE there too, KONTROLL_IN_CONTAINER=1 for the two deploy passes and bootstrap, as the installer
    runs them, and for nothing else (the caller's own value dropped before kontroll-init, whose installer-container
    refusal reads it; bootstrap without it runs the legacy host path, whose apt task in check mode read the host's
    python3-apt in T27's first real run, not a fresh node's install), the caller's collections still on the
    collections path; the plays ran in order (the deploy, the deploy of every service, bootstrap, then ping and
    backup-configs against the mock inventory, the captures under TMPDIR); and afterwards the checkout is as
    committed (not even an ignored file), the caller's HOME is empty and TMPDIR is empty. Guards the script run on a
    configured control node writing a second instance/ into the checkout or a second age key into the operator's
    ~/.config/sops/age, and a step that runs the checkout's tree rather than the export."""
    r, calls, p = _run(tmp_path, extra_env={"KONTROLL_IN_CONTAINER": "1"})
    assert r.returncode == 0 and "fresh-node-check: PASS" in r.stdout, r
    tmpdir = str(p["tmpdir"]) + os.sep
    init, plays = calls[0], calls[1:]
    assert init["tool"] == "kontroll-init" and init["root"].startswith(tmpdir) and init["root"].endswith("/tree")
    assert init["argv"] == ["--fresh", "--mgmt-ip", "192.0.2.10", "--domain", "example.test", "--trust-mode",
                            "separated", "--no-break-glass"]
    assert init["env"]["KONTROLL_IN_CONTAINER"] is None
    home = init["env"]["HOME"]
    assert home.startswith(tmpdir) and init["env"]["SOPS_AGE_KEY_FILE"] == home + "/.config/sops/age/keys.txt"
    assert [next(a for a in c["argv"] if a.startswith("playbooks/")) for c in plays] == [
        "playbooks/deploy-stack.yml", "playbooks/deploy-stack.yml", "playbooks/bootstrap.yml",
        "playbooks/ping.yml", "playbooks/backup-configs.yml"]
    for c in plays:
        assert c["cwd"] == init["root"] + "/ansible" and "--check" in c["argv"], c
        assert c["env"]["HOME"] == home and c["env"]["SOPS_AGE_KEY_FILE"] == init["env"]["SOPS_AGE_KEY_FILE"], c
        assert str(p["home"]) + "/.ansible/collections" in c["env"]["ANSIBLE_COLLECTIONS_PATH"].split(":"), c
    assert [c["env"]["KONTROLL_IN_CONTAINER"] for c in plays] == ["1", "1", "1", None, None]
    assert json.loads(plays[1]["argv"][plays[1]["argv"].index("-e") + 1]) == {"stack_services": ALL}
    for c in plays[3:]:
        assert c["argv"][c["argv"].index("-i") + 1] == "../tests/mock-inventory.yml", c
    backup_dir = plays[4]["argv"][plays[4]["argv"].index("-e") + 1]
    assert backup_dir.startswith("config_backup_dir=" + tmpdir), backup_dir
    st = subprocess.run(["git", "-C", str(p["repo"]), "status", "--porcelain", "--ignored", "--untracked-files=all"],
                        capture_output=True, text=True, check=True)
    assert st.stdout == "", st.stdout
    assert os.listdir(p["home"]) == [] and os.listdir(p["tmpdir"]) == []


def test_every_play_runs_in_a_repository_on_main_holding_heads_tree(tmp_path):
    """Every `ansible-playbook` the script starts runs in a tree that is a git repository of its own (its top the
    export, not the checkout), on `main`, whose commit holds exactly the files of the checkout's HEAD. Guards the
    export left a bare `git archive` tree: local-canonical.yml, which deploy-stack.yml imports, refuses to commit
    from a tree git cannot read as a worktree, so with the real tools the deploy pass failed on every run (T27's
    first real run, on both validate cores) while the fakes here, which read no git, passed; and a repository on
    another branch, or missing a tracked file, which a fresh clone never is."""
    r, calls, p = _run(tmp_path)
    assert r.returncode == 0, r
    head = subprocess.run(["git", "-C", str(p["repo"]), "ls-tree", "-r", "--name-only", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    plays = [c for c in calls if c["tool"] == "ansible-playbook"]
    assert len(plays) == 5, calls
    for c in plays:
        g = c["git"]
        assert g["top"] and os.path.realpath(g["top"]) == os.path.realpath(os.path.dirname(c["cwd"])), c
        assert g["branch"] == "main" and g["files"] == head, c


@pytest.mark.parametrize("mode", ["other-red", "all-pass"])
def test_the_full_stack_pass_must_fail_for_the_age_key_reason(tmp_path, mode):
    """The deploy of every service failing for another reason, or passing, fails the script, naming that step
    (`deploy-check-all`) and printing the play's own output. Guards the second pass degrading to an exit-code check:
    the G9 refusal (the onboard-gui's age key MISSING, which deploy-stack never mints) is the one red a fresh node
    must show there, and a play that dies first, on anything else, would otherwise pass as that refusal."""
    r, _, p = _run(tmp_path, mode)
    assert r.returncode == 1 and "fresh-node-check: PASS" not in r.stdout, r
    assert "deploy-check-all" in r.stderr, r.stderr
    assert ("something else broke" in r.stderr) if mode == "other-red" else ("expected fail" in r.stderr), r.stderr
    assert os.listdir(p["tmpdir"]) == []


@pytest.mark.parametrize("how", ["no-recap", "failed-host"])
@pytest.mark.parametrize("step, play", [("deploy-check", "playbooks/deploy-stack.yml"),
                                        ("bootstrap-check", "playbooks/bootstrap.yml"),
                                        ("ping-check", "playbooks/ping.yml")])
def test_a_play_that_exits_0_without_a_clean_recap_fails_the_check(tmp_path, how, step, play):
    """For each step that must pass (the deploy, bootstrap, ping), a play that exits 0 but prints no
    PLAY RECAP (it ran no play), or whose recap names a failed host, fails the script at that step. Guards a pass
    read from the exit alone: a play whose hosts all fell out before the first task, or an `ignore_errors` that hid
    a host's failure, would then pass the step that exists to show the play evaluated."""
    r, _, _ = _run(tmp_path, "%s:%s" % (how, play))
    assert r.returncode == 1 and ("FAIL — %s's log has no PLAY RECAP" % step) in r.stderr, r


@pytest.mark.parametrize("step, play", [("deploy-check", "playbooks/deploy-stack.yml"),
                                        ("bootstrap-check", "playbooks/bootstrap.yml"),
                                        ("ping-check", "playbooks/ping.yml")])
def test_a_play_that_must_pass_and_exits_non_zero_fails_at_its_exit(tmp_path, step, play):
    """For each step that must pass, a play that exits non-zero fails the script at that step, by its exit, and the
    message prints the play's own output. Guards the step's exit check being dropped for the recap alone: the
    failure would then be reported as a missing recap, hiding the exit and the reason the play gave."""
    r, _, _ = _run(tmp_path, "red:%s" % play)
    assert r.returncode == 1 and ("FAIL — %s exited 4, expected pass" % step) in r.stderr, r
    assert "the playbook could not be read" in r.stderr, r.stderr


@pytest.mark.parametrize("mode, says", [
    ("backup:pass", "backup-check exited 0, expected fail"),
    ("backup:no-summary", "backup-check's log does not say the run reached its summary"),
    ("red:playbooks/backup-configs.yml", "backup-check's log does not say the run reached its summary"),
    ("backup:no-recap", "backup-check: the hosts that failed are not the listed ones"),
    ("backup:other-host", "'mock-ap': 'backend_raw_ssh : Write capture'"),
    ("backup:other-task", "'mock-edge': 'opnsense : Resolve capture filename'"),
    ("backup:listed-passes", "backup-check: the hosts that failed are not the listed ones")])
def test_the_backup_pass_must_fail_where_the_mock_inventory_stops_it_and_nowhere_else(tmp_path, mode, says):
    """The backup pass against the mock inventory must fail, reach its summary, and have exactly the listed hosts
    fail, each at its listed task (the read of its device, or the API-credential gate a fresh node cannot pass). It
    passing, dying before its summary, printing no recap, failing one more host, failing a listed host at another
    task, or a listed host passing each fails the script at that step, and a stop mismatch names what the log held.
    Guards the step degrading to "any red is fine": backup-configs.yml rescues nothing, so under the mock's local
    connection those classes fail by design, and a host that fails anywhere else is a play that did not evaluate (T27's
    first real run: the openwrt and RouterOS captures failed their write in every --check run)."""
    r, _, p = _run(tmp_path, mode)
    assert r.returncode == 1 and "fresh-node-check: PASS" not in r.stdout, r
    assert says in r.stderr, r.stderr
    assert os.listdir(p["tmpdir"]) == []


def test_the_backup_pass_says_where_each_host_stopped(tmp_path):
    """With the fake answering as the real play does against the mock inventory, the backup step passes and says the
    run reached its summary and the hosts that failed were the listed ones, mock-switch's stop read from its last
    failure line, after an ignored one. Guards the parse reading the recap or the failure lines wrongly (a host's
    first failure, which `ignore_errors` may have let pass, rather than the one that stopped it), which would fail
    every run, or (with the exact match dropped) pass any."""
    r, _, _ = _run(tmp_path)
    assert r.returncode == 0, r
    assert "fresh-node-check: backup-check — ok (rc=2, expected fail)" in r.stdout, r.stdout
    assert "fresh-node-check: backup-check — says the run reached its summary" in r.stdout, r.stdout
    assert "fresh-node-check: backup-check — the hosts that failed, and where, are the listed ones" in r.stdout


@pytest.mark.parametrize("mode, says", [("init-unfinished", "init's log does not say kontroll-init finished"),
                                        ("init-no-key", "kontroll-init wrote no key"),
                                        ("init-no-sops", "kontroll-init wrote no key")])
def test_a_kontroll_init_that_did_not_finish_fails_the_check(tmp_path, mode, says):
    """kontroll-init exiting 0 without its last line, or without writing the control key or instance/.sops.yaml,
    fails the script before any play runs. Guards the plays then running against an instance that was never
    scaffolded: their red, or their pass, would say nothing about what a fresh node's install does."""
    r, calls, _ = _run(tmp_path, mode)
    assert r.returncode == 1 and says in r.stderr, r
    assert [c["tool"] for c in calls] == ["kontroll-init"], calls


def test_keep_leaves_the_directory_and_names_it(tmp_path):
    """With KONTROLL_FRESH_KEEP=1 the script passes, leaves its one directory under TMPDIR with every step's log in
    it, and prints that directory's path. Guards the way a maintainer reads a run's whole logs afterwards (a failing
    step prints only its last lines), and that the default, without it, is still to remove the directory (the
    first row's empty TMPDIR)."""
    r, _, p = _run(tmp_path, extra_env={"KONTROLL_FRESH_KEEP": "1"})
    kept = os.listdir(p["tmpdir"])
    assert r.returncode == 0 and len(kept) == 1 and kept[0].startswith("kontroll-fresh."), (r, kept)
    d = os.path.join(str(p["tmpdir"]), kept[0])
    assert ("fresh-node-check: kept " + d) in r.stdout, r.stdout
    assert sorted(os.listdir(os.path.join(d, "logs"))) == sorted(
        n + ".log" for n in ("init", "deploy-check", "deploy-check-all", "bootstrap-check", "ping-check",
                             "backup-check"))
