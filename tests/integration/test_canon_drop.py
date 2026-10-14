"""Root does not run git in a repository the service can write — with REAL uids (C10, rule 5 of `kontroll.canon`).

WHY: when a deploy is armed, the canonical's own files — its config, its refs, its packs — are written
by a network service (uid 1001). `sudo kontroll-promote` and the installer are root. The seam (`canon.git_env`)
answers every way a repository makes git run a program or read another store THAT SOMEBODY HAS LISTED; this is
for the rest: whatever the files of that store can make a git do, they must make it do as the service that wrote
them, never as root. `canon.git_kwargs` makes a root caller's git the service uid, and these rows hold that the
change of identity is real — a hook that writes down who it runs as — on a canonical laid out as
`ansible/playbooks/local-canonical.yml` leaves one, in every bit the rule and git read (`<operator>:1001`,
directories `2775`, group-writable files, `core.sharedRepository=group`, `hooks/` root's).

It needs ROOT and a Linux kernel: it changes file ownership and starts processes under other uids. Nothing else
in the suite can show this — the rows of tests/unit/ run as whoever runs them and stand a stub in for the uid.
tests/validate.sh runs this file as a step of its own, as root (directly, or through `sudo -n`), and that step
fails when a row here was SKIPPED; in a plain `pytest` run by an ordinary user every row is skipped.
"""
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tests", "unit"))      # the canonical builder lives beside the unit rows

import _canon_repo as R  # noqa: E402
from kontroll import canon  # noqa: E402
from kontroll.service import discard, pending  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not (hasattr(os, "geteuid") and os.geteuid() == 0 and sys.platform.startswith("linux")
                            and all(shutil.which(tool) for tool in ("git", "setpriv", "sh", "id"))),
                       reason="needs root on Linux (it changes ownership and starts processes as other uids), "
                              "with git and setpriv — tests/validate.sh runs it as root"),
]

OPERATOR = 1000                      # a uid that is neither root nor the service: the play's `operator_uid`
SERVICE = {"user": canon.SERVICE_UID, "group": canon.SERVICE_GID, "extra_groups": []}
STAGED = "0000000000c5"              # the run id of the proposal the service stages in these rows
_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}


def _promote():
    """scripts/kontroll-promote.py, loaded by path (its name is not a module name)."""
    spec = importlib.util.spec_from_file_location("kontroll_promote_drop",
                                                  os.path.join(ROOT, "scripts", "kontroll-promote.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def world():
    """A directory every uid can enter. pytest's own `tmp_path` is under a directory only its owner may enter —
    root's, here — and the point of these rows is a git that is NOT root."""
    path = tempfile.mkdtemp(prefix="kontroll-drop-", dir="/tmp")
    os.chmod(path, 0o755)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _the_answer_is_asked_again(monkeypatch):
    """`canon._can_drop` remembers its answer for the life of a process; a row starts without one."""
    monkeypatch.setattr(canon, "_DROP", [])


def _build(world, name="canonical"):
    """`_canon_repo.build` under a path, not a `pathlib` object of pytest's."""
    class _Under:
        def __truediv__(self, leaf):
            return os.path.join(world, leaf)
    return R.build(_Under(), name=name)


def _as_the_play_leaves_it(r):
    """Lay the repository out as `local-canonical.yml` does: everything `<operator>:1001`, directories `2775`,
    files group-writable, `core.sharedRepository=group` — and `hooks/` root's, `0755`. Returns `r`, with
    `r.tree` (the tree of the builder's fast-forward proposal) read while root may still run git here."""
    r.tree = R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode()
    R.git(r.bare, "config", "core.sharedRepository", "group")
    for here, _dirs, files in os.walk(r.bare):
        os.chown(here, OPERATOR, canon.SERVICE_GID)
        os.chmod(here, 0o2775)
        for name in files:
            path = os.path.join(here, name)
            os.chown(path, OPERATOR, canon.SERVICE_GID)
            os.chmod(path, stat.S_IMODE(os.stat(path).st_mode) | 0o060)
    for here, _dirs, files in os.walk(os.path.join(r.bare, "hooks")):
        os.chown(here, 0, 0)
        os.chmod(here, 0o755)
        for name in files:
            os.chown(os.path.join(here, name), 0, 0)
            os.chmod(os.path.join(here, name), 0o755)
    return r


def _service(bare, *argv, stdin=None):
    """Run git in `bare` AS THE SERVICE (uid and gid 1001, no other group), the way the network service's own
    push would write: the repository's own config applies, nothing of the seam. Returns stdout, stripped."""
    env = dict({"PATH": os.environ["PATH"], "GIT_DIR": bare, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull}, **_WHO)
    p = subprocess.run(["git"] + list(argv), input=stdin, capture_output=True, env=env, cwd=bare, **SERVICE)
    assert p.returncode == 0, "as the service, git %s: %s" % (" ".join(argv), p.stderr.decode("utf-8", "replace"))
    return p.stdout.decode().strip()


def _stage_as_the_service(r, run_id=STAGED):
    """A proposal the SERVICE stages: a commit object and a ref it writes itself, on top of `main`."""
    oid = _service(r.bare, "commit-tree", r.tree, "-p", r.main, "-m", "feat: staged by the service")
    _service(r.bare, "update-ref", canon.PROPOSED + run_id, oid)
    assert os.stat(os.path.join(r.bare, "refs", "heads", "proposed", run_id)).st_uid == canon.SERVICE_UID
    return oid


def _a_hook_that_says_who(directory, canary, as_service=None):
    """A `reference-transaction` hook in `directory` that appends the uid it runs as to `canary`. Written by the
    service when `as_service` names the repository (it is then also named by that repository's
    `core.hooksPath`, set by the service), else by whoever runs the row."""
    script = "#!/bin/sh\nid -u >> '%s'\n" % canary
    if as_service:
        subprocess.run(["sh", "-c", 'mkdir -p "$0" && printf "%s" "$1" > "$0/reference-transaction" && '
                                    'chmod 0755 "$0/reference-transaction"', directory, script],
                       check=True, **SERVICE)
        _service(as_service, "config", "core.hooksPath", directory)
        return
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "reference-transaction"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(script)
    os.chmod(os.path.join(directory, "reference-transaction"), 0o755)


def _canary(world):
    """A file anybody may append to, so that a hook can write its uid whoever it runs as."""
    path = os.path.join(world, "who-ran")
    open(path, "w", encoding="utf-8").close()
    os.chmod(path, 0o666)
    return path


def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().split()


def _seam_minus(monkeypatch, *names):
    """Take entries OUT of the seam for the rest of a row (`_canon_repo.seam_without`, applied to every git the
    tool starts) — so that the row shows what the CHANGE OF IDENTITY answers, and not what an entry of the
    environment answers in its place."""
    real = canon.git_env

    def thinner(repo):
        with monkeypatch.context() as inner:
            inner.setattr(canon, "git_env", real)
            return R.seam_without(repo, *names)
    monkeypatch.setattr(canon, "git_env", thinner)


def test_in_a_repository_that_is_roots_own_nothing_changes_identity(world, monkeypatch):
    """Over a repository root made for itself (root's files, no write bit for anybody else) `service_writable`
    answers False, `git_kwargs` carries an environment and nothing else, and — with the seam's own answer to
    hooks taken out — a `reference-transaction` hook that writes down its uid writes `0` during a promote.

    Guards the other half of the rule and is this file's CONTROL: a repository only root can write is one root
    may run git in (the whole unit suite, run as root in the gate container, builds such repositories under a
    directory nobody else may enter — a git made the service uid could not even reach them); and the hook's `0`
    is what shows that the `1001` of the next row is the change of identity and not the hook's own doing."""
    r = _build(world)
    canary = _canary(world)
    _a_hook_that_says_who(os.path.join(world, "hooks-named"), canary)
    R.git(r.bare, "config", "core.hooksPath", os.path.join(world, "hooks-named"))
    assert canon.service_writable(r.bare) is False
    assert sorted(canon.git_kwargs(r.bare)) == ["env"]
    _seam_minus(monkeypatch, "core.hooksPath")
    assert _promote().main(["%s@%s" % (R.FF, r.ff), "--skip-conflict-check", "--repo", r.bare]) == 0
    assert canon.read_ref(r.bare, canon.MAIN)[0] == r.ff
    ran_as = _lines(canary)
    assert ran_as and set(ran_as) == {"0"}, ran_as


def test_on_the_plays_layout_a_promote_by_root_runs_git_as_the_service_never_as_root(world, monkeypatch, capsys):
    """On a canonical laid out as the play leaves it, with a proposal the service staged and a
    `reference-transaction` hook the service planted through `core.hooksPath`, a promote run by ROOT — with the
    seam's own answer to hooks taken out by this row — lands the proposal, and every line the hook wrote is
    `1001`: never `0`. The promote says on stderr as whom git runs.

    Guards the rule itself, by its reversion: the seam points `core.hooksPath` at nothing, so with the seam whole
    this hook would not run at all and the row would pass with or without the change of identity. Taken out, the
    hook stands for EVERYTHING of the repository that nobody has listed — and what it shows is who such a thing
    runs as. Without the change of identity in `git_kwargs` the file holds `0`: a network service that can write
    the canonical's config ran a program as root at the next `sudo kontroll-promote` (measured on the build
    before the seam existed)."""
    r = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(r)
    canary = _canary(world)
    _a_hook_that_says_who(os.path.join(r.bare, "h"), canary, as_service=r.bare)
    assert canon.service_writable(r.bare) is True
    kw = canon.git_kwargs(r.bare)
    assert (kw["user"], kw["group"], kw["extra_groups"]) == (canon.SERVICE_UID, canon.SERVICE_GID, [])
    _seam_minus(monkeypatch, "core.hooksPath")
    assert _promote().main(["%s@%s" % (STAGED, oid), "--skip-conflict-check", "--repo", r.bare]) == 0
    assert canon.read_ref(r.bare, canon.MAIN)[0] == oid
    ran_as = _lines(canary)
    assert ran_as and set(ran_as) == {str(canon.SERVICE_UID)}, ran_as
    assert "as the service uid (%d)" % canon.SERVICE_UID in capsys.readouterr().err


def test_the_listing_the_promote_the_discard_and_the_tree_read_all_work_as_the_service(world, monkeypatch):
    """On the play's layout, as root, with the seam's `safe.directory` entry taken out as well (so that nothing
    anywhere tells git the repository is safe): the Pending listing lists the service's proposal, the promote
    lands it, the commit's tree is read back object by object, and a discard removes another proposal.

    Guards the change of identity being livable: the canonical's top directory is the operator's, and git
    refuses a repository whose owner is not the caller unless it is named outright (`GIT_DIR`, which the seam
    sets — the claim is that this suffices for uid 1001 with no `safe.directory` at all). What the promote
    leaves behind, and who can write beside it, is the next row's."""
    r = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(r)
    _seam_minus(monkeypatch, "safe.directory")
    listed = pending.list_pending(r.bare)
    assert STAGED in [p["run_id"] for p in listed["proposals"]], listed.get("note")
    assert _promote().main(["%s@%s" % (STAGED, oid), "--skip-conflict-check", "--repo", r.bare]) == 0
    assert canon.read_ref(r.bare, canon.MAIN)[0] == oid
    assert set(canon.read_tree_verified(r.bare, oid)) == {b"seed.txt", b"adv.txt", b"ff.txt"}
    assert discard.discard_proposal(R.STALE, canonical=r.bare)["ok"] is True
    assert canon.read_ref(r.bare, canon.PROPOSED + R.STALE) is None


# Asked of `sh`, not of the interpreter this suite runs under: the child is the SERVICE uid, and that interpreter
# may live where only its owner goes (a runner's tool cache under its own home) — the child would never start.
_TERMINAL_PROBE = ('if [ -t 0 ]; then a=stdin-is-a-terminal; else a=stdin-is-not; fi; '
                   'if ( : < /dev/tty ) 2>/dev/null; then b=has-a-controlling-terminal; else b=has-none; fi; '
                   'printf "%s %s" "$a" "$b"')


def _on_a_terminal(world, keywords):
    """What a child started with `keywords()` says of its standard input and of its controlling terminal when its
    parent is ROOT ON A TERMINAL: the leader of a session whose terminal is a pseudo-terminal made here — what a
    shell under `sudo` is. The parent side of the fork writes the child's answer to a file and leaves."""
    import pty                                            # POSIX only; these rows are skipped elsewhere
    answer = os.path.join(world, "said-%d" % len(os.listdir(world)))
    pid, master = pty.fork()
    if pid == 0:
        try:
            try:
                said = subprocess.run(["sh", "-c", _TERMINAL_PROBE], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, **keywords()).stdout
            except BaseException as exc:                  # a child that could not start is an answer too
                said = ("the child did not start: %s" % type(exc).__name__).encode()
            with open(answer, "wb") as fh:
                fh.write(said)
        finally:
            os._exit(0)
    try:
        while os.read(master, 4096):
            pass
    except OSError:
        pass                                              # the terminal's other end is gone: the child has left
    os.waitpid(pid, 0)
    os.close(master)
    with open(answer, encoding="utf-8") as fh:
        return fh.read()


def test_the_services_git_is_given_nothing_of_roots_session(world):
    """Root on a terminal starts a child with the keywords `git_kwargs` gives on the play's layout: the child's
    standard input is not the terminal, and it has no controlling terminal at all — for a git the tool feeds
    (`fed=True`, its standard input a pipe of the tool's) as for one it does not. The control is the same child
    started with the identity ALONE: its standard input is root's terminal, and that terminal is its own.

    Guards what "as the service" has to mean. A child that has the service's uid but sits in root's session
    holds the terminal root was started from — the one the person at the keyboard types the next line into.
    The identity is handed over with a session of the child's own and no standard input of the caller's."""
    r = _as_the_play_leaves_it(_build(world))

    def alone():
        return dict(SERVICE)

    def as_the_seam_starts_it():
        return canon.git_kwargs(r.bare)

    def fed_by_the_tool():
        return dict(canon.git_kwargs(r.bare, fed=True), stdin=subprocess.PIPE)
    assert _on_a_terminal(world, alone) == "stdin-is-a-terminal has-a-controlling-terminal", "control"
    assert _on_a_terminal(world, as_the_seam_starts_it) == "stdin-is-not has-none"
    assert _on_a_terminal(world, fed_by_the_tool) == "stdin-is-not has-none"


def _swap_a_link_in(bare):
    """As the service: `refs/heads` renamed away, a LINK to the root-owned `hooks/` in its place."""
    subprocess.run(["sh", "-c", 'mv "$0/refs/heads" "$0/refs/heads.was" && ln -s ../hooks "$0/refs/heads"', bare],
                   check=True, **SERVICE)


def _rename_it_away(bare):
    """As the service: `refs/heads` renamed away, nothing in its place."""
    subprocess.run(["sh", "-c", 'mv "$0/refs/heads" "$0/refs/heads.was"', bare], check=True, **SERVICE)


def _swap_a_closed_directory_in(bare):
    """A directory of the operator's with NO group write bit beside `refs/heads` (root makes it, as a deploy
    might leave one), which the service then moves into its place."""
    other = os.path.join(bare, "refs", "remotes")
    os.mkdir(other)
    os.chown(other, OPERATOR, canon.SERVICE_GID)
    os.chmod(other, 0o755)
    subprocess.run(["sh", "-c", 'mv "$0/refs/heads" "$0/refs/heads.was" && mv "$0/refs/remotes" "$0/refs/heads"',
                    bare], check=True, **SERVICE)


@pytest.mark.parametrize("exchange", [_swap_a_link_in, _rename_it_away, _swap_a_closed_directory_in],
                         ids=["a-link-to-roots-hooks", "renamed-away", "a-directory-with-no-group-write"])
def test_the_service_cannot_talk_root_out_of_the_change_of_identity(world, exchange):
    """On the play's layout the service exchanges `refs/heads` — for a link to the root-owned `hooks/`, for
    nothing, or (where one stands beside it: this row makes one, as root) for a directory of somebody else's
    that has no group write bit. Each time `service_writable` still answers True and `git_kwargs` still carries
    the service's identity; the control shows what ONE look at `refs/heads` would have said.

    Guards the question being asked of the whole repository. One `stat` of `refs/heads` looks like enough — the
    service cannot change that directory's owner or mode — and is not: the service has write on `refs/`, so it
    replaces the ENTRY (measured, in the order of the cases: False, an exception, False). With the answer
    False root would go on running git as root in a repository whose every other file the service writes."""
    r = _as_the_play_leaves_it(_build(world))
    exchange(r.bare)

    def one_look():
        st = os.stat(os.path.join(r.bare, "refs", "heads"))
        return st.st_uid == canon.SERVICE_UID or (st.st_gid == canon.SERVICE_GID and bool(st.st_mode & 0o020))
    if exchange is _rename_it_away:
        with pytest.raises(FileNotFoundError):            # control: one look cannot answer at all
            one_look()
    else:
        assert one_look() is False, "control: one look at refs/heads is talked out of it"
    assert canon.service_writable(r.bare) is True
    assert canon.git_kwargs(r.bare).get("user") == canon.SERVICE_UID


def _one_file_of_the_services(bare):
    path = os.path.join(bare, "objects", "info", "left-by-the-service")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").close()
    os.chown(path, canon.SERVICE_UID, 0)
    os.chmod(path, 0o600)


def _one_directory_its_group_may_write(bare):
    path = os.path.join(bare, "objects", "pack")
    os.makedirs(path, exist_ok=True)
    os.chown(path, 0, canon.SERVICE_GID)
    os.chmod(path, 0o775)


def _one_directory_anybody_may_write(bare):
    os.chmod(os.path.join(bare, "refs", "heads"), 0o757)


def _one_link(bare):
    os.symlink("/nonexistent", os.path.join(bare, "info", "a-link"))


def _only_the_top_directory(bare):
    os.chown(bare, 0, canon.SERVICE_GID)
    os.chmod(bare, 0o2775)


@pytest.mark.parametrize("one_thing", [_one_file_of_the_services, _one_directory_its_group_may_write,
                                       _one_directory_anybody_may_write, _one_link, _only_the_top_directory],
                         ids=["a-file-it-owns", "a-directory-its-group-may-write", "a-directory-anybody-may-write",
                              "a-link", "only-the-top-directory"])
def test_one_thing_of_the_services_anywhere_below_a_closed_top_is_enough(world, one_thing):
    """A repository whose top directory and top-level entries are root's, closed — and ONE thing deep inside it
    that the service owns, or its group may write, or anybody may, or a symbolic link: `service_writable`
    answers True and root's git is the service. Without that one thing it answers False (the control). The
    last case is the other way round — the top directory alone is the service group's to write, and nothing
    in it: True as well, since whoever may write a directory may replace every entry of it.

    Guards a layout nobody builds today and somebody will: a top directory closed by hand (`chown root`,
    `chmod 755`) over a store whose packs and refs the service still writes. A rule that looked at the top
    directory alone would call that repository root's own, and the change of identity would be switched off by
    the very hardening meant to add to it."""
    r = _build(world)
    assert canon.service_writable(r.bare) is False, "control"
    one_thing(r.bare)
    assert canon.service_writable(r.bare) is True
    assert canon.git_kwargs(r.bare).get("user") == canon.SERVICE_UID


def test_a_caller_who_is_neither_root_nor_the_service_is_told_to_use_sudo(world, monkeypatch, capsys):
    """With the process's effective uid answered as the operator's (1000): on the play's layout the promote —
    and its `--list` — exit 1 with a sentence that says `sudo`, and no ref is read; on a repository of which
    nothing is the service's, the same caller's `--list` prints what is staged.

    Guards who reaches the canonical's store with their own account. Root is made the service; the service is
    it; a member of the canonical's group who is neither would run git as THEMSELVES in a store a network
    service writes — the seam's environment, and nothing between that store's parsers and the operator's
    account. Their own clone is not the service's to write, and is not asked about. (Whether group members are
    refused or only told is a maintainer's call; this row holds the rule as built.)"""
    promote = _promote()
    laid_out = _as_the_play_leaves_it(_build(world))
    own = _build(world, name="own")
    monkeypatch.setattr(os, "geteuid", lambda: OPERATOR)
    read = []
    real_read_ref, real_proposals = canon.read_ref, canon.proposals
    monkeypatch.setattr(canon, "read_ref", lambda *a, **k: read.append(a) or real_read_ref(*a, **k))
    monkeypatch.setattr(canon, "proposals", lambda *a, **k: read.append(a) or real_proposals(*a, **k))
    for argv in (["--list", "--repo", laid_out.bare],
                 ["%s@%s" % (R.FF, laid_out.ff), "--skip-conflict-check", "--repo", laid_out.bare]):
        assert promote.main(argv) == 1, argv
        assert "sudo" in capsys.readouterr().err
    assert read == [], "the canonical was read for a caller who was to be turned away"
    assert promote.main(["--list", "--repo", own.bare]) == 0
    assert "%s@%s" % (R.FF, own.ff) in capsys.readouterr().out


def test_a_root_that_cannot_change_identity_is_refused_in_one_sentence(world):
    """The promote's `--list`, started as root WITHOUT the capabilities to change uid and gid (`setpriv` takes
    them out of the bounding set, so the new process does not hold them), on the play's layout: exit 1, the
    sentence "could not drop to the service uid", and nothing listed. The control is the same command with its
    capabilities: exit 0, the service's proposal listed.

    Guards what happens where root is not quite root — a container started with those capabilities dropped. The
    change of identity then fails in every child before git starts, so nothing runs as root either way; but
    the caller was told "git could not be run on the canonical" by whichever call came first, and sent looking
    for git."""
    r = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(r)
    argv = [sys.executable, "-B", os.path.join(ROOT, "scripts", "kontroll-promote.py"), "--list", "--repo", r.bare]
    with_them = subprocess.run(argv, capture_output=True, text=True)
    assert with_them.returncode == 0 and "%s@%s" % (STAGED, oid) in with_them.stdout, with_them.stderr
    without = subprocess.run(["setpriv", "--bounding-set", "-setuid,-setgid"] + argv, capture_output=True, text=True)
    assert without.returncode == 1, (without.stdout, without.stderr)
    assert "could not drop to the service uid" in without.stderr and STAGED not in without.stdout
    assert "as the service uid" in with_them.stderr and "as the service uid" not in without.stderr


# --- what the two as-built readers added (2026-10-13) -------------------------------------------------------------- #


def _one_file_anybody_may_write(bare):
    os.chmod(os.path.join(bare, "config"), 0o646)


def _one_file_its_group_may_write(bare):
    path = os.path.join(bare, "packed-refs")
    open(path, "a", encoding="ascii").close()
    os.chown(path, 0, canon.SERVICE_GID)
    os.chmod(path, 0o664)


def _one_link_to_a_file_of_roots(bare):
    os.symlink(os.path.join(bare, "HEAD"), os.path.join(bare, "info", "a-link-to-HEAD"))


@pytest.mark.parametrize("one_thing", [_one_file_anybody_may_write, _one_file_its_group_may_write,
                                       _one_link_to_a_file_of_roots],
                         ids=["a-file-anybody-may-write", "a-file-its-group-may-write", "a-link-to-a-file-of-roots"])
def test_one_file_or_one_live_link_below_a_closed_top_is_enough(world, one_thing):
    """A repository that is root's own, closed — and ONE FILE in it that anybody may write, or that the
    service's group may write, or one symbolic link whose target is a file of root's that exists:
    `service_writable` answers True and root's git is the service. Without it the answer is False (the control).

    Guards the three kinds of entry the rows beside this one do not reach: they open a DIRECTORY to the group
    and to everybody, and their one link points at nothing. A rule that asked the write bits of directories
    only would pass them — and call a store whose `config` or `packed-refs` the service rewrites "root's own";
    so would one that looked THROUGH a link, at a target that is root's, instead of at the link."""
    r = _build(world)
    assert canon.service_writable(r.bare) is False, "control"
    one_thing(r.bare)
    assert canon.service_writable(r.bare) is True
    assert canon.git_kwargs(r.bare).get("user") == canon.SERVICE_UID


def test_the_canonical_reached_by_a_link_is_still_the_canonical(world, capsys):
    """The play's layout, named by a symbolic link that root made beside it: `service_writable` answers True for
    the link, `git_kwargs` carries the service's identity, and the promote's `--list` given the link lists the
    service's proposal and says that git runs as the service.

    Guards the first look of the rule being a look at the DIRECTORY and not at the name it was given: asked of
    the link itself, a link is not a directory, "there is no repository here" is the answer, and root runs git
    as root in the canonical — for nothing more than the path having been spelled another way."""
    r = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(r)
    link = os.path.join(world, "by-another-name.git")
    os.symlink(r.bare, link)
    assert canon.service_writable(link) is True
    assert canon.git_kwargs(link).get("user") == canon.SERVICE_UID
    assert _promote().main(["--list", "--repo", link]) == 0
    said = capsys.readouterr()
    assert "%s@%s" % (STAGED, oid) in said.out and "as the service uid" in said.err


def test_a_root_that_cannot_change_identity_still_reads_a_repository_of_its_own(world):
    """The promote's `--list`, started as root WITHOUT the capabilities to change uid and gid, on a repository
    that is root's own: exit 0, the proposal listed.

    Guards the refusal standing only where the change of identity is needed. A root that cannot become the
    service has no need to in a repository the service writes nothing of; refused there as well, a container
    without those capabilities could not run the suite, nor promote in a clone of its own."""
    own = _build(world, name="own")
    argv = [sys.executable, "-B", os.path.join(ROOT, "scripts", "kontroll-promote.py"), "--list", "--repo", own.bare]
    p = subprocess.run(["setpriv", "--bounding-set", "-setuid,-setgid"] + argv, capture_output=True, text=True)
    assert p.returncode == 0 and "%s@%s" % (R.FF, own.ff) in p.stdout, (p.stdout, p.stderr)


def test_the_caller_rule_is_asked_of_the_git_directory_not_of_the_checkout(world, monkeypatch, capsys):
    """A clone that is its maker's own, with a file anybody may write in its WORK TREE (and nothing of the kind
    under `.git`): a caller who is neither root nor the service is not turned away — `--list --repo <clone>`
    prints the proposals the clone fetched.

    Guards which directory the caller rule is asked of. Git is started in `<clone>/.git`; a scratch file, a
    build output or a link in the checkout beside it is no part of the store git reads, and asked of the
    checkout, the rule sent a developer to `sudo` for a clone of their own."""
    own = _build(world, name="own")
    clone = os.path.join(world, "clone")
    subprocess.run(["git", "clone", "-q", own.bare, clone], check=True, capture_output=True)
    R.git(clone, "fetch", "-q", "origin", "refs/heads/proposed/*:refs/heads/proposed/*")
    scratch = os.path.join(clone, "scratch")
    open(scratch, "w", encoding="utf-8").close()
    os.chmod(scratch, 0o666)
    assert canon.service_writable(os.path.join(clone, ".git")) is False, "control"
    monkeypatch.setattr(os, "geteuid", lambda: OPERATOR)
    assert _promote().main(["--list", "--repo", clone]) == 0
    assert "%s@%s" % (R.FF, own.ff) in capsys.readouterr().out


def test_a_promote_by_root_works_from_a_directory_the_service_may_not_enter(world, monkeypatch):
    """With the process's working directory one that only root may enter (mode 0700 — an operator's home, root's
    own): on the play's layout the listing lists the service's proposal, the promote lands it and a discard
    removes another, all as root.

    Guards where `sudo kontroll-promote` is typed. The child keeps the caller's working directory and gives up
    the caller's identity; git takes that directory for a work tree (the seam sets GIT_COMMON_DIR) and
    `rev-list <id>` asks whether `<id>` is also a file there — as the service uid, in a directory it may not
    search, that question is refused and the promote ended "the commits this promote would land could not be
    listed", from `~` and from `/root`, where the build before promoted."""
    r = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(r)
    closed = os.path.join(world, "home-of-somebody")
    os.mkdir(closed, 0o700)
    monkeypatch.chdir(closed)
    assert subprocess.run(["sh", "-c", "test -x ."], **SERVICE).returncode != 0, "control: the service may not enter"
    listed = pending.list_pending(r.bare)
    assert STAGED in [p["run_id"] for p in listed["proposals"]], listed.get("note")
    assert _promote().main(["%s@%s" % (STAGED, oid), "--skip-conflict-check", "--repo", r.bare]) == 0
    assert canon.read_ref(r.bare, canon.MAIN)[0] == oid
    assert set(canon.read_tree_verified(r.bare, oid)) == {b"seed.txt", b"adv.txt", b"ff.txt"}
    assert discard.discard_proposal(R.STALE, canonical=r.bare)["ok"] is True


def test_what_a_promote_by_root_leaves_is_the_services_and_writable_for_the_canonicals_group(world):
    """On the play's layout, after a promote and a discard run by root: every file and directory they made or
    rewrote is the service's (uid and gid 1001) and writable for its group — directories with the setgid bit.
    Then the OPERATOR (uid 1000), as a member of that group, moves `main` — which writes to what the promote
    left — and the same operator WITHOUT the group is refused (the control: it is the group that lets them, not
    their owning the directory).

    Guards the next writer. A promote leaves more than a ref — files that the operator's next write of `main`
    has to write to as well, and that are the service's now. Left without the group's write bit (a child started
    with a closed umask, `core.sharedRepository` lost) they lock the operator's next push out of the canonical;
    and writing ANOTHER ref beside them proves nothing, since the operator owns that directory."""
    r = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(r)
    before = {}
    for here, dirs, files in os.walk(r.bare):
        for name in dirs + files:
            st = os.lstat(os.path.join(here, name))
            before[os.path.join(here, name)] = (st.st_ino, st.st_mtime_ns)
    assert _promote().main(["%s@%s" % (STAGED, oid), "--skip-conflict-check", "--repo", r.bare]) == 0
    assert discard.discard_proposal(R.STALE, canonical=r.bare)["ok"] is True
    made = []
    for here, dirs, files in os.walk(r.bare):
        for name in dirs + files:
            path = os.path.join(here, name)
            st = os.lstat(path)
            if before.get(path, (None, None))[0] != st.st_ino:
                made.append(os.path.relpath(path, r.bare))
                assert (st.st_uid, st.st_gid) == (1001, 1001), (path, st.st_uid, st.st_gid)
                want = 0o2770 if stat.S_ISDIR(st.st_mode) else 0o660
                assert stat.S_IMODE(st.st_mode) & want == want, (path, oct(st.st_mode))
    assert "refs/heads/main" in made and os.path.join("logs", "refs", "heads", "main") in made, made

    def as_the_operator(groups):
        return subprocess.run(["git", "update-ref", canon.MAIN, r.main, oid], capture_output=True,
                              env=canon.git_env(r.bare), user=OPERATOR, group=OPERATOR, extra_groups=groups)
    assert as_the_operator([]).returncode != 0, "control: without the group the operator cannot append to it"
    moved = as_the_operator([canon.SERVICE_GID])
    assert moved.returncode == 0, moved.stderr
    assert canon.read_ref(r.bare, canon.MAIN)[0] == r.main


def test_the_identity_git_runs_under_is_the_services_uid_gid_and_no_other_group(world, monkeypatch):
    """On the play's layout, a hook the service planted writes down `id -u`, `id -g` and `id -G` during a promote
    by root (the seam's own answer to hooks taken out by this row): every line is `1001:1001:1001` — the
    numbers, not the module's constants. In a repository that is root's own it is `0:0:0`.

    Guards the two thirds of an identity that `id -u` does not show. A child that is uid 1001 with root's GROUP,
    or with root's supplementary groups, still writes whatever those groups may write — and the rows that build
    their layout and their expectations from `canon.SERVICE_GID` agree with whatever that constant says."""
    for own in (False, True):
        home = os.path.join(world, "own" if own else "laid-out")
        os.mkdir(home, 0o755)
        r = _build(home) if own else _as_the_play_leaves_it(_build(home))
        canary = _canary(home)
        script = "#!/bin/sh\necho \"$(id -u):$(id -g):$(id -G)\" >> '%s'\n" % canary
        hooks = os.path.join(home, "named")
        os.mkdir(hooks, 0o755)
        with open(os.path.join(hooks, "reference-transaction"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        os.chmod(os.path.join(hooks, "reference-transaction"), 0o755)
        if own:
            R.git(r.bare, "config", "core.hooksPath", hooks)
        else:
            _service(r.bare, "config", "core.hooksPath", hooks)
        with monkeypatch.context() as inner:
            _seam_minus(inner, "core.hooksPath")
            assert _promote().main(["%s@%s" % (R.FF, r.ff), "--skip-conflict-check", "--repo", r.bare]) == 0
        with open(canary, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        assert lines and set(lines) == {"0:0:0" if own else "1001:1001:1001"}, lines


def _interpreter_env(home):
    """What a child needs in order to BE this interpreter: where programs are, a home, and the paths a relocated
    Python finds itself by (a runner's tool cache). Paths, nothing else of the caller's environment."""
    env = {"PATH": os.environ["PATH"], "HOME": home}
    for name in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "pythonLocation"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _a_third_account(home):
    """A uid that is neither root nor the service and CAN start the interpreter this suite runs under: the
    operator's (1000) where that works, else the account the interpreter belongs to (a runner's tool cache lives
    under the runner's own home, which nobody else may enter)."""
    for uid in (OPERATOR, os.lstat(sys.executable).st_uid, os.stat(ROOT).st_uid):
        if uid in (0, canon.SERVICE_UID):
            continue
        try:
            started = subprocess.run([sys.executable, "-c", "pass"], capture_output=True, env=_interpreter_env(home),
                                     cwd=home, user=uid, group=uid, extra_groups=[])
        except OSError:
            continue
        if started.returncode == 0:
            return uid
    pytest.fail("no account that is neither root nor the service can start %s — this row needs one" % sys.executable)


def test_a_member_of_the_canonicals_group_under_its_own_uid_is_turned_away_and_starts_no_git(world):
    """The promote's `--list`, started as a REAL third account — neither root nor the service, with the
    canonical's group (1001) as a supplementary group, from a copy of the tool that account can read — on the
    play's layout: exit 1, the sentence that says `sudo`, nothing listed. The control is the same account on a
    repository of which nothing is the service's: exit 0, the proposals listed.

    Guards what a stubbed uid cannot show: the row that answers `os.geteuid` as 1000 inside a process that is
    still root holds the comparison, not the account — a rule that let a member of the group through (asked of
    `os.getgroups`) passes it, because root's groups are root's. This one is the account the rule is about."""
    tool = os.path.join(world, "tool")
    shutil.copytree(os.path.join(ROOT, "scripts", "kontroll"), os.path.join(tool, "kontroll"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(os.path.join(ROOT, "scripts", "kontroll-promote.py"), tool)
    laid_out = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(laid_out)
    own = _build(world, name="own")

    third = _a_third_account(world)

    def a_member(repo):
        return subprocess.run([sys.executable, "-B", os.path.join(tool, "kontroll-promote.py"), "--list", "--repo",
                               repo], capture_output=True, text=True, cwd=world, env=_interpreter_env(world),
                              user=third, group=third, extra_groups=[canon.SERVICE_GID])
    away = a_member(laid_out.bare)
    assert away.returncode == 1 and "sudo" in away.stderr and oid not in away.stdout, (away.stdout, away.stderr)
    home = a_member(own.bare)
    assert home.returncode == 0 and "%s@%s" % (R.FF, own.ff) in home.stdout, home.stderr


def test_one_file_of_the_services_under_hooks_is_enough_too(world):
    """A repository that is root's own, closed, with ONE file under `hooks/` that the service owns:
    `service_writable` answers True. Without it, False (the control).

    Guards "every entry" meaning every entry: `hooks/` is the one directory the play prunes from its grant, and
    a walk that pruned it the same way would not see what somebody left there."""
    r = _build(world)
    assert canon.service_writable(r.bare) is False, "control"
    path = os.path.join(r.bare, "hooks", "left-by-the-service")
    open(path, "w", encoding="utf-8").close()
    os.chown(path, canon.SERVICE_UID, 0)
    os.chmod(path, 0o600)
    assert canon.service_writable(r.bare) is True


def test_a_repository_named_relative_to_where_root_stands_is_still_found(world, monkeypatch, capsys):
    """Root stands in the directory that holds the canonical and names it by its bare name (`--repo
    canonical.git`): the promote's `--list` lists the service's proposal, and a promote of it lands.

    Guards the service's git being started somewhere else than where the caller stands: it is started in `/`,
    and a repository handed to it as the caller typed it — relative to a directory the child is no longer in —
    would not be found."""
    r = _as_the_play_leaves_it(_build(world))
    oid = _stage_as_the_service(r)
    monkeypatch.chdir(world)
    relative = os.path.basename(r.bare)
    assert _promote().main(["--list", "--repo", relative]) == 0
    assert "%s@%s" % (STAGED, oid) in capsys.readouterr().out
    assert _promote().main(["%s@%s" % (STAGED, oid), "--skip-conflict-check", "--repo", relative]) == 0
    assert canon.read_ref(r.bare, canon.MAIN)[0] == oid


def test_the_audits_one_git_is_the_service_when_root_audits_the_canonical(world, monkeypatch):
    """`scripts/kontroll-canonical.py --audit`, run by root with a `git` first on the path that writes down the
    uid it runs as before it becomes the real one: on the play's layout that file holds `1001`, once; on a
    repository that is root's own (the control) it holds `0`.

    Guards the one process the audit starts. It parses the canonical's `config` — a file the network service
    writes — with git's own parser; the audit hands it the bytes and not the repository, but whatever those
    bytes can make a git do, they would make it do as root in the installer container, on every deploy."""
    spec = importlib.util.spec_from_file_location("kontroll_canonical_drop",
                                                  os.path.join(ROOT, "scripts", "kontroll-canonical.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    canary = _canary(world)
    shim = os.path.join(world, "bin")
    os.mkdir(shim, 0o755)
    with open(os.path.join(shim, "git"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/sh\nid -u >> '%s'\nexec '%s' \"$@\"\n" % (canary, shutil.which("git")))
    os.chmod(os.path.join(shim, "git"), 0o755)
    laid_out = _as_the_play_leaves_it(_build(world))
    own = _build(world, name="own")
    monkeypatch.setenv("PATH", shim + os.pathsep + os.environ["PATH"])
    assert "config-unread: config" not in tool.audit(laid_out.bare).lines()
    assert _lines(canary) == [str(canon.SERVICE_UID)]
    open(canary, "w", encoding="utf-8").close()
    assert "config-unread: config" not in tool.audit(own.bare).lines()
    assert _lines(canary) == ["0"]
