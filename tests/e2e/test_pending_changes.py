"""E2E — what a Pending row says a proposal changes, and the refs beside the proposals that are not proposals.

WHY: the Pending panel is what an operator reads before approving a change in Semaphore, so what it shows has to
be the proposal's own change, all of it, under names that are what was committed. These prove three things in a
REAL browser against the JSON shape `service/pending.list_pending` returns (mocked per test with `page.route`, so
no canonical repository is involved): a moved file is on the row at BOTH ends; a name that could not be shown as
written is on the row in its escaped spelling with a note saying so (and there is no such note on an ordinary
row); and a ref under `refs/heads/proposed/` that is not a proposal is listed in a card of its own that holds no
control at all — it can be seen, and nothing can be done to it from here. Selectors are `data-testid` only.
"""
import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

_SHA = "ab" * 20


def _payload(proposals, unrecognised=(), note=None, **more):
    out = {"proposals": list(proposals), "unrecognised": list(unrecognised), "canonical": "/srv/kontroll.git",
           "promote_task": "promote-proposal", "note": note, "note_kind": "empty" if note else None,
           "main_note": None, "skipped_note": None}
    out.update(more)
    return out


def _proposal(run_id, changes, promotable=True, changes_read=True):
    return {"run_id": run_id, "ref": "refs/heads/proposed/" + run_id, "sha": _SHA,
            "staged_at": "2026-07-05T00:00:00+00:00", "author": "kontroll", "subject": "feat: a change",
            "paths": [c["path"] for c in changes], "changes": changes, "changes_read": changes_read,
            "promotable": promotable, "repropose_hint": None}


def _open(page, payload):
    page.route("**/api/pending", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))
    page.goto("/")
    page.get_by_test_id("pending-open").click()


def test_a_moved_file_is_on_the_row_at_both_ends(page):
    """A proposal that moves a file shows `D <old name>` and `A <new name>` in `pending-paths`, with the commit id
    in full in the row's head, and no `pending-path-inexact` note. Guards the row an operator approves from: with
    the new name alone a proposal that takes an inventory file away read as one that only adds a file."""
    _open(page, _payload([_proposal("abc123def456", [
        {"status": "D", "path": "instance/inventory/old.yml", "exact": True},
        {"status": "A", "path": "instance/inventory/new.yml", "exact": True}])]))
    row = page.get_by_test_id("pending-row").first
    expect(row.get_by_test_id("pending-change")).to_have_text(
        ["D instance/inventory/old.yml", "A instance/inventory/new.yml"])
    expect(row.get_by_test_id("pending-head")).to_contain_text(_SHA)
    expect(row.get_by_test_id("pending-path-inexact")).to_have_count(0)
    expect(page.get_by_test_id("pending-unrecognised")).to_have_count(0)


def test_a_name_that_is_not_shown_as_written_is_marked(page):
    """A change whose name arrives escaped (`exact: false`) is on the row in that spelling, and the row carries the
    `pending-path-inexact` note; the proposal next to it, with ordinary names, carries none. Guards the difference
    between a file called `caf\\xe9.yml` and a file whose name holds a byte the page cannot show: without the note
    the escaped spelling reads as the name."""
    _open(page, _payload([
        _proposal("abc123def456", [{"status": "A", "path": "caf\\xe9.yml", "exact": False},
                                   {"status": "M", "path": "instance/fleet.yml", "exact": True}]),
        _proposal("def456abc789", [{"status": "M", "path": "instance/fleet.yml", "exact": True}])]))
    first, second = page.get_by_test_id("pending-row").nth(0), page.get_by_test_id("pending-row").nth(1)
    expect(first.get_by_test_id("pending-change")).to_have_text(["A caf\\xe9.yml", "M instance/fleet.yml"])
    expect(first.get_by_test_id("pending-path-inexact")).to_be_visible()
    expect(second.get_by_test_id("pending-change")).to_have_text(["M instance/fleet.yml"])
    expect(second.get_by_test_id("pending-path-inexact")).to_have_count(0)


def test_refs_that_are_not_proposals_are_shown_apart_with_no_control(page):
    """Two refs the service did not stage — one with a name that is not a run id, one symbolic — are listed in the
    `pending-unrecognised` card, each with its name, id and reason; the card holds no button (no copy, no promote
    hint, no discard), and the one real proposal keeps its own row and controls. Guards both halves of "listed
    apart": such a ref must be SEEN (nothing the service does creates one), and nothing on the page may treat it
    as a proposal."""
    _open(page, _payload(
        [_proposal("abc123def456", [{"status": "M", "path": "instance/fleet.yml", "exact": True}])],
        unrecognised=[{"ref": "refs/heads/proposed/run1", "sha": _SHA, "why": "its name is not a run id"},
                      {"ref": "refs/heads/proposed/bbbbbbbbbbbb", "sha": _SHA, "why": "it is a symbolic ref"}]))
    card = page.get_by_test_id("pending-unrecognised")
    expect(card).to_be_visible()
    rows = card.get_by_test_id("pending-unrecognised-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_have_text("refs/heads/proposed/run1 · " + _SHA + " — its name is not a run id")
    expect(rows.nth(1)).to_have_text("refs/heads/proposed/bbbbbbbbbbbb · " + _SHA + " — it is a symbolic ref")
    expect(card.locator("button")).to_have_count(0)
    expect(card.get_by_test_id("pending-copy-runid")).to_have_count(0)
    expect(card.get_by_test_id("pending-promote-hint")).to_have_count(0)
    expect(card.get_by_test_id("pending-discard")).to_have_count(0)
    expect(page.get_by_test_id("pending-row")).to_have_count(1)
    expect(page.get_by_test_id("pending-row").first.get_by_test_id("pending-copy-runid")).to_be_visible()


def test_such_refs_are_shown_even_when_nothing_is_pending(page):
    """With no proposal at all and one ref that is not a proposal, the panel shows the empty-state note AND the
    `pending-unrecognised` card. Guards the early return the empty state used to be: a canonical whose only ref
    under the prefix is one somebody else wrote would have read "no proposals pending" and nothing more."""
    _open(page, _payload([], note="no proposals pending",
                         unrecognised=[{"ref": "refs/heads/proposed/run1", "sha": _SHA,
                                        "why": "its name is not a run id"}]))
    expect(page.get_by_test_id("pending-empty")).to_have_text("no proposals pending")
    expect(page.get_by_test_id("pending-unrecognised-row")).to_have_count(1)
    expect(page.get_by_test_id("pending-row")).to_have_count(0)


def test_a_name_cannot_read_as_two_entries(page):
    """A file whose NAME is `a.yml, D instance/secrets/vault.yml` is one `pending-change` element with that whole
    text; the row has two such elements for its two files, not three. Guards the paint: joined into one line with
    commas, that name read as an added file AND a deletion that is not happening — an entry nobody committed,
    invented on the row an operator approves from."""
    _open(page, _payload([_proposal("abc123def456", [
        {"status": "A", "path": "a.yml, D instance/secrets/vault.yml", "exact": True},
        {"status": "M", "path": "instance/fleet.yml", "exact": True}])]))
    changes = page.get_by_test_id("pending-row").first.get_by_test_id("pending-change")
    expect(changes).to_have_count(2)
    expect(changes).to_have_text(["A a.yml, D instance/secrets/vault.yml", "M instance/fleet.yml"])


def test_a_row_that_could_not_be_read_says_so_and_offers_no_recipe(page):
    """Three rows: one whose change list could not be read (`changes_read` false), one whose fast-forward question
    has no answer (`promotable` null), one ordinary. The first carries `pending-changes-unread` and NO promote
    recipe; the second carries `pending-unknown-hint` in the recipe's place and no stale badge; the third has its
    recipe and neither warning. Guards the states the server detects as "not read": each used to be painted as a
    plain, promotable row — no change list, no badge, and the instruction to promote."""
    _open(page, _payload([
        _proposal("aaaaaaaaaaaa", [], changes_read=False),
        _proposal("bbbbbbbbbbbb", [], promotable=None, changes_read=False),
        _proposal("cccccccccccc", [{"status": "M", "path": "instance/fleet.yml", "exact": True}])]))
    unread, unknown, fine = (page.get_by_test_id("pending-row").nth(i) for i in range(3))
    expect(unread.get_by_test_id("pending-changes-unread")).to_be_visible()
    expect(unread.get_by_test_id("pending-promote-hint")).to_have_count(0)
    expect(unknown.get_by_test_id("pending-unknown-hint")).to_be_visible()
    expect(unknown.get_by_test_id("pending-promote-hint")).to_have_count(0)
    expect(unknown.get_by_test_id("pending-stale")).to_have_count(0)
    expect(fine.get_by_test_id("pending-promote-hint")).to_be_visible()
    expect(fine.get_by_test_id("pending-changes-unread")).to_have_count(0)
    expect(fine.get_by_test_id("pending-unknown-hint")).to_have_count(0)


def test_what_the_list_could_not_read_is_a_warning_above_it(page):
    """A `main_note` and a `skipped_note` from the server are each shown above the rows; and a canonical that could
    not be read (`note_kind` "unreadable") shows its note in the warning style, where an ordinary empty list shows
    it in the quiet one. Guards the panel against saying "nothing here" in the same grey for a queue that is empty
    and for one it was not able to read."""
    _open(page, _payload([_proposal("abc123def456", [], promotable=None, changes_read=False)],
                         main_note="main is not an ordinary branch", skipped_note="git left a ref out"))
    expect(page.get_by_test_id("pending-main-note")).to_have_text("main is not an ordinary branch")
    expect(page.get_by_test_id("pending-skipped-note")).to_have_text("git left a ref out")
    page.get_by_test_id("pending-close").click()

    page.unroute("**/api/pending")
    _open(page, _payload([], note="could not read the canonical (a reason)", note_kind="unreadable"))
    expect(page.get_by_test_id("pending-empty")).to_have_class("warn")
    page.get_by_test_id("pending-close").click()

    page.unroute("**/api/pending")
    _open(page, _payload([], note="no proposals pending"))
    expect(page.get_by_test_id("pending-empty")).to_have_class("meta")

