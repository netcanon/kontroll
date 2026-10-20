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
import shutil
import subprocess
import sys

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


@pytest.fixture
def box(tmp_path):
    """A node in miniature: a bare canonical (`bare`) whose `main` holds the clean tree, and the operator's
    worktree (`wt`) — a clone of it with a `local` remote, as `local-canonical.yml` leaves one — at that same
    commit (`seed`). `core.autocrlf` is off in the worktree so a tree hashes the same on every host."""
    bare = str(tmp_path / "canonical.git")
    wt = str(tmp_path / "worktree")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True, capture_output=True)
    _git(wt, "config", "core.autocrlf", "false")
    _git(wt, "config", "user.name", "t")                           # `commit_and_push`'s own `git commit` needs one
    _git(wt, "config", "user.email", "t@example.test")
    seed = _commit(wt, CLEAN, "seed")
    _git(wt, "remote", "add", "local", bare)
    _git(wt, "push", "-q", "local", "HEAD:refs/heads/main")       # the fixture's seed: the bare carries no hook yet
    return type("Box", (), {"bare": bare, "wt": wt, "seed": seed})()


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


def test_a_canonical_that_has_commits_the_worktree_does_not_is_refused_with_the_plays_sentence(box):
    """A promote moved the canonical's `main` to a commit this worktree has never fetched (rc 128 from
    `merge-base`: not a valid object here), and — a second case — the worktree holds a commit that is not a
    descendant of `main` (rc 1): both are refused (rc 1) with the sentence the play printed, which names the
    remedy (`fetch local && rebase local/main`) and never `--force`; `main` is untouched and nothing was packed.
    Guards the never-discard rule: a landing that forced, or that took rc 128 for "no main", would delete every
    promoted proposal."""
    promoted = R.commit(box.bare, _git(box.bare, "rev-parse", box.seed + "^{tree}"), [box.seed], subject=b"a promote")
    R.set_ref(box.bare, canon.MAIN, promoted)
    _commit(box.wt, {"c.txt": "c\n"}, "the operator's own change")
    rc, said = gitio.land_commit(box.wt, box.bare)
    assert rc == 1 and _main(box.bare) == promoted, said
    assert "has commit(s) your worktree" in said and "rebase local/main" in said and "--force" not in said
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
    `update-ref`, and a `pack-objects`, appear in no other function than those three and the promote's. Guards the
    canonical's side of the landing against a git that inherited the caller's environment (it would run the
    canonical's config-named programs, as whoever lands — root in the installer), and against a second, unpinned
    copy of the write appearing elsewhere in the module."""
    for verb in ("land_commit", "_land_in_canonical", "_pack", "land_main"):
        assert verb in WRITE_VERBS, verb
    assert_git_runs_in_the_seam("scripts/kontroll/gitio.py", verbs={"index-pack", "rev-list", "update-ref"},
                                functions=("_land_in_canonical",))
    tree = ast.parse(open(os.path.join(ROOT, "scripts", "kontroll", "gitio.py"), encoding="utf-8").read())
    allowed = {"index-pack": {"_land_in_canonical"}, "update-ref": {"_land_in_canonical", "promote_commit"},
               "pack-objects": {"_pack"}}
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
    `ansible/playbooks/local-canonical.yml` (which also has no `git push`, no `ls-remote` and no `fetch` task), and
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
    assert sum("kontroll-land.py" in cmd(t) for t in tasks) == 2, "the play runs the landing twice: --check, then for real"


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
