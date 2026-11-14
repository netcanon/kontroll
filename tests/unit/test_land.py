"""The landing — `main` moves only through one function, and none of the doors pushes (C10, rule 8 of the
canonical's doors; SECURITY.md C10).

WHY: four doors used to put a commit on the canonical's `main` with `git push … HEAD:refs/heads/main` —
`scripts/update.sh`, `scripts/update-from-bundle.sh`, the operator CLI's onboard (`scripts/galaxy.py`) and the
deploy's mirror task (`ansible/playbooks/local-canonical.yml`) — and a fifth, `gitio.commit_and_push` outside
staging (the GUI run from a checkout). A push runs the canonical's `update` hook and whatever its config names, as
the PUSHER (the operator; root in the installer container), in a repository whose config a network service writes
when the deploy is armed; and it passed no gate, so a fleet line or a collection pin the promote refuses was on
`main` the moment a deploy ended — and every proposal was refused from then on. `gitio.land_commit` is the one way
now: a commit id, judged by the promote gate (the same `_tree_refusal` the promote runs, over the commit's files
read from the CALLER'S repository and re-hashed), brought across as a pack on a pipe (`pack-objects --stdout` in
the worktree, `index-pack --stdin` in the canonical, in the seam), checked whole, and compare-and-swapped in one
`update-ref --stdin` transaction. These rows run REAL git: only git can say what a pack holds, what a transaction
accepts, and whether a hook ran.
"""
import ast
import importlib.util
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest

import _canon_repo as R
from _readonly_pins import WRITE_VERBS, assert_git_runs_in_the_seam
from kontroll import canon, gitio, paths

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}

# Trees the REAL collection generator judges (the same shapes tests/unit/test_kontroll_promote.py uses): a clean
# registry; two units pinning one collection differently; a fleet-wide signature policy outside the closed set.
_ALPHA = "key: alpha\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n"
_BETA_SAME = "key: beta\ncollections:\n  - name: acme.appliance\n    version: '==1.0.0'\n"
_BETA_CONFLICT = "key: beta\ncollections:\n  - name: acme.appliance\n    version: '==2.0.0'\n"
CLEAN = {"modules/_core.yml": "collections: []\n", "config/fleet.yml": "enabled_modules: [alpha, beta]\n",
         "modules/alpha/module.yml": _ALPHA, "modules/beta/module.yml": _BETA_SAME}
CONFLICT = {**CLEAN, "modules/beta/module.yml": _BETA_CONFLICT}
BAD_POLICY = {**CLEAN, "config/fleet.yml": "enabled_modules: [alpha, beta]\nsignature_policy: whatever\n"}
RID = "0123456789ab"


def _git(cwd, *args, stdin=None, check=True):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), input=stdin, capture_output=True,
                       env=dict(os.environ, **_WHO))
    if check and p.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace")))
    return p.stdout.decode("utf-8", "replace").strip()


def _write(worktree, files):
    """Write `files` ({relative path: text}) into the worktree, LF line endings, removing nothing."""
    for rel, text in files.items():
        path = os.path.join(worktree, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def _commit(worktree, files, subject):
    """Write `files`, `git add -A`, commit; return the commit id."""
    _write(worktree, files)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", subject)
    return _git(worktree, "rev-parse", "HEAD")


# The box's paths as they are, and with a space and a quote in each (`box` parametrised indirectly): the remedy a
# refusal prints must quote every path in it for the shell (fold 8's own mutants, G8-nff-wt/repo-unquoted).
NAMES = [pytest.param(("canonical.git", "worktree"), id="plain"),
         pytest.param(("canon ical'x.git", "work tree'x"), id="spaced", marks=pytest.mark.skipif(
             os.name != "posix", reason="a directory whose name holds a space and a quote"))]


@pytest.fixture
def box(tmp_path, request):
    """A node in miniature: a bare canonical (`bare`) whose `main` holds the clean tree, and the operator's
    worktree (`wt`) — a clone of it with a `local` remote, as `local-canonical.yml` leaves one — at that same
    commit (`seed`). `core.autocrlf` is off in the worktree so a tree hashes the same on every host. A test
    parametrised over `NAMES` (indirectly) runs it also with a space and a quote in both paths."""
    names = getattr(request, "param", ("canonical.git", "worktree"))
    bare = str(tmp_path / names[0])
    wt = str(tmp_path / names[1])
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    _git(wt, "config", "user.name", "t")                           # `commit_and_push`'s own `git commit` needs one
    _git(wt, "config", "user.email", "t@example.test")
    seed = _commit(wt, CLEAN, "seed")
    _git(wt, "remote", "add", "local", bare)
    _git(wt, "push", "-q", "local", "HEAD:refs/heads/main")       # the fixture's seed: the bare carries no hook yet
    return type("Box", (), {"bare": bare, "wt": wt, "seed": seed})()


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_the_box_takes_the_names_it_is_given(box, request):
    """`box`, parametrised over NAMES: the worktree and the canonical are named as the param says (`work tree'x` and
    `canon ical'x.git` in the spaced case, which `shlex.quote` then changes). Guards the parametrisation the quote rows
    stand on: a box that ignored its param ran every spaced row on plain names, which quoting leaves as they are, so
    each landing row's quote passed whether the code quotes or not (read B-f8, N3:
    B9-land-box-ignores-param+nff-unquoted)."""
    names = request.node.callspec.params["box"]
    assert os.path.basename(box.wt) == names[1] and os.path.basename(box.bare) == names[0], (box.wt, box.bare)
    assert (shlex.quote(box.wt) != box.wt) == (" " in names[1]), box.wt


def _main(bare):
    got = canon.read_ref(bare, canon.MAIN)
    return got[0] if got else None


def _land_cli():
    """scripts/kontroll-land.py, loaded by path (its name is not a module name)."""
    spec = importlib.util.spec_from_file_location("kontroll_land_cli", os.path.join(ROOT, "scripts", "kontroll-land.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the rule, through the library ---------------------------------------------------------------------------- #

def test_a_clean_commit_lands_and_a_second_run_is_up_to_date(box):
    """A commit on top of the canonical's `main` lands: `main` is that commit, its objects are in the canonical
    (as a pack, under `objects/pack/`), the message says `landed refs/heads/main <id> (was <old>)`; run again,
    nothing moves and the message says `up to date` — the idempotence the play's `changed_when` reads. Guards
    the happy path against real git: a mocked pipe could not tell a pack git takes from one it does not."""
    new = _commit(box.wt, {"note.txt": "hello\n"}, "a change")
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert (rc, _main(box.bare)) == (0, new), said
    assert said == "landed refs/heads/main %s (was %s)" % (new, box.seed)
    assert any(n.endswith(".pack") for n in os.listdir(os.path.join(box.bare, "objects", "pack")))
    assert _git(box.bare, "cat-file", "-t", new) == "commit"
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert (rc, _main(box.bare)) == (0, new) and said.startswith("up to date"), said


def test_a_first_seed_creates_main_and_a_second_seed_is_refused_by_the_transaction(tmp_path, box):
    """Into an EMPTY canonical (no `main`) a landing creates `refs/heads/main` with the zero id as its old value
    — the whole history goes across as one pack. A second landing into it from a worktree that does not hold
    its `main` is not a seed any more: the transaction's "must not exist" fails, `main` keeps the first commit,
    and the sentence says so. Guards the one case the ancestry check cannot see (there is no `main` to read),
    and that it is git's compare-and-swap — not a read a moment earlier — that keeps a second seed out."""
    empty = str(tmp_path / "empty.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", empty], check=True, capture_output=True)
    rc, said = gitio.land_commit(box.wt, empty)
    assert (rc, _main(empty)) == (0, box.seed), said
    assert "a first seed" in said
    other = str(tmp_path / "other")
    subprocess.run(["git", "init", "-q", "-b", "main", other], check=True, capture_output=True)
    _git(other, "config", "core.autocrlf", "false")
    _commit(other, CLEAN, "another seed")
    rc, said = gitio.land_commit(other, empty, gate=lambda c, g, m: None)
    assert rc == 1 and _main(empty) == box.seed, said
    assert "has commit(s) your worktree" in said, "a canonical with a main is not seeded over: " + said


@pytest.mark.parametrize("tree, names", [(CONFLICT, ("acme.appliance", "conflict")),
                                         (BAD_POLICY, ("signature_policy",))])
def test_a_tree_the_promote_gate_refuses_is_refused_before_anything_touches_the_canonical(box, tree, names):
    """A commit whose tree holds two units pinning one collection to different `==` versions, or a fleet-wide
    `signature_policy` outside the closed set, is REFUSED (rc 2) with the generator's own sentence; `main` did
    not move, no pack reached the canonical's store (the commit is not there at all), and the worktree keeps the
    commit. Guards the point of the change: these trees used to be mirrored by every door and were then refused
    by every promote; the gate that refuses a proposal now refuses a landing, and refuses it BEFORE the pipe."""
    bad = _commit(box.wt, tree, "a tree the gate refuses")
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert rc == 2 and _main(box.bare) == box.seed, said
    assert all(n.lower() in said.lower() for n in names), said
    assert "the tree does not resolve" in said and "skip-conflict-check" in said
    assert subprocess.run(["git", "-C", box.bare, "cat-file", "-e", bad], capture_output=True).returncode != 0, \
        "a refused commit's objects reached the canonical: the gate ran after the pipe"
    assert _git(box.wt, "rev-parse", "HEAD") == bad


def test_a_repair_of_a_refused_main_lands(box):
    """The canonical's `main` holds a tree the gate refuses (landed by the previous build, which judged nothing);
    a commit on top of it that corrects the tree LANDS. Guards that the gate judges the commit being landed and
    not `main` as it stands: a landing that asked "is `main` clean?" first could never repair one."""
    broken = _commit(box.wt, CONFLICT, "the previous build mirrored this")
    _git(box.wt, "push", "-q", "local", "HEAD:refs/heads/main")      # as the previous build did: no gate, a push
    assert _main(box.bare) == broken
    fixed = _commit(box.wt, CLEAN, "repair")
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert (rc, _main(box.bare)) == (0, fixed), said


def test_skip_gate_lands_a_refused_tree_and_the_cli_says_the_gate_was_not_asked(box, capsys):
    """`skip_gate` (the CLI's `--skip-conflict-check`) lands a tree the gate refuses — the human's never-brick
    override — and the CLI's own log line says the gate was NOT asked. Guards the override staying an override
    (a landing that still judged under it would brick a node whose gate cannot run) and its being said aloud."""
    bad = _commit(box.wt, CONFLICT, "a tree the gate refuses")
    assert _land_cli().main(["--worktree", box.wt, "--repo", box.bare, "--skip-conflict-check"]) == 0
    assert _main(box.bare) == bad
    out = capsys.readouterr().out
    assert "the gate is NOT asked" in out and "landed refs/heads/main %s" % bad in out


def test_the_commit_landed_is_the_commit_judged(box):
    """Between the judgement and the transaction a NEW commit is made in the worktree (the gate callable makes
    it — the one moment a writer of the worktree can get in). What lands is the commit that was read and judged,
    not the worktree's new HEAD. Guards the landing holding an ID from its first read on — a landing that
    re-read HEAD for the pack would bring in a commit nobody judged."""
    judged = _commit(box.wt, {"a.txt": "a\n"}, "judged")
    made = []

    def gate(commit_id, gitdir, main_id):
        assert commit_id == judged and main_id == box.seed
        made.append(_commit(box.wt, {"b.txt": "b\n"}, "made while judging"))
        return None
    rc, said = gitio.land_commit(box.wt, box.bare, gate=gate)
    assert (rc, _main(box.bare)) == (0, judged), said
    assert made and _git(box.wt, "rev-parse", "HEAD") == made[0] != judged
    assert subprocess.run(["git", "-C", box.bare, "cat-file", "-e", made[0]], capture_output=True).returncode != 0, \
        "the unjudged commit's objects were packed"


@pytest.mark.parametrize("box", NAMES, indirect=True)
def test_a_canonical_that_has_commits_the_worktree_does_not_is_refused_with_the_plays_sentence(box):
    """A promote moved the canonical's `main` to a commit this worktree has never fetched (rc 128 from
    `merge-base`: not a valid object here), and — a second case — the worktree holds a commit that is not a
    descendant of `main` (rc 1): both are refused (rc 1) with the sentence the play printed, which names the
    remedy (`kontroll-land.py --sync`, which brings the worktree forward with no fetch) and never `--force`;
    `main` is untouched and nothing was packed; the remedy's worktree and canonical are quoted for the shell (with
    a space and a quote in the paths, `shlex.quote` differs). Guards the never-discard rule: a landing that forced,
    or that took rc 128 for "no main", would delete every promoted proposal; and the remedy pasted as printed
    (fold 8's own mutants G8-nff-wt-unquoted, G8-nff-repo-unquoted)."""
    promoted = R.commit(box.bare, _git(box.bare, "rev-parse", box.seed + "^{tree}"), [box.seed], subject=b"a promote")
    R.set_ref(box.bare, canon.MAIN, promoted)
    _commit(box.wt, {"c.txt": "c\n"}, "the operator's own change")
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert rc == 1 and _main(box.bare) == promoted, said
    assert "has commit(s) your worktree" in said and "kontroll-land.py --sync" in said and "--force" not in said
    assert "`python3 -B scripts/kontroll-land.py --sync --worktree %s --repo %s`" % (
        shlex.quote(box.wt), shlex.quote(box.bare)) in said, said
    _git(box.wt, "fetch", "-q", "local")
    _git(box.wt, "reset", "-q", "--hard", box.seed)          # now the worktree HOLDS the promoted commit, but is not on it
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert rc == 1 and "has commit(s) your worktree" in said and _main(box.bare) == promoted
    assert os.listdir(os.path.join(box.bare, "objects", "pack")) == [] or all(
        subprocess.run(["git", "-C", box.bare, "cat-file", "-e", c], capture_output=True).returncode != 0
        for c in [_git(box.wt, "rev-parse", "HEAD@{1}")])


def test_main_moved_between_the_judgement_and_the_transaction_is_refused_by_git(box):
    """Another landing (or a promote) moves `main` after this landing read it and judged its commit: the
    transaction carries `main`'s old value, git refuses it, `main` keeps the winner and the sentence says it
    moved. Guards the compare-and-swap: a landing that wrote `main` without its old value would overwrite a
    promote that landed first, as two racing promotes once did."""
    mine = _commit(box.wt, {"d.txt": "d\n"}, "mine")
    winner = []

    def gate(commit_id, gitdir, main_id):
        winner.append(R.commit(box.bare, _git(box.bare, "rev-parse", box.seed + "^{tree}"), [box.seed], subject=b"first"))
        R.set_ref(box.bare, canon.MAIN, winner[0])
        return None
    rc, said = gitio.land_commit(box.wt, box.bare, gate=gate)
    assert rc == 1 and _main(box.bare) == winner[0] != mine, said
    assert "moved while this landing was being judged" in said


def test_a_pack_short_of_an_object_is_refused_before_any_ref_names_it(box):
    """The canonical's side is handed a pack that LACKS objects the canonical does not hold (made against a base
    it never received): `index-pack` takes it — a pack is whole in itself — but the connectivity check finds the
    commit not whole, refuses, and `main` is untouched. Guards the read-back: without it a cut-short or wrongly
    based pack would land a `main` whose checkout fails on a missing object. (A missing BLOB is noticed too —
    `rev-list --objects` opens each one — measured on git 2.47 and 2.55.)"""
    x = _commit(box.wt, {"x.txt": "x\n"}, "X: never reaches the canonical")
    c = _commit(box.wt, {"y.txt": "y\n"}, "C, on X")
    packer = gitio._pack(box.wt, c, x)                       # excludes X's objects, which the canonical lacks
    rc, said = gitio._land_in_canonical(box.bare, canon.MAIN, c, box.seed, box.seed, packer)
    assert rc == 1 and "not whole" in said and _main(box.bare) == box.seed, said


def _history(tmp_path):
    """A whole repository (`src`): the clean tree, then three commits on it. Returns `(src, [c0, c1, c2, c3])`."""
    src = str(tmp_path / "src")
    subprocess.run(["git", "init", "-q", "-b", "main", src], check=True, capture_output=True)
    _git(src, "config", "core.autocrlf", "false")
    return src, [_commit(src, CLEAN, "seed")] + [_commit(src, {"%d.txt" % n: "%d\n" % n}, "c%d" % n) for n in (1, 2, 3)]


def _shallow_clone(src, wt, bare, depth):
    """`git clone --depth <depth>` of `src` — through a `file://` URL, since a clone of a local path ignores
    `--depth` — with core.autocrlf off and the `local` remote naming `bare`, as `local-canonical.yml` leaves one."""
    subprocess.run(["git", "clone", "-q", "--depth", str(depth), pathlib.Path(src).as_uri(), wt], check=True,
                   capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    _git(wt, "remote", "add", "local", bare)
    assert _git(wt, "rev-parse", "--is-shallow-repository") == "true"


def test_a_shallow_clone_short_of_what_the_canonical_lacks_is_refused_first_and_lands_once_unshallowed(tmp_path,
                                                                                                      capsys):
    """A `git clone --depth 1` worktree holds HEAD and not its parents. Into an EMPTY canonical (the first deploy
    after a quickstart) the landing, and its `--check` (the deploy's dry-run), is refused (rc 1) before anything is
    packed: the sentence says `is a shallow clone` and gives the remedy, `git -C <worktree> fetch --unshallow`, and
    the command exits 1 with it on stderr. The sentence names the shallow clone (`made by git clone --depth`),
    what git said, and that the canonical is untouched. Into a canonical whose `main` is a commit below where the
    clone stops, and not in it, the same sentence, not the not-a-fast-forward one: its remedy, a rebase, cannot
    mend a cut history. After `fetch --unshallow` the same commit lands, and the canonical holds it whole. Guards
    I-20: before this, the first landing from a shallow clone packed HEAD without its parents and failed only after
    the canonical had received the pack, with `the commit is not whole in the canonical`, which names neither the
    cause nor the remedy."""
    bare = str(tmp_path / "canonical.git")
    wt = str(tmp_path / "worktree")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, capture_output=True)
    src, (c0, _c1, _c2, c3) = _history(tmp_path)
    _shallow_clone(src, wt, bare, 1)
    remedy = "git -C %s fetch --unshallow" % wt
    for check in (True, False):
        rc, said = gitio.land_commit(wt, bare, check=check)
        assert rc == 1 and _main(bare) is None, said
        assert "is a shallow clone (made by `git clone --depth" in said and remedy in said, said
        assert "having no main yet (git: " in said and "the canonical is untouched" in said, said
    assert os.listdir(os.path.join(bare, "objects", "pack")) == [], "a pack was received"
    assert subprocess.run(["git", "-C", bare, "cat-file", "-e", c3], capture_output=True).returncode != 0
    assert _land_cli().main(["--worktree", wt, "--check"]) == 1
    out, err = capsys.readouterr()
    assert "kontroll_run_id=" in out and err.startswith("kontroll-land: the worktree") and remedy in err, err
    _git(src, "push", "-q", bare, c0 + ":refs/heads/main")       # main below the cut: the worktree has only c3
    rc, said = gitio.land_commit(wt, bare)
    assert rc == 1 and _main(bare) == c0, said
    assert "is a shallow clone" in said and "main (%s) is under HEAD cannot be told: it is not in the clone" % c0 \
        in said, said
    assert remedy in said and "has commit(s) your worktree" not in said, said
    _git(wt, "fetch", "-q", "--unshallow")
    assert _git(wt, "rev-parse", "--is-shallow-repository") == "false"
    rc, said = gitio.land_commit(wt, bare)
    assert (rc, _main(bare)) == (0, c3), said
    _git(bare, "rev-list", "--objects", "--quiet", c3)             # whole in the canonical: rc 0, or _git raises


@pytest.mark.parametrize("depth", [1, 2])
def test_a_shallow_clone_whose_cut_the_canonical_holds_lands(tmp_path, depth):
    """A `--depth 1` clone of the canonical's own `main` (cut AT `main`), and a `--depth 2` one (cut one commit
    below it), each with a commit made on top: everything the pack must carry is in the worktree, since the
    canonical holds `main` and all under it, so the landing goes through as from a whole clone, and the canonical
    holds the new commit whole. Guards the shallow check against refusing every shallow clone: it refuses a
    landing that would arrive short, and a clone that only adds commits on top of `main` arrives whole."""
    bare = str(tmp_path / "canonical.git")
    wt = str(tmp_path / "worktree")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, capture_output=True)
    src, made = _history(tmp_path)
    _git(src, "push", "-q", bare, made[-1] + ":refs/heads/main")
    _shallow_clone(src, wt, bare, depth)
    _git(wt, "config", "user.name", "t")
    _git(wt, "config", "user.email", "t@example.test")
    new = _commit(wt, {"4.txt": "4\n"}, "c4")
    rc, said = gitio.land_commit(wt, bare)
    assert (rc, said) == (0, "landed refs/heads/main %s (was %s)" % (new, made[-1])), said
    _git(bare, "rev-list", "--objects", "--quiet", new)


def _bare(tmp_path):
    """An empty bare canonical (no hook, no ref) at `<tmp>/canonical.git`."""
    bare = str(tmp_path / "canonical.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, capture_output=True)
    return bare


def _received(bare):
    """The packs the canonical holds: none, where a landing was refused before its pack."""
    return [n for n in os.listdir(os.path.join(bare, "objects", "pack")) if n.endswith(".pack")]


def _no_gate(calls=None):
    """A gate that passes every tree, recording the commits it was asked about in `calls`."""
    def gate(commit_id, gitdir, main_id):
        if calls is not None:
            calls.append(commit_id)
        return None
    return gate


def _merge_history(tmp_path, with_q):
    """A whole repository (`src`): c0, c1 on main; a side branch on c1 (q then s, or s alone); c2 on main; `m` =
    main merging side (`--no-ff`). A `--depth 2` clone of it holds m, c2 and s, and stops at c2 and s, whose
    parents (c1; q, or c1) it never fetched. Returns `(src, ids)`, the ids by name."""
    src = str(tmp_path / "src")
    subprocess.run(["git", "init", "-q", "-b", "main", src], check=True, capture_output=True)
    _git(src, "config", "core.autocrlf", "false")
    ids = {"c0": _commit(src, CLEAN, "c0"), "c1": _commit(src, {"1.txt": "1\n"}, "c1")}
    _git(src, "checkout", "-q", "-b", "side")
    if with_q:
        ids["q"] = _commit(src, {"q.txt": "q\n"}, "q")
    ids["s"] = _commit(src, {"s.txt": "s\n"}, "s")
    _git(src, "checkout", "-q", "main")
    ids["c2"] = _commit(src, {"2.txt": "2\n"}, "c2")
    _git(src, "merge", "-q", "--no-ff", "--no-edit", "side")
    ids["m"] = _git(src, "rev-parse", "HEAD")
    return src, ids


@pytest.mark.parametrize("side", ["under-main", "under-a-proposal", "nowhere"])
def test_a_shallow_merge_lands_where_the_canonical_holds_what_its_cut_sides_rest_on(tmp_path, side, monkeypatch):
    """A `--depth 2` clone of a merge `m` (first parent c2, the canonical's `main`; second parent s) stops at c2
    and at s. "under-main": s rests on c1, which the canonical's `main` holds, and the landing goes through, the
    canonical holding m whole; so it does where s rests on q, held by the canonical only under a proposal ref
    ("under-a-proposal"). "nowhere": the canonical holds no q, the clone does not either, and the landing is
    refused before anything is packed, with the shallow sentence and git's own line. After each landing, refused or not, by
    `--check` or not, the temporary list of the stops kept is gone. Guards the I-20 fold's S1: the first shallow
    check read the canonical's `main` cut in the clone too and refused every such merge, though it arrives whole
    (the old code landed it); the fold reaching too far, a merge whose side the canonical lacks let through to a
    pack it then refuses; and a landing that leaves a file behind in the temporary directory each time it runs."""
    cuts = tmp_path / "tmp"
    cuts.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(cuts))
    bare = _bare(tmp_path)
    src, ids = _merge_history(tmp_path, with_q=side != "under-main")
    _git(src, "push", "-q", bare, ids["c2"] + ":refs/heads/main")
    if side == "under-a-proposal":
        _git(src, "push", "-q", bare, ids["q"] + ":refs/heads/proposed/" + RID)
    wt = str(tmp_path / "worktree")
    _shallow_clone(src, wt, bare, 2)
    holds = {n: subprocess.run(["git", "-C", wt, "cat-file", "-e", ids[n]], capture_output=True).returncode == 0
             for n in ids}
    assert holds == {n: n in ("s", "c2", "m") for n in ids}, holds
    held = side != "nowhere"
    for check in (True, False):
        rc, said = gitio.land_commit(wt, bare, check=check, gate=_no_gate())
        if held:
            assert rc == 0, said
        else:
            assert rc == 1 and "is a shallow clone" in said and "does not either (git: " in said, said
            assert "(git: rc " not in said, "the sentence lost git's own line: " + said
        assert os.listdir(str(cuts)) == [], "the list of the stops kept was left behind"
    if held:
        assert _main(bare) == ids["m"]
        _git(bare, "rev-list", "--objects", "--quiet", ids["m"])
    else:
        assert _main(bare) == ids["c2"] and _received(bare) == [], said


def test_a_shallow_clone_on_main_whose_tip_is_a_stop_lands(tmp_path):
    """A `--depth 1` clone (c3 only, a stop) after `git fetch local` has brought the canonical's `main`, c2, which
    is c3's own parent: in the clone's own view c2 is not under c3 (the stop hides it), but c3 records it, so the
    ancestry is asked again with the stops lifted, and the landing goes through, whole. Guards the fold's S5/S4: the
    first shallow check said "cannot be told: the history between them was never fetched" here, with nothing
    missing; and the re-ask must not let through a `main` the clone cannot show under HEAD (the next row)."""
    bare = _bare(tmp_path)
    src, made = _history(tmp_path)
    wt = str(tmp_path / "worktree")
    _shallow_clone(src, wt, bare, 1)
    _git(src, "push", "-q", bare, made[2] + ":refs/heads/main")
    _git(wt, "fetch", "-q", "local")
    rc, said = gitio.land_commit(wt, bare, gate=_no_gate())
    assert (rc, _main(bare)) == (0, made[3]), said
    _git(bare, "rev-list", "--objects", "--quiet", made[3])


def test_a_main_below_the_cut_is_refused_with_the_shallow_sentence_by_the_dry_run_and_before_the_gate(tmp_path):
    """A `--depth 1` clone (c3), the canonical's `main` c1: not in the clone, then fetched into it (`git fetch
    local`, the rebase advice's first half), where the clone still cannot show it under HEAD. Each time, `--check`
    and the landing both refuse with the shallow sentence (`cannot be told`; "it is not in the clone", then "the
    clone stops before the history that would tell"), never the not-a-fast-forward one, and the gate is never
    asked. And into an empty canonical, the same: refused, the gate never asked. Guards the shallow refusals'
    place: in the dry-run (`--check` returning before them gave the rebase advice again, the I-20 bug) and before
    the gate (which would judge, and run its generator over, a tree that will be refused). Each sentence also names
    the sync for a `main` a promote moved — `python3 -B scripts/kontroll-land.py --sync --worktree … --repo …`, each
    path quoted — which the second one lacked (read A-r21)."""
    bare = _bare(tmp_path)
    src, made = _history(tmp_path)
    wt = str(tmp_path / "worktree")
    _shallow_clone(src, wt, bare, 1)
    calls = []
    for check in (True, False):
        rc, said = gitio.land_commit(wt, bare, check=check, gate=_no_gate(calls))
        assert rc == 1 and "is a shallow clone" in said, said
    _git(src, "push", "-q", bare, made[1] + ":refs/heads/main")
    for fetched, clause in ((False, "it is not in the clone"), (True, "the clone stops before the history")):
        if fetched:
            _git(wt, "fetch", "-q", "local")
            assert _git(wt, "cat-file", "-t", made[1]) == "commit"
        for check in (True, False):
            rc, said = gitio.land_commit(wt, bare, check=check, gate=_no_gate(calls))
            assert rc == 1 and "under HEAD cannot be told: " + clause in said, said
            assert "has commit(s) your worktree" not in said, said
            assert ("`python3 -B scripts/kontroll-land.py --sync --worktree %s --repo %s`"
                    % (shlex.quote(wt), shlex.quote(bare))) in said, said
    assert calls == [] and _main(bare) == made[1] and _received(bare) == []


def test_a_linked_worktree_of_a_shallow_clone_is_judged_as_shallow(tmp_path):
    """`git worktree add` of a `--depth 1` clone: its `.git` is a file, and its shallow list is the main
    checkout's (git says it is shallow). Into an empty canonical it is refused before anything is packed, as the
    clone itself is. Guards how the shallow question is asked (git's own answer, `--is-shallow-repository`, and
    its own path to the list, `--git-path shallow`), not a look for `<worktree>/.git/shallow`, which is not there."""
    bare = _bare(tmp_path)
    src, _made = _history(tmp_path)
    wt = str(tmp_path / "worktree")
    _shallow_clone(src, wt, bare, 1)
    linked = str(tmp_path / "linked")
    _git(wt, "worktree", "add", "-q", "--detach", linked)
    assert os.path.isfile(os.path.join(linked, ".git"))
    rc, said = gitio.land_commit(linked, bare, gate=_no_gate())
    assert rc == 1 and "is a shallow clone" in said and _received(bare) == [], said


def test_a_whole_clone_short_of_an_object_is_not_called_shallow(box):
    """A WHOLE worktree that lacks a blob under HEAD (removed from its store) is not told it is a shallow clone:
    the pack's own view is judged for a shallow clone only, and a damaged whole clone still gets the canonical's
    read-back, "not whole … after the pack was received", whose row in docs/troubleshooting.md §10 names it.
    Guards the shallow sentence's reach: its remedy, `fetch --unshallow`, means nothing to a whole clone."""
    new = _commit(box.wt, {"z.txt": "z\n"}, "z")
    blob = _git(box.wt, "rev-parse", new + ":z.txt")
    loose = os.path.join(box.wt, ".git", "objects", blob[:2], blob[2:])
    os.chmod(loose, 0o644)
    os.remove(loose)
    rc, said = gitio.land_commit(box.wt, box.bare, gate=_no_gate())
    assert rc == 1 and "shallow" not in said and _main(box.bare) == box.seed, said


@pytest.mark.parametrize("how", ["replace-ref", "grafts"])
def test_a_shallow_clone_whose_stop_is_rewritten_is_judged_by_the_parents_it_records(tmp_path, how):
    """A `--depth 1` clone (c3, its parent c2 never fetched) into an empty canonical, with c3 made parentless in
    the clone's own view: by `git replace --graft`, or by an `info/grafts` line. The canonical reads c3's parents
    as c3 records them, through neither rewrite, so a pack that leaves c2's history out arrives short. The landing
    is refused before anything is packed, with the shallow sentence, by `--check` too. Guards the fold's S2: the
    first shallow check followed the rewrite, said "would land", and the apply failed after the pack, the I-20
    symptom."""
    bare = _bare(tmp_path)
    src, made = _history(tmp_path)
    wt = str(tmp_path / "worktree")
    _shallow_clone(src, wt, bare, 1)
    if how == "replace-ref":
        _git(wt, "replace", "--graft", made[3])
    else:
        with open(os.path.join(wt, ".git", "info", "grafts"), "w", encoding="ascii", newline="\n") as fh:
            fh.write(made[3] + "\n")
    for check in (True, False):
        rc, said = gitio.land_commit(wt, bare, check=check, gate=_no_gate())
        assert rc == 1 and "is a shallow clone" in said, said
    assert _main(bare) is None and _received(bare) == []


def test_a_depth_fetch_into_a_whole_clone_lands_whole(tmp_path):
    """A whole clone that holds a branch's f0, then `git fetch --depth=1` of that branch after f1 was made on f0:
    git lists f1 as a stop although f0 is in the clone, and HEAD merges f1 (`--allow-unrelated-histories`, as the
    cut view needs). The canonical's `main` (m3) holds no f*. f1's stop is lifted (the canonical lacks f0), the
    walk goes on into f0, which the clone holds, and the pack, made in the same view, carries f0: the landing
    goes through and the canonical holds HEAD whole. Guards the pack's view: a check that reads past the stop while
    `pack-objects` keeps it says "would land" and the apply fails after the pack (the fold's S2)."""
    bare = _bare(tmp_path)
    src = str(tmp_path / "src")
    subprocess.run(["git", "init", "-q", "-b", "main", src], check=True, capture_output=True)
    _git(src, "config", "core.autocrlf", "false")
    m = [_commit(src, CLEAN, "m0"), _commit(src, {"m1.txt": "1\n"}, "m1")]
    _git(src, "checkout", "-q", "-b", "feature")
    f0 = _commit(src, {"f0.txt": "f0\n"}, "f0")
    _git(src, "checkout", "-q", "main")
    m += [_commit(src, {"m2.txt": "2\n"}, "m2"), _commit(src, {"m3.txt": "3\n"}, "m3")]
    _git(src, "push", "-q", bare, m[3] + ":refs/heads/main")
    wt = str(tmp_path / "worktree")
    subprocess.run(["git", "clone", "-q", "--no-local", pathlib.Path(src).as_uri(), wt], check=True,
                   capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    _git(wt, "fetch", "-q", "origin", "feature:refs/remotes/origin/feature")
    _git(src, "checkout", "-q", "feature")
    f1 = _commit(src, {"f1.txt": "f1\n"}, "f1")
    _git(src, "checkout", "-q", "main")
    _git(wt, "fetch", "-q", "--depth=1", "origin", "feature")
    assert _git(wt, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(wt, "cat-file", "-t", f0) == "commit"
    _git(wt, "merge", "-q", "--no-edit", "--allow-unrelated-histories", "origin/feature")
    head = _git(wt, "rev-parse", "HEAD")
    assert f1 in _git(wt, "rev-parse", "HEAD^2")
    for check in (True, False):
        rc, said = gitio.land_commit(wt, bare, check=check, gate=_no_gate())
        assert rc == 0, said
    assert _main(bare) == head
    _git(bare, "rev-list", "--objects", "--quiet", head)


def test_a_shallow_partial_clone_short_of_a_blob_is_refused_and_fetches_nothing(tmp_path):
    """A `--filter=blob:none --depth 4` clone from a local promisor (`file://`, filters served), its stop at c0,
    the canonical's `main`; above `main` c1 adds tmp.txt and c2 drops it, so no checkout ever fetched that blob.
    The landing is refused, `main` unmoved, and the blob is still missing in the worktree: the judgement fetched
    nothing. Guards the no-transport rule of the judgement (GIT_NO_LAZY_FETCH, `protocol.allow=never`): a check
    allowed to fetch lazily would reach the promisor for the blob, pass, and pack. (The first build called the
    mutant that drops both rules equivalent; this row tells them apart.)"""
    bare = _bare(tmp_path)
    src = str(tmp_path / "src")
    subprocess.run(["git", "init", "-q", "-b", "main", src], check=True, capture_output=True)
    for key, value in (("core.autocrlf", "false"), ("uploadpack.allowFilter", "true"),
                       ("uploadpack.allowAnySHA1InWant", "true")):
        _git(src, "config", key, value)
    _commit(src, {"x.txt": "x\n"}, "x")
    c0 = _commit(src, CLEAN, "c0")
    c1 = _commit(src, {"tmp.txt": "tmp\n"}, "c1 adds tmp.txt")
    os.remove(os.path.join(src, "tmp.txt"))
    _commit(src, {}, "c2 drops tmp.txt")
    _commit(src, {"3.txt": "3\n"}, "c3")
    blob = _git(src, "rev-parse", c1 + ":tmp.txt")
    _git(src, "push", "-q", bare, c0 + ":refs/heads/main")
    wt = str(tmp_path / "worktree")
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--depth", "4", pathlib.Path(src).as_uri(), wt],
                   check=True, capture_output=True)

    def missing():
        return subprocess.run(["git", "-C", wt, "cat-file", "-e", blob], capture_output=True,
                              env=dict(os.environ, GIT_NO_LAZY_FETCH="1")).returncode != 0

    assert _git(wt, "rev-parse", "--is-shallow-repository") == "true" and missing()
    rc, said = gitio.land_commit(wt, bare, gate=_no_gate())
    assert rc == 1 and "is a shallow clone" in said, said
    assert _main(bare) == c0 and missing() and _received(bare) == [], said


def test_a_judgement_that_cannot_run_refuses_before_the_pack(tmp_path, monkeypatch):
    """When git cannot finish the pack view's walk (a timeout, stood in for by a wrapper around `gitio._wt` that
    raises for `rev-list` only), the shallow clone is refused before anything is packed, the sentence saying that
    it could not be judged and naming no cause it did not find, and the temporary list of the stops kept is gone.
    Guards the judgement failing open: a check that passed when it could not run would hand the canonical a pack
    it then refuses."""
    cuts = tmp_path / "tmp"
    cuts.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(cuts))
    bare = _bare(tmp_path)
    src, _made = _history(tmp_path)
    wt = str(tmp_path / "worktree")
    _shallow_clone(src, wt, bare, 1)
    real = gitio._wt

    def times_out(worktree, argv, *a, **k):
        if "rev-list" in argv:
            raise subprocess.TimeoutExpired(argv, 1)
        return real(worktree, argv, *a, **k)

    monkeypatch.setattr(gitio, "_wt", times_out)
    rc, said = gitio.land_commit(wt, bare, gate=_no_gate())
    assert rc == 1 and "could not finish judging what the pack would carry (TimeoutExpired)" in said, said
    assert _received(bare) == [] and os.listdir(str(cuts)) == []


def test_a_proposal_lands_from_a_shallow_clone_whose_cut_the_canonical_holds(tmp_path):
    """`--propose` (`target` a `refs/heads/proposed/<run id>`) from a `--depth 1` clone of the canonical's `main`
    with a commit on top: the proposal is made, `main` unmoved, and the canonical holds the commit whole. Guards
    the shallow judgement on the proposal door, whose old value is the zero id while the pack is still made
    against `main`."""
    bare = _bare(tmp_path)
    src, made = _history(tmp_path)
    _git(src, "push", "-q", bare, made[-1] + ":refs/heads/main")
    wt = str(tmp_path / "worktree")
    _shallow_clone(src, wt, bare, 1)
    new = _commit(wt, {"4.txt": "4\n"}, "c4")
    rc, said = gitio.land_commit(wt, bare, target=canon.proposal_ref(RID), gate=_no_gate())
    assert rc == 0 and _main(bare) == made[-1], said
    assert canon.read_ref(bare, canon.proposal_ref(RID))[0] == new
    _git(bare, "rev-list", "--objects", "--quiet", new)


def test_a_proposal_is_created_once_and_a_proposal_of_mains_commit_is_nothing_to_propose(box):
    """`target=refs/heads/proposed/<run id>` creates the proposal with the zero id as its old value (`main` is not
    moved); a second landing to the same name is refused ("created once") and the proposal keeps its commit — so
    a hand-made proposal cannot be re-pointed after a human read it, as the hook holds for a pushed one. A
    proposal of `main`'s own commit is refused as nothing to propose, and a target that is neither `main` nor a
    minted run id's proposal is refused before any read. Guards the troubleshooting row that replaced
    `git push <canonical> HEAD:refs/heads/proposed/…`."""
    rc, said = gitio.land_commit(box.wt, box.bare, target=canon.PROPOSED + RID)
    assert rc == 1 and "nothing to propose" in said
    first = _commit(box.wt, {"p.txt": "1\n"}, "proposal")
    rc, said = gitio.land_commit(box.wt, box.bare, target=canon.PROPOSED + RID)
    assert rc == 0 and canon.read_ref(box.bare, canon.PROPOSED + RID) == (first, "commit", "") and _main(box.bare) == box.seed, said
    _commit(box.wt, {"p.txt": "2\n"}, "re-pointed")
    rc, said = gitio.land_commit(box.wt, box.bare, target=canon.PROPOSED + RID)
    assert rc == 1 and "created once" in said and canon.read_ref(box.bare, canon.PROPOSED + RID)[0] == first, said
    for bad in ("refs/heads/feature", canon.PROPOSED + "run1", "refs/tags/main", canon.PROPOSED + RID + "\n"):
        rc, said = gitio.land_commit(box.wt, box.bare, target=bad)
        assert rc == 1 and "must be refs/heads/main or" in said, (bad, said)


def test_a_gate_that_cannot_run_refuses_with_its_own_sentence(box):
    """A gate that raises `GateCouldNotRun` — this checkout has no generator, git could not read — refuses the
    landing (rc 2) with the sentence that names the override, a DIFFERENT sentence from "the tree is refused";
    `main` is untouched. And `_the_gate` itself raises it when the promote script is not where the package is.
    Guards fail-closed: a landing that took "could not judge" for "nothing to check" is the fail-open the old
    gate had."""
    _commit(box.wt, {"e.txt": "e\n"}, "e")

    def cannot(commit_id, gitdir, main_id):
        raise gitio.GateCouldNotRun("the collection generator could not be started")
    rc, said = gitio.land_commit(box.wt, box.bare, gate=cannot)
    assert rc == 2 and "the gate could not run" in said and "could not be started" in said and _main(box.bare) == box.seed
    assert "does not resolve" not in said
    assert gitio._the_gate().__name__ == "_tree_refusal"


def test_a_main_that_is_symbolic_or_names_no_commit_refuses_the_landing(box):
    """A canonical whose `refs/heads/main` is a symbolic ref, or names a tag object, refuses the landing (rc 1)
    and nothing is landed beside it. Guards rule 1 of `kontroll.canon` at this door: a landing that followed the
    pointer would move the ref it points at — or take a tag's id for a commit's."""
    _commit(box.wt, {"f.txt": "f\n"}, "f")
    R.write_ref_file(box.bare, canon.MAIN, "ref: refs/heads/elsewhere")
    R.set_ref(box.bare, "refs/heads/elsewhere", box.seed)
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert rc == 1 and "symbolic ref, or names no commit" in said, said
    assert canon.read_ref(box.bare, "refs/heads/elsewhere")[0] == box.seed


# ---- not a push: the canonical's hooks and config-named programs do not run at any door --------------------------- #

def _plant_canaries(bare, canary):
    """Everything a push would make run in the canonical, each writing a line to `canary`: an `update`, a
    `reference-transaction` and a `post-update` hook in a directory `core.hooksPath` names (and in `hooks/`),
    `core.alternateRefsCommand`, `receive.procReceiveRefs` is not a program — so the three that are."""
    for hooks in (os.path.join(bare, "hooks"), os.path.join(bare, "hooks-named")):
        os.makedirs(hooks, exist_ok=True)
        for name in ("update", "reference-transaction", "post-update", "pre-receive"):
            path = os.path.join(hooks, name)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("#!/bin/sh\necho %s >> '%s'\n" % (name, canary.replace("\\", "/")))
            os.chmod(path, 0o755)
    _git(bare, "config", "core.hooksPath", os.path.join(bare, "hooks-named").replace("\\", "/"))
    _git(bare, "config", "core.alternateRefsCommand", "sh -c 'echo alternate >> %s'" % canary.replace("\\", "/"))


def _canary_lines(canary):
    return open(canary, encoding="utf-8").read().split() if os.path.exists(canary) else []


@pytest.mark.skipif(not shutil.which("sh"), reason="the canary hooks are sh scripts (git runs hooks through sh)")
def test_a_landing_runs_none_of_the_canonicals_hooks_or_programs_and_a_push_runs_them(box, tmp_path):
    """With an `update`, a `reference-transaction`, a `pre-receive` and a `post-update` hook planted in the
    canonical (in `hooks/` AND in a `core.hooksPath` directory) and a `core.alternateRefsCommand` in its config,
    each writing its name to a file: a landing to `main` and a proposal landing leave the file EMPTY — nothing of
    the canonical's ran. The control beside it: a real `git push` of a proposal into the same canonical makes the
    hooks run (the file gains their names). Guards the reversion "land_commit -> git push" at the library, which
    no grep could tell from a push spelled another way: whatever brings the commit across must run nothing the
    canonical names."""
    canary = str(tmp_path / "ran")
    _plant_canaries(box.bare, canary)
    _commit(box.wt, {"g.txt": "g\n"}, "g")
    rc, said = gitio.land_commit(box.wt, box.bare, gate=lambda c, g, m: None)
    assert rc == 0, said
    _commit(box.wt, {"h.txt": "h\n"}, "h")
    rc, said = gitio.land_commit(box.wt, box.bare, target=canon.PROPOSED + RID, gate=lambda c, g, m: None)
    assert rc == 0, said
    assert _canary_lines(canary) == [], "a landing ran a program of the canonical's: %r" % _canary_lines(canary)
    _commit(box.wt, {"i.txt": "i\n"}, "i")
    pushed = subprocess.run(["git", "-C", box.wt, "push", "-q", "local", "HEAD:refs/heads/proposed/0123456789cd"],
                            capture_output=True, env=dict(os.environ, **_WHO))
    assert pushed.returncode == 0, pushed.stderr
    assert {"update", "reference-transaction", "post-update", "pre-receive"} <= set(_canary_lines(canary)), \
        "control: a push must run the canonical's hooks, or this test shows nothing"


def test_the_landing_is_a_registered_write_and_its_canonical_side_runs_in_the_seam():
    """`land_commit`, `_land_in_canonical`, `_pack` and `land_main` are in `WRITE_VERBS` (a read view that reached
    one would fail its pin); every process `_land_in_canonical` starts is `git index-pack | rev-list | update-ref`
    passing `**canon.git_kwargs(…)` with a timeout — and in the whole of gitio.py the verbs `index-pack` and
    `update-ref`, and a `pack-objects`, appear in no other function than those three, the promote's and the sync's
    two (`_receive_in_worktree` takes the canonical's pack into the worktree; `_pack_in_canonical` makes it, in the
    seam — tests/unit/test_sync.py pins that one). Guards the
    canonical's side of the landing against a git that inherited the caller's environment (it would run the
    canonical's config-named programs, as whoever lands — root in the installer), and against a second, unpinned
    copy of the write appearing elsewhere in the module."""
    for verb in ("land_commit", "_land_in_canonical", "_pack", "land_main"):
        assert verb in WRITE_VERBS, verb
    assert_git_runs_in_the_seam("scripts/kontroll/gitio.py", verbs={"index-pack", "rev-list", "update-ref"},
                                functions=("_land_in_canonical",))
    tree = ast.parse(open(os.path.join(ROOT, "scripts", "kontroll", "gitio.py"), encoding="utf-8").read())
    allowed = {"index-pack": {"_land_in_canonical", "_receive_in_worktree"},
               "update-ref": {"_land_in_canonical", "promote_commit"}, "pack-objects": {"_pack", "_pack_in_canonical"}}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.List):
                words = {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                for verb, where in allowed.items():
                    assert not (verb in words and "git" in words) or fn.name in where, (
                        "`git %s` in %s — the landing's write lives in %s only" % (verb, fn.name, sorted(where)))


def test_no_door_pushes_main_and_no_pack_is_thin():
    """No file of the four doors nor gitio.py carries a `push` to `refs/heads/main` (or `local main`), the
    landing's pack-objects carries no `--thin`, and each door names `kontroll-land.py` or `land_main`:
    `scripts/update.sh`, `scripts/update-from-bundle.sh`, `scripts/galaxy.py` (both commit sites),
    `ansible/playbooks/local-canonical.yml` (which also has no `git push`, no `ls-remote` and no `fetch` task, and
    runs the landing twice and the sync twice — `--check`, then for real), and
    `gitio.commit_and_push`'s non-staging branch. The staging push of a PROPOSAL stays. Guards the reversions
    "one door pushes again" and "--thin added" (a thin pack is made of deltas against objects assumed present —
    the landing reads nothing of the canonical's store to know)."""
    def read(rel):
        return open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for rel in ("scripts/update.sh", "scripts/update-from-bundle.sh"):
        s = read(rel)
        assert "git push" not in s and "HEAD:refs/heads/main" not in s, rel
        assert "kontroll-land.py" in s, rel
    g = read("scripts/galaxy.py")
    assert "HEAD:refs/heads/main" not in g and '"push", "local"' not in g and g.count("gitio.land_main()") == 2
    io = read("scripts/kontroll/gitio.py")
    assert "land_main(run_id=run_id)" in io, "commit_and_push's non-staging branch must land, not push"
    # in gitio.py the one argv that pushes `local` is the staging push, inside commit_and_push, under `if staging:`
    # (its refspec is built from `target`, which is a proposal there); no other function pushes anything to local;
    # and no argv carries `--thin` (the prose may name it; an argv may not)
    pushes, thin = [], []
    for fn in [n for n in ast.walk(ast.parse(io)) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.List):
                words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if "push" in words and "local" in words:
                    pushes.append((fn.name, words))
                if "--thin" in words:
                    thin.append(fn.name)
    assert [p[0] for p in pushes] == ["commit_and_push"] and "HEAD:refs/heads/main" not in pushes[0][1], pushes
    assert thin == [], "a thin pack is made against objects the landing never checked the canonical for: %r" % thin
    play = read("ansible/playbooks/local-canonical.yml")
    import yaml
    tasks = [t for p in yaml.safe_load(play) for t in p.get("tasks", [])]

    def cmd(t):
        for k in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
            v = t.get(k)
            if isinstance(v, dict):
                return str(v.get("cmd", v.get("_raw_params", "")))
            if isinstance(v, str):
                return v
        return ""
    assert not any("git push" in cmd(t) or "ls-remote" in cmd(t) or "git fetch" in cmd(t) for t in tasks), \
        [t["name"] for t in tasks if "git push" in cmd(t) or "ls-remote" in cmd(t) or "git fetch" in cmd(t)]
    landing = [t["name"] for t in tasks if "kontroll-land.py" in cmd(t) and "--sync" not in cmd(t)]
    syncing = [t["name"] for t in tasks if "kontroll-land.py" in cmd(t) and "--sync" in cmd(t)]
    assert (len(landing), len(syncing)) == (2, 2), "the play lands twice and syncs twice: %r %r" % (landing, syncing)


# ---- the doors: the command, the operator CLI's seam, commit_and_push outside staging, the shell scripts ---------- #

def test_the_command_lands_checks_and_refuses_with_the_exit_statuses_the_doors_read(box, capsys):
    """`scripts/kontroll-land.py`: `--check` on a clean commit exits 0, prints `kontroll_run_id=… land --check …`
    and `would land`, and moves nothing; without it exits 0 and lands; `--check` on a refused tree exits 2 with
    `REFUSED:` on stderr and the generator's sentence; `--propose <id>` creates the proposal and a second exits
    1; `--repo` that is no directory, `--propose` with a string that is not a run id, a worktree with no `local`
    remote and no `--repo`, and a usage error each exit 1 — never 2, which the doors read as "refused by the
    gate". Guards the command's contract with `update.sh`, `update-from-bundle.sh` and the play's two tasks."""
    cli = _land_cli()
    new = _commit(box.wt, {"j.txt": "j\n"}, "j")
    assert cli.main(["--worktree", box.wt, "--repo", box.bare, "--check"]) == 0
    out, err = capsys.readouterr()
    assert "kontroll_run_id=" in out and "land --check" in out and "would land refs/heads/main %s" % new in out
    assert _main(box.bare) == box.seed and err == ""
    assert cli.main(["--worktree", box.wt]) == 0                  # --repo defaults to the `local` remote
    assert _main(box.bare) == new and "landed refs/heads/main %s" % new in capsys.readouterr().out
    bad = _commit(box.wt, CONFLICT, "refused")
    assert cli.main(["--worktree", box.wt, "--repo", box.bare, "--check"]) == 2
    out, err = capsys.readouterr()
    assert err.startswith("REFUSED: ") and "acme.appliance" in err and _main(box.bare) == new
    assert cli.main(["--worktree", box.wt, "--repo", box.bare]) == 2 and _main(box.bare) == new
    capsys.readouterr()
    _commit(box.wt, CLEAN, "clean again")
    assert cli.main(["--worktree", box.wt, "--propose", RID]) == 0
    assert canon.read_ref(box.bare, canon.PROPOSED + RID) is not None and _main(box.bare) == new
    assert cli.main(["--worktree", box.wt, "--propose", RID]) == 1
    assert "created once" in capsys.readouterr().err
    assert cli.main(["--worktree", box.wt, "--propose", "run1"]) == 1
    assert cli.main(["--worktree", box.wt, "--repo", os.path.join(box.bare, "nowhere")]) == 1
    assert cli.main(["--worktree", box.wt, "--repo", "ssh://host/srv/kontroll.git"]) == 1
    _git(box.wt, "remote", "remove", "local")
    assert cli.main(["--worktree", box.wt]) == 1
    assert "no `local` remote" in capsys.readouterr().err
    assert cli.main(["--no-such-option"]) == 1


def test_land_main_lands_the_write_roots_head_on_its_local_remote_and_narrates(box, monkeypatch, capsys):
    """`gitio.land_main()` — what `scripts/galaxy.py`'s two commit sites and a non-staging `commit_and_push` call —
    finds the canonical through the write root's `local` remote (a path, a `file://` URL, a relative path), lands
    HEAD there, prints a `$ kontroll-land --repo …` line and the verdict with the run id, and returns the landing's
    rc; a refused tree returns 2 and moves nothing; a worktree with no `local` remote returns 1 and names the
    play that sets one. Guards the operator CLI's door end to end against real git, and the narration an
    operator reads at the terminal (the `$` line is the one every `_run` prints)."""
    monkeypatch.setattr(paths, "ROOT", box.wt)
    new = _commit(box.wt, {"k.txt": "k\n"}, "k")
    assert gitio.land_main(run_id="000000000042") == 0 and _main(box.bare) == new
    out = capsys.readouterr().out
    assert "$ kontroll-land --repo" in out and "landed refs/heads/main %s" % new in out and "(run_id 000000000042)" in out
    _git(box.wt, "remote", "set-url", "local", "file://" + box.bare.replace("\\", "/"))
    assert gitio._canonical_of(box.wt) is not None
    bad = _commit(box.wt, CONFLICT, "refused")
    assert gitio.land_main() == 2 and _main(box.bare) == new
    assert "acme.appliance" in capsys.readouterr().out
    _git(box.wt, "remote", "set-url", "local", "ssh://example.invalid/srv/kontroll.git")
    assert gitio._canonical_of(box.wt) is None and gitio.land_main() == 1
    assert "local-canonical.yml" in capsys.readouterr().out
    _git(box.wt, "remote", "set-url", "local", "../canonical.git")
    assert gitio._canonical_of(box.wt) == os.path.abspath(box.bare)


def test_commit_and_push_outside_staging_lands_on_main_and_never_pushes_it(box, monkeypatch, capsys):
    """`commit_and_push` with `KONTROLL_STAGE_PUSHES` unset (the GUI run from a checkout) commits and LANDS on the
    canonical's `main`: `pushed_canonical` True, `main` is the commit, and no `git push` ran for `main` (the
    canonical's hook — installed here — would refuse it from everyone). A refused tree answers `pushed_canonical`
    False with `main` untouched (the commit is in the worktree for the operator to correct). Guards the fifth
    door, the one the ruling's count of four did not name: with the hook refusing `main` from everyone, a push
    here would fail at every request."""
    monkeypatch.setattr(paths, "ROOT", box.wt)
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    shutil.copyfile(os.path.join(ROOT, "ansible", "playbooks", "files", "canonical-update-hook.sh"),
                    os.path.join(box.bare, "hooks", "update"))
    os.chmod(os.path.join(box.bare, "hooks", "update"), 0o755)
    _write(box.wt, {"state.yml": "remotes: [cli]\n"})
    out = gitio.commit_and_push(["state.yml"], ["operator edit"])
    assert out["committed"] and out["pushed_canonical"] is True and out["target_ref"] == "main" and out["staged"] is False
    assert _main(box.bare) == _git(box.wt, "rev-parse", "HEAD")
    said = capsys.readouterr().out
    assert "kontroll-land --repo" in said and "push local" not in said
    _write(box.wt, {"modules/beta/module.yml": _BETA_CONFLICT})
    out = gitio.commit_and_push(["modules/beta/module.yml"], ["a conflict"])
    assert out["committed"] and out["pushed_canonical"] is False
    assert _main(box.bare) != _git(box.wt, "rev-parse", "HEAD")


def _box_with_scripts(box):
    """The worktree as a node's checkout: this tree's `scripts/` and `modules/_core.yml` copied in and committed,
    so the shell doors run THEIR copy of `kontroll-land.py` (as a box does) and the gate judges the clean tree."""
    shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(box.wt, "scripts"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    _git(box.wt, "add", "-A")
    _git(box.wt, "commit", "-q", "-m", "the tool")
    _git(box.wt, "push", "-q", "local", "HEAD:refs/heads/main")
    box.seed = _git(box.wt, "rev-parse", "HEAD")
    return box


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("python3"),
                    reason="the shell doors run under bash with a `python3` on the PATH (Linux; the gate image)")
def test_update_sh_lands_through_the_gate_and_stops_on_a_refusal(box, tmp_path):
    """`scripts/update.sh` run on a box's checkout (an `origin` with a new clean commit): fetches, merges, and
    LANDS on the canonical — `main` is the merged commit, its output says so, no push ran (the hook, installed,
    would have refused one). With an upstream commit the gate refuses, the merge happens, the script exits 2
    with the refusal and the canonical's `main` stays — the sentence tells the operator the code is in the
    checkout only. Guards the first door end to end, as a shell script, with the exit status the operator reads."""
    _box_with_scripts(box)
    shutil.copyfile(os.path.join(ROOT, "ansible", "playbooks", "files", "canonical-update-hook.sh"),
                    os.path.join(box.bare, "hooks", "update"))
    os.chmod(os.path.join(box.bare, "hooks", "update"), 0o755)
    upstream = str(tmp_path / "upstream")
    subprocess.run(["git", "clone", "-q", box.wt, upstream], check=True, capture_output=True)
    _git(upstream, "config", "core.autocrlf", "false")
    _git(box.wt, "remote", "add", "origin", upstream)
    good = _commit(upstream, {"code.txt": "v2\n"}, "code update")
    p = subprocess.run(["bash", "scripts/update.sh"], cwd=box.wt, capture_output=True, text=True,
                       env=dict(os.environ, **_WHO))
    assert p.returncode == 0, p.stdout + p.stderr
    assert _main(box.bare) == _git(box.wt, "rev-parse", "HEAD") == good
    assert "Local canonical updated" in p.stdout and "landed refs/heads/main" in p.stdout and "push" not in p.stdout
    _commit(upstream, CONFLICT, "a code update the gate refuses")
    p = subprocess.run(["bash", "scripts/update.sh"], cwd=box.wt, capture_output=True, text=True,
                       env=dict(os.environ, **_WHO))
    said = p.stdout + p.stderr                               # scripts/lib/run-log.sh tees both streams into one log
    assert p.returncode == 2, said
    assert "REFUSED:" in said and "acme.appliance" in said and "checkout only" in said, said
    assert _main(box.bare) == good and _git(box.wt, "rev-parse", "HEAD") != good


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("python3"),
                    reason="the shell doors run under bash with a `python3` on the PATH (Linux; the gate image)")
def test_update_from_bundle_sh_lands_through_the_gate(box, tmp_path):
    """`scripts/update-from-bundle.sh` with a bundle of one new clean commit: verifies, fetches, merges and LANDS —
    `main` is the merged commit and no push ran; a bundle whose tree the gate refuses exits 2 with the refusal
    and leaves `main`. Guards the second door end to end: the remoteless box's update is the same landing."""
    _box_with_scripts(box)
    upstream = str(tmp_path / "upstream")
    subprocess.run(["git", "clone", "-q", box.wt, upstream], check=True, capture_output=True)
    _git(upstream, "config", "core.autocrlf", "false")
    good = _commit(upstream, {"code.txt": "v2\n"}, "code update")
    bundle = str(tmp_path / "k.bundle")
    _git(upstream, "bundle", "create", bundle, box.seed + "..main")
    p = subprocess.run(["bash", "scripts/update-from-bundle.sh", bundle], cwd=box.wt, capture_output=True,
                       text=True, env=dict(os.environ, **_WHO))
    assert p.returncode == 0, p.stdout + p.stderr
    assert _main(box.bare) == good and "landed refs/heads/main" in p.stdout
    _commit(upstream, CONFLICT, "refused")
    _git(upstream, "bundle", "create", bundle, good + "..main")
    p = subprocess.run(["bash", "scripts/update-from-bundle.sh", bundle], cwd=box.wt, capture_output=True,
                       text=True, env=dict(os.environ, **_WHO))
    assert p.returncode == 2 and "REFUSED:" in p.stdout + p.stderr and _main(box.bare) == good, p.stdout + p.stderr


def test_the_transaction_text_is_one_compare_and_swap_under_no_deref(box, monkeypatch):
    """The text handed to `git update-ref --stdin` is exactly `start`, `option no-deref`, `update <target> <new>
    <old>`, `prepare`, `commit` — for `main` with the commit `main` was read as, for a first seed with the zero
    id, for a proposal with the zero id. Guards two words a behavioural row cannot reach here: `option no-deref`
    (without it a `main` that turned symbolic AFTER the read would move the ref it points at — the promote's
    transaction carries the same option, `tests/unit/test_promote_cas.py`), and the old value on every path (the
    race row holds `main`'s; this holds the proposal's and the seed's)."""
    fed = []
    real = subprocess.run

    def spy(argv, *a, **kw):
        if list(argv[:3]) == ["git", "update-ref", "--stdin"]:
            fed.append(kw.get("input"))
        return real(argv, *a, **kw)
    monkeypatch.setattr(gitio.subprocess, "run", spy)
    new = _commit(box.wt, {"t.txt": "t\n"}, "t")
    assert gitio.land_commit(box.wt, box.bare, gate=lambda c, g, m: None)[0] == 0
    assert gitio.land_commit(box.wt, box.bare, target=canon.PROPOSED + RID, gate=lambda c, g, m: None)[0] == 1
    newer = _commit(box.wt, {"u.txt": "u\n"}, "u")
    assert gitio.land_commit(box.wt, box.bare, target=canon.PROPOSED + RID, gate=lambda c, g, m: None)[0] == 0
    empty = str(box.bare) + "-empty"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", empty], check=True, capture_output=True)
    assert gitio.land_commit(box.wt, empty, gate=lambda c, g, m: None)[0] == 0
    zero = "0" * len(new)
    assert fed == [
        ("start\noption no-deref\nupdate refs/heads/main %s %s\nprepare\ncommit\n" % (new, box.seed)).encode(),
        ("start\noption no-deref\nupdate %s %s %s\nprepare\ncommit\n" % (canon.PROPOSED + RID, newer, zero)).encode(),
        ("start\noption no-deref\nupdate refs/heads/main %s %s\nprepare\ncommit\n" % (newer, zero)).encode(),
    ], fed


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("python3"),
                    reason="runs the printed command by bash, from a path holding a quote")
def test_the_shallow_refusals_sync_clause_brings_the_worktree_forward_run_as_printed(tmp_path):
    """A `--depth 1` clone of the canonical, at a path that needs quoting, then a promote moves the canonical's main:
    the landing's shallow sentence ("it is not in the clone") names `python3 -B scripts/kontroll-land.py --sync
    --worktree … --repo …`, and that command, run by bash as printed from the checkout, brings the worktree forward;
    the landing is then up to date. Guards the remedy's words (rebase read A, decision c): `--sync`, the worktree and
    the canonical in that order, each quoted for the shell — a command that only reads right sends the operator to a
    second refusal."""
    src, made = _history(tmp_path)
    bare = _bare(tmp_path)
    _git(src, "push", "-q", bare, made[3] + ":refs/heads/main")
    wt = str(tmp_path / "it's a work tree")
    _shallow_clone(bare, wt, bare, 1)
    _git(src, "commit", "-q", "--allow-empty", "-m", "a promote")
    _git(src, "push", "-q", bare, "HEAD:refs/heads/main")
    rc, said = gitio.land_commit(wt, bare, gate=_no_gate())
    assert rc == 1 and "it is not in the clone" in said, said
    start = said.find("`python3 -B scripts/kontroll-land.py --sync ")
    assert start >= 0, said
    cmd = said[start + 1:said.index("`", start + 1)]
    p = subprocess.run(["bash", "-c", cmd], cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, **_WHO))
    assert p.returncode == 0 and "brought forward" in p.stdout, (cmd, p.stdout, p.stderr)
    rc, said = gitio.land_commit(wt, bare, gate=_no_gate())
    assert rc == 0 and said.startswith("up to date"), said
