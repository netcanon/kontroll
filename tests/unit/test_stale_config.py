"""A deploy restarts each service whose read-only config changed since it loaded it — the digest and the tasks.

WHY: a service that reads a bound config only when it starts (Vector, Grafana's provisioning, Loki, Prometheus,
the exporters, Caddy) keeps the config it started with, and `docker compose up` recreates a container only when
its service definition changed. On both boxes' redeploy (2026-10-22) the new Vector transforms and Grafana alert
rules reached the disk and not the services: the apply reported success, and Vector kept logging the warnings the
build had fixed until both were restarted by hand. scripts/stale-config.py names the services whose config
changed, as `docker compose config` resolves their binds; deploy-stack.yml restarts them and records what they
loaded. These rows hold what counts as config (the tree's read-only binds, the files the deploy renders under the
storage root, never a store or a bind the service reloads itself), the digest to content (the generators rewrite
their files every run, so an mtime would restart every service on every deploy and break the second apply's 0
changed), the stamp to what the services loaded (merged, so a one-service deploy does not make the next full one
restart everything), the output to names (the rendered snmp.yml holds credentials, and a digest of it tests
guesses), and the tasks to their order and to never acting in a dry-run.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPT = os.path.join(_ROOT, "scripts", "stale-config.py")
_DEPLOY = os.path.join(_ROOT, "ansible", "playbooks", "deploy-stack.yml")


def _mod():
    spec = importlib.util.spec_from_file_location("stale_config", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bind(source, target, ro=True):
    return {"type": "bind", "source": str(source), "target": target, "read_only": ro}


def _tree(tmp_path):
    """A repo tree, a storage root and a host path, with the resolved config compose would print for them: each
    kind of bind a service may hold. Returns (repo, store, config path)."""
    repo, store, host = tmp_path / "repo", tmp_path / "store", tmp_path / "host"
    (repo / "docker").mkdir(parents=True)
    (repo / "app" / "conf.d").mkdir(parents=True)
    (repo / "app" / "main.yml").write_text("a: 1\n")
    (repo / "app" / "conf.d" / "one.yml").write_text("b: 2\n")
    (repo / "boards").mkdir()
    (repo / "boards" / "b.json").write_text("{}\n")
    (repo / "subboards").mkdir()
    (repo / "subboards" / "s.json").write_text("{}\n")
    (repo / "rules.yml").write_text("r: 1\n")
    (store / "caddy").mkdir(parents=True)
    (store / "caddy" / "Caddyfile").write_text(":443\n")
    (store / "audit").mkdir()
    host.mkdir()
    (host / "machine-id").write_text("0\n")
    config = {"name": "docker", "services": {
        "app": {"container_name": "app", "volumes": [
            _bind(repo / "app", "/etc/app"), {"type": "volume", "source": "app-data", "target": "/var/lib/app"},
            _bind(store / "audit", "/audit"), _bind(host / "machine-id", "/etc/machine-id")]},
        "rules": {"container_name": "rules", "volumes": [_bind(repo / "rules.yml", "/r.yml")]},
        "writer": {"container_name": "writer", "volumes": [_bind(repo / "app", "/etc/app", ro=False)]},
        "plain": {"container_name": "plain"},
        "other": {"volumes": [_bind(repo / "app" / "conf.d", "/etc/other")]},
        "caddy": {"container_name": "caddy", "volumes": [_bind(store / "caddy" / "Caddyfile", "/etc/caddy/Caddyfile")]},
        "graf": {"container_name": "graf", "labels": {"kontroll.config.live": "/var/lib/boards/"}, "volumes": [
            _bind(repo / "rules.yml", "/etc/prov.yml"), _bind(repo / "boards", "/var/lib/boards"),
            _bind(repo / "subboards", "/var/lib/boards/sub")]},
    }}
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))
    return repo, store, cfg


def _run(tmp_path, *args):
    r = subprocess.run([sys.executable, "-B", _SCRIPT, "--config-json", str(tmp_path / "config.json"),
                        "--stack-dir", str(tmp_path / "repo" / "docker"), "--storage-root", str(tmp_path / "store"),
                        "--stamp", str(tmp_path / "stamp.json")] + list(args),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


_ALL = ["app", "rules", "writer", "plain", "other", "caddy", "graf"]
_WITH_CONFIG = ["app", "rules", "other", "caddy", "graf"]


def test_only_read_only_config_binds_count(tmp_path):
    """A service's config is each read-only bind from the repo tree, file or directory, and each read-only FILE
    from the storage root (what the deploy puts there: the Caddyfile, Vector's key and CA bundle, the GUI's age
    key). Not config:
    a read-write bind or a named volume (digesting them would restart a service for its own writes), a directory
    under the storage root (a store: audit logs, TLS), a host path, and a bind whose target the service names in
    its `kontroll.config.live` label (one it reloads on its own, where a restart only interrupts it), or below
    one, the label written with or without a trailing slash."""
    repo, store, cfg = _tree(tmp_path)
    found = _mod().services(json.loads(cfg.read_text()), str(repo), str(store))
    assert sorted(found) == sorted(_WITH_CONFIG), found
    assert found["app"] == [("tree", str(repo / "app"))]
    assert found["caddy"] == [("store", str(store / "caddy" / "Caddyfile"))]
    assert found["graf"] == [("tree", str(repo / "rules.yml"))]


def test_with_no_stamp_every_service_with_config_is_stale(tmp_path):
    """No stamp yet (a first deploy, or the first with this check): what the services loaded is not known, so each
    with a config is named, once; a service with none never is."""
    _tree(tmp_path)
    assert _run(tmp_path, *_ALL) == _WITH_CONFIG


def test_after_a_write_nothing_is_stale_and_a_second_write_changes_nothing(tmp_path):
    """`--write` records the services' digests (`written`); compared again, none is stale, and writing again
    reports `unchanged` — the second apply's 0 changed."""
    _tree(tmp_path)
    assert _run(tmp_path, "--write", *_ALL) == ["written"]
    assert _run(tmp_path, *_ALL) == []
    assert _run(tmp_path, "--write", *_ALL) == ["unchanged"]


def test_a_rewrite_with_the_same_content_is_not_a_change(tmp_path):
    """The generators rewrite every file on every run with the same bytes: the digest is the content, so a
    rewrite (a new mtime) restarts nothing."""
    repo, _store, _cfg = _tree(tmp_path)
    _run(tmp_path, "--write", *_ALL)
    f = repo / "app" / "conf.d" / "one.yml"
    f.write_text(f.read_text())
    os.utime(f, (1, 1))
    assert _run(tmp_path, *_ALL) == []


@pytest.mark.parametrize("change,stale", [
    ("edit", ["app", "other"]), ("add", ["app", "other"]), ("remove", ["app", "other"]),
    ("rename", ["app", "other"]), ("store", ["caddy"]), ("live", []), ("live-below", [])])
def test_a_changed_config_names_each_service_that_reads_it(tmp_path, change, stale):
    """An edit, a file added or removed in a config dir (a drop-in), or a rename, makes each service that binds it
    stale — `app` binds the dir and `other` the subdir the change is in — and no other; a re-rendered Caddyfile
    makes Caddy stale; a changed board, in a bind Grafana reloads itself or one below it, makes nothing stale."""
    repo, store, _cfg = _tree(tmp_path)
    _run(tmp_path, "--write", *_ALL)
    d = repo / "app" / "conf.d"
    if change == "edit":
        (d / "one.yml").write_text("b: 3\n")
    elif change == "add":
        (d / "two.yml").write_text("c: 1\n")
    elif change == "remove":
        (d / "one.yml").unlink()
    elif change == "rename":
        (d / "one.yml").rename(d / "uno.yml")
    elif change == "store":
        (store / "caddy" / "Caddyfile").write_text(":8443\n")
    elif change == "live":
        (repo / "boards" / "b.json").write_text('{"v": 2}\n')
    else:
        (repo / "subboards" / "s.json").write_text('{"v": 2}\n')
    assert _run(tmp_path, *_ALL) == stale


def _add_service(cfg, name, *volumes):
    config = json.loads(cfg.read_text())
    config["services"][name] = {"container_name": name, "volumes": list(volumes)}
    cfg.write_text(json.dumps(config))


def test_a_file_moved_between_two_sources_of_one_service_is_a_change(tmp_path):
    """Each source is named in the digest, so a file that moves from one of a service's binds to another — the
    same bytes, a different path in the container — is a change, not a no-op. The file is each source's only
    entry, so the source's name is all that tells the two states apart."""
    repo, _store, cfg = _tree(tmp_path)
    (repo / "first").mkdir()
    (repo / "second").mkdir()
    (repo / "first" / "x.yml").write_text("x: 1\n")
    _add_service(cfg, "pair", _bind(repo / "first", "/etc/first"), _bind(repo / "second", "/etc/second"))
    _run(tmp_path, "--write", "pair")
    (repo / "first" / "x.yml").rename(repo / "second" / "x.yml")
    assert _run(tmp_path, "pair") == ["pair"]


def test_a_source_missing_when_recorded_is_a_change_once_it_exists(tmp_path):
    """A bind source that was missing when recorded and exists now, even as an empty dir, is a different bind:
    it is named, not taken for the empty one it would hash the same as."""
    repo, _store, cfg = _tree(tmp_path)
    _add_service(cfg, "late", _bind(repo / "late", "/etc/late"))
    _run(tmp_path, "--write", "late")
    assert _run(tmp_path, "late") == []
    (repo / "late").mkdir()
    assert _run(tmp_path, "late") == ["late"]


@pytest.mark.skipif(os.name != "posix", reason="links as a POSIX tree holds them")
def test_a_source_that_is_a_link_is_followed_to_its_content(tmp_path):
    """A bind's own source is followed, as Docker follows it when it binds it: a file bound through a link is
    digested by the content behind the link, so a change there is a change, though the link's text is not."""
    repo, _store, cfg = _tree(tmp_path)
    (repo / "real.yml").write_text("v: 1\n")
    os.symlink("real.yml", repo / "link.yml")
    _add_service(cfg, "linked", _bind(repo / "link.yml", "/etc/linked.yml"))
    _run(tmp_path, "--write", "linked")
    (repo / "real.yml").write_text("v: 2\n")
    assert _run(tmp_path, "linked") == ["linked"]


@pytest.mark.parametrize("store_rel,tree_rel", [("repo/local/store", "repo"), ("store", "store/checkout")])
def test_a_root_inside_the_other_classifies_by_the_deeper_one(tmp_path, store_rel, tree_rel):
    """A storage root inside the checkout holds stores, not tree config: its dirs (audit logs, run logs, TLS) grow
    on every run, and taken for config they would restart their service on every deploy and break the second
    apply's 0 changed; a file there is still the deploy's (store). The reverse, a checkout under the storage root,
    holds tree config, dirs included."""
    store, tree = tmp_path / store_rel, tmp_path / tree_rel
    (store / "audit").mkdir(parents=True)
    (store / "caddy").mkdir()
    (store / "caddy" / "Caddyfile").write_text(":443\n")
    (tree / "conf").mkdir(parents=True, exist_ok=True)
    config = {"services": {"svc": {"volumes": [_bind(store / "audit", "/audit"),
                                               _bind(store / "caddy" / "Caddyfile", "/etc/Caddyfile"),
                                               _bind(tree / "conf", "/etc/conf")]}}}
    found = _mod().services(config, str(tree), str(store))
    assert sorted(found["svc"]) == [("store", str(store / "caddy" / "Caddyfile")), ("tree", str(tree / "conf"))], found


@pytest.mark.skipif(os.name != "posix", reason="links and FIFOs as a POSIX tree holds them")
def test_a_link_or_a_fifo_in_a_config_dir_is_recorded_not_followed(tmp_path):
    """An entry that is not a regular file is recorded by its type (a link by its target), never opened: a FIFO
    would block the deploy, and a link resolves in the container's tree, not the host's. Changing where a link
    points is a change."""
    repo, _store, _cfg = _tree(tmp_path)
    d = repo / "app" / "conf.d"
    os.symlink("one.yml", d / "link.yml")
    os.mkfifo(d / "pipe")
    _run(tmp_path, "--write", *_ALL)
    assert _run(tmp_path, *_ALL) == []
    (d / "link.yml").unlink()
    os.symlink("other.yml", d / "link.yml")
    assert _run(tmp_path, *_ALL) == ["app", "other"]


def test_a_service_missing_from_the_stamp_is_stale(tmp_path):
    """A service added to the stack after the stamp was written: not known to have loaded anything, so named."""
    _tree(tmp_path)
    _run(tmp_path, "--write", "app", "other", "caddy", "graf")
    assert _run(tmp_path, *_ALL) == ["rules"]


def test_a_subset_write_keeps_the_other_services_records(tmp_path):
    """A deploy of some services (the GUI's secret-rotation hint deploys one) records only those and keeps the
    others' records: replaced whole, the stamp would make the next full deploy restart every config service, and
    report a change, for nothing."""
    _tree(tmp_path)
    _run(tmp_path, "--write", *_ALL)
    assert _run(tmp_path, "--write", "rules") == ["unchanged"]
    assert _run(tmp_path, "--write", "plain") == ["unchanged"]
    assert _run(tmp_path, *_ALL) == []


@pytest.mark.parametrize("write", [False, True])
def test_a_service_the_stack_does_not_hold_fails_the_run(tmp_path, write):
    """A service the deploy names that `docker compose config` does not resolve (one behind a compose profile:
    `up` starts it by name, `config` leaves it out) fails both the find and the record, naming it: its config was
    never looked at, and an empty answer would read as "unchanged". The stamp is not touched."""
    _tree(tmp_path)
    r = subprocess.run([sys.executable, "-B", _SCRIPT, "--config-json", str(tmp_path / "config.json"),
                        "--stack-dir", str(tmp_path / "repo" / "docker"), "--storage-root", str(tmp_path / "store"),
                        "--stamp", str(tmp_path / "stamp.json")] + (["--write"] if write else []) + ["app", "ghost"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and r.stdout == "" and "ghost" in r.stderr and "app" not in r.stderr, r
    assert not (tmp_path / "stamp.json").exists()


def test_a_container_up_started_is_recorded_and_not_named(tmp_path):
    """A container `up` started in this run (created, recreated, or started from stopped) loaded the config as
    it is: it is not named for a restart — on a fresh node that would restart every config service seconds after
    its first start — but `--write` still records it. A service with no container_name is matched by compose's
    default name, <project>-<service>-<n>."""
    _tree(tmp_path)
    assert _run(tmp_path, "--started", "caddy,docker-other-1", *_ALL) == ["app", "rules", "graf"]
    _run(tmp_path, "--write", *_ALL)
    assert _run(tmp_path, *_ALL) == []


def test_the_output_names_services_and_never_a_digest(tmp_path):
    """The rendered snmp.yml holds the SNMP credentials, and a digest of a file whose other bytes are known tests
    guesses at them: the script prints names (or `written`/`unchanged`), and the deploy logs what it prints."""
    _tree(tmp_path)
    out = _run(tmp_path, *_ALL) + _run(tmp_path, "--write", *_ALL)
    assert all(len(w) < 40 and not all(c in "0123456789abcdef" for c in w) for w in out), out


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX's")
def test_the_stamp_is_written_0600(tmp_path):
    """The stamp holds those digests, so only its owner (root, on a box) may read it."""
    _tree(tmp_path)
    _run(tmp_path, "--write", *_ALL)
    assert (os.stat(tmp_path / "stamp.json").st_mode & 0o777) == 0o600
    assert sorted(json.loads((tmp_path / "stamp.json").read_text())) == sorted(_WITH_CONFIG)


def _compose_works():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _compose_works(), reason="needs the docker compose CLI (the gate image has it)")
def test_the_shipped_stack_covers_each_config_and_leaves_out_the_live_ones(tmp_path, monkeypatch):
    """Resolved by compose from the shipped fragments, with every file and dir the deploy puts under the storage
    root present, the config services are exactly these: Vector its tree and the key, CA bundle and known_hosts the
    deploy renders; Caddy its rendered Caddyfile; the GUI its age key (a rotated key is re-installed, a new inode
    the running GUI would not see); Grafana its provisioning; Loki and Prometheus their config files; the exporters
    theirs — the rendered snmp.yml among them, which this check restarts the exporter for when it changes. Left out:
    the binds those services reload on their own (Grafana's boards, Loki's overrides, Prometheus's targets), the TLS
    dirs (stores), and Semaphore and the api, which load none. Exact, so a new read-only bind under either root (a
    TLS key bound as a file, say) is a reviewed decision here, not a silent new restart."""
    for rel in ("caddy/Caddyfile", "vector/file_tail_ssh.key", "vector/device-ca-bundle.pem",
                "vector/file_tail_known_hosts", "onboard-gui/age.key"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n")
    for rel in ("api/tls", "onboard-gui/tls"):
        (tmp_path / rel).mkdir(parents=True)
    monkeypatch.setenv("KONTROLL_STORAGE_ROOT", str(tmp_path))
    mod = _mod()
    stack = os.path.join(_ROOT, "docker")
    found = mod.services(mod.compose_config(stack, os.path.join(stack, ".env.example")), _ROOT, str(tmp_path))
    rel = {s: sorted("%s:%s" % (k, os.path.relpath(p, _ROOT if k == "tree" else str(tmp_path)).replace(os.sep, "/"))
                     for k, p in v) for s, v in found.items()}
    assert rel == {
        "blackbox-exporter": ["tree:prometheus/exporters/blackbox/blackbox.yml"],
        "caddy": ["store:caddy/Caddyfile"],
        "grafana": ["tree:dashboards/grafana/provisioning"],
        "loki": ["tree:docker/loki/loki-config.yml"],
        "onboard-gui": ["store:onboard-gui/age.key"],
        "prometheus": ["tree:prometheus/jobs.d", "tree:prometheus/prometheus.yml"],
        "snmp-exporter": ["tree:prometheus/exporters/snmp/snmp.yml"],
        "vector": ["store:vector/device-ca-bundle.pem", "store:vector/file_tail_known_hosts",
                   "store:vector/file_tail_ssh.key", "tree:docker/vector"],
    }, rel


def _tasks():
    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            yield node
            for key in ("block", "rescue", "always", "tasks"):
                if key in node:
                    yield from walk(node[key])
    with open(_DEPLOY, encoding="utf-8") as fh:
        return list(walk(yaml.safe_load(fh)))


def _named(tasks):
    names = [str(t.get("name", "")) for t in tasks]
    at = {k: names.index(n) for k, n in [
        ("up", next(n for n in names if n.startswith("Bring up the selected stack"))),
        ("find", "Find the services whose read-only config changed since they last loaded it"),
        ("restart", "Restart each service whose read-only config changed since it last loaded it"),
        ("record", "Record the config each running service loaded")]}
    return at, names


def test_the_deploy_finds_restarts_and_records_in_that_order_after_up():
    """After `up`: find the stale services (run in check mode too, so a dry-run names them, and never failing
    one), restart them, then record — after the restart, so a failed restart leaves them stale for the next run;
    only when this run found them, so a run started past the find records nothing it did not restart; and never in
    a dry-run, where the restart is skipped: a record with no restart behind it is the defect itself. Both script
    runs use the playbook's own Python (the play is local, so the interpreter running it is one known to be there)
    over one stamp under the storage root."""
    tasks = _tasks()
    at, names = _named(tasks)
    assert at["up"] < at["find"] < at["restart"] < at["record"], at
    find, restart, record = tasks[at["find"]], tasks[at["restart"]], tasks[at["record"]]
    for t in (find, record):
        cmd = " ".join(t["ansible.builtin.command"]["cmd"].split())
        assert cmd.startswith("{{ ansible_playbook_python }} -B {{ playbook_dir }}/../../scripts/stale-config.py")
        for part in ("--stamp {{ kontroll_storage_root }}/config-loaded.json", "--stack-dir {{ stack_dir }}",
                     "--storage-root {{ kontroll_storage_root }}"):
            assert part in cmd, (part, cmd)
        assert cmd.endswith("{{ _up_services | join(' ') }}")
    assert find.get("check_mode") is False and find.get("register") == "_stale_config"
    assert find["changed_when"] == "ansible_check_mode and (_stale_config.stdout_lines | length > 0)"
    assert find["failed_when"] == "_stale_config.rc != 0 and not ansible_check_mode"
    assert "--started {{ _started | join(',') | quote }}" in " ".join(find["ansible.builtin.command"]["cmd"].split())
    assert restart["when"] == "(_stale_config.stdout_lines | default([])) | length > 0"
    rcmd = " ".join(restart["ansible.builtin.command"]["cmd"].split())
    assert "--env-file {{ stack_dir }}/.env restart {{ _stale_config.stdout_lines | join(' ') }}" in rcmd
    assert record["when"] == "_stale_config is defined" and "--write" in record["ansible.builtin.command"]["cmd"]
    assert record["changed_when"] == "_config_recorded.stdout == 'written'"
    for t in (restart, record):
        assert "check_mode" not in t, t["name"]
    assert not any(n.startswith("Restart snmp-exporter") for n in names), "the digest covers snmp.yml now"


def test_the_deploy_reads_the_containers_up_started_from_its_lines():
    """The find's `--started` list is the containers `up` printed `Started` for — created, recreated or started
    from stopped — and none it left `Running`. Applied here with Python's `re`, which Ansible's `search` and
    `regex_findall` filters are, to the lines `--progress plain` prints. (It takes no back-reference: a Jinja
    string literal decodes a backslash escape, so one written plainly would arrive as a control character.)"""
    find = _tasks()[_named(_tasks())[0]["find"]]
    select = re.search(r"select\('search', '([^']+)'\)", find["vars"]["_started"]).group(1)
    pattern = re.search(r"regex_findall\('([^']+)'\)", find["vars"]["_started"]).group(1)
    lines = [" Container vector  Recreate", " Container vector  Recreated", " Container vector  Starting",
             " Container vector  Started", " Container loki  Running", " Network docker_kontroll  Created",
             "Container caddy  Started",
             # compose v5.3.1, measured: one space, a trailing space, on stderr (the task reads both streams)
             " Container grafana Recreated ", " Container grafana Started ", " Container loki Running "]
    found = re.findall(pattern, " ".join(ln for ln in lines if re.search(select, ln)))
    assert found == ["vector", "caddy", "grafana"]
