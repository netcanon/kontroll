"""deploy-stack.yml's choice of the host journal Vector reads, run for real by ansible-playbook (I-25).

WHY: vector.yaml bound /var/log/journal from the host. On a host whose journald is volatile that path is absent, so
the first `up` made Docker create it (root:root 0755), and journald's default `Storage=auto` reads that directory as
"persist" from its next start: a deploy changed a host setting. deploy-stack now chooses the directory Vector binds
(KONTROLL_JOURNAL_DIR): kontroll_journal_dir when the inventory sets it, else the host's own answer, a `stat` of
/var/log/journal on the host path and the launcher's KONTROLL_HOST_JOURNAL_DIR in the installer container, which cannot
see the host's /var/log; with neither, /run/log/journal, which makes nothing new, and a NOT KNOWN note. Any value but
the two directories journald writes is refused.

These rows take the four tasks from the playbook as they are (the probe, the choice, the refusal, the note), in the
playbook's order, with the play's own `in_container`, and run them with ansible-playbook. The probe's path is the one
change: a path under tmp_path that each row makes a directory, a symlink to one, a file or nothing, so no row reads or
touches this host's /var/log. Each row runs as a dry-run (the mandated `--check --diff`), and the first two also as an
apply. Needs POSIX and ansible-playbook; without them every row is skipped.
"""
import json
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "ansible", "playbooks", "deploy-stack.yml")
PERSISTENT, VOLATILE = "/var/log/journal", "/run/log/journal"
TASKS = ("Look for the host's persistent journal", "Choose the host journal directory Vector reads",
         "Refuse a journal directory journald does not write", "Say which host journal Vector reads")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix" or not shutil.which("ansible-playbook"),
                       reason="needs POSIX and ansible-playbook (it runs deploy-stack.yml's own tasks)"),
]


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _picked():
    """The play's `in_container` expression and its four journal tasks, in the play's order."""
    with open(DEPLOY, encoding="utf-8") as fh:
        plays = yaml.safe_load(fh.read())
    hits = [(play, task) for play in plays for task in _tasks(play.get("tasks", []))
            if any(frag in str(task.get("name", "")) for frag in TASKS)]
    assert [next(f for f in TASKS if f in t["name"]) for _, t in hits] == list(TASKS), \
        "a journal task moved or was renamed: %r" % [t.get("name") for _, t in hits]
    assert len({id(p) for p, _ in hits}) == 1, "the journal tasks are in one play"
    return hits[0][0]["vars"]["in_container"], [dict(t) for _, t in hits]


def _run(tmp_path, host, check=True, container=False, launcher=None, given=None, services=("vector",)):
    """Run the four tasks with the probe pointed at a stand-in for the host's /var/log/journal (`host`: "dir",
    "link" to a dir, "file" or None); return (rc, output, the recorded facts or None when the play stopped)."""
    in_container, tasks = _picked()
    probe = tasks[0]["ansible.builtin.stat"]
    assert probe["path"] == PERSISTENT and probe.get("follow") is True, probe
    stand_in = tmp_path / "var-log-journal"
    if host == "dir":
        stand_in.mkdir()
    elif host == "link":
        (tmp_path / "elsewhere").mkdir()
        stand_in.symlink_to(tmp_path / "elsewhere")
    elif host == "file":
        stand_in.write_text("", encoding="utf-8")
    tasks[0] = dict(tasks[0], **{"ansible.builtin.stat": dict(probe, path=str(stand_in))})
    result = tmp_path / "result.json"
    record = ("{{ {'dir': _journal_dir | default(none), 'how': _journal_how | default(none), "
              "'probed': not (_journal_persistent.skipped | default(false))} | to_json }}")
    play_vars = {"in_container": in_container, "stack_services": list(services)}
    if given is not None:
        play_vars["kontroll_journal_dir"] = given
    play = [{"name": "The journal tasks, as deploy-stack.yml runs them", "hosts": "localhost",
             "gather_facts": False, "vars": play_vars,
             "tasks": tasks + [{"name": "Write down the results", "check_mode": False,
                                "ansible.builtin.copy": {"content": record, "dest": str(result), "mode": "0600"}}]}]
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
    if container:
        env["KONTROLL_IN_CONTAINER"] = "1"
    if launcher is not None:
        env["KONTROLL_HOST_JOURNAL_DIR"] = launcher
    argv = ["ansible-playbook", "-i", str(tmp_path / "hosts.yml"), str(tmp_path / "play.yml")]
    if check:
        argv += ["--check", "--diff"]
    p = subprocess.run(argv, cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    return p.returncode, out, (json.loads(result.read_text(encoding="utf-8")) if result.exists() else None)


# (id, the stand-in host path, in the container, the launcher's value, kontroll_journal_dir, the directory, its source,
#  whether the probe runs, the modes)
ROWS = [
    ("host-persistent", "dir", False, None, None, PERSISTENT, "a stat of /var/log/journal", True, (True, False)),
    ("host-volatile", None, False, None, None, VOLATILE, "a stat of /var/log/journal", True, (True, False)),
    ("host-link-to-a-dir", "link", False, None, None, PERSISTENT, "a stat of /var/log/journal", True, (True,)),
    ("host-a-file-there", "file", False, None, None, VOLATILE, "a stat of /var/log/journal", True, (True,)),
    ("host-launcher-ignored", None, False, PERSISTENT, None, VOLATILE, "a stat of /var/log/journal", True, (True,)),
    ("container-persistent", None, True, PERSISTENT, None, PERSISTENT, "the launcher", False, (True,)),
    ("container-volatile", "dir", True, VOLATILE, None, VOLATILE, "the launcher", False, (True,)),
    ("container-no-launcher", "dir", True, None, None, VOLATILE, "NOT KNOWN", False, (True, False)),
    ("given-over-the-stat", "dir", False, None, VOLATILE, VOLATILE, "kontroll_journal_dir", True, (True,)),
    ("given-over-the-launcher", None, True, PERSISTENT, VOLATILE, VOLATILE, "kontroll_journal_dir", False, (True,)),
]


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_vector_reads_the_journal_the_host_has(tmp_path, row):
    """Each row's directory and its source, and that the probe runs on the host path only; the note says NOT KNOWN,
    and binds the volatile journal, exactly when the installer container ran without a launcher and no
    kontroll_journal_dir; otherwise it names the directory and its source. Guards I-25 (a /var/log/journal bound where
    the host lacks it, which Docker then made and journald then persisted into) and the ways the choice can go wrong:
    a stat in the container (which reads the container's own /var/log), a launcher's value read on the host path, an
    override that loses, a symlink read as no journal, a plain file read as one, a guess of /var/log/journal when
    nothing is known."""
    name, host, container, launcher, given, want, how, probed, modes = row
    for check in modes:
        sub = tmp_path / ("check" if check else "apply")
        sub.mkdir()
        rc, out, got = _run(sub, host, check=check, container=container, launcher=launcher, given=given)
        assert rc == 0, out[-3000:]
        assert got == {"dir": want, "how": how, "probed": probed}, (name, check, got)
        if how == "NOT KNOWN":
            assert "NOT KNOWN — the host journal directory" in out and "Vector reads the host journal at" not in out
        else:
            assert "Vector reads the host journal at %s (from %s)." % (want, how) in out and "NOT KNOWN" not in out
        if host == "dir":
            assert (sub / "var-log-journal").is_dir() and not any((sub / "var-log-journal").iterdir())
        elif host is None:
            assert not (sub / "var-log-journal").exists(), "the probe made the path it looks for"


@pytest.mark.parametrize("given,launcher", [("/srv/journal", None), (None, "/var/log/journal/../../../srv"),
                                            ("/var/log/journal/", None), (None, "/run/log/journal\nX=1")],
                         ids=["given-elsewhere", "launcher-climbs-out", "given-trailing-slash", "launcher-newline"])
def test_a_journal_directory_journald_does_not_write_is_refused(tmp_path, given, launcher):
    """kontroll_journal_dir, or in the installer container the launcher's value, naming anything but /var/log/journal
    or /run/log/journal stops the play at the refusal, in a dry-run, before the note and before .env is rendered, and
    says which source gave what. Guards a bind source the playbook never chose reaching docker/.env and Vector's
    compose file: a directory journald does not write (Vector reads nothing), or a value that carries a second .env
    line."""
    rc, out, got = _run(tmp_path, None, container=launcher is not None, launcher=launcher, given=given)
    assert rc != 0 and got is None, out[-3000:]
    source = "kontroll_journal_dir" if given is not None else "the launcher"
    assert "The host journal directory from %s is" % source in out, out[-3000:]
    assert "Vector reads the host journal at" not in out and "NOT KNOWN —" not in out


def test_a_stack_without_vector_chooses_nothing(tmp_path):
    """With no `vector` in stack_services the four tasks are skipped: no probe, no choice, no note, so docker/.env
    renders KONTROLL_JOURNAL_DIR empty and compose's volatile default stands. Guards the choice running, and failing
    on a bad value, for a stack that binds no journal."""
    rc, out, got = _run(tmp_path, "dir", services=("prometheus",), given="/srv/journal")
    assert rc == 0, out[-3000:]
    assert got == {"dir": None, "how": None, "probed": False}, got
    assert "Vector reads the host journal" not in out and "NOT KNOWN" not in out
