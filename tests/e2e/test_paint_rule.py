"""E2E — the paint rule (SECURITY.md C19) in a REAL browser: a value the GUI did not author is text, not markup.

WHY: the source gate (tests/unit/test_card_paint_gate.py) proves the code SHAPE — only literal markup reaches
`innerHTML`. Only a browser can show the consequence: a value that carries `<img src=x onerror=…>` is displayed as
those characters, creates no element, and runs nothing. Before the rule was applied as a class, the Pending panel
painted a staged proposal's run id, commit subject and path names as markup — bytes that exist BEFORE any human has
promoted anything — and the Pending, Index, Settings, Homepage and Discovery panels and the capability,
reconfigure, secret and keygen dialogs painted their API error string, their title or their rows the same way.

Each test serves a panel's JSON with `page.route` (the mocking seam the Pending tests already use), puts a payload
in the fields that panel paints, and asserts three things: the payload is visible as text where it belongs, the
PAGE holds no `<img>` (it has none of its own, so one anywhere means a value was parsed as markup), and the
payload's marker was never set. No canonical repository, no real ref and no write is involved. The Backups panel,
the unit dialogs and the search card are covered by their own tests (C14, test_unit_flow, test_onboard_flow).
"""
import json
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# The C19 model payload: an element that executes on load failure if — and only if — it is parsed as markup.
PAYLOAD = '<img src=x onerror="window.__kontroll_xss=1">'
# The same element with no quote in it, for a value the page shows through JSON.stringify (which escapes quotes).
BARE = "<img src=x onerror=window.__kontroll_xss=1>"
# A link target that executes on click if it ever becomes an href.
SCRIPT_URL = "javascript:window.__kontroll_xss=1"


def P(tag):
    """The payload with a suffix, so an assertion can tell WHICH field a container is showing."""
    return PAYLOAD + tag


def _serve(page, glob, body, status=200):
    """Fulfil every request matching `glob` with `body` as JSON."""
    page.route(glob, lambda route: route.fulfill(
        status=status, content_type="application/json", body=json.dumps(body)))


def _assert_inert(page):
    """No value was parsed as markup anywhere on the page, and the payload's marker was never set. The page has no
    `<img>` of its own, so the count is taken page-wide: a payload painted outside the panel under test is seen too,
    and the check does not depend on `onerror` having had time to fire."""
    expect(page.locator("img")).to_have_count(0)
    assert page.evaluate("window.__kontroll_xss === undefined"), "the payload EXECUTED — a value reached innerHTML"


def test_a_staged_proposal_is_painted_as_text(page):
    """Every field of a staged proposal — the run id (a git ref name), staged-at, sha, the commit subject, each
    change's status and path name — the promote-task name, and every field of a ref listed apart as "not a
    proposal" (its name, its id, the reason) render as literal text in the Pending panel, and so does the run id
    in the select-to-copy fallback a plain-HTTP deploy takes. Guards the hole this rule closed: those bytes are
    whatever somebody committed or wrote into the canonical, nobody has promoted them, and the panel is where an
    operator looks at them, so markup there ran in the operator's session on opening the list."""
    _serve(page, "**/api/pending", {
        "proposals": [
            {"run_id": P("rid"), "ref": "refs/heads/proposed/x", "sha": "deadbee",
             "staged_at": "2026-07-05T00:00:00+00:00", "author": "kontroll", "subject": P("subject"),
             "paths": [P("path"), "instance/fleet.yml"],
             "changes": [{"status": P("st"), "path": P("path"), "exact": True},
                         {"status": "M", "path": "instance/fleet.yml", "exact": True}],
             "promotable": True},
            {"run_id": "abc123def456", "ref": "refs/heads/proposed/abc123def456", "sha": P("sha"),
             "staged_at": P("at"), "author": "kontroll", "subject": "feat(onboard): stage cisco_ios", "paths": [],
             "changes": [], "promotable": True}],
        "unrecognised": [{"ref": P("ref"), "sha": P("oid"), "why": P("why")}],
        "main_note": P("main"), "skipped_note": P("skipped"),
        "canonical": "/srv/kontroll.git", "promote_task": P("task"), "note": None})
    page.goto("/")
    page.get_by_test_id("pending-open").click()
    row = page.get_by_test_id("pending-row").first
    expect(row.get_by_test_id("pending-head")).to_have_text(P("rid") + " staged 2026-07-05T00:00:00+00:00 · deadbee")
    expect(row.get_by_test_id("pending-subject")).to_have_text(P("subject"))
    expect(row.get_by_test_id("pending-change")).to_have_text([P("st") + " " + P("path"), "M instance/fleet.yml"])
    expect(row.get_by_test_id("pending-promote-hint")).to_contain_text(P("task"))
    second = page.get_by_test_id("pending-row").nth(1)
    expect(second.get_by_test_id("pending-head")).to_have_text("abc123def456 staged " + P("at") + " · " + P("sha"))
    expect(page.get_by_test_id("pending-unrecognised-row")).to_have_text(
        P("ref") + " · " + P("oid") + " — " + P("why"))
    expect(page.get_by_test_id("pending-main-note")).to_have_text(P("main"))
    expect(page.get_by_test_id("pending-skipped-note")).to_have_text(P("skipped"))
    _assert_inert(page)
    # the copy fallback: with no clipboard API the button is replaced by the run id as selectable text
    page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true})")
    row.get_by_test_id("pending-copy-runid").click()
    expect(row.get_by_test_id("pending-copy-runid")).to_have_count(0)
    expect(row).to_contain_text(P("rid"))
    _assert_inert(page)


@pytest.mark.parametrize("opener, routes", [
    ("pending-open", {"**/api/pending": "pending-error"}),
    ("discovery-open", {"**/api/discovery": "discovery-error"}),
    ("settings-open", {"**/api/settings": "settings-error"}),
    ("homepage-open", {"**/api/homepage": "homepage-error"}),
    ("fleet-open", {"**/api/services": "services-error", "**/api/fleet": "fleet-error",
                    "**/api/actuation": "automations-error", "**/api/provenance": "provenance-error",
                    "**/api/image-provenance": "image-provenance-error"}),
])
def test_a_panel_error_string_is_painted_as_text(page, opener, routes):
    """A route's `error` string is text in every panel that shows one. An error can quote request input, a
    repository value (the fleet read's message quotes the entry it could not resolve) or a remote response, so it
    is foreign on the same footing as a record field. Guards the error lines that used to be written with a direct
    `innerHTML` assignment the old gate never read."""
    for glob in routes:
        _serve(page, glob, {"error": PAYLOAD})
    page.goto("/")
    page.get_by_test_id(opener).click()
    for testid in routes.values():
        expect(page.get_by_test_id(testid)).to_have_text(PAYLOAD)
    _assert_inert(page)


def test_dialog_error_strings_are_painted_as_text(page):
    """The four dialogs that load a projection — capability and reconfigure (opened from a fleet row), secret and
    keygen (opened from their pickers) — paint its `error` as text. Same rule as the panel errors, through the
    dialogs that open on top of a panel."""
    _serve(page, "**/api/fleet", [{"key": "cisco_ios", "collection": "cisco.ios", "group": "core_switch",
                                   "capabilities": {}, "hosts": []}])
    _serve(page, "**/api/configurable*", {"error": PAYLOAD})
    _serve(page, "**/api/secrets", [{"domain": "network", "label": "Network", "description": "device logins"}])
    _serve(page, "**/api/keygen", [{"role": "breakglass", "label": "Break-glass", "description": "offline key"}])
    _serve(page, "**/api/keygen/*", {"error": PAYLOAD})
    page.goto("/")
    page.get_by_test_id("fleet-open").click()
    row = page.get_by_test_id("fleet-class").first
    row.get_by_test_id("fleet-class-cap").first.click()
    expect(page.get_by_test_id("cap-error")).to_have_text(PAYLOAD)
    page.get_by_test_id("cap-close").click()
    row.get_by_test_id("fleet-class-identity").click()
    expect(page.get_by_test_id("reconfig-error")).to_have_text(PAYLOAD)
    page.get_by_test_id("reconfig-close").click()
    page.get_by_test_id("fleet-close").click()
    page.get_by_test_id("secrets-open").click()
    page.get_by_test_id("secret-domain").first.click()
    expect(page.get_by_test_id("secret-error")).to_have_text(PAYLOAD)
    page.get_by_test_id("secret-close").click()
    page.get_by_test_id("keygen-open").click()
    page.get_by_test_id("keygen-role").first.click()
    expect(page.get_by_test_id("keygen-error")).to_have_text(PAYLOAD)
    _assert_inert(page)


def test_index_rows_paint_repository_values_as_text_and_never_link_a_script_url(page):
    """A fleet class row and a service row paint repository values as text, and a service URL that is not a web
    link is shown as text instead of becoming a link. Guards two things at once: markup in a promoted
    `module.yml`/`services.yml` value, and the neighbour that text-painting alone does not close — an
    `href="javascript:…"`, which runs on click however its label is painted."""
    _serve(page, "**/api/fleet", [{"key": P("key"), "collection": P("coll"), "group": P("group"),
                                   "capabilities": {}, "hosts": []}])
    _serve(page, "**/api/services", [
        {"name": P("svc"), "url": SCRIPT_URL, "description": "a service", "enabled": True},
        {"name": "Grafana", "url": "https://grafana.example.test", "description": "boards", "enabled": True}])
    page.goto("/")
    page.get_by_test_id("fleet-open").click()
    fleet_row = page.get_by_test_id("fleet-class").first
    for tag in ("key", "coll", "group"):
        expect(fleet_row).to_contain_text(P(tag))
    services = page.get_by_test_id("service-row")
    expect(services.first).to_contain_text(P("svc"))
    expect(services.first).to_contain_text(SCRIPT_URL + " (not a web link)")
    expect(services.first.get_by_test_id("service-link")).to_have_count(0)
    expect(services.nth(1).get_by_test_id("service-link")).to_have_attribute("href", "https://grafana.example.test")
    _assert_inert(page)


def test_the_capability_dialog_paints_its_projection_as_text(page):
    """The capability dialog builds its body from the server's projection — the suggestion note, the declared
    methods, each method's name and label, a knob's label/help/choices, the Stage-3 label and chip, the credential
    domain — and none of it is parsed as markup; a method name holding a quote stays a value; the identity confirm
    row paints the changed path and both values as text; a plan's link with a script URL is not an anchor, and one
    with no URL says so instead of printing `undefined`. Guards
    the largest interpolated block the template had (method `<option>`s, a server-supplied `data-testid` and three
    labels in one `innerHTML` string), the confirm row where `JSON.stringify` looked like protection, and the second
    `href` sink."""
    _serve(page, "**/api/fleet", [{"key": "cisco_ios", "collection": "cisco.ios", "group": "core_switch",
                                   "capabilities": {"telemetry": True}, "hosts": []}])
    _serve(page, "**/api/configurable*", {
        "methods": [{"name": 'm"1', "label": P("method"), "declared": True, "secret_domain": P("domain"),
                     "group": {"testid": "cap-params", "source_kind": "params",
                               "knobs": [{"key": "k", "label": P("label"), "type": "enum", "allowed": [P("choice")],
                                          "help": P("help")}]}}],
        "declared": [{"methods": [P("declared")]}],
        "suggestion": {"note": P("note")},
        "resource_stage": {"source_kind": "curated_list", "testid": "cap-telemetry-dashboards", "label": P("stage"),
                           "knobs": [{"key": "board", "label": P("chip"), "default": False}]}})
    _serve(page, "**/api/capability/*", {
        "token": "t", "paths": [], "enact": [],
        "changes": [{"kind": "modify", "severity": "identity", "path": P("path"), "before": BARE + "before",
                     "after": "new-value", "blast_radius": P("blast")}],
        "links": [{"label": P("link"), "url": SCRIPT_URL},
                  {"label": "Open Grafana", "url": "http://192.0.2.10:3002"},
                  {"label": "Unresolved"}]})
    page.goto("/")
    page.get_by_test_id("fleet-open").click()
    page.get_by_test_id("fleet-class").first.get_by_test_id("fleet-class-cap").first.click()
    dialog = page.get_by_test_id("cap-dialog")
    for tag in ("note", "declared", "stage"):
        expect(dialog.get_by_test_id("cap-body")).to_contain_text(P(tag))
    expect(dialog.get_by_test_id("cap-method")).to_contain_text(P("method"))       # the option's label
    expect(dialog.get_by_test_id("cap-method")).to_have_value('m"1')               # the option's value, set as data
    expect(dialog.get_by_test_id("cap-cred-prereq")).to_contain_text(P("domain"))
    for tag in ("label", "choice", "help"):
        expect(dialog.get_by_test_id("cap-params")).to_contain_text(P(tag))
    expect(dialog.get_by_test_id("cap-telemetry-dashboards")).to_contain_text(P("chip"))   # the server-named container
    expect(dialog.get_by_test_id("cap-promote")).to_be_disabled()
    _assert_inert(page)
    dialog.get_by_test_id("cap-propose").click()
    confirm = dialog.get_by_test_id("reconfig-confirm-row")
    for text in (P("path"), BARE + "before", P("blast")):
        expect(confirm).to_contain_text(text)
    links = dialog.get_by_test_id("cap-links")
    expect(links).to_contain_text(P("link") + " (" + SCRIPT_URL + " — not a web link)")
    expect(links).to_contain_text("Unresolved (no url)")
    expect(links.get_by_test_id("cap-link")).to_have_count(1)                      # only the web link is a link
    expect(links.get_by_test_id("cap-link")).to_have_attribute("href", "http://192.0.2.10:3002")
    _assert_inert(page)


def test_the_settings_and_homepage_panels_paint_instance_values_as_text(page):
    """Settings rows (identity values, the trust posture, enabled modules, the service and secret-domain rosters), a
    reconfigure dialog's title and a Homepage section name are text. Guards values that live on promoted `main`
    (`instance.yml`, `fleet.yml`, the Homepage overlay): a promote is a human decision about a change, not a review
    of every byte the GUI will later paint."""
    _serve(page, "**/api/settings", {
        "identity": {"mgmt_ip": P("ip"), "domain": P("dom"), "tls_mode": P("tls"), "source_of_truth": P("sot"),
                     "trust_mode": P("trust")},
        "backup": {"backup_remotes": [P("remote")]},
        "fleet": {"enabled_modules": [P("module")]},
        "status": {"armed": True, "services": [{"name": P("svc")}],
                   "secret_domains": [{"domain": P("secret"), "set": True}]}})
    _serve(page, "**/api/configurable*", {"groups": []})
    _serve(page, "**/api/homepage", {"sections": [{"name": P("section"), "generated": True,
                                                   "items": [{"name": "a tile", "present": True}]}]})
    page.goto("/")
    page.get_by_test_id("settings-open").click()
    panel = page.get_by_test_id("settings-panel")
    for tag in ("ip", "dom", "tls", "sot", "remote"):
        expect(panel.get_by_test_id("settings-body")).to_contain_text(P(tag))
    expect(panel.get_by_test_id("settings-trust-mode")).to_contain_text(P("trust"))
    expect(panel.get_by_test_id("settings-services")).to_contain_text(P("svc"))
    expect(panel.get_by_test_id("settings-secrets-roster")).to_contain_text(P("secret"))
    expect(panel.get_by_test_id("settings-fleet-row").first).to_contain_text(P("module"))
    _assert_inert(page)
    panel.get_by_test_id("settings-fleet-disable").first.click()                  # the title carries the module name
    expect(page.get_by_test_id("reconfig-dialog")).to_contain_text("fleet · disable " + P("module"))
    _assert_inert(page)
    page.get_by_test_id("reconfig-close").click()
    page.get_by_test_id("settings-close").click()
    page.get_by_test_id("homepage-open").click()
    expect(page.get_by_test_id("homepage-section").first).to_contain_text(P("section"))
    _assert_inert(page)


def test_the_secret_and_keygen_dialogs_paint_their_descriptors_as_text(page):
    """The secret dialog (the domain in its title, the description, a field's label) and the keygen dialog (the role
    in its title, the description, the recipient domains, the key path, the warning) paint descriptor values as
    text, and so does the keygen RESULT: the minted private key, the public recipient and the next steps — whose
    `<run_id>` placeholder is now shown instead of being swallowed as an unknown tag. Guards the two dialogs that
    sit next to secret material; the keygen result is where a freshly minted private key is displayed."""
    _serve(page, "**/api/secrets", [{"domain": P("domain"), "label": "Network", "description": "device logins"}])
    _serve(page, "**/api/configurable*", {
        "description": P("desc"),
        "groups": [{"testid": "secret-fields", "source_kind": "fields",
                    "knobs": [{"key": "token", "label": P("label"), "secret": True, "already_set": True,
                               "help": P("help")}]}]})
    _serve(page, "**/api/keygen", [{"role": P("role"), "label": "Break-glass", "description": "offline key"}])

    def keygen(route):                                          # GET = the plan, POST = the one-time result
        body = ({"private_key": P("private"), "public_key": P("public"), "changed": True, "staged": True,
                 "target_ref": P("ref"), "run_id": P("runid"),
                 "next_steps": ["save the key offline", "then promote proposed/<run_id>"]}
                if route.request.method == "POST" else
                {"description": P("kdesc"), "recipient_domains": [P("recipient")], "placement": "online",
                 "private_key_path": P("keypath"), "warning": P("warning")})
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    page.route("**/api/keygen/*", keygen)
    page.goto("/")
    page.get_by_test_id("secrets-open").click()
    page.get_by_test_id("secret-domain").first.click()
    secret = page.get_by_test_id("secret-dialog")
    expect(secret).to_contain_text("secrets · " + P("domain"))                     # the title
    expect(secret.get_by_test_id("secret-body")).to_contain_text(P("desc"))
    expect(secret.get_by_test_id("secret-fields")).to_contain_text(P("label") + " (set — blank keeps it)")
    expect(secret.get_by_test_id("secret-apply")).to_be_visible()
    _assert_inert(page)
    secret.get_by_test_id("secret-close").click()
    page.get_by_test_id("keygen-open").click()
    page.get_by_test_id("keygen-role").first.click()
    keygen_dialog = page.get_by_test_id("keygen-dialog")
    expect(keygen_dialog).to_contain_text("keygen · " + P("role"))                 # the title
    expect(keygen_dialog.get_by_test_id("keygen-warning")).to_have_text(P("warning"))
    for text in (P("kdesc"), P("recipient"), "save to " + P("keypath")):
        expect(keygen_dialog.get_by_test_id("keygen-body")).to_contain_text(text)
    _assert_inert(page)
    keygen_dialog.get_by_test_id("keygen-generate").click()
    expect(keygen_dialog.get_by_test_id("keygen-private")).to_have_text(P("private"))
    expect(keygen_dialog.get_by_test_id("keygen-public")).to_have_text("public recipient: " + P("public"))
    steps = keygen_dialog.get_by_test_id("keygen-steps")
    expect(steps).to_contain_text("STAGED " + P("ref") + " (run_id " + P("runid") + ")")
    expect(steps).to_contain_text("promote proposed/<run_id>")                     # shown, not swallowed as a tag
    _assert_inert(page)


def test_a_request_path_segment_is_encoded(page):
    """A dialog's data value never retargets a same-origin request: a keygen role or a secret domain holding `/`,
    `?` or `..` is sent as ONE encoded path segment. Guards a real retarget — unencoded, a domain of
    `../keygen/control` made the secret dialog's Save reach `POST /api/keygen/control`, which mints a key on any
    POST."""
    hostile_role, hostile_domain = "../pending/x/discard?y=1", "../keygen/control"
    _serve(page, "**/api/keygen", [{"role": hostile_role, "label": "Break-glass", "description": "offline key"}])
    _serve(page, "**/api/secrets", [{"domain": hostile_domain, "label": "Network", "description": "device logins"}])
    _serve(page, "**/api/configurable*", {"description": "d", "groups": [{"testid": "secret-fields",
                                          "source_kind": "fields", "knobs": [{"key": "token", "label": "Token"}]}]})
    seen = []
    page.on("request", lambda request: seen.append((request.method, request.url)))
    _serve(page, re.compile(r".*/api/(keygen|secrets)/.+"), {"error": "refused by the test"})
    page.goto("/")
    page.get_by_test_id("keygen-open").click()
    page.get_by_test_id("keygen-role").first.click()
    expect(page.get_by_test_id("keygen-error")).to_be_visible()
    page.get_by_test_id("keygen-close").click()
    page.get_by_test_id("secrets-open").click()
    page.get_by_test_id("secret-domain").first.click()
    page.get_by_test_id("secret-apply").click()
    expect(page.get_by_test_id("secret-result")).to_contain_text("refused by the test")
    paths = [url.split("/api/", 1)[1] for _, url in seen if "/api/keygen/" in url or "/api/secrets/" in url]
    assert "keygen/..%2Fpending%2Fx%2Fdiscard%3Fy%3D1" in paths, paths
    assert "secrets/..%2Fkeygen%2Fcontrol" in paths, paths
    assert not any("/api/pending/" in url or url.endswith("/api/keygen/control") for _, url in seen), seen


def test_safehref_accepts_only_what_the_browser_resolves_to_a_web_link(page):
    """`safeHref` is the one decision about a link target: it returns the value when the browser's own URL parser
    resolves it to http(s) — absolute, relative or scheme-relative — and null for everything else. Guards the
    spellings that fool a prefix check: `javascript:` in any case, behind a leading space or control character, or
    with a tab inside the scheme, and `data:`/`vbscript:`/`file:`; and the non-strings a response could carry."""
    accepted = ["http://a.example.test", "HTTPS://A.EXAMPLE.TEST/x", " https://a.example.test", "/grafana/",
                "//grafana.example.test/x", "relative/path", "#frag", "?q=1"]
    refused = ["javascript:alert(1)", " javascript:alert(1)", "JAVASCRIPT:alert(1)", "java\tscript:alert(1)",
               "\u0001javascript:alert(1)", "data:text/html,x", "vbscript:x", "file:///etc/hosts", "blob:x",
               "ssh://192.0.2.7", "mailto:a@example.test", "http://javascript:alert(1)", "", "   ", None, 7, True,
               {"toString": "x"}, ["http://a.example.test"]]
    page.goto("/")
    out = page.evaluate("(values) => values.map(v => safeHref(v))", accepted + refused)
    assert out[:len(accepted)] == accepted, out[:len(accepted)]
    assert out[len(accepted):] == [None] * len(refused), list(zip(refused, out[len(accepted):]))
