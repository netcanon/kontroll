"""Make a failing `no_log: true` task's error message print nothing it was given, in the ansible-core this Python
imports (I-75).

WHY. ansible-core 2.19 and later censor a `no_log: true` result down to a few preserved keys, and `exception` is one of
them; the default callback then prints that exception's message for a no_log task that fails, `[ERROR]: Task failed:
...`: a `fail` or `assert` message, a command line, a path, whatever the module or action put in it (CVE-2026-123036,
upstream ansible/ansible#85696; no fixed release). The runner image's base ships such a core, and every Semaphore job,
and the API's and the onboard GUI's Ansible runs, use it; their output is kept (Semaphore's database, the GUI).

With `exception` dropped from the preserved keys, a censored result holds none for the callback to print: a failing
no_log task prints only its `fatal: ... censored` line, as on 2.18. The `[ERROR]: Task failed:` block goes whole, its
reason, Origin and source lines with the value. The task still fails as before: the run's rc, the recap and a rescue's
`ansible_failed_result` (the run's result is not the censored copy) are unchanged. What this does not close, on any
core: a module's warnings and deprecations (the censor keeps them), an include's or import's templated file or role
name (with the source lines around it), and connection lines at -vvv (SECURITY.md's accepted risks).

What it changes, and what it refuses. The file is `ansible/executor/task_result.py`, found by import (with bytecode
writing off, so the lookup leaves no cache behind), so the copy a caller's PYTHONPATH puts first is the one changed.
Its preserved keys must be one `_PRESERVE = {...}` (or `(...)`) line of string literals, read with `ast`: holding
'exception' and other keys, the line is rewritten without it, the other keys in their order, under a one-line notice
that the file was modified (ansible-core is GPL-3.0); holding no 'exception', nothing is written. Any other shape (no
such line, two, one that is not a literal of strings, one holding 'exception' alone) is refused and nothing is
written: a base bump that moves the code fails the build here and is looked at. 2.21 and later keep the censor as
`PRESERVE` in `ansible/_internal/_task.py`, so a release that fixes it upstream is likely refused, not left. The
rewritten file and the module's cached bytecode keep the module's owner (the step runs as root over a venv uid 1001
owns); the cached bytecode is removed and compiled again, so no `.pyc` from before (one built unchecked, above all) is
what runs.

Then the check, run by this core in a directory of its own: a no_log task that fails with a marker in its message, and
one whose command names a path holding the marker, must print no marker, on stdout, on stderr or in the ansible log,
and must fail (rc 2, a `FAILED!` line) saying their output is hidden; the same failure without no_log must still print
the marker (a check that saw nothing printed proves nothing). Exit 0: the core passed the check; 1: refused or the
check failed (the reason on stderr). Run as root by docker/semaphore-runner/Dockerfile; held by
tests/unit/test_no_log_censor.py and tests/integration/test_no_log_censor_runs.py.
"""
import ast
import glob
import importlib.util
import os
import py_compile
import re
import secrets
import subprocess
import sys
import tempfile

MODULE = "ansible.executor.task_result"
LINE = re.compile(r"^_PRESERVE = (.*)$", re.M)
# the notice a modified GPL-3.0 file carries (section 5a); it does not start `_PRESERVE = `, so the line is still one
NOTICE = ("# Modified by kontroll's docker/semaphore-runner/no_log_censor.py (I-75, 2026-11-12): 'exception' dropped "
          "from _PRESERVE, so a failing no_log task's error message is not printed (CVE-2026-123036).")
WHERE = ("2.19 and 2.20 keep the censor as `_PRESERVE` in ansible/executor/task_result.py, 2.21 and later as "
         "`PRESERVE` in ansible/_internal/_task.py")


class Refused(Exception):
    """The file is not in a shape this script knows; nothing was written."""


def drop_exception(source):
    """Return (the source without 'exception' in its `_PRESERVE` line, the notice above it, whether it was there).
    Raise Refused when the source has no such line, more than one, one that is not a set or tuple literal of strings,
    or one that holds 'exception' alone."""
    found = LINE.findall(source)
    if len(found) != 1:
        raise Refused("expected one `_PRESERVE = ...` line, found %d (%s)" % (len(found), WHERE))
    try:
        node = ast.parse(found[0], mode="eval").body
    except SyntaxError:
        raise Refused("the `_PRESERVE` line is not one expression: %r" % found[0])
    if not isinstance(node, (ast.Set, ast.Tuple)) or not node.elts or not all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        raise Refused("the `_PRESERVE` line is not a set or tuple of strings: %r" % found[0])
    keys = [e.value for e in node.elts]
    if "exception" not in keys:
        return source, False
    kept = [k for k in keys if k != "exception"]
    if not kept:
        raise Refused("the `_PRESERVE` line holds 'exception' alone, which leaves nothing to keep: %r" % found[0])
    if isinstance(node, ast.Set):
        literal = "{%s}" % ", ".join(repr(k) for k in kept)
    else:
        literal = "(%s)" % "".join(repr(k) + ", " for k in kept).rstrip(" ")
    new = LINE.sub(lambda m: NOTICE + "\n_PRESERVE = " + literal, source, count=1)
    return new, True


def _own(path, st):
    """Give `path` the owner and group in `st`, where this process may give it away (as root); a process of another
    user owns what it writes, as the file it rewrote was its own."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(path, st.st_uid, st.st_gid)


def patch(path):
    """Rewrite `path` without 'exception' in its preserved keys, and compile it again; return whether it changed. The
    rewritten file and the compiled bytecode take the file's owner, and a `__pycache__` this made the directory's."""
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    new, dropped = drop_exception(source)
    st = os.stat(path)
    if dropped:
        tmp = path + ".no-log-censor"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        _own(tmp, st)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
    # every cached copy of this module goes, then one is compiled from the file as it now is (timestamp-checked)
    stem = os.path.splitext(os.path.basename(path))[0]
    cache_dir = os.path.join(os.path.dirname(path), "__pycache__")
    made = not os.path.isdir(cache_dir)
    for pyc in glob.glob(os.path.join(cache_dir, stem + ".*.pyc")):
        os.remove(pyc)
    pyc = py_compile.compile(path, doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    _own(pyc, st)
    if made:
        _own(cache_dir, os.stat(os.path.dirname(path)))
    return dropped


PLAY = """- name: The no-log censor check
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: A failing task
      {task}
"""
CASES = {
    # (the task, whether it is no_log)
    "no_log fail": ("no_log: true\n      ansible.builtin.fail:\n        msg: \"{{ marker }}\"", True),
    "no_log command": ("no_log: true\n      ansible.builtin.command:\n        argv: [\"/nonexistent/{{ marker }}\"]", True),
    "fail": ("ansible.builtin.fail:\n        msg: \"{{ marker }}\"", False),
}


def check():
    """Run each case with this Python's ansible-playbook; return the reasons the core is not safe (empty: safe)."""
    bad = []
    with tempfile.TemporaryDirectory(prefix="no-log-censor-") as d:
        cfg = os.path.join(d, "ansible.cfg")
        with open(cfg, "w") as fh:
            fh.write("[defaults]\nretry_files_enabled = False\nstdout_callback = default\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
        env.update(ANSIBLE_CONFIG=cfg, ANSIBLE_HOME=os.path.join(d, "home"), ANSIBLE_LOCAL_TEMP=os.path.join(d, "tmp"),
                   ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="0", PYTHONDONTWRITEBYTECODE="1")
        for name, (task, censored) in CASES.items():
            marker = "nolog" + secrets.token_hex(8)
            book = os.path.join(d, "check.yml")
            with open(book, "w") as fh:
                fh.write(PLAY.format(task=task))
            extra = os.path.join(d, "vars.yml")
            with open(extra, "w") as fh:
                fh.write("marker: %s\n" % marker)
            log = os.path.join(d, "ansible.log")
            if os.path.exists(log):
                os.remove(log)
            p = subprocess.run([sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,", "-e", "@" + extra,
                                book], cwd=d, env=dict(env, ANSIBLE_LOG_PATH=log), capture_output=True, text=True,
                               timeout=300)
            seen = p.stdout + p.stderr
            if os.path.exists(log):
                with open(log, encoding="utf-8", errors="replace") as fh:
                    seen += fh.read()
            if p.returncode != 2 or "FAILED!" not in seen:
                bad.append("%s: expected a failed run (rc 2, a FAILED! line), got rc %d" % (name, p.returncode))
            elif censored and marker in seen:
                bad.append("%s: the marker it was given was printed" % name)
            elif censored and "censored" not in seen:
                bad.append("%s: no line says its output is hidden" % name)
            elif not censored and marker not in seen:
                bad.append("%s: an ordinary failure no longer prints its message (the check sees nothing)" % name)
    return bad


def main():
    """Change the module if it needs it, then check the core; exit 1 on a refusal or a failed check."""
    # the lookup imports ansible's packages; run as root it would leave root-owned caches in a venv uid 1001 owns
    sys.dont_write_bytecode = True
    spec = importlib.util.find_spec(MODULE)
    if spec is None or not spec.origin:
        print("no-log-censor: REFUSED: %s cannot be found" % MODULE, file=sys.stderr)
        return 1
    try:
        dropped = patch(spec.origin)
    except Refused as e:
        print("no-log-censor: REFUSED %s: %s (nothing written)" % (spec.origin, e), file=sys.stderr)
        return 1
    print("no-log-censor: %s %s" % ("dropped 'exception' from _PRESERVE in" if dropped else "nothing to drop in",
                                    spec.origin), flush=True)
    bad = check()
    for reason in bad:
        print("no-log-censor: CHECK FAILED: %s" % reason, file=sys.stderr)
    if bad:
        return 1
    print("no-log-censor: a failing no_log task's error message prints nothing it was given")
    return 0


if __name__ == "__main__":
    sys.exit(main())
