"""pending — the read-only "proposals awaiting promotion" view (#129, gap 3 / C10).

These pin service/pending.py: list_pending() is a PURE read of the canonical bare repo's refs/heads/proposed/*
branches into {run_id, ref, sha, staged_at, author, subject, paths, changes}, newest-first, with the run_id taken
from the ref's full name (the value the operator pastes into the Semaphore promote-proposal survey). WHY (the gap
this fills): before this a staged proposal's run_id was visible only in the transient propose response — the
operator had no standing list to promote from. They also pin the C10 read-only guarantee: the lister NEVER promotes
(no promote/push path) — the proposer's read surface must not be the promoter.

And they pin HOW it reads, because the list is what a human reads before approving: by full name and by id (a tag
called `main` must not change what a proposal is compared with), in the seam (a file planted in the repository must
not change the answer), as bytes (a name that is not text is listed, escaped, never raised on), and with a moved
file at both of its ends. The repositories are built by tests/unit/_canon_repo.py.
"""
import ast
import inspect
import os
import re
import subprocess

import pytest

import _canon_repo as R
from _readonly_pins import assert_read_only
from kontroll import canon
from kontroll.service import pending

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True, text=True)


def _by_id(bare):
    """list_pending's proposals, keyed by run id."""
    return {p["run_id"]: p for p in pending.list_pending(canonical=bare)["proposals"]}


@pytest.fixture
def bare_with_proposals(tmp_path):
    """A throwaway CANONICAL: a bare repo with `main` + two proposed/<run_id> refs, each a 1-commit FF over main
    touching a file — so list_pending sees two proposals + their changed paths. A local filesystem path is used as
    the push target (cross-platform; no file:// URL). Returns the bare path."""
    bare = tmp_path / "canonical.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "seed.txt").write_text("base\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "push", str(bare), "HEAD:refs/heads/main")
    for rid, path in (("abc123def456", "modules/foo/module.yml"), ("def456abc789", "ansible/inventory/foo.yml")):
        (work / "p.txt").write_text(rid + "\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "feat(onboard): stage %s (run_id %s)" % (path, rid))
        _git(work, "push", str(bare), "HEAD:refs/heads/proposed/%s" % rid)
        _git(work, "reset", "--hard", "HEAD~1")                  # next proposal branches off main again
    return bare


def test_lists_proposals_with_runid_and_metadata(bare_with_proposals):
    """Each proposed/<run_id> ref appears once with the run_id taken from the ref's FULL name, the full name itself,
    the full commit id, and staged_at/subject; the run_id is exactly the string the Semaphore promote-proposal
    survey expects. Guards the parse of the one for-each-ref call the panel relies on (a wrong parse would feed the
    operator an un-promotable id) and the commit id being the whole one — it is what tells two proposals, or two
    states of one, apart."""
    out = pending.list_pending(canonical=str(bare_with_proposals))
    ids = {p["run_id"] for p in out["proposals"]}
    assert ids == {"abc123def456", "def456abc789"}
    p = next(x for x in out["proposals"] if x["run_id"] == "abc123def456")
    assert p["ref"] == "refs/heads/proposed/abc123def456" and p["staged_at"]
    assert canon.valid_oid(p["sha"]) and len(p["sha"]) == 40
    assert "feat(onboard)" in p["subject"] and p["author"] == "t"
    assert out["promote_task"] == "promote-proposal" and out["unrecognised"] == []


def test_proposal_reports_its_changed_paths(bare_with_proposals):
    """Each proposal carries what it changes — `changes`, one status and one NAME per file, and `paths`, the same
    names as a plain list — the read-only "what it changes" detail the row shows: the metadata analogue of a diff
    WITHOUT serving file contents (no C8 weight). Guards the changes-summary against drifting from the actual
    ref."""
    out = pending.list_pending(canonical=str(bare_with_proposals))
    for p in out["proposals"]:
        assert p["changes"] == [{"status": "A", "path": "p.txt", "exact": True}] and p["paths"] == ["p.txt"]
        assert p["changes_read"] is True
    assert out["main_note"] is None and out["skipped_note"] is None and out["note_kind"] is None
    # Every proposal carries the read-only re-propose hint field (None here — the fixture proposals aren't onboards);
    # pins the field's PRESENCE so the GUI can always read p.repropose_hint without a per-row existence check (Move 1).
    assert all("repropose_hint" in p for p in out["proposals"])
    assert all(p["repropose_hint"] is None for p in out["proposals"])


def test_repropose_hint_derives_the_class_from_an_onboard_dropin():
    """FF-race Move 1: `_repropose_hint` yields {kind:onboard, key} ONLY from an `onboarded-<key>.yml` path NAME —
    the single fact a ref's path names carry — and None for any non-onboard proposal shape. Guards the MF-2 boundary:
    the read view derives the CLASS to route the re-propose button, and NEVER reconstructs host/group/creds (those
    live inside the inventory YAML, not in a path name; the onboard dialog + the human own them). A drift that let a
    capability/homepage/secret proposal masquerade as an onboard — or that started parsing YAML for host/group —
    would fail here."""
    hint = pending._repropose_hint(["instance/inventory/onboarded-cisco_ios.yml", "modules/cisco_ios/module.yml",
                                     "config/fleet.yml"])
    assert hint == {"kind": "onboard", "key": "cisco_ios"}
    # a backslash path (a Windows-style name in the diff) still yields the leaf key
    assert pending._repropose_hint(["ansible\\inventory\\onboarded-arista_eos.yml"]) == {"kind": "onboard",
                                                                                          "key": "arista_eos"}
    # non-onboard shapes → None (the panel keeps the plain 'stale' prose, no button)
    assert pending._repropose_hint(["modules/foo/module.yml", "config/fleet.yml"]) is None   # capability-only
    assert pending._repropose_hint(["gui/homepage/overlay.yml"]) is None                     # homepage edit
    assert pending._repropose_hint(["onboarded-.yml"]) is None                               # empty key ⇒ no hint
    assert pending._repropose_hint([]) is None and pending._repropose_hint(None) is None


def test_promotable_flag_distinguishes_ff_from_stale(tmp_path):
    """`promotable` surfaces the fast-forward-only promote gate's verdict read-only: a proposal that is a clean FF of
    `main` reports True; one that `main` has advanced PAST (non-FF) reports False. Guards the exact FF-race the live
    Cisco dogfood hit — an earlier HIGH-blast proposal stranded when a later LOW-blast one promoted; the panel must
    badge it 'stale — re-propose' rather than list a dead run_id as promotable (which would refuse on promote)."""
    r = R.build(tmp_path)
    by = _by_id(r.bare)
    assert by[R.FF]["promotable"] is True              # a clean fast-forward of current main
    assert by[R.STALE]["promotable"] is False          # main advanced past it → non-FF (stale)


def test_a_stale_proposal_lists_its_own_change_not_mains(tmp_path):
    """A proposal `main` has moved past lists the file IT adds — against the commit it was staged on — and nothing
    of what `main` gained since. Guards the comparison point: against `main` itself the list of a stale proposal
    read `D adv.txt, A stale.txt`, showing `main`'s later change turned inside out as if the proposal deleted it —
    on the one row an operator is deciding whether to re-propose or discard."""
    r = R.build(tmp_path)
    by = _by_id(r.bare)
    assert by[R.STALE]["changes"] == [{"status": "A", "path": "stale.txt", "exact": True}]
    assert by[R.FF]["changes"] == [{"status": "A", "path": "ff.txt", "exact": True}]


def test_a_moved_file_is_listed_at_both_ends(tmp_path):
    """A proposal that moves a file (same content, another name) lists the old name as `D` and the new one as `A`.
    Guards the half a rename-following list hid: it printed the new name alone, so the row for a proposal that
    takes a file AWAY from where it was — an inventory file, a module file — showed only an addition."""
    r = R.build(tmp_path)
    seed, adv = R.blob(r.bare, b"base\n"), R.blob(r.bare, b"z\n")
    R.propose(r, "0000000000a1", {b"seed-moved.txt": seed, b"adv.txt": adv}, r.main)
    p = _by_id(r.bare)["0000000000a1"]
    assert sorted((c["status"], c["path"]) for c in p["changes"]) == [("A", "seed-moved.txt"), ("D", "seed.txt")]
    assert sorted(p["paths"]) == ["seed-moved.txt", "seed.txt"]


def test_names_that_are_not_plain_text_are_listed_escaped_and_marked(tmp_path):
    """A proposal whose tree holds a UTF-8 name, a name with a quote, a Latin-1 name (not UTF-8), a name with a
    newline in it and one with a direction override is listed with ALL five: the first two as written (`exact`
    True), the others as an escaped spelling with `exact` False — and nothing raises. Guards the queue against one
    name: a decode error used to take the whole panel down (and so hide every other proposal), git's default
    quoting showed such a name in a spelling of its own, and a name that re-orders the line it is painted on would
    be read as another name."""
    r = R.build(tmp_path)
    b = R.blob(r.bare, b"x\n")
    names = {"café.yml".encode("utf-8"): ("café.yml", True),
             b'"quoted".yml': ('"quoted".yml', True),
             b"caf\xe9.yml": ("caf\\xe9.yml", False),
             b"new\nline.yml": ("new\\nline.yml", False),
             "rtl\u202elmy.txt".encode("utf-8"): ("rtl\\u202elmy.txt", False)}
    entries = {n: b for n in names}
    entries[b"seed.txt"] = R.blob(r.bare, b"base\n")
    entries[b"adv.txt"] = R.blob(r.bare, b"z\n")
    R.propose(r, "0000000000b2", entries, r.main)
    p = _by_id(r.bare)["0000000000b2"]
    assert sorted((c["path"], c["exact"]) for c in p["changes"]) == sorted(names.values())
    assert all(c["status"] == "A" for c in p["changes"]) and sorted(p["paths"]) == sorted(t for t, _ in names.values())


def test_an_author_and_a_subject_that_are_not_text_do_not_cost_the_list(tmp_path):
    """A proposal whose commit carries a Latin-1 author name and a subject with a byte that is not UTF-8 is listed
    — escaped — beside the others. Guards the read that used to decode git's output on git's behalf: those two
    fields reach the caller as the bytes that were committed, the decode raised outside the handled errors, and the
    Pending panel answered with an error instead of the queue."""
    r = R.build(tmp_path)
    R.propose(r, "0000000000c3", {b"seed.txt": R.blob(r.bare, b"base\n"), b"n.txt": R.blob(r.bare, b"n\n")}, r.main,
              subject=b"subj \xff here", author=b"J\xf6rg")
    by = _by_id(r.bare)
    assert set(by) == {R.STALE, R.FF, "0000000000c3"}
    assert by["0000000000c3"]["author"] == "J\\xf6rg" and by["0000000000c3"]["subject"] == "subj \\xff here"


def test_the_list_is_the_same_beside_every_short_name_that_would_win(tmp_path):
    """Beside a tag `main`, a `refs/main`, a file `main` in the git directory, a tag `proposed/<id>` and a
    `refs/proposed/<id>`, the list is what it is in a clean repository: the same run ids, the same commit ids, the
    same changes, the same promotable flags. Guards the short names the list used to hand to git — `main..<ref>`
    was the TAG's range beside a tag of that name, so a row showed no change (or another commit's), and a short ref
    name printed as `heads/proposed/<id>` became the run id an operator was told to paste."""
    clean, planted = R.build(tmp_path, "clean"), R.build(tmp_path, "planted")
    R.plant_short_names(planted)
    want, got = _by_id(clean.bare), _by_id(planted.bare)
    assert got == want and set(got) == {R.STALE, R.FF}
    assert got[R.FF]["sha"] == planted.ff and got[R.FF]["promotable"] is True and got[R.FF]["paths"] == ["ff.txt"]
    assert pending.list_pending(canonical=planted.bare)["unrecognised"] == []


@pytest.mark.parametrize("plant", ["replace", "graft", "shallow", "graph-parent", "graph-tree"])
def test_the_list_is_the_same_whatever_store_is_planted(tmp_path, plant):
    """With a replace ref, a graft line, a `shallow` file, or a commit-graph that lies about a commit's parent or
    about its tree planted in the repository, the list is what it is in a clean one. Guards the seam where the
    operator reads it: each of these files makes plain git answer about another commit — a stale proposal reads as
    promotable, a promotable one as stale, a row lists another tree's files — and the list is what a human approves
    from. (The seam's entries are tested one at a time, each with its control, in test_canon.py; this is the reader
    they exist for. The parent lie fools some versions of git and not others; it is planted here all the same.)"""
    clean, r = R.build(tmp_path, "clean"), R.build(tmp_path, "planted")
    if plant == "replace":
        R.plant_replace(r)
    elif plant == "graft":
        R.plant_graft(r)
    elif plant == "shallow":
        R.plant_shallow(r)
    elif plant == "graph-parent":
        R.plant_lying_commit_graph(r, parentless=r.ff)
    else:
        R.plant_lying_commit_graph(r, tree_of=(r.ff, R.git(r.bare, "rev-parse", r.stale + "^{tree}").decode()))
    got = _by_id(r.bare)
    assert got == _by_id(clean.bare)
    # … and the clean list is the right one: two lists that are wrong in the same way are equal too
    assert got[R.FF]["promotable"] is True and got[R.FF]["paths"] == ["ff.txt"]
    assert got[R.STALE]["promotable"] is False and got[R.STALE]["paths"] == ["stale.txt"]


def test_refs_that_are_not_proposals_are_listed_apart(tmp_path):
    """A ref under `refs/heads/proposed/` whose name is not a run id, and one that is a symbolic ref to `main`, are
    in `unrecognised` — with the name, the commit and the reason — and not among the proposals; the two real
    proposals are still listed. Guards both directions: neither ref may be offered for promotion (a symbolic one
    would name `main` itself), and neither may vanish from the panel — nothing the service does creates them, so
    their presence is the news."""
    r = R.build(tmp_path)
    R.set_ref(r.bare, canon.PROPOSED + "run1", r.ff)
    R.write_ref_file(r.bare, canon.PROPOSED + "bbbbbbbbbbbb", "ref: " + canon.MAIN)
    out = pending.list_pending(canonical=r.bare)
    assert {p["run_id"] for p in out["proposals"]} == {R.STALE, R.FF}
    assert sorted((u["ref"], u["sha"], u["why"]) for u in out["unrecognised"]) == [
        ("refs/heads/proposed/bbbbbbbbbbbb", r.main, "it is a symbolic ref"),
        ("refs/heads/proposed/run1", r.ff, "its name is not a run id")]


def test_a_main_that_is_not_a_branch_makes_nothing_promotable(tmp_path):
    """When `refs/heads/main` is a symbolic ref (here: to a proposal), or is not there, no proposal is called
    promotable or stale and none lists a change — the flag is None, `changes_read` is False — and `main_note` says
    why. Guards the comparison against a `main` that is not what a promote would compare with: following the
    pointer would badge proposals against whatever it names; and guards the SILENCE — rows with no badge and no
    change list read as proposals that are fine and change nothing."""
    r = R.build(tmp_path)
    os.remove(os.path.join(r.bare, "refs", "heads", "main"))
    R.write_ref_file(r.bare, canon.MAIN, "ref: " + canon.PROPOSED + R.FF)
    out = pending.list_pending(canonical=r.bare)
    by = {p["run_id"]: p for p in out["proposals"]}
    assert set(by) == {R.STALE, R.FF}
    assert all(p["promotable"] is None and p["changes"] == [] and p["changes_read"] is False for p in by.values())
    assert out["main_note"] and "not an ordinary branch" in out["main_note"]
    os.remove(os.path.join(r.bare, "refs", "heads", "main"))                 # and with no `main` at all
    gone = pending.list_pending(canonical=r.bare)
    assert gone["main_note"] and all(p["promotable"] is None for p in gone["proposals"])


def _gitlink_proposal(r, run_id):
    """`main` gains a submodule entry `sub`; the proposal `run_id` moves it to another commit. Returns nothing —
    the change the list must show is `M sub`."""
    seed, adv = R.blob(r.bare, b"base\n"), R.blob(r.bare, b"z\n")
    files = [(b"100644", b"adv.txt", adv), (b"100644", b"seed.txt", seed)]
    with_link = R.commit(r.bare, R.raw_tree(r.bare, files + [(b"160000", b"sub", r.base)]), [r.main])
    R.set_ref(r.bare, canon.MAIN, with_link)
    moved = R.commit(r.bare, R.raw_tree(r.bare, files + [(b"160000", b"sub", r.stale)]), [with_link])
    R.set_ref(r.bare, canon.PROPOSED + run_id, moved)
    return with_link, moved


def _plain_diff_in_the_seam(bare, base_id, oid):
    """The list's own diff WITHOUT `--ignore-submodules=none`, in the seam — the control for the tests below."""
    return subprocess.run(["git", "diff", "--no-renames", "--name-status", "-z", base_id, oid, "--"],
                          capture_output=True, **canon.git_kwargs(bare))


@pytest.mark.parametrize("planted", ["a config key", "a corrupt index", "a .gitmodules in the caller's directory"])
def test_a_changed_submodule_entry_is_listed_whatever_is_planted(tmp_path, monkeypatch, planted):
    """A proposal that moves a submodule entry lists `M sub` — with `diff.ignoreSubmodules = all` in the
    repository's config, beside a corrupt `index` file in the repository, and with a `.gitmodules` saying `ignore =
    all` in the directory the CALLER happens to run in. Control for each: the same diff without
    `--ignore-submodules=none` prints nothing, or fails. Guards the change list being the two trees' own: for a
    changed submodule entry git consults the config, the index and — because the seam leaves it no work tree but
    the caller's directory — a `.gitmodules` there, so one of three files could take an entry off the row an
    operator approves from, the last of them without anything planted in the repository."""
    r = R.build(tmp_path)
    base, moved = _gitlink_proposal(r, "0000000000d4")
    if planted == "a config key":
        R.git(r.bare, "config", "diff.ignoreSubmodules", "all")
    elif planted == "a corrupt index":
        with open(os.path.join(r.bare, "index"), "wb") as fh:
            fh.write(b"not an index")
    else:
        elsewhere = tmp_path / "cwd"
        elsewhere.mkdir()
        (elsewhere / ".gitmodules").write_text('[submodule "sub"]' + "\n\tpath = sub\n\turl = x\n\tignore = all\n",
                                               encoding="utf-8")
        monkeypatch.chdir(elsewhere)
    control = _plain_diff_in_the_seam(r.bare, base, moved)
    assert control.returncode != 0 or control.stdout == b"", "control: without the flag the entry was expected lost"
    row = _by_id(r.bare)["0000000000d4"]
    assert row["changes"] == [{"status": "M", "path": "sub", "exact": True}] and row["changes_read"] is True


def test_a_change_list_that_cannot_be_read_is_not_an_empty_one(tmp_path):
    """A proposal whose commit names a tree the repository does not hold is listed — it is a fast-forward of
    `main` — with `changes: []` and `changes_read: False`; an ordinary proposal beside it has `changes_read: True`.
    Guards the difference between "this proposal changes nothing" and "what it changes could not be read": the
    two used to be the same empty list, on a row that then looked promotable and harmless."""
    r = R.build(tmp_path)
    broken = R.commit(r.bare, "ab" * 20, [r.main], subject=b"a commit whose tree is not there")
    R.set_ref(r.bare, canon.PROPOSED + "0000000000e5", broken)
    by = _by_id(r.bare)
    assert by["0000000000e5"]["changes"] == [] and by["0000000000e5"]["changes_read"] is False
    assert by["0000000000e5"]["promotable"] is True
    assert by[R.FF]["changes_read"] is True and by[R.FF]["changes"]
    assert pending._changes(r.bare, "main", r.ff) is None and pending._merge_base(r.bare, r.main, "HEAD") is None


def test_a_ref_git_left_out_and_refs_that_are_not_proposals_are_each_said(tmp_path):
    """A file under the prefix that holds no object name makes the list carry `skipped_note`; and a canonical
    with NO proposal but a ref that is not one gets a note that says so, not the usual "this deploy may commit
    directly to main". Guards the two sentences that explain an emptiness with a reason that is not the reason:
    a list missing a ref git could not read looked complete, and "no proposals pending (…not armed)" stood above
    a card of refs somebody had written under the prefix."""
    r = R.build(tmp_path)
    assert pending.list_pending(canonical=r.bare)["skipped_note"] is None
    R.write_ref_file(r.bare, canon.PROPOSED + "cccccccccccc", "not an object name")
    out = pending.list_pending(canonical=r.bare)
    assert out["skipped_note"] and "left out" in out["skipped_note"] and len(out["proposals"]) == 2

    bare = tmp_path / "only-apart.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    c = R.commit(str(bare), R.tree(str(bare), {b"x": R.blob(str(bare), b"x\n")}))
    R.set_ref(str(bare), canon.MAIN, c)
    R.set_ref(str(bare), canon.PROPOSED + "run1", c)
    out = pending.list_pending(canonical=str(bare))
    assert out["proposals"] == [] and len(out["unrecognised"]) == 1 and out["note_kind"] == "empty"
    assert "no proposals pending" in out["note"], "the empty state is still said when something is listed apart"
    assert "refs that are not proposals" in out["note"] and "armed" not in out["note"]


def _commit_at(r, name, committed, authored=None):
    """A one-file commit on `main` whose committer stamp is `committed` (`<seconds> <zone>`) — the builder's own
    commits all carry one stamp, which is no use to a test about order."""
    files = {b"seed.txt": R.blob(r.bare, b"base\n"), b"adv.txt": R.blob(r.bare, b"z\n"), name: R.blob(r.bare, b"q\n")}
    return R.commit(r.bare, R.tree(r.bare, files), [r.main], subject=b"x", when=committed, authored=authored or committed)


def test_the_list_is_newest_first_by_when_each_proposal_was_committed(tmp_path):
    """Three proposals committed a hundred seconds apart are listed newest first, ahead of the builder's two older
    ones, and every `staged_at` is an ISO-8601 stamp. The run ids are chosen so that the order of the NAMES is the
    reverse of the wanted one, and each commit's AUTHOR date runs the other way. Guards the one promise of the
    list nothing else holds: an unsorted list (git's own order is by name), a list sorted the wrong way, and a
    `staged_at` taken from the author date — which the proposal's author sets freely — all read "newest first" in
    the docstring and put the oldest proposal on top."""
    r = R.build(tmp_path)
    for rid, name, committed, authored in (("00000000000a", b"a.txt", b"1750000100 +0000", b"1750009000 +0000"),
                                           ("00000000000b", b"b.txt", b"1750000200 +0000", b"1750008000 +0000"),
                                           ("00000000000c", b"c.txt", b"1750000300 +0000", b"1750007000 +0000")):
        R.set_ref(r.bare, canon.PROPOSED + rid, _commit_at(r, name, committed, authored))
    out = pending.list_pending(canonical=r.bare)["proposals"]
    assert [p["run_id"] for p in out][:3] == ["00000000000c", "00000000000b", "00000000000a"]
    assert {p["run_id"] for p in out[3:]} == {R.STALE, R.FF}
    assert out[0]["staged_at"] == "2025-06-15T15:11:40Z"
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(Z|[+-]\d\d:\d\d)", p["staged_at"]) for p in out)


def test_the_list_orders_moments_not_the_spelling_of_a_stamp(tmp_path):
    """A proposal committed one hour AFTER another, by a committer whose clock is twelve hours west, is listed
    first. Guards the sort key: `staged_at` is the stamp in the committer's own zone, and sorted as a string
    `…T04:06:40-12:00` comes before `…T15:06:40Z` although it is the later moment — so whoever commits with a
    western zone sinks to the bottom of the queue, and an eastern one floats to the top."""
    r = R.build(tmp_path)
    R.set_ref(r.bare, canon.PROPOSED + "00000000000a", _commit_at(r, b"a.txt", b"1750000100 +0000"))
    R.set_ref(r.bare, canon.PROPOSED + "00000000000b", _commit_at(r, b"b.txt", b"1750003700 -1200"))
    out = pending.list_pending(canonical=r.bare)["proposals"]
    assert [p["run_id"] for p in out][:2] == ["00000000000b", "00000000000a"]
    assert out[0]["staged_at"] < out[1]["staged_at"], "the fixture no longer has stamps that sort wrongly as text"


def test_the_change_list_is_asked_for_with_the_flags_no_behaviour_can_show():
    """The one `git diff` of the lister is the argv literal `git diff --no-ext-diff --no-textconv --no-renames
    --ignore-submodules=none --name-status -z <base> <proposal> --`, read from the source. `--no-renames`, `-z`
    and the submodule flag have behaviour tests of their own; the other three have none and can have none —
    `--name-status` starts no external diff and no text conversion on any git this suite runs on, and two full
    object ids are never ambiguous in a bare repository — so a later edit that drops one passes every test and is
    missed until the day the call grows a patch, when a program named by the repository's config would start.
    Guards those three, and the call staying a list of names."""
    lists = [n for n in ast.walk(ast.parse(inspect.getsource(pending._changes))) if isinstance(n, ast.List)]
    argvs = [n for n in lists if [getattr(e, "value", None) for e in n.elts[:2]] == ["git", "diff"]]
    assert len(argvs) == 1, "the lister runs one `git diff`, as one list literal"
    assert [e.value if isinstance(e, ast.Constant) else None for e in argvs[0].elts] == [
        "git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=none",
        "--name-status", "-z", None, None, "--"]


def test_an_absent_canonical_says_so_and_a_directory_that_is_not_one_says_that(tmp_path):
    """A path that does not exist yields the note "canonical repo not present"; a directory that is not a
    repository yields "could not read the canonical". Guards the two notes staying two: an unarmed deploy (no
    canonical at all — nothing is wrong) must not read like a canonical that is there and cannot be read."""
    absent = pending.list_pending(canonical=str(tmp_path / "does-not-exist"))
    assert absent["proposals"] == [] and "not present" in absent["note"] and "could not read" not in absent["note"]
    (tmp_path / "plain-dir").mkdir()
    plain = pending.list_pending(canonical=str(tmp_path / "plain-dir"))
    assert "could not read the canonical" in plain["note"] and "not present" not in plain["note"]


@pytest.mark.parametrize("answers", [4, 5, 6])
def test_a_canonical_refused_after_the_refs_were_read_still_lists_them(tmp_path, monkeypatch, answers):
    """When the seam refuses the repository AFTER the refs were read — a `commondir` file planted between two git
    processes of one listing — the proposals are still listed, none with a change it could not read shown as read,
    and nothing raises. Simulated by letting `git_kwargs` answer `answers` times and refuse from then on: after
    the ref reads (every per-proposal question is refused, so `promotable` is None), after the first ancestry
    check (the merge base is refused), after the first merge base (the change list is refused). Guards the
    `Unreadable` each of the per-proposal reads catches: the refusal is a check before every process, so it can
    land on any of them, and the lister promises never to raise."""
    r = R.build(tmp_path)
    real, calls = canon.git_kwargs, []

    def refuses_later(repo):
        calls.append(repo)
        if len(calls) > answers:
            raise canon.Unreadable("planted between two reads")
        return real(repo)

    monkeypatch.setattr(canon, "git_kwargs", refuses_later)
    out = pending.list_pending(canonical=r.bare)
    assert {p["run_id"] for p in out["proposals"]} == {R.STALE, R.FF} and len(calls) > answers
    assert all(p["changes"] == [] and p["changes_read"] is False for p in out["proposals"])
    assert [p["promotable"] for p in out["proposals"]].count(None) == (2 if answers == 4 else 1)


def test_a_ref_listed_apart_whose_name_is_not_text_is_shown_escaped(tmp_path):
    """Two refs under `refs/heads/proposed/` whose names are a Latin-1 byte string and a name with a direction
    override — written into `packed-refs`, which takes any bytes — are listed apart in their escaped spellings, and
    the answer is still JSON-able text. Guards the name of a ref nobody staged: it is the one field of the panel
    whose author is by definition not the service, a ref name may hold any byte above 0x20, and a decode there
    either raised (costing the whole list) or painted a name that re-orders its own line."""
    r = R.build(tmp_path)
    with open(os.path.join(r.bare, "packed-refs"), "ab") as fh:
        fh.write(r.ff.encode("ascii") + b" refs/heads/proposed/caf\xe9\n")
        fh.write(r.ff.encode("ascii") + b" refs/heads/proposed/" + "rtl\u202elmy".encode("utf-8") + b"\n")
    out = pending.list_pending(canonical=r.bare)
    assert {p["run_id"] for p in out["proposals"]} == {R.STALE, R.FF}
    assert sorted((u["ref"], u["sha"], u["why"]) for u in out["unrecognised"]) == [
        ("refs/heads/proposed/caf\\xe9", r.ff, "its name is not a run id"),
        ("refs/heads/proposed/rtl\\u202elmy", r.ff, "its name is not a run id")]
    assert all(u["ref"].isprintable() for u in out["unrecognised"])


def test_the_list_has_one_budget_not_one_per_proposal(tmp_path, monkeypatch):
    """With the list's time budget already spent, every proposal is still LISTED — run id, commit, subject — with
    `promotable` None and `changes_read` False, and no per-proposal git is started for it. Guards the worker that
    serves the panel: each git has its own short timeout, but those add up per proposal, and a canonical that
    makes each of them wait would otherwise hold the request for their sum."""
    r = R.build(tmp_path)
    monkeypatch.setattr(pending, "LIST_SECONDS", 0)
    started = []
    monkeypatch.setattr(pending, "_merge_base", lambda *a: started.append(a))
    rows = pending.list_pending(canonical=r.bare)["proposals"]
    assert {p["run_id"] for p in rows} == {R.STALE, R.FF} and started == []
    assert all(p["promotable"] is None and p["changes_read"] is False and p["sha"] for p in rows)


def test_a_canonical_with_a_commondir_file_is_reported_not_read(tmp_path):
    """A canonical that holds a `commondir` file yields no proposals and a note that says the repository could not
    be read and why — never the refs of the directory the file names. Guards the refusal in the seam where a
    person meets it: an empty list with the usual "no proposals pending" note would be a queue hidden behind one
    planted file."""
    r, other = R.build(tmp_path), R.build(tmp_path, "planted")
    R.plant_commondir(r, other.bare)
    out = pending.list_pending(canonical=r.bare)
    assert out["proposals"] == [] and out["unrecognised"] == []
    assert "could not read the canonical" in out["note"] and "commondir" in out["note"]
    assert out["note_kind"] == "unreadable"


def test_empty_and_absent_canonical_degrade_never_raise(tmp_path):
    """An empty canonical (a bare repo with no proposed/* refs) → proposals == [] + an informational note; an
    absent path → likewise [] + a note; a directory that is not a repository → [] + a note that it could not be
    read (never an exception, never a crash) — and `note_kind` tells the three apart, so the panel can paint the
    last one as a warning and not as "nothing here". Guards the service-never-sys.exit / degrade-gracefully
    posture: a fresh/unarmed deploy shows an honest empty state, not a 500."""
    bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    empty = pending.list_pending(canonical=str(bare))
    assert empty["proposals"] == [] and empty["note"] and empty["note_kind"] == "empty"
    assert empty["main_note"] is None, "a fresh canonical with nothing staged is not a warning"
    absent = pending.list_pending(canonical=str(tmp_path / "does-not-exist"))
    assert absent["proposals"] == [] and absent["note"] and absent["note_kind"] == "absent"
    (tmp_path / "plain-dir").mkdir()
    plain = pending.list_pending(canonical=str(tmp_path / "plain-dir"))
    assert plain["proposals"] == [] and "could not read the canonical" in plain["note"]
    assert plain["note_kind"] == "unreadable"


def test_pending_is_read_only_by_construction():
    """C10 two-key: the Pending lister must NEVER gain a promote/push affordance — the proposer's read surface is
    not the promoter. The AST pin (#133) asserts list_pending calls no write/actuation verb; an additional source
    grep asserts pending.py names no git-write subcommand (a `git push`/`update-ref` would slip past a call-name
    check since it rides a subprocess arg list). Guards a future edit quietly adding a 'promote from the GUI' path
    that would collapse the propose→promote split. (That every git it runs is a read-only verb IN THE SEAM is
    pinned in test_canon.py.)"""
    assert_read_only("scripts/kontroll/service/pending.py", "list_pending")
    # Grep the EXECUTABLE code (docstring stripped — it legitimately names the forbidden helpers to document the
    # C10 contract). ast.unparse drops the module docstring + comments, leaving code + string/identifier literals.
    tree = ast.parse(inspect.getsource(pending))
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        tree.body = tree.body[1:]                              # drop the module docstring node
    code = ast.unparse(tree)
    for forbidden in ("promote_ref", "commit_and_push", "update-ref", '"push"', "'push'", "text=True"):
        assert forbidden not in code, "pending.py must not hold this in code: %s" % forbidden


def test_template_wires_the_pending_panel():
    """The header exposes a `pending-open` button → `openPendingPanel()` which fetches `/api/pending` and renders
    `pending-row`s with a `pending-copy-runid` affordance + a static `pending-promote-hint` (NEVER a promote
    trigger). Pins the GUI round-trip + the C10 no-promote-button property at the template level (testids recorded
    in testid_reference.md)."""
    tpl = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert 'data-testid="pending-open"' in tpl and "openPendingPanel()" in tpl and "/api/pending" in tpl
    assert "'pending-row'" in tpl and "'pending-copy-runid'" in tpl and "'pending-promote-hint'" in tpl
    assert "cannot promote" in tpl                              # the hint states the GUI cannot promote (C10)
    # the FF-race surface: a non-FF proposal is badged 'stale — re-propose' + gets a re-propose hint (not the recipe)
    assert "'pending-stale'" in tpl and "'pending-stale-hint'" in tpl and "p.promotable === false" in tpl
    # FF-race Move 1: a stale onboard proposal (repropose_hint) gets a `pending-re-propose` button that navigates to
    # the class's onboard dialog (reproposeFromPending → search). It is gated on stale + a hint, and NEVER promotes.
    assert "'pending-re-propose'" in tpl and "function reproposeFromPending" in tpl
    assert "stale && p.repropose_hint" in tpl                   # button only on a stale proposal that HAS a hint
    assert "reproposeFromPending" in tpl and "promote_ref" not in tpl and "/api/promote" not in tpl   # no promote path
    # FF-race Move 2: a stale row gets a Discard button (stale-only) + a two-step confirm → discardProposal POSTs to
    # the discard route. Un-stage is not promote — no promote path in the discard flow.
    assert "'pending-discard'" in tpl and "'pending-discard-confirm'" in tpl and "'pending-discard-error'" in tpl
    assert "'pending-discard-cancel'" in tpl                    # the confirm escape hatch (grep-verified; e2e-exercised)
    assert "function discardProposal" in tpl and "/discard'" in tpl and "method: 'POST'" in tpl
    assert "if (stale) {" in tpl                                # the discard control is gated on stale
    # what a row changes is painted from `changes` (status + name, both ends of a move), with a mark for a name
    # that is not shown as written; refs that are not proposals get a card of their own and no control
    assert "'pending-paths'" in tpl and "p.changes" in tpl and "'pending-path-inexact'" in tpl
    assert "'pending-change'" in tpl and "'pending-changes-unread'" in tpl and "p.changes_read" in tpl
    assert "'pending-unknown-hint'" in tpl and "p.promotable === null" in tpl
    assert "'pending-main-note'" in tpl and "'pending-skipped-note'" in tpl and "data.note_kind" in tpl
    assert "'pending-unrecognised'" in tpl and "'pending-unrecognised-row'" in tpl and "data.unrecognised" in tpl
    assert "if (apart.length) body.appendChild(pendingApart(apart));" in tpl      # … and the card is put on the page
