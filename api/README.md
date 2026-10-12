# `api/` — the kontroll onboarding API (typed; inquiry is open, every repository write is token-gated and staged)

A **thin typed FastAPI** over the [`scripts/kontroll/`](../scripts/README.md) service package. It
carries no logic of its own: every route calls a `service_*` function that returns structured data,
and FastAPI validates + serializes it against the Pydantic models in [models.py](models.py). This is
the API tail of the capability-matrix design and **workstream 2** of
[docs/api-architecture.md](../docs/api-architecture.md) — standing a typed surface over the service
layer the [galaxy.py refactor](../scripts/galaxy.py) extracted.

## Status & scope

**Read-only inquiry** (open, cheap) **+ the privileged routes** (Bearer-token; every write audited), per §8 of
the architecture. The table is held against the app: a test fails for a mounted route that is not in it, and
for a row whose route is gone.

| Method & path | Auth | Returns |
|---|---|---|
| `GET /search?q=…&origin=…&limit=…&vector=…&deep=` | — | capability records for the keyword(s); local hits are SHALLOW by default, `deep=true` (or a `vector`) re-probes them deeply |
| `GET /probe/{collection}` | — | a deep-probe record + raw module/plugin facts (404 if not installed; 422 if the segment is not `namespace.name`) |
| `GET /units/{collection}` | — | the collection's runnable units (collection-shipped playbooks + roles; empty if it ships only modules; 404 if not installed) |
| `POST /units/{key}/configure` | — | **write-free** (validates, stages nothing): re-check an actuation unit's submitted configure `values` against its curated knobs → `{key, ok, errors}` (404 if no such unit) |
| `POST /units/preview` | — | **write-free** (renders, stages nothing): the Review-stage preview of what unit `key` would run with `values` → the would-run `play` + resolved `vars` + `check_first` + `enact` (404 no unit; 422 invalid values) |
| `GET /classify/{collection}` | — | the fitting execution backend(s) (404 if not installed; 422 if the segment is not `namespace.name`) |
| `GET /health` | — | liveness `{status, service}` |
| `POST /refresh` | token | rebuild the (gitignored) capability-matrix cache, written under the write root (the propose clone on a baked install) → `{count, run_id}` |
| `POST /capture-exceptions` | token | add a capture-exception + commit + push the canonical → what changed |
| `POST /onboard` | token | dry-run plan, or apply (module + drop-in host + fleet-enable + creds→SOPS + commit + push) |
| `GET /onboard/cred-fields?backend=…&secrets=…&collection=…&deep=` | token | the credential fields a backend (or a curated class) asks for — names, kinds and labels, never values |
| `GET /capability/{cap}/suggest?key=…` | token | **read-only**: whether capability `cap` (telemetry, logging, backup) fits onboarded class `key`, and with which methods |
| `POST /capability/{cap}` | token | propose (a dry-run plan + an anti-drift token) or promote (apply — audited) a capability for a class; the write is staged, never pushed to `main`, under the gated deploy |
| `GET /secrets/{domain}/fields` | token | **read-only**: a secret domain's form fields — their names and which are already set, never a value |
| `POST /secrets/{domain}` | token | propose (dry-run) or apply (encrypt + stage — audited) a domain's secrets; values are never returned or logged |
| `POST /actuation/create` | token | **author an app-store unit** from a searched collection: propose (the derived key + the unit.yml path + a token) or create (apply — audited) — renders + validates the `unit.yml` and stages it to `proposed/<run_id>` (never main). 422 bad inputs/no class; 409 collision/not-enabled; **403 the Tier-cap** (unsigned install onto edge_firewall/core_switch) |
| `POST /actuation/{key}` | token | **the first app-store write verb**: propose (plan paths + an anti-drift token) or stage (apply — audited) a configured unit's non-secret values to `proposed/<run_id>` (never main; the operator promotes) |
| `GET /audit/log?action=&user=&run_id=&since=` | token | query the audit log |
| `GET /openapi.json`, `/docs`, `/redoc` | — | the typed schema + Swagger/ReDoc UIs |

**Read-only without `api_privileged`** ([api-architecture.md](../docs/api-architecture.md) §9):
`docker/services/api.yaml` serves at `${KONTROLL_MGMT_IP}:8444` with a root-owned content clone, the canonical
mounted **read-only** and **no token** — the inquiry routes serve and the privileged routes are fail-closed
(503). Per [SECURITY.md](../SECURITY.md) C3/C9 it is
**mgmt-VLAN-only, never WAN**. Enabling the privileged mutations is the gated `api_privileged` deploy (token +
a writable content clone + a `local` remote; no age key) — a privileged write then stages `proposed/<run_id>`,
never `main`: [docs/privileged-mutation-enablement.md](../docs/privileged-mutation-enablement.md).
(`onboard --apply` is dry-run by default, creds never returned/logged; bootstrap stays a Semaphore task.)

**Frontend exposure** is per-instance (`instance/instance.yml` `frontend.tls_mode`, resolved by
[`scripts/kontroll/frontend.py`](../scripts/kontroll/frontend.py)): `byo_proxy` serves **HTTP** behind your
reverse proxy, which owns TLS (this instance: Nginx Proxy Manager); `self_signed` serves HTTPS from a
host-provisioned cert (the zero-config default); `acme` fronts with a bundled Caddy + Let's Encrypt (DNS-01).
An unknown/empty mode fails safe to `self_signed`. See
[frontend-exposure.md](../docs/frontend-exposure.md).

Deploy: `ansible-playbook playbooks/deploy-stack.yml -e '{"stack_services":["api"]}'` (from `ansible/`).

## Auth (privileged routes)

The privileged routes are **fail-closed**: they require a Bearer token matching `KONTROLL_API_TOKEN` (SOPS-backed at
deploy, like the GUI's `GUI_PASSWORD`); **unset ⇒ those routes are disabled (503)**, never open. The token is
compared constant-time. Every privileged call is **audited** — an append-only, size-rotated TSV line (`ts, user, ip,
action, run_id, detail`, never a credential) at `KONTROLL_API_AUDIT_LOG` — and the audit write is **fail-closed**: a
mutation that can't be audited is refused (503). Each request carries a `kontroll_run_id` (in the response + the
audit line). C9 of [SECURITY.md](../SECURITY.md). A request field that is not a closed charset is answered **422**
with a `code` derived from the field (`invalid_collection`, `invalid_device_class_key`, …) and a message that never
quotes the value; a privileged refusal leaves a best-effort `request-refuse` line (nothing was mutated), an open
route writes none (C22). On `POST /onboard` the line is `onboard-refuse`, with `reason=request-boundary
field=<name>` for a refused field and `reason=planner-guard` (body `code: invalid_request`) for a planner refusal
that is not about one field, such as a partially filled credential set. When what stops a request is the
repository's own data or layout — a class's module file holds a group, domain, backend or role that is not a name; a
credential field is declared with a name that is not one; a backend names no role and none can be derived from its
name (or its registry entry holds a role that is not a name); a write target does not resolve inside the repository
— the answer is **409** `repository_data` with a sentence that says where to look, and the audit action is
`repository-refuse`: nothing in the request is wrong, and the remedy is the file or the layout. `POST /onboard`'s
other 409 — a drop-in that exists with different content, answered without a `code` and retried with `overwrite` —
is a different answer: key on `code`, not on the status.

```bash
export KONTROLL_API_TOKEN=…          # at deploy, sourced from SOPS like GUI_PASSWORD
curl -s -XPOST localhost:8000/refresh -H "Authorization: Bearer $KONTROLL_API_TOKEN"
```

## Rate limiting

A single in-process middleware ([ratelimit.py](ratelimit.py), attached once in `create_app()` — no
per-route edits) caps request rate by class: **per-IP** for the open inquiry routes, **per-token-digest**
(never the raw token) for the privileged routes; `/health` + the OpenAPI/docs endpoints are exempt. A
breach is **HTTP 429 + `Retry-After`, enforced before auth runs** (so a flood can't force probe I/O or
spam the audit), with a best-effort, throttled `rate-limited` audit line. The limiter **fails open only on
its own internal malfunction** — auth + audit stay fully fail-closed. Limits are code defaults overridable
at deploy via the **`KONTROLL_API_RATELIMIT`** env knob (not a secret; e.g.
`inquiry=30/min;privileged=10/min`, or `off`). C9 of [SECURITY.md](../SECURITY.md).

## Run (local dev)

```bash
pip install -r api/requirements.txt
uvicorn api.main:app --reload          # http://127.0.0.1:8000  (/docs for the Swagger UI)
```

`api/__init__.py` puts `scripts/` on `sys.path` so `from kontroll …` resolves (the same resolution
the GUI + tests use). The app imports the service layer **directly** — no subprocess. The GUI now does
the same (ws4 + A2 retired `run_galaxy` entirely; search/classify/onboard all call the service in-process).

The per-request I/O runs on a host with the ansible toolchain — i.e. the control node. The DEFAULT
`/search` is now **shallow** (local hits read from the collection's files: `ansible-galaxy` list + a
`plugins/` walk — **no `ansible-doc`**), so it stays fast; `?deep=true`, `/probe`, and `/classify`
deep-probe via `ansible-doc`. On a box without the toolchain, `/search` returns only Galaxy results and
`/probe`/`/classify` 404 (nothing installed to probe).

## Dogfooding

The emitted `/openapi.json` is a real OpenAPI 3 document, so kontroll can read its **own** API the
same way it reads any device's:

```bash
python3 scripts/galaxy.py openapi http://127.0.0.1:8000/openapi.json
```

`tests/integration/test_api.py` asserts this round-trips (the ingester derives a recipe from the
API's own schema) — the design's dogfooding, pinned.

## Testing

The L2b contract: `tests/integration/test_api.py` drives the app with FastAPI's **TestClient** over
the real ASGI stack, with the service I/O seams patched in their home modules
(`kontroll.probe` / `kontroll.catalog`) so it runs **offline** — no ansible, no network, no lab.
Run it in the hermetic default (`pytest -m "not e2e and not slow"`); CI covers `api/` in the matrix.

## See also

- [../docs/api-architecture.md](../docs/api-architecture.md) — the plan of record (ws2 = this surface)
- [../scripts/README.md](../scripts/README.md) — `galaxy.py` (the CLI) + the `kontroll/` service package
- [../docs/capability-matrix.md](../docs/capability-matrix.md) — the domain these routes expose
- [../gui/README.md](../gui/README.md) — the current Flask GUI (subprocess-scrape; ws4 makes it an API client)
- [../SECURITY.md](../SECURITY.md) — C3 (reachability), C8 (the privileged-surface posture auth will extend)
