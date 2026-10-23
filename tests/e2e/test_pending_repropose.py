"""E2E — the Pending panel's 'Re-propose' button on a STALE proposal (FF-race remediation, Move 1).

WHY: when the base moves under a staged proposal (the auto-promoter promoted a later LOW-blast one), that proposal
becomes non-FF and its promote is silently REFUSED. Part 1 (#144) badges it 'stale'; this proves the follow-through
in a REAL browser — the stale row exposes a `pending-re-propose` button that NAVIGATES the operator back to the
proposal's device class (the class key is all a ref's path names yield), landing them on the SAME onboard dialog to
re-run it against current main. It is a navigation affordance, NOT an actuation: no promote/stage happens on click
(C10 two-key intact — the button only closes the panel, sets a context banner, and runs the class search). The
proposal list is mocked per-test with `page.route('**/api/pending', …)` (an isolated stale onboard proposal) so no
canonical repo / real staging is needed; the class card comes from the conftest shallow-search mock (cisco.ios).
"""
import json

import pytest
from playwright.sync_api import expect

import _canned

pytestmark = pytest.mark.e2e


def _mock_pending(page, payload):
    page.route("**/api/pending", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))


def test_stale_proposal_offers_re_propose_to_the_class(page):
    """A stale onboard proposal renders the `pending-stale` badge AND a `pending-re-propose` button; clicking it
    closes the Pending panel, shows the re-propose context banner, and lands the operator on the proposal's device
    class (its onboard card + form appear, `field-key` = the class key). Proves Move 1's navigation end to end — the
    stale hint's prose ("re-run the same onboard dialog") wired to a working button. The onboard form is pre-ROUTED,
    NOT pre-filled with host/creds (a path name yields only the class; the human re-enters the rest — apply stays
    unchecked)."""
    _mock_pending(page, _canned.PENDING_STALE_ONBOARD)
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    row = page.get_by_test_id("pending-row").first
    expect(row.get_by_test_id("pending-stale")).to_be_visible()
    expect(row.get_by_test_id("pending-re-propose")).to_be_visible()
    row.get_by_test_id("pending-re-propose").click()
    # the panel closed, the context banner appeared, and the class search ran → the onboard card is now on the page
    expect(page.get_by_test_id("pending-panel")).to_have_count(0)
    expect(page.get_by_test_id("discovery-prefill-banner")).to_contain_text("Re-proposing cisco_ios")
    page.get_by_test_id("onboard-toggle").first.click()
    form = page.get_by_test_id("onboard-form").first
    expect(form.get_by_test_id("field-key")).to_have_value("cisco_ios")         # routed to the right class
    expect(form.get_by_test_id("field-apply")).not_to_be_checked()              # dry-run first (the human floor, C10)


def test_promotable_proposal_has_no_re_propose_button(page):
    """A PROMOTABLE (fast-forwardable) proposal shows neither the stale badge NOR a re-propose button — it gets the
    normal Semaphore promote-hint. Guards the gate: re-propose is a STALE-only affordance (a healthy proposal must
    not be nudged to re-propose)."""
    ok = json.loads(json.dumps(_canned.PENDING_STALE_ONBOARD))
    ok["proposals"][0]["promotable"] = True
    _mock_pending(page, ok)
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    row = page.get_by_test_id("pending-row").first
    expect(row.get_by_test_id("pending-promote-hint")).to_be_visible()
    expect(row.get_by_test_id("pending-re-propose")).to_have_count(0)
    expect(row.get_by_test_id("pending-stale")).to_have_count(0)
