"""docker/semaphore-runner/no_log_censor.py run on a copy of this run's own ansible-core, and ansible-playbook run on
that copy before and after it (I-75).

WHY. On ansible-core 2.19 and later a `no_log: true` task that fails prints its error message, `[ERROR]: Task failed:
...`, with what it was given in it: a `fail` or `assert` message, a command line, a path (CVE-2026-123036, upstream
ansible/ansible#85696; no fixed release; 2.18 keeps no `exception`). The runner image's base ships such a core, so
every Semaphore job's kept output could carry a secret a failing task held. The image build runs the script, which
drops `exception` from the censor's preserved keys and then checks the core; no pull-request job builds the image, so
these rows run the same script on a copy of the ansible package this Python's subprocesses import (each validate
image's core, and CI's validate job's, 2.19.4), put first on PYTHONPATH, and ansible-playbook on that copy: the copy
prints the marker before (where the core is one that does), and after it prints it nowhere, in the output or the
ansible log, under a block's rescue and `ignore_errors` too, while an ordinary failure still prints its message. They
also run the script on copies it must refuse or fail: a censor of another shape (nothing written), a core that prints
the value by another path (the check fails the build), a stale bytecode file built unchecked (removed, so the file as
rewritten is what runs), and, as root, a copy another user owns (the rewrite and its cache stay that user's).

It needs ansible-core importable by this Python's subprocesses, found as they find it (`python -I`, so neither the
working directory nor PYTHONPATH counts): with the repo root on sys.path, the repo's own `ansible/` (playbooks, no
`__init__.py`) would pass for the package, and the rows would copy it, fail, or patch the installed core. Where it is
not importable (CI's pytest legs, a host without it), every row is skipped; CI runs them in its validate job. Each run
on a copy checks that the module it imported is the copy's. The marker is only in a `-e @` vars file, never in a
playbook's text, so a hit is the value printed, not 2.19's quote of a task's source lines. Every task runs on
localhost with `connection: local`.
"""
import importlib.util
import os
import py_compile
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "docker", "semaphore-runner", "no_log_censor.py")
# the package as the rows' subprocesses import it (`-I`: no working directory, no PYTHONPATH), not as this process
# sees it, where the repo root on sys.path offers the repo's ansible/ as a namespace package
_FOUND = subprocess.run([sys.executable, "-I", "-c", "import os, ansible.executor.task_result, ansible; "
                         "print(os.path.dirname(ansible.__file__))"], capture_output=True, text=True, timeout=120)
if _FOUND.returncode != 0:
    pytest.skip("ansible-core is not importable by this Python", allow_module_level=True)
ANSIBLE = _FOUND.stdout.strip()
NOTICE = "# Modified by kontroll's docker/semaphore-runner/no_log_censor.py (I-75"
MARKER = "nolog7f3a9c2e"

TASKS = {
    "fail": "ansible.builtin.fail:\n{i}  msg: \"{{{{ secret }}}}\"",
    "assert": "ansible.builtin.assert:\n{i}  that: false\n{i}  fail_msg: \"{{{{ secret }}}}\"",
    "command argv": "ansible.builtin.command:\n{i}  argv: [\"/nonexistent/{{{{ secret }}}}\", --password, "
                    "\"{{{{ secret }}}}\"]",
    "slurp path": "ansible.builtin.slurp:\n{i}  src: \"/nonexistent/{{{{ secret }}}}\"",
}
WRAPS = {
    "plain": "    - name: The task\n      no_log: true\n      {task}\n",
    "rescue": ("    - name: A block\n      block:\n        - name: The task\n          no_log: true\n          {task}\n"
               "      rescue:\n        - name: Rescued\n          ansible.builtin.debug:\n            msg: rescued\n"),
    "ignore_errors": "    - name: The task\n      no_log: true\n      ignore_errors: true\n      {task}\n",
}
INDENT = {"plain": "      ", "rescue": "          ", "ignore_errors": "      "}
# a block's rescue and ignore_errors end the run passing; the task alone fails it
RC = {"plain": 2, "rescue": 0, "ignore_errors": 0}


def _copy(tmp_path):
    """A copy of this Python's ansible package under tmp_path/site (its cached bytecode left out); return the dir."""
    site = tmp_path / "site"
    shutil.copytree(ANSIBLE, str(site / "ansible"), ignore=shutil.ignore_patterns("__pycache__"))
    return site


def _env(site, home):
    """The environment for a run on the copy: it first on PYTHONPATH, an Ansible home and config of the run's own."""
    home.mkdir(exist_ok=True)
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\nretry_files_enabled = False\nstdout_callback = default\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_"))}
    env.update(PYTHONPATH=str(site), ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home),
               ANSIBLE_LOCAL_TEMP=str(home / "tmp"), ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0",
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0", PYTHONDONTWRITEBYTECODE="1")
    return env


def _play(site, work, task, wrap, no_log=True):
    """Run one task under one wrap with ansible-playbook from the copy; return (rc, stdout + stderr + ansible log)."""
    body = WRAPS[wrap].format(task=TASKS[task].format(i=INDENT[wrap]))
    if not no_log:
        body = body.replace("no_log: true\n", "no_log: false\n")
    work.mkdir(exist_ok=True)
    book = work / "play.yml"
    book.write_text("- name: Probe\n  hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n" + body,
                    encoding="utf-8")
    assert MARKER not in book.read_text(encoding="utf-8")
    extra = work / "vars.yml"
    extra.write_text("secret: %s\n" % MARKER, encoding="utf-8")
    log = work / "ansible.log"
    if log.exists():
        log.unlink()
    env = _env(site, work / "home")
    env["ANSIBLE_LOG_PATH"] = str(log)
    p = subprocess.run([sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,", "-e", "@%s" % extra,
                        str(book)], cwd=str(work), env=env, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr + (log.read_text(encoding="utf-8", errors="replace") if log.exists()
                                                 else "")


def _script(site, home):
    """Run the script with the copy first on PYTHONPATH; return (rc, stdout, stderr)."""
    p = subprocess.run([sys.executable, SCRIPT], env=_env(site, home), capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout, p.stderr


def _keeps_exception(site, home):
    """Whether the copy's censor keeps `exception` (2.19 and later as shipped; not 2.18). Fails unless the module
    imported is the copy's: a run on another package would patch it, and pass or fail for its reasons."""
    p = subprocess.run([sys.executable, "-c", "import ansible.executor.task_result as t; print(t.__file__); "
                        "print('exception' in t._PRESERVE)"], env=_env(site, home), capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0, p.stderr
    where, keeps = p.stdout.strip().splitlines()
    assert os.path.realpath(where).startswith(os.path.realpath(str(site)) + os.sep), (where, site)
    return keeps == "True"


@pytest.fixture(scope="module")
def censored(tmp_path_factory):
    """One copy, every case run on it before the script and after it: {'before': {(task, wrap): (rc, out)},
    'script': (rc, out, err), 'after': {...}, 'leaky': whether the core as shipped keeps `exception`}."""
    tmp = tmp_path_factory.mktemp("censored")
    site = _copy(tmp)
    leaky = _keeps_exception(site, tmp / "h0")
    before = {(t, w): _play(site, tmp / "before", t, w) for t in TASKS for w in WRAPS}
    script = _script(site, tmp / "h1")
    after = {(t, w): _play(site, tmp / "after", t, w) for t in TASKS for w in WRAPS}
    plain = _play(site, tmp / "plain", "fail", "plain", no_log=False)
    return {"before": before, "script": script, "after": after, "leaky": leaky, "plain": plain, "site": site,
            "tmp": tmp}


@pytest.mark.parametrize("wrap", sorted(WRAPS))
@pytest.mark.parametrize("task", sorted(TASKS))
def test_a_failing_no_log_task_prints_nothing_it_was_given_after_the_script(censored, task, wrap):
    """Each kind of failure 2.19 prints (an action's message, a module's message naming a command or a path), alone,
    under a block's rescue and with `ignore_errors`: after the script, the run ends as it did (rc 2 alone, 0 rescued or
    ignored), the task's `FAILED!` line is there and says its output is hidden, and the marker is in no line of the
    output or the ansible log. Before it, on a core that keeps `exception` (2.19, 2.20), the same run printed the
    marker, so the row sees what it guards; on 2.18 nothing printed it before either. Guards the script's rewrite (a
    key left in, the wrong key dropped), and the image's failing no_log tasks printing their message into Semaphore's
    output and the ansible log."""
    rc, out = censored["after"][(task, wrap)]
    assert censored["script"][0] == 0, censored["script"]
    assert rc == RC[wrap], out
    assert "FAILED!" in out and "censored" in out, out
    assert MARKER not in out, out
    rc0, out0 = censored["before"][(task, wrap)]
    assert rc0 == RC[wrap], out0
    assert (MARKER in out0) == censored["leaky"], out0


def test_the_script_says_what_it_did_and_an_ordinary_failure_still_prints(censored):
    """The script exits 0, says it dropped 'exception' (a core that keeps it) or that there was nothing to drop (2.18),
    and that a failing no_log task's error message prints nothing it was given; the file it rewrote carries one line
    of notice that it was modified (none where nothing was rewritten); run again it has nothing to drop and leaves the
    file's bytes as they are. The same `fail` without no_log, after it, still prints its message. Guards a script
    that rewrites a file each run, ships a modified GPL-3.0 file with no notice, and a rewrite that hid every
    failure's message (the operator then reads nothing on any failed task)."""
    rc, out, err = censored["script"]
    assert rc == 0, (out, err)
    said = "dropped 'exception' from _PRESERVE in" if censored["leaky"] else "nothing to drop in"
    assert said in out and "a failing no_log task's error message prints nothing it was given" in out, out
    target = censored["site"] / "ansible" / "executor" / "task_result.py"
    first = target.read_bytes()
    assert first.decode("utf-8").count(NOTICE) == (1 if censored["leaky"] else 0)
    rc, out, err = _script(censored["site"], censored["tmp"] / "h2")
    assert rc == 0 and "nothing to drop in" in out, (out, err)
    assert target.read_bytes() == first
    rc, out = censored["plain"]
    assert rc == 2 and MARKER in out, out


def test_a_censor_of_another_shape_is_refused_and_left_unwritten(tmp_path):
    """A copy whose preserved keys are not the one literal line the script knows (here a frozenset call): the script
    exits 1, says REFUSED and that nothing was written, and the file's bytes are unchanged. Guards a script that
    guesses at a moved censor: the build passing with the core unpatched, or patched wrong."""
    site = _copy(tmp_path)
    target = site / "ansible" / "executor" / "task_result.py"
    text = target.read_text(encoding="utf-8")
    line = next(ln for ln in text.split("\n") if ln.startswith("_PRESERVE = "))
    target.write_text(text.replace(line, "_PRESERVE = frozenset(%s)" % line[len("_PRESERVE = "):]), encoding="utf-8")
    before = target.read_bytes()
    rc, out, err = _script(site, tmp_path / "h")
    assert rc == 1 and "REFUSED" in err and "nothing written" in err, (out, err)
    assert target.read_bytes() == before


def test_a_core_that_prints_the_value_by_another_path_fails_the_check(tmp_path):
    """A copy whose censor also keeps the result's `msg` (a stand-in for a core that prints a failing no_log task's
    message by some other path than `exception`): the script's rewrite runs, and its check then fails, exit 1, naming
    the no_log `fail` case whose marker printed. Guards a check that reads the file and not the run: the build must
    stop on any core that still prints the value, whatever prints it."""
    site = _copy(tmp_path)
    target = site / "ansible" / "executor" / "task_result.py"
    text = target.read_text(encoding="utf-8")
    # where each core builds its censored copy: 2.19 and later's censor_result, 2.18's clean_copy
    hooks = [("    censored_result.update(censored=", "    censored_result['msg'] = result.get('msg')\n"),
             ("            result._result = x\n", "            x['msg'] = self._result.get('msg')\n")]
    found = [(hook, add) for hook, add in hooks if text.count(hook) == 1]
    assert len(found) == 1, "the copy's censor is not a shape this row edits"
    hook, add = found[0]
    target.write_text(text.replace(hook, add + hook), encoding="utf-8")
    rc, out, err = _script(site, tmp_path / "h")
    assert rc == 1, (out, err)
    assert "CHECK FAILED: no_log fail: the marker it was given was printed" in err, err


def test_a_stale_unchecked_bytecode_file_is_not_what_runs(tmp_path):
    """A copy whose task_result module has cached bytecode files built from the file as shipped with `unchecked-hash`
    (Python then never compares them with the source), the plain one and the `-O` one a run under PYTHONOPTIMIZE
    reads: the script replaces the plain one with one compiled from the file as it rewrote it, so its check passes,
    and removes the `-O` one, which it does not compile; left, either would run the old censor and print the marker.
    Only a core that keeps `exception` has a rewrite for a cache to hide: on 2.18 the row is skipped. Guards a rewrite
    of the source that the image's runs never read."""
    site = _copy(tmp_path)
    if not _keeps_exception(site, tmp_path / "h0"):
        pytest.skip("this core keeps no `exception`: the script rewrites nothing, so no cache can hide a rewrite")
    target = site / "ansible" / "executor" / "task_result.py"
    cache = importlib.util.cache_from_source(str(target))
    optimized = importlib.util.cache_from_source(str(target), optimization=1)
    for cfile, level in ((cache, -1), (optimized, 1)):
        py_compile.compile(str(target), cfile=cfile, doraise=True, optimize=level,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    stale = open(cache, "rb").read()
    rc, out, err = _script(site, tmp_path / "h")
    assert rc == 0, (out, err)
    assert open(cache, "rb").read() != stale
    assert not os.path.exists(optimized), "the -O cache built from the file as shipped is still there"
    rc, out = _play(site, tmp_path / "w", "fail", "plain")
    assert rc == 2 and MARKER not in out, out


def test_as_root_the_rewritten_module_keeps_its_owner_and_the_lookup_leaves_no_cache(tmp_path):
    """Run as root, as the image's build runs it, on a copy uid 1001 owns (as the runner base's venv is), with
    bytecode writing allowed: the script exits 0; task_result.py, its compiled bytecode and the `__pycache__` the
    script made are 1001's; and no other `__pycache__` appears in the package, though finding the module imports
    ansible's packages. Guards the step leaving root-owned files in a venv uid 1001 owns: a module uid 1001 cannot
    cache, compiled again in every run, and a file whose owner changed for no reason. Skipped where the test run is
    not root (it cannot give a file away)."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("only root can run the script on a copy another user owns")
    site = _copy(tmp_path)
    for dirpath, dirnames, names in os.walk(str(site)):
        for name in [dirpath] + [os.path.join(dirpath, n) for n in dirnames + names]:
            os.lchown(name, 1001, 1001)
    env = _env(site, tmp_path / "h")
    env.pop("PYTHONDONTWRITEBYTECODE")
    p = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, (p.stdout, p.stderr)
    target = site / "ansible" / "executor" / "task_result.py"
    cache = importlib.util.cache_from_source(str(target))
    for path in (str(target), cache, os.path.dirname(cache)):
        st = os.lstat(path)
        assert (st.st_uid, st.st_gid) == (1001, 1001), (path, st.st_uid, st.st_gid)
    caches = sorted(d for d, _, _ in os.walk(str(site)) if os.path.basename(d) == "__pycache__")
    assert caches == [os.path.dirname(cache)], caches
