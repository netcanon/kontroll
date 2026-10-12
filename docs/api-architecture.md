# API Architecture — plan of record

How kontroll's functionality gets **fully API-ified** the way the netcanon reference is:
extract a **service layer** from `scripts/galaxy.py`, stand a typed **API** over it, and
retire the GUI's subprocess-scrape — with **testing across every layer** and the **Layer 0
logging/audit** wired in. Sibling to [qa-and-release-pipeline.md](qa-and-release-pipeline.md)
(CI/QA) and [logging-architecture.md](logging-architecture.md) (observability); same
plan-of-record style. Status legend: ⬜ todo · 🟡 partial · ✅ done.

---

## 0. Goal & constraints

The bar is **not "every function is an endpoint."** It's: **every user-facing surface — GUI,
CLI, tests — calls the same service layer**, and the operations that *should* be remote-callable
are, behind auth, audited. Honest calibration: even netcanon is **~80%** API-ified (its
server-rendered pages read app state in-process, not via the API), so "fully" is a direction,
not a binary.

The lift is **genuinely smaller than netcanon's** for one reason: **Semaphore already gives us
job execution, scheduling, RBAC, a secret store, and a task API for free.** netcanon hand-rolled
all of that. kontroll's API therefore covers the **capability/onboarding domain**; **execution +
scheduling stay delegated to Semaphore's REST API** (we query/trigger it, never reinvent it).

Constraints that shape every workstream:
- **Modular, no god files.** A new endpoint = a new route file, never a hub edit (same doctrine
  as inventory drop-ins, vectors, backends, the capture-exception matrix).
- **Secret-safe + privileged.** The API can write the repo, encrypt secrets, and trigger Ansible.
  Every mutation is audited (who/when/what, never creds) and run_id-correlated. Fail-closed auth.
- **Semaphore delegation.** ping/backup/update/run-playbook are **not** kontroll endpoints — they
  are Semaphore tasks. The API *delegates* to them. **As-built, no kontroll route fires a Semaphore
  task-run** (the only Semaphore HTTP client is `scripts/configure-semaphore.py`, at setup); the operator
  triggers the task in Semaphore's own UI and the GUI/API emit the deterministic command + `run_id`. An
  API-fired task-run trigger is a planned enhancement — consistent with the C9 "API must not run Ansible" boundary.
- **Testable across all layers** — unit → integration (TestClient) → e2e (Playwright) → live
  (Semaphore smoke gate) → contract (OpenAPI). §6.

---

## 1. Workstream — extract a service layer from `galaxy.py`  ✅ (the keystone)

**Status: ✅ done (2026-06-13).** `scripts/galaxy.py` is now a thin CLI over a `scripts/kontroll/`
service package — core modules (`paths`/`predicate`/`probe`/`catalog`/`record`/`gitio`) + one
`service/<domain>.py` per verb (search/probe/classify/refresh/audit/scaffold/onboard/openapi/
capture_exception). The `cmd_*` wrappers call service functions that RETURN data and render it; CLI
output is **byte-identical** (parity-verified). onboard split into `build_onboard_plan` (pure) +
`apply_onboard_plan` (the mutations). Test seams moved to the home modules; `tests/unit/test_service_layer.py`
drives the services directly. 94 hermetic tests green. The original design rationale follows.

**What:** `galaxy.py`'s `cmd_*` functions compute a result and then `print()` it. Refactor so the
logic **returns structured data** and the `cmd_*` wrappers become thin formatters. The pure core
(`eval_pred`/`classify`/`build_record`/`derive_*`/`classify_endpoints`) already returns data; the
`cmd_*` wrappers and the repo-mutators (`enable_in_fleet`, `sops_set`, `_write_new`) are the target.

**netcanon mirror:** its route handlers are thin, and the shared backup logic (`_run_backup_job`)
is called by both the API route and the APScheduler job. Note, honestly: in netcanon that shared
function still lives in the **routes** package (`api/routes/backups.py`) — there is no separate
service tier; persistence is routes→storage. So this workstream's "extract a service layer" is the
step netcanon itself hasn't fully taken — kontroll matching/exceeding it is fair framing.

**Exemplar (already built):** `galaxy.py capture-exception add` — `add_capture_exception(...)` is a
pure-ish mutator returning a bool, and `cmd_capture_exception_add` is the thin CLI over it with the
commit/push gate. That split (service fn ↔ thin CLI) is exactly the workstream-1 pattern, applied
once; this workstream generalizes it across `cmd_search/probe/classify/onboard/openapi/refresh/audit`.

**Sizing:** Medium — mechanical (move prints out, return dicts), low-risk, and it makes the pytest
suite cleaner (tests call `service_*` directly, no capsys/stdout-parsing). It also removes the GUI's
stdout-scrape dependency (workstream 2).

**Testing:** unit tests call `service_*` with synthetic facts + the `tmp_repo` fixture (the existing
pattern). **Logging:** mutating service fns log what changed + run_id, never creds (§7).

---

## 2. Workstream — a typed API over the service layer  🟡 (scaffold + read-only routes done)

**Status: 🟡 (2026-06-13).** The FastAPI scaffold + read-only routes are ✅: `api/` (`create_app()` +
a lifespan that loads the catalog once; one router module per domain under `api/routes/`, registered in
`api/main.py`) exposes `GET /search`,
`/probe/{collection}`, `/units/{collection}`, `/classify/{collection}`, `/health`, each calling a `service_*` fn directly.
Pydantic models give an auto `/openapi.json` + `/docs`; `tests/integration/test_api.py` (FastAPI
TestClient, service seams mocked) pins the routes + the dogfood (galaxy ingests the API's own spec);
booted under uvicorn over real HTTP. **Since shipped (ws3/ws4 + A2):** the mutation routes, the full
`run_galaxy` retirement (the GUI onboard is now in-process too — `gui/app.py` no longer shells out at all),
and a live deployment (read-only unless `api_privileged`, C10-gated privileged). The original design follows.

**What:** stand up **FastAPI** (auto-OpenAPI, Pydantic validation, dependency injection — the netcanon
pattern) calling the service functions **directly**, replacing `gui/app.py`'s `run_galaxy` subprocess
+ stdout-parse (kontroll's worst current wart). Routes grouped by domain (search / probe / classify /
onboard / openapi / audit), each a drop-in file; a `create_app(settings)` factory + lifespan that
loads vectors/backends/overrides once (mirrors netcanon's `create_app`).

**Why FastAPI:** auto `/openapi.json` + `/docs` (and galaxy already *ingests* OpenAPI — dogfooding),
Pydantic request/response models, DI that makes TestClient integration tests trivial. The GUI then
becomes a real API client (or is kept thin over the same service layer).

**Sizing:** Medium. Routes are thin once the service layer exists; the models + DI scaffold port
closely from netcanon's `main.py`/`api/deps.py`/`api/routes/`.

**Testing:** every route via FastAPI `TestClient` (§6 L2b — the existing Flask test-client tests port
over). **Logging:** a request-boundary middleware mints the run_id + audits every privileged call.

---

## 3. Workstream — the actuation boundary  ✅ (implemented — incl. the onboard --apply route, 2026-06-13)

**What:** decide, per operation, what is **read-only** (no auth, rate-limited), **privileged**
(token auth + audited), or **delegated to Semaphore** (not a kontroll endpoint). The table in §8 is
the contract. The rule: capability *inquiry* is open + cheap; repo *mutation* is privileged + audited;
job *execution* is Semaphore's. Host scripts (`update`/`backup`/`state-*`) stay operator-only —
API-mutating the host is a privilege jump we don't take in v1.

**Sizing:** Small–Medium (a decision matrix + route guards). It's mostly judgment, captured in §8.

---

## 4. Workstream — auth / authz / audit  🟡 (built + verified 2026-06-13)

**Status: 🟡 (2026-06-13).** Built + tested: `api/auth.py` (Bearer token, constant-time, fail-closed —
no `KONTROLL_API_TOKEN` ⇒ privileged routes 503), `api/audit.py` (append-only rotated TSV
`ts,user,ip,action,run_id,detail`, never creds, **fail-closed** — an un-auditable mutation is refused),
a per-request `kontroll_run_id`, and `GET /audit/log`. Applied to the low-blast-radius routes
(`/refresh`, `/capture-exceptions`); SECURITY.md **C9** lands with it. (Written when deployment, rate-limiting
and the onboard `--apply` route were still deferred; all three have since been built — see `api/README.md` for
the routes as they are.) The original design follows.

**What:** the GUI already has **fail-closed HTTP Basic + TLS + a rotating audit log** (SECURITY.md C8);
netcanon's API has **none**. Extend ours to the API: **token (Bearer) auth**, constant-time validated,
fail-closed; **per-operation audit** (TSV `ts⇥user⇥ip⇥action⇥run_id⇥detail`, rotated like the GUI log,
creds never logged); **run_id correlation** minted at the API boundary and threaded into the audit log
+ any Ansible the call triggers (the Layer 0 key, [logging-architecture.md](logging-architecture.md) §3).

**Sizing:** Small — the GUI's fail-closed + audit + rotation patterns are reused; tokens are the new bit.
This is a place kontroll *exceeds* the reference.

**Testing:** auth unit tests (valid/invalid/disabled → 401, constant-time), audit write+query tests.

---

## 5. Workstream — tests + published OpenAPI  ⬜

**What:** port the GUI's Flask test-client tests to FastAPI `TestClient`; publish `/openapi.json` as the
contract; add schema/contract checks. The e2e (Playwright) + live (smoke gate) layers (being built now)
slot in as L3/L4. **Sizing:** Small. Detail in §6.

---

## 6. Testing across all layers

Maps onto the existing pyramid in [qa-and-release-pipeline.md](qa-and-release-pipeline.md) §1, extended
for the API:

| Layer | What | Tool | Status |
|---|---|---|---|
| **L1 lint/validate** | yaml/ansible/compose/secrets | `tests/validate.sh` | ✅ |
| **L2 unit** | pure fns + service layer + Pydantic models + auth | pytest + monkeypatch + `tmp_repo` | 🟡 (galaxy + service layer + auth ✅; full API surface ⬜) |
| **L2b integration** | every route as a black box | FastAPI `TestClient` (ports the Flask tests) | ✅ (read-only routes; mutation routes land with ws3) |
| **L3 e2e** | the GUI in a headless browser over the real app | Playwright (service seams mocked) — a dedicated CI job (cost $0, private-repo free tier) | ✅ |
| **L4 live smoke** | the real fleet responds, post-deploy | the Semaphore **smoke-gate** wrapper (`run-smoke-gate.sh`), queryable via the task API | ✅ |
| **L5 contract** | the published schema is valid + honored | `/openapi.json` + a schema check in CI | ⬜ |

The hermetic default (`pytest -m "not e2e and not slow"`) stays fast; e2e is a **separate** CI job;
live smoke runs against Semaphore, never in GitHub Actions (CI stays hermetic — the live lab is the
instance's job, not CI's). Coverage focus: the service layer + auth are the high-bar surfaces.

---

## 7. Logging & audit (the API integrates Layer 0)

The API is a **privileged surface**, so it is wired into the Layer 0 foundations
([logging-architecture.md](logging-architecture.md) §3):
- **run_id** — minted at the request boundary, written into every audit line **and** passed to any
  Ansible the call triggers (`-e kontroll_run_id=…`), so one operation is greppable end-to-end across
  the API audit log, the durable ansible log, and the Semaphore job output.
- **Audit** — every mutation logs `who/when/what` (never creds), append-only, rotated (the GUI's
  `RotatingFileHandler` pattern). A `GET /audit/log?action=&user=&run_id=&since=` route (privileged)
  makes it queryable — the cross-silo "show me everything for this run" §G2 wanted.
- **Fail-closed** — if the audit write fails, the mutation does not proceed; auth failures 401 and are
  logged (denied attempts only), exactly as the GUI does today.
- A new **SECURITY.md control** (API as a privileged surface) lands with workstream 4.

---

## 8. Actuation boundary (the contract)

| Operation | Category | Auth | Where it lives |
|---|---|---|---|
| search / probe / classify | read-only inquiry | none (rate-limited) | API GET + CLI + service layer |
| openapi ingest | read-only (derive a recipe) | token | API + CLI (returns YAML; `--emit` writes) |
| onboard dry-run | read-only (plan) | token | API + CLI |
| **onboard --apply/--commit** | **privileged mutation** | **token + audit** | API + CLI → repo write + sops + local-canonical push |
| **capture-exception add** | **privileged mutation** | **token + audit** | API + CLI (✅ CLI built) → matrix + local-canonical push |
| refresh (capability cache) | privileged batch | token + audit | API + CLI |
| audit-log query | privileged read | token | API |
| **onboard --bootstrap** | execution | — | **delegated → Semaphore** (operator-triggered; no API-fired task-run as-built) |
| ping / backup / update / run-playbook | execution | — | **delegated → Semaphore** (operator triggers the task; the GUI emits the command — an API-fired task-run trigger is planned, not as-built) |
| host scripts (state-snapshot/restore, deploy) | host mutation | — | **operator-only CLI** (not exposed in v1) |

---

## 9. Sequenced rollout

1. ✅ **Service-layer refactor** (workstream 1) — the keystone; DONE (2026-06-13). `galaxy.py` is now a
   thin CLI over the `scripts/kontroll/` package (one `service/<domain>.py` per verb); the
   `capture-exception add` CLI was the pattern, now generalized across every verb. Byte-identical output.
2. ✅ **FastAPI scaffold + read-only routes** (search/probe/classify + /health) — DONE (2026-06-13).
   `api/` over the service layer; typed `/openapi.json`; TestClient + uvicorn-verified; dogfood pinned.
3. ✅ **Privileged routes** — DONE (2026-06-13): refresh, capture-exception, audit query, AND the
   highest-blast-radius **onboard `--apply`** (dry-run + apply: repo write + creds→SOPS + commit +
   canonical push) — all behind Bearer auth + a fail-closed run_id-correlated audit (SECURITY.md C9).
   Creds are never returned or logged; bootstrap is delegated to Semaphore.
4. ✅ **GUI → service layer (in-process)** — DONE: the GUI's `/api/search` + `/api/classify` (2026-06-13) AND
   `/api/onboard` (2026-06-15, A2) call the service layer **in-process** — `run_galaxy` / `subprocess` fully
   retired from `gui/app.py`; onboard now mirrors `api/routes/onboard.py` (build + apply +
   `commit_and_push(run_id)`, staging `proposed/<run_id>` under C10). (Kept thin over the service layer rather
   than an HTTP client — both are allowed by §2.)
5. ⬜ **Tests + OpenAPI** — TestClient suite, e2e (landing now), live smoke (landing now), `/openapi.json`.
6. 🟡 **Hardening** — **rate-limiting ✅** + **frontend-exposure mode 🟡** (2026-06-13): rate-limiting is a
   single in-process middleware (`api/ratelimit.py` — per-IP inquiry / per-token-digest privileged, a 429
   *before* auth, fail-open only on the limiter's own malfunction, breaches best-effort audited; tunable via
   `KONTROLL_API_RATELIMIT`). Frontend exposure is now a per-instance mode (`instance/instance.yml`
   `frontend.tls_mode`: `byo_proxy` live for the API, `acme`/Caddy planned) — this **replaces the bespoke-CA
   plan** (`docs/reviews/2026-06-13-roadmap/03-redesign-frontend-exposure.md`). Still ⬜: the Caddy ingress
   (modes B/C) + optional SSO (Authelia) + structured audit for the Loki layer.

Gate: the API is **never WAN-exposed**; it lives on the mgmt VLAN behind the same boundary as the GUI +
Semaphore (SECURITY.md C3). Dissemination/exposure follows the same beta gate as the GUI.

**The read-only deploy (first shipped 2026-06-13; what a deploy without `api_privileged` still is):**
`docker/services/api.yaml` serves at `${KONTROLL_MGMT_IP}:8444` with a root-owned content clone, the canonical
`:ro` + no token (privileged routes 503). The frontend TLS posture is **per-instance** (`instance/instance.yml`
`frontend.tls_mode` — `byo_proxy` / `self_signed` / `acme`, resolver `scripts/kontroll/frontend.py`; this
instance fronts with Nginx Proxy Manager → the API serves HTTP). The token + writable-clone enablement for the
privileged mutations is the gated `api_privileged` deploy
([privileged-mutation-enablement.md](privileged-mutation-enablement.md)).

## 10. Shallow search (fast discovery; deep classification on demand)  ✅ (2026-06-13)

**Status: ✅ implemented (2026-06-13).** `/search` returns **shallow** local records FAST and defers the deep
capability classification to an explicit opt-in.

**Why:** search is integral to onboarding, but the old `/search` deep-probed every local match
(`probe.deep_probe` → `ansible-doc`, ~2.6s/collection, CPU-bound). A broad keyword (`cisco` → ~10 installed
collections) was ~10–20s on the 2-core control VM even after the 2026-06-13 concurrency fix.

**As built:** local matches are probed **shallow** — modules + plugins read from the installed collection's
files (`plugins/` walk + `MANIFEST.json` metadata, `probe.shallow_from_local`), **no `ansible-doc`** —
surfaced by `catalog.local_shallow` (the fast local mirror of `galaxy_search`: keyword-matched + capped by
`limit`). `service_search(deep=False)` is the default; `?deep=true` (CLI `--deep`, GUI "deep" toggle) — or
any `vector` filter, which needs exact cells — re-probes only the matched candidates deeply (concurrent),
resolving the deferred `maybe` cells. `GET /probe/{c}` and `GET /classify/{c}` stay the per-collection deep
forms. The Record `depth` field labels each record `shallow`/`deep`, so **no model change** was needed.
`GET /classify/{c}` additionally surfaces `telemetry_declared` — the device-class module(s) already declaring
a `metrics:` block for the collection (the detected/declared telemetry reconciliation; read-only, additive to
`ClassifyResponse`).

**The accuracy guarantee — shallow ⊑ deep.** A shallow capability cell never *contradicts* the full deep
probe: it is identical, or a `maybe` where deep is definite — **never a flipped yes↔no**. Two axes:
- **Depth axis (proved hermetically):** the only signal that needs the deep per-module doc parse is
  *backup via a `*_config` module's `backup:` option*; with `module_options` empty a `module_option` rule
  reads as unknown-at-depth → `maybe`. Every other rule (cliconf/httpapi/netconf plugins, `*_config`/
  `_facts`/`_resource` suffixes) is file-visible, so shallow == deep on it. `tests/unit/test_shallow_search.py`
  proves the ⊑ relation cell-by-cell over the predicate signal space.
- **Source axis (verified live):** walking `plugins/` reproduces `ansible-doc -l`'s module/plugin set —
  measured **zero divergence** across the network fleet (arista.eos / cisco.ios / fortinet.fortios /
  ansible.netcommon). `galaxy.py verify-parity <kw>` (over `service_search_parity`) re-checks this against the
  real fleet on demand and **exits non-zero on any unsound cell**, so the guarantee is continuously testable.

**Result:** sub-second local discovery; the deep detail stays one `--deep`/click/call away, and the fast path
is provably honest about what it cannot yet see. Cross-ref the 2026-06-13 `/search` fix (`7f0ce5e` concurrent
probe, `bd8ff68` tag-normalize + cap), now the *deep* opt-in's fan-out path.

## 11. Backlog — onboard composed SERVICES via their OpenAPI specs (the "services" track)

**Why (operator-prompted 2026-06-14):** kontroll already **ingests OpenAPI specs to derive `api` backend
recipes** for device onboarding (`build_openapi_recipe`, and it dogfoods its *own* `/openapi.json`). The
same engine can point *outward* at the **services kontroll composes** — Grafana, Prometheus, Semaphore, NPM
— because **a service is just a device whose API you integrate.** Each of those interactions (provision a
dashboard, query/trigger a Semaphore task, a Homepage widget's read) is "kontroll talking to a service's
API," and most of those services publish a spec the ingester could consume + track as it drifts.

**What's real vs. not (verified 2026-06-14):** the Grafana **product** API publishes a spec
(`/public/api-merged.json`), as does the Grafana.com **management** API (`grafana.com/api/openapi.json`, 34
paths). But the **public dashboards registry** endpoints `fetch-dashboards.py` rides (`/api/dashboards/{id}`
+ `/revisions/{rev}/download`) are **not in any published spec** — a de-facto, undocumented API. So you
*cannot* spec-track the dashboard fetch; its right-altitude guard is a small contract/smoke test (assert the
shape), and the dashboards are vendored anyway (gnet drift has zero runtime impact). The OpenAPI-onboarding
idea applies to the **spec-publishing service surfaces**, not the registry fetch.

**Sketch:** a `services` onboarding lane beside the device-class one — a service declares its spec URL
(`config/services/<name>.yml`), the ingester derives the integration recipe, and a check re-validates it
against the live spec (drift caught loudly). Reuses `build_openapi_recipe`; the dogfood (ingest our own
spec) already proves the loop closes. **Sizing:** Medium. Independent; schedule when a service integration
goes API-deep (e.g. dashboard provisioning via the Grafana API rather than file provisioning).

## See also
- [qa-and-release-pipeline.md](qa-and-release-pipeline.md) — the CI/QA pyramid this testing plugs into
- [logging-architecture.md](logging-architecture.md) — Layer 0 (run_id, audit) the API integrates
- [capability-matrix.md](capability-matrix.md) — the domain the service layer + API expose
- [../SECURITY.md](../SECURITY.md) — C8 (privileged surface, audit), C3 (reachability)
- [../gui/README.md](../gui/README.md) — the current GUI security posture the API extends
- [../scripts/galaxy.py](../scripts/galaxy.py) — the service-layer candidate (and the built CLI exemplar)
- netcanon reference (read-only): the netcanon repository — `netconfig/main.py`, `api/routes/`, `api/deps.py` (the FastAPI app-factory + DI + routes→storage pattern; shared `_run_backup_job` across API + scheduler)
- [observability-onboarding-flow.md](observability-onboarding-flow.md) — Option-A design: the §11 service-via-OpenAPI root built out + the GUI-actuatable propose-then-promote `/observe` flow, fully specified **(design only, not built)**
