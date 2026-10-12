# gui — the onboarding web surface (alpha)

A **thin** web layer over the kontroll service layer + [`scripts/galaxy.py`](../scripts/galaxy.py).
It carries no logic of its own: **search, classify, AND onboard all call the `kontroll` service layer
in-process** (the same functions the [API](../api/README.md) uses — no subprocess; onboard mirrors
`api/routes/onboard.py`). The point is the operator's flow end-to-end with **no manual step other than
the search selection, the connection details, and one approval (promote)**:

```
search  →  see capability badges (A/B/L + suggested backend)  →  "Onboard this"
        →  enter key/group/host + creds  →  Run  →  a STAGED proposal  →  promote  →  managed device
```

## The flow is fully piped (propose-then-promote, C10)

Pressing **Run** with *apply* ticked calls the onboard service **in-process** — `build_onboard_plan` +
`apply_onboard_plan` + `gitio.commit_and_push(run_id=…)` — which does **all** of:

| Step | Who does it | Manual? |
|---|---|---|
| Pick a collection | operator (search + click) | ✅ the one selection |
| Connection details (host, group, creds) | operator (form) | ✅ the one detail entry |
| **Which credential fields to show** (SSH login for a switch, a token for a REST device — no dead boxes) | derived per-backend (F1 TIER-A), fetched from `GET /api/onboard/cred-fields` on the form's first open | automated |
| Module declaration `modules/<key>/module.yml` | onboard | automated |
| Drop-in inventory host (self-contained, inline SOPS cred lookups) | onboard | automated |
| `instance/fleet.yml` enable | onboard | automated |
| **Credentials → SOPS** (`sops --set`, per-host keys; in-process, never in subprocess argv) | onboard | automated |
| Commit (rationale-first + trailer) | onboard | automated |
| **Stage the canonical** → `proposed/<run_id>` (C10), never `main` | onboard | automated |
| Push to origin (offsite backup) | onboard (if "push" ticked) | automated |
| **Promote** `proposed/<run_id>` → `main` | operator / a Semaphore approve task | ✅ the one approval |
| Install the collection + live-verify (bootstrap) | a Semaphore enact, **after** promote | automated (deferred) |

**The deliberate trust boundary (surfaced, not relaxed):** when the deploy armed `KONTROLL_STAGE_PUSHES`
(the C10 privileged-mutation enablement, default off), the GUI can only **PROPOSE** — it pushes a
`proposed/<run_id>` staging ref, never `main`; a trusted `kontroll promote <run_id>` (FF-only) lands it. A
leaked/abused GUI can only park a *rejectable* proposal, never rewrite the canonical. Bootstrap is **deferred**
to after promote — the network surface runs no Ansible, and a host that lives only in an un-promoted proposal
can't be verified yet. See [SECURITY.md](../SECURITY.md) C10 +
[docs/privileged-mutation-enablement.md](../docs/privileged-mutation-enablement.md).

## Beyond onboarding: the "Secrets" and "Keys" dialogs (D)

Two header buttons turn the remaining **security-boundary** setup steps into guided GUI forms (the operator's
"automate everything, or GUI-onboard what crosses a security boundary" goal):

- **Secrets** — a domain picker (the registered [`secret-forms/`](../secret-forms/README.md)) → one shared
  dialog per SOPS domain (dashboards / semaphore / snmp / acme). Values are encrypted into
  `instance/secrets/<domain>.sops.yml` **on the box** and **staged** `proposed/<run_id>`; a value is **never**
  returned, logged, or committed — the result shows only the field NAMES set. `generate:` fields are minted
  server-side when ABSENT (first-time) or when the operator clicks `gen` (an explicit re-mint); a blank
  already-set generated field is KEPT (#141). A `rotatable:` field can be **re-saved with a new value** (rotation, #141):
  overwriting an already-set field fails CLOSED (HTTP 409 / `secret-overwrite-confirm`, NAMES only) until the
  operator acks, and — for a field with an `actuation:` block — the result shows a **post-promote hand-off**
  (`secret-enact`: `deploy-stack` recreate / `grafana-cli` reset / `semaphore-admin` recreate-then-`change-by-login`)
  the operator runs to make it live. The GUI actuates nothing itself (no docker socket). See
  [`secret-forms/README.md`](../secret-forms/README.md) § Rotation.
- **Keys** — a role picker (the [`key-roles/`](../key-roles/README.md) registry) → one shared dialog per role
  (break-glass / scoped-Semaphore / control-rotation). Generate mints an age keypair on the box, **shows the
  private key exactly once** for the operator to store (it is never persisted by the service), and adds only the
  **public** recipient to `instance/.sops.yaml` — additively, parse-verified, and staged. The re-wrap
  (`sops updatekeys`) is a deferred operator step, never auto-run. The **first** control key + `GUI_PASSWORD`
  remain a CLI `kontroll-init` bootstrap (a running GUI can't mint the key it needs to run).

Both ride the same auth + audit + C10 staging as onboard; the no-leak / show-once / additive-recipient
properties are **SECURITY.md C11**.

## The actionable config plane: one knob renderer (Phase 3, #137)

"If there are knobs and levers for an action or object, I want them exposed and actionable." The capability and
secret dialogs render through **one** data-driven renderer over **one** server-side projection, so exposing a
new lever is a descriptor edit, never UI code (the no-bespoke-config tenet):

- **`GET /api/configurable?kind=&id=`** → `service/configurable.configurable_view(kind, id)` (read-only,
  degrade-to-empty, pinned `assert_read_only`). A `capability:<cap>` wraps the read-only `suggest_view` and adds
  a **`params` knob group** per method + the **resource_stage** group; a `secret-domain` projects its **`fields`**
  group (NAMES only, never a value). The knob descriptor is a strict **superset** of the existing method-param +
  secret-form shapes — legacy descriptors map with no edit (`type` inferred `allowed`→`enum`). It is a **closed**
  dispatch over those two kinds, not a universal "configure-anything" registry.
- **The renderer** (`index.html`, top-level pure → `page.evaluate`-testable): `renderKnob` is a **type→widget
  table** (`enum`→`<select>`, `bool`→checkbox, `int`→number(min/max), `text`→pattern-checked text,
  `secret`→write-only password); `renderKnobGroup` is the **Stage-3 render-table** keyed by `source_kind`;
  `knobSelection` harvests by `data-param`. Every widened value is **re-validated server-side** against its
  descriptor (`service/_validate`, P0a) — the widget is convenience, the descriptor is the contract, the server
  is the guard. A `secret` knob is write-only and **never pre-filled** (C11).
- The write spine is unchanged: capability propose/promote still POST `/api/capability/<cap>`, secrets POST
  `/api/secrets/<domain>`, both STAGE `proposed/<run_id>` (C10).
- **App-store unit dialog (create-unit):** `GET /api/units/<collection>` lists a collection's runnable units (the
  Pick stage, read-only); `POST /api/actuation/create` AUTHORS an actuation `unit.yml` from a searched collection
  (propose `apply:false` → the derived key + a token; create `apply:true` → STAGE
  `instance/actuation/<key>/unit.yml` to `proposed/<run_id>`, C10). Mirrors `api/routes/actuation.py`'s create
  in-process. The descriptor is validated through the same fail-closed schema before it stages; a collection whose
  device-class targets `edge_firewall`/`core_switch` from an unsigned source is **refused 403** (the create-time
  Tier-cap; SECURITY.md C15-a). The GUI never promotes (C10 two-key — no promote button).
- **App-store unit dialog (configure an existing unit):** `GET /api/actuation` lists the **registered** units (the
  Automations index — read-only over `service/units.registered_units_view`, each unit's derived target/`==`-pin/
  knob-count/configured flag, NEVER a value; pinned `assert_read_only`); `POST /api/actuation/<key>` mirrors
  `api/routes/actuation.py::stage` in-process — **preview** `apply:false` renders the would-run play + resolved
  non-secret vars + a token (writing nothing), **stage** `apply:true` writes `instance/actuation/<key>/vars.yml`
  to `proposed/<run_id>` (C10). The curated knob group itself is the **same** read-only `/api/configurable?kind=
  actuation-unit&id=<key>` projection rendered by the **same** `renderKnobGroup` (the `unit` source_kind), so the
  configure form is zero new renderer code. Validate-before-stage + the anti-drift token (409) + no-clobber hold;
  the audit (`unit-registry`/`unit-config-stage`/`unit-config-result`) carries names/counts/run_id only (no value
  is a secret — SEC-2). The GUI never promotes (C10 two-key — no promote button). **Entry point:** the Index panel's
  third **Automations** section lists the registered units; a row's **Configure** opens `openUnitConfigDialog` —
  the knob form (`unit-config-fields` via `renderKnobGroup`) → **Preview** (the would-run play, writing nothing) →
  **Stage** (`unit-config-result` reuses `renderCapPromote`); a zero-knob unit (`unit-config-noknobs`) still
  previews/stages. `tests/e2e/test_unit_config_flow.py`.
- **Supply-chain provenance (MF-5 — no verification theatre):** `GET /api/provenance` (read-only over
  `service/provenance.fleet_provenance`, `assert_read_only`-pinned, audited `provenance-index` counts-by-class)
  projects the generated trust sidecar + the L2b digest lock into a per-collection class — **`unsigned-pinned`**
  (amber, verified by pin + checksum only) or **`signed`** (green, policy `required` + a keyring held) + the
  `digest_recorded` flag. The Index panel's fourth **Supply chain** section renders each collection with the amber
  badge + the honest note, so a green "installed" never implies a signature was checked. Reads no value/secret
  (SEC-2); `available:false` (HTTP 200) when the sidecar isn't generated yet.
  The same section carries an **images sub-panel** (C1, report 22 §7.3): `GET /api/image-provenance` (read-only over
  `service/provenance.image_provenance`, `assert_read_only`-pinned, audited `image-provenance-index`) projects
  `docker/images.lock.yml` into a per-image class — **`digest-pinned`** (amber: a recorded `@sha256:` Docker verifies
  on pull, **not** a signature), **`local-build`** (no digest ⇒ built on the node), or **`signed`** (green, cosign —
  deferred, never true today). So a digest-pin is never read as a verified signature. Reads only image names + public
  digests (no secret); `available:false` when the lock is absent.

**Safe reconfigure (Phase 4a, #138) — change a declared capability knob without a silent clobber.** A declared
telemetry/backup/logging method is no longer a wall: the picker marks it "· declared (reconfigure)", its knobs
open **pre-filled** with the current values (`current_values` → `suggest_view.current` → the projection's knob
defaults), and changing one + Propose surfaces a field-level **diff** (`reconfig-diff`) of `~ modify` /`+ add` /
`- remove`. A value clobber (`modify`/`remove`) keeps **Promote disabled** until `reconfig-overwrite-confirm` is
acknowledged; a pure add flows straight through. The write is a comment-preserving `upsert_into_block` (line-span
replace, never a flatten), still gated by the closed-allow-list `validate_params`. At promote the spine runs
`verify_no_drop` (refuses, `would_drop` 409, if it would silently drop a co-owner's declared method) and
`verify_token` over `[text_after, severity_decision]` (anti-drift + no-quiet-upgrade). **The token is anti-drift,
not authorization** — the anti-clobber controls are `verify_no_drop` + the human confirm + C10's two-key promote.

**Heavier reconfigure (Phase 4b, #139) — module identity + inventory host-var.** Two higher-blast surfaces ride
the **one** data-driven reconfigure dialog (`openReconfigureDialog` — the shared knob renderer pre-filled with
`current` + the `reconfig-diff` pane + the shared `reconfigGate`), opened from the **Index** fleet card:
- **⚙ identity** (a per-class badge) → `module-identity` re-classify of `secrets_domain`/`inventory_group`/
  `backend`/`role`/`status` (`POST /api/identity/<key>` → `service/identity.py` → `upsert_top_level_key`,
  comment-preserving). An identity change is the heaviest tier — it **re-homes hosts / orphans creds** — so the
  `settings/module-identity.yml` descriptor upgrades it to `severity: identity`, gated by a **type-to-confirm**
  (`reconfig-identity-confirm` — retype the new value); **at most one** identity key per proposal.
- **edit IP** (a per-host control) → `inventory-host` change of `ansible_host` (`POST /api/host/<key>/<host>` →
  `service/hostvars.py` → `gitio.owned_merge`, which lifts `write_inventory_host`'s silent `.update()` into a
  reported diff). MVP is `ansible_host` (validated by the P0a `ipv4` guard); the host's **templated cred-lookup
  vars stay read-only** (rotating a credential is the secret path, not this surface).

Both stage `proposed/<run_id>` (C10 — the GUI never promotes), reuse `verify_no_drop` + the severity confirm, and
re-derive severity server-side for the audit. Secret-field reconfigure remains a later phase (needs the M-R-B
NAMES-only `will_overwrite` first).

## The "Index" panel (#121, widened #128) — services + what you've onboarded

A third header button, **Index** (was "Fleet"; the `fleet-*` testids + `/api/fleet` are unchanged), opens a
**read-only**, two-section panel:

- **Control-plane services** (`GET /api/services` → `kontroll.service.fleet.list_services`, a pure read of
  **`config/services.yml`** — SHIPPED + repo-tracked, NOT an instance overlay) — the fixed set kontroll stands up
  (onboard-GUI / Semaphore / Homepage / Grafana / Prometheus / API), each link templated from this instance's
  `KONTROLL_MGMT_IP`/`KONTROLL_DOMAIN`. Link-only (no capability/backup drill-in — it's the control plane, not an
  onboarded device); an unresolvable base renders the name without a dead link.
- **Onboarded fleet** (`GET /api/fleet` → `list_fleet`, a pure read of `instance/fleet.yml` +
  `modules/<key>/module.yml` + `instance/inventory/*`) — one row per enabled class with its declared capabilities
  (telemetry / backup / logging, from the module's `metrics:`/`backup:`/`logs:` blocks) and its inventory hosts.
  Each **capability badge reuses the same `openCapabilityDialog(cap, key)`** the search cards use — so the operator
  actuates telemetry/backup/logging per class straight from the Index (declared **or** addable — the dialog
  detects).

Each section fetches + degrades independently (a malformed `config/services.yml` can't blank the fleet section,
and vice-versa). Read-only, so it never gates onboarding (INVARIANT D*).

## The "Pending" panel (#129) — proposals awaiting promotion

A header button, **Pending**, opens a **read-only** list of every change STAGED as `proposed/<run_id>` and not yet
promoted (`GET /api/pending` → `kontroll.service.pending.list_pending`, a pure read of the canonical bare repo via
`git for-each-ref`). Each row shows the **run_id** (with a one-click **copy** affordance), the tip subject, the
changed-path NAMES (never file contents), and a **static** instruction to run the Semaphore `promote-proposal`
task. **The panel never promotes** — advancing `main` is the separate, admin-authed Semaphore app (C10 two-key);
there is deliberately no Promote/Approve/Reject control. It is the standing answer to "where do I get the run_id to
promote?" once the propose dialog is gone. On an unarmed deploy (no canonical / commits-direct-to-main) it shows an
honest empty state. A proposal `main` has advanced *past* (non-fast-forward — the FF-race) is badged **"stale —
re-propose"** instead of the promote recipe (its promote would be silently refused); a stale **onboard** proposal
also offers a **Re-propose** button that navigates back to its device class's onboard dialog to re-run it against
current `main` — a navigation affordance, still never a promote (C10). A stale row additionally offers a
**Discard** button (stale-only, two-step confirm) → `POST /api/pending/<run_id>/discard` → the server
compare-and-deletes the dead `proposed/<run_id>` ref; this **un-stages, never promotes** (C10-safe), so the reaper
cannot advance `main` — it only removes cruft the FF-race left behind.

## The "Backups" panel (#131, C14 — GATED) — view + diff device config history

A header button, **Backups** (and a per-host `fleet-backup-open` drill-in on an Index fleet card), opens a
**read-only** per-device backup **index → redacted single-rev view → unified diff** over the secret-bearing config
capture store (`GET /api/backups{,/<file>/revisions,/<file>/view,/<file>/diff}` →
`kontroll.service.backups` — a pure `git log/show/diff` read of the `:ro`-mounted captures git). This is the **viewer**;
it is DISTINCT from `service/backup.py`, the backup-capability *builder* (which writes the `backup:` block + the
schedule). Captures hold crown-jewel device secrets (running-config hashes, PSKs, SNMP communities), so this is the
**riskiest GUI surface** and is **fail-CLOSED by construction** (SECURITY.md **C14**):

- **`:ro` mount, onboard-gui only** — a GUI RCE can read, never rewrite/`git rm` the history (compose-pinned on the
  literal `:ro` suffix). The store is the SAME `${KONTROLL_BACKUPS_DIR}` the Semaphore writer mounts.
- **Read-only by construction** — only `git log/show/diff/ls-files`; the AST + argv git-verb pins
  (`tests/unit/_readonly_pins.py`) fail CI if any mutating subcommand appears. No restore/rollback/prune.
- **Validated before any `git show`** — a 7–40-hex rev + a file that must be a MEMBER of the store's own index
  allow-list (stronger than a path regex) close traversal/injection.
- **Redacted by default, redact-then-diff** — known per-vendor secret token shapes are masked server-side
  (`config/capture-redactions.yml`) before the bytes leave the process; the diff runs on masked text. Redaction is
  **defense-in-depth, not the boundary** (the boundary is auth + TLS + mgmt-bind + `:ro`); a novel token shape can
  still leak into the view (the tracked C14 residual). The view paints every byte with `textContent` (zero XSS).
- **Every read audited NAMES-only** (`backup-{index,revisions,view,diff}`, file + rev, never a value).
- **Graceful-absent** — no mount ⇒ `{available:false}`, the panel shows "not mounted", never a crash.

The MVP serves **tracked history only** — an excluded on-disk capture (FortiGate, history-excluded for its
non-deterministic export) is *listed but never rendered* (its secret-dense blob stays off the wire). Raw
(unredacted) download + side-by-side diff are deferred (C2). The feature ships behind a binding security GATE whose
last step (**G5**) is a human security sign-off.

## The "Settings" panel (#135 read-only · #140 EDIT) — your instance, viewable + editable

A header button, **Settings**, opens a snapshot of the platform configuration
(`GET /api/settings` → `kontroll.service.settings.read_view`), grouped into four area cards: **identity**
(`mgmt_ip`/`domain`/`tls_mode`/`source_of_truth` from `instance.yml`), **backup** (`backup_remotes`), **fleet**
(`enabled_modules`), and **status** (the privileged-mutation arming posture from the GUI's own
`KONTROLL_STAGE_PUSHES` env, the running services, and the secret-domain roster by NAME with a set/unset badge,
deep-linking to the Secrets dialog). It serves **no secret value** (NAMES + set/unset only — C11) and never reads
`docker/.env`. Each group degrades independently. The arming switch is surfaced read-only — never a toggle (a
settings page that could disarm itself is a C10 footgun).

**Settings EDIT (Phase 5, #140) — stage a platform change behind the right-weight guard.** The identity/backup/
fleet rows are now editable: each `edit` (`settings-knob-edit`) / `disable` (`settings-fleet-disable`) control
opens the **shared** reconfigure dialog (`openReconfigureDialog`) against `/api/settings/<knob>` or
`/api/settings/fleet/<module>` → `service/settings.build_plan`/`build_fleet_disable_plan` (ride `run_promote`).
The three danger knobs each carry a consequence-naming confirm (rendered in `reconfig-consequence`): **`mgmt_ip`**
(`severity: identity` → type-to-confirm the new IP; the re-IP severs this session — reconnect at the new IP),
**`tls_mode`/`domain`** (`redeploy` → the ack gate), **`enabled_modules` removal** (`remove` → the drop-monitoring
confirm; adding stays the Onboard flow). **`backup_remotes`** is the low-blast EDIT proof. Every write STAGES
`proposed/<run_id>` (C10 — the GUI never promotes), validates server-side (P0a), and runs `verify_no_drop` over
the full instance.yml. **FORBID:** `api_privileged` (the arming switch), `docker/.env`, and `.sops.yaml`
recipients have **no** edit control — a knob not in the `settings/<area>.yml` descriptor set is a 404; the page
edits the INPUTS, never the rendered `.env`.

## The "Homepage" editor (#136) — arrange the portal from :8443

A header button, **Homepage**, opens an editor to **choose which control-plane tiles appear** and **reorder
sections + tiles**. The `:3000` portal (gethomepage) only READS YAML, so the authenticated `:8443` GUI is the
authoring surface that rewrites the overlay `instance/dashboards/homepage/{services,settings}.yaml`
(`GET /api/homepage` → `kontroll.service.homepage.read_board`; `POST /api/homepage` propose/apply). **Preview**
shows the unified diff; **Save** stages one `proposed/<run_id>` (C10 — the GUI never promotes; the proposal lands
in the Pending panel). The rewrite is a **comment-preserving line-span serializer** that can only relocate/omit
tiles the file already contained (never synthesizes a tile — C11); the generated `gen-homepage` fleet block is
locked (operator-immovable contents). MVP is item-selection + move-up/down; drag-drop and tile add/edit are
deferred follow-ons. C3 is untouched — the edit is authored on the auth'd `:8443` app; the served YAML stays
secret-free.

## Run (directly on the control VM)

```bash
python3 -m venv ~/.venv/kontroll-gui && ~/.venv/kontroll-gui/bin/pip install -r gui/requirements.txt
export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt           # for cred encryption
export GUI_USER=admin GUI_PASSWORD='<from-sops>'              # auth (REQUIRED — fail-closed)
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \         # self-signed TLS (mgmt VLAN)
  -keyout /tmp/gui.key -out /tmp/gui.crt -subj /CN=kontroll-gui
export GUI_TLS_CERT=/tmp/gui.crt GUI_TLS_KEY=/tmp/gui.key
GUI_BIND=${KONTROLL_MGMT_IP} GUI_PORT=8443 ~/.venv/kontroll-gui/bin/python gui/app.py
```

Then open `https://<mgmt-ip>:8443` (Basic auth). The composed-stack fragment
([docker/services/onboard-gui.yaml](../docker/services/onboard-gui.yaml)) packages this
on the runner image for `deploy-stack.yml` (it provisions the cert + the SOPS password).

## Security posture (read before exposing)

This surface is **privileged**: it can write the repo, encrypt secrets, push, and run
Ansible against the lab. Treat it like the Semaphore runner. **Implemented:**
- **Auth — HTTP Basic, fail-closed**: the app *refuses to start* without `GUI_PASSWORD`;
  constant-time compare; the password comes from SOPS (`dashboards: gui_admin_password`).
- **TLS**: serves HTTPS from `GUI_TLS_CERT`/`GUI_TLS_KEY` (warns + HTTP only if unset).
- **Audit log** (`GUI_AUDIT_LOG`): every onboard action + auth-denial — timestamp, client
  IP, what (collection/key/host/flags) — **never** credentials.
- Actuation is explicit: the UI dry-runs unless **apply** is ticked.
- **Foreign bytes render as TEXT, never markup** (SECURITY.md C19). The rule is an allow-list: only markup written
  as a literal in `index.html` reaches `innerHTML`. `E()` assigns `innerHTML` and takes literals only; `ET()` assigns
  `textContent` and `T()` makes a text node, and they paint **every** other value — device bytes, Galaxy-published
  bytes, a staged proposal's ref name, subject and paths, repository values, API error strings. A link is built
  only by `LINK()`, which sets an `href` when `safeHref()` — the browser's own URL parser — resolves the value to
  http(s), and a dynamic request-path segment is `encodeURIComponent`-ed. This is not advisory:
  `tests/unit/test_card_paint_gate.py` scans every `E()` markup argument and every `.innerHTML =`, refuses the
  other spellings of the sink that a token scan can read (a computed property name is beyond it), holds every
  request to a direct `fetch()` with an encoded path, and fails the build on the first non-literal, whatever the
  value is called;
  `tests/e2e/test_paint_rule.py` proves the result in a browser for the panels and dialogs that changed. It exists
  because the rule was broken twice: until 2026-07-27 the search card painted a collection's Galaxy description
  through `innerHTML`, and until 2026-10-12 the Pending panel painted a staged proposal's ref name, subject and
  paths the same way. Add a value to the page ⇒ paint it with `ET()`/`T()`, or leave an empty element in the
  literal and fill its `textContent`; add a link ⇒ `LINK(url, text)`.
- **CSP**: `object-src`/`base-uri`/`frame-ancestors` are set on every response, plus `nosniff`. `script-src` is
  *not* set — the page carries a large inline `<script>` and inline `onclick` attributes on its header buttons;
  a per-response nonce with those handlers rebound is the prerequisite, and CSP is the second layer regardless.
  The paint rule above is the actual control.
- **Still: bind to the mgmt network only; never expose it publicly.**

**Pre-beta remaining:** a real CA-signed cert (vs self-signed) and SSO (Authelia) if it's
ever fronted beyond the mgmt VLAN; rate-limiting.

## See also
- [../scripts/README.md](../scripts/README.md) — `galaxy.py`, the engine
- [../docs/capability-matrix.md](../docs/capability-matrix.md) — the design
- [../docs/qa-and-release-pipeline.md](../docs/qa-and-release-pipeline.md) — CI/QA + build/dissemination plan
