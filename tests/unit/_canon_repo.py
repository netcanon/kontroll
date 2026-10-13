"""Throwaway canonical repositories for the tests of `kontroll.canon` and of what reads through it.

One builder (`build`) makes a bare repository shaped like a node's canonical — `main`, a proposal `main` has moved
past and one that fast-forwards it — and one function per thing a writer of that repository can leave in it (a
tag called `main`, a replace ref, a graft line, a `shallow` file, a commit-graph that lies, a `commondir`, a hook).
Each planter is a FIXTURE SEAM: a test plants one thing, then asks the code under test the question it would ask
of a clean repository. Objects are built from bytes (`hash-object`, `mktree`), never from files on disk, so a name
that no filesystem of this host would take still gets into a tree.

The planters write refs and stores as FILES: they assume git's files ref store, which is the default of every git
this suite runs on (a canonical made with another ref format would need them re-read).

Leading-underscore module name ⇒ pytest does not collect it; the `test_*` files import it. The git these helpers
run is the BUILDER's git (plain, whatever the suite's isolation gives it) — never the seam, which is what the
tests are about.
"""
import os
import struct
import subprocess
from types import SimpleNamespace

from kontroll import canon

STALE, FF = "57a1e0000000", "ff0000000000"        # the two proposals `build` stages, by run id

_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}


def git(repo, *args, stdin=None, check=True):
    """Run the builder's git in `repo` and return its stdout as bytes (stripped of one trailing newline)."""
    p = subprocess.run(["git", "-C", str(repo)] + list(args), input=stdin, capture_output=True,
                       env=dict(os.environ, **_WHO))
    if check and p.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace")))
    return p.stdout[:-1] if p.stdout.endswith(b"\n") else p.stdout


def rc(repo, *args):
    """The exit status of the builder's git — for a CONTROL: what git answers outside the seam."""
    return subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True,
                          env=dict(os.environ, **_WHO)).returncode


def blob(repo, data):
    return git(repo, "hash-object", "-w", "--stdin", stdin=data).decode("ascii")


def tree(repo, entries):
    """A tree object from `{name bytes: blob id | ("tree", id)}`. `mktree -z` takes any name without a slash, so
    a name that is not UTF-8, or holds a newline, goes in as its bytes."""
    lines = b""
    for name, val in sorted(entries.items()):
        kind, oid = val if isinstance(val, tuple) else ("blob", val)
        mode = b"040000" if kind == "tree" else b"100644"
        lines += mode + b" " + kind.encode("ascii") + b" " + oid.encode("ascii") + b"\t" + name + b"\0"
    return git(repo, "mktree", "-z", stdin=lines).decode("ascii")


def commit(repo, tree_id, parents=(), subject=b"a commit", author=b"t", when=b"1750000000 +0000", authored=None):
    """A commit object written from BYTES — so an author or a subject that is not UTF-8 gets in as it is (the
    porcelain would re-encode or refuse it). Every commit carries one fixed stamp unless `when` (the committer's
    `<seconds> <offset>`) or `authored` (the author's) says otherwise, so an id depends on content alone."""
    body = b"tree " + tree_id.encode("ascii") + b"\n"
    for p in parents:
        body += b"parent " + p.encode("ascii") + b"\n"
    body += b"author " + author + b" <t@example.test> " + (authored or b"1750000000 +0000") + b"\n"
    body += b"committer t <t@example.test> " + when + b"\n\n" + subject + b"\n"
    return git(repo, "hash-object", "-t", "commit", "-w", "--stdin", stdin=body).decode("ascii")


def raw_tree(repo, entries):
    """A tree object from raw `(mode bytes, name bytes, object id)` triples, written as given — modes and names
    `mktree` would not take (a submodule entry, a link, a name twice, a name with a slash)."""
    body = b"".join(mode + b" " + name + b"\0" + bytes.fromhex(oid) for mode, name, oid in entries)
    return git(repo, "hash-object", "-t", "tree", "-w", "--stdin", "--literally", stdin=body).decode("ascii")


def set_ref(repo, name, oid):
    git(repo, "update-ref", name, oid)


def write_ref_file(repo, name, text):
    """Write a ref as a FILE — what a direct writer of the repository does, and the only way to a symbolic ref
    under `refs/heads/proposed/` or to a branch that names a tag object (`update-ref` refuses both)."""
    path = os.path.join(str(repo), *name.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write(text + "\n")


def build(tmp_path, name="canonical"):
    """A bare canonical:

        base ── adv            <- refs/heads/main          (adds adv.txt)
           │      └── ff       <- refs/heads/proposed/FF    (adds ff.txt; a fast-forward of main)
           └── stale           <- refs/heads/proposed/STALE (adds stale.txt; main moved past it)

    Returns a namespace: `bare` (str), and the ids `base`, `main`, `stale`, `ff`."""
    bare = str(tmp_path / (name + ".git"))
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, capture_output=True)
    seed = blob(bare, b"base\n")
    t_base = tree(bare, {b"seed.txt": seed})
    base = commit(bare, t_base, subject=b"base")
    stale = commit(bare, tree(bare, {b"seed.txt": seed, b"stale.txt": blob(bare, b"y\n")}), [base],
                   subject=b"feat: stale one")
    main = commit(bare, tree(bare, {b"seed.txt": seed, b"adv.txt": blob(bare, b"z\n")}), [base],
                  subject=b"advance main")
    ff = commit(bare, tree(bare, {b"seed.txt": seed, b"adv.txt": blob(bare, b"z\n"), b"ff.txt": blob(bare, b"x\n")}),
                [main], subject=b"feat: ff one")
    set_ref(bare, canon.MAIN, main)
    set_ref(bare, canon.PROPOSED + STALE, stale)
    set_ref(bare, canon.PROPOSED + FF, ff)
    return SimpleNamespace(bare=bare, base=base, main=main, stale=stale, ff=ff)


def propose(repo, run_id, entries, parent, subject=b"feat: a proposal", author=b"t"):
    """Stage `refs/heads/proposed/<run_id>`: one commit on `parent` whose tree is `entries`. Returns its id."""
    oid = commit(repo.bare, tree(repo.bare, entries), [parent], subject=subject, author=author)
    set_ref(repo.bare, canon.PROPOSED + run_id, oid)
    return oid


# ---- what a writer of the repository can leave in it ---------------------------------------------------------- #

def plant_short_names(repo):
    """Every other thing the SHORT names `main` and `proposed/<id>` can mean, each pointing at `base`: a tag
    `main`, `refs/main`, a file `main` in the git directory, a tag `proposed/<FF>` and `refs/proposed/<FF>`."""
    set_ref(repo.bare, "refs/tags/main", repo.base)
    set_ref(repo.bare, "refs/main", repo.base)
    write_ref_file(repo.bare, "main", repo.base)
    set_ref(repo.bare, "refs/tags/proposed/" + FF, repo.base)
    set_ref(repo.bare, "refs/proposed/" + FF, repo.base)


def plant_replace(repo):
    """A replace ref that makes the stale proposal's commit stand for the fast-forward one."""
    set_ref(repo.bare, "refs/replace/" + repo.stale, repo.ff)


def plant_graft(repo):
    """An `info/grafts` line that gives the stale proposal `main` as its parent."""
    os.makedirs(os.path.join(repo.bare, "info"), exist_ok=True)
    with open(os.path.join(repo.bare, "info", "grafts"), "w", encoding="ascii", newline="\n") as fh:
        fh.write("%s %s\n" % (repo.stale, repo.main))


def plant_shallow(repo):
    """A `shallow` file that cuts the fast-forward proposal off from its parent."""
    with open(os.path.join(repo.bare, "shallow"), "w", encoding="ascii", newline="\n") as fh:
        fh.write(repo.ff + "\n")


def plant_lying_commit_graph(repo, parentless=None, tree_of=None):
    """Write a commit-graph, then rewrite one commit's record in it: `parentless=<id>` takes that commit's parent
    away; `tree_of=(<id>, <tree id>)` gives it another root tree. The file's checksum is left wrong — git reads
    the graph without checking it (`git commit-graph verify` is what does)."""
    git(repo.bare, "commit-graph", "write", "--reachable")
    path = os.path.join(repo.bare, "objects", "info", "commit-graph")
    os.chmod(path, 0o644)
    with open(path, "rb") as fh:
        data = bytearray(fh.read())
    assert data[:4] == b"CGPH" and data[5] == 1, "not a sha1 commit-graph: the fixture needs to be re-read"
    toc = [struct.unpack(">4sQ", data[8 + 12 * i: 20 + 12 * i]) for i in range(data[6] + 1)]
    span = {cid: (off, toc[i + 1][1]) for i, (cid, off) in enumerate(toc[:-1])}
    lo, hi = span[b"OIDL"]
    oids = [bytes(data[lo + 20 * i: lo + 20 * i + 20]).hex() for i in range((hi - lo) // 20)]

    def record(oid):
        return span[b"CDAT"][0] + oids.index(oid) * 36          # <root tree: 20> <parent 1: 4> <parent 2: 4> <8>

    if parentless:
        at = record(parentless)
        data[at + 20:at + 24] = struct.pack(">I", 0x70000000)   # the graph's "no parent"
    if tree_of:
        at = record(tree_of[0])
        data[at:at + 20] = bytes.fromhex(tree_of[1])
    with open(path, "wb") as fh:
        fh.write(data)


def plant_commondir(repo, other):
    """A `commondir` file naming another repository's directory."""
    with open(os.path.join(repo.bare, "commondir"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(other.replace("\\", "/") + "\n")


def plant_hook(repo, canary, name="reference-transaction", directory=None):
    """A hook that appends a line to `canary` when it runs — in the repository's own `hooks/`, or in `directory`
    (then named by `core.hooksPath` in the repository's config)."""
    hooks = directory or os.path.join(repo.bare, "hooks")
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/sh\necho ran >> '%s'\n" % str(canary).replace("\\", "/"))
    os.chmod(path, 0o755)
    if directory:
        git(repo.bare, "config", "core.hooksPath", str(directory).replace("\\", "/"))


def seam_without(bare, *names):
    """`canon.git_env(bare)` with the named entries taken out — an environment variable by its name, a config key
    by its key. For the tests that show ONE entry is the one that answers a planted thing."""
    env = canon.git_env(bare)
    n = int(env.pop("GIT_CONFIG_COUNT"))
    pairs = [(env.pop("GIT_CONFIG_KEY_%d" % i), env.pop("GIT_CONFIG_VALUE_%d" % i)) for i in range(n)]
    known = set(env) | {k for k, _ in pairs}
    assert set(names) <= known, "not an entry of the seam: %s" % sorted(set(names) - known)
    pairs = [(k, v) for k, v in pairs if k not in names]
    env = {k: v for k, v in env.items() if k not in names}
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (k, v) in enumerate(pairs):
        env["GIT_CONFIG_KEY_%d" % i], env["GIT_CONFIG_VALUE_%d" % i] = k, v
    return env
