"""deploy-stack.yml's storage-root tasks inside the installer, run for real: the root the installer binds is the
default there, and a root or a store it does not bind is refused before anything is made (I-26).

WHY: the installer (docker/services/installer.yaml) binds the storage root its launcher's shell names
(KONTROLL_STORAGE_ROOT, default /var/lib/kontroll) at the same path, and only that one. The deploy took its root from
`-e` or the instance vars alone. A root named only there was made in the container's own filesystem and gone with
it, while the stack's binds of it on the host got Docker's root:root 0755 directories and the G9 gate stat'd the
container's path, never the key the operator installed on the host; a root named only in the shell was bound and
never used. A store with its own knob (kontroll_logs_dir and the rest) set outside the root went the same way. Each
row runs the playbook's own tasks, copied as they are (the resolve, the two refusals and the per-store derivation),
under ansible-playbook with the installer's variable set or not and the operator's `-e`, and reads back what they
resolved or the refusal. Off the installer (the variable unset) nothing changes, and these rows hold that too.
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
    refusal follows their derivation at once, and comes before every task that names a store. Guards the claim both
    refusals print, that nothing has been made: a refusal placed after the first `file` task would refuse with a
    directory already made in the installer's filesystem, or, on a root the installer does bind, on the host."""
    tasks = _deploy_tasks()
    resolve, unsafe, refusal = (_index(tasks, n) for n in (_RESOLVE, _UNSAFE, _ROOT_REFUSAL))
    derive, stores = _index(tasks, _DERIVE), _index(tasks, _STORE_REFUSAL)
    assert (unsafe, refusal) == (resolve + 1, resolve + 2), (resolve, unsafe, refusal)
    assert stores == derive + 1, (derive, stores)
    ours = {resolve, unsafe, refusal, derive, stores}
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


def _run(tmp_path, installer_root, extra):
    """Run the five tasks, copied from deploy-stack.yml, under ansible-playbook with KONTROLL_INSTALLER_STORAGE_ROOT
    set to `installer_root` (None: unset, as off the installer) and `extra` as `-e`. Returns
    {"resolved": {var: value}} or {"refused": [each refusal's message]}."""
    tasks = _deploy_tasks()
    mine = [tasks[_index(tasks, n)] for n in (_RESOLVE, _UNSAFE, _ROOT_REFUSAL, _DERIVE, _STORE_REFUSAL)]
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
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
        "ansible_connection": "local", "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
        encoding="utf-8")
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
    return json.loads(out.read_text(encoding="utf-8"))


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
])
def test_a_root_and_stores_the_installer_binds_resolve(tmp_path, installer_root, extra, root, logs):
    """Off the installer (its variable unset) the root and the stores resolve as before: the default, or what `-e`
    names, a store on another disk included. Inside it the root it binds is the default, so an exported
    KONTROLL_STORAGE_ROOT alone relocates the root and its stores; the same root written with a trailing or a
    doubled slash, a store under the root however its slashes run, and a root with a regex character in its name
    all pass. Guards I-26's half that was silent: a root named only in the launcher's shell, bound and never used."""
    got = _run(tmp_path, installer_root, extra)
    assert "resolved" in got, got
    assert (got["resolved"]["kontroll_storage_root"], got["resolved"]["kontroll_logs_dir"]) == (root, logs), got


@_ANSIBLE
@pytest.mark.integration
@pytest.mark.parametrize("installer_root,extra,said", [
    ("/var/lib/kontroll", {"kontroll_storage_root": "/data/k"},
     ["kontroll_storage_root is /data/k (an -e or an instance var)", "which binds /var/lib/kontroll from the host",
      "export KONTROLL_STORAGE_ROOT=/data/k before the launcher", "Nothing has been made under either."]),
    ("/data/k", {"kontroll_storage_root": "/var/lib/kontroll"},
     ["kontroll_storage_root is /var/lib/kontroll", "which binds /data/k from the host"]),
    ("/data/k", {"kontroll_logs_dir": "/disk2/loki"},
     ["kontroll_logs_dir is /disk2/loki, outside the storage root /data/k", "at /data/k/loki, say",
      "Nothing has been made there."]),
    ("/data/k", {"kontroll_api_audit_dir": "/var/lib/kontroll/api/audit/"},
     ["kontroll_api_audit_dir is /var/lib/kontroll/api/audit/, outside the storage root /data/k",
      "at /data/k/audit, say"]),
    ("/data/k", {"kontroll_backups_dir": "/data/kk/backups"}, ["kontroll_backups_dir is /data/kk/backups, outside"]),
    ("/data/k+1", {"kontroll_logs_dir": "/data/kk1/loki"}, ["kontroll_logs_dir is /data/kk1/loki, outside"]),
])
def test_a_root_or_a_store_the_installer_does_not_bind_is_refused(tmp_path, installer_root, extra, said):
    """Inside the installer, a root other than the one it binds (named by `-e` or an instance var) is refused with
    both roots named and the remedy, an `export` of the one root before the launcher; a store set outside the root
    (another disk, the default root's path after a relocation, a sibling whose name only begins like the root's, a
    root whose name a regex would read loosely) is refused by name with a mount under the root as the remedy. Guards
    I-26: each was made in the installer's own filesystem and gone with it, and the stack bound Docker-made
    root:root 0755 directories on the host in its place."""
    got = _run(tmp_path, installer_root, extra)
    assert "refused" in got and len(got["refused"]) == 1, got
    for phrase in said:
        assert phrase in got["refused"][0], (phrase, got["refused"][0])
