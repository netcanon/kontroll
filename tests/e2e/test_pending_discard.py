"""E2E — the Pending panel's 'Discard' control on a STALE proposal (FF-race remediation, Move 2).

WHY: a proposal `main` advanced past can never be promoted (non-fast-forward); Move 1 offers re-propose, and this
reaps the dead ref so the list doesn't accrete un-promotable cruft. These prove the two load-bearing UI properties
in a REAL browser: (1) the Discard control is STALE-ONLY and gated behind a two-step confirm (a healthy proposal
shows no Discard, and a single click doesn't delete) — the availability-only griefing bound; (2) confirming POSTs
to the discard route and removes the row on success. Both `/api/pending` (an isolated stale proposal) and the
`POST …/discard` are mocked per-test with `page.route` so no canonical repo / real ref-delete happens — this tests
the GUI contract (C10-safe: un-stage, never promote — there is no promote control anywhere in the flow).
"""
import json

import pytest
from playwright.sync_api import expect

import _canned

pytestmark = pytest.mark.e2e


def _mock_pending(page, payload):
    page.route("**/api/pending", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))


def test_stale_proposal_discard_needs_confirm_then_removes_the_row(page):
    """A stale row shows a `pending-discard` button; clicking it does NOT delete (it reveals a two-step confirm) —
    only `pending-discard-confirm` POSTs to the discard route, and on success the row is removed. Proves the
    availability-only griefing bound (confirm-gated) + the C10-safe un-stage end to end (the POST hits …/discard, no
    promote control exists)."""
    _mock_pending(page, _canned.PENDING_STALE)
    posted = {"n": 0}
    def _fulfill_discard(route):
        posted["n"] += 1
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(_canned.DISCARD_ANSWER))
    page.route("**/api/pending/*/discard", _fulfill_discard)
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    row = page.get_by_test_id("pending-row").first
    expect(row.get_by_test_id("pending-discard")).to_be_visible()
    row.get_by_test_id("pending-discard").click()             # step 1: reveal confirm — must NOT delete yet
    expect(row.get_by_test_id("pending-discard-confirm")).to_be_visible()
    assert posted["n"] == 0, "a single Discard click must not POST (confirm required)"
    row.get_by_test_id("pending-discard-confirm").click()     # step 2: confirm → POST
    expect(page.get_by_test_id("pending-row")).to_have_count(0)   # the row is gone after a successful discard
    assert posted["n"] == 1


def test_discard_error_renders_inline_and_keeps_the_row(page):
    """A discard that the server refuses (e.g. 409 — the ref moved) renders `pending-discard-error` inline and LEAVES
    the row (nothing was deleted). Guards the compare-and-delete refusal surfacing — the operator sees why, and the
    proposal is still listed."""
    _mock_pending(page, _canned.PENDING_STALE)
    page.route("**/api/pending/*/discard", lambda route: route.fulfill(
        status=409, content_type="application/json", body=json.dumps(_canned.DISCARD_REFUSED)))
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    row = page.get_by_test_id("pending-row").first
    row.get_by_test_id("pending-discard").click()
    row.get_by_test_id("pending-discard-confirm").click()
    expect(row.get_by_test_id("pending-discard-error")).to_contain_text("moved or vanished")
    expect(page.get_by_test_id("pending-row")).to_have_count(1)   # still listed — nothing deleted


def test_discard_cancel_restores_the_button_without_posting(page):
    """The two-step confirm's escape hatch: after revealing the confirm, clicking `pending-discard-cancel` restores
    the un-confirmed Discard button and POSTs nothing. Guards the cancel path (a broken restore handler would trap
    the operator in the confirm or silently drop the affordance) — the availability-preserving half of the confirm
    gate."""
    _mock_pending(page, _canned.PENDING_STALE)
    posted = {"n": 0}
    page.route("**/api/pending/*/discard", lambda route: (posted.__setitem__("n", posted["n"] + 1),
               route.fulfill(status=200, content_type="application/json", body=json.dumps(_canned.DISCARD_ANSWER))))
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    row = page.get_by_test_id("pending-row").first
    row.get_by_test_id("pending-discard").click()
    expect(row.get_by_test_id("pending-discard-confirm")).to_be_visible()
    row.get_by_test_id("pending-discard-cancel").click()
    expect(row.get_by_test_id("pending-discard")).to_be_visible()     # the un-confirmed button is back
    expect(row.get_by_test_id("pending-discard-confirm")).to_have_count(0)
    expect(page.get_by_test_id("pending-row")).to_have_count(1)       # nothing deleted
    assert posted["n"] == 0, "cancel must not POST"


def test_promotable_proposal_has_no_discard(page):
    """A PROMOTABLE proposal shows no `pending-discard` (nor the stale badge) — discard is a stale-only reaper; a
    healthy fast-forwardable proposal must never be offered for deletion."""
    ok = json.loads(json.dumps(_canned.PENDING_STALE))
    ok["proposals"][0]["promotable"] = True
    _mock_pending(page, ok)
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    row = page.get_by_test_id("pending-row").first
    expect(row.get_by_test_id("pending-promote-hint")).to_be_visible()
    expect(row.get_by_test_id("pending-discard")).to_have_count(0)
