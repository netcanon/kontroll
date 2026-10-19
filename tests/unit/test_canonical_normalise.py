"""scripts/kontroll-canonical.py --normalise — the play's ownership pass over the canonical (C10, F-CANON).

WHY: `local-canonical.yml` brings every entry of an existing canonical outside `hooks/` to `<operator>:1001`,
group-writable, directories setgid, so that the operator's push and the service's write land beside each other.
The canonical is a directory the network service writes while a deploy is armed, and every name in it is that
writer's to choose. So the pass has to hold three things, and these rows are them:

  * it changes ONLY what it opened: each entry is opened from the directory it was listed in, never through a
    link, and the owner and the mode are set through that descriptor — a symbolic link in the tree is named and
    left as it is, and a file or a directory outside the tree that a link points at is exactly as it was after
    the pass; a pipe is named and not waited on; `hooks/` is passed over;
  * `--check` says what the apply would do and does nothing, with names only — the play's dry-run prints it;
    applied, the bits are set and a second run has nothing to do (the play reports 0 changed);
  * where the platform cannot change an owner or a mode through a descriptor, the pass REFUSES — it does not
    change anything by its path instead — and says so.

The rows that change an OWNER need root and are in tests/integration/test_canonical_normalise_as_root.py (the
gate's root step runs them); here the owner asked for is the caller's own, so only the mode bits move.
"""
import ast
import importlib.util
import os
import stat
import threading

import pytest

import _canon_repo as R

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "scripts", "kontroll-canonical.py")
SECRET = "CONTENT-nobody-should-see-4c1d"


def _load():
    spec = importlib.util.spec_from_file_location("kontroll_canonical_pass", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = _load()
_FD = getattr(tool, "_FD_WRITES", False)
needs_fd = pytest.mark.skipif(not _FD, reason="needs an owner and a mode changed through a descriptor (Linux)")
_ME = (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)


def _run(bare, *flags, capsys, owner=_ME):
    """`main` with `--normalise <bare> --owner <me> --group <my group>` and `flags`: `(rc, stdout lines, stderr)`."""
    rc = tool.main(["--normalise", bare, "--owner", str(owner[0]), "--group", str(owner[1])] + list(flags))
    said = capsys.readouterr()
    return rc, said.out.splitlines(), said.err


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _entries(bare):
    """Every entry of the tree, by path from the top, with its `lstat` mode — links by themselves, not followed."""
    out = {}
    for here, dirs, files in os.walk(bare):
        for name in dirs + files:
            path = os.path.join(here, name)
            out[os.path.relpath(path, bare).replace(os.sep, "/")] = _mode(path)
    return out


def _within(seconds, call):
    """`call()`'s result, or an AssertionError when it has not returned after `seconds` — a pass that waits on
    something it opened must fail the row, not hang the suite."""
    box = []
    worker = threading.Thread(target=lambda: box.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert box, "the call did not return within %d s: it is waiting on something it opened" % seconds
    return box[0]


def _as_git_leaves_it(tmp_path):
    """`_canon_repo.build` — a bare repository with `main` and two proposals — with a `hooks/update` of the
    play's mode, and every entry as `git init` leaves it (files 0644, directories 0755 under the usual umask):
    nothing group-writable, no directory setgid."""
    r = R.build(tmp_path)
    with open(os.path.join(r.bare, "hooks", "update"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(r.bare, "hooks", "update"), 0o755)
    for here, dirs, files in os.walk(r.bare):
        os.chmod(here, 0o755)
        for name in files:
            os.chmod(os.path.join(here, name), 0o644)
    return r


@needs_fd
def test_check_names_what_would_change_and_changes_nothing(tmp_path, capsys):
    """On a canonical as git leaves it, `--check` exits 3 and names entries by kind and path (`mode: HEAD` among
    them; more than the report shows of one kind, which it says), and every mode is what it was; one of the
    files holds a secret, and the output does not. After an apply, a file cleared of the group's write bit, a
    directory cleared of the setgid bit and the top directory cleared of both are exactly the three `mode` lines
    of the next `--check`, with exit 3 again and the modes still as cleared.

    Guards the play's detect task, which runs under the mandated dry-run: a check that changed anything would
    make the dry-run the apply, and one that printed content would put a file of the canonical into a deploy's
    log."""
    r = _as_git_leaves_it(tmp_path)
    with open(os.path.join(r.bare, "description"), "w", encoding="utf-8") as fh:
        fh.write(SECRET + "\n")
    before = _entries(r.bare)
    rc, lines, err = _run(r.bare, "--check", capsys=capsys)
    assert rc == 3 and err == "" and lines, lines
    assert all(line.startswith("mode: ") or line.startswith("unread: ") for line in lines), lines
    assert "mode: HEAD" in lines and len([line for line in lines if line.startswith("mode: ")]) == tool._MAX_PER_KIND
    outside_hooks = [rel for rel in before if rel != "hooks" and not rel.startswith("hooks/")]
    assert [line for line in lines if line.startswith("unread: ")] == [
        "unread: %d more `mode` lines are not shown" % (len(outside_hooks) + 1 - tool._MAX_PER_KIND)]  # + the top
    assert SECRET not in "\n".join(lines)
    assert _entries(r.bare) == before, "a check changed a mode"

    assert _run(r.bare, capsys=capsys)[0] == 0
    os.chmod(os.path.join(r.bare, "refs", "heads", "main"), 0o644)
    os.chmod(os.path.join(r.bare, "refs", "heads"), 0o775)
    os.chmod(r.bare, 0o755)
    rc, lines, _err = _run(r.bare, "--check", capsys=capsys)
    assert (rc, lines) == (3, ["mode: refs/heads", "mode: refs/heads/main", "mode: the top directory"])
    assert _mode(os.path.join(r.bare, "refs", "heads", "main")) == 0o644
    assert _mode(os.path.join(r.bare, "refs", "heads")) == 0o775 and _mode(r.bare) == 0o755


@needs_fd
def test_applied_the_bits_are_set_and_a_second_run_has_nothing_to_do(tmp_path, capsys):
    """Applied to a canonical as git leaves it: exit 0, every regular file outside `hooks/` is group-writable,
    every directory outside `hooks/` is group-writable and setgid, the top directory too, and no other bit of
    any entry changed. Then `--check` exits 0 with no line, and a second apply exits 0 with no line.

    Guards both halves of the grant and the play's idempotence: the apply task runs only when the detect task
    exited 3, so a pass that left a bit unset would run on every deploy (and report a change each time), and
    one that set a bit it was not asked for — an execute bit, a write bit for others — would widen the grant."""
    r = _as_git_leaves_it(tmp_path)
    before = _entries(r.bare)
    rc, lines, _err = _run(r.bare, capsys=capsys)
    assert rc == 0 and lines and all(line.startswith("mode: ") or line.startswith("unread: ") for line in lines)
    after = _entries(r.bare)
    assert set(after) == set(before)
    assert _mode(r.bare) == 0o2775
    for rel, was in before.items():
        if rel == "hooks" or rel.startswith("hooks/"):
            assert after[rel] == was, rel
            continue
        want = was | 0o020 | (0o2000 if os.path.isdir(os.path.join(r.bare, rel)) else 0)
        assert after[rel] == want, (rel, oct(was), oct(after[rel]))
    assert _run(r.bare, "--check", capsys=capsys)[:2] == (0, [])
    assert _run(r.bare, capsys=capsys)[:2] == (0, [])
    assert _entries(r.bare) == after


@needs_fd
def test_a_link_to_a_file_and_to_a_directory_outside_the_tree_is_named_and_what_it_points_at_is_as_it_was(
        tmp_path, capsys):
    """A symbolic link inside the canonical to a file OUTSIDE it (mode 0600), and one to a directory outside it
    (mode 0700) that holds a file (0600) and a directory (0700): the apply exits 0, names both links (`link`),
    and the file, the directory and what is in it have the modes they had — and the links are named again by
    the next `--check`, which exits 0 (a link is nothing to do).

    Guards "changes only what it opened". With paths, a `chmod g+w` of the link's name is a `chmod` of the file
    it names, wherever that is, and a walk that steps through a link to a directory changes everything under
    it; here the link is opened with `O_NOFOLLOW`, which refuses, and nothing behind it is looked at."""
    r = _as_git_leaves_it(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("x\n", encoding="utf-8")
    (outside / "dir").mkdir()
    (outside / "dir" / "inner").write_text("y\n", encoding="utf-8")
    (outside / "dir" / "deeper").mkdir()
    os.chmod(str(outside / "file"), 0o600)
    os.chmod(str(outside / "dir" / "inner"), 0o600)
    os.chmod(str(outside / "dir" / "deeper"), 0o700)
    os.chmod(str(outside / "dir"), 0o700)
    try:
        os.symlink(str(outside / "file"), os.path.join(r.bare, "refs", "to-a-file"))
        os.symlink(str(outside / "dir"), os.path.join(r.bare, "objects", "to-a-directory"))
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot make a symbolic link here")
    rc, lines, _err = _run(r.bare, capsys=capsys)
    assert rc == 0 and {"link: objects/to-a-directory", "link: refs/to-a-file"} <= set(lines), lines
    assert not [line for line in lines if "to-a-" in line and not line.startswith("link: ")], lines
    assert [_mode(str(outside / p)) for p in ("file", "dir", "dir/inner", "dir/deeper")] == [0o600, 0o700, 0o600,
                                                                                            0o700]
    assert _run(r.bare, "--check", capsys=capsys)[:2] == (0, ["link: objects/to-a-directory", "link: refs/to-a-file"])


@needs_fd
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_a_pipe_is_named_and_not_waited_on(tmp_path, capsys):
    """A named pipe at `refs/a-pipe` and at `objects/info/a-pipe`: the apply returns within ten seconds, exits
    0 and names both (`not-a-file`); nothing else of the report concerns them.

    Guards the pass against the one entry that can make it wait forever — a pipe is opened only with
    `O_NONBLOCK`, and here it is not opened at all: it is named by what `lstat` says it is."""
    r = _as_git_leaves_it(tmp_path)
    assert _run(r.bare, capsys=capsys)[0] == 0
    os.mkfifo(os.path.join(r.bare, "refs", "a-pipe"))
    os.mkfifo(os.path.join(r.bare, "objects", "info", "a-pipe"))
    rc, lines, _err = _within(10, lambda: _run(r.bare, capsys=capsys))
    assert (rc, lines) == (0, ["not-a-file: objects/info/a-pipe", "not-a-file: refs/a-pipe"])


@needs_fd
def test_hooks_is_passed_over(tmp_path, capsys):
    """With `hooks/` 0755, `hooks/update` 0644 and a second file `hooks/evil` 0600 — and the rest of the canonical
    as git leaves it — the apply changes every mode outside `hooks/` and none inside it, and no line of its
    report names `hooks` or anything under it — and a `hooks` deeper down (`refs/heads/hooks/`, a branch of that
    name) is normalised like any other entry.

    Guards the one subtree the grant must not reach: the play keeps `hooks/` root's and 0755 so that a gid-1001
    writer cannot plant a hook the next `sudo kontroll-promote` runs as root (2026-10-08 review, finding 2),
    and a pass that made `hooks/update` group-writable would undo that in the task before."""
    r = _as_git_leaves_it(tmp_path)
    os.chmod(os.path.join(r.bare, "hooks", "update"), 0o644)
    with open(os.path.join(r.bare, "hooks", "evil"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(os.path.join(r.bare, "hooks", "evil"), 0o600)
    os.makedirs(os.path.join(r.bare, "refs", "heads", "hooks"))
    with open(os.path.join(r.bare, "refs", "heads", "hooks", "x"), "w", encoding="utf-8") as fh:
        fh.write("0" * 40 + "\n")
    os.chmod(os.path.join(r.bare, "refs", "heads", "hooks"), 0o755)
    os.chmod(os.path.join(r.bare, "refs", "heads", "hooks", "x"), 0o644)
    rc, lines, _err = _run(r.bare, capsys=capsys)
    assert rc == 0 and lines
    assert not [line for line in lines if line.endswith(": hooks") or ": hooks/" in line], lines
    deep = os.path.join(r.bare, "refs", "heads", "hooks")
    assert _mode(deep) == 0o2775 and _mode(os.path.join(deep, "x")) == 0o664, (
        "only the TOP directory's `hooks` is passed over: a `hooks` further down is an entry like any other")
    assert _mode(os.path.join(r.bare, "hooks")) == 0o755
    assert _mode(os.path.join(r.bare, "hooks", "update")) == 0o644
    assert _mode(os.path.join(r.bare, "hooks", "evil")) == 0o600
    assert _mode(os.path.join(r.bare, "HEAD")) == 0o664 and _mode(os.path.join(r.bare, "refs")) == 0o2775


@needs_fd
def test_a_directory_exchanged_for_a_link_after_it_was_looked_at_is_not_gone_into(tmp_path, monkeypatch, capsys):
    """`refs/heads` is exchanged for a symbolic link to a directory OUTSIDE the canonical (0700, holding a file
    0600) at the worst moment — right after the pass has looked at the entry and before it opens it. The apply
    exits 1 and says `refs/heads` was not gone over (`unread`); the outside directory and its file have the
    modes they had; nothing of the report names them.

    Guards the step between the look and the open, where the walk's own `lstat` cannot help: the directory is
    opened from its parent's descriptor with `O_NOFOLLOW`, so an exchanged name is refused rather than
    followed, and a part that was refused is said — exit 1 fails the play's task by name instead of letting a
    deploy pass over it in silence."""
    r = _as_git_leaves_it(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main").write_text("x\n", encoding="utf-8")
    os.chmod(str(outside / "main"), 0o600)
    os.chmod(str(outside), 0o700)
    target = os.path.join(r.bare, "refs", "heads")
    real_kind, done = tool._Dir.kind, []

    def kind_then_exchange(self, name):
        answer = real_kind(self, name)
        if not done and answer == "dir" and name == b"heads" and os.fsdecode(self.path).endswith("refs"):
            done.append(1)
            os.rename(target, target + ".was")
            try:
                os.symlink(str(outside), target)
            except (OSError, NotImplementedError):
                pytest.skip("this host cannot make a symbolic link here")
        return answer
    monkeypatch.setattr(tool._Dir, "kind", kind_then_exchange)
    rc, lines, _err = _run(r.bare, capsys=capsys)
    assert done, "control: the exchange was made"
    assert rc == 1 and "unread: refs/heads" in lines, lines
    assert not [line for line in lines if "outside" in line or "refs/heads/" in line], lines
    assert _mode(str(outside)) == 0o700 and _mode(str(outside / "main")) == 0o600


@needs_fd
def test_a_tree_deeper_than_the_pass_goes_is_said_and_exit_1(tmp_path, capsys):
    """Forty directories one inside the other under `objects/`, with a file at the bottom: the apply exits 1 and
    says the top directory is deeper than the pass goes; what it could reach was changed.

    Guards the bound on the descriptors the walk holds (one per level) and that reaching it is SAID: a pass
    that stopped early and exited 0 would leave the play reporting a grant it did not make."""
    r = _as_git_leaves_it(tmp_path)
    deep = os.path.join(r.bare, "objects", *(["d"] * 40))
    os.makedirs(deep)
    with open(os.path.join(deep, "leaf"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    rc, lines, _err = _run(r.bare, capsys=capsys)
    assert rc == 1 and "unread: the top directory (deeper than this reads)" in lines, lines
    assert _mode(os.path.join(r.bare, "HEAD")) == 0o664


def test_the_pass_refuses_where_no_owner_or_mode_can_be_changed_through_a_descriptor(tmp_path, monkeypatch, capsys):
    """With the capability flag off (`_FD_WRITES = False` — what the script computes on Windows), `--normalise`
    exits 2 with one sentence on stderr that says nothing was changed, prints no report, and a file cleared of
    the group's write bit is still so; `--check` is refused the same way.

    Guards the one fallback the pass must not have: a change by path on a platform that cannot open an entry
    from its directory's descriptor would be exactly the form this mode replaces. The flag is monkeypatched so
    the row holds on Linux too, where the flag is on."""
    r = R.build(tmp_path)
    head = os.path.join(r.bare, "HEAD")
    os.chmod(head, 0o644)
    was = _mode(head)                                     # on Windows a mode is what the host makes of 0644
    monkeypatch.setattr(tool, "_FD_WRITES", False)
    for flags in ((), ("--check",)):
        rc, lines, err = _run(r.bare, *flags, capsys=capsys)
        assert (rc, lines) == (2, [])
        assert "nothing was changed" in err and err.count("\n") == 1 and "descriptor" in err
    assert _mode(head) == was
    if _FD:
        assert not was & 0o020, "control: the bit the pass would have set is still off"


def test_usage_errors_change_nothing_and_say_so(tmp_path, capsys):
    """`--normalise` without `--owner` or `--group`, with a negative uid, with `--strict`; `--audit` with
    `--owner`, `--group` or `--check`; both `--audit` and `--normalise`; `--normalise` of a file and of a path
    that is not there: each exits 2 with one line on stderr and nothing on stdout. `--audit` alone still works.

    Guards the play's command line: a flag the script took for another mode's — a check that audited, an
    owner of -1 (which `fchown` reads as "leave the owner") — would run without an error and do something other
    than what the task says."""
    r = R.build(tmp_path)
    bad = [["--normalise", r.bare], ["--normalise", r.bare, "--owner", "1"], ["--normalise", r.bare, "--group", "1"],
           ["--normalise", r.bare, "--owner", "-1", "--group", "1001"],
           ["--normalise", r.bare, "--owner", "1", "--group", "-1"],
           ["--normalise", r.bare, "--owner", "1", "--group", "1001", "--strict"],
           ["--audit", r.bare, "--owner", "1"], ["--audit", r.bare, "--group", "1"], ["--audit", r.bare, "--check"],
           ["--audit", r.bare, "--normalise", r.bare],
           ["--normalise", os.path.join(r.bare, "HEAD"), "--owner", "1", "--group", "1001"],
           ["--normalise", str(tmp_path / "not-there"), "--owner", "1", "--group", "1001"]]
    for argv in bad:
        assert tool.main(argv) == 2, argv
        said = capsys.readouterr()
        assert said.out == "" and said.err != "", argv
        if said.err.startswith("kontroll-canonical:"):      # the script's own sentence; the rest are argparse's
            assert said.err.count("\n") == 1, argv
    assert tool.main(["--audit", r.bare]) == 0


def test_the_audit_and_the_pass_share_one_walker():
    """Read as a syntax tree: `normalise` and `_files_below` (the audit's walk of `refs/` and `logs/`) each call
    `_walk`; `_walk` and `_enter` (the audit's step into one named directory of the top) are the only functions
    of the script that call `_Dir.sub`; `normalise` opens a file only through `_Dir.file`, which `_Dir.read`
    also uses; and `os.fchown` and `os.fchmod` are called in `_own` only, each on a name called `fd`.

    Guards "one walker": a second walk written for the pass would be a second reading of how a directory is
    opened — the one that was tested is the audit's — and a chown or chmod anywhere but in `_own`, or on
    anything but a descriptor, is a change by path."""
    tree = ast.parse(open(SCRIPT, encoding="utf-8").read())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def calls(fn, attr=None, name=None):
        out = []
        for n in ast.walk(funcs[fn]):
            if isinstance(n, ast.Call):
                if attr and isinstance(n.func, ast.Attribute) and n.func.attr == attr:
                    out.append(n)
                if name and isinstance(n.func, ast.Name) and n.func.id == name:
                    out.append(n)
        return out
    assert calls("normalise", name="_walk") and calls("_files_below", name="_walk")
    assert sorted(fn for fn in funcs if calls(fn, attr="sub")) == ["_enter", "_walk"]
    assert calls("normalise", attr="file") and calls("read", attr="file")
    for attr in ("fchown", "fchmod"):
        where = [fn for fn in funcs if calls(fn, attr=attr)]
        assert where == ["_own"], attr
        assert all(isinstance(c.args[0], ast.Name) and c.args[0].id == "fd" for c in calls("_own", attr=attr))


@needs_fd
def test_an_entry_gone_between_the_look_and_the_open_is_nothing(tmp_path, monkeypatch, capsys):
    """`refs/heads/main.lock` (a file) and `refs/heads/gone` (an empty directory) are removed right after the pass
    has looked at each and before it opens it — what git's own write leaves and takes away during a push the
    service makes while a deploy is armed. The apply exits 0 and no line names either; everything else is as a
    normalised canonical is.

    Guards the one race that is nobody's fault: a part that is no longer there was not "not gone over", and a
    deploy that stopped at `unread: refs/heads/main.lock` because the operator clicked "propose" during it would
    be a false alarm with a frightening name."""
    r = _as_git_leaves_it(tmp_path)
    assert _run(r.bare, capsys=capsys)[0] == 0
    lock = os.path.join(r.bare, "refs", "heads", "main.lock")
    gone = os.path.join(r.bare, "refs", "heads", "gone")
    open(lock, "w").close()
    os.makedirs(gone, exist_ok=True)
    real_kind = tool._Dir.kind

    def kind_then_remove(self, name):
        answer = real_kind(self, name)
        if name == b"main.lock" and answer == "file" and os.path.exists(lock):
            os.unlink(lock)
        if name == b"gone" and answer == "dir" and os.path.isdir(gone):
            os.rmdir(gone)
        return answer
    monkeypatch.setattr(tool._Dir, "kind", kind_then_remove)
    assert _run(r.bare, capsys=capsys)[:2] == (0, [])
    assert not os.path.exists(lock) and not os.path.exists(gone), "control: both were removed under the pass"


@needs_fd
def test_a_file_exchanged_for_a_link_after_it_was_looked_at_is_named_and_not_followed(tmp_path, monkeypatch, capsys):
    """`HEAD` is exchanged for a symbolic link to a file OUTSIDE the canonical (0600) right after the pass has
    looked at it and before it opens it. The apply exits 0, names `link: HEAD`, and the outside file has the
    mode and the content it had; nothing of the report names it.

    Guards the step between the look and the open for a FILE — the walk's `lstat` names a link that was there
    when it looked; this is the one that was not. The file is opened from its directory's descriptor with
    `O_NOFOLLOW`, which refuses a link, and the refusal is named as what it is, not as `unread`."""
    r = _as_git_leaves_it(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("not-the-head\n", encoding="utf-8")
    os.chmod(str(outside), 0o600)
    head = os.path.join(r.bare, "HEAD")
    top = os.fsencode(os.path.abspath(r.bare))
    real_kind, done = tool._Dir.kind, []

    def kind_then_exchange(self, name):
        answer = real_kind(self, name)
        if not done and answer == "file" and name == b"HEAD" and os.fsencode(os.path.abspath(self.path)) == top:
            done.append(1)
            os.unlink(head)
            try:
                os.symlink(str(outside), head)
            except (OSError, NotImplementedError):
                pytest.skip("this host cannot make a symbolic link here")
        return answer
    monkeypatch.setattr(tool._Dir, "kind", kind_then_exchange)
    rc, lines, _err = _run(r.bare, capsys=capsys)
    assert done, "control: the exchange was made"
    parts = [line for line in lines if line.startswith("unread:") and " more `" not in line]   # not the count line
    assert rc == 0 and "link: HEAD" in lines and not parts, lines
    assert _mode(str(outside)) == 0o600 and outside.read_text(encoding="utf-8") == "not-the-head\n"
