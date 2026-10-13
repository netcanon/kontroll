"""canon.read_tree_verified — one commit's tree, read as it is (C10).

The promote gate decides whether a proposal may become `main` by looking at the proposal's files. If what it looks
at can differ from what the commit holds, the gate judges one tree and the fast-forward lands another. These tests
pin the reader that closes that gap: one `git cat-file --batch`, every object re-hashed against the id it was asked
for, trees parsed from their own bytes, regular files only.

Each test plants, with real git (tests/unit/_canon_repo.py), one of the things that makes an EXPORT of a commit —
or git's own answer about an object — differ from the commit: an attribute, a filter, a replace ref, a
commit-graph, an object file swapped under its name, an entry no checkout could take. Where plain git is fooled,
the test shows that first (a control). The reader then either returns the committed bytes or refuses with a
sentence that names the rule and nothing of the commit.
"""
import importlib.util
import os
import subprocess
import sys
import time
import zlib

import pytest

import _canon_repo as R
from kontroll import canon

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPORT = os.path.join(ROOT, "scripts", "kontroll-export.py")

FILES = {b"modules/_core.yml": b"collections: []\n", b"modules/alpha/module.yml": b"key: alpha\n",
         b"instance/fleet.yml": b"enabled_modules: [alpha]\n", b"seed.txt": b"base\n"}


def _commit(r, files, parent=None):
    return R.commit(r.bare, R.snapshot(r.bare, files), [parent or r.main])


def test_a_commit_reads_as_the_files_it_holds(tmp_path):
    """Every regular file of a commit comes back under its full path with the bytes that were committed — nested
    directories, an executable file, an empty file, a name that is not ASCII — and nothing else does. The baseline:
    what the gate judges for an ordinary proposal is the proposal."""
    r = R.build(tmp_path)
    exe = R.blob(r.bare, b"#!/bin/sh\n")
    sub = R.raw_tree(r.bare, [(b"100755", b"run.sh", exe), (b"100644", b"empty", R.blob(r.bare, b""))])
    top = R.raw_tree(r.bare, [(b"100644", "café.yml".encode("utf-8"), R.blob(r.bare, b"k: v\n")),
                              (b"40000", b"bin", sub)])
    got = canon.read_tree_verified(r.bare, R.commit(r.bare, top, [r.main]))
    assert got == {"café.yml".encode("utf-8"): b"k: v\n", b"bin/run.sh": b"#!/bin/sh\n", b"bin/empty": b""}
    assert canon.read_tree_verified(r.bare, _commit(r, FILES)) == FILES


def test_attributes_and_filters_do_not_decide_what_is_read(tmp_path):
    """A commit whose `.gitattributes` says `instance export-ignore`, `modules export-ignore`, `seed.txt
    export-subst` and `* filter=x`, in a repository whose `info/attributes` hides more and whose config names a
    program for filter `x`: the reader returns EVERY path with its committed bytes and the program never runs.
    Control: `git archive` of the same commit has no `instance/` and no `modules/`. Guards the read the gate used
    to make — it judged the archive, so a proposal chose what its judge saw (and a filter in the repository's
    config ran as whoever promoted)."""
    r = R.build(tmp_path)
    canary = tmp_path / "ran"
    files = dict(FILES)
    files[b".gitattributes"] = (b"instance export-ignore\nmodules export-ignore\nseed.txt export-subst\n"
                                b"* filter=x\n")
    files[b"seed.txt"] = b"id: $Format:%H$\n"
    commit = _commit(r, files)
    os.makedirs(os.path.join(r.bare, "info"), exist_ok=True)
    with open(os.path.join(r.bare, "info", "attributes"), "w", encoding="ascii", newline="\n") as fh:
        fh.write("seed.txt export-ignore\n")
    prog = "sh -c 'echo ran >> %s; cat'" % str(canary).replace("\\", "/")
    R.git(r.bare, "config", "filter.x.smudge", prog)
    R.git(r.bare, "config", "filter.x.clean", prog)

    archive = subprocess.run(["git", "-C", r.bare, "archive", "--format=tar", commit], capture_output=True)
    archived = archive.stdout
    assert archive.returncode == 0 and b".gitattributes" in archived, "control: the export was expected to succeed"
    assert b"modules/alpha/module.yml" not in archived and b"instance/fleet.yml" not in archived, \
        "control: the export was expected to leave the ignored paths out"
    if canary.exists():
        canary.unlink()                                   # the control's own filter run, if this git made one

    assert canon.read_tree_verified(r.bare, commit) == files
    assert not canary.exists(), "a filter of the repository ran while the commit was read"


def test_a_replace_ref_does_not_stand_in_for_a_file(tmp_path):
    """With a replace ref for a blob of the commit, the reader returns the committed bytes. Control: plain
    `git cat-file` returns the replacement. Guards the seam on this read — and that the hash check alone would
    refuse it too."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    real = R.blob(r.bare, b"key: alpha\n")
    R.set_ref(r.bare, "refs/replace/" + real, R.blob(r.bare, b"key: EVIL\n"))
    assert R.raw(r.bare, "blob", real) == b"key: EVIL\n", "control: plain git was expected to follow the replace ref"
    assert canon.read_tree_verified(r.bare, commit)[b"modules/alpha/module.yml"] == b"key: alpha\n"


def test_without_the_seams_entry_a_replace_ref_is_refused_by_the_hash(tmp_path, monkeypatch):
    """With `GIT_NO_REPLACE_OBJECTS` taken OUT of the environment the reader runs git in, a replace ref for a blob
    of the commit makes the reader REFUSE ("not what its name says") — it does not return the replacement. Guards
    the second half of the claim the row above makes in words only: the re-hash is a defence of its own, so a seam
    that lost that entry would turn a read into a refusal and never into another file."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    real = R.blob(r.bare, b"key: alpha\n")
    R.set_ref(r.bare, "refs/replace/" + real, R.blob(r.bare, b"key: EVIL\n"))
    whole = canon.git_env

    def without(repo):
        env = whole(repo)
        del env["GIT_NO_REPLACE_OBJECTS"]
        return env
    monkeypatch.setattr(canon, "git_env", without)
    with pytest.raises(canon.TreeRefused, match="not what its name says"):
        canon.read_tree_verified(r.bare, commit)


def test_a_lying_commit_graph_does_not_decide_the_tree(tmp_path):
    """With a commit-graph that gives the commit another commit's root tree, the reader returns the tree the
    commit's own bytes name. Control: plain `git diff` against the parent lists the other tree's files. Guards
    where the root tree's id comes from: the commit object, re-hashed — never git's parsed view of it."""
    r = R.build(tmp_path)
    R.plant_lying_commit_graph(r, tree_of=(r.ff, R.git(r.bare, "rev-parse", r.stale + "^{tree}").decode()))
    assert R.git(r.bare, "diff", "--name-status", "-z", r.main, r.ff, "--") != b"A\0ff.txt\0", \
        "control: plain git was expected to take the tree from the graph"
    assert sorted(canon.read_tree_verified(r.bare, r.ff)) == [b"adv.txt", b"ff.txt", b"seed.txt"]


@pytest.mark.parametrize("which", ["blob", "tree", "commit"])
def test_an_object_swapped_under_its_name_is_refused(tmp_path, which):
    """An object FILE replaced by a well-formed object that holds other content — a blob of the commit, its root
    tree, the commit itself — makes the reader refuse with "not what its name says". Control (for the blob): plain
    `git cat-file` returns the substitute and exits 0, because git does not re-hash a loose object it reads.
    Guards the three hash checks one at a time: without the blob's, a file is judged that is not the one
    committed; without the tree's, a whole other directory; without the commit's, another commit's tree — and in
    each case the writer can put the real object back afterwards, so what was judged is not what is promoted."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    if which == "blob":
        target = R.blob(r.bare, b"key: alpha\n")
        R.swap_loose(r.bare, target, b"blob", b"key: EVIL\n")
        assert R.raw(r.bare, "blob", target) == b"key: EVIL\n", "control: plain git was expected to return it"
    elif which == "tree":
        target = R.git(r.bare, "rev-parse", commit + "^{tree}").decode()
        R.swap_loose(r.bare, target, b"tree", R.raw(r.bare, "tree", R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode()))
    else:
        R.swap_loose(r.bare, commit, b"commit", R.raw(r.bare, "commit", r.ff))
    with pytest.raises(canon.TreeRefused, match="not what its name says"):
        canon.read_tree_verified(r.bare, commit)


def test_a_sha256_repository_is_read_with_its_own_hash(tmp_path):
    """In a repository whose object format is sha256 the reader returns a commit's files (ids are sixty-four
    digits, a tree's raw ids thirty-two bytes), and still refuses a blob swapped under its name. Guards the two
    places the id's LENGTH decides something — which digest re-hashes an object, and how many bytes of a tree
    record are the id: with either fixed at sha1's, every commit of such a repository is refused (or, worse for
    the second, a tree is cut at the wrong byte)."""
    bare = str(tmp_path / "s256.git")
    made = subprocess.run(["git", "init", "-q", "--bare", "--object-format=sha256", "-b", "main", bare], capture_output=True)
    if made.returncode != 0:
        pytest.skip("this git cannot make a sha256 repository")
    commit = R.commit(bare, R.snapshot(bare, FILES), [])
    assert len(commit) == 64 and canon.read_tree_verified(bare, commit) == FILES
    R.swap_loose(bare, R.blob(bare, b"key: alpha\n"), b"blob", b"key: EVIL!\n")
    with pytest.raises(canon.TreeRefused, match="not what its name says"):
        canon.read_tree_verified(bare, commit)


def test_a_commit_whose_first_line_is_not_its_tree_line_is_refused(tmp_path):
    """A commit object whose first line is not `tree <id>` — here `eert <the id of a real tree>` — is refused:
    "does not name a tree". Guards where the root tree's id is taken from: the FIRST LINE of the commit's own
    bytes, and only when that line is the tree line. A reader that took whatever stood at that offset would walk
    a tree for a commit git itself cannot parse."""
    r = R.build(tmp_path)
    tree = R.git(r.bare, "rev-parse", r.ff + "^{tree}")
    body = (b"eert " + tree + b"\nauthor t <t@example.test> 1750000000 +0000\n"
            b"committer t <t@example.test> 1750000000 +0000\n\nnot a commit git can parse\n")
    odd = R.git(r.bare, "hash-object", "-t", "commit", "-w", "--stdin", "--literally", stdin=body).decode("ascii")
    with pytest.raises(canon.TreeRefused, match="does not name a tree"):
        canon.read_tree_verified(r.bare, odd)


def _entries(r):
    b, sub = R.blob(r.bare, b"x\n"), R.tree(r.bare, {b"x": R.blob(r.bare, b"x\n")})
    return {
        "a symbolic link": ([(b"120000", b"link.yml", b)], "not a regular file or a directory"),
        "a submodule entry": ([(b"160000", b"sub", r.base)], "not a regular file or a directory"),
        "a mode git does not write": ([(b"100664", b"a", b)], "not a regular file or a directory"),
        "a .git entry": ([(b"100644", b".git", b)], "no checkout could take"),
        "a .GIT entry": ([(b"40000", b".GIT", sub)], "no checkout could take"),
        "a name with a newline": ([(b"100644", b"a\nb", b)], "no checkout could take"),
        "a name with a slash": ([(b"100644", b"a/b", b)], "no checkout could take"),
        "a name with a backslash": ([(b"100644", b"a\\b", b)], "no checkout could take"),
        "the name dot-dot": ([(b"40000", b"..", sub)], "no checkout could take"),
        "the name dot": ([(b"100644", b".", b)], "no checkout could take"),
        "an empty name": ([(b"100644", b"", b)], "no checkout could take"),
        "one name twice": ([(b"100644", b"a", b), (b"100644", b"a", b)], "no checkout could take"),
        "a file and a directory of one name": ([(b"100644", b"a", b), (b"40000", b"a", sub)], "no checkout could take"),
        "a directory entry that names a blob": ([(b"40000", b"a", b)], "not the kind"),
        "a file entry that names a tree": ([(b"100644", b"a", sub)], "not the kind"),
        "an object that is not there": ([(b"100644", b"a", "ab" * 20)], "is missing"),
    }


@pytest.mark.parametrize("label", ["a symbolic link", "a submodule entry", "a mode git does not write", "a .git entry",
                                   "a .GIT entry", "a name with a newline", "a name with a slash",
                                   "a name with a backslash", "the name dot-dot", "the name dot", "an empty name",
                                   "one name twice", "a file and a directory of one name",
                                   "a directory entry that names a blob", "a file entry that names a tree",
                                   "an object that is not there"])
def test_an_entry_no_checkout_could_take_refuses_the_whole_tree(tmp_path, label):
    """A tree that holds — one level down, beside ordinary files — a symbolic link, a submodule entry, a `.git`
    entry, a name with a separator or a control byte, `.` or `..`, one name twice, an entry whose object is of
    the wrong kind or absent, is REFUSED as a whole, with a sentence that names the rule and holds none of the
    entry's bytes. Guards "refuse, do not skip": a reader that skipped the link would judge a tree without it and
    the fast-forward would land it — and a link is how a tree makes a later reader open a file outside the tree."""
    r = R.build(tmp_path)
    entries, rule = _entries(r)[label]
    bad = R.raw_tree(r.bare, entries)
    top = R.raw_tree(r.bare, [(b"40000", b"modules", bad), (b"100644", b"seed.txt", R.blob(r.bare, b"base\n"))])
    with pytest.raises(canon.TreeRefused, match=rule) as ei:
        canon.read_tree_verified(r.bare, R.commit(r.bare, top, [r.main]))
    assert "link.yml" not in str(ei.value) and "seed.txt" not in str(ei.value)


def _gits_own_checkout_refuses(bare, tree_id, tmp_path):
    """Does git itself refuse to read `tree_id` into an index (the first step of any checkout)? A CONTROL."""
    env = dict(os.environ, GIT_DIR=bare, GIT_INDEX_FILE=str(tmp_path / "scratch-index"))
    return subprocess.run(["git", "read-tree", tree_id], capture_output=True, env=env).returncode != 0


@pytest.mark.parametrize("name, git_refuses_it", [
    (b".git ", True), (b".git.", True), (b".git. . ", True), (b"git~1", True), (b"GIT~1", True),
    (b".git::$INDEX_ALLOCATION", True), (b".GiT:stream", True),
    (b".g\xe2\x80\x8cit", False),                     # .g<U+200C>it — `.git` on HFS+ (git refuses it only on a Mac)
    (b"n" * 256, False),                               # one byte more than a file system takes: git reads it, cannot write it
], ids=["dot-git-space", "dot-git-dot", "dot-git-dots-and-spaces", "git-tilde-1", "GIT-tilde-1", "dot-git-stream",
        "dot-git-stream-mixed-case", "dot-git-with-an-ignored-character", "a-name-of-256-bytes"])
def test_a_name_gits_own_checkout_refuses_is_refused_by_the_reader(tmp_path, name, git_refuses_it):
    """A tree with an entry git's own checkout refuses on every platform — `.git` or `git~1` with trailing dots
    and spaces or a `:stream` after it (the names NTFS opens as `.git`; `core.protectNTFS` is on by default
    everywhere) — or `.git` with a character HFS+ ignores, or a name longer than a file system takes, is REFUSED,
    as a tree and not as a gate that could not run. Control (for the NTFS spellings): `git read-tree` of the same
    tree fails. Guards the gap between the reader's rule and git's: a proposal with one such entry and nothing
    else wrong passed the gate, and a `main` that holds it is a `main` no clone can check out — the next deploy
    stops, which is the outcome the gate exists to refuse. (The long name was refused before too, but as "the
    gate could not run", with the override offered — a sentence the proposal chose.)"""
    r = R.build(tmp_path)
    sub = R.tree(r.bare, {b"x": R.blob(r.bare, b"x\n")})
    top = R.raw_tree(r.bare, [(b"40000", name, sub), (b"100644", b"seed.txt", R.blob(r.bare, b"base\n"))])
    if git_refuses_it:
        assert _gits_own_checkout_refuses(r.bare, top, tmp_path), "control: git was expected to refuse this tree"
    with pytest.raises(canon.TreeRefused, match="no checkout could take"):
        canon.read_tree_verified(r.bare, R.commit(r.bare, top, [r.main]))


@pytest.mark.parametrize("name", [b".gitattributes", b".gitignore", b".github", b".git-keep", b"git~2", b"agit~1",
                                  b"n" * 255, b"a:b", b"a.", b"a "])
def test_the_names_beside_those_are_still_read(tmp_path, name):
    """`.gitattributes`, `.gitignore`, `.github`, `git~2`, a name of exactly 255 bytes, a name with a colon or a
    trailing dot are READ: git checks each of them out on the node. Guards the rule above against swallowing its
    neighbours — a reader that refused `.gitattributes` would refuse every tree this tool ships."""
    r = R.build(tmp_path)
    top = R.raw_tree(r.bare, [(b"100644", name, R.blob(r.bare, b"x\n"))])
    assert canon.read_tree_verified(r.bare, R.commit(r.bare, top, [r.main])) == {name: b"x\n"}


@pytest.mark.parametrize("label, entry", [
    ("a directory mode with a leading zero", (b"040000", b"d", "tree")),
    ("a file mode with a leading zero", (b"0100644", b"a", "blob")),
    ("a name with DEL in it", (b"100644", b"a\x7fb", "blob")),
])
def test_a_mode_or_a_name_git_does_not_write_is_refused(tmp_path, label, entry):
    """A tree entry whose mode is spelled with a leading zero (`040000`, `0100644` — git reads both and writes
    neither) and a name with the DEL byte in it refuse the whole tree. Guards the reader's two closed lists — the
    three modes it takes, the bytes a name may not hold — against being widened one harmless-looking spelling at
    a time: each spelling the reader takes and another reader of the same tree does not is a way to make the two
    see different trees."""
    r = R.build(tmp_path)
    mode, name, kind = entry
    target = R.blob(r.bare, b"x\n") if kind == "blob" else R.tree(r.bare, {b"x": R.blob(r.bare, b"x\n")})
    top = R.raw_tree(r.bare, [(mode, name, target), (b"100644", b"seed.txt", R.blob(r.bare, b"base\n"))])
    with pytest.raises(canon.TreeRefused, match="not a regular file or a directory|no checkout could take"):
        canon.read_tree_verified(r.bare, R.commit(r.bare, top, [r.main]))


def test_the_caps_refuse_before_the_reader_holds_more(tmp_path):
    """A tree with more entries than `max_entries`, or more bytes than `max_bytes`, is refused; the same tree
    within both caps is read. The bytes cap is applied to the size git ANNOUNCES for an object, before its body is
    read. Guards the reader against a commit made to be large: it is run by the promoter on a commit somebody else
    wrote."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    assert len(canon.read_tree_verified(r.bare, commit)) == len(FILES)
    with pytest.raises(canon.TreeRefused, match="more entries"):
        canon.read_tree_verified(r.bare, commit, max_entries=3)
    with pytest.raises(canon.TreeRefused, match="larger than"):
        canon.read_tree_verified(r.bare, commit, max_bytes=200)
    big = _commit(r, {b"big.bin": b"x" * 5000})
    with pytest.raises(canon.TreeRefused, match="larger than"):
        canon.read_tree_verified(r.bare, big, max_bytes=4000)


def test_the_caps_count_every_object_and_every_entry_and_take_exactly_the_budget(tmp_path):
    """A commit whose objects' sizes sum to N bytes and whose trees hold E entries is read with `max_bytes=N` and
    `max_entries=E`, and refused with one less of either; the bytes of the commit and of the trees count (a
    budget of the blobs' bytes alone is refused), and a directory counts as an entry (a tree of nothing but
    directories, one more than the cap, is refused). Guards the caps being about what the reader HOLDS and
    WALKS: a cap that counted only files, or only blobs, would let a commit of a hundred thousand nested empty
    directories — no file, no blob — run the reader to its time limit."""
    r = R.build(tmp_path)
    files = {b"modules/_core.yml": b"collections: []\n", b"a.bin": b"z" * 1000}
    commit = _commit(r, files)
    ids = {commit} | {line.split(b" ")[0].decode() for line in
                      R.git(r.bare, "rev-list", "--objects", "--no-walk", commit).split(b"\n")}
    total = sum(int(R.git(r.bare, "cat-file", "-s", i)) for i in ids)
    blobs = sum(len(v) for v in files.values())
    entries = 3                                           # modules/, modules/_core.yml, a.bin
    assert canon.read_tree_verified(r.bare, commit, max_bytes=total, max_entries=entries) == files
    with pytest.raises(canon.TreeRefused, match="larger than"):
        canon.read_tree_verified(r.bare, commit, max_bytes=total - 1)
    with pytest.raises(canon.TreeRefused, match="larger than"):
        canon.read_tree_verified(r.bare, commit, max_bytes=blobs)
    with pytest.raises(canon.TreeRefused, match="more entries"):
        canon.read_tree_verified(r.bare, commit, max_entries=entries - 1)
    empty = R.raw_tree(r.bare, [])
    dirs = R.commit(r.bare, R.raw_tree(r.bare, [(b"40000", b"d%d" % i, empty) for i in range(6)]), [r.main])
    assert canon.read_tree_verified(r.bare, dirs, max_entries=6) == {}
    with pytest.raises(canon.TreeRefused, match="more entries"):
        canon.read_tree_verified(r.bare, dirs, max_entries=5)


def test_the_bytes_cap_is_applied_to_the_size_git_announces_before_a_body_is_read(tmp_path, monkeypatch):
    """A process in git's place that ANNOUNCES an object of a hundred gigabytes and then writes nothing is refused
    at once — "larger than the gate reads" — long before the reader's time limit. Guards the order of two lines:
    with the cap applied after the body is read, the reader would first try to hold whatever a header announces
    (the row above cannot tell the two orders apart: a real git's objects are small either way)."""
    import time
    r = R.build(tmp_path)
    real_popen = canon.subprocess.Popen
    script = ("import sys, time\nsys.stdin.buffer.readline()\n"
              "sys.stdout.buffer.write(b'%s commit 100000000000' + bytes([10]))\nsys.stdout.buffer.flush()\ntime.sleep(60)\n" % r.ff)
    monkeypatch.setattr(canon.subprocess, "Popen", lambda argv, **kw: real_popen([sys.executable, "-c", script], **kw))
    started = time.monotonic()
    with pytest.raises(canon.TreeRefused, match="larger than"):
        canon.read_tree_verified(r.bare, r.ff, timeout=30)
    assert time.monotonic() - started < 20


def test_what_is_read_is_a_commit_named_by_its_full_id(tmp_path):
    """A ref name, a short id, the null id, a tree's id and an id of no object are each refused. Guards the first
    rule of `canon` at this door: the reader is handed the id a ref was READ as — a name here would be looked up
    again, and a tree id would be "judged" as if it were the proposal."""
    r = R.build(tmp_path)
    for not_an_id in ("refs/heads/main", "main", r.ff[:12], "0" * 40, "", None):
        with pytest.raises(canon.TreeRefused, match="full id"):
            canon.read_tree_verified(r.bare, not_an_id)
    with pytest.raises(canon.TreeRefused, match="not the kind"):
        canon.read_tree_verified(r.bare, R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode())
    with pytest.raises(canon.TreeRefused, match="is missing"):
        canon.read_tree_verified(r.bare, "ab" * 20)


def test_a_git_that_cannot_run_or_stops_answering_is_not_a_verdict_on_the_tree(tmp_path, monkeypatch):
    """When git cannot be started, the reader raises `Unreadable` and NOT `TreeRefused`; when the process started
    in its place never answers, the reader ends it after `timeout` seconds and raises `Unreadable` too. Guards the
    difference the gate's two sentences rest on — "the tree is refused" (the proposal's fault) and "the gate could
    not run" (ours) — and that a reader whose git hangs does not hang the promote with it."""
    r = R.build(tmp_path)
    real_popen = canon.subprocess.Popen

    def absent(*a, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(canon.subprocess, "Popen", absent)
    with pytest.raises(canon.Unreadable) as ei:
        canon.read_tree_verified(r.bare, r.ff)
    assert not isinstance(ei.value, canon.TreeRefused)

    def silent(argv, **kw):
        return real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
    monkeypatch.setattr(canon.subprocess, "Popen", silent)
    started = time.monotonic()
    with pytest.raises(canon.Unreadable) as ei:
        canon.read_tree_verified(r.bare, r.ff, timeout=1)
    assert not isinstance(ei.value, canon.TreeRefused) and time.monotonic() - started < 30


def test_a_git_that_ends_in_the_middle_of_an_answer_is_not_a_verdict_on_the_tree(tmp_path, monkeypatch):
    """A process in git's place that announces an object of a thousand bytes, writes ten and ends — and one that
    writes ten and then nothing, ended by the reader after `timeout` — each raise `Unreadable` and NOT
    `TreeRefused`, and no partial body is ever returned. Guards the read of a BODY the way the row above guards
    the read of a header: a reader that took the short body, or called a git that was killed a verdict on the
    proposal, would have the promote say "the tree is refused" about a tree nobody read."""
    r = R.build(tmp_path)
    real_popen = canon.subprocess.Popen
    script = ("import sys, time\nsys.stdin.buffer.readline()\n"
              "sys.stdout.buffer.write(b'%s commit 1000' + bytes([10]) + b'tree 01234')\nsys.stdout.buffer.flush()\n%s\n")
    for then, timeout in (("sys.exit(0)", 30), ("time.sleep(60)", 1)):
        monkeypatch.setattr(canon.subprocess, "Popen",
                            lambda argv, _then=then, **kw: real_popen([sys.executable, "-c", script % (r.ff, _then)], **kw))
        with pytest.raises(canon.Unreadable) as ei:
            canon.read_tree_verified(r.bare, r.ff, timeout=timeout)
        assert not isinstance(ei.value, canon.TreeRefused)


def test_a_git_that_dies_on_one_object_refuses_the_tree_and_a_cut_answer_does_not(tmp_path, monkeypatch):
    """With one blob's object file replaced by a file git cannot read (an object of a kind git does not know: git
    ends with "fatal: invalid object type" in the middle of the walk), the reader raises `TreeRefused` — the
    object store's fault, like a missing object. A process in git's place that ends after HALF a header line
    raises plain `Unreadable`. Guards the fault being decided by who ended git: a writer of the object store
    could turn "the tree is refused" into "the gate could not run" — the sentence that offers the override — by
    damaging one object instead of swapping it."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    target = R.blob(r.bare, b"key: alpha\n")
    path = os.path.join(r.bare, "objects", target[:2], target[2:])
    os.chmod(path, 0o644)
    with open(path, "wb") as fh:
        fh.write(zlib.compress(b"evil 11\0key: alpha\n"))
    ctl = subprocess.run(["git", "-C", r.bare, "cat-file", "--batch"], input=target.encode() + b"\n", capture_output=True)
    assert ctl.returncode != 0 and ctl.stdout == b"", "control: git was expected to die on this object without an answer"
    with pytest.raises(canon.TreeRefused, match="cannot read"):
        canon.read_tree_verified(r.bare, commit)

    real_popen = canon.subprocess.Popen
    cut = ("import sys\nsys.stdin.buffer.readline()\nsys.stdout.buffer.write(b'%s commit 2')\nsys.stdout.buffer.flush()\n" % r.ff)
    monkeypatch.setattr(canon.subprocess, "Popen", lambda argv, **kw: real_popen([sys.executable, "-c", cut], **kw))
    with pytest.raises(canon.Unreadable) as ei:
        canon.read_tree_verified(r.bare, r.ff)
    assert not isinstance(ei.value, canon.TreeRefused)


def test_a_git_that_is_gone_before_it_is_asked_is_unreadable(tmp_path, monkeypatch):
    """A process in git's place that has already ended when the reader writes its first request raises
    `Unreadable` — not a `BrokenPipeError` from the write. Guards the one place the reader WRITES to git: without
    the catch, a git that dies at start-up (a repository format it does not know) is a traceback through the
    promote instead of "the gate could not run"."""
    r = R.build(tmp_path)
    real_popen = canon.subprocess.Popen

    def gone(argv, **kw):
        proc = real_popen([sys.executable, "-c", "pass"], **kw)
        proc.wait()
        return proc
    monkeypatch.setattr(canon.subprocess, "Popen", gone)
    with pytest.raises(canon.Unreadable) as ei:
        canon.read_tree_verified(r.bare, r.ff)
    assert not isinstance(ei.value, canon.TreeRefused)


def test_the_reader_leaves_no_process_and_no_thread_behind(tmp_path, recwarn):
    """After a read that returns, one that refuses a tree and one of a repository git cannot open, no thread of
    the reader is still alive and no `ResourceWarning` was raised for a process nobody waited for. Guards the
    `finally` of the reader: the promote runs it twice and ends, but the same function serves any caller — a
    watchdog left armed is a thread per read for two minutes, and a git that is ended but not waited for is a
    zombie until its parent ends."""
    import gc
    import threading
    import warnings
    r = R.build(tmp_path)
    link = R.commit(r.bare, R.raw_tree(r.bare, [(b"120000", b"l", R.blob(r.bare, b"x"))]), [r.main])
    before = threading.active_count()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        assert canon.read_tree_verified(r.bare, r.ff)
        with pytest.raises(canon.TreeRefused):
            canon.read_tree_verified(r.bare, link)
        with pytest.raises(canon.Unreadable):
            canon.read_tree_verified(str(tmp_path / "no-such-repository"), r.ff)
        gc.collect()
    assert threading.active_count() == before
    assert [str(w.message) for w in recwarn.list if issubclass(w.category, ResourceWarning)] == []


def test_a_repository_with_a_commondir_file_is_not_read(tmp_path):
    """The tree reader goes through the seam like every other git of the canonical: a repository that holds a
    `commondir` file is refused (`Unreadable`), not read through it. Guards the reader being the one git call that
    was written around `git_kwargs`."""
    r, other = R.build(tmp_path), R.build(tmp_path, "planted")
    R.plant_commondir(r, other.bare)
    with pytest.raises(canon.Unreadable, match="commondir"):
        canon.read_tree_verified(r.bare, r.ff)


# ---- writing the files out ------------------------------------------------------------------------------------ #

def test_write_files_puts_each_file_once_and_only_under_the_prefixes(tmp_path):
    """`write_files` writes every path under the destination with its bytes; with prefixes, `modules/` takes the
    directory and `seed.txt` that one file — and `seed.txt.bak`, `modules-old/x` are left out. A file that is
    already there is refused, never overwritten. Guards the two ways an export could hold something the commit
    does not: a prefix that also matched a neighbour, and a path the destination already had."""
    files = dict(FILES)
    files[b"seed.txt.bak"] = b"no\n"
    files[b"modules-old/x.yml"] = b"no\n"
    out = tmp_path / "all"
    out.mkdir()
    assert canon.write_files(files, str(out)) == len(files)
    assert (out / "modules" / "alpha" / "module.yml").read_bytes() == b"key: alpha\n"
    part = tmp_path / "part"
    part.mkdir()
    assert canon.write_files(files, str(part), prefixes=[b"modules/", b"seed.txt"]) == 3
    assert sorted(str(p.relative_to(part)).replace("\\", "/") for p in part.rglob("*") if p.is_file()) == [
        "modules/_core.yml", "modules/alpha/module.yml", "seed.txt"]
    with pytest.raises(canon.TreeRefused, match="takes for one"):
        canon.write_files({b"seed.txt": b"other\n"}, str(part))
    assert (part / "seed.txt").read_bytes() == b"base\n"


def test_write_files_writes_nothing_outside_the_directory_it_is_given(tmp_path):
    """`write_files` refuses a path with an empty, `.` or `..` component — whoever hands it in — and, on Windows,
    a name that is a path on another drive (`Q:x.yml` joined to a directory is `Q:x.yml`). Nothing is created
    outside the destination. Guards the function's own contract ("under the directory `dest`"): the reader hands
    in no such path, but the function is called by more than the reader, and its promise must not rest on its
    callers."""
    dest = tmp_path / "dest"
    dest.mkdir()
    for path in (b"../escaped.txt", b"a/../../escaped.txt", b"/abs.txt", b"a//b", b"./a"):
        with pytest.raises(canon.TreeRefused, match="outside the directory"):
            canon.write_files({path: b"x\n"}, str(dest))
    if os.name == "nt":
        with pytest.raises(canon.TreeRefused, match="outside the directory"):
            canon.write_files({b"modules/Q:x.yml": b"x\n"}, str(dest))
    assert not (tmp_path / "escaped.txt").exists() and [p.name for p in tmp_path.iterdir()] == ["dest"]


def test_a_file_that_cannot_be_written_is_ours_and_not_the_trees(tmp_path):
    """`write_files` into a place where nothing can be created (a path under a regular FILE) raises `Unreadable`
    and not `TreeRefused`. Guards the other half of the gate's two sentences on the way OUT: a full disk or a
    temporary directory that cannot be written is the promoter's condition, and calling it a refusal of the tree
    would send the operator to look for a fault in a proposal that has none."""
    blocker = tmp_path / "a-file"
    blocker.write_bytes(b"")
    with pytest.raises(canon.Unreadable) as ei:
        canon.write_files({b"seed.txt": b"base\n"}, str(blocker / "under"))
    assert not isinstance(ei.value, canon.TreeRefused)


def test_export_commit_writes_the_commit_under_the_prefixes_it_is_given(tmp_path):
    """`export_commit(repo, commit, dest, prefixes)` writes the commit's files under `dest` and returns how many:
    all of them without prefixes, and with `modules/` and one file path only what is under the directory and
    that file. Guards the library's own entry, which the export command no longer goes through (it reads, checks
    its prefixes and writes in steps of its own, so that a prefix that selects nothing is refused before anything
    is written): with nothing else calling it, an `export_commit` that dropped its prefixes wrote the whole
    commit where a caller asked for a part, and no test said so."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    assert canon.export_commit(r.bare, commit, str(tmp_path / "all")) == len(FILES)
    some = tmp_path / "some"
    assert canon.export_commit(r.bare, commit, str(some), [b"modules/", b"seed.txt"]) == 3
    assert sorted(f.relative_to(some).as_posix() for f in some.rglob("*") if f.is_file()) == [
        "modules/_core.yml", "modules/alpha/module.yml", "seed.txt"]


def test_a_path_that_is_not_utf8_is_refused_on_the_way_out(tmp_path):
    """A tree with a path that is not UTF-8 can be READ (the listing shows such a name, escaped) but not written
    out: `write_files` refuses it as the tree's fault. Guards the export against inventing a file name — what the
    generator would then read is not a path of the commit."""
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(canon.TreeRefused, match="not UTF-8"):
        canon.write_files({b"modules/caf\xe9.yml": b"x\n"}, str(out))


def _export(*args):
    return subprocess.run([sys.executable, "-B", EXPORT] + list(args), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def test_the_export_command_writes_the_commit_or_nothing(tmp_path):
    """`kontroll-export.py --repo <git dir> <commit id> <dest>` writes the commit's files and exits 0 — the whole
    tree, or the named prefixes. For a commit whose tree holds a link it exits 2 with the rule and the destination
    does not exist afterwards; a ref name instead of an id, and a destination that is not empty, exit 1 before
    anything is read. Guards the command a play calls in place of `git archive | tar -x`: the export is the
    commit, and a refused commit leaves no half-written tree for the next step to read."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    dest = tmp_path / "out"
    p = _export("--repo", r.bare, commit, str(dest))
    assert p.returncode == 0, p.stderr
    assert (dest / "instance" / "fleet.yml").read_bytes() == b"enabled_modules: [alpha]\n"
    assert sorted(x.name for x in tmp_path.iterdir() if x.name.startswith(".kontroll-export-")) == []

    only = tmp_path / "only"
    assert _export("--repo", r.bare, commit, str(only), "modules/").returncode == 0
    assert sorted(x.name for x in only.iterdir()) == ["modules"]

    link = R.raw_tree(r.bare, [(b"120000", b"link.yml", R.blob(r.bare, b"/etc/passwd")),
                               (b"100644", b"seed.txt", R.blob(r.bare, b"base\n"))])
    refused = tmp_path / "refused"
    p = _export("--repo", r.bare, R.commit(r.bare, link, [r.main]), str(refused))
    assert p.returncode == 2 and "not a regular file" in p.stderr and "link.yml" not in p.stderr
    assert not refused.exists()

    by_name = _export("--repo", r.bare, "refs/heads/main", str(tmp_path / "byname"))
    assert by_name.returncode == 1 and "full id" in by_name.stderr
    again = _export("--repo", r.bare, commit, str(dest))                         # not empty any more
    assert again.returncode == 1 and "not an empty directory" in again.stderr and "Traceback" not in again.stderr
    assert _export("--repo", str(tmp_path / "no-such-repo"), commit, str(tmp_path / "x")).returncode == 1
    assert not (tmp_path / "byname").exists() and not (tmp_path / "x").exists()


def _load_script(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(ROOT, "scripts", filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_export_commands_exit_2_means_a_refused_tree_and_nothing_else(tmp_path):
    """A usage error (no arguments; an option the command does not have) exits 1, not 2; a prefix that selects no
    file of the commit — `modules` written without its slash, `../x`, an empty string — exits 1 with a sentence
    and leaves no directory; and into a destination that exists (an empty directory) the export arrives whole.
    Guards the command's contract with a play: `rc == 2` is "this commit is refused" and a play acts on it, so
    argparse's own 2 must not be it; and "wrote 0 file(s)", exit 0, for a prefix typed without its slash is a
    step that hands the next one an empty directory and calls it the commit."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    assert _export().returncode == 1
    assert _export("--repo", r.bare, commit, str(tmp_path / "o0"), "--no-such-option").returncode == 1
    for n, prefix in enumerate(("modules", "../x", "", "/", "nothing/")):
        dest = tmp_path / ("o%d" % (n + 1))
        p = _export("--repo", r.bare, commit, str(dest), prefix)
        assert p.returncode == 1 and "nothing under one of the prefixes" in p.stderr and not dest.exists(), (prefix, p)
    p = _export("--repo", r.bare, commit, str(tmp_path / "o9"), "modules/", "nothing/")
    assert p.returncode == 1 and not (tmp_path / "o9").exists()
    made = tmp_path / "made-by-the-caller"
    made.mkdir()
    p = _export("--repo", r.bare, commit, str(made), "modules/", "seed.txt")
    assert p.returncode == 0, p.stderr
    assert sorted(x.name for x in made.iterdir()) == ["modules", "seed.txt"]
    assert (made / "modules" / "alpha" / "module.yml").read_bytes() == b"key: alpha\n"


def test_an_export_that_cannot_be_moved_in_leaves_the_destination_as_it_was(tmp_path, monkeypatch, capsys):
    """When the SECOND move into an existing destination fails, the export exits 1 with one sentence — no
    traceback, no name of the commit — and the destination is empty again. Guards "the whole export or nothing"
    where it was not true: the files were moved in one top-level name at a time, and a failure part-way left the
    first names in place for the next step to read as the commit."""
    r = R.build(tmp_path)
    commit = _commit(r, FILES)
    export = _load_script("kontroll_export_f", "kontroll-export.py")
    dest = tmp_path / "dest"
    dest.mkdir()
    real_rename, calls = os.rename, []

    def second_fails(a, b):
        calls.append(a)
        if len(calls) == 2:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(a, b)
    monkeypatch.setattr(export.os, "rename", second_fails)
    assert export.main(["--repo", r.bare, commit, str(dest)]) == 1
    monkeypatch.setattr(export.os, "rename", real_rename)
    err = capsys.readouterr().err
    assert "could not be moved" in err and "modules" not in err and "seed.txt" not in err
    assert list(dest.iterdir()) == [] and len(calls) == 2


def test_the_export_command_takes_no_destination_that_is_not_its_to_fill(tmp_path):
    """The export refuses (exit 1, nothing read, no traceback) a destination that is a regular file, one that is a
    symbolic link to an empty directory, and one whose parent directory does not exist; none of them is touched.
    Guards "the whole export or nothing" at its edges: a link is somebody else's directory, and without the
    parent test the stage directory cannot be made and the command ends in a traceback."""
    r = R.build(tmp_path)
    a_file = tmp_path / "a-file"
    a_file.write_bytes(b"keep\n")
    p = _export("--repo", r.bare, r.ff, str(a_file))
    assert p.returncode == 1 and "not an empty directory" in p.stderr and a_file.read_bytes() == b"keep\n"
    p = _export("--repo", r.bare, r.ff, str(tmp_path / "no" / "such" / "out"))
    assert p.returncode == 1 and "parent directory does not exist" in p.stderr and "Traceback" not in p.stderr
    target = tmp_path / "somebody-elses"
    target.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        return                                            # this host cannot make a link: the two rows above stand
    p = _export("--repo", r.bare, r.ff, str(link))
    assert p.returncode == 1 and "not an empty directory" in p.stderr and list(target.iterdir()) == []
