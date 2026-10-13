"""canon — how the tool reads the canonical, and the one environment a git that touches it runs in (C10).

These pin `scripts/kontroll/canon.py`, the module every reader of the canonical goes through. The canonical is a
bare repository the network service can write into — that is how it stages a proposal — so what is IN it is not
something a reader may trust: a ref of another name that a short name finds first, a ref that is a pointer to
another ref, and the files through which a repository tells git what else to read or to run. Each test below
plants one such thing with real git (tests/unit/_canon_repo.py) and asks `canon` the question it would ask of a
clean repository. Where the plain git of this host IS fooled, the test says so first (a control): a planted thing
that fools nobody would prove nothing about the seam.

The seam's entries that a planted file can show are tested one at a time, for the same reason they are listed
one at a time in `canon.git_env`: a later edit that drops one must fail the test that names it, not pass because
another entry happens to cover the case today. The rest are held by name in one structural test.
"""
import os
import re
import shutil
import subprocess
import uuid

import pytest

import yaml

import _canon_repo as R
import _readonly_pins
from _readonly_pins import GIT_READONLY_VERBS, assert_git_runs_in_the_seam
from api.audit import mint_run_id
from kontroll import canon

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Run ids every spelling of the rule must take, and strings every spelling must refuse. `mint_run_id()` and
# `uuid4().hex[:12]` are the two LIVE minters, so a change to either one's alphabet fails here.
_GOOD_IDS = ("abc123def456", "0" * 12, "20260705T101112Z-abcdef")
_BAD_IDS = ("-d", "--all", "-z", "../main", "main", "HEAD", "proposed/x", "a b", "abc123def45", "abc123def4567",
            "ABC123DEF456", "", "  ", "20260705T101112Z-xyz", "20260705T101112Z-ABCDEF", "20260705t101112z-abcdef",
            # the trailing-newline trap: `^…$` with a prefix match lets each of these through
            "abc123def456\n", "abc123def456\r", "abc123def456\t", "20260705T101112Z-abcdef\n", "abc123\ndef456",
            " abc123def456", "abc123def456 ", "refs/heads/proposed/abc123def456")


def _in_seam(bare, *argv, env=None):
    """Run git with the seam's environment (or a variant of it) and return the CompletedProcess."""
    return subprocess.run(["git"] + list(argv), capture_output=True,
                          **({"env": env} if env is not None else canon.git_kwargs(bare)))


# ---- the names ------------------------------------------------------------------------------------------------ #

def test_a_run_id_is_one_of_the_two_minted_shapes_and_nothing_else():
    """`valid_run_id` accepts exactly what the two minters make (the GUI's `uuid4().hex[:12]`, the API's
    `mint_run_id()`) and refuses a git option, a path, a branch name, upper case, a trailing newline, a value that
    is not a string. Guards the one check between a string from a request (or a Semaphore survey) and a ref name:
    only a validated id ever becomes `refs/heads/proposed/<id>`, and `proposal_ref` raises rather than build a name
    from anything else."""
    for good in _GOOD_IDS + (mint_run_id(), uuid.uuid4().hex[:12]):
        assert canon.valid_run_id(good), "must accept %r" % (good,)
        assert canon.proposal_ref(good) == "refs/heads/proposed/" + good
    for bad in _BAD_IDS + (None, 12, b"abc123def456"):
        assert not canon.valid_run_id(bad), "must refuse %r" % (bad,)
        with pytest.raises(ValueError):
            canon.proposal_ref(bad)


def test_the_promote_play_spells_the_run_id_rule_as_the_validator_does():
    """The promote play's assert takes `<run id>@<commit id>`, and its two halves accept and refuse the SAME
    samples as `canon.valid_run_id` and the shape of a full commit id; a bare run id is refused. The play's pattern
    is read out of the play's parsed assert and applied the way its `regex(…, match_type='fullmatch')` test
    applies it. Guards two spellings of one rule drifting apart — and the drift that was there: the play used
    `match` with `^…$`, which accepts an id followed by a newline (`$` matches before a final one), while the
    Python rule refuses it. The null commit id is the one sample the two halves treat differently by design —
    the play's pattern is a shape, and the command refuses the null id with its own sentence."""
    that = _promote_tasks()[0]["ansible.builtin.assert"]["that"]
    found = [m.group(1) for m in (re.fullmatch(r"run_id is regex\('([^']+)',\s+match_type='fullmatch'\)", line) for line in that) if m]
    assert len(found) == 1, "the play's run-id assert is not the one full-match test this reads: %r" % that
    assert not any("is match(" in line for line in that), "a prefix match of the run id is back in the play"
    pattern = re.compile(found[0])
    oid = "a" * 40
    for sample in _GOOD_IDS + _BAD_IDS + (mint_run_id(), uuid.uuid4().hex[:12]):
        assert bool(pattern.fullmatch(sample + "@" + oid)) == canon.valid_run_id(sample), \
            "the two rules differ on the run id %r" % sample
    for pin in ("a" * 40, "b" * 64, "a" * 39, "a" * 41, "A" * 40, "g" * 40, "", "a" * 40 + "\n", "a" * 40 + "@" + "a" * 40):
        assert bool(pattern.fullmatch("abc123def456@" + pin)) == bool(canon._OID.fullmatch(pin)), \
            "the two rules differ on the commit id %r" % pin
    assert not pattern.fullmatch("abc123def456"), "the job takes the pinned form only"


def test_an_object_id_is_full_lower_case_and_not_the_null_id():
    """`valid_oid` takes forty or sixty-four lower-case hex digits and refuses a short id, upper case, a trailing
    newline, and the all-zero id. Guards what is handed to `update-ref` as an OLD value: to that command the null
    id means "the ref must not exist", so a null id that got through would turn a compare-and-delete into a
    different question."""
    assert canon.valid_oid("a" * 40) and canon.valid_oid("0" * 39 + "1") and canon.valid_oid("b" * 64)
    for bad in ("a" * 39, "a" * 41, "A" * 40, "a" * 40 + "\n", "0" * 40, "0" * 64, "g" * 40, "", None, 7):
        assert not canon.valid_oid(bad), "must refuse %r" % (bad,)


def test_a_clean_canonical_reads_as_it_is(tmp_path):
    """In a repository with nothing planted: `read_ref` gives `main`'s commit id, its type and no symbolic target;
    a ref that does not exist is None; `proposals` gives the two staged proposals by run id and commit id and
    nothing apart; the ancestry question is True for the fast-forward and False for the one `main` moved past.
    The baseline every other test in this file compares a planted repository against."""
    r = R.build(tmp_path)
    assert canon.read_ref(r.bare, canon.MAIN) == (r.main, "commit", "")
    assert canon.read_ref(r.bare, "refs/heads/absent") is None
    assert canon.read_ref(r.bare, canon.proposal_ref(R.FF)) == (r.ff, "commit", "")
    found, apart, left_out = canon.proposals(r.bare)
    assert {f["run_id"]: f["oid"] for f in found} == {R.STALE: r.stale, R.FF: r.ff} and apart == []
    assert left_out is False
    assert next(f for f in found if f["run_id"] == R.FF)["subject"] == b"feat: ff one"
    assert next(f for f in found if f["run_id"] == R.FF)["when"] == 1750000000
    assert canon.is_ancestor(r.bare, r.main, r.ff) is True
    assert canon.is_ancestor(r.bare, r.main, r.stale) is False


def test_a_ref_is_read_by_its_full_name_beside_every_short_name_that_would_win(tmp_path):
    """Beside a tag `main`, a `refs/main`, a file `main` in the git directory, a tag `proposed/<id>` and a
    `refs/proposed/<id>` — each pointing somewhere else — `read_ref` and `proposals` return what they return in a
    clean repository. The control shows the planting is real: this host's `git rev-parse main` answers with the
    planted commit. Guards the lookup a short name gets: `main` is the tag before it is the branch, so a reader
    that asked for `main` judged, listed or compared a commit nobody proposed."""
    r = R.build(tmp_path)
    R.plant_short_names(r)
    assert R.git(r.bare, "rev-parse", "main").decode() == r.base, "control: the short name was expected to be fooled"
    assert R.git(r.bare, "rev-parse", "proposed/" + R.FF).decode() == r.base, "control: … and the proposal's too"
    assert canon.read_ref(r.bare, canon.MAIN) == (r.main, "commit", "")
    found, apart, left_out = canon.proposals(r.bare)
    assert {f["run_id"]: f["oid"] for f in found} == {R.STALE: r.stale, R.FF: r.ff} and apart == []


def test_a_ref_under_the_prefix_that_is_not_a_proposal_is_returned_apart(tmp_path):
    """Four refs under `refs/heads/proposed/` that the service never stages — a name that is not a run id, a
    nested name, a symbolic ref, and a ref that names a tag object — are each returned in `apart` with the reason,
    and none is in `found`. Guards both halves: such a ref is not a proposal (the listing must not offer it, the
    promote must not take it), and it is not hidden either — it is exactly what a direct writer of the repository
    leaves behind, so dropping it would hide the one thing worth seeing."""
    r = R.build(tmp_path)
    R.set_ref(r.bare, canon.PROPOSED + "run1", r.ff)
    R.set_ref(r.bare, canon.PROPOSED + "aaaaaaaaaaaa/nested", r.ff)
    R.write_ref_file(r.bare, canon.PROPOSED + "bbbbbbbbbbbb", "ref: " + canon.MAIN)
    tag = R.git(r.bare, "mktag", stdin=("object %s\ntype commit\ntag t\ntagger t <t@example.test> 1750000000 +0000"
                                        "\n\nt\n" % r.ff).encode()).decode()
    R.write_ref_file(r.bare, canon.PROPOSED + "cccccccccccc", tag)
    found, apart, left_out = canon.proposals(r.bare)
    assert sorted(f["run_id"] for f in found) == sorted([R.STALE, R.FF])
    why = {a["ref"]: a["why"] for a in apart}
    assert why == {b"refs/heads/proposed/run1": "its name is not a run id",
                   b"refs/heads/proposed/aaaaaaaaaaaa/nested": "its name is not a run id",
                   b"refs/heads/proposed/bbbbbbbbbbbb": "it is a symbolic ref",
                   b"refs/heads/proposed/cccccccccccc": "it does not name a commit"}
    # `for-each-ref` matches a pattern as a PREFIX: asked for the run id that only has a ref under it, the read
    # must say "no such ref", not hand back the nested ref's commit as if it were the proposal's.
    assert canon.read_ref(r.bare, canon.PROPOSED + "aaaaaaaaaaaa") is None


def test_read_ref_returns_the_refs_own_value_and_says_when_it_is_a_pointer(tmp_path):
    """For a branch that names a TAG object, `read_ref` returns the tag object's id and the type `tag` — not the
    commit the tag peels to. For a SYMBOLIC ref it returns the name it points at. Guards the two ways a ref stands
    for something other than its own value: a caller that compared-and-swapped on a peeled id would be comparing
    against a value the ref does not hold, and a caller that deleted "the proposal" through a symbolic ref would
    delete the ref it points at."""
    r = R.build(tmp_path)
    tag = R.git(r.bare, "mktag", stdin=("object %s\ntype commit\ntag t\ntagger t <t@example.test> 1750000000 +0000"
                                        "\n\nt\n" % r.ff).encode()).decode()
    R.write_ref_file(r.bare, canon.PROPOSED + "cccccccccccc", tag)
    R.write_ref_file(r.bare, canon.PROPOSED + "bbbbbbbbbbbb", "ref: " + canon.MAIN)
    assert tag != r.ff
    assert canon.read_ref(r.bare, canon.PROPOSED + "cccccccccccc") == (tag, "tag", "")
    assert canon.read_ref(r.bare, canon.PROPOSED + "bbbbbbbbbbbb") == (r.main, "commit", canon.MAIN)


def test_a_name_the_ref_store_gives_two_values_is_not_a_proposal(tmp_path):
    """A `packed-refs` written by hand that lists one proposal name TWICE, with two commits: the name is in `apart`
    ("git gives its name more than one value") and not among the proposals, while the ordinary proposals are still
    found. Control: this host's plain `for-each-ref` over the prefix prints the name twice. Guards the two ways the
    ref store is read agreeing: the listing WALKS the store, and a promote or a discard LOOKS ONE NAME UP — with
    such a file the walk showed a row for one commit while the look-up resolved the same name to the other, so
    the commit an operator read about was not the commit the name stood for."""
    r = R.build(tmp_path)
    name = canon.PROPOSED + "aaaaaaaaaaaa"
    with open(os.path.join(r.bare, "packed-refs"), "wb") as fh:
        fh.write(b"# pack-refs with: peeled fully-peeled sorted \n"
                 + ("%s %s\n%s %s\n" % (r.stale, name, r.ff, name)).encode("ascii"))
    listed = R.git(r.bare, "for-each-ref", "--format=%(refname)", canon.PROPOSED).split(b"\n")
    assert listed.count(name.encode()) == 2, "control: plain git was expected to print the name twice"
    found, apart, left_out = canon.proposals(r.bare)
    assert sorted(f["run_id"] for f in found) == sorted([R.STALE, R.FF])
    mine = [a for a in apart if a["ref"] == name.encode()]
    assert mine and all(a["why"] == "git gives its name more than one value" for a in mine)
    assert {a["oid"] for a in mine} == {r.stale, r.ff}


def test_a_proposal_whose_look_up_disagrees_with_the_walk_is_listed_apart(tmp_path, monkeypatch):
    """When the walk of the prefix prints ONE row for a run id and the look-up of that one name returns another
    commit, the ref is in `apart` ("git gives its name more than one value") and not among the proposals — and
    when the look-up cannot be made at all, `proposals` raises `Unreadable` instead of guessing. Simulated by
    answering the second read for one name with another commit (a `packed-refs` whose header claims an order it
    does not have does this for real, on some files). Guards the comparison itself: the test with the name listed
    twice is caught by the count alone, and a listing that kept a row whose name resolves elsewhere showed an
    operator one commit under an id that promotes another."""
    r = R.build(tmp_path)
    real = canon.read_ref

    def elsewhere(repo, name, timeout=5):
        return (r.base, "commit", "") if name == canon.PROPOSED + R.FF else real(repo, name, timeout)

    monkeypatch.setattr(canon, "read_ref", elsewhere)
    found, apart, left_out = canon.proposals(r.bare)
    assert [f["run_id"] for f in found] == [R.STALE]
    assert apart == [{"ref": (canon.PROPOSED + R.FF).encode(), "oid": r.ff,
                      "why": "git gives its name more than one value"}]

    def refused(repo, name, timeout=5):
        raise canon.Unreadable("planted between the walk and the look-up")

    monkeypatch.setattr(canon, "read_ref", refused)
    with pytest.raises(canon.Unreadable):
        canon.proposals(r.bare)


def test_git_is_new_enough_for_the_seam_to_mean_anything():
    """The git this suite runs is 2.31 or later. Guards the floor the seam stands on: its config overrides travel
    in `GIT_CONFIG_COUNT`, which an older git ignores without a word — every canary in this file would then be
    testing a seam that overrides nothing, and the first sign would be a hook that ran."""
    out = subprocess.run(["git", "version"], capture_output=True, text=True).stdout
    found = re.search(r"(\d+)\.(\d+)", out)
    assert found and (int(found.group(1)), int(found.group(2))) >= (2, 31), out


# ---- the seam, one entry at a time ---------------------------------------------------------------------------- #

def test_the_seam_inherits_nothing_that_names_a_repository_a_config_or_a_key(tmp_path, monkeypatch):
    """`git_env` carries the repository it was given and no variable of the caller's that could name another one,
    another config, or a secret: a caller's `GIT_DIR`, `GIT_CONFIG_PARAMETERS`, `GIT_SSH_COMMAND`, `HOME`, `PAGER`
    and `SOPS_AGE_KEY` are all absent, and only the program path, the temp directory and the locale are kept.
    Guards the inheritance half of the seam: the services run with a rich environment (the Semaphore runner holds
    the age key), and a git started with `os.environ` would take its orders from it."""
    for name, value in (("GIT_DIR", "/elsewhere"), ("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/x'"),
                        ("GIT_SSH_COMMAND", "x"), ("HOME", str(tmp_path)), ("PAGER", "x"), ("SOPS_AGE_KEY", "x"),
                        ("LANG", "C")):
        monkeypatch.setenv(name, value)
    env = canon.git_env("/srv/kontroll.git")
    assert env["GIT_DIR"] == env["GIT_COMMON_DIR"] == "/srv/kontroll.git"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert not {"GIT_CONFIG_PARAMETERS", "GIT_SSH_COMMAND", "HOME", "PAGER", "SOPS_AGE_KEY"} & set(env)
    assert env["LANG"] == "C"
    assert {k for k in env if not k.startswith("GIT_")} <= {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL"}
    assert canon.git_kwargs(str(tmp_path)) == {"env": canon.git_env(str(tmp_path))}


def test_the_seam_carries_every_entry_it_is_documented_to(tmp_path):
    """`git_env` holds exactly the entries below, with these values. Most of them have a test of their own in this
    file that plants the thing the entry answers. The rest cannot be shown by any verb the readers use today — a
    pager or an fsmonitor runs for porcelain with a terminal or an index, `protocol.allow` for a transport,
    `safe.directory` for a repository owned by another uid — and this row is what holds them: an entry dropped in
    an edit fails here by name, instead of surviving until the first caller that needed it."""
    repo = str(tmp_path / "c.git")
    env = canon.git_env(repo)
    n = int(env["GIT_CONFIG_COUNT"])
    config = {env["GIT_CONFIG_KEY_%d" % i]: env["GIT_CONFIG_VALUE_%d" % i] for i in range(n)}
    assert len(config) == n, "a config key is set twice"
    assert config == {
        "core.hooksPath": os.devnull, "core.fsmonitor": "false", "core.pager": "cat",
        "core.attributesFile": os.devnull, "core.commitGraph": "false", "log.showSignature": "false",
        "gc.auto": "0", "maintenance.auto": "false", "protocol.allow": "never",
        "advice.graftFileDeprecated": "false", "core.packedRefsTimeout": "1000",
        "core.filesRefLockTimeout": "100", "safe.directory": repo}
    git_vars = {k: v for k, v in env.items() if k.startswith("GIT_") and not k.startswith("GIT_CONFIG_KEY_")
                and not k.startswith("GIT_CONFIG_VALUE_")}
    assert git_vars == {
        "GIT_DIR": repo, "GIT_COMMON_DIR": repo, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": str(n), "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull,
        "GIT_SHALLOW_FILE": os.devnull, "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_LITERAL_PATHSPECS": "1",
        "GIT_COMMITTER_NAME": "kontroll", "GIT_COMMITTER_EMAIL": "kontroll@example.invalid"}


def test_no_hook_of_the_repository_runs_in_the_seam(tmp_path):
    """A `reference-transaction` hook in the repository's `hooks/`, and another in a directory its config names
    with `core.hooksPath`, do not run when `update-ref` deletes a ref in the seam. The control runs the same
    command outside the seam and the hook fires. Guards `core.hooksPath` in the seam: a hook of the canonical runs
    as whoever runs git, and the service can write that repository — so without the override a click on Discard
    ran a program the repository chose."""
    control, seam = R.build(tmp_path, "control"), R.build(tmp_path, "seam")
    canary_c, canary_s = tmp_path / "ran-control", tmp_path / "ran-seam"
    R.plant_hook(control, canary_c)
    R.git(control.bare, "update-ref", "-d", canon.PROPOSED + R.FF, control.ff)
    assert canary_c.exists(), "control: this host's git was expected to run the hook outside the seam"

    R.plant_hook(seam, canary_s)
    R.plant_hook(seam, canary_s, directory=str(tmp_path / "named-hooks"))
    p = _in_seam(seam.bare, "update-ref", "--no-deref", "-d", "--end-of-options", canon.PROPOSED + R.FF, seam.ff)
    assert p.returncode == 0 and canon.read_ref(seam.bare, canon.PROPOSED + R.FF) is None
    assert not canary_s.exists(), "a hook of the repository ran inside the seam"


def test_a_replace_ref_does_not_stand_in_for_a_commit_in_the_seam(tmp_path):
    """With a replace ref that makes the stale proposal's commit stand for the fast-forward one, the ancestry
    question about the stale commit is still False. Control: plain git answers yes. Guards
    `GIT_NO_REPLACE_OBJECTS`: a replace ref is a ref like any other, and it makes the fast-forward check — and
    every read of that commit — be about another commit."""
    r = R.build(tmp_path)
    R.plant_replace(r)
    assert R.rc(r.bare, "merge-base", "--is-ancestor", r.main, r.stale) == 0, "control: plain git was expected fooled"
    assert canon.is_ancestor(r.bare, r.main, r.stale) is False


def test_a_graft_line_does_not_rewrite_a_parent_in_the_seam(tmp_path):
    """With an `info/grafts` line giving the stale proposal `main` as its parent, the ancestry question is still
    False. Control: plain git answers yes. Guards `GIT_GRAFT_FILE` — switching replacements off does not switch
    grafts off, so this entry is the only thing that answers that file."""
    r = R.build(tmp_path)
    R.plant_graft(r)
    assert R.rc(r.bare, "merge-base", "--is-ancestor", r.main, r.stale) == 0, "control: plain git was expected fooled"
    assert canon.is_ancestor(r.bare, r.main, r.stale) is False
    assert _in_seam(r.bare, "for-each-ref", canon.MAIN).stderr == b"", "the seam makes git print a hint on every call"


def test_a_shallow_file_does_not_cut_a_commit_off_in_the_seam(tmp_path):
    """With a `shallow` file naming the fast-forward proposal — which cuts it off from its parent — `main` is still
    its ancestor. Control: plain git says it is not. Guards `GIT_SHALLOW_FILE`: without it a file in the
    repository's top directory makes every promotable proposal read as stale, and a promote refuse."""
    r = R.build(tmp_path)
    R.plant_shallow(r)
    assert R.rc(r.bare, "merge-base", "--is-ancestor", r.main, r.ff) == 1, "control: plain git was expected fooled"
    assert canon.is_ancestor(r.bare, r.main, r.ff) is True


def test_a_lying_commit_graph_is_not_consulted_in_the_seam(tmp_path):
    """With a commit-graph rewritten so that the fast-forward proposal's commit carries ANOTHER commit's root tree,
    a diff of `main` against the proposal — the listing's own question — still names the one file the proposal
    adds. Control: plain git, which takes a commit's tree from the graph, lists the other tree's files. The third
    step takes `GIT_SHALLOW_FILE` out of the seam and asks again — still right — because that entry happens to
    switch the graph off too (git treats the repository as shallow), and without this step a seam that lost
    `core.commitGraph=false` would pass; the last step shows that with neither entry the graph is read. Guards
    that key: the graph is a file under `objects/`, which the service must be able to write, and git reads it
    without checking it. (The lie is about the TREE on purpose: which git reads a commit's PARENTS from the graph
    for an ancestry question differs between the versions this suite runs on.)"""
    r = R.build(tmp_path)
    R.plant_lying_commit_graph(r, tree_of=(r.ff, R.git(r.bare, "rev-parse", r.stale + "^{tree}").decode()))
    diff = ("diff", "--no-renames", "--name-status", "-z", r.main, r.ff, "--")
    right = b"A\0ff.txt\0"
    assert R.git(r.bare, *diff) != right, "control: plain git was expected to take the tree from the graph"
    assert _in_seam(r.bare, *diff).stdout == right
    alone = _in_seam(r.bare, *diff, env=R.seam_without(r.bare, "GIT_SHALLOW_FILE"))
    assert alone.stdout == right, "core.commitGraph=false does not answer the graph by itself"
    neither = _in_seam(r.bare, *diff, env=R.seam_without(r.bare, "GIT_SHALLOW_FILE", "core.commitGraph"))
    assert neither.returncode == 0 and neither.stdout != right, "control: with neither entry the graph is read"


def test_a_repository_with_a_commondir_file_is_refused(tmp_path):
    """A `commondir` file in the repository names another directory for its config, objects and refs. The seam's
    `GIT_COMMON_DIR` keeps the config and the objects the repository's own (shown first, with the seam's
    environment used directly) — but git's ref store follows the file whatever the environment says (shown second:
    `main` reads as the planted repository's). So `git_kwargs` refuses the repository, and `read_ref` and
    `proposals` raise `Unreadable` instead of answering from somewhere else. Guards both the entry and the refusal:
    without the first, objects and config come from a directory a writer chose; without the second, a reader lists
    — and a writer moves — refs in that directory and reports success."""
    r, other = R.build(tmp_path), R.build(tmp_path, "planted")
    R.set_ref(other.bare, canon.MAIN, other.base)                  # the planted store's `main` is somewhere else
    only_real = R.blob(r.bare, b"only in the real repository\n")
    R.git(r.bare, "config", "kontroll.marker", "real")
    R.git(other.bare, "config", "kontroll.marker", "planted")
    env = canon.git_env(r.bare)
    R.plant_commondir(r, other.bare)

    assert _in_seam(r.bare, "cat-file", "-e", only_real, env=env).returncode == 0
    assert _in_seam(r.bare, "config", "--get", "kontroll.marker", env=env).stdout.strip() == b"real"
    without = R.seam_without(r.bare, "GIT_COMMON_DIR")
    assert _in_seam(r.bare, "cat-file", "-e", only_real, env=without).returncode != 0, "control: objects not planted"
    refs = _in_seam(r.bare, "for-each-ref", "--format=%(objectname)", canon.MAIN, env=env).stdout.strip().decode()
    assert refs == other.base, "control: git was expected to read refs through the planted file"

    with pytest.raises(canon.Unreadable, match="commondir"):
        canon.git_kwargs(r.bare)
    with pytest.raises(canon.Unreadable):
        canon.read_ref(r.bare, canon.MAIN)
    with pytest.raises(canon.Unreadable):
        canon.proposals(r.bare)
    assert canon.is_ancestor(r.bare, r.main, r.ff) is None


@pytest.mark.parametrize("part", ["refs", "packed-refs", "logs", "objects", "HEAD", "config"])
def test_a_repository_with_a_part_that_is_a_symbolic_link_is_refused(tmp_path, part):
    """A repository whose `refs`, `packed-refs`, `logs`, `objects`, `HEAD` or `config` is a symbolic link is refused
    by `git_kwargs` — `read_ref` and `proposals` raise `Unreadable` naming the part — and the same repository with
    the link replaced by what it pointed at is read. Guards the doors beside `commondir`: git reads refs through a
    linked `refs` or `packed-refs` exactly as it does through that file, and a delete rewrites `packed-refs` and
    removes a file under `logs` THROUGH the link — a write that lands wherever the link's author pointed it.
    (Skipped where this host cannot create a link.)"""
    try:
        os.symlink(str(tmp_path), str(tmp_path / "probe-link"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symbolic link")
    r, other = R.build(tmp_path), R.build(tmp_path, "elsewhere")
    if part == "packed-refs":
        R.git(other.bare, "pack-refs", "--all")
    os.makedirs(os.path.join(other.bare, "logs"), exist_ok=True)
    here, there = os.path.join(r.bare, part), os.path.join(other.bare, part)
    if os.path.isdir(here):
        for dirpath, _, names in os.walk(here):                # git's object files are read-only; Windows minds
            for name in names:
                os.chmod(os.path.join(dirpath, name), 0o644)
        shutil.rmtree(here)
    elif os.path.exists(here):
        os.remove(here)
    os.symlink(there, here, target_is_directory=os.path.isdir(there))
    with pytest.raises(canon.Unreadable, match="`%s` is a symbolic link" % part):
        canon.git_kwargs(r.bare)
    with pytest.raises(canon.Unreadable):
        canon.read_ref(r.bare, canon.MAIN)
    with pytest.raises(canon.Unreadable):
        canon.proposals(r.bare)
    os.remove(here)
    if os.path.isdir(there):
        shutil.copytree(there, here)
    else:
        shutil.copyfile(there, here)
    assert canon.read_ref(r.bare, canon.MAIN) is not None


def test_a_ref_git_leaves_out_is_reported_as_left_out(tmp_path):
    """A file under `refs/heads/proposed/` that holds no object name is not in the listing — git skips it — and
    `proposals` says so: `left_out` is True, where it is False for a clean repository. Guards the one thing the
    reader can still say about a ref it cannot show: without the flag the list reads as complete, and the file is
    exactly what somebody writing into the repository by hand leaves behind."""
    r = R.build(tmp_path)
    assert canon.proposals(r.bare)[2] is False
    R.write_ref_file(r.bare, canon.PROPOSED + "cccccccccccc", "not an object name")
    found, apart, left_out = canon.proposals(r.bare)
    assert left_out is True and sorted(f["run_id"] for f in found) == sorted([R.STALE, R.FF])
    assert not [a for a in apart if a["ref"].endswith(b"cccccccccccc")]


def test_no_program_a_config_names_is_started_by_a_read(tmp_path):
    """A repository whose config names a program under every key that can start one for a READ — a pager, an
    fsmonitor, an external diff, a text conversion and a filter (with an attributes file routing every path to
    them), a signature checker with `log.showSignature`, an alternate-refs command — and whose hooks directory
    holds every hook a read or a ref update can reach: `read_ref`, `proposals` and the ancestry check return what
    they return in a clean repository and no program ran. There is no control here on purpose: the verbs `canon`
    uses start none of these even outside the seam, so this test holds the VERBS, not the seam — it fails the day
    one of these three functions is rewritten onto a verb that starts a program. Which verbs a reader may run at
    all is held by the pin (`test_every_git_of_the_canonical_readers_runs_in_the_seam`)."""
    r = R.build(tmp_path)
    canary = tmp_path / "ran"
    prog = "sh -c 'echo ran >> %s'" % str(canary).replace("\\", "/")
    for key in ("core.pager", "core.fsmonitor", "diff.external", "diff.x.textconv", "filter.x.smudge",
                "filter.x.clean", "gpg.program", "core.alternateRefsCommand", "core.sshCommand", "core.editor"):
        R.git(r.bare, "config", key, prog)
    R.git(r.bare, "config", "log.showSignature", "true")
    os.makedirs(os.path.join(r.bare, "info"), exist_ok=True)
    with open(os.path.join(r.bare, "info", "attributes"), "w", encoding="ascii", newline="\n") as fh:
        fh.write("* filter=x diff=x\n")
    for hook in ("reference-transaction", "post-index-change", "pre-auto-gc"):
        R.plant_hook(r, canary, name=hook)
    assert canon.read_ref(r.bare, canon.MAIN) == (r.main, "commit", "")
    found, apart, left_out = canon.proposals(r.bare)
    assert {f["run_id"]: f["oid"] for f in found} == {R.STALE: r.stale, R.FF: r.ff} and apart == []
    assert canon.is_ancestor(r.bare, r.main, r.ff) is True and canon.is_ancestor(r.bare, r.main, r.stale) is False
    assert not canary.exists(), "a program named by the repository's config or hooks ran during a read"


def test_what_a_fast_forward_would_land_is_counted_and_listed_by_id(tmp_path):
    """`commits_between(main, proposal)` returns how many commits a fast-forward lands and the newest of them as
    `(commit id, subject bytes)` — one for the builder's fast-forward proposal, with a subject that is not UTF-8
    returned as its bytes; with a limit below the count the list is cut and the count is not; beside a tag called
    `main` and a replace ref the answer is the same; and names in place of ids, or an id of no object, raise
    `Unreadable`. Guards the promote's preview — the lines an approval is recorded by: they are asked of the two
    ids the promote read, in the seam, so they are the commits that land and not a range some other ref names."""
    r = R.build(tmp_path)
    assert canon.commits_between(r.bare, r.main, r.ff) == (1, [(r.ff, b"feat: ff one")])
    t = R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode()
    two = R.commit(r.bare, t, [r.ff], subject=b"subj \xff")
    assert canon.commits_between(r.bare, r.main, two) == (2, [(two, b"subj \xff"), (r.ff, b"feat: ff one")])
    assert canon.commits_between(r.bare, r.main, two, limit=1) == (2, [(two, b"subj \xff")])
    assert canon.commits_between(r.bare, r.main, r.main) == (0, [])
    R.plant_short_names(r)
    R.plant_replace(r)
    assert canon.commits_between(r.bare, r.main, r.ff) == (1, [(r.ff, b"feat: ff one")])
    for a, b in (("main", r.ff), (r.main, "refs/heads/proposed/" + R.FF), (r.main, "a" * 40)):
        with pytest.raises(canon.Unreadable):
            canon.commits_between(r.bare, a, b)


def test_a_repository_is_named_by_its_git_directory(tmp_path):
    """`git_dir` returns a bare repository's own path and `<path>/.git` for a checkout. Guards the one place a
    caller's "repository" becomes the directory the seam names to git: handed a checkout's root as `GIT_DIR`, git
    finds no repository, and every promote from a clone failed as "the canonical could not be read"."""
    r = R.build(tmp_path)
    assert canon.git_dir(r.bare) == r.bare
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, capture_output=True)
    assert canon.git_dir(str(work)) == os.path.join(str(work), ".git")
    assert canon.read_ref(canon.git_dir(str(work)), canon.MAIN) is None          # a repository git can read


def test_an_unreadable_canonical_is_said_not_guessed(tmp_path):
    """A directory that is not a repository makes `read_ref` and `proposals` raise `Unreadable`, and the ancestry
    check answer None — for ids that are not ids as well. Guards the difference between "there is no such ref"
    (None from `read_ref`, an empty list from `proposals`) and "the repository could not be read": a caller that
    took the second for the first would report an empty queue, or a missing proposal, on a broken canonical."""
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    with pytest.raises(canon.Unreadable):
        canon.read_ref(str(empty), canon.MAIN)
    with pytest.raises(canon.Unreadable):
        canon.proposals(str(empty))
    assert canon.is_ancestor(str(empty), "a" * 40, "b" * 40) is None
    r = R.build(tmp_path)
    assert canon.is_ancestor(r.bare, "main", r.ff) is None and canon.is_ancestor(r.bare, r.main, "0" * 40) is None
    assert canon.is_ancestor(r.bare, r.main, "a" * 40) is None            # an id that is no object of this repository


def test_the_seam_keeps_nothing_of_a_caller_that_git_or_its_helpers_would_obey(tmp_path, monkeypatch):
    """With every variable below set in the caller's environment, none of them is in `git_env`. Guards `_KEEP`
    against a name added "because git needs it": the sibling test sets six names and passes for any other that
    this host happens not to export, so an alternate object directory, an agent socket or an external-diff
    program kept by a later edit went unseen wherever the suite ran without it."""
    hostile = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_OBJECT_DIRECTORY", "GIT_INDEX_FILE", "GIT_WORK_TREE",
               "GIT_NAMESPACE", "GIT_EXEC_PATH", "GIT_TRACE", "GIT_ASKPASS", "GIT_EDITOR", "GIT_EXTERNAL_DIFF",
               "GIT_SSH", "GIT_PROXY_COMMAND", "GIT_CEILING_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_SYSTEM",
               "SSH_AUTH_SOCK", "SSH_ASKPASS", "GNUPGHOME", "XDG_CONFIG_HOME", "USERPROFILE", "HOMEDRIVE",
               "HOMEPATH", "EDITOR", "VISUAL", "LESS", "LD_PRELOAD", "PYTHONPATH")
    for name in hostile:
        monkeypatch.setenv(name, "planted")
    env = canon.git_env(str(tmp_path))
    assert [name for name in hostile if name in env] == []
    assert "planted" not in env.values()


def test_a_git_that_cannot_be_started_or_does_not_answer_is_unreadable_not_an_error(tmp_path, monkeypatch):
    """With no `git` on the path, and then with a git that runs past its time limit, `read_ref` and `proposals`
    raise `Unreadable` and the ancestry check answers None. Guards the two exceptions `subprocess` raises that are
    not about the repository at all: either one let out of `canon` is a traceback in the GUI worker where the
    callers handle exactly one exception type."""
    r = R.build(tmp_path)
    (tmp_path / "no-programs").mkdir()
    with monkeypatch.context() as m:
        m.setenv("PATH", str(tmp_path / "no-programs"))
        with pytest.raises(canon.Unreadable):
            canon.read_ref(r.bare, canon.MAIN)
        with pytest.raises(canon.Unreadable):
            canon.proposals(r.bare)
        assert canon.is_ancestor(r.bare, r.main, r.ff) is None
    assert canon.read_ref(r.bare, canon.MAIN) == (r.main, "commit", "")       # the control: git is back, so is the ref

    def too_slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(canon.subprocess, "run", too_slow)
    with pytest.raises(canon.Unreadable):
        canon.read_ref(r.bare, canon.MAIN)
    with pytest.raises(canon.Unreadable):
        canon.proposals(r.bare)
    assert canon.is_ancestor(r.bare, r.main, r.ff) is None


def _promote_tasks():
    """The tasks of the promote play, parsed — a commented-out line is not a task."""
    with open(os.path.join(ROOT, "ansible", "playbooks", "promote.yml"), encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    return [t for play in plays for t in play.get("tasks", [])]


def test_the_promote_play_checks_the_pinned_id_first_and_runs_no_git_of_its_own():
    """Read as YAML: the play's FIRST task is the assert that holds `run_id is defined` and the one full-match
    test of `<run id>@<commit id>`; its ONE command is the promote with `--require-pin`; and no raw `git` command
    is left in it, nor a sentence that sends a person to run git in the canonical. Guards three things: the assert
    being there as a task (commented out, its line still matches a text search); the pin being required on the
    job (a bare run id approves whatever the proposal is by then); and the preview — the play's own `git log` ran
    outside the seam, by short name, and recorded as "what was approved" a range the promote did not land. The
    promoter prints its own preview now, from the ids it uses."""
    tasks = _promote_tasks()
    first = tasks[0]["ansible.builtin.assert"]["that"]
    assert first[0] == "run_id is defined" and len(first) == 2
    assert re.fullmatch(r"run_id is regex\('([^']+)',\s+match_type='fullmatch'\)", first[1])
    commands = [t["ansible.builtin.command"]["cmd"] for t in tasks if "ansible.builtin.command" in t]
    assert [c for c in commands if c.split()[0] == "git"] == [], "a raw git command is back in the promote play"
    assert commands == ["python3 {{ repo_root }}/scripts/kontroll-promote.py {{ run_id }} --require-pin "
                        "--repo {{ canonical }}"]
    text = open(os.path.join(ROOT, "ansible", "playbooks", "promote.yml"), encoding="utf-8").read()
    assert "git -C" not in text, "the play tells a person to run git in the canonical by hand"


# ---- bytes to text -------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, text, exact", [
    (b"modules/cisco_ios/module.yml", "modules/cisco_ios/module.yml", True),
    ("dir/café.yml".encode("utf-8"), "dir/café.yml", True),
    (b'dir/"quoted".yml', 'dir/"quoted".yml', True),
    (b"a\\b.yml", "a\\b.yml", True),
    (b"", "", True),
    (b"caf\xe9.yml", "caf\\xe9.yml", False),                       # Latin-1, not UTF-8
    (b"new\nline.yml", "new\\nline.yml", False),
    (b"tab\there", "tab\\there", False),
    (b"esc\x1b[31m.yml", "esc\\x1b[31m.yml", False),
    ("rtl\u202e.yml".encode("utf-8"), "rtl\\u202e.yml", False),    # a direction override
    ("nb\u00a0sp".encode("utf-8"), "nb\\u00a0sp", False),          # a no-break space reads as a space
    (b"s\xadt", "s\\xadt", False),                                 # the BYTE 0xad, not UTF-8 …
    ("s\u00adt".encode("utf-8"), "s\\u00adt", False),              # … and the CHARACTER U+00AD: two spellings
    (b"a\\b\n", "a\\\\b\\n", False),                               # once escaped, a backslash is doubled
    (b"\xff\xfe", "\\xff\\xfe", False),
])
def test_show_gives_text_and_says_when_it_is_an_escaped_spelling(raw, text, exact):
    """`show` returns UTF-8 that prints as itself, exactly; anything else — a byte that is not UTF-8, a newline, a
    tab, an escape sequence, a direction override, a no-break space — comes back escaped, with `exact` False, and
    the escaped spelling reads one way only: a literal backslash is doubled, and a character in U+0080–U+00FF is
    spelled `\\u00NN` so that `\\xNN` always means a byte that was not UTF-8 (a soft hyphen and the stray byte
    0xad used to come out as the same text). Guards the last step of every
    listing: a name is whatever was committed, the panel paints it as text, and a name that could re-order or hide
    part of the line it is shown on must not be shown as if it were plain."""
    assert canon.show(raw) == (text, exact)
    assert canon.show(raw)[0].isprintable()


@pytest.mark.parametrize("raw, text", [(b" ", " "), (b"a ", "a "), (b" a.yml", " a.yml"), (b"a  b", "a  b")])
def test_show_calls_a_name_with_plain_spaces_exact(raw, text):
    """A name that is, starts with or ends in an ordinary space is returned as written, `exact` True. Guards a
    decision rather than a hole: such a name cannot be told from its trimmed twin on the page, and whichever way
    that is settled, a row has to hold it — a `show` that marked these inexact, or one that trimmed them, used to
    pass every test."""
    assert canon.show(raw) == (text, True)


# ---- the pin ---------------------------------------------------------------------------------------------------- #

def test_every_git_of_the_canonical_readers_runs_in_the_seam():
    """In `canon.py`, `service/pending.py` and `service/discard.py` every `subprocess` call is a git argv literal
    that passes `**canon.git_kwargs(…)` — and its verb is one of the verbs THAT module runs today: `for-each-ref`,
    `merge-base`, `cat-file` (the batch reader of one commit's objects) and `rev-list` (ids and subjects, for the
    promote's preview); `merge-base` and `diff`; `update-ref`. Guards the seam being a convention — a new call that
    inherits `os.environ`, names the repository with `-C`, or builds its verb at run time would run git on the
    canonical outside it — and guards its reach: the seam is proven for these verbs and for no others, so a reader
    rewritten onto another verb fails here, in the commit that adds the call, which is the moment to ask what the
    seam does for that verb."""
    assert_git_runs_in_the_seam("scripts/kontroll/canon.py",
                                verbs={"for-each-ref", "merge-base", "cat-file", "rev-list"})
    assert_git_runs_in_the_seam("scripts/kontroll/service/pending.py", verbs={"merge-base", "diff"})
    assert_git_runs_in_the_seam("scripts/kontroll/service/discard.py", verbs={"update-ref"})


@pytest.mark.parametrize("label, source, complaint", [
    ("no seam", "subprocess.run(['git', 'for-each-ref'], capture_output=True)", "git_kwargs"),
    ("own env", "subprocess.run(['git', 'for-each-ref'], env=os.environ)", "git_kwargs"),
    ("seam and an env", "subprocess.run(['git', 'for-each-ref'], env=os.environ, **canon.git_kwargs(repo))", "env beside"),
    ("seam and a cwd", "subprocess.run(['git', 'for-each-ref'], cwd=repo, **canon.git_kwargs(repo))", "cwd beside"),
    ("seam and -C", "subprocess.run(['git', '-C', repo, 'for-each-ref'], **canon.git_kwargs(repo))", "`-C`"),
    ("seam and -c", "subprocess.run(['git', '-c', 'x=y', 'for-each-ref'], **canon.git_kwargs(repo))", "`-c`"),
    ("a write verb", "subprocess.run(['git', 'update-ref', ref, oid], **canon.git_kwargs(repo))", "update-ref"),
    ("a built verb", "subprocess.run(['git', verb, ref], **canon.git_kwargs(repo))", "static"),
    ("a built argv", "subprocess.run(argv, **canon.git_kwargs(repo))", "literal"),
    ("a concatenation", "subprocess.run(['git'] + rest, **canon.git_kwargs(repo))", "literal"),
    ("a shell", "subprocess.run('git for-each-ref', shell=True, **canon.git_kwargs(repo))", "literal"),
    ("Popen", "subprocess.Popen(['git', 'cat-file', '--batch'])", "git_kwargs"),
    ("check_output", "subprocess.check_output(['git', 'log'])", "git_kwargs"),
    ("another kwargs", "subprocess.run(['git', 'for-each-ref'], **other.git_kwargs(repo))", "git_kwargs"),
])
def test_the_seam_pin_refuses_each_way_around_it(tmp_path, monkeypatch, label, source, complaint):
    """`assert_git_runs_in_the_seam` fails on each planted call: no seam, an environment of the caller's own, a
    repository named by `-C` or a key set by `-c` beside the seam, an `env` or a `cwd` passed next to it, a verb
    outside the allowed set, a verb or an argv built at run time, a shell string, and the same through `Popen` /
    `check_output`. Guards the pin itself —
    a pin that passes while blind is worse than none — and the one accepted call passes."""
    import _readonly_pins
    (tmp_path / "m.py").write_text(
        "import os\nimport subprocess\nfrom kontroll import canon\n\n\ndef f(repo, ref, oid, verb, argv, rest):\n"
        "    return %s\n" % source, encoding="utf-8")
    (tmp_path / "ok.py").write_text(
        "import subprocess\nfrom kontroll import canon\n\n\ndef f(repo):\n"
        "    return subprocess.run(['git', 'for-each-ref', '--format=x'], capture_output=True, timeout=5,\n"
        "                          **canon.git_kwargs(repo))\n", encoding="utf-8")
    monkeypatch.setattr(_readonly_pins, "ROOT", str(tmp_path))
    _readonly_pins.assert_git_runs_in_the_seam("ok.py", verbs=GIT_READONLY_VERBS)
    with pytest.raises(AssertionError, match=re.escape(complaint)):
        _readonly_pins.assert_git_runs_in_the_seam("m.py", verbs=GIT_READONLY_VERBS)


_CALL = "import os\nimport subprocess\nfrom kontroll import canon\n\n\ndef f(repo, extra):\n    return %s\n"


@pytest.mark.parametrize("label, module, complaint", [
    ("from subprocess import run",
     "from subprocess import run\nfrom kontroll import canon\n\n\ndef f(repo):\n"
     "    return run(['git', 'for-each-ref'], timeout=5, **canon.git_kwargs(repo))\n", "by its own name"),
    ("import subprocess as sp",
     "import subprocess as sp\nfrom kontroll import canon\n\n\ndef f(repo):\n"
     "    return sp.run(['git', 'for-each-ref'], timeout=5, **canon.git_kwargs(repo))\n", "under another name"),
    ("os.system", _CALL % "os.system('git for-each-ref')", "os.system starts a process"),
    ("os.popen", _CALL % "os.popen('git for-each-ref')", "os.popen starts a process"),
    ("os.execv", _CALL % "os.execv('/usr/bin/git', ['git', 'for-each-ref'])", "os.execv starts a process"),
    ("os.spawnv", _CALL % "os.spawnv(os.P_WAIT, '/usr/bin/git', ['git', 'for-each-ref'])", "os.spawnv starts"),
    ("os.posix_spawn", _CALL % "os.posix_spawn('/usr/bin/git', ['git', 'for-each-ref'], {})", "os.posix_spawn starts"),
    ("no call at all", "from kontroll import canon\n\n\ndef f(repo):\n    return canon.git_kwargs(repo)\n",
     "no subprocess call found"),
    ("a canon that is not kontroll's",
     "import subprocess\nfrom elsewhere import thing as canon\n\n\ndef f(repo):\n"
     "    return subprocess.run(['git', 'for-each-ref'], timeout=5, **canon.git_kwargs(repo))\n",
     "`from kontroll import canon` not found"),
    ("another program", _CALL % "subprocess.run(['sops', 'for-each-ref'], timeout=5, **canon.git_kwargs(repo))",
     "must open with a static"),
    ("subprocess.call", _CALL % "subprocess.call(['git', 'for-each-ref'], timeout=5)", "must pass **canon.git_kwargs"),
    ("subprocess.check_call", _CALL % "subprocess.check_call(['git', 'for-each-ref'], timeout=5)",
     "must pass **canon.git_kwargs"),
    ("subprocess.getoutput", _CALL % "subprocess.getoutput('git for-each-ref')", "one list literal"),
    ("seam and an executable",
     _CALL % "subprocess.run(['git', 'for-each-ref'], timeout=5, executable='/tmp/x', **canon.git_kwargs(repo))",
     "executable beside"),
    ("seam and shell=",
     _CALL % "subprocess.run(['git', 'for-each-ref'], timeout=5, shell=True, **canon.git_kwargs(repo))",
     "shell beside"),
    ("seam and a positional executable",
     _CALL % "subprocess.run(['git', 'for-each-ref'], -1, '/tmp/x', timeout=5, **canon.git_kwargs(repo))",
     "no other positional argument"),
    ("seam and a second **",
     _CALL % "subprocess.run(['git', 'for-each-ref'], timeout=5, **canon.git_kwargs(repo), **extra)",
     "no second **"),
    ("no timeout", _CALL % "subprocess.run(['git', 'for-each-ref'], **canon.git_kwargs(repo))", "carries a timeout"),
])
def test_the_seam_pin_refuses_the_ways_around_it_that_are_not_a_call(tmp_path, monkeypatch, label, module, complaint):
    """`assert_git_runs_in_the_seam` fails, with the message that names the fault, on: `subprocess` imported as
    names or under an alias, a process started through `os.system` / `os.popen` / `os.exec*` / `os.spawn*` /
    `os.posix_spawn`, a module with no call at all, a `canon` that is not `kontroll.canon`, a program that is not
    git, the other `subprocess` entry points, an `executable` or a `shell` beside the seam (as a keyword, as a
    positional argument, inside a second `**`), and a call with no time limit. Guards the half of the pin that
    the call-shaped rows do not reach: each of these assertions could be deleted, or never have worked, with every
    other row green — and "the pin holds every git of this module to the seam" is true only while they hold."""
    (tmp_path / "m.py").write_text(module, encoding="utf-8")
    monkeypatch.setattr(_readonly_pins, "ROOT", str(tmp_path))
    with pytest.raises(AssertionError, match=re.escape(complaint)):
        _readonly_pins.assert_git_runs_in_the_seam("m.py", verbs=GIT_READONLY_VERBS)


# ---- the preview's listing, the git directory, the play's own summary ---------------------------------------------- #

def _commit_with_no_message(r, parent, when):
    """A commit object whose message is empty — what `git commit --allow-empty-message -m ''` writes."""
    tree = R.git(r.bare, "rev-parse", r.ff + "^{tree}")
    body = (b"tree " + tree + b"\nparent " + parent.encode("ascii") + b"\nauthor t <t@example.test> " + when +
            b"\ncommitter t <t@example.test> " + when + b"\n\n")
    return R.git(r.bare, "hash-object", "-t", "commit", "-w", "--stdin", stdin=body).decode("ascii")


def test_a_commit_with_no_message_does_not_shift_the_listing(tmp_path):
    """Three commits ahead of `main` — the oldest with the SUBJECT `commit <forty hex digits>`, the middle one with
    no message at all, the newest an ordinary one: `commits_between` lists exactly those three ids, newest first,
    each with its own subject (the middle one's is empty). Guards the parse of `rev-list`'s output: git prints no
    subject line for a format that expands to nothing, so with `--format=%s` the middle commit's missing line
    shifted the pairs — the oldest commit's ID was returned as a subject, and the forty hex digits of its subject
    as the id of a commit that is not in the repository. Whoever can stage a proposal chose both."""
    r = R.build(tmp_path)
    tree = R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode()
    planted = "c0ffee" + "0" * 34
    oldest = R.commit(r.bare, tree, [r.main], subject=("commit " + planted).encode("ascii"), when=b"1750000001 +0000")
    middle = _commit_with_no_message(r, oldest, b"1750000002 +0000")
    newest = R.commit(r.bare, tree, [middle], subject=b"feat: the tip", when=b"1750000003 +0000")
    assert canon.commits_between(r.bare, r.main, newest) == (
        3, [(newest, b"feat: the tip"), (middle, b""), (oldest, ("commit " + planted).encode("ascii"))])
    assert canon.commits_between(r.bare, r.main, middle) == (2, [(middle, b""), (oldest, b"commit " + planted.encode())])


@pytest.mark.parametrize("key", ["i18n.logOutputEncoding", "i18n.commitEncoding"])
def test_an_output_encoding_named_by_the_repository_does_not_rewrite_a_subject(tmp_path, key):
    """With the repository's config naming UTF-16BE as the log output (or commit) encoding, and a proposal whose
    subject is characters chosen so that their UTF-16 bytes read `real subject`, a newline, `commit <forty f>`, a
    newline and another subject: `commits_between` returns ONE commit for the one commit there is, with the
    subject's bytes as they were committed — the same answer as without the key. Guards a key of the repository's
    config deciding what the promote prints as its evidence: `rev-list` re-encodes a formatted message to that
    encoding, so the listing came back with a second commit line that names no commit of the repository."""
    r = R.build(tmp_path)
    tree = R.git(r.bare, "rev-parse", r.ff + "^{tree}").decode()
    want = b"real subject\ncommit " + b"f" * 40 + b"\nfeat: looks like another commit "
    subject = want[:len(want) // 2 * 2].decode("utf-16-be").encode("utf-8")
    one = R.commit(r.bare, tree, [r.main], subject=subject)
    plain = canon.commits_between(r.bare, r.main, one)
    assert plain == (1, [(one, subject)])
    R.git(r.bare, "config", key, "UTF-16BE")
    assert canon.commits_between(r.bare, r.main, one) == plain


def test_a_bare_repository_is_its_own_git_directory_whatever_it_holds(tmp_path):
    """`git_dir` of a bare repository is the repository — also when it holds a directory called `.git`, empty or a
    repository of its own; and for a checkout whose `.git` is a FILE (a linked worktree) it returns the path it
    was given, which the seam then reports as unreadable. Guards the bare repository being asked FIRST: the
    canonical's top directory is writable by the service, and `<canonical>/.git` taken for the repository moved
    every promote into whatever that directory was."""
    r = R.build(tmp_path)
    os.makedirs(os.path.join(r.bare, ".git"))
    assert canon.git_dir(r.bare) == r.bare
    shutil.rmtree(os.path.join(r.bare, ".git"))
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", os.path.join(r.bare, ".git")], check=True,
                   capture_output=True)
    assert canon.git_dir(r.bare) == r.bare and canon.read_ref(canon.git_dir(r.bare), canon.MAIN)[0] == r.main
    work, linked = tmp_path / "work", tmp_path / "linked"
    subprocess.run(["git", "clone", "-q", R.build(tmp_path, "second").bare, str(work)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(work), "worktree", "add", "-q", "--detach", str(linked)], check=True,
                   capture_output=True)
    assert os.path.isfile(linked / ".git") and canon.git_dir(str(linked)) == str(linked)
    with pytest.raises(canon.Unreadable):
        canon.read_ref(canon.git_dir(str(linked)), canon.MAIN)


def test_the_listing_of_commits_with_no_git_or_a_slow_one_is_unreadable(tmp_path, monkeypatch):
    """With no `git` on the path, and with a git that runs past its time limit, `commits_between` raises
    `Unreadable`. Guards the promote's preview against the two exceptions `subprocess` raises that are not about
    the repository: the command handles `Unreadable` and nothing else, so either one let out is a traceback where
    a sentence and exit 1 were promised."""
    r = R.build(tmp_path)
    (tmp_path / "no-programs").mkdir()
    with monkeypatch.context() as m:
        m.setenv("PATH", str(tmp_path / "no-programs"))
        with pytest.raises(canon.Unreadable):
            canon.commits_between(r.bare, r.main, r.ff)

    def too_slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(canon.subprocess, "run", too_slow)
    with pytest.raises(canon.Unreadable):
        canon.commits_between(r.bare, r.main, r.ff)


def test_the_promote_play_reports_changed_by_the_promoters_own_line():
    """The play's one command registers its result and is `changed` only when the promoter's stdout holds
    `promoted`; the task after it prints that stdout. Guards the job's own summary: without `changed_when` every
    run of the command reads as a change, and without the register the preview — the record of what was approved
    — is not printed on a success."""
    tasks = _promote_tasks()
    command = next(t for t in tasks if "ansible.builtin.command" in t)
    assert command["register"] == "_promote" and command["changed_when"] == "'promoted' in _promote.stdout"
    assert tasks[-1]["ansible.builtin.debug"]["msg"] == "{{ _promote.stdout_lines }}"


@pytest.mark.parametrize("label, listing", [
    ("a record that names another commit than its header", "commit {a}\n{a}\0s\ncommit {b}\n{a}\0s\n"),
    ("a first record that is not the commit asked about", "commit {b}\n{b}\0s\ncommit {a}\n{a}\0s\n"),
    ("fewer records than the count", "commit {a}\n{a}\0s\n"),
    ("a pair that is no record, between two that are",
     "commit {a}\n{a}\0s\nsomething else\nx\0s\ncommit {b}\n{b}\0s\n"),
    ("a header with no record under it", "commit {a}\n{a}\0s\ncommit {b}\n"),
    ("a record with no separator", "commit {a}\n{a} s\ncommit {b}\n{b}\0s\n"),
    ("no final newline", "commit {a}\n{a}\0s\ncommit {b}\n{b}\0s"),
])
def test_a_listing_that_is_not_in_the_form_asked_for_is_unreadable(tmp_path, monkeypatch, label, listing):
    """`commits_between` handed, in place of git's answer, a listing that says two commits and then: a record
    whose own id is not its header's, a first record that is not the commit asked about, fewer records than the
    count, a pair that is no record standing between two that are, a header with nothing under it, a record without its separator, and output
    cut before its last newline — each raises `Unreadable`; the same call with the well-formed listing returns
    both commits. Guards the checks no real git reaches today: they are what makes the preview "the ids the
    promote lands" rather than "whatever came back", and each of them could be deleted with every row that runs
    git still green."""
    r = R.build(tmp_path)
    a, b = r.ff, r.main

    def fake(text):
        def run(argv, **kwargs):
            out = b"2\n" if "--count" in argv else text.format(a=a, b=b).encode("ascii")
            return subprocess.CompletedProcess(argv, 0, out, b"")
        return run

    monkeypatch.setattr(canon.subprocess, "run", fake("commit {a}\n{a}\0s one\ncommit {b}\n{b}\0s two\n"))
    assert canon.commits_between(r.bare, r.base, a) == (2, [(a, b"s one"), (b, b"s two")])
    monkeypatch.setattr(canon.subprocess, "run", fake(listing))
    with pytest.raises(canon.Unreadable):
        canon.commits_between(r.bare, r.base, a)
