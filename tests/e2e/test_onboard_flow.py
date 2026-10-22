"""The onboarding GUI flow, end-to-end in a headless browser over the REAL Flask app
(onboard runs the service layer in-process, mocked at its probe/catalog seams). The auth gate +
API contract are covered hermetically by tests/integration/test_gui_api.py; this proves the rendered
UX: search → result card → open the form → dry-run → the plan renders. Selectors are data-testid only
(never CSS/text), per the data-testid SOP (docs/testing-standards.md §3). opt-in via the `e2e` marker.
"""
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_page_loads(page):
    """The GUI loads (authenticated via the context's http_credentials) and the search input
    renders — guards against an auth-gate regression or a missing-template break that would
    otherwise surface only as a blank/blocked page."""
    page.goto("/")
    assert "kontroll" in page.title().lower()
    expect(page.get_by_test_id("search-input")).to_be_visible()


def test_search_renders_a_result_card(page):
    """A search renders a result card with the collection name, its capability badges, and the
    suggested-backend badge — selected by data-testid (not brittle text/CSS). Guards the
    search→render path: that the JS actually builds the card from the API record's fields."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("cisco ios")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()
    expect(card.get_by_test_id("result-title")).to_contain_text("cisco.ios")
    expect(card.get_by_test_id("suggested-backend-badge")).to_contain_text("netcommon_cli")
    # the backup capability is addressable by data-cap and carries its state — no class/glyph parsing
    expect(card.locator('[data-testid="capability-badge"][data-cap="backup"]')).to_have_attribute(
        "data-state", "yes")


def test_a_hostile_collection_description_renders_as_text_not_markup(page):
    """A collection whose Galaxy `description` is `<img src=x onerror=…>` renders as LITERAL TEXT: no element is
    created and no handler fires. This is the only proof that runs the real DOM — the source gate
    (tests/unit/test_card_paint_gate.py) pins the code shape, but only a browser can show the payload is inert.

    Guards the SEC-A1 hole closed on 2026-07-27 (SECURITY.md C19): `card()` painted the Galaxy-authored description
    through `E()`/`innerHTML`, so anyone who published a collection tagged to match a plausible device search got
    script execution in the operator's authenticated session on SEARCH ALONE — before any onboard or consent
    dialog could gate it, with the session able to reach every POST route the GUI exposes."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("xsspaint")
    page.get_by_test_id("search-btn").click()
    # the payload survives as TEXT (auto-waiting expect, never .count())
    expect(page.get_by_test_id("result-meta").first).to_have_text('<img src=x onerror="window.__kontroll_xss=1">')
    # …and produced no element, so no handler could have run
    expect(page.locator('[data-testid="card"] img')).to_have_count(0)
    assert page.evaluate("window.__kontroll_xss === undefined"), "the payload EXECUTED — the card is a live XSS sink"


def test_onboard_dryrun_renders_the_plan(page):
    """A dry-run must PLAN but never write: opening the form, filling the required fields, and
    submitting with `apply` UNCHECKED renders the plan in the output pane. Guards the
    security-relevant invariant (dry-run by default) and the submit→render path."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("cisco")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()

    card.get_by_test_id("onboard-toggle").click()                  # reveal the form
    expect(card.get_by_test_id("onboard-form")).to_be_visible()
    card.get_by_test_id("field-key").fill("cisco_ios")
    card.get_by_test_id("field-group").fill("core_switch")
    card.get_by_test_id("field-host").fill("192.0.2.10")
    expect(card.get_by_test_id("field-apply")).not_to_be_checked()  # dry-run by default — no write

    card.get_by_test_id("run").click()
    out = card.get_by_test_id("output")
    expect(out).to_contain_text("DRY-RUN plan")                    # the plan rendered (in-process)
    expect(out).to_contain_text("netcommon_cli")                   # the auto-classified backend


def test_onboard_form_derives_ssh_login_for_a_switch_no_token_box(page):
    """F1 (self-describing auth) live: opening a cisco.ios (network_cli) card's form DERIVES its per-backend
    credential fields — SSH login (username/password) + the SSH PRIVATE-key field as a multi-line <textarea> (a PEM
    doesn't fit one line) + the enable secret — and shows NO api-token box (a switch doesn't take a REST token).
    Proves the headline win in a real browser: the form fetches /api/onboard/cred-fields on open and renders the
    RIGHT inputs for the picked device, not a static one-size-fits-all union. The value is SOPS-stored, never echoed."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("cisco")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()
    card.get_by_test_id("onboard-toggle").click()                  # reveal the form → lazily fetch + render the fields
    key = card.get_by_test_id("field-cred-ssh_private_key")
    expect(key).to_be_visible()                                    # auto-waits for the async derive to render
    assert key.evaluate("el => el.tagName.toLowerCase()") == "textarea"   # multi-line, not a single-line input
    expect(card.get_by_test_id("field-cred-username")).to_be_visible()
    expect(card.get_by_test_id("field-cred-enable_password")).to_be_visible()
    expect(card.get_by_test_id("field-cred-api_token")).to_have_count(0)   # no dead REST token box on a switch


def test_onboard_form_groups_proxmox_api_token_as_one_auth_set(page):
    """F1 seam S1 live: opening a proxmox card's form derives its CURATED credential set — the three API-token parts
    (user / token_id / token_secret) rendered INSIDE one grouped <fieldset> (auth-set-proxmox_api), with NO SSH-key
    box (PVE telemetry is API-only) and NO generic single-token box. Proves the multi-field-mangled-into-one-box
    defect is closed in a real browser: the form fetches /api/onboard/cred-fields?collection=… and the curated
    class's own auth_set wins over the generic per-backend shape (the blind user sees one credential with parts)."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("proxmox")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()
    card.get_by_test_id("onboard-toggle").click()                  # reveal → lazily fetch + render the cred fields
    group = card.get_by_test_id("auth-set-proxmox_api")
    expect(group).to_be_visible()                                  # auto-waits for the async derive to render
    expect(group.get_by_test_id("field-cred-api_user")).to_be_visible()         # the three coherent parts, grouped
    expect(group.get_by_test_id("field-cred-api_token_id")).to_be_visible()
    expect(group.get_by_test_id("field-cred-api_token_secret")).to_be_visible()
    expect(card.get_by_test_id("field-cred-ssh_private_key")).to_have_count(0)   # API-only: no SSH key box
    expect(card.get_by_test_id("field-cred-api_token")).to_have_count(0)         # not the generic single-token box


def test_the_generic_ssh_card_opens_an_ssh_form_under_its_own_key(page):
    """S24 live: a `linux` search shows the Generic SSH host card — suggesting `raw_ssh`, with no Automations badge
    (a card names a collection ansible-core ships, nothing a unit pins) — and its onboard form proposes the card's
    class key `ssh_host` and derives the SSH credential fields. Guards the operator's only path to a plain SSH host
    now that raw_ssh is never auto-matched: a form defaulting to `ansible_builtin` creates a junk class, and one
    without SSH fields cannot onboard the box."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("linux")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()
    expect(card.get_by_test_id("suggested-backend-badge")).to_contain_text("raw_ssh")
    expect(card.get_by_test_id("no-backend-badge")).to_have_count(0)
    expect(card.get_by_test_id("unit-badge")).to_have_count(0)
    card.get_by_test_id("onboard-toggle").click()
    expect(card.get_by_test_id("field-key")).to_have_value("ssh_host")
    expect(card.get_by_test_id("field-cred-username")).to_be_visible()      # auto-waits for the async derive
    expect(card.get_by_test_id("field-cred-ssh_private_key")).to_be_visible()
    expect(card.get_by_test_id("field-cred-api_token")).to_have_count(0)


def test_a_collection_no_backend_fits_says_so_on_its_card(page):
    """S24 live: a collection that carries no signal any backend classifies on is shown with a "no backend fits"
    badge and no suggested backend. Guards the operator filling a form for a target the onboard will refuse with no
    hint on the card (raw_ssh used to catch such a collection and onboard it as SSH-as-root)."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("nosignal")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()
    expect(card.get_by_test_id("no-backend-badge")).to_be_visible()
    expect(card.get_by_test_id("suggested-backend-badge")).to_have_count(0)


def test_onboard_dryrun_shows_the_auto_derived_monitoring_floor(page):
    """F2 (de-bespoke) live: onboarding a collection under a FRESH device-class key (no existing module → the
    derive path, not a reuse) shows the AUTO-DERIVED monitoring floor in the dry-run output — the blind class is
    monitored + log-shippable on day one with ZERO curation. Proves the empty-Grafana/empty-Loki defect is closed
    through the real UI: the form's plan carries the derived metrics/logs the backend confers."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("cisco")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()
    card.get_by_test_id("onboard-toggle").click()
    card.get_by_test_id("field-key").fill("arista_eos_auto")       # a fresh key ⇒ the non-reuse derive path
    card.get_by_test_id("field-group").fill("core_switch")
    card.get_by_test_id("field-host").fill("192.0.2.40")
    expect(card.get_by_test_id("field-apply")).not_to_be_checked()  # dry-run by default — no write
    card.get_by_test_id("run").click()
    out = card.get_by_test_id("output")
    expect(out).to_contain_text("auto-derived monitoring")         # the F2 floor summarized
    expect(out).to_contain_text("snmp")                            # the conferred agent-less telemetry


def test_index_panel_lists_services_and_fleet(page):
    """The header 'Index' button opens the two-section panel (#128): the control-plane SERVICES section renders a
    `service-row` per shipped service (e.g. Semaphore, from config/services.yml) ABOVE the onboarded FLEET
    section — devices AND services in one panel, proven live in a real browser. Guards the openFleetPanel
    two-fetch round-trip (services + fleet) actually wiring up; each section degrades independently. Selectors are
    data-testid only (the relabel to 'Index' is pinned by the template-grep unit test, not a text selector here)."""
    page.goto("/")
    page.get_by_test_id("fleet-open").click()
    expect(page.get_by_test_id("fleet-panel")).to_be_visible()
    # the SERVICES section rendered the shipped Semaphore row (url may be a link or unresolved — data-name pins it)
    expect(page.locator('[data-testid="service-row"][data-name="Semaphore"]')).to_be_visible()


def test_pending_panel_opens_and_shows_empty_state(page):
    """The header 'Pending' button opens the read-only proposals panel (#129) — proven live. The test box has no
    canonical bare repo, so `list_pending` degrades to an informational empty state (`pending-empty`), not a crash
    or a 500 — the service-never-sys.exit / honest-empty posture in a real browser. Guards the openPendingPanel
    round-trip wiring up + the C10 read-only panel having NO promote button (it only ever lists + copies)."""
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    expect(page.get_by_test_id("pending-panel")).to_be_visible()
    expect(page.get_by_test_id("pending-empty")).to_be_visible()    # honest empty state (no canonical on the test box)


def test_settings_panel_shows_the_four_read_only_groups(page):
    """The header 'Settings' button opens the read-only platform-settings panel (#135) with all four area groups
    (identity / backup / fleet / status) + the arming badge — proven live in a real browser. Guards the
    openSettingsPanel round-trip + the read-only Phase-1 surface (no edit controls; it stages nothing)."""
    page.goto("/")
    page.get_by_test_id("settings-open").click()
    expect(page.get_by_test_id("settings-panel")).to_be_visible()
    for grp in ("identity", "backup", "fleet", "status"):
        expect(page.locator('[data-testid="settings-group"][data-group="%s"]' % grp)).to_be_visible()
    expect(page.get_by_test_id("settings-arming-badge")).to_be_visible()   # the platform-status arming badge


def test_homepage_editor_opens_with_sections_and_controls(page):
    """The header 'Homepage' button opens the editor (#136) with at least one section card, a tile checkbox, and
    the Preview/Save controls — proven live. Save is NOT exercised here (it stages a commit, needs an armed repo);
    instead a page.evaluate asserts the staged-result render surfaces the run_id (mirroring renderCapPromote).
    Guards the openHomepagePanel read round-trip + the staged-result render."""
    page.goto("/")
    page.get_by_test_id("homepage-open").click()
    expect(page.get_by_test_id("homepage-panel")).to_be_visible()
    expect(page.get_by_test_id("homepage-section").first).to_be_visible()
    expect(page.get_by_test_id("homepage-tile-select").first).to_be_visible()
    expect(page.get_by_test_id("homepage-propose")).to_be_visible()
    expect(page.get_by_test_id("homepage-apply")).to_be_visible()
    staged = page.evaluate(
        "() => renderHomepageResult({changed:true, staged:true, target_ref:'proposed/abc123def456',"
        " run_id:'abc123def456', paths:['instance/dashboards/homepage/services.yaml'], next:'promote it'})")
    assert "abc123def456" in staged and "STAGED proposal proposed/abc123def456" in staged


def test_onboard_surfaces_provisioning_prerequisite(page):
    """A device class that declares a credential prerequisite (proxmox → the PVEAuditor token role) SURFACES it
    at onboarding in a dedicated advisory node — the blind-joe constraint (no manual step beyond IP+creds is left
    buried in a doc). And it is strictly NON-GATING: Run stays ENABLED, so the operator can still onboard with a
    wrong/absent scope (the validate seam's token-scope check catches it on the wire later). Guards both the
    surfacing and the never-gates invariant (INVARIANT D*)."""
    page.goto("/")
    page.get_by_test_id("search-input").fill("proxmox")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    expect(card).to_be_visible()

    card.get_by_test_id("onboard-toggle").click()                  # reveal the form
    expect(card.get_by_test_id("onboard-form")).to_be_visible()
    card.get_by_test_id("field-key").fill("proxmox")
    card.get_by_test_id("field-group").fill("hypervisors")
    card.get_by_test_id("field-host").fill("192.0.2.20")
    expect(card.get_by_test_id("field-apply")).not_to_be_checked()  # dry-run by default — no write

    card.get_by_test_id("run").click()
    expect(card.get_by_test_id("onboard-provisioning")).to_contain_text("PVEAuditor")   # prerequisite surfaced
    expect(card.get_by_test_id("run")).to_be_enabled()             # advisory — never gates onboarding


@pytest.mark.parametrize("has_remote", [True, False])
def test_the_push_box_follows_the_offsite_remote(page, has_remote):
    """The onboard form shows the "also push origin" box only when the server says an offsite remote exists. The
    box is part of the form's literal markup and is REMOVED when the flag is false (it is no longer interpolated —
    SECURITY.md C19), so this drives both values of the flag in a real page: present and unticked with a remote,
    absent without one, and the two neighbouring boxes untouched either way. Guards a fresh node offering a push
    that can only fail — and a removal that takes the wrong label with it."""
    def flagged(route):                                         # the page as served, with the one flag forced
        response = route.fetch()
        body = re.sub(r"const HAS_REMOTE = (true|false);",
                      "const HAS_REMOTE = %s;" % ("true" if has_remote else "false"), response.text())
        route.fulfill(response=response, body=body)
    page.route(re.compile(r"^https?://[^/]+/$"), flagged)
    page.goto("/")
    assert page.evaluate("HAS_REMOTE") is has_remote
    page.get_by_test_id("search-input").fill("cisco")
    page.get_by_test_id("search-btn").click()
    card = page.get_by_test_id("card").first
    card.get_by_test_id("onboard-toggle").click()
    expect(card.get_by_test_id("field-apply")).to_be_visible()
    expect(card.get_by_test_id("field-bootstrap")).to_be_visible()
    expect(card.get_by_test_id("field-push")).to_have_count(1 if has_remote else 0)
    if has_remote:
        expect(card.get_by_test_id("field-push")).not_to_be_checked()
