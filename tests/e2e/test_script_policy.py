"""E2E — the page's `script-src` nonce (SECURITY.md C19, T23) in a REAL browser.

WHY: gui/app.py gives the page a fresh nonce per response and every other response `script-src 'none'`; the
integration tests (tests/integration/test_gui_api.py) prove the header and the tag agree. Only a browser can show
the two consequences that matter: markup that reaches the page anyway (the paint rule's second layer) runs nothing,
and the page itself still works — its one script runs and every header button, bound from that script now that
handler attributes cannot run, still opens what it opened. The suite drives the real app through the live server
(conftest.py), so the header arrives as it does in production; nothing here sets `bypass_csp`, which would stop
the suite from testing the policy at all.
"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# Inserts an element whose handler attribute would set a marker, and resolves once its load has failed (when the
# handler, if it were allowed, has already run: an attribute's listener is registered before this one).
_INJECT = """() => new Promise(done => {
  const box = document.createElement('div');
  box.innerHTML = '<img src="x" onerror="window.__kontroll_csp = 1">';
  box.firstChild.addEventListener('error', () => setTimeout(done, 0));
  document.body.appendChild(box);
})"""


def test_an_injected_handler_attribute_does_not_run(page):
    """An `<img onerror=…>` inserted into the live page fails to load and its handler never runs, and the browser
    reports the refusal as a policy violation. Guards the policy being present in the header and absent in effect —
    a nonce that also matched attributes, an `'unsafe-inline'`, a header the page never got."""
    page.goto("/")
    page.evaluate("() => { window.__kontroll_violations = [];"
                  " document.addEventListener('securitypolicyviolation',"
                  " e => window.__kontroll_violations.push(e.effectiveDirective)); }")
    page.evaluate(_INJECT)
    assert page.evaluate("window.__kontroll_csp === undefined"), "an injected handler attribute RAN"
    page.wait_for_function("window.__kontroll_violations.length > 0")
    assert page.evaluate("window.__kontroll_violations")[0].startswith("script-src"), \
        page.evaluate("window.__kontroll_violations")


def test_without_the_policy_the_same_handler_runs(page):
    """The control for the test above: the same page served WITHOUT its Content-Security-Policy header (the real
    response, the header dropped) runs the injected handler. Without this, a test harness in which no handler ever
    ran (a blocked image load, an event that never fired) would pass the test above for the wrong reason."""
    def drop_policy(route):
        response = route.fetch()
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-security-policy"}
        route.fulfill(response=response, headers=headers)
    page.route("**/", drop_policy)
    page.goto("/")
    page.evaluate(_INJECT)
    assert page.evaluate("window.__kontroll_csp === 1"), "the control's handler did not run — the test above proves nothing"


@pytest.mark.parametrize("button, opened", [
    ("fleet-open", "fleet-panel"),
    ("pending-open", "pending-panel"),
    ("discovery-open", "discovery-panel"),
    ("backup-open", "backup-panel"),
    ("secrets-open", "secret-picker"),
    ("keygen-open", "keygen-picker"),
    ("settings-open", "settings-panel"),
    ("homepage-open", "homepage-panel"),
])
def test_every_header_button_still_opens_its_panel(page, button, opened):
    """A click on each header button opens its panel, and the page's script ran without a policy violation. Guards
    a binding lost when the handler attributes moved into the script (a button that does nothing), a script the
    policy refused (every button dead, the page blank), and a button bound to a bare function reference."""
    page.goto("/")
    page.evaluate("() => { window.__kontroll_violations = [];"
                  " document.addEventListener('securitypolicyviolation',"
                  " e => window.__kontroll_violations.push(e.effectiveDirective)); }")
    page.get_by_test_id(button).click()
    expect(page.get_by_test_id(opened)).to_be_visible()
    assert page.evaluate("window.__kontroll_violations") == []


def test_the_search_button_still_searches(page):
    """The search button, now bound from the script, runs a search: the mocked catalog's card appears. Guards the
    one header control that is not a panel losing its binding."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("cisco ios")
    page.get_by_test_id("search-btn").click()
    expect(page.get_by_test_id("card").first).to_be_visible()


def test_the_backups_button_opens_the_unfiltered_list(page):
    """The Backups button asks for every device's backups — `/api/backups` with no `host` — and its caption names no
    host. Guards the one opener that takes an argument (`openBackupPanel(host)`): bound as a bare reference it would
    receive the click event as its host and ask for `?host=[object PointerEvent]`."""
    page.goto("/")
    with page.expect_request(lambda r: "/api/backups" in r.url) as request:
        page.get_by_test_id("backup-open").click()
    assert "host=" not in request.value.url, request.value.url
    expect(page.get_by_test_id("backup-panel")).not_to_contain_text("[object")
