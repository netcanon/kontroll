"""deploy-stack.yml's storage-root tasks inside the installer, run for real: the root the installer binds is the
default there, and a root or a store it does not bind is refused before anything is made (I-26).

WHY: the installer (docker/services/installer.yaml) binds the storage root its launcher's shell names
(KONTROLL_STORAGE_ROOT, default /var/lib/kontroll) at the same path, and only that one. The deploy took its root from
`-e` or the instance vars alone. A root named only there was made in the container's own filesystem and gone with
it, while the stack's binds of it on the host got Docker's root:root 0755 directories and the G9 gate stat'd the
container's path, never the key the operator installed on the host; a root named only in the shell was bound and
never used. A store with its own knob (kontroll_logs_dir and the rest) set outside the root went the same way, and
so did one that a `..` or a link under the root leads out of it. Each row runs the playbook's own tasks, copied as
they are (the resolve, the two refusals, the per-store derivation and the line naming the root), under
ansible-playbook with the installer's variable set or not, the operator's `-e` and inventory vars (where the
instance's sit), and reads back what they resolved, the refusal, and the line. Off the installer (the variable
unset) nothing changes, and these rows hold that too.
It needs POSIX and ansible-playbook (CI's validate job installs it); without them every row is skipped.
"""
import json
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")
INSTALLER = os.path.join(ROOT, "docker", "services", "installer.yaml")

_RESOLVE = "Resolve the storage-location root"
_UNSAFE = "Fail closed on an unsafe storage root"
_ROOT_REFUSAL = "Refuse a storage root other than the one the installer binds"
_DERIVE = "Derive the per-store dirs from the validated root"
_STORE_REFUSAL = "Refuse a store the installer does not bind"
_SAY = "Say which storage root this run uses"
_STORES = ("kontroll_logs_dir", "kontroll_backups_dir", "kontroll_api_audit_dir", "kontroll_gui_audit_dir",
           "kontroll_ansible_log_dir")


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _deploy_tasks():
    with open(DEPLOY, encoding="utf-8") as fh:
        return [t for t in _tasks(yaml.safe_load(fh.read())) if "name" in t and "hosts" not in t]


def _index(tasks, prefix):
    found = [i for i, t in enumerate(tasks) if str(t["name"]).startswith(prefix)]
    assert len(found) == 1, (prefix, found)
    return found[0]


def test_the_two_refusals_come_before_anything_is_made_under_the_root():
    """In deploy-stack.yml the root's refusal follows the resolve and the unsafe-root assert at once, and comes before
    every other task that names the storage root (the stat and the making of a missing root among them); the stores'
    refusal follows their derivation at once, and comes before every task that names a store; the line naming the
    root follows the stores' refusal at once. Guards the claim both refusals print, that nothing has been made: a
    refusal placed after the first `file` task would refuse with a directory already made in the installer's
    filesystem, or, on a root the installer does bind, on the host. And a line before a refusal would name a root
    the run is about to refuse."""
    tasks = _deploy_tasks()
    resolve, unsafe, refusal = (_index(tasks, n) for n in (_RESOLVE, _UNSAFE, _ROOT_REFUSAL))
    derive, stores, say = _index(tasks, _DERIVE), _index(tasks, _STORE_REFUSAL), _index(tasks, _SAY)
    assert (unsafe, refusal) == (resolve + 1, resolve + 2), (resolve, unsafe, refusal)
    assert (stores, say) == (derive + 1, derive + 2), (derive, stores, say)
    ours = {resolve, unsafe, refusal, derive, stores, say}
    naming_root = [i for i, t in enumerate(tasks) if i not in ours and "kontroll_storage_root" in yaml.safe_dump(t)]
    assert naming_root and min(naming_root) > refusal, [tasks[i]["name"] for i in naming_root if i < refusal]
    naming_store = [i for i, t in enumerate(tasks)
                    if i not in ours and any(s in yaml.safe_dump(t) for s in _STORES)]
    assert naming_store and min(naming_store) > stores, [tasks[i]["name"] for i in naming_store if i < stores]


def test_the_installer_passes_the_root_it_binds_as_it_binds_it():
    """docker/services/installer.yaml hands the container KONTROLL_INSTALLER_STORAGE_ROOT written exactly as the
    storage root's bind source is, and binds that path at the same path. Guards the two drifting apart: the deploy
    would then take as "the root the installer binds" a path the installer does not bind, and the refusal would let
    through the very case it is for."""
    with open(INSTALLER, encoding="utf-8") as fh:
        init = yaml.safe_load(fh)["services"]["init"]
    value = init["environment"]["KONTROLL_INSTALLER_STORAGE_ROOT"]
    assert value == "${KONTROLL_STORAGE_ROOT:-/var/lib/kontroll}", value
    assert "%s:%s" % (value, value) in init["volumes"], init["volumes"]


def _run(tmp_path, installer_root, extra, host_vars=None):
    """Run the storage-root tasks, copied from deploy-stack.yml, under ansible-playbook with KONTROLL_INSTALLER_STORAGE_ROOT
    set to `installer_root` (None: unset, as off the installer), `extra` as `-e` and `host_vars` as the inventory's
    (below set_fact, as the instance vars are). Returns {"resolved": {var: value}} or {"refused": [each refusal's
    message]}, with the run's output under "stdout"."""
    tasks = _deploy_tasks()
    mine = [tasks[_index(tasks, n)] for n in (_RESOLVE, _UNSAFE, _ROOT_REFUSAL, _DERIVE, _STORE_REFUSAL, _SAY)]
    out = tmp_path / "out.json"
    resolved = "{{ {'kontroll_storage_root': kontroll_storage_root, %s} | to_json }}" % ", ".join(
        "'%s': %s" % (s, s) for s in _STORES)
    refused = ("{{ {'refused': ([ansible_failed_result.msg] if 'results' not in ansible_failed_result else"
               " ansible_failed_result.results | selectattr('failed', 'defined') | selectattr('failed')"
               " | map(attribute='msg') | list)} | to_json }}")
    play = [{
        "name": "deploy-stack's storage-root tasks", "hosts": "localhost", "gather_facts": False,
        "tasks": [{
            "name": "The tasks as the playbook has them",
            "block": mine + [{"name": "Write down what they resolved", "ansible.builtin.copy": {
                "content": '{"resolved": ' + resolved + "}", "dest": str(out), "mode": "0600"}}],
            "rescue": [{"name": "Write down the refusal", "ansible.builtin.copy": {
                "content": refused, "dest": str(out), "mode": "0600"}}],
        }],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    home = tmp_path / "ansible-home"
    home.mkdir()
    (home / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n",
                                      encoding="utf-8")
    host = {"ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}
    host.update(host_vars or {})
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": host}}}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(ANSIBLE_CONFIG=str(home / "ansible.cfg"), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               PYTHONDONTWRITEBYTECODE="1")
    if installer_root is not None:
        env["KONTROLL_INSTALLER_STORAGE_ROOT"] = installer_root
    cmd = ["ansible-playbook", "-i", str(tmp_path / "hosts.yml"), str(tmp_path / "play.yml")]
    if extra:
        cmd += ["-e", json.dumps(extra)]
    p = subprocess.run(cmd, cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0 and out.exists(), p.stdout + p.stderr
    got = json.loads(out.read_text(encoding="utf-8"))
    got["stdout"] = p.stdout
    return got


def _said(line):
    """The debug task's line as the default callback prints it."""
    return '"msg": "%s"' % line


_ANSIBLE = pytest.mark.skipif(os.name != "posix" or not shutil.which("ansible-playbook"),
                              reason="needs POSIX and ansible-playbook (it runs deploy-stack.yml's own tasks)")


@_ANSIBLE
@pytest.mark.integration
@pytest.mark.parametrize("installer_root,extra,root,logs", [
    (None, {}, "/var/lib/kontroll", "/var/lib/kontroll/loki"),
    (None, {"kontroll_storage_root": "/data/k"}, "/data/k", "/data/k/loki"),
    (None, {"kontroll_storage_root": "/data/k", "kontroll_logs_dir": "/disk2/loki"}, "/data/k", "/disk2/loki"),
    ("/var/lib/kontroll", {}, "/var/lib/kontroll", "/var/lib/kontroll/loki"),
    ("/data/k", {}, "/data/k", "/data/k/loki"),
    ("/data/k/", {"kontroll_storage_root": "/data//k"}, "/data//k", "/data//k/loki"),
    ("/data/k", {"kontroll_logs_dir": "/data/k//big/loki/"}, "/data/k", "/data/k//big/loki/"),
    ("/data/k+1", {"kontroll_logs_dir": "/data/k+1/loki"}, "/data/k+1", "/data/k+1/loki"),
    ("/data/k", {"kontroll_storage_root": "/data/k/."}, "/data/k/.", "/data/k/./loki"),
    ("/data/k", {"kontroll_storage_root": "/data/x/../k"}, "/data/x/../k", "/data/x/../k/loki"),
    ("/data/x/../k", {"kontroll_storage_root": "/data/k"}, "/data/k", "/data/k/loki"),
    ("/data/k", {"kontroll_logs_dir": "/data/k/big/../loki"}, "/data/k", "/data/k/big/../loki"),
])
def test_a_root_and_stores_the_installer_binds_resolve(tmp_path, installer_root, extra, root, logs):
    """Off the installer (its variable unset) the root and the stores resolve as before: the default, or what `-e`
    names, a store on another disk included. Inside it the root it binds is the default, so an exported
    KONTROLL_STORAGE_ROOT alone relocates the root and its stores; the same root written with a trailing or a
    doubled slash, a `.` or a `..` that leads back to it (on either side), a store under the root however its slashes run or a `..`
    that stays in it, and a root with a regex character in its name all pass. Guards I-26's half that was silent: a
    root named only in the launcher's shell, bound and never used; and a real-path compare that refused the root
    the installer binds, spelled another way."""
    got = _run(tmp_path, installer_root, extra)
    assert "resolved" in got, got
    assert (got["resolved"]["kontroll_storage_root"], got["resolved"]["kontroll_logs_dir"]) == (root, logs), got


@_ANSIBLE
@pytest.mark.integration
@pytest.mark.parametrize("installer_root,extra,said", [
    ("/var/lib/kontroll", {"kontroll_storage_root": "/data/k"},
     ["kontroll_storage_root is /data/k (an -e or an instance var)", "which binds /var/lib/kontroll from the host",
      "export KONTROLL_STORAGE_ROOT=/data/k before the launcher, in every shell that runs it",
      "keep kontroll_storage_root /data/k in the instance vars as well",
      "refused, not moved back to the default root", "Nothing has been made under either."]),
    ("/data/k", {"kontroll_storage_root": "/var/lib/kontroll"},
     ["kontroll_storage_root is /var/lib/kontroll", "which binds /data/k from the host",
      "or keep /data/k and drop kontroll_storage_root"]),
    ("/data/k", {"kontroll_storage_root": "/data/k/../x"}, ["kontroll_storage_root is /data/k/../x"]),
    ("/var/lib/kontroll", {"kontroll_storage_root": "/data/k", "_installer_storage_root": ""},
     ["kontroll_storage_root is /data/k (an -e or an instance var)"]),
    (None, {"kontroll_storage_root": ""}, ["must be an absolute path other than '/' (got '')"]),
    ("/data/k", {"kontroll_logs_dir": "/disk2/loki"},
     ["kontroll_logs_dir is /disk2/loki, outside the storage root /data/k", "at /data/k/logs, say; a mount, not a link",
      "Nothing has been made there."]),
    ("/data/k/", {"kontroll_api_audit_dir": "/var/lib/kontroll/api/audit/"},
     ["kontroll_api_audit_dir is /var/lib/kontroll/api/audit/, outside the storage root /data/k/",
      "at /data/k/api_audit, say"]),
    ("/data/k", {"kontroll_backups_dir": "/data/kk/backups"}, ["kontroll_backups_dir is /data/kk/backups, outside"]),
    ("/data/k+1", {"kontroll_logs_dir": "/data/kk1/loki"}, ["kontroll_logs_dir is /data/kk1/loki, outside"]),
    ("/data/k", {"kontroll_logs_dir": "/mnt/data/k/loki"}, ["kontroll_logs_dir is /mnt/data/k/loki, outside"]),
    ("/data/k", {"kontroll_logs_dir": "/data/k/../elsewhere/loki"},
     ["kontroll_logs_dir is /data/k/../elsewhere/loki (which leads to /data/elsewhere/loki), outside the storage"]),
    ("/data/k", {"kontroll_logs_dir": "/disk2/loki", "_installer_storage_root": ""},
     ["kontroll_logs_dir is /disk2/loki, outside"]),
])
def test_a_root_or_a_store_the_installer_does_not_bind_is_refused(tmp_path, installer_root, extra, said):
    """Inside the installer, a root other than the one it binds (named by `-e` or an instance var, or a `..` that
    leads off it) is refused with both roots named and the remedy: an `export` of the one root in every shell that
    runs the launcher, with the instance var kept as well, so that a later shell without the export is refused, not
    moved back to the default root. A store set outside the root (another disk, the default root's path after a
    relocation, a sibling whose name only begins like the root's, a root whose name a regex would read loosely, a
    path that holds the root's but does not begin with it, a `..` that leads out of it, named as it leads) is
    refused by name, with a mount under the root, at the knob's own name, as the remedy. An `-e` of the fact the
    refusals once read turns neither off, and an empty `-e` root fails closed. Guards I-26: each was made in the
    installer's own filesystem and gone with it, and the stack bound Docker-made root:root 0755 directories on the
    host in its place; and the old remedy, which told the operator to drop the only setting that outlived the
    shell."""
    got = _run(tmp_path, installer_root, extra)
    assert "refused" in got and len(got["refused"]) == 1, got
    for phrase in said:
        assert phrase in got["refused"][0], (phrase, got["refused"][0])


@_ANSIBLE
@pytest.mark.integration
@pytest.mark.parametrize("knob,mount", [
    ("kontroll_logs_dir", "logs"), ("kontroll_backups_dir", "backups"), ("kontroll_api_audit_dir", "api_audit"),
    ("kontroll_gui_audit_dir", "gui_audit"), ("kontroll_ansible_log_dir", "ansible_log"),
])
def test_each_store_outside_the_root_is_refused_with_a_mount_point_of_its_own(tmp_path, knob, mount):
    """Inside the installer, each store with its own knob, set outside the root, is refused by name,
    and the remedy names a mount point under the root from the knob's name. Guards a store left out of the loop
    (made in the installer's filesystem), and the old remedy's mount point, the store's last path component: two
    stores whose paths end alike (api/audit, onboard-gui/audit) were offered the same one, and a store ending in
    `api` the api directory."""
    got = _run(tmp_path, "/data/k", {knob: "/disk2/store"})
    assert "refused" in got and len(got["refused"]) == 1, got
    for phrase in ("%s is /disk2/store, outside the storage root /data/k" % knob,
                   "at /data/k/%s, say; a mount, not a link" % mount, "point %s there" % knob):
        assert phrase in got["refused"][0], (phrase, got["refused"][0])


@_ANSIBLE
@pytest.mark.integration
def test_a_link_under_the_root_leads_where_it_points(tmp_path):
    """Inside the installer, a store through a link under the root that points out of it is refused and named as
    it leads; a root written through a link under it that points back to it is the root, and resolves. Guards the
    text compare the refusals began with: the store passed, and was made through the link wherever it led, in the
    installer's own filesystem."""
    root, away, first, second = (tmp_path / n for n in ("root", "away", "one", "two"))
    for d in (root, away, first, second):
        d.mkdir()
    (root / "out").symlink_to(away)
    (root / "here").symlink_to(root)
    got = _run(first, str(root), {"kontroll_logs_dir": str(root / "out" / "loki")})
    assert "refused" in got, got
    phrase = "kontroll_logs_dir is %s (which leads to %s), outside the storage root %s" % (
        root / "out" / "loki", os.path.realpath(str(away / "loki")), root)
    assert phrase in got["refused"][0], (phrase, got["refused"][0])
    got = _run(second, str(root), {"kontroll_storage_root": str(root / "here")})
    assert "resolved" in got, got
    assert got["resolved"]["kontroll_logs_dir"] == str(root / "here" / "loki"), got


@_ANSIBLE
@pytest.mark.integration
@pytest.mark.parametrize("installer_root,host_vars,outcome,said", [
    ("/var/lib/kontroll", {"kontroll_storage_root": "/data/k"}, "refused",
     "kontroll_storage_root is /data/k (an -e or an instance var), but this run is inside the installer"),
    ("/data/k", {"kontroll_storage_root": "/var/lib/kontroll"}, "refused",
     "kontroll_storage_root is /var/lib/kontroll"),
    ("/data/k", {"kontroll_logs_dir": "/disk2/loki"}, "refused", "kontroll_logs_dir is /disk2/loki, outside"),
    ("/data/k", {"kontroll_storage_root": "/data/k"}, "resolved",
     _said("storage root /data/k (an -e or an instance var)")),
    ("/data/k", {"kontroll_storage_root": ""}, "resolved",
     _said("storage root /data/k (the root the installer binds)")),
    ("/data/k", {"kontroll_logs_dir": ""}, "resolved", _said("storage root /data/k (the root the installer binds)")),
])
def test_an_instance_var_is_held_to_the_root_the_installer_binds(tmp_path, installer_root, host_vars, outcome, said):
    """Inside the installer, a root or a store set in the inventory (where the instance vars sit, below set_fact)
    meets the same refusals as an `-e`: another root is refused, and the same root resolves; an empty one falls back
    to the root the installer binds, an empty store to its directory under it. Guards the rows above being `-e`
    only: an `-e` is the one value set_fact cannot override, and the instance var is the setting the remedy tells
    the operator to keep, the one a later shell without the export is held to."""
    got = _run(tmp_path, installer_root, {}, host_vars)
    assert outcome in got, got
    if outcome == "refused":
        assert len(got["refused"]) == 1 and said in got["refused"][0], (said, got["refused"])
    else:
        assert said in got["stdout"], (said, got["stdout"])
        assert got["resolved"]["kontroll_logs_dir"] == "/data/k/loki", got


@_ANSIBLE
@pytest.mark.integration
@pytest.mark.parametrize("installer_root,extra,line", [
    (None, {}, "storage root /var/lib/kontroll (the default)"),
    (None, {"kontroll_storage_root": "/data/k"}, "storage root /data/k (an -e or an instance var)"),
    ("/data/k", {}, "storage root /data/k (the root the installer binds)"),
    ("/data/k", {"kontroll_storage_root": "/data/k/"}, "storage root /data/k/ (an -e or an instance var)"),
])
def test_the_run_says_which_root_it_uses_and_where_it_came_from(tmp_path, installer_root, extra, line):
    """A run that passes both refusals prints one line naming the storage root and its source: the default, an
    `-e` or instance var, or the root the installer binds (KONTROLL_STORAGE_ROOT in the launcher's shell); an `-e`
    equal to the installer's root is named as the `-e`. Guards a relocation from the shell alone that leaves no word
    in the run's output: the root every store lives under, moved by a variable the run never names."""
    got = _run(tmp_path, installer_root, extra)
    assert "resolved" in got, got
    assert _said(line) in got["stdout"], (line, got["stdout"])
