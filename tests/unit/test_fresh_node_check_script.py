"""tests/fresh-node-check.sh, run against a scratch checkout with fake tools: where it runs each step, with what
environment, and what makes it fail.

WHY — the script is the one gate that makes Ansible evaluate a fresh node's install (CI's validate job runs it), and
two ways it could go wrong would never show as a red run: run on a configured control node, it could mint a second
age key into the operator's ~/.config/sops/age or scaffold an instance/ into the checkout it was started from; and its
passes could degrade to "an exit code is enough" — the full-stack pass, which must FAIL, passing a play that breaks
for a reason that has nothing to do with the age key, or a pass that must succeed passing a play that matched no host,
or a rescue that hid a template error. The real tools are not needed to see either: a fake `ansible-playbook` records
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
ENV = ("HOME", "SOPS_AGE_KEY_FILE", "KONTROLL_IN_CONTAINER", "ANSIBLE_COLLECTIONS_PATH", "KONTROLL_OPERATOR_USER",
       "KONTROLL_OPERATOR_UID", "KONTROLL_OPERATOR_GID", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "ANSIBLE_NOCOLOR",
       "ANSIBLE_FORCE_COLOR", "ANSIBLE_CONFIG")

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

# The stub age-keygen: writes a key where `-o` names one, as the real one does, mode 0600.
_KEYGEN = r'''#!/bin/sh
while [ $# -gt 0 ]; do
  if [ "$1" = -o ]; then echo "AGE-SECRET-KEY-1FAKE" > "$2"; chmod 600 "$2"; fi
  shift
done
'''

# The fake ansible-playbook: records how it was called, what git reads of the tree above its cwd, and (for a deploy
# naming a storage root) the mode of the onboard-gui key there; answers by FAKE_MODE. Every pass that must succeed
# prints its landmark (the stack play's last task, bootstrap's Summary, ping's summary play) and a clean recap.
# ping.yml and backup-configs.yml answer as the real ones do against the mock inventory: ping rescues two hosts at
# their checks, backup stops three at their device's read (every host in the recap, exit 2), and mock-fortigate goes
# past its credential gate where the pass gives it a token (ping says its GET was not made, then OK; backup records
# its capture) and stops there where it has none. `--limit mock-fortigate` runs that host alone. A `<how>:<play>`
# mode bends one play (`ping-gate` and `backup-gate` the limited passes); `ping:<how>` and `backup:<how>` bend those.
_PLAYBOOK = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
sys.stdout.reconfigure(encoding="utf-8")
args = sys.argv[1:]
def git(*a):
    r = subprocess.run(["git", "-C", os.path.dirname(os.getcwd())] + list(a), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None
extra = {}
for i, a in enumerate(args):
    if a.startswith("{"):
        extra.update(json.loads(a))
    elif i and args[i - 1] == "-e" and "=" in a:
        extra.update([a.split("=", 1)])
key = None
if "kontroll_storage_root" in extra:
    p = os.path.join(extra["kontroll_storage_root"], "onboard-gui", "age.key")
    key = oct(os.stat(p).st_mode & 0o777) if os.path.exists(p) else None
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": "ansible-playbook", "cwd": os.getcwd(), "argv": args, "key_mode": key,
                         "env": {k: os.environ.get(k) for k in ENV},
                         "git": {"top": git("rev-parse", "--show-toplevel"),
                                 "branch": git("symbolic-ref", "--short", "HEAD"),
                                 "files": git("ls-tree", "-r", "--name-only", "HEAD")}}) + "\n")
mode = os.environ.get("FAKE_MODE", "")
how, _, target = mode.partition(":")
pb = next(a for a in args if a.startswith("playbooks/"))
limited = "--limit" in args
assert not limited or args[args.index("--limit") + 1] == "mock-fortigate", args
play = {"playbooks/ping.yml": "ping-gate", "playbooks/backup-configs.yml": "backup-gate"}.get(pb) if limited else pb
ignored = {"ping:token-ignored": "playbooks/ping.yml", "backup:token-ignored": "playbooks/backup-configs.yml"}
token = "" if ignored.get(mode) == pb else extra.get("mock_fortigate_api_token", "")
hosts = ["mock-fortigate"] if limited else HOSTS
GATE = "backend_api : Refuse an unresolvable API credential (var NAMES only — never the value)"
everything = "onboard-gui" in extra.get("stack_services", [])
keyed = "kontroll_storage_root" in extra
ROW = "%-26s : ok=3    changed=0    unreachable=%d    failed=%d    skipped=1    rescued=%d    ignored=0"
def recap(rows):
    return "\n".join(["", "PLAY RECAP ****"] + [ROW % r for r in rows])
ok, bad = recap([("localhost", 0, 0, 0)]), recap([("localhost", 0, 1, 0)])
LAND = {"playbooks/deploy-stack.yml": "TASK [Report the running stack containers] ****",
        "playbooks/bootstrap.yml": "TASK [Summary] ****", "playbooks/ping.yml": "PLAY [Reachability summary] ****"}
land = "" if how == "no-landmark" and target == ("gui" if keyed else play) else LAND.get(pb, "")
if how == "unresolved" and target == play:
    print("[ERROR]: couldn't resolve module/action 'community.routeros.command'. This often indicates a "
          "misspelling, missing collection, or incorrect module path." + recap([("mock-routeros", 0, 1, 0)]))
    sys.exit(2)
if everything and keyed:
    if mode == "gui-red":
        print("fatal: [localhost]: FAILED! => an onboard-gui task after G9 broke" + bad); sys.exit(2)
    if target == "gui" and how == "no-recap":
        sys.exit(0)
    if target == "gui" and how == "failed-host":
        print(land + bad); sys.exit(0)
    if target == "gui" and how == "unreachable-host":
        print(land + recap([("localhost", 1, 0, 0)])); sys.exit(0)
    print(land + ok); sys.exit(0)
if everything and mode == "all-pass":
    print(land + ok); sys.exit(0)
if everything and mode == "other-red":
    print("fatal: [localhost]: FAILED! => something else broke" + bad); sys.exit(2)
if everything:
    print("onboard-gui's age key at /opt/kontroll/gui/age.key is MISSING. deploy-stack never creates it" + bad)
    sys.exit(2)
if how == "no-recap" and target == play:
    sys.exit(0)
if how == "failed-host" and target == play:
    print(land + bad); sys.exit(0)
if how == "unreachable-host" and target == play:
    print(land + recap([("localhost", 1, 0, 0)])); sys.exit(0)
if how == "red" and target == play:
    print("ERROR! the playbook could not be read"); sys.exit(4)
if pb == "playbooks/ping.yml":
    rescued = [] if limited else list(RESCUES)
    if not token and mode != "ping:gate-passes":
        rescued.append(("mock-fortigate", GATE))
    if mode == "ping:none":
        rescued = []
    elif mode == "ping:other-host":
        rescued.append(("mock-hv", "proxmox : SSH liveness ping"))
    elif mode == "ping:other-task":
        rescued[0] = ("mock-switch", "backend_netcommon_cli : Read the version")
    elif mode == "ping:gate-other-task" and limited:
        rescued[0] = ("mock-fortigate", "backend_api : GET the check endpoint")
    why = {"mock-fortigate": "mock-fortigate: no API token is in scope, so this check cannot authenticate."}
    other = {"ping:other-reason": "mock-switch", "ping:other-reason-xe": "mock-xe",
             "ping:other-reason-fortigate": "mock-fortigate"}
    if mode in other:
        why[other[mode]] = "The task includes an option with an undefined variable"
    out = []
    for h, t in rescued:
        out += ["", "TASK [%s] ****" % t, 'fatal: [%s]: FAILED! => {"changed": false, "msg": "%s"}' % (
            h, why.get(h, "Connection type local is not valid for this module"))]
    if token and mode != "ping:api-get-unsaid":
        out += ["", "TASK [backend_api : Report the check endpoint as not read in a dry-run] ****",
                "ok: [mock-fortigate] => {", '    "msg": "mock-fortigate: the fortios check endpoint is not read in a '
                'dry-run (`uri` has no check mode), so its API was not asked; a real run reads it."', "}"]
    out += ["", land, "", "TASK [Per-host reachability] ****"]
    for h in hosts:
        down = (any(h == s for s, _ in rescued) and not (mode == "ping:gate-ok" and limited)) or (
            mode == "ping:api-not-ok" and h == "mock-fortigate")
        out += ["ok: [%s] => {" % h, '    "msg": "%s: %s"' % (h, "UNREACHABLE" if down else "OK"), "}"]
    out += [recap([(h, 0, 0, any(h == s for s, _ in rescued)) for h in hosts])]
    print("\n".join(out)); sys.exit(0)
if pb == "playbooks/backup-configs.yml":
    stops = [] if limited else list(STOPS)
    if not token and mode != "backup:gate-passes":
        stops.append(("mock-fortigate", GATE))
    if mode == "backup:pass":
        stops = []
    elif mode == "backup:other-host":
        stops.append(("mock-ap", "backend_raw_ssh : Write capture"))
    elif mode == "backup:other-task":
        stops[0] = ("mock-edge", "opnsense : Resolve capture filename")
    elif mode == "backup:listed-passes":
        stops = stops[:-1]
    elif mode == "backup:gate-other-task" and limited:
        stops[0] = ("mock-fortigate", "backend_api : Pull config via the recipe's backup endpoint")
    out = ["", "TASK [backend_netcommon_cli : A read whose failure is ignored] ****",
           'fatal: [mock-switch]: FAILED! => {"msg": "no answer"}', "...ignoring"]
    if mode == "backup:rescued-first":
        out += ["", "TASK [backend_netcommon_cli : A probe whose failure is rescued] ****",
                'fatal: [mock-xe]: FAILED! => {"msg": "no answer"}']
    for h, t in stops:
        why = ("%s: no API token is in scope, so this capture cannot authenticate." % h
               if h == "mock-fortigate" and mode != "backup:no-token-message" else "censored")
        out += ["", "TASK [%s] ****" % t, 'fatal: [%s]: FAILED! => {"msg": "%s"}' % (h, why)]
    failed = {h for h, _ in stops}
    if mode == "backup:handler":
        out += ["", "RUNNING HANDLER [proxmox : Note the capture] ****", 'fatal: [mock-hv]: FAILED! => {}']
        failed.add("mock-hv")
    if mode == "backup:ignored-after":
        out += ["", "TASK [backend_netcommon_cli : Clean up, errors ignored] ****",
                'fatal: [mock-switch]: FAILED! => {', '    "msg": "nothing to clean"', '}', "...ignoring"]
    if mode == "backup:ignored-loop-after":
        out += ["", "TASK [backend_netcommon_cli : Clean up each, errors ignored] ****",
                'failed: [mock-switch] (item=a) => {"msg": "nothing to clean"}',
                'failed: [mock-switch] (item=b) => {"msg": "nothing to clean"}',
                'fatal: [mock-switch]: FAILED! => {"msg": "One or more items failed"}', "...ignoring"]
    unreach = set()
    if mode == "backup:unreachable-host":
        out += ["", "TASK [proxmox : SSH capture] ****", 'fatal: [mock-hv]: UNREACHABLE! => {"msg": "no route"}']
        unreach.add("mock-hv")
    if mode == "backup:wrote-capture":
        open(os.path.join(extra["config_backup_dir"], "OpenWrt_192-0-2-3.cfg"), "w").close()
    if token and mode != "backup:api-pull-unsaid":
        out += ["", "TASK [backend_api : Report the backup endpoint as not read in a dry-run] ****",
                "ok: [mock-fortigate] => {", '    "msg": "mock-fortigate: the fortios backup endpoint is not read in a '
                'dry-run (`uri` has no check mode), so no config was pulled and none is written; a real run pulls it."',
                "}"]
    if mode != "backup:no-summary" and not set(hosts) <= failed:  # every host failed: Ansible runs no later play
        out += ["", "PLAY [Backup run summary] ****", "", "TASK [Per-host backup result] ****"]
        if token and mode != "backup:api-not-captured":
            out += ["ok: [mock-fortigate] => {",
                    '    "msg": "mock-fortigate (fortigate): captured -> FortiGate_192-0-2-4_full-configuration.conf"',
                    "}"]
    if mode != "backup:no-recap":
        out += [recap([(h, h in unreach, h in failed, 0) for h in hosts])]
    print("\n".join(out)); sys.exit(2 if failed else 0)
print(land + ok)
'''.replace("ENV", repr(ENV)).replace("RESCUES", repr([
    ("mock-switch", "backend_netcommon_cli : Liveness via cli_command (network_cli)"),
    ("mock-xe", "backend_netcommon_cli : Liveness via cli_command (network_cli)")])).replace("STOPS", repr([
        ("mock-edge", "opnsense : Read /conf/config.xml"),
        ("mock-switch", "backend_netcommon_cli : Pull config via cli_command"),
        ("mock-xe", "backend_netcommon_cli : Pull config via cli_command")])).replace("HOSTS", repr(
            ["mock-ap", "mock-docker", "mock-edge", "mock-fortigate", "mock-hv", "mock-routeros", "mock-switch",
             "mock-xe"]))


def _exe(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path, mode="", extra_env=None):
    """A scratch checkout (the real script, the stub kontroll-init, one commit), fake tools first on PATH, an empty
    caller HOME and TMPDIR, and none of the variables the script must set itself inherited (a caller that exports
    PYTHONDONTWRITEBYTECODE, as a mutation harness does, would otherwise meet the test's check for the script); runs
    the script from the checkout and returns (result, calls, paths)."""
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
    _exe(bin_ / "age-keygen", _KEYGEN)
    _exe(bin_ / "sops", "#!/bin/sh\nexit 0\n")
    home, tmpdir, log = tmp_path / "caller-home", tmp_path / "tmpdir", tmp_path / "calls.jsonl"
    home.mkdir()
    tmpdir.mkdir()
    env = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]), HOME=str(home), TMPDIR=str(tmpdir),
               FAKE_LOG=str(log), FAKE_MODE=mode)
    env.pop("ANSIBLE_COLLECTIONS_PATH", None)
    for k in ("KONTROLL_FRESH_KEEP", "PYTHONDONTWRITEBYTECODE", "ANSIBLE_NOCOLOR", "KONTROLL_OPERATOR_USER",
              "KONTROLL_OPERATOR_UID", "KONTROLL_OPERATOR_GID"):
        env.pop(k, None)
    env.update(extra_env or {})
    r = subprocess.run(["bash", str(repo / "tests" / "fresh-node-check.sh")], cwd=str(repo), env=env,
                       capture_output=True, text=True, timeout=120)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return r, calls, {"repo": repo, "home": home, "tmpdir": tmpdir}


def _plays(calls):
    return [c for c in calls if c["tool"] == "ansible-playbook"]


def test_the_script_runs_every_step_in_a_throwaway_export_and_leaves_the_checkout_alone(tmp_path):
    """With every tool answering as a fresh node's would, the script passes; kontroll-init and every
    `ansible-playbook` ran inside an export under TMPDIR (kontroll-init's root and each play's cwd), with HOME and
    SOPS_AGE_KEY_FILE there too, KONTROLL_IN_CONTAINER=1 for the three deploy passes and bootstrap, as the installer
    runs them, and for nothing else (the caller's own value dropped before kontroll-init, whose installer-container
    refusal reads it; bootstrap without it runs the legacy host path, whose apt task in check mode read the host's
    python3-apt in T27's first real run, not a fresh node's install), the caller's collections still on the
    collections path; the plays ran in order (the deploy, the deploy of every service, that again with a key, the
    three with `--diff` as the installer's `check` verb runs them, bootstrap, then ping and backup-configs against the
    mock inventory, each with mock-fortigate's placeholder token and then once more for that host alone without it,
    the captures under TMPDIR); and afterwards
    the checkout is as committed (not even an ignored file), the caller's HOME is empty and TMPDIR is empty. Guards
    the script run on a configured control node writing a second instance/ into the checkout or a second age key into
    the operator's ~/.config/sops/age, and a step that runs the checkout's tree rather than the export."""
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
        "playbooks/deploy-stack.yml", "playbooks/deploy-stack.yml", "playbooks/deploy-stack.yml",
        "playbooks/bootstrap.yml", "playbooks/ping.yml", "playbooks/ping.yml", "playbooks/backup-configs.yml",
        "playbooks/backup-configs.yml"]
    for c in plays:
        assert c["cwd"] == init["root"] + "/ansible" and "--check" in c["argv"], c
        assert c["env"]["HOME"] == home and c["env"]["SOPS_AGE_KEY_FILE"] == init["env"]["SOPS_AGE_KEY_FILE"], c
        assert str(p["home"]) + "/.ansible/collections" in c["env"]["ANSIBLE_COLLECTIONS_PATH"].split(":"), c
    assert all("--diff" in c["argv"] for c in plays[:3]), [c["argv"] for c in plays[:3]]
    assert [c["env"]["KONTROLL_IN_CONTAINER"] for c in plays] == ["1", "1", "1", "1", None, None, None, None]
    assert "-e" not in plays[0]["argv"]
    assert json.loads(plays[1]["argv"][plays[1]["argv"].index("-e") + 1]) == {"stack_services": ALL}
    for c in plays[4:]:
        assert c["argv"][c["argv"].index("-i") + 1] == "../tests/mock-inventory.yml", c
    for c in (plays[4], plays[6]):
        assert "mock_fortigate_api_token=placeholder" in c["argv"] and "--limit" not in c["argv"], c
    for c in (plays[5], plays[7]):
        assert c["argv"][c["argv"].index("--limit") + 1] == "mock-fortigate", c
        assert not any("api_token" in a for a in c["argv"]), c
    for c in plays[6:]:
        backup_dir = next(a for a in c["argv"] if a.startswith("config_backup_dir="))
        assert backup_dir.startswith("config_backup_dir=" + tmpdir), backup_dir
    st = subprocess.run(["git", "-C", str(p["repo"]), "status", "--porcelain", "--ignored", "--untracked-files=all"],
                        capture_output=True, text=True, check=True)
    assert st.stdout == "", st.stdout
    assert os.listdir(p["home"]) == [] and os.listdir(p["tmpdir"]) == []


def test_every_play_runs_as_a_fresh_operator_with_plain_logs_and_its_temporary_files_inside(tmp_path):
    """Every `ansible-playbook` runs with the operator named as the installer's launcher names one
    (KONTROLL_OPERATOR_USER, _UID, _GID = fresh-operator, 1000, 1000), with TMPDIR inside the script's directory,
    PYTHONDONTWRITEBYTECODE=1 and ANSIBLE_NOCOLOR=1, and with neither the caller's ANSIBLE_CONFIG nor its
    ANSIBLE_FORCE_COLOR. Guards: a GitHub-hosted runner is uid 1001, which local-canonical.yml refuses as an operator
    (Q-6), so with no operator named the deploy pass failed on the very runner CI targets (T27's fold-1 read, B1); a
    caller's ANSIBLE_CONFIG replaces the export's ansible.cfg, and colour codes leave the stops' parse nothing to read
    (S4, N7); a module's temporary files and Python's bytecode landed beside the caller's TMPDIR and collections
    (N1)."""
    r, calls, p = _run(tmp_path, extra_env={"ANSIBLE_CONFIG": "/elsewhere/ansible.cfg", "ANSIBLE_FORCE_COLOR": "1",
                                            "KONTROLL_OPERATOR_UID": "1001"})
    assert r.returncode == 0, r
    plays = _plays(calls)
    assert len(plays) == 8, calls
    inside = str(p["tmpdir"]) + os.sep + "kontroll-fresh."
    for c in plays:
        e = c["env"]
        assert (e["KONTROLL_OPERATOR_USER"], e["KONTROLL_OPERATOR_UID"], e["KONTROLL_OPERATOR_GID"]) == (
            "fresh-operator", "1000", "1000"), c
        assert e["TMPDIR"].startswith(inside) and e["TMPDIR"].endswith("/tmp"), c
        assert e["PYTHONDONTWRITEBYTECODE"] == "1" and e["ANSIBLE_NOCOLOR"] == "1", c
        assert e["ANSIBLE_CONFIG"] is None and e["ANSIBLE_FORCE_COLOR"] is None, c


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
    plays = _plays(calls)
    assert len(plays) == 8, calls
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


def test_the_full_stack_pass_runs_again_with_a_throwaway_key_and_must_pass(tmp_path):
    """After the pass that must fail at G9, the script deploys every service again with `kontroll_storage_root`
    inside its directory and a throwaway onboard-gui key there, written by age-keygen and made mode 0644 (G9 asks
    that the GUI's uid 1001 can read it), and that pass must pass; its failing fails the script at
    `deploy-check-gui`. Guards the onboard-gui's tasks after G9 going unevaluated: the pass that must fail stops at
    G9 and the first pass deploys no onboard-gui, so a fault after G9 passed every pass (T27's fold-1 read, S3)."""
    r, calls, p = _run(tmp_path)
    assert r.returncode == 0, r
    gui = _plays(calls)[2]
    extra = json.loads(gui["argv"][gui["argv"].index("-e") + 1])
    assert extra["stack_services"] == ALL, extra
    assert extra["kontroll_storage_root"].startswith(str(p["tmpdir"]) + os.sep + "kontroll-fresh."), extra
    assert gui["key_mode"] == "0o644", gui
    r, _, _ = _run(tmp_path / "red", "gui-red")
    assert r.returncode == 1 and "FAIL — deploy-check-gui exited 2, expected pass" in r.stderr, r
    assert "an onboard-gui task after G9 broke" in r.stderr, r.stderr


@pytest.mark.parametrize("how", ["no-recap", "failed-host", "unreachable-host"])
@pytest.mark.parametrize("step, play", [("deploy-check", "playbooks/deploy-stack.yml"),
                                        ("deploy-check-gui", "gui"),
                                        ("bootstrap-check", "playbooks/bootstrap.yml"),
                                        ("ping-check", "playbooks/ping.yml"),
                                        ("ping-check-gate", "ping-gate")])
def test_a_play_that_exits_0_without_a_clean_recap_fails_the_check(tmp_path, how, step, play):
    """For each step that must pass (the deploy, the deploy with a throwaway onboard-gui key, bootstrap, ping, and
    ping for mock-fortigate alone without its token), a play that exits 0 but prints no
    PLAY RECAP (it ran no play), or whose recap names a failed or an unreachable host, fails the script at that step.
    Guards a pass read from the exit alone: a play whose hosts all fell out before the first task, or an
    `ignore_errors` or `ignore_unreachable` that hid a host's failure, would then pass the step that exists to show
    the play evaluated."""
    r, _, _ = _run(tmp_path, "%s:%s" % (how, play))
    assert r.returncode == 1 and ("FAIL — %s's log has no PLAY RECAP" % step) in r.stderr, r


@pytest.mark.parametrize("mode, step", [("no-landmark:playbooks/deploy-stack.yml", "deploy-check"),
                                        ("no-landmark:gui", "deploy-check-gui"),
                                        ("no-landmark:playbooks/bootstrap.yml", "bootstrap-check"),
                                        ("no-landmark:playbooks/ping.yml", "ping-check"),
                                        ("no-landmark:ping-gate", "ping-check-gate")])
def test_a_play_that_must_pass_and_never_reaches_its_end_fails_the_check(tmp_path, mode, step):
    """For each pass that must succeed, a play that exits 0 with a clean recap but never prints its landmark (the
    stack play's last task for the deploys, bootstrap's Summary task, ping's summary play) fails the script at that
    step, naming what its log does not say. Guards a pass read from the recap alone: a play that matched no host, or
    ran no task, prints an empty recap or one with no failure, and T27's fold-1 read passed bootstrap, ping and the
    stack play each with its `hosts:` misspelt."""
    r, _, _ = _run(tmp_path, mode)
    assert r.returncode == 1 and ("FAIL — %s's log does not say" % step) in r.stderr, r


@pytest.mark.parametrize("step, play", [("deploy-check", "playbooks/deploy-stack.yml"),
                                        ("bootstrap-check", "playbooks/bootstrap.yml"),
                                        ("ping-check", "playbooks/ping.yml"),
                                        ("ping-check-gate", "ping-gate")])
def test_a_play_that_must_pass_and_exits_non_zero_fails_at_its_exit(tmp_path, step, play):
    """For each step that must pass, a play that exits non-zero fails the script at that step, by its exit, and the
    message prints the play's own output. Guards the step's exit check being dropped for the recap alone: the
    failure would then be reported as a missing recap, hiding the exit and the reason the play gave."""
    r, _, _ = _run(tmp_path, "red:%s" % play)
    assert r.returncode == 1 and ("FAIL — %s exited 4, expected pass" % step) in r.stderr, r
    assert "the playbook could not be read" in r.stderr, r.stderr


@pytest.mark.parametrize("mode, says", [
    ("ping:none", "ping-check: the hosts it rescued are not the listed ones"),
    ("ping:other-host", "'mock-hv': 'proxmox : SSH liveness ping'"),
    ("ping:other-task", "'mock-switch': 'backend_netcommon_cli : Read the version'"),
    ("ping:other-reason", "ping-check's log does not say mock-switch was refused its network_cli connection"),
    ("ping:other-reason-xe", "ping-check's log does not say mock-xe was refused its network_cli connection"),
    ("ping:token-ignored", "'mock-fortigate': 'backend_api : Refuse an unresolvable API credential"),
    ("ping:api-get-unsaid", "ping-check's log does not say mock-fortigate's check went past its credential gate"),
    ("ping:api-not-ok", "ping-check's log does not say mock-fortigate ended OK"),
    ("ping:gate-passes", "ping-check-gate: the hosts it rescued are not the listed ones"),
    ("ping:gate-other-task", "'mock-fortigate': 'backend_api : GET the check endpoint'"),
    ("ping:other-reason-fortigate",
     "ping-check-gate's log does not say mock-fortigate stopped at its credential gate"),
    ("ping:gate-ok", "ping-check-gate's log does not say mock-fortigate was reported UNREACHABLE")])
def test_the_ping_pass_must_rescue_exactly_the_listed_hosts_each_at_its_task(tmp_path, mode, says):
    """ping.yml must pass, and the hosts it rescues (and reports UNREACHABLE) must be exactly the listed ones, each
    rescued from its listed task (the class's read of its device) for its listed reason; mock-fortigate, given a
    placeholder token, must say the GET a dry-run skips was not made and end OK; and the pass for that host alone
    without the token must rescue it at its credential gate, for that reason, and report it UNREACHABLE. None
    rescued, one more, a listed host rescued from another task or at its task for another reason, mock-fortigate
    stopped at its gate though the token was given, saying nothing of its GET or ending UNREACHABLE, or its gate
    passed, missed or reported OK without the token, each fails the script at that step, naming what the log held.
    Guards a class's `check` that does not evaluate passing as a device that did not answer: ping.yml's rescue
    catches a template error too, and T27's fold-1 read passed an undefined variable in proxmox's check that way
    (S2); the fold-2 read passed one in the very task the netcommon classes are listed at, which only the reason
    tells apart (its S2). And it guards I-6: with a credential, the API class's check read the GET a dry-run skips
    as a failed answer, and ping reported the host UNREACHABLE; and a rescue widened so that host ended OK passed
    the pass without the token (I-6's runtime read, N2)."""
    r, _, p = _run(tmp_path, mode)
    assert r.returncode == 1 and "fresh-node-check: PASS" not in r.stdout, r
    assert says in r.stderr, r.stderr
    assert os.listdir(p["tmpdir"]) == []


@pytest.mark.parametrize("mode, says", [
    ("backup:pass", "backup-check exited 0, expected fail"),
    ("backup:no-summary", "backup-check's log does not say the run reached its summary"),
    ("red:playbooks/backup-configs.yml", "backup-check's log does not say the run reached its summary"),
    ("backup:no-token-message", "backup-check-gate's log does not say the API class stopped at its credential gate"),
    ("backup:token-ignored", "'mock-fortigate': 'backend_api : Refuse an unresolvable API credential"),
    ("backup:api-pull-unsaid",
     "backup-check's log does not say mock-fortigate's capture went past its credential gate to the pull a dry-run"),
    ("backup:api-not-captured",
     "backup-check's log does not say mock-fortigate's capture went past its credential gate to its write"),
    ("backup:gate-passes", "backup-check-gate exited 0, expected fail"),
    ("backup:gate-other-task", "'mock-fortigate': \"backend_api : Pull config via the recipe's backup endpoint\""),
    ("backup:no-recap", "backup-check: the hosts that failed are not the listed ones"),
    ("backup:other-host", "'mock-ap': 'backend_raw_ssh : Write capture'"),
    ("backup:other-task", "'mock-edge': 'opnsense : Resolve capture filename'"),
    ("backup:handler", "'mock-hv': 'proxmox : Note the capture'"),
    ("backup:listed-passes", "backup-check: the hosts that failed are not the listed ones"),
    ("backup:unreachable-host", "'mock-hv': 'proxmox : SSH capture'"),
    ("backup:wrote-capture", "a --check run wrote a capture under")])
def test_the_backup_pass_must_fail_where_the_mock_inventory_stops_it_and_nowhere_else(tmp_path, mode, says):
    """The backup pass against the mock inventory must fail, reach its summary, have exactly the listed hosts fail,
    each at its listed task (the read of its device), say mock-fortigate's pull was not made and record its capture
    (given a placeholder token, it goes past its credential gate to the pull a dry-run skips, and to its write,
    which the record follows), and write no capture; the pass for that host alone without the token must fail it at
    its credential gate, for that reason (its one host failed, so that run has no summary). It passing, dying before
    its summary, printing no recap, failing one more host (in a task or in a handler, which is charged to the
    handler and not to the task before it) or leaving one unreachable, failing a listed host at another task, a
    listed host passing, mock-fortigate stopped at its gate though the token was given, saying nothing of its pull
    or recording no capture, the pass without the token passing or stopping it elsewhere or for another reason, or a
    capture written by the dry-run each fails the script at that step, and a stop mismatch names what the log held,
    with the step to take. A dry-run that writes a capture would, on a real fleet, overwrite the last good one with
    the empty preview (T27's fold-2 read planted `check_mode: false` on the raw-SSH write and the step passed, N6).
    Guards the step degrading to "any red is fine": backup-configs.yml rescues nothing, so under the mock's local
    connection those classes fail by design, and a host that fails anywhere else is a play that did not evaluate
    (T27's first real run: the openwrt and RouterOS captures failed their write in every --check run). And it guards
    I-6: with a credential, the API class's capture read the pull a dry-run skips as a failed answer and failed the
    host; it now reaches its write, which the empty-directory check holds as it holds the others. Its record
    followed a copy of the write's condition, not the write, so a dry-run write skipped still said "captured" and
    passed (I-6's runtime read, N1); the record now follows the write."""
    r, _, p = _run(tmp_path, mode)
    assert r.returncode == 1 and "fresh-node-check: PASS" not in r.stdout, r
    assert says in r.stderr, r.stderr
    if "are not the listed ones" in r.stderr:
        assert "is listed in this script as host=role : task" in r.stderr, r.stderr
    assert os.listdir(p["tmpdir"]) == []


@pytest.mark.parametrize("mode", ["", "backup:ignored-after", "backup:ignored-loop-after", "backup:rescued-first"])
def test_the_backup_pass_says_where_each_host_stopped(tmp_path, mode):
    """With the fake answering as the real play does against the mock inventory, the backup step passes and says the
    run reached its summary and the hosts that failed were the listed ones, each host's stop read from its last
    failure line that was not ignored: mock-switch's after an ignored one, and before one that is (its result printed
    over several lines, as Ansible does for a structured one, or a loop's, whose items fail one line each before the
    task's own line); mock-xe's after a failure a `rescue` took, which is not where it stopped. Guards the parse
    reading the recap or the failure lines wrongly (a host's first failure, which `ignore_errors` may have let pass or
    a `rescue` recovered, or a cleanup's ignored failure after the one that stopped it, rather than that one), which
    would fail every run, or (with the exact match dropped) pass any."""
    r, _, _ = _run(tmp_path, mode)
    assert r.returncode == 0, r
    assert "fresh-node-check: backup-check — ok (rc=2, expected fail)" in r.stdout, r.stdout
    assert "fresh-node-check: backup-check — says the run reached its summary" in r.stdout, r.stdout
    assert "fresh-node-check: backup-check — the hosts that failed, and where, are the listed ones" in r.stdout
    assert "fresh-node-check: ping-check — the hosts it rescued, and where, are the listed ones" in r.stdout


def test_a_module_no_installed_collection_provides_is_named_with_the_install_that_fixes_it(tmp_path):
    """A play that fails because Ansible could not resolve a module (its collection is not installed) fails the
    script at that step, and the message names the module and the install of every module's set
    (`gen-requirements.py <file> --all-modules`). Guards the failure CI's first run would have shown: CI installed the
    example fleet's collections, the mock inventory has a host for every class, and the backup step said only that
    the run never reached its summary, naming neither community.routeros nor the fix (T27's fold-1 read, B2)."""
    r, _, _ = _run(tmp_path, "unresolved:playbooks/backup-configs.yml")
    assert r.returncode == 1, r
    assert "backup-check — no installed collection provides community.routeros.command" in r.stderr, r.stderr
    assert "gen-requirements.py <file> --all-modules" in r.stderr, r.stderr


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
        n + ".log" for n in ("init", "deploy-check", "deploy-check-all", "deploy-check-gui", "bootstrap-check",
                             "ping-check", "ping-check-gate", "backup-check", "backup-check-gate"))
