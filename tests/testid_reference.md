# data-testid reference — the onboarding GUI

The single inventory of every `data-testid` in kontroll's first-party GUI
([gui/templates/index.html](../gui/templates/index.html)). E2E tests
([e2e/test_onboard_flow.py](e2e/test_onboard_flow.py)) select on these **only** — never on CSS
classes or DOM structure — so restyling never breaks a test. SOP + rationale:
[docs/testing-standards.md](../docs/testing-standards.md) §3.

**Rule:** every interactive or asserted element has a `data-testid`. When you add/rename one in
the template, update this file in the same commit — and grep-verify it:
`grep -r 'data-testid="<new-id>"' tests/testid_reference.md` (no hit ⇒ the inventory is stale).
Both emission forms count: static `data-testid="…"` and JS-built `el.dataset.testid='…'`
(and `E(tag, class, html, testid)` here).

## Page chrome

| `data-testid` | Element | Notes |
|---|---|---|
| `search-input` | the search `<input id="q">` | also has `#q` (kept) |
| `search-btn` | the Search button | |
| `search-deep` | the "deep" checkbox `<input id="deep">` | unchecked = fast shallow; checked appends `&deep=true` (full probe) |
| `results` | the results container `<div id="results">` | also has `#results` (kept) |

## Search states (one of these renders per search, mutually exclusive)

| `data-testid` | State |
|---|---|
| `search-loading` | "Searching…" while the request is in flight |
| `search-error` | the `fetch` itself threw (transport error) |
| `search-api-error` | the API returned `{error: …}` |
| `no-results` | the API returned an empty list |

## Result card (one per search result, repeated)

| `data-testid` | Element | Discriminator |
|---|---|---|
| `card` | the result card root | `data-collection="<ns.coll>"` to target a specific card |
| `result-title` | the `<h3>` — collection name as text, with version/origin/depth as a child `<span class="meta">` | painted with `ET()`; the version suffix is a CHILD element, not interpolated markup (C19) |
| `result-meta` | the description/note line | painted with `ET()` — the description is **Galaxy-authored** (C19) |
| `result-badges` | the badges container | |
| `capability-badge` | a per-capability badge | `data-cap="actuate\|backup\|bespoke\|telemetry\|logging"` + `data-state="yes\|no\|maybe"`; `telemetry`/`backup`/`logging` are clickable (open the shared dialog) |
| `suggested-backend-badge` | the "→ <backend>" badge | painted with `ET()` — locally derived, but no `rec.*` value reaches `innerHTML` (C19) |
| `no-backend-badge` | the "no backend fits" badge, in place of `suggested-backend-badge` on a record with no `suggested_backend` | literal text; the onboard of that collection is refused `no_backend` unless a class key that declares a backend is used (S24) |
| `onboard-toggle` | the "Onboard this" button — reveals the form; label flips to "Collapse" when open and is **`disabled` while the form holds input** (collapse gated on a pristine form: a peek is dismissable, but entered text / ticked boxes are never silently dropped) | |

## Onboard form (per card, revealed by `onboard-toggle`)

| `data-testid` | Element |
|---|---|
| `onboard-form` | the `<form>` root (assert visible/hidden) |
| `field-key` / `field-group` / `field-host` | the required text inputs (also keep `name=`, the submit payload key) |
| `field-host-name` | optional inventory hostname input (`name=host_name`); blank ⇒ the server auto-names `<key>-N`; pre-filled from a Discovery row's hostname |
| `field-secrets` | secrets-domain input |
| `cred-fields` | the container the DERIVED per-backend credential fields (F1 TIER-A) render into — fetched from `GET /api/onboard/cred-fields?backend=…` on the form's first open, rendered by `renderCredFields` REUSING the secret-form widget (`renderSecretKnob`). Replaces the former static `field-username`/`field-password`/`field-api-token`/`field-ssh-key` union: a `network_cli` switch shows SSH login (+enable), a REST device shows a token (no dead SSH box). Submit harvests every `[data-field]` here into the `creds` map |
| `field-cred-<field>` | one derived credential widget, e.g. `field-cred-username`, `field-cred-password`, `field-cred-ssh_private_key` (multi-line `<textarea>` for a PEM — the unified password-OR-key seam, G6), `field-cred-api_token` (REST), `field-cred-enable_password`, `field-cred-port` (the SSH backends' plain, unmasked port box — `inline`, written into the drop-in as `ansible_port`, an int; blank = 22; S28). Keyed by `data-field=<field>` (the submit `creds` key); the SOPS-stored secret value is never pre-filled/echoed (write-only). The SET of fields is per-backend, so e.g. a switch has NO `field-cred-api_token`. A curated multi-field class (proxmox) emits its own set inside an `auth-set-<name>` group (e.g. `field-cred-api_user`/`field-cred-api_token_id`/`field-cred-api_token_secret`) |
| `auth-set-<name>` | a `<fieldset>` wrapping the `field-cred-*` widgets that share one `auth_set` — ONE coherent credential with parts (F1 seam S1), e.g. `auth-set-proxmox_api` groups the PVE API token's user/id/secret (all-required-together; the planner refuses a partial set). Surfaced when the curated class (`collection=`) declares an `auth_set`; ungrouped fields render flat (no fieldset). The legend reads `<name> — all required together` |
| `cred-derivation-source` | the meta line under the fields noting whether they were derived for this backend (`shallow`) or are the generic union (`fallback`) |
| `cred-fallback-banner` | shown ABOVE the fields ONLY when `cred_source=fallback` (a backend that declares no auth shape) — explains the generic set is shown so a brand-new/unclassified device is never blocked (INVARIANT D*) |
| `onboard-flags` | the checkbox group container |
| `field-apply` / `field-push` / `field-bootstrap` | the actuation checkboxes (apply now writes + STAGES the proposal; bootstrap is deferred to a post-promote Semaphore task — `field-commit` was retired when apply subsumed commit). **`field-push` ("also push origin") is CONDITIONAL** — rendered only when the server reports an offsite git remote (`has_remote`), so a fresh node with no offsite backup never shows it |
| `run` | the Run button (submits the dry-run / apply) |
| `onboard-overwrite` | the **"⚠ Overwrite the existing file"** button — appears (just above `output`) ONLY when an apply returns a `conflict`. Note: onboarding a device of an *existing* class now **reuses** it (no conflict, #124), so a conflict here means a **different** class collides on this key — clicking fires a loud `confirm()` warning that Overwrite can strip a curated class's `metrics:`/`backup:`/`logs:` blocks, then re-submits with `overwrite=true`. Removed on the next submit |
| `output` | the `<pre>` plan/result pane |
| `onboard-provisioning` | the advisory `<pre>` credential-prerequisite pane (sibling of `output`) — shows the class's declared cred prereqs (e.g. "grant PVEAuditor"); **non-gating** (`run` stays enabled), hidden when the class declares none |
| `onboard-capability-gap` | the advisory `<pre>` F2 capability-GAP pane (sibling of `output`) — names the curated richness a BLIND class can't auto-derive (a vendor exporter, a credentialed log pull) + how to add it (`renderCapabilityGap` from the plan's `capability_gap`); **non-gating**, hidden for a full-parity / reused class. The auto-derived monitoring FLOOR (the `derived: true` metrics/logs) is summarized in the `output` pane's dry-run text |

## Secondary-capability dialog (standalone modal — opened from a clickable capability badge)

The generalized capability dialog (telemetry = instance #1; backup joins at Phase 8). The same modal serves
every capability — `cap` is data, never a per-capability testid. Opened by clicking a `capability-badge`
whose `data-cap` is a registered secondary capability (the badge then carries the `cap-open` class).

| `data-testid` | Element | Notes |
|---|---|---|
| `cap-overlay` | the modal overlay (the dim backdrop) | one at a time; reopening removes the prior |
| `cap-dialog` | the dialog root | `data-cap` + `data-key` discriminate which capability/class |
| `cap-close` | the × close control | removes `cap-overlay` |
| `cap-body` | the dialog body (suggestion + picker + plan) | |
| `cap-error` | the error line (unknown cap / not onboarded) | shown instead of the picker |
| `cap-method` | the Stage-1 method `<select>` (offerable methods) | |
| `cap-params` | the Stage-1 param GROUP container | filled by `renderKnobGroup` (Phase 3): one `knob-<key>` row per param. An `enum` param renders a `<select data-param>` (unchanged); `bool`/`int`/`text` render their widened widget — all server-re-validated (P0a) |
| `knob-<key>` | one rendered knob row (any type) | the knob `key` is the discriminator (`knob-schedule`, …); the `[data-param]` widget inside is a select/checkbox/number/text/password per `type`. Asserted in `test_knob_renderer.py` |
| `knob-empty` | the Stage-3 `curated_list` empty state | shown when a `curated_list` group has no items yet (telemetry: `suggested_dashboards` is a later phase) — the honest "no curated items" line, not fabricated chips |
| `cap-cred-prereq` | the advisory credential-prerequisite pane | shown when the selected method names a `secret_domain` (mirrors the onboard provisioning pane — advisory, non-gating); hidden otherwise |
| `cap-cred-open` | the "supply it in Secrets" button inside `cap-cred-prereq` | opens the secret dialog for the method's `secret_domain` |
| `cap-propose` | the Propose button (pure plan — writes nothing) | |
| `cap-promote` | the Promote button (token-gated write) | `disabled` until a successful propose |
| `cap-plan` | the `<pre>` plan/enact pane | contains the scoped paths + the enact commands |
| `cap-result` | the `<pre>` promote-result pane (appended) | surfaces the `run_id` + the staged `proposed/<ref>` — the proposal the operator then promotes from the Pending panel's copy (mirrors `renderOnboard`, #126); asserted in `tests/e2e/test_capability_flow.py` |
| `cap-links` | the capability LINKS block (appended): one `cap-link` anchor per entry whose URL is a web link, plain text naming the URL otherwise (C19) | rendered from the plan's generic `links: [{label, url}]`; telemetry emits Grafana deep-links (`Open Grafana` + `/d/<uid>` per curated dashboard, #122). Shown on propose (preview) + after promote; absent when the plan carries no links (e.g. no mgmt base resolvable) |
| `cap-link` | one link inside `cap-links` | built by `LINK()`: opens in a new tab; exists only when the browser resolves the plan's URL to http(s) — a `javascript:`/`data:` URL never becomes an `href` (C19) |

### Reconfigure testids (Phase 4a, #138 — changing an already-configured capability block)

When the cap dialog opens a **declared** method (the picker marks it "· declared (reconfigure)"), its knobs are
pre-filled with the current values; changing one and proposing surfaces a field-level diff + a severity gate.

| `data-testid` | Element | Notes |
|---|---|---|
| `reconfig-diff` | the `<pre>` diff pane (field-level change-set) | rendered by `renderReconfigDiff` from the plan's `changes`; shows `~ modify` (before→after, OVERWRITES), `+ add`, `- remove`. NAMES + values are non-secret (telemetry/backup/logging carry no secret value). Also appended to the promote result on a drift/would_drop **409** (the diff-on-409, G6) |
| `reconfig-confirm-row` | the row holding the severity-confirm control | created by `capReconfigGate` only when the change-set has a `modify`/`remove`; cleared on re-render |
| `reconfig-overwrite-confirm` | the "confirm replace of N values" control (`modify`) | **gates Promote** — Promote stays `disabled` until this is clicked; a pure `add` enables Promote with no confirm |
| `reconfig-remove-confirm` | the "confirm replace/removal" control when any change is a `remove` | the stronger variant of the gate (a dropped declared value) |
| `reconfig-redeploy-confirm` | the ack control for a `redeploy`-severity change (Phase 4b) | gates Promote behind "takes effect only after promote + deploy-stack"; fired for the strongest severity when it is `redeploy` |
| `reconfig-identity-confirm` | the **type-to-confirm INPUT** for an `identity`-severity change (Phase 4b) | gates Promote until the typed value EXACTLY equals the change's new value (mirrors the mgmt-IP guard); fired for the strongest severity when it is `identity` |

The gate is the **shared** `reconfigGate(dlg, changes, promoteBtn)` (Phase 4b generalized `capReconfigGate`): it
renders `reconfig-diff` + fires the confirm for the STRONGEST effective severity (`c.severity || c.kind`) in the
change-set — `add`→none, `modify`→`reconfig-overwrite-confirm`, `remove`→`reconfig-remove-confirm`,
`redeploy`→`reconfig-redeploy-confirm`, `identity`→`reconfig-identity-confirm`. `capReconfigGate` is a thin
wrapper over it (the capability dialog is unchanged).

### Reconfigure-dialog testids (Phase 4b, #139 — module identity + inventory host-var)

The **one** data-driven reconfigure surface (`openReconfigureDialog`): the shared knob renderer (pre-filled with
`current`) + the `reconfig-diff` pane + the shared gate. `kind`/`id` pick the projection; the dialog POSTs the
surface's own route (`/api/identity/<key>` or `/api/host/<key>/<host>`), which **stages** `proposed/<run_id>`.

| `data-testid` | Element | Notes |
|---|---|---|
| `fleet-class-identity` | the **⚙ identity** badge on a fleet class card | opens the module-identity reconfigure dialog for the class |
| `fleet-host` | a per-host row under a fleet class card | `data-host=<name>`; carries the host's address + the edit control |
| `fleet-host-edit` | the **edit IP** control on a host row | `data-key`/`data-host`; opens the inventory-host reconfigure dialog |
| `reconfig-overlay` / `reconfig-dialog` / `reconfig-close` / `reconfig-body` | the dialog chrome | the standard modal lifecycle; one at a time |
| `reconfig-error` | a whole-view read failure | shown instead of the knob groups (e.g. `not_onboarded`) |
| `identity-fields` | the module-identity knob group container | `configurable_view(kind=module-identity)` `source_kind: identity`; knobs pre-filled from the class's module.yml |
| `host-fields` | the inventory-host knob group container | `configurable_view(kind=inventory-host)` `source_kind: host`; the editable connection vars (templated cred vars excluded — read-only) |
| `reconfig-propose` | the Propose button | POST `apply:false` → the pure plan + the `reconfig-diff` |
| `reconfig-promote` | the Promote button | POST `apply:true`; stays `disabled` until the severity gate is satisfied; **never** promotes (stages `proposed/<run_id>`, C10) |
| `reconfig-plan` | the `<pre>` plan/enact pane | paths + the enact hand-off (run after promote) |
| `reconfig-result` | the `<pre>` promote result | `renderCapPromote` (staged ref + run_id); appends the diff on a drift/would_drop 409 |

### Stage-3 resource testids (descriptor-supplied)

The dialog's Stage-3 "second concern" is **not** a fixed testid: the shared shell renders it via a render-table
keyed by the descriptor's `resource_stage.source_kind`, and emits the testid from the descriptor's
`resource_stage.testid` (see [docs/observability/secondary-capability-dialog.md](../docs/observability/secondary-capability-dialog.md) §3.2).
So each capability's Stage-3 testid is **data in `capabilities/<cap>.yml`**, recorded here so the inventory stays
grep-verifiable (CLAUDE.md hard rule). It must be `cap-<cap>-` then `[a-z0-9-]`, 63 characters at most — the
server drops any other value, and the group renders no container — so it is never a testid the page gives anything
else in the dialog (a control such as `cap-promote`, the method container `cap-params`, a knob row `knob-<key>`):
the page makes the container first and finds each element by testid, first match first
(`test_a_group_testid_is_never_a_control_of_its_dialog`). The one exception is a capability named `cred`, whose
namespace holds the page's own `cap-cred-*` controls: until T23 wires the dialog by reference, the name is reserved,
the registry check refuses the clash (`test_the_registry_check_refuses_a_descriptor_in_the_occupied_namespace`), and
`cred` is pinned as the only such name, with no `cap-` prefix literal or `cap-…${` template in the page after a quote, a
backtick or `=` (how a testid built at run time is spelled; e.g. a `.join` or a split literal is not read;
`test_cred_is_the_one_capability_name_the_page_occupies`). `source_kind: curated_list` renders an asserted picker;
`source_kind: none` renders nothing (the testid is descriptor data only, kept honest here so a later flip to a rendering
`source_kind` already has its row).

| `data-testid` | Supplied by | `source_kind` | Rendered? | Element |
|---|---|---|---|---|
| `cap-telemetry-dashboards` | `capabilities/telemetry.yml` `resource_stage.testid` | `curated_list` | yes | the Stage-3 group CONTAINER, rendered by `renderKnobGroup` (Phase 3, #137). No curated dashboards are offered yet (`suggested_dashboards` is a later phase — `telemetry/README.md`), so it shows the `knob-empty` honest empty state, not fabricated chips. Asserted visible in `test_capability_flow.py` |
| `cap-backup-policy` | `capabilities/backup.yml` `resource_stage.testid` | `none` | no | descriptor data only — backup's `schedule`, `retention`, and `destination` ALL ride as Stage-1 method `params` (#123), rendered by the generic `cap-params` picker (no Stage-3 render). History-prune *enforcement* is now LIVE (#125 — backup-configs.yml). Deferred: offsite *transfer* only (offered, refused) |
| `cap-logging-policy` | `capabilities/logging.yml` `resource_stage.testid` | `none` | no | descriptor data only — logging's retention/parse rides as a Stage-1 method param (V1 must-fix #1) |

## Secret-onboarding dialog (standalone modal — opened from the header "Secrets" button)

The generalized secret dialog (D): one shared modal serves every SOPS domain — `domain` is data, never a
per-domain testid (the secret analogue of the capability dialog). Opened by `secrets-open` → a domain picker →
`openSecretDialog(domain)`. Values are typed in but NEVER returned by the server (only field NAMES + a source).

| `data-testid` | Element | Notes |
|---|---|---|
| `secrets-open` | the header "Secrets" button | opens the domain picker |
| `secret-overlay` | the modal overlay (picker + dialog share it) | one at a time; reopening removes the prior |
| `secret-picker-body` | the domain-picker body | lists one `secret-domain` button per registered domain |
| `secret-domain` | a domain choice button | `data-domain` discriminates; opens the dialog for that domain |
| `secret-dialog` | the per-domain dialog root | `data-domain` = the SOPS domain |
| `secret-close` | the × close control | removes `secret-overlay` |
| `secret-body` | the dialog body (fields + Save) | |
| `secret-fields` | the fields GROUP container (Phase 3, #137) | filled by `renderKnobGroup` over the projected `fields` group from `/api/configurable?kind=secret-domain` — the dialog converged onto the one renderer (`renderSecretKnob` per field) |
| `secret-error` | the error line (unknown domain) | shown instead of the form |
| `secret-field` | a field input/textarea | `data-field` = the SOPS key; `password` unless the descriptor says `type: text` (single-line) or `type: textarea` (multi-line, e.g. a PEM key — renders a `<textarea>`). Rendered by `renderSecretKnob` — a secret is NEVER pre-filled (C11) |
| `secret-generate` | the per-field "gen" button (descriptor `generate:`) | MARKS the field for regeneration (sent in the POST `regenerate` list) + clears it, so the server re-mints it on save; a blank field WITHOUT this mark is KEPT — an already-set generated secret is never silently re-minted (#141). Typing a value clears the mark |
| `secret-apply` | the Save+stage button | encrypts to SOPS + stages `proposed/<run_id>` |
| `secret-result` | the `<pre>` result pane | staged ref + the field NAMES set — never a value |
| `secret-overwrite-confirm` | the overwrite-confirm row (rotation, #141) | shown when a Save would clobber already-set field(s); the SERVER fail-closed 409'd (no write) — this is the operator-facing ack. Lists field NAMES only (C11) |
| `secret-overwrite-go` | the confirm button inside `secret-overwrite-confirm` | on click, re-submits with `overwrite:true` |
| `secret-enact` | the `<pre>` actuation hand-off pane (rotation, #141) | post-promote command strings to make the staged value live (recreate / grafana-cli / semaphore-admin recreate-then-`change-by-login`) — the GUI runs none of it; NAMES + cmds only, never a value |

## Keygen dialog (standalone modal — opened from the header "Keys" button)

The generalized keygen dialog (D): one shared modal mints an age key for any role — `role` is data, never a
per-role testid (the key analogue of the secret dialog). Opened by `keygen-open` → a role picker →
`openKeygenDialog(role)`. The minted PRIVATE key is shown in `keygen-private` **exactly once** (the apply
response) and is never stored, logged, or committed; only the public recipient is added to `/.sops.yaml`.

| `data-testid` | Element | Notes |
|---|---|---|
| `keygen-open` | the header "Keys" button | opens the role picker |
| `keygen-overlay` | the modal overlay (picker + dialog share it) | one at a time; reopening removes the prior |
| `keygen-picker-body` | the role-picker body | lists one `keygen-role` button per registered role |
| `keygen-role` | a role choice button | `data-role` discriminates; opens the dialog for that role |
| `keygen-dialog` | the per-role dialog root | `data-role` = the key role |
| `keygen-close` | the × close control | removes `keygen-overlay` |
| `keygen-body` | the dialog body (scope + warning + Generate) | |
| `keygen-error` | the error line (unknown role) | shown instead of the form |
| `keygen-warning` | the show-once safety warning (from the descriptor) | |
| `keygen-generate` | the Generate+stage button | mints the keypair; adds the public recipient + stages `proposed/<run_id>` |
| `keygen-result` | the `<pre>` result pane | holds the show-once private key + steps |
| `keygen-private` | the show-once PRIVATE key block | the ONLY place the private key ever appears; not stored |
| `keygen-public` | the public recipient line | safe to record (it's the `.sops.yaml` recipient) |
| `keygen-steps` | the result header (STAGED + ref, committed to main, or the note) + the next-steps pane | promote (only when staged) + `sops updatekeys` (never auto-run); asserted in `tests/e2e/test_keygen_flow.py` |

## Index panel (standalone modal — opened from the header "Index" button) — #121, widened #128

A read-only TWO-SECTION index. **Section 1 — control-plane services** (`/api/services` →
`kontroll.service.fleet.list_services`): the fixed set kontroll stands up (onboard-GUI/Semaphore/Homepage/Grafana/
Prometheus/API, from `config/services.yml`), link-only. **Section 2 — onboarded fleet** (`/api/fleet` →
`list_fleet`): each device-class's declared capabilities + its inventory hosts; the per-class capability badge
reuses the SAME `openCapabilityDialog(cap, key)` the search cards use. The header button label widened from
"Fleet" to **"Index"** (#128); the `fleet-*` testids + `/api/fleet` contract are **unchanged** (label ≠ testid).

| `data-testid` | Element | Notes |
|---|---|---|
| `fleet-open` | the header "Index" button (was "Fleet") | opens the panel; **testid unchanged** for the contract |
| `fleet-overlay` | the modal overlay (the dim backdrop) | one at a time; reopening removes the prior |
| `fleet-panel` | the panel root | |
| `fleet-close` | the × close control | removes `fleet-overlay` |
| `fleet-body` | the panel body (now the two-section parent) | |
| `services-body` | the **services** section container (one `service-row` each) | #128; degrades independently of the fleet section |
| `services-error` | services read failed (a malformed `config/services.yml`) | #128; shown instead of the service rows |
| `services-empty` | the "no services declared" state | #128; shown when `list_services()` is empty |
| `service-row` | a per-service row (link-only) | #128; `data-name` = service name; `.muted` when `enabled=false`; the name is text, the URL is a `service-link` when it is a web link and plain text followed by `(not a web link)` otherwise (C19) |
| `service-link` | the service's URL as a link inside `service-row` | built by `LINK()`: opens in a new tab; absent when the URL is unresolvable or not http(s) |
| `fleet-classes` | the **fleet** section container (one `fleet-class` each) | #128; a wrapper so the two sections degrade independently |
| `fleet-error` | the error line (a malformed inventory/module) | shown instead of the fleet list |
| `fleet-empty` | the "nothing onboarded yet" state | shown when `list_fleet()` is empty |
| `fleet-class` | a per-class row | `data-key` = the device-class key |
| `fleet-class-cap` | a per-class capability badge (clickable) | `data-cap="telemetry\|backup\|logging"` + `data-key` + `data-state="yes\|no"`; opens the shared capability dialog for that class (declared **or** addable — the dialog detects) |

The Index panel also carries the **Automations** section (registered app-store units → `automation-row`/`automation-configure`, documented in the configure-dialog section below) and the **Supply chain** section (MF-5 — collection install provenance, `GET /api/provenance`, read-only): a green "installed" must never imply a signature was checked, so every `unsigned-pinned` collection shows an **amber** badge + the honest pin+checksum note.

| `data-testid` | Element | Notes |
|---|---|---|
| `provenance-classes` | the **Supply chain** section container | independently degradable (own fetch); read-only over `service/provenance.fleet_provenance` |
| `provenance-summary` | the one-line counts summary | "N of M unsigned-pinned … K sha256-recorded" (names/counts only) |
| `provenance-row` | a per-collection row | `data-name` = the collection; pin / pin_kind / policy / digest-recorded / the honest note |
| `provenance-class` | the per-row provenance badge | `data-class="unsigned-pinned\|signed"`; **amber** (`badge maybe`) for unsigned-pinned, green (`badge yes`) for signed |
| `provenance-empty` | the "provenance not generated yet" state | a fresh tree pre-bootstrap (sidecar absent) — degrade, never error |
| `provenance-error` | the provenance read failed | degrade, never crash |

The Supply chain section also carries an **images sub-panel** (C1 — report 22 §7.3, the MF-5 honesty extended to the
image supply chain, `GET /api/image-provenance`, read-only over `service/provenance.image_provenance`): a
`digest-pinned` image shows an **amber** badge (Docker verifies the `@sha256:` on pull — **not** a signature), a
`local-build` image a neutral badge, and `signed` (green) is the deferred cosign rung (never true today).

| `data-testid` | Element | Notes |
|---|---|---|
| `image-provenance-classes` | the images sub-panel container | independently degradable (own fetch); read-only over `service/provenance.image_provenance` |
| `image-provenance-summary` | the one-line counts summary | "N of M image(s) digest-pinned … K local-build" (names/counts + public digests only) |
| `image-provenance-row` | a per-image row | `data-name` = the image; `sha256:<short>…` + the honest note |
| `image-provenance-class` | the per-row image badge | `data-class="digest-pinned\|local-build\|signed"`; **amber** (`badge maybe`) for digest-pinned, neutral for local-build, green (`badge yes`) for signed |
| `image-provenance-empty` | the "image lock not present" state | degrade, never error |
| `image-provenance-error` | the image-provenance read failed | degrade, never crash |

## Pending panel (standalone modal — opened from the header "Pending" button) — #129

A read-only list of the proposals STAGED (`proposed/<run_id>`) and awaiting promotion (`/api/pending` →
`kontroll.service.pending.list_pending`, a pure read of the canonical bare repo). Each row carries the run_id (the
value pasted into the Semaphore `promote-proposal` survey), the tip subject + changed-path NAMES, a copy-run_id
button, and a STATIC Semaphore-task instruction. The panel **never promotes** — promote is the separate,
admin-authed Semaphore app (C10 two-key); there is deliberately no Promote/Approve/Reject control here.

| `data-testid` | Element | Notes |
|---|---|---|
| `pending-open` | the header "Pending" button | opens the panel |
| `pending-overlay` | the modal overlay (the dim backdrop) | one at a time; reopening removes the prior |
| `pending-panel` | the panel root | |
| `pending-close` | the × close control | removes `pending-overlay` |
| `pending-body` | the panel body (one row per proposal, then `pending-unrecognised` when there is one) | |
| `pending-error` | the error line (a git/read failure) | shown instead of the list; painted with `ET()` (C19) |
| `pending-empty` | the "nothing pending" state | the server's `note` (unarmed deploy / no canonical / a canonical that could not be read — the note then says why); quiet grey only when that is all it is: painted as a warning when `note_kind` is `unreadable`, and when there are no proposals but there are refs under `pending-unrecognised` |
| `pending-main-note` | a warning above the rows | present when `refs/heads/main` is missing or is not an ordinary branch (`main_note`): no row is called promotable against it |
| `pending-skipped-note` | a warning above the rows | present when git said it left a ref under the prefix out of its listing (`skipped_note`) |
| `pending-row` | a per-proposal row | `data-run-id` = the run_id |
| `pending-head` | the row's `<h3>` — the run_id as text, with staged-at and the proposal's **full** commit id as a child `<span class="meta">` | painted with `ET()`: the run_id is a git **ref name**, foreign and unpromoted (C19); `pending-stale` is appended here when stale |
| `pending-subject` | the proposal's commit subject line (when present) | painted with `ET()` — whatever the proposal's author committed (C19); bytes that are not text arrive escaped |
| `pending-unrecognised` | the "Not proposals" card after the rows | present only when the canonical holds a ref under `refs/heads/proposed/` that is not a proposal (a name the service does not mint, a symbolic ref, a ref that names no commit); **no** copy, promote or discard control inside |
| `pending-unrecognised-row` | one such ref: its name, its object id and the reason | painted with `ET()` — a ref name written by another hand (C19) |
| `pending-copy-runid` | the "copy to promote" button inside a row | copies `<run id>@<full commit id>` — the form the promote takes (a row with no full id copies the bare run id); clipboard + a select-text fallback on plain-HTTP dev |
| `pending-paths` | the container of what the proposal changes (when it changes anything) | holds one `pending-change` per file |
| `pending-change` | one change: a status letter and one NAME | names only — never file contents (no C8 weight); a moved file is two entries (`D` old name, `A` new name); one element per entry, so a name cannot read as two entries; painted with `ET()` (C19) |
| `pending-changes-unread` | the warning that the row's change list could not be read | present when `changes_read` is `false`; the row then carries no `pending-promote-hint` |
| `pending-unknown-hint` | the warning in place of the recipe when `promotable` is `null` | "could not be told" is neither stale nor promotable |
| `pending-path-inexact` | the note under `pending-paths` that a name is shown escaped | present only when an entry has `exact: false` (a name that is not UTF-8, or holds a character that does not print) |
| `pending-promote-hint` | the static Semaphore `promote-proposal` instruction inside a row | a signpost, **never** a run trigger (C10); rendered only when `promotable` is `true` (or absent from the payload) and the row's changes were read |
| `pending-stale` | a red "stale — re-propose" tag in the row `<h3>` | shown when `pending.promotable === false` (the proposal is non-FF: `main` advanced past it — the FF-race) |
| `pending-stale-hint` | the re-propose guidance inside a stale row | replaces `pending-promote-hint` when stale (a promote would be refused non-fast-forward) |
| `pending-re-propose` | the Re-propose button inside a stale row (FF-race Move 1) | shown only when stale **and** the proposal is an onboard (`repropose_hint`); `reproposeFromPending` closes the panel + navigates to the class's onboard dialog — a navigation affordance, **never** a promote/stage (C10) |
| `pending-discard` | the Discard button inside a stale row (FF-race Move 2) | **stale-only**; opens a two-step confirm — a single click never deletes |
| `pending-discard-confirm` | the confirm button of the two-step discard | POSTs `/api/pending/<run_id>/discard` → the server compare-and-deletes the ref (C10-safe un-stage, **never** a promote); on success the row is removed |
| `pending-discard-cancel` | the cancel button of the two-step discard | restores the row to the un-confirmed Discard button |
| `pending-discard-error` | the inline error after a refused discard | 400 (bad run_id) / 404 (no such proposal) / 409 (the ref moved) — the row is left intact |

## Discovery panel (standalone modal — opened from the header "Discovery" button) — discovery-inbox Rung 1b

A read-only inbox of un-onboarded hosts an already-onboarded device's OWN lease view sees (`/api/discovery` →
`kontroll.service.discovery.read_inbox`, a pure read of the git-ignored cached sweep artifact — the sweep runs
out-of-band, SECURITY C18). Each row's **"Onboard this"** stashes the discovered host and pre-fills it into the
EXISTING onboard form's `field-host` (after the operator searches + picks the device class — Rung 1 doesn't guess
it); the human still onboards + promotes (C10 — **no auto-onboard**). EVERY device byte (hostname/ip/mac) is painted
via `ET`/`textContent` — a lease is attacker-influenceable, so a `<script>` hostname renders as literal text (F6).

| `data-testid` | Element | Notes |
|---|---|---|
| `discovery-open` | the header "Discovery" button | opens the panel |
| `discovery-overlay` | the modal overlay (the dim backdrop) | one at a time; reopening removes the prior |
| `discovery-panel` | the panel root | |
| `discovery-close` | the × close control | removes `discovery-overlay` |
| `discovery-body` | the panel body (one row per un-onboarded candidate) | |
| `discovery-error` | the read-failed line (a malformed artifact/inventory) | shown instead of the list |
| `discovery-empty` | the "nothing new / no sweep yet" state | renders the honest empty note |
| `discovery-swept` | the "last swept … · N source(s), M ok" meta line | a read-only signpost (the sweep is out-of-band) |
| `discovery-row` | a per-candidate row | `data-mac` = the normalized MAC (stable id); hostname/ip painted via `textContent` (F6) |
| `discovery-vendor` | the vendor-hint line inside a row (the IEEE OUI→vendor lookup for the candidate's MAC) | advisory DISPLAY ONLY — never a class/collection guess (F8); painted via `textContent`; absent when the MAC has no OUI match |
| `discovery-source` | the "seen by: `<class>`" provenance line inside a row | the onboarded class that reported the host |
| `discovery-onboard` | the per-row **"Onboard this"** button | stashes the host + pre-fills the EXISTING onboard form (no new onboard/stage path) |
| `discovery-prefill-banner` | the "Onboarding `<ip>` …" banner above the results | the visible, clearable indicator that a discovered host is stashed for the next onboard form |
| `discovery-prefill-clear` | the "clear" control inside the banner | resets the stash (no silent stale pre-fill) |

The pre-filled onboard form then carries its EXISTING testids (`onboard-form`, `field-host`, `field-key`, …); the
e2e asserts on `field-host` to prove the pre-fill landed — no new onboard testid (the reuse is the point).

## App-store / runnable-unit configure (R3)

The configure stage of the GUI-actuation app-store: an actuation unit's CURATED knobs (`actuation/<key>/unit.yml`
`knobs:`) render through the ONE knob renderer, exactly like a capability's params. The projection comes from
`/api/configurable?kind=actuation-unit&id=<key>` (`source_kind: unit`); the values re-validate server-side via the
write-free `POST /units/{key}/configure`. This is the worked-extraction MVP surface (curated knobs, **not** argspec
extraction). The create-unit **dialog chrome** (badge → pick → review → create) ships in the section below; the
configure pane that MOUNTS this `unit-config-fields` group on an *already-created* unit ships in the **configure
dialog** section below (opened from the Index *Automations* section; a zero-knob unit renders an honest
`unit-config-noknobs` note and still previews/stages).

| `data-testid` | Element | Notes |
|---|---|---|
| `unit-config-fields` | the curated-knob GROUP container | filled by `renderKnobGroup` over the `unit` group (`source_kind: unit` → `renderKnob` per knob) — the configure form |
| `knob-<key>` | one rendered knob row | **reused** (the existing per-knob testid); `[data-param]` widget inside, one per curated knob |

## App-store / runnable-unit dialog — create-unit (standalone modal, opened from the `unit-badge`)

The CREATE chrome that AUTHORS an actuation unit from a searched collection (the dialog that mounts the create-unit
substrate, `gui/app.py` `api_units` + `api_actuation_create`). Flow: `unit-badge` → `openUnitDialog` → pick a
runnable unit (`unit-list`/`unit-row`) → Review (propose) → the derived target/pin/provenance (`unit-provenance`,
or the **Tier-cap refusal** `unit-tier-cap`) → acknowledge (`unit-blast-ack`) → Create (`unit-create`) → the staged
result (`unit-result`, reusing `renderCapPromote`) + the post-promote hand-off (`unit-enact`). The GUI **never**
promotes (no promote button — C10 two-key). `collection` is data on the dialog, never a per-unit testid.

| `data-testid` | Element | Notes |
|---|---|---|
| `unit-badge` | the "⚙ Automations" badge on a result card | `data-collection`; opens `openUnitDialog(collection, version)`; absent on a search card (provenance `declared` — the Generic SSH host, whose collection ships inside ansible-core) |
| `unit-overlay` | the modal overlay | one at a time; reopening removes the prior (modal-lifecycle convention) |
| `unit-dialog` | the dialog root | `data-collection` discriminates which collection |
| `unit-close` | the × close control | removes `unit-overlay` |
| `unit-body` | the active-stage pane container | each stage (list → review → result) mounts here |
| `unit-list` | the runnable-unit list container | one `unit-row` per unit (from `GET /api/units/<collection>`) |
| `unit-row` | a per-unit row | `data-unit` + `data-kind` (`collection_playbook`\|`role`) |
| `unit-pick` | the per-row Select control | advances to the Review pane |
| `unit-list-empty` | "this collection ships no runnable units" | honest empty (a modules-only collection) |
| `unit-list-error` | the units read failed / not installed | degrade, never crash (404 / probe failure) |
| `unit-blast` | the blast-radius `<select>` | `this-device`\|`VLAN`\|`LAN`\|`WAN`\|`all` (the access-chain severity) |
| `unit-review` | the Review (propose) button | proposes the create plan, writing nothing |
| `unit-provenance` | the would-create `<pre>` (key/device-class/pin/blast/paths) | `data-pin` carries the exact `==` pin; writes nothing yet |
| `unit-tier-cap` | the create-time **Tier-cap refusal** pane | shown for an unsigned install onto `edge_firewall`/`core_switch` (403); **no** Create offered |
| `unit-review-error` | a propose failure (invalid/no_module/not_enabled/collision) | degrade |
| `unit-blast-ack` | the content-trust + blast acknowledge gate | `data-blast`; **gates** `unit-create` (disabled until acked) |
| `unit-ack-row` | the acknowledge row container | holds `unit-blast-ack` |
| `unit-create` | the Create (propose→`proposed/<run_id>`) button | the `cap-promote` analogue; **disabled** until `unit-blast-ack` |
| `unit-create-row` | the Create button row container | holds `unit-create` |
| `unit-result` | the staged-ref + run_id `<pre>` | **reuses `renderCapPromote`** verbatim |
| `unit-enact` | the post-promote hand-off pane | promote → configure the unit's knobs → first run is `--check` |

## App-store / runnable-unit dialog — configure an existing unit (Index Automations section → configure modal)

The post-create CONFIGURE flow: the Index panel grows a third **Automations** section listing the REGISTERED units
(`GET /api/actuation`); each row's **Configure** opens a modal that mounts the curated-knob group (the same
`/api/configurable?kind=actuation-unit` projection + `renderKnobGroup`), then **Preview** (`POST /api/actuation/<key>`
`apply:false` → the would-run play, writing nothing) → **Stage** (`apply:true` → `vars.yml` to `proposed/<run_id>`).
A knob edit invalidates the preview+token (re-Preview required). A zero-knob unit shows `unit-config-noknobs` and
still previews/stages. The GUI **never** promotes (no promote button — C10 two-key); `<key>` is data on the dialog.

| `data-testid` | Element | Notes |
|---|---|---|
| `automations-classes` | the Index *Automations* section container | independently degradable (own fetch); not asserted directly |
| `automation-row` | a per-registered-unit row | `data-key` = the unit key; derived target/pin/knob-count/configured (NAMES only — no value) |
| `automation-configure` | the per-row Configure control | opens `openUnitConfigDialog(key)` on top of the Index panel |
| `automations-empty` | "No automations yet — create one…" | honest empty (the registry has no units) |
| `automations-error` | the registry read failed | degrade, never crash |
| `unit-config-overlay` | the configure modal overlay | one at a time; reopening removes the prior |
| `unit-config-dialog` | the dialog root | `data-key` discriminates which unit |
| `unit-config-close` | the × close control | removes `unit-config-overlay` |
| `unit-config-body` | the active-stage pane container | knob form → preview → result mount here |
| `unit-config-fields` | the curated-knob GROUP container | **reused** (R3) — filled by `renderKnobGroup` over the `unit` group |
| `unit-config-noknobs` | the "no curated knobs" note (zero-knob unit) | still previewable/stageable |
| `unit-config-preview` | the Preview button | proposes the would-run play, writing nothing |
| `unit-config-preview-row` | the Preview button row container | holds `unit-config-preview` |
| `unit-config-play` | the would-run play `<pre>` (target + the rendered play + `--check`-first) | writes nothing yet |
| `unit-config-error` | a preview failure (422 invalid values / 404 no unit) | degrade |
| `unit-config-stage` | the Stage (propose→`proposed/<run_id>`) button | the `cap-promote` analogue; appears after a clean Preview |
| `unit-config-stage-row` | the Stage button row container | holds `unit-config-stage` |
| `unit-config-result` | the staged-ref + run_id `<pre>` | **reuses `renderCapPromote`** verbatim |
| `unit-config-enact` | the post-promote hand-off pane | promote → the first run is a `--check --diff` dry-run |

## Backup viewer panel (standalone modal — opened from the header "Backups" button) — #131 (C14, GATED)

A **read-only** per-device backup index → redacted single-rev view → unified diff over the secret-bearing capture
git store (`/api/backups*` → `kontroll.service.backups`, a pure `git log/show/diff` read of the `:ro`-mounted store).
Opened from the header **Backups** button (flat list of all capture-bearing devices) **or** the per-host
`fleet-backup-open` drill-in on an Index fleet card (passes the dotted `ansible_host` → `?host=`). Captures are
**redacted server-side** (defense-in-depth, not the boundary) and every read is audited NAMES-only. The MVP serves
**tracked history only** — an excluded on-disk capture (FortiGate) is listed but never rendered. No raw download, no
split view (deferred — C2). Diff lines are addressed by `data-t` (`add|del|ctx|hunk`), **not** a per-line testid.

| `data-testid` | Element | Notes |
|---|---|---|
| `backup-open` | the header "Backups" button | opens the panel across ALL capture-bearing devices |
| `fleet-backup-open` | the per-host drill-in button on an Index fleet host row | `data-host` = the dotted `ansible_host` → `openBackupPanel(host)` (filters via `?host=`) |
| `backup-overlay` | the modal overlay (the dim backdrop) | one at a time; reopening removes the prior |
| `backup-panel` | the panel root | |
| `backup-close` | the × close control | removes `backup-overlay` |
| `backup-body` | the panel body (one section per device) | |
| `backup-error` | a request/read failure line | shown instead of the list |
| `backup-unavailable` | the "captures store not mounted / no history" state | `available:false` — **never a crash** (a fresh node without the mount) |
| `backup-empty` | the "no backups captured yet" / "no history for this device" state | `available:true` + no devices/revisions |
| `backup-device` | a per-device section card | `data-file` = the capture filename (the index key) |
| `backup-excluded-note` | the "latest only — excluded from history; not rendered" note | shown for a `diffable:false` (capture-exception) file — listed, never served |
| `backup-index` | the per-device version-control container | holds the two rev pickers + the view/diff actions + the render target |
| `backup-base` / `backup-compare` | the two rev `<select>`s (older / newer) | populated from `/revisions`; defaults base=2nd-newest, compare=newest |
| `backup-view-btn` / `backup-diff-btn` | the View-base / Diff actions | |
| `backup-out` | the shared view/diff render target inside a device section | |
| `backup-view` | the single-config `<pre>` (REDACTED) | line-numbered gutter; every line painted with `textContent` (no XSS) |
| `backup-view-head` | the view header (`file · short-rev`) | |
| `backup-redacted-note` | the "secrets masked (best-effort) — see SECURITY.md C14" caveat | shown when `redacted:true` (view **and** diff) |
| `backup-truncated-note` | the "view truncated" note | shown when the file exceeds the line ceiling |
| `backup-diff-head` | the diff header (`file · base → compare · +A −D`) | |
| `diff-view` | the unified-diff `<pre>` container | each line a `<div class="dl …">` addressed by `data-t ∈ add\|del\|ctx`; `textContent` everywhere |
| `diff-hunk` | a hunk-header line (`@@ … @@`) | `data-t="hunk"` |
| `diff-empty` | the "identical at these revisions" state | |

## Settings panel (standalone modal — opened from the header "Settings" button) — #135 (Phase 1, read-only)

A read-only, value-free snapshot of the platform configuration (`/api/settings` → `kontroll.service.settings.
read_view`), grouped into four area cards: **identity** (mgmt_ip/domain/tls_mode/source_of_truth), **backup**
(`backup_remotes`), **fleet** (`enabled_modules`), **status** (arming posture, running services, the secret-domain
NAMES roster). Phase-1 is **SURFACE only** — no edit controls, stages nothing, serves no secret value (C11); the
EDIT/GUARD write surfaces (mgmt_ip re-IP, tls flip, fleet removal) are a later C10-staged phase.

| `data-testid` | Element | Notes |
|---|---|---|
| `settings-open` | the header "Settings" button | opens the panel |
| `settings-overlay` | the modal overlay (the dim backdrop) | one at a time; reopening removes the prior |
| `settings-panel` | the panel root | |
| `settings-close` | the × close control | removes `settings-overlay` |
| `settings-body` | the panel body (the four group cards) | |
| `settings-error` | a whole-view read failure | shown instead of the groups |
| `settings-group` | an area-group card | `data-group="identity\|backup\|fleet\|status"` |
| `settings-<group>-error` | a per-group degrade message | independent degrade (`settings-identity-error`, etc.) — one bad source never blanks the panel |
| `settings-fleet-add-hint` | the "add via Onboard" signpost | read-only; not a control (removal lands in a later phase) |
| `settings-arming-badge` | the privileged-mutation ARMED/disarmed badge | read-only (FORBID-as-a-write — the C10 arming switch is a deploy posture, never a toggle) |
| `settings-services` | the running control-plane services line | reuses the `list_services` read |
| `settings-secrets-roster` | the secret-domain roster | NAMES + set/unset (`✓`/`—`) only — never a value (C11) |
| `settings-secrets-jump` | deep-link to the Secrets dialog | one secret-write seam — Settings never re-implements secret entry |

### Settings EDIT testids (Phase 5, #140 — the panel becomes editable)

Each editable identity/backup knob renders a `settings-knob` row with an `edit` control; each active fleet module
a `disable` control. Both open the **shared** reconfigure dialog (`openReconfigureDialog`) against
`/api/settings/<knob>` or `/api/settings/fleet/<module>`, which STAGES `proposed/<run_id>` (C10 — never promotes).
The severity gate is the shared `reconfigGate` (mgmt_ip → `reconfig-identity-confirm` type-to-confirm; domain/
tls_mode → `reconfig-redeploy-confirm`; fleet removal → `reconfig-remove-confirm`). `api_privileged` + `docker/.env`
have **no** edit control (FORBID — the status group surfaces arming read-only).

| `data-testid` | Element | Notes |
|---|---|---|
| `settings-knob` | an editable knob row | `data-knob="mgmt_ip\|domain\|tls_mode\|backup_remotes"`; a ⚠ marks a danger (GUARD) knob; `source_of_truth` stays a read-only `settingsRow` (no `settings-knob`) |
| `settings-trust-mode` | the F3 `trust_mode` row in the identity group | **READ-ONLY** (a plain meta row, NOT a `settings-knob` — no edit control; this edition has no auto-promoter). Shows the chosen promote posture (`separated`/`solo`/`—` if unset) + a note; `solo` notes that it behaves like `separated` on this edition |
| `settings-knob-edit` | the per-knob `edit` control | opens the reconfigure dialog (`kind=settings-knob`) for `data-knob` |
| `settings-fleet-row` | a per-module fleet row | `data-module=<name>`; carries the Disable control |
| `settings-fleet-disable` | the per-module `disable` control | opens the reconfigure dialog (`kind=settings-fleet`, no knobs — Propose computes the `remove`); GUARDED by `reconfig-remove-confirm` |
| `settings-fields` | the settings-knob group container (in the reconfigure dialog) | `configurable_view(kind=settings-knob)` `source_kind: settings` — the one pre-filled knob |
| `reconfig-consequence` | the consequence-naming warning pane | rendered by `reconfigPropose` from the plan's `consequence` (the descriptor `confirm_text` — re-IP disconnect / per-tls-mode / drop-monitoring); shown above the diff for a GUARD knob |

## Homepage editor panel (standalone modal — opened from the header "Homepage" button) — #136 (Phase 2)

The portal-arranging surface (`GET /api/homepage` → `kontroll.service.homepage.read_board`; `POST /api/homepage`
propose/apply). Choose which tiles appear (membership) + reorder sections/tiles; **Preview** shows the diff,
**Save** stages one `proposed/<run_id>` (C10 — the GUI never promotes). The generated fleet section renders locked.

| `data-testid` | Element | Notes |
|---|---|---|
| `homepage-open` | the header "Homepage" button | opens the editor |
| `homepage-overlay` / `homepage-panel` / `homepage-close` / `homepage-body` | the modal chrome | the standard lifecycle set |
| `homepage-error` | a board-read failure | shown instead of the editor |
| `homepage-section` | a section card | `data-section="<name>"`; a generated section shows 🔒 + locked controls |
| `homepage-section-up` / `homepage-section-down` | per-section reorder controls | splice the section order + re-render |
| `homepage-tile` | a tile row | `data-section` + `data-tile` discriminators |
| `homepage-tile-select` | the per-tile present/absent checkbox | disabled on a generated section's tiles |
| `homepage-tile-up` / `homepage-tile-down` | per-tile reorder controls | (absent on a generated section) |
| `homepage-propose` | the "Preview changes" button | POST `apply:false` — renders the diff, writes nothing |
| `homepage-apply` | the "Save + stage" button | POST `apply:true` — token-gated, stages `proposed/<run_id>`; widget-removal fires a `window.confirm` first |
| `homepage-diff` | the `<pre>` diff/preview pane | the current-vs-proposed unified diff |
| `homepage-result` | the `<pre>` staged-result pane | surfaces the run_id + the staged ref (`renderHomepageResult`) |

## Playwright selector patterns (the canonical locators)

```python
page.get_by_test_id("search-input").fill("cisco ios")
page.get_by_test_id("search-btn").click()
card = page.get_by_test_id("card").first                       # or .filter on data-collection
expect(card.get_by_test_id("suggested-backend-badge")).to_contain_text("netcommon_cli")
expect(card.locator('[data-testid="capability-badge"][data-cap="backup"]')).to_have_attribute("data-state", "yes")
card.get_by_test_id("onboard-toggle").click()
card.get_by_test_id("field-host").fill("192.0.2.10")
expect(card.get_by_test_id("field-apply")).not_to_be_checked()  # dry-run by default
card.get_by_test_id("run").click()
expect(card.get_by_test_id("output")).to_contain_text("ONBOARD")
```

Naming convention: lowercase kebab-case, named by role/action (not appearance); repeated rows
carry a stable `data-testid` + a `data-*` discriminator. `name=` attributes on form inputs stay
(they're the submit payload contract) — the `data-testid` is additive.

## See also
- [README.md](README.md) — test-suite layout + the mock seam
- [../docs/testing-standards.md](../docs/testing-standards.md) §3 — the data-testid SOP this inventories
- [e2e/test_onboard_flow.py](e2e/test_onboard_flow.py) — the consumer
- [../gui/templates/index.html](../gui/templates/index.html) — the template these annotate
