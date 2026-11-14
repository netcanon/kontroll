"""Every test file whose source starts git takes `isolated_git` module-wide, and the fixture outlasts a row's undo.

WHY — `isolated_git` (tests/conftest.py) takes the runner's GIT_* out and points git at an empty configuration. A
suite run from a linked worktree's pre-commit hook inherits the developer's GIT_DIR and GIT_INDEX_FILE, and a git a
row starts without the fixture acts on the developer's repository: test_sync's skip-worktree row staged
config/fleet.yml in their index (and, under a GIT_WORK_TREE naming theirs, wrote it into their work tree), and
test_local_canonical's busy-check row committed a seed onto their checked-out branch (read B-f18, S1). A run of the
whole suite under a stand-in GIT_DIR found rows in nine more files committing there, making branches, moving HEAD and
writing a test identity into the repository's config, and under a stand-in GIT_INDEX_FILE staging an entry for a blob
the repository does not hold (read B-f19, S1). A row lost the fixture three ways: its file never took it; its file's
git environment was a copy of `os.environ` made at import (test_fleet_line's), which keeps a hook's GIT_DIR whatever
the fixture does; or, while the fixture set its environment through the row's own `monkeypatch`, the row's
`monkeypatch.undo()` took it out (read B-f18, S1: twelve test_sync rows; fold 19 found four more in test_discard and
test_promote_cas). The fixture now keeps its environment in a MonkeyPatch of its own, and the rule below holds the
first two.

The rebase's read found three more (read B-r21): main's test_alerting_metrics started git as `shutil.which("git")`,
which the rule did not read as git, and staged into a hook's index (S1); test_gitignore_secret_shapes took the fixture
module-wide but ran its `git init` in a module-scoped fixture, set up before the fixture holds, which reinitialised a
hook's repository (N7); and the rule passed a copy made at import in a block, a class, a default argument or a call,
`environ` imported from os, a `GIT_DIR` read at import, and a `pytestmark` that names the fixture only in a string or
is assigned again (N5). The rule now reads each: an argv headed by "git", `shutil.which("git")` or a name bound to
either; the last `pytestmark`, by its `usefixtures`; everything that runs at import; and a git started in a fixture
wider than a row. A row whose own `monkeypatch` was set up before the fixture and set a `GIT_*` through it put the
fixture's configuration back after the fixture had gone, for every later row (S4): tests/conftest.py's
`_every_row_gives_the_git_environment_back` fails such a row at its teardown, and a row below holds it to that.

The rule reads the source, so a file is in by default: a new file that starts git is held until EXEMPT names it with a
reason. It cannot see a row that starts git only through the code under test (no git argv in its own source): a run of
the suite with GIT_DIR and GIT_INDEX_FILE naming a stand-in repository is the check for those.
"""
import ast
import glob
import os
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The test files that start git and need not take the fixture module-wide, each with why.
EXEMPT = {
    "unit/test_executable_bits.py": "reads the suite's own checkout (`ls-files -s`) and writes nothing",
    "unit/test_homepage_env_isolation.py": "reads the suite's own checkout (`check-ignore`) and writes nothing",
    "unit/test_install_commands.py": "reads the suite's own checkout (`ls-files`) and writes nothing",
    "unit/test_promote_hint_single_source.py": "reads the suite's own checkout (`ls-files`), with every repository "
                                               "variable taken out, and writes nothing",
    "unit/test_bind_sources.py": "reads a tree's tracked files (`rev-parse`, `ls-files`), with every repository "
                                 "variable taken out, and writes nothing, but in its one row that makes a repository, "
                                 "which takes the fixture by its own mark",
    "unit/test_check_doc_links.py": "starts git in one row alone, which makes a repository and takes the fixture by "
                                    "its own mark",
    "unit/test_group_dispatch_refuses_local.py": "reads the suite's own checkout (`ls-files`) and writes nothing",
    "unit/test_requires_checkout.py": "each row that makes a repository takes the fixture by its own mark, but the "
                                      "end-to-end row, whose `git init` of a scratch directory and whose pytest run "
                                      "take an environment built at the call with every repository variable out; "
                                      "the one that reads the suite's own checkout builds its git's environment at "
                                      "the call, the repository variables out, as the fixture's empty configuration "
                                      "would change what that read answers",
}
# The git verbs an EXEMPT file runs outside a row that takes the fixture which are not reads, each with why it is safe.
WRITES = {
    "unit/test_requires_checkout.py::test_the_marked_rows_skip_with_the_reason_in_a_copy_that_is_not_a_checkout: "
    "git init": "a scratch directory's, with every repository variable out of the environment it is given (EXEMPT)",
}
# The verbs that read a repository and write nothing in it.
_READS = {"ls-files", "rev-parse", "check-ignore", "cat-file", "log", "show", "diff", "status", "archive", "ls-tree",
          "for-each-ref", "show-ref", "merge-base", "describe", "version", "--version"}


def _which_git(e):
    """`shutil.which("git")`, or any callable named `which` given that one string."""
    return (isinstance(e, ast.Call) and ast.unparse(e.func).split(".")[-1] == "which" and len(e.args) == 1
            and isinstance(e.args[0], ast.Constant) and e.args[0].value == "git")


def _is_git(e, names=frozenset()):
    """`"git"`, `shutil.which("git")`, or a name in `names`."""
    return (isinstance(e, ast.Constant) and e.value == "git") or _which_git(e) or (
        isinstance(e, ast.Name) and e.id in names)


def _git_names(tree):
    """The names the module binds anywhere to `shutil.which("git")`, or to an argv (a list or a tuple) whose head is
    "git" or `shutil.which("git")`."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            v = n.value
            if _which_git(v) or (isinstance(v, (ast.List, ast.Tuple)) and v.elts and _is_git(v.elts[0])):
                names |= {t.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                          if isinstance(t, ast.Name)}
    return names


def _starts_git(tree, names=None):
    """Whether `tree` (a module, or a part of one given the module's `names`) holds a call whose first argument is an
    argv starting git: a list or a tuple headed by "git", `shutil.which("git")` or a name bound to either, or a name
    bound to such an argv — alone, or with more added to it (`["git", "-C", d] + list(args)`, `git + [...]`)."""
    names = _git_names(tree) if names is None else names
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args:
            a = n.args[0]
            while isinstance(a, ast.BinOp):
                a = a.left
            if (isinstance(a, ast.Name) and a.id in names) or (
                    isinstance(a, (ast.List, ast.Tuple)) and a.elts and _is_git(a.elts[0], names)):
                return True
    return False


def _takes_it(tree):
    """Whether the module's LAST module-level `pytestmark` — the one pytest reads — holds a `usefixtures` call that
    names "isolated_git" as a string of its own: the name inside another string (a warning filter, a reason) holds
    nothing, and neither does a `pytestmark` assigned again after it."""
    last = None
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
                last = n.value
    return last is not None and any(
        isinstance(c, ast.Call) and ast.unparse(c.func).split(".")[-1] == "usefixtures"
        and any(isinstance(a, ast.Constant) and a.value == "isolated_git" for a in c.args) for c in ast.walk(last))


def _copies_the_environment(tree):
    """Whether anything that runs at import takes `os.environ` whole (a copy made once): module-level statements of
    every kind and the blocks they hold (`if`, `try`, `with`, `for`, `while`), class bodies, and every function's
    default arguments and decorators; `os` imported under another name, and `environ` imported from it, are the same.
    A read of one variable — `os.environ[<key>]`, `os.environ.get(<key>, …)`, `<key> in os.environ` — whose key is a
    string not starting `GIT_` is no copy: any other use is one, and a `GIT_DIR` read at import is the one variable
    that carries a hook's repository. Text that only names it (a script written for a subprocess) is no use of it."""
    oses, environs = {"os"}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            oses |= {a.asname for a in n.names if a.name == "os" and a.asname}
        elif isinstance(n, ast.ImportFrom) and n.module == "os":
            environs |= {a.asname or a.name for a in n.names if a.name == "environ"}

    def one_read(key):
        return isinstance(key, ast.Constant) and isinstance(key.value, str) and not key.value.startswith("GIT_")

    def whole(node):
        up = {c: p for p in ast.walk(node) for c in ast.iter_child_nodes(p)}
        for a in ast.walk(node):
            if not ((isinstance(a, ast.Attribute) and a.attr == "environ" and isinstance(a.value, ast.Name)
                     and a.value.id in oses) or (isinstance(a, ast.Name) and a.id in environs)):
                continue
            p = up.get(a)
            if isinstance(p, ast.Subscript) and p.value is a:
                key = p.slice
            elif isinstance(p, ast.Attribute) and p.attr == "get" and isinstance(up.get(p), ast.Call) \
                    and up[p].func is p:
                key = up[p].args[0] if up[p].args else ast.Constant("")      # `.get()` alone raises: no copy
            elif isinstance(p, ast.Compare) and p.comparators == [a] and isinstance(p.ops[0], (ast.In, ast.NotIn)):
                key = p.left
            else:
                key = None
            if not one_read(key):
                return True
        return False

    def at_import(body):
        for n in body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parts = n.args.defaults + [d for d in n.args.kw_defaults if d] + n.decorator_list
            elif isinstance(n, ast.ClassDef):
                if at_import(n.body):
                    return True
                parts = n.decorator_list + n.bases + [k.value for k in n.keywords]
            elif isinstance(n, (ast.If, ast.Try, getattr(ast, "TryStar", ast.Try), ast.With, ast.AsyncWith, ast.For,
                                ast.AsyncFor, ast.While)):
                blocks = [getattr(n, k, None) or [] for k in ("body", "orelse", "finalbody")]
                blocks += [h.body for h in getattr(n, "handlers", None) or []]
                if any(at_import(b) for b in blocks):
                    return True
                parts = [getattr(n, k) for k in ("test", "iter") if getattr(n, k, None) is not None]
                parts += [i.context_expr for i in getattr(n, "items", None) or []]
            else:
                parts = [n]
            if any(whole(x) for x in parts):
                return True
        return False
    return at_import(tree.body)


def _fixture_scope(fn):
    """The scope a function's `pytest.fixture` decorator gives it — "function" where it names none, the source of
    one it computes — or None for a function that is no fixture."""
    for d in fn.decorator_list:
        call = d if isinstance(d, ast.Call) else None
        if ast.unparse(call.func if call else d).split(".")[-1] != "fixture":
            continue
        for k in call.keywords if call else []:
            if k.arg == "scope":
                return k.value.value if isinstance(k.value, ast.Constant) else ast.unparse(k.value)
        return "function"
    return None


def _wide_fixtures_starting_git(tree):
    """Each fixture wider than a row (its scope not "function") that starts git — in its own body, or through a
    function of the module it calls by name, at any depth — as `<name> (scope <scope>)`. Such a fixture is set up
    before a row's `isolated_git` applies, so its git runs with the runner's GIT_DIR (read B-r21, N7)."""
    names = _git_names(tree)
    defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    starters = {name for name, fn in defs.items() if _starts_git(fn, names)}
    grew = True
    while grew:
        grew = False
        for name, fn in defs.items():
            if name not in starters and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                                            and c.func.id in starters for c in ast.walk(fn)):
                starters.add(name)
                grew = True
    return ["%s (scope %s)" % (name, scope) for name, fn in sorted(defs.items())
            for scope in [_fixture_scope(fn)] if scope not in (None, "function") and name in starters]


def _each_test_file(tests):
    for path in sorted(glob.glob(os.path.join(tests, "**", "test_*.py"), recursive=True)):
        with open(path, encoding="utf-8") as fh:
            yield os.path.relpath(path, tests).replace(os.sep, "/"), ast.parse(fh.read(), path)


def _lacking(tests):
    """Each test file under `tests` that starts git, but EXEMPT, and does not take `isolated_git` in its module
    `pytestmark`, copies `os.environ` at import, or starts git in a fixture wider than a row, as `<file>: <why>`."""
    out = []
    for rel, tree in _each_test_file(tests):
        if rel in EXEMPT or not _starts_git(tree):
            continue
        if not _takes_it(tree):
            out.append(rel + ": no isolated_git in its pytestmark")
        if _copies_the_environment(tree):
            out.append(rel + ": copies os.environ at import")
        out += [rel + ": starts git in a fixture wider than a row, " + f for f in _wide_fixtures_starting_git(tree)]
    return out


def _stale(tests):
    """Each EXEMPT file that is not under `tests`, or starts git no more, as `<file>: <why>`."""
    out = []
    for rel in sorted(EXEMPT):
        path = os.path.join(tests, *rel.split("/"))
        if not os.path.isfile(path):
            out.append(rel + ": gone")
            continue
        with open(path, encoding="utf-8") as fh:
            if not _starts_git(ast.parse(fh.read(), path)):
                out.append(rel + ": starts no git")
    return out


def _verb(argv):
    """The git verb a literal argv names: its first word after "git" that is not an option (`-C <dir>` and
    `-c <key=value>` skipped whole), or None where that is not a string."""
    words = [e.value if isinstance(e, ast.Constant) else None for e in argv.elts]
    i = 1
    while i < len(words):
        if words[i] in ("-C", "-c"):
            i += 2
        elif isinstance(words[i], str) and words[i].startswith("-"):
            i += 1
        else:
            return words[i]
    return None


def _exempt_writes(tests):
    """Each git an EXEMPT file starts, from a literal argv in a function that does not take `isolated_git` by its own
    mark or argument, whose verb is not a read, as `<file>::<function>: git <verb>`."""
    out = []
    for rel, tree in _each_test_file(tests):
        if rel not in EXEMPT:
            continue
        names = _git_names(tree)
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if "isolated_git" in [a.arg for a in fn.args.args] or any(
                    isinstance(c, ast.Constant) and c.value == "isolated_git"
                    for d in fn.decorator_list for c in ast.walk(d)):
                continue
            for c in [n for n in ast.walk(fn) if isinstance(n, ast.Call) and n.args]:
                a = c.args[0]
                while isinstance(a, ast.BinOp):
                    a = a.left
                if isinstance(a, (ast.List, ast.Tuple)) and a.elts and _is_git(a.elts[0], names):
                    verb = _verb(a)
                    if verb is not None and verb not in _READS:
                        out.append("%s::%s: git %s" % (rel, fn.name, verb))
    return sorted(set(out))


def test_every_test_file_that_starts_git_takes_isolated_git_module_wide():
    """Each test file under tests/ whose source starts git, but those EXEMPT names with a reason, takes `isolated_git`
    in its module `pytestmark`, copies no `os.environ` at import and starts git in no fixture wider than a row; each
    EXEMPT file is there and still starts git; and an EXEMPT file's gits outside a row that takes the fixture only
    read, but those WRITES names with why. Guards a git a row starts on the runner's GIT_DIR (read B-f19, S1): fold 19
    held a hand list of six files, and the suite's run under a stand-in GIT_DIR found rows in nine more committing
    into it, test_fleet_line's among them through an environment copied at import, which the fixture alone left in
    place; main's test_alerting_metrics through `shutil.which("git")`, and test_gitignore_secret_shapes' module
    fixture, the rule passed (read B-r21, S1 and N7); an exemption whose file has gone or starts git no more would
    let a new file of its name in unheld; and an EXEMPT reason is the rule's only record of why a file needs no
    fixture, so a write it does not name is a reason that is no longer true (read B-r21, N3)."""
    assert _lacking(TESTS) == [], _lacking(TESTS)
    assert _stale(TESTS) == [], _stale(TESTS)
    assert _exempt_writes(TESTS) == sorted(WRITES), _exempt_writes(TESTS)


def test_the_rule_names_each_planted_file_that_starts_git_unheld(tmp_path):
    """A planted tree: a file starting git from a list with `unit` alone in its `pytestmark`, one from a list with
    more added to it, one from a tuple in another directory, two taking `isolated_git` module-wide but copying
    `os.environ` at import (spread into a dict, and through `dict()`); and beside them one that takes it, one that
    takes it and at import reads one variable (by `[...]` and by `.get`) and writes a script naming `os.environ.copy()`,
    one that starts no git, an EXEMPT name and a helper that is not a test file. `_lacking` names the five, each with
    why, and none of the rest (a read of one variable, or text naming the environment, copies nothing: main's
    test_backup_cli_dry_run and test_fresh_node_check_script were named for them); `_stale` names every
    EXEMPT file but the planted one as gone, and that one once it starts git no more. The rule's positive
    control (read B-f19, N3): a matcher that never matches, or a mark read from the wrong place, passes the real tree
    as a sound one does, and fold 19's scan, which nothing pointed at a planted tree, survived both
    (S20-scan-matcher-typo, S20-scan-ignores-module-wide)."""
    unit = 'import os\nimport subprocess\n\nimport pytest\n\npytestmark = pytest.mark.unit\n'
    held = unit.replace("pytest.mark.unit", '[pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]')
    plant = {
        "unit/test_list.py": unit + '\n\ndef test_x(tmp_path):\n'
                                    '    subprocess.run(["git", "init", "-q", str(tmp_path)])\n',
        "unit/test_added.py": unit + '\n\ndef _git(d, *args):\n'
                                     '    return subprocess.run(["git", "-C", d] + list(args))\n',
        "integration/test_tuple.py": unit + '\n\ndef test_x():\n    subprocess.run(("git", "status"))\n',
        "unit/test_env.py": held + '\nENV = {**os.environ, "GIT_AUTHOR_NAME": "t"}\n\n\ndef test_x():\n'
                                   '    subprocess.run(["git", "status"], env=ENV)\n',
        "unit/test_env_copy.py": held + '\nENV = dict(os.environ, GIT_AUTHOR_NAME="t")\n\n\ndef test_x():\n'
                                        '    subprocess.run(["git", "status"], env=ENV)\n',
        "unit/test_env_read.py": held + '\nCOLL = os.environ.get("ANSIBLE_COLLECTIONS_PATH") or ""\n'
                                        'HOME = os.environ["HOME"]\nSCRIPT = "import os; os.environ.copy()"\n\n\n'
                                        'def test_x():\n    subprocess.run(["git", "status"])\n',
        "unit/test_held.py": held + '\n\ndef test_x():\n    subprocess.run(["git", "status"])\n',
        "unit/test_no_git.py": unit + '\n\ndef test_x():\n    subprocess.run(["ls", "git"])\n',
        "unit/test_executable_bits.py": unit + '\n\ndef test_x():\n    subprocess.run(["git", "ls-files", "-s"])\n',
        "unit/helpers.py": unit + '\n\ndef run():\n    subprocess.run(["git", "init"])\n',
    }
    for rel, text in plant.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    assert _lacking(str(tmp_path)) == [
        "integration/test_tuple.py: no isolated_git in its pytestmark",
        "unit/test_added.py: no isolated_git in its pytestmark",
        "unit/test_env.py: copies os.environ at import",
        "unit/test_env_copy.py: copies os.environ at import",
        "unit/test_list.py: no isolated_git in its pytestmark",
    ], _lacking(str(tmp_path))
    gone = [rel + ": gone" for rel in sorted(EXEMPT) if rel != "unit/test_executable_bits.py"]
    assert _stale(str(tmp_path)) == gone, _stale(str(tmp_path))
    (tmp_path / "unit" / "test_executable_bits.py").write_text(unit, encoding="utf-8")
    assert _stale(str(tmp_path)) == sorted(gone + ["unit/test_executable_bits.py: starts no git"]), _stale(
        str(tmp_path))


def test_the_rule_reads_each_wider_shape_a_file_starts_git_or_copies_the_environment_in(tmp_path):
    """A planted tree of the shapes the rebase's read found the rule passed (read B-r21, S1, N5, N7), each in a file
    of its own: git started from `[shutil.which("git"), …]`, from a name bound to `shutil.which("git")`, and from a
    name bound to an argv (`BASE + [...]`), unheld; a `pytestmark` naming the fixture only inside a warning filter's
    string, and one held and then assigned again; the fixture named on a row's own mark alone; and, held, a copy at
    import as `os.environ.copy()`, an annotated `{**os.environ}`, a default argument, inside an `if`, a class
    attribute, `.update(os.environ)`, `{**environ}` with `environ` imported from os, `os.environ.get("GIT_DIR")`; a git
    `init` in a module-scoped fixture, and in a session-scoped one through a helper it calls. `_lacking` names each
    with why. Beside them, held, three it names none of: `"CI" in os.environ` and `os.environ.get("HOME", "")` at
    import, a function-scoped fixture's `init`, and an environment built from `os.environ` at the call. Guards each
    reading the rule gained: a file planted in a shape it misses passes the real tree as a held one does (the read's
    mutants R21-fleet-git-by-which, R21-pm-backups-in-a-string, R21-pm-backups-reassigned, R21-takes-it-anywhere,
    R21-copies-* and R21-fleet-env-* passed the planted row above)."""
    unit = 'import os\nimport shutil\nimport subprocess\n\nimport pytest\n\npytestmark = pytest.mark.unit\n'
    held = unit.replace("pytest.mark.unit", '[pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]')
    run = '\n\ndef test_x():\n    subprocess.run(["git", "status"])\n'
    module = ('\n\n@pytest.fixture(scope="module")\ndef repo(tmp_path_factory):\n'
              '    d = tmp_path_factory.mktemp("r")\n    subprocess.run(["git", "init", "-q", str(d)])\n    return d\n')
    session = ('\n\ndef _init(d):\n    subprocess.run(["git", "init", "-q", str(d)])\n\n\n'
               '@pytest.fixture(scope="session")\ndef repo(tmp_path_factory):\n'
               '    d = tmp_path_factory.mktemp("r")\n    _init(d)\n    return d\n')
    plant = {
        "unit/test_which.py": unit + '\n\ndef test_x():\n    subprocess.run([shutil.which("git"), "status"])\n',
        "unit/test_which_name.py": unit + '\nGIT = shutil.which("git")\n\n\ndef test_x():\n'
                                          '    subprocess.run([GIT, "status"])\n',
        "unit/test_argv_name.py": unit + '\nBASE = ["git", "-C", "."]\n\n\ndef test_x():\n'
                                         '    subprocess.run(BASE + ["status"])\n',
        "unit/test_mark_in_a_string.py": unit.replace(
            "pytest.mark.unit", '[pytest.mark.unit, pytest.mark.filterwarnings("default:isolated_git")]') + run,
        "unit/test_mark_assigned_again.py": held + '\npytestmark = pytest.mark.unit\n' + run,
        "unit/test_row_only.py": unit + '\n\n@pytest.mark.usefixtures("isolated_git")\ndef test_x(tmp_path):\n'
                                        '    subprocess.run(["git", "init", "-q", str(tmp_path)])\n',
        "unit/test_copy_call.py": held + '\nENV = os.environ.copy()\n' + run,
        "unit/test_copy_annotated.py": held + '\nENV: dict = {**os.environ}\n' + run,
        "unit/test_copy_default.py": held + '\n\ndef _git(*args, env={**os.environ}):\n'
                                            '    return subprocess.run(["git"] + list(args), env=env)\n',
        "unit/test_copy_in_if.py": held + '\nif os.name == "posix":\n    ENV = dict(os.environ)\n' + run,
        "unit/test_copy_class.py": held + '\n\nclass TestX:\n    ENV = dict(os.environ)\n' + run,
        "unit/test_copy_update.py": held + '\nENV = {}\nENV.update(os.environ)\n' + run,
        "unit/test_copy_from_os.py": held + '\nfrom os import environ  # noqa: E402\n\nENV = {**environ}\n' + run,
        "unit/test_copy_git_dir.py": held + '\nGD = os.environ.get("GIT_DIR")\n' + run,
        "unit/test_fixture_module.py": held + module,
        "unit/test_fixture_session.py": held + session,
        "unit/test_reads_one.py": held + '\nIN_CI = "CI" in os.environ\nHOME = os.environ.get("HOME", "")\n' + run,
        "unit/test_fixture_function.py": held + '\n\n@pytest.fixture\ndef repo(tmp_path):\n'
                                                '    subprocess.run(["git", "init", "-q", str(tmp_path)])\n'
                                                '    return tmp_path\n',
        "unit/test_env_at_the_call.py": held + '\n\ndef _git(*args):\n    return subprocess.run(["git"] + list(args), '
                                               'env=dict(os.environ, GIT_AUTHOR_NAME="t"))\n',
    }
    for rel, text in plant.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    copies = ["call", "annotated", "default", "in_if", "class", "update", "from_os", "git_dir"]
    unheld = ["which", "which_name", "argv_name", "mark_in_a_string", "mark_assigned_again", "row_only"]
    want = (["unit/test_copy_%s.py: copies os.environ at import" % c for c in copies]
            + ["unit/test_%s.py: no isolated_git in its pytestmark" % u for u in unheld]
            + ["unit/test_fixture_module.py: starts git in a fixture wider than a row, repo (scope module)",
               "unit/test_fixture_session.py: starts git in a fixture wider than a row, repo (scope session)"])
    assert sorted(_lacking(str(tmp_path))) == sorted(want), _lacking(str(tmp_path))


def test_an_exempt_files_write_outside_a_fixture_row_is_named(tmp_path):
    """A planted EXEMPT file (`unit/test_executable_bits.py`) holding a row that reads (`ls-files`, after `-C <dir>`
    and `-c <key=value>`), a row that writes (`commit`) without the fixture, and a row that writes under the fixture by
    its own mark: `_exempt_writes` names the one unheld write, and none of the rest. Guards the check that keeps an
    EXEMPT reason true (read B-r21, N3: test_requires_checkout's reason said every row that makes a repository takes
    the fixture, and its end-to-end row's `git init` did not)."""
    (tmp_path / "unit").mkdir()
    (tmp_path / "unit" / "test_executable_bits.py").write_text(
        'import subprocess\n\nimport pytest\n\n\ndef test_reads():\n'
        '    subprocess.run(["git", "-C", ".", "-c", "core.quotePath=false", "ls-files"])\n\n\n'
        'def test_writes():\n    subprocess.run(["git", "commit", "-qm", "x"])\n\n\n'
        '@pytest.mark.usefixtures("isolated_git")\ndef test_held(tmp_path):\n'
        '    subprocess.run(["git", "init", "-q", str(tmp_path)])\n', encoding="utf-8")
    assert _exempt_writes(str(tmp_path)) == ["unit/test_executable_bits.py::test_writes: git commit"]


_GUARDED = '''import os

import pytest

_early = {}


class TestARowWhoseMonkeypatchCameFirst:
    @pytest.fixture(autouse=True)
    def _asks_for_monkeypatch_first(self, monkeypatch):
        monkeypatch.setattr(os.path, "sep", os.path.sep)

    @pytest.mark.usefixtures("isolated_git")
    def test_sets_git_config_global_through_its_own_monkeypatch(self, monkeypatch, tmp_path):
        _early["gcg"] = os.environ["GIT_CONFIG_GLOBAL"]
        (tmp_path / "own-config-value").write_bytes(b"")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "own-config-value"))


def test_the_row_after_it():
    assert "gcg" in _early
    assert os.environ.get("GIT_CONFIG_GLOBAL") != _early["gcg"], "the fixture's configuration outlived its row"
'''


def test_a_row_that_leaves_the_git_environment_changed_fails_at_its_teardown_and_the_next_row_has_it_back(tmp_path):
    """A planted pair, run by a pytest of its own with tests/conftest.py as its plugin: a row whose own `monkeypatch`
    is set up before `isolated_git` (an autouse fixture asks for it, as test_canon_drop's `_the_answer_is_asked_again`
    did) and sets GIT_CONFIG_GLOBAL through it, then a row after it without the fixture. The first errors at its
    teardown — "this row left the git environment changed after its teardown: GIT_CONFIG_GLOBAL", naming the variable
    and neither the value the row set nor the one it left (the fixture's, its undo's doing) — and the row after it
    passes: GIT_CONFIG_GLOBAL is not the fixture's. Guards `_every_row_gives_the_git_environment_back` (read B-r21, S4):
    teardown runs in reverse, so the fixture's own MonkeyPatch exited first and the row's undo then put back the value
    it had saved — the fixture's — for every later row of the run; no order of the two MonkeyPatches serves every row,
    so the guard fails such a row instead, and puts the environment back. Run apart, since the planted row is meant to
    fail. (The left value is the one a message would print: a check for the row's own passed a guard printing values,
    fold 11's G-guard-says-values.)"""
    planted = tmp_path / "test_planted_guard.py"
    planted.write_text(_GUARDED, encoding="utf-8")
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-rA", "-o", "addopts=", "-p", "no:cacheprovider",
                        "-p", "conftest", "--basetemp", str(tmp_path / "bt"), str(planted)],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=300,
                       env=dict(os.environ, PYTHONPATH=TESTS, PYTHONDONTWRITEBYTECODE="1"))
    out = p.stdout + p.stderr
    assert p.returncode == 1, out
    assert "this row left the git environment changed after its teardown: GIT_CONFIG_GLOBAL" in out, out
    assert re.search(r"^PASSED \S*::test_the_row_after_it\b", out, re.M), out
    assert re.search(r"^ERROR \S*::test_sets_git_config_global_through_its_own_monkeypatch\b", out, re.M), out
    assert "own-config-value" not in out and "no-git-config" not in out, out


@pytest.fixture
def _a_hooks_git():
    """GIT_DIR and GIT_INDEX_FILE set as a linked worktree's pre-commit hook sets them, before `isolated_git` runs, in
    a MonkeyPatch of their own (put back after the row)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GIT_DIR", "/nowhere/developer/.git")
        mp.setenv("GIT_INDEX_FILE", "/nowhere/developer/.git/index")
        yield


@pytest.mark.usefixtures("_a_hooks_git", "isolated_git")
def test_isolated_git_outlasts_the_rows_own_undo(monkeypatch):
    """A row under `isolated_git`, started with a hook's GIT_DIR and GIT_INDEX_FILE set before the fixture ran, that
    patches and then calls `monkeypatch.undo()`: after the undo neither variable is set, and git's configuration is
    still the fixture's empty one. Guards the fixture's own MonkeyPatch (read B-f19, N3): set through the row's, the
    undo put the hook's variables back for every git the row started after it, and fold 19's scan for the undo missed
    one made through a helper, a fixture, a class mark, `request.getfixturevalue` or another name for the row's
    monkeypatch (U20-undo-in-helper and its four peers). Each assert reads named variables into a local first: a
    failing assert on `os.environ` itself prints the runner's whole environment, its tokens too."""
    def hooks():
        return sorted(k for k in ("GIT_DIR", "GIT_INDEX_FILE") if k in os.environ)

    assert hooks() == [], "isolated_git ran after the hook's variables were set"
    want = os.environ["GIT_CONFIG_GLOBAL"]
    monkeypatch.setenv("KONTROLL_TEST_PATCHED", "1")
    monkeypatch.undo()
    patched = "KONTROLL_TEST_PATCHED" in os.environ
    config = (os.environ.get("GIT_CONFIG_GLOBAL"), os.environ.get("GIT_CONFIG_NOSYSTEM"))
    assert not patched
    assert hooks() == []
    assert config == (want, "1"), config
