"""The keygen dialog (D), end-to-end in a headless browser over the REAL Flask app. The hermetic auth + the
no-leak / show-once contract are covered by tests/integration/test_gui_api.py (keygen) + tests/unit/
test_keygen_service.py; THIS proves the rendered UX + that the new dialog JS actually runs (the integration
tests never execute the browser JS): open "Keys" → the role picker lists the registered roles → open one → its scope +
the show-once WARNING + the Generate button render. The first test stops BEFORE clicking Generate — the GET routes touch
no age-keygen and never write /.sops.yaml (they read the descriptor + derive the recipient domains); the second clicks
it with the POST served canned, so neither mints a key or writes anything. Selectors are data-testid only
(testid_reference.md). opt-in via the `e2e` marker.
"""
import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_keygen_picker_and_dialog_render(page):
    """Clicking "Keys" opens the picker (one button per registered role); opening a role renders its scope,
    the show-once warning, and the Generate button. Guards the keygen dialog's open→list→open→render path +
    the new dialog JS loading cleanly (a template JS syntax error would surface here as an empty/broken modal).
    It deliberately does NOT click Generate (that would mint a key + edit /.sops.yaml)."""
    page.goto("/")
    page.get_by_test_id("keygen-open").click()
    expect(page.get_by_test_id("keygen-picker-body")).to_be_visible()
    bg = page.locator('[data-testid="keygen-role"][data-role="break-glass"]')
    expect(bg).to_be_visible()

    bg.click()
    body = page.get_by_test_id("keygen-body")
    expect(body).to_be_visible()
    # the break-glass descriptor carries a show-once warning + a Generate button (the mint trigger)
    expect(body.get_by_test_id("keygen-warning")).to_be_visible()
    expect(body.get_by_test_id("keygen-generate")).to_be_visible()


@pytest.mark.parametrize("changed, staged, header", [
    (True, True, "STAGED proposed/0123456789ab (run_id 0123456789ab)"),
    (True, False, "committed to main (run_id 0123456789ab)"),
    (False, False, "public key was already a recipient; nothing staged")],
    ids=["staged", "committed-to-main", "nothing-changed"])
def test_the_keygen_result_says_whether_it_staged(page, changed, staged, header):
    """Verifies: the keygen result's header says STAGED and the proposal only when the route staged the write,
    names `main` when the deploy commits straight to it, and gives the route's note when nothing changed. Guards
    against (T19): "STAGED main" printed above steps that, on such a deploy, rightly carry no promote step — the
    header and the steps disagreeing about whether anything waits to be promoted. Only the POST is served canned,
    so no key is minted and nothing is written."""
    def generate(route):
        if route.request.method != "POST":
            return route.fallback()
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "role": "break-glass", "changed": changed, "committed": changed, "staged": staged,
            "run_id": "0123456789ab", "target_ref": "proposed/0123456789ab" if staged else "main",
            "private_key": "shown-once", "public_key": "recipient", "next_steps": ["store it offline"],
            "note": None if changed else "public key was already a recipient; nothing staged"}))
    page.route("**/api/keygen/*", generate)
    page.goto("/")
    page.get_by_test_id("keygen-open").click()
    page.locator('[data-testid="keygen-role"][data-role="break-glass"]').click()
    page.get_by_test_id("keygen-generate").click()
    steps = page.get_by_test_id("keygen-steps")
    expect(steps).to_contain_text(header)
    if not staged:
        expect(steps).not_to_contain_text("STAGED")
