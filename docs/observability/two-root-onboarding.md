# Two-root onboarding (device=Galaxy, service=OpenAPI)

> **DESIGN HISTORY — not a description of the tree.** This is the design record of two-root onboarding (a device root and a service root), written in June 2026. It was not built. Where it disagrees with the code or with `SECURITY.md`, they are right.

> **Part of the [Option-A observability + onboarding flow](../observability-onboarding-flow.md) design (baseline HEAD `63de129`).**
>
> ⚠️ **Binding contracts (the catalog loader name, the flat `telemetry/<method>.yml` schema, the single clean-cutover dispatcher, the `network` job-vs-inventory-group distinction, the `.env` secret-render mechanism) are pinned in the master design's §4.0 SHARED CONTRACTS.** Where this section names a symbol, schema, or sequencing differently, **§4.0 wins** and this section is reconciled to it before implementation. See the master design's §8 “Resolved conflicts” for the per-section reconciliation list.

This design section is for the **Two-root onboarding** dimension. It converges the Galaxy device root and the OpenAPI service root on a shared observability opt-in, walks radarr end-to-end, and stays strictly inside the trust boundary the self-review flagged (propose-only on the network surfaces; enact via deploy-stack/Semaphore). It is phased so the keystone parts land first and the GUI/privileged-actuation parts are explicitly deferred behind the un-built propose-then-promote posture.

---

## 1. Framing decisions (made once, binding for the rest)

These resolve the self-review blockers up front so the implementer never re-decides them.

**D1 — Two parallel seams, never a fork in a hub.** The device root (`build_onboard_plan`, Galaxy-rooted) and the service root (a new `build_service_plan`, OpenAPI-rooted) are **sibling service functions** that each return the *same plan shape* and converge only at `apply_onboard_plan` (the shared mutator) and at a shared, optional `observability` sub-plan. There is **no** `if root == "service"` branch inside `build_onboard_plan` — the two builders are separate files. This honors the "one dispatch seam" doctrine: the seam is *which builder the caller picks*, expressed as a CLI subcommand / API route / GUI tab, not a conditional inside a god function. (Grounded: `build_onboard_plan` at `scripts/kontroll/service/onboard.py:24` already returns a flat plan dict; `build_openapi_recipe` at `scripts/kontroll/service/openapi.py:103` already returns `{name,recipe,text,summary}` with **no writes** — the CLI owns `--emit`.)

**D2 — `module.yml` gains a `kind: device | service` field, defaulting to `device`.** Confirmed by grep: **no `kind:` field exists anywhere today**, and `gen-observability.py`/`fetch-dashboards.py`/`bootstrap.yml` all read `module.yml` keys that are absent-tolerant (`module.get('metrics') or []`). Adding `kind:` with a default is back-compatible: every existing module is `device` implicitly. The field is **documentary + a small number of generator branches** (which inventory shape, which secret-render path), not a new dispatch engine. A service module differs from a device module in exactly four ways, enumerated in §3.

**D3 — The service root requires a collection-less plan builder.** The self-review is correct that `build_onboard_plan` hard-requires `probe.deep_probe(collection)` (`onboard.py:32`) and returns `not_installed` when `not f["modules"] and not f["plugins"]` (`onboard.py:33`). A pure-API service (radarr) has no collection and cannot enter that path. Therefore the service root is a **new** `build_service_plan` that skips `deep_probe` entirely and starts from a recipe (an already-emitted `ansible/backends/api/recipes/<name>.yml`) or a spec URL. It reuses every *other* primitive (`enable_in_fleet`, `gitio._write_new`, `gitio.sops_set`, the `_sops` inline-lookup pattern). This is **new architecture**, scoped to its own phase.

**D4 — Observability opt-in is PROPOSE-ONLY on the network surfaces.** Both roots may *propose* an `observability` sub-plan (write a `metrics:`/`dashboards:`/`widget:` block into the module; regenerate the target files in the working tree). They **never** install an agent, provision an exporter container, render a secret into `.env`, or reload Prometheus/Grafana from the API/GUI. Those ENACT steps stay operator-CLI (`deploy-stack`) and Semaphore — preserving the actuation boundary that `api/routes/onboard.py:5-6` and api-architecture §8 enshrine ("the API does not run Ansible"). This is what lets the whole thing ship without the deferred privileged-mutation enablement.

**D5 — What is new vs. already-backlogged in api-architecture §11.**

| Concern | Status in the repo today | This design |
|---|---|---|
| Ingest an OpenAPI spec → an `api` recipe (`build_openapi_recipe`) | **Built** (`openapi.py`, CLI `galaxy.py openapi … --emit`, dogfood-tested) | **Reused verbatim**, not rebuilt |
| A `services` onboarding lane (`config/services/<name>.yml`, ingester derives recipe, drift check) | **Backlog only** — §11 "Sketch"; no `config/services/` dir exists (confirmed) | **Designed in full here** (§3–§5) |
| `module.yml` `kind:` field | **Does not exist** (confirmed) | **New** (§3) |
| Service inventory shape (localhost-delegated `uri` host) | **Does not exist** for collection-less services | **New** (§3.3) |
| exportarr as a `via:proxy` telemetry method | **Does not exist**; radarr is only a Homepage tile (`dashboards/homepage/services.yaml:42-47`) | **New**, but **depends on the telemetry-method-registry phase** owned by the sibling telemetry dimension (§6 names the dependency) |
| Shared observability opt-in across both roots | **Does not exist** (`build_onboard_plan` has no `metrics:` key — `onboard.py:46-54`) | **New** (§4) |
| Homepage `widget:` generation from `module.yml` | **Documented, unwired** — `modules/README.md:34` promises it; no generator reads `widget:` (confirmed) | **New** `gen-homepage.py` (§4.4) |

So: the *spec-ingestion engine* is done; **everything that turns it into a managed-service onboarding lane is new and designed here.**

---

## 2. The two roots, side by side

```
DEVICE ROOT (Galaxy)                          SERVICE ROOT (OpenAPI)
─────────────────────                          ──────────────────────
catalog.galaxy_search / local_shallow          openapi.fetch_spec(spec_url)
        │ keyword → shallow records                    │ parsed spec
        ▼                                               ▼
probe.deep_probe(collection)                    openapi.build_openapi_recipe(spec)
        │ facts                                         │ {name, recipe, text, summary}
        ▼                                               ▼
predicate.classify(facts, backends)             (backend is ALWAYS `api`)
        │ best backend                                 │
        ▼                                               ▼
service.onboard.build_onboard_plan(...)         service.service_onboard.build_service_plan(...)
        │  PURE → plan dict                             │  PURE → plan dict (SAME shape)
        └──────────────┬────────────────────────────────┘
                       ▼
        (optional) attach_observability(plan, telemetry_method, dashboard, widget)
                       │  PURE → plan["observability"]
                       ▼
        ┌──────────────────────────────────────────────┐
        │  service.onboard.apply_onboard_plan(plan)     │  ← THE SHARED MUTATOR
        │  _write_new(module) ; _write_new(inventory)   │
        │  enable_in_fleet(key) ; sops_set(creds)       │
        │  + if plan["observability"]:                  │
        │      _write_new(metrics block already in mod) │
        │      run gen-observability.py (regen targets) │
        │      run gen-homepage.py  (append widget)     │
        │      stage dashboard pick (fetch deferred)    │
        └──────────────────────────────────────────────┘
                       │ {changed, paths}
                       ▼
        commit_and_push  (CLI gate / API privileged route)
```

The convergence point is `apply_onboard_plan`. Both roots produce a plan with the **same top-level keys** (`module`, `module_text`, host/inventory block, `creds_to_set`, `mod_path`, `inv_path`, and the new optional `observability`). `apply_onboard_plan` does not care which root produced it.

---

## 3. Data model — service module vs. device module

### 3.1 `module.yml` schema additions

Add **one required-with-default field** (`kind`) and make the service-shape fields legal. New/changed rows for the `modules/README.md` schema table (§7 doc-sync lists this):

| Field | Meaning | Device | Service |
|---|---|---|---|
| `kind` *(new, default `device`)* | `device` (Galaxy-rooted, an inventory host with a real connection) or `service` (OpenAPI-rooted, a localhost-delegated `uri` integration) | `device` | `service` |
| `collections` | Ansible collections | a real collection or `[]` (raw SSH) | **always `[]`** — a service has no collection |
| `role` | implementing role | per device class | **always `backend_api`** |
| `backend` | execution backend | auto-classified | **always `api`** |
| `recipe` *(new, service-only)* | the `ansible/backends/api/recipes/<name>.yml` this service integrates through | absent | the recipe name (e.g. `radarr`) |
| `service_url` *(new, service-only)* | the live service base URL (for the Homepage tile `href` + the exporter target) | absent | e.g. `http://192.0.2.20:7878` |
| `inventory_group` | functional group it feeds | a device group | **`services`** (a new group, §3.3) |
| `metrics` | observability list — **now method-typed** (telemetry dimension) | `[{method: host_node}]` etc. | `[{method: exportarr, params: {...}}]` |
| `widget` *(now wired)* | Homepage tile `{type, url, secret}` | optional | the service's portal tile |
| `dashboards` | curated Grafana boards | as today | as today |

### 3.2 Worked example — `modules/radarr/module.yml` (the new service module)

```yaml
# Service module: Radarr (Servarr) — onboarded from its OpenAPI spec, NOT a Galaxy
# collection. kontroll integrates it through the `api` backend + a derived recipe;
# its metrics come from exportarr (a via:proxy telemetry method, no agent on radarr).
# This is the worked example of the SERVICE root (the second of the two onboarding roots).
key: radarr
kind: service                      # NEW: device | service (default device). Selects the
                                   #      service inventory shape + the widget/href wiring.
description: Radarr (movie PVR) — REST service integrated via OpenAPI (VERIFY before active)
                                   # (this record once had a `status: staged` line here; the field was
                                   #  removed 2026-10-19 — a module has no lifecycle state)
collections: []                    # a service has no collection — recipe-driven
role: backend_api                  # generic uri-driven role (ansible/roles/backend_api)
backend: api                       # the api backend (ansible/backends/api/backend.yml)
recipe: radarr                     # NEW (service-only): ansible/backends/api/recipes/radarr.yml
service_url: http://192.0.2.20:7878   # NEW (service-only): live base URL (widget href + exporter target)
inventory_group: services          # NEW functional group for localhost-delegated services
secrets_domain: services           # NEW SOPS domain for service API keys (§7 SECURITY)
# Observability (method-typed — see the telemetry-method registry dimension). exportarr is a
# via:proxy exporter: a container on the control node queries radarr's API and exposes /metrics.
metrics:
  - method: exportarr             # telemetry/exportarr.yml descriptor (proxy-exporter kind)
    params:
      app: radarr                 # exportarr --radarr
      url_var: radarr_service_url # the target URL var (rendered from service_url)
      api_key_var: radarr_api_key # the SOPS-backed API key var name (NEVER a value)
# Homepage portal tile — generated into dashboards/homepage/services.yaml by gen-homepage.py.
widget:
  type: radarr
  url: http://192.0.2.20:7878
  secret: HOMEPAGE_VAR_RADARR_KEY  # the env var name (rendered from SOPS by deploy-stack)
# Curated Grafana dashboard for an exportarr/Servarr target.
dashboards:
  - {gnet: 12896, name: radarr}    # "Radarr (exportarr)" — verify the gnet id at fetch time
```

### 3.3 Service inventory shape

A device onboard writes `instance/inventory/onboarded-<key>.yml` with a real `ansible_host` + connection vars (`onboard.py:78`). A **service** instead writes a localhost-delegated host carrying the recipe reference and the API-key var — mirroring the `api` backend's `ansible_connection: local` (`backend.yml:22-23`). New file `instance/inventory/onboarded-radarr.yml`:

```yaml
# Drop-in service host, onboarded by scripts/galaxy.py service-onboard. Additive — safe to
# delete to remove the service. Reached by localhost-delegated `uri` (recipe-driven), so the
# host connection is local; the recipe + the SOPS-backed API key carry everything else.
services:
  hosts:
    radarr:
      ansible_host: 127.0.0.1            # localhost-delegated; the recipe's port/url do the work
      ansible_connection: local
      device_role: backend_api
      backend_api_recipe: radarr        # ansible/backends/api/recipes/radarr.yml
      radarr_service_url: http://192.0.2.20:7878
      radarr_api_key: "{{ (lookup('community.sops.sops', inventory_dir ~ '/../secrets/services.sops.yml') | from_yaml).radarr_api_key }}"
```

Note the **same self-contained inline-SOPS-lookup pattern** as `onboard.py:66` — the host carries its own `lookup('community.sops.sops', …) | from_yaml` so no shared group_vars is needed. The `services` group is added to `instance/inventory/hosts.yml` as an empty leaf group (`all.children.services.hosts: {}`) in the same commit that introduces the first service module — the drop-in host above merges into it.

### 3.4 Why `kind:` and not "infer from `collections: []`"

`openwrt` is a **device** with `collections: []` (raw SSH — confirmed `modules/openwrt/module.yml:5`). So emptiness is ambiguous. `kind:` is the explicit, one-word disambiguator. Generators that need the distinction (`gen-homepage.py` for the `href`, `bootstrap.yml`'s secrets-domain report) read `module.get("kind", "device")`. Everything that *doesn't* care (the telemetry generator, the dashboard fetcher) ignores it — they already iterate `metrics:`/`dashboards:` which are kind-agnostic.

---

## 4. The shared observability opt-in

A single PURE function, callable from both roots, that **attaches** an observability sub-plan to an already-built plan. It mutates only the plan dict (writes nothing).

> **Note (standalone-dialog revision).** `attach_observability` is invoked by the **observe path** — the standalone observability dialog ([gui-actuatable-flow.md §5](gui-actuatable-flow.md)) and `galaxy.py observe` — **not** inside `build_onboard_plan`; onboarding writes no observability ([gui-actuatable-flow.md §0.1 INVARIANT D](gui-actuatable-flow.md); the opt-out `test_attach_observability_optout_is_noop` is the sibling data-layer proof). Because a service has no search card (`build_onboard_plan` hard-requires `deep_probe`, `onboard.py:32-34`), the opt-in **cannot** nest in an onboard card for the service root — the standalone dialog, opened by an already-onboarded key, is the **only viable home for service observability** (it 404s `no_module` until `build_service_plan` has written the service module, exactly mirroring the device decoupling).

### 4.1 New file: `scripts/kontroll/service/observe.py`

```python
"""observe domain — the shared observability opt-in for BOTH onboarding roots.

attach_observability is PURE: given an already-built onboard/service plan and an operator's
choices (a telemetry method by NAME, an optional dashboard gnet, an optional widget), it
injects a metrics:/dashboards:/widget: block into the plan's module dict and records the
files apply_onboard_plan must regenerate. It writes NOTHING and actuates NOTHING — the ENACT
steps (install agent / provision exporter / render secret / reload prometheus) stay
operator-CLI (deploy-stack) and Semaphore. This is the propose half of propose-then-promote.
"""
from kontroll import catalog


def attach_observability(plan, telemetry_method=None, dashboard=None, widget=None,
                         method_params=None, methods=None):
    """Attach an observability sub-plan to `plan` (from EITHER root). Returns the plan
    with plan["observability"] populated and the module dict carrying the generated
    metrics:/dashboards:/widget: blocks. Pure — no writes. error: "unknown_method" if the
    named telemetry method has no telemetry/<name>.yml descriptor (fail closed, never a
    silent no-op)."""
    if not telemetry_method and not dashboard and not widget:
        plan["observability"] = None          # opt-out: nothing to do
        return plan
    methods = methods if methods is not None else catalog.load_telemetry()
    obs = {"metrics": [], "dashboards": [], "widget": None,
           "regenerate": [], "enact": []}     # `enact` = the operator-CLI steps, listed not run
    if telemetry_method:
        mdef = next((m for m in methods if m["name"] == telemetry_method), None)
        if mdef is None:
            plan["error"] = "unknown_method"
            plan["unknown_method"] = telemetry_method
            return plan
        entry = {"method": telemetry_method}
        if method_params:
            entry["params"] = method_params
        obs["metrics"].append(entry)
        plan["module"]["metrics"] = obs["metrics"]
        obs["regenerate"].append("scripts/gen-observability.py")
        # The ENACT step depends on the method kind — recorded for the operator, never run here.
        if mdef["kind"] == "host-agent":
            obs["enact"].append("ansible-playbook playbooks/install-node-exporter.yml -e target=%s"
                                % plan["host_name"])
        elif mdef["kind"] == "proxy-exporter":
            obs["enact"].append("deploy-stack (provisions the %s exporter + renders its secret)"
                                % mdef["name"])
    if dashboard:                              # {gnet, name}
        obs["dashboards"].append(dashboard)
        plan["module"]["dashboards"] = obs["dashboards"]
        obs["enact"].append("scripts/fetch-dashboards.py (fetches gnet %s)" % dashboard["gnet"])
    if widget:                                 # {type, url, secret}
        obs["widget"] = widget
        plan["module"]["widget"] = widget
        obs["regenerate"].append("scripts/gen-homepage.py")
    # The module text must be re-rendered now that metrics:/dashboards:/widget: were added.
    import yaml
    plan["module_text"] = yaml.safe_dump(plan["module"], sort_keys=False,
                                         allow_unicode=True, width=4096)
    plan["observability"] = obs
    return plan
```

`catalog.load_telemetry()` is owned by the **telemetry-method-registry dimension** (`telemetry/<method>.yml`, mirroring `load_backends` at `catalog.py:50`). This dimension *consumes* it and names the dependency (§6) — it does not define it.

### 4.2 `apply_onboard_plan` grows an observability tail (propose-only writes)

Modify `scripts/kontroll/service/onboard.py:89-107`. The additions write the regenerated **target files** and the **widget append** into the working tree (data-only, already-in-the-canonical-anyway artifacts) and **list** the enact steps in the return value — they do not run the enact steps.

```python
def apply_onboard_plan(plan):
    """... (existing docstring) ... If plan["observability"] is set, also regenerate the
    Prometheus targets and the Homepage tile from the just-written module block (data-only,
    via gen-observability.py / gen-homepage.py). Agent install / exporter provisioning /
    dashboard fetch / prometheus reload are NOT done here — they are returned in
    plan["observability"]["enact"] for the operator (deploy-stack / Semaphore)."""
    changed = gitio._write_new(plan["mod_path"], plan["module_text"], _BANNER_MOD)
    changed |= gitio._write_new(plan["inv_path"], plan["host_text"], _BANNER_INV)
    changed |= enable_in_fleet(plan["key"])
    paths_changed = [plan["mod_path"], plan["inv_path"], os.path.join("config", "fleet.yml")]
    creds_to_set = plan["creds_to_set"]
    if creds_to_set:
        print("--- encrypting credentials into the %s SOPS domain ---" % plan["secrets"])
        for k, v in creds_to_set.items():
            if gitio.sops_set(plan["secrets"], k, v):
                changed = True
        paths_changed.append(os.path.join("ansible", "secrets", "%s.sops.yml" % plan["secrets"]))
    obs = plan.get("observability")
    if obs:
        # Data-only regeneration in the working tree (no actuation). Reuse the standalone
        # generators (yaml+stdlib only). These rewrite GENERATED lockfiles, kept honest by
        # the --check guards in tests/validate (gen-observability already; gen-homepage new).
        if "scripts/gen-observability.py" in obs["regenerate"]:
            gitio._run(["python3", os.path.join(paths.ROOT, "scripts", "gen-observability.py")])
            paths_changed.append(os.path.join("prometheus", "targets"))   # dir; git add catches the new file
            changed = True
        if "scripts/gen-homepage.py" in obs["regenerate"]:
            gitio._run(["python3", os.path.join(paths.ROOT, "scripts", "gen-homepage.py")])
            paths_changed.append(os.path.join("dashboards", "homepage", "services.yaml"))
            changed = True
    return {"changed": changed, "paths": paths_changed, "enact": (obs or {}).get("enact", [])}
```

The `enact` list surfaces in the CLI/API/GUI response as the operator's remaining steps — making the propose/enact split visible and honest.

### 4.3 `gen-observability.py` and the exportarr `via:proxy` method

The radarr `metrics:` block uses `{method: exportarr}`. That method's descriptor (`telemetry/exportarr.yml`), the exportarr docker fragment, and the generated Prometheus relabel are **owned by the telemetry dimension** (the keystone seam at `gen-observability.py:51`). This dimension's only requirement is: **`build_service_plan` + `attach_observability` emit a `metrics:` entry that the telemetry dispatcher already knows how to render.** The contract between the two dimensions is the `{method, params}` shape — nothing more. If the telemetry phase has not landed, a radarr onboard with `--telemetry-method exportarr` returns `error: "unknown_method"` (fail closed), and the operator onboards radarr *without* metrics until exportarr exists. This is the clean phase boundary the self-review demanded.

### 4.4 New file: `scripts/gen-homepage.py` (the widget generator)

The `widget:` block is documented but unwired (`modules/README.md:34`). This generator reads every enabled module's `widget:` and **appends** a tile to the shared `dashboards/homepage/services.yaml` (Homepage has no per-service drop-in glob — confirmed by the file's own header: "Add a service = add a list item under the right group"). It is standalone (yaml + stdlib), carries a GENERATED-region marker, and is kept honest by a new `--check` step.

```python
#!/usr/bin/env python3
"""Generate the Homepage portal tiles for service/device modules that declare a `widget:`
block — the portal analogue of gen-observability.py. Reads each enabled module's widget:
{type, url, secret} and renders a tile into a GENERATED region of
dashboards/homepage/services.yaml (Homepage has no per-service drop-in glob, so this is an
append into the shared file, between marker comments). Standalone (yaml+stdlib). Kept honest
by `--check` (a tests/validate step). Secrets are env-var NAMES (HOMEPAGE_VAR_*), never values.

Usage: python3 scripts/gen-homepage.py [--check]
"""
# Implementation sketch:
#  1. _load instance/fleet.yml + each enabled modules/<key>/module.yml.
#  2. for modules with a widget: block, build a tile:
#        {<key-title>: {href: <service_url or widget.url>, widget: {type, url, key: "{{<secret>}}"}}}
#     grouped under a "kontroll-managed" group, rendered between
#       "# >>> generated by gen-homepage.py >>>" and "# <<< generated by gen-homepage.py <<<"
#     markers so the hand-authored tiles above are never touched.
#  3. --check: re-render the generated region and diff against the file's current region;
#     non-zero + a "STALE homepage tiles" message if they differ (mirror gen-observability main()).
#  4. write: splice the new region in place, LF-only (stable lockfile).
```

The marker-region approach keeps `services.yaml` composition-friendly: the hand-authored Infrastructure/Media/Management tiles stay where they are; only the bracketed generated region is machine-owned.

---

## 5. Radarr end-to-end (the worked walkthrough)

Every step grounded in a real seam. Steps 1 and 6 already exist; 2–5 and 7 are this design.

**Step 1 — Discover (EXISTS).** Operator runs the already-built ingester:
```
python3 scripts/galaxy.py openapi http://192.0.2.20:7878/api/v3/openapi.json --name radarr --port 7878 --emit
```
`fetch_spec` (lenient TLS, `openapi.py:24`) → `build_openapi_recipe` (`openapi.py:103`). Radarr's spec declares an `X-Api-Key` apiKey-in-header scheme, so `derive_auth` (`openapi.py:48`) returns `{type: apikey_header, header_name: X-Api-Key, token_var: radarr_api_token}`. `derive_server` defaults 443; `--port 7878` overrides it. `classify_endpoints` finds `/api/v3/system/status` (check) and no backup endpoint → an actuate+check recipe. Writes `ansible/backends/api/recipes/radarr.yml` (refuses overwrite — `galaxy.py:309`).

**Step 2 — Service plan (NEW — `build_service_plan`).** New file `scripts/kontroll/service/service_onboard.py`:

```python
"""service-onboard domain — the SERVICE root (OpenAPI-rooted), sibling to the device root.

build_service_plan is PURE: from a recipe name (an already-emitted
ansible/backends/api/recipes/<name>.yml) + the service's live URL, it computes the SAME plan
shape build_onboard_plan returns — a `kind: service` module dict, a localhost-delegated
inventory host, the API-key creds_to_set, the rel paths — WITHOUT a Galaxy collection or a
deep probe (a service has neither). apply_onboard_plan (shared) does the writes.
"""
import os
import yaml
from kontroll import gitio, paths


def build_service_plan(recipe, key, service_url, secrets="services",
                       api_token=None, host_name=None):
    """Compute the SERVICE onboard plan for a recipe-driven API service. Returns the same
    plan shape as build_onboard_plan (so apply_onboard_plan is shared), with error:
    "no_recipe" if ansible/backends/api/recipes/<recipe>.yml is absent. No deep_probe — a
    service has no collection. Credentials flow through creds_to_set in memory only."""
    recipe_path = os.path.join(paths.ROOT, "ansible", "backends", "api", "recipes",
                               "%s.yml" % recipe)
    if not os.path.exists(recipe_path):
        return {"error": "no_recipe", "recipe": recipe}
    rdef = yaml.safe_load(open(recipe_path, encoding="utf-8")) or {}
    host_name = host_name or key
    module = {"key": key, "kind": "service",
              "description": "%s — REST service via the api backend (onboarded - VERIFY before active)" % key,
              # (a "status": "staged" line stood here; the field was removed 2026-10-19)
              "collections": [], "role": "backend_api", "backend": "api",
              "recipe": recipe, "service_url": service_url,
              "inventory_group": "services", "secrets_domain": secrets}
    module_text = yaml.safe_dump(module, sort_keys=False, allow_unicode=True, width=4096)

    key_var = "%s_api_key" % key
    _sops = ("lookup('community.sops.sops', inventory_dir ~ '/../secrets/%s.sops.yml') | from_yaml"
             % secrets)
    hostvars = {"ansible_host": "127.0.0.1", "ansible_connection": "local",
                "device_role": "backend_api", "backend_api_recipe": recipe,
                "%s_service_url" % key: service_url,
                key_var: "{{ (%s).%s }}" % (_sops, key_var)}
    host_block = {"services": {"hosts": {host_name: hostvars}}}
    host_text = yaml.safe_dump(host_block, sort_keys=False, allow_unicode=True, width=4096)
    creds_to_set = {key_var: api_token} if api_token is not None else {}

    return {"error": None, "collection": None, "key": key, "group": "services",
            "secrets": secrets, "backend": "api", "role": "backend_api",
            "host_name": host_name, "module": module, "module_text": module_text,
            "host_block": host_block, "host_text": host_text, "creds_to_set": creds_to_set,
            "mod_path": os.path.join("modules", key, "module.yml"),
            "inv_path": os.path.join("ansible", "inventory", "onboarded-%s.yml" % key),
            "observability": None}
```

This returns the **exact same keys** `build_onboard_plan` does (`onboard.py:81-86`) plus `observability: None`, so `apply_onboard_plan` and `_plan_view` work unchanged.

**Step 3 — Attach observability (NEW — shared opt-in).** The operator picks exportarr + the radarr dashboard + the widget:
```python
plan = build_service_plan("radarr", "radarr", "http://192.0.2.20:7878",
                          api_token="<radarr-api-key>")
plan = attach_observability(plan, telemetry_method="exportarr",
                            method_params={"app": "radarr",
                                           "url_var": "radarr_service_url",
                                           "api_key_var": "radarr_api_key"},
                            dashboard={"gnet": 12896, "name": "radarr"},
                            widget={"type": "radarr", "url": "http://192.0.2.20:7878",
                                    "secret": "HOMEPAGE_VAR_RADARR_KEY"})
```
Now `plan["module"]` carries `metrics:`/`dashboards:`/`widget:` and `plan["observability"]["enact"]` lists `["deploy-stack (provisions the exportarr exporter + renders its secret)", "scripts/fetch-dashboards.py (fetches gnet 12896)"]`.

**Step 4 — Apply (SHARED mutator).** `apply_onboard_plan(plan)` writes `modules/radarr/module.yml`, `instance/inventory/onboarded-radarr.yml`, enables `radarr` in `instance/fleet.yml`, encrypts `radarr_api_key` into `instance/secrets/services.sops.yml`, regenerates Prometheus targets (the exportarr `via:proxy` target file), and appends the radarr Homepage tile via `gen-homepage.py`. Returns `enact: [...]`.

**Step 5 — Enact (OPERATOR-CLI / Semaphore, NOT the network surface).** The operator (or a Semaphore task) runs `deploy-stack` — which provisions the exportarr container, renders `RADARR_API_KEY`/`HOMEPAGE_VAR_RADARR_KEY` from the `services` SOPS domain into `docker/.env` (`no_log`, mirroring `deploy-stack.yml:165-167`), regenerates targets, and reloads Prometheus/Grafana — then `fetch-dashboards.py` (online) fetches gnet 12896.

**Step 6 — Homepage tile (was hand-authored; now generated).** `dashboards/homepage/services.yaml:42-47` already has a hand-authored radarr tile. After this design, that tile moves into the `gen-homepage.py` generated region (the same commit deletes the hand-authored one and lets the generator own it — proving the unwired `widget:` block now drives the portal).

**Step 7 — GUI/API (DEFERRED — §6).** The "service" tab in the GUI and the `POST /service-onboard` route are the last phase, gated behind the propose-then-promote posture.

---

## 6. CLI / API / GUI surfaces (parallel seams, drop-in routes)

### 6.1 CLI — new subcommand (drop-in, beside `onboard` and `openapi`)

Add to `scripts/galaxy.py`'s subparser block (after `oa = sub.add_parser("openapi" …)` at `galaxy.py:418`):

```python
so = sub.add_parser("service-onboard", help="onboard a composed SERVICE from an emitted api recipe")
so.add_argument("--recipe", required=True, help="ansible/backends/api/recipes/<recipe>.yml (from `openapi --emit`)")
so.add_argument("--key", required=True, help="service module key (modules/<key>/)")
so.add_argument("--service-url", dest="service_url", required=True, help="live service base URL")
so.add_argument("--secrets", default="services", help="secrets domain (default services)")
so.add_argument("--api-token", dest="api_token", help="service API key — encrypted into the secrets domain")
so.add_argument("--telemetry-method", help="observability opt-in: a telemetry/<method>.yml name (e.g. exportarr)")
so.add_argument("--dashboard-gnet", type=int, help="curated Grafana.com dashboard id to attach")
so.add_argument("--apply", action="store_true", help="write module + drop-in service host + fleet-enable + creds")
so.add_argument("--commit", action="store_true", help="git-commit the applied edits (implies --apply)")
so.add_argument("--push", action="store_true", help="also push origin (offsite backup; implies --commit)")
so.set_defaults(func=cmd_service_onboard)
```

`cmd_service_onboard` mirrors `cmd_onboard` (`galaxy.py:200`) exactly — dry-run prints the plan, `--apply`/`--commit`/`--push` promote, **no `--bootstrap`** (a service has no agent to install; enact is `deploy-stack`). It prints `plan["observability"]["enact"]` as the operator's remaining steps. The `unknown_method` error → `sys.exit("no telemetry method '%s' — see telemetry/ (omit --telemetry-method to onboard without metrics)")`.

### 6.2 API — new drop-in route `api/routes/service_onboard.py`

A **privileged** route mirroring `api/routes/onboard.py` (token + fail-closed audit, dry-run default, propose-only). Registered by adding `service_onboard` to the `include_router` loop in `api/main.py:51`.

```python
router = APIRouter(tags=["privileged"])

class ServiceOnboardIn(BaseModel):
    """A service onboard request. The api_token (service API key) is encrypted into SOPS during
    apply and is NEVER echoed back or logged — only its derived var name appears in the plan."""
    recipe: str
    key: str
    service_url: str
    secrets: str = "services"
    api_token: Optional[str] = None
    telemetry_method: Optional[str] = None      # observability opt-in (a telemetry/<name>.yml)
    dashboard_gnet: Optional[int] = None
    apply: bool = False
    push: bool = False

@router.post("/service-onboard", summary="Onboard a composed service (dry-run, or apply — audited)")
def service_onboard(body, request, principal=Depends(require_token), cat=Depends(get_catalog)) -> dict:
    """Dry-run returns the SERVICE onboard plan (module + localhost host + observability sub-plan
    + the ENACT steps the operator must run via deploy-stack/Semaphore); apply writes them +
    encrypts the API key + regenerates targets/tiles, then commits + pushes the local canonical.
    422 no_recipe; 422 unknown_method. The exporter container, the secret render, the dashboard
    fetch, and the prometheus reload are DELEGATED (never run by the API) — propose-only here."""
    plan = build_service_plan(body.recipe, body.key, body.service_url,
                              secrets=body.secrets, api_token=body.api_token)
    if plan["error"] == "no_recipe":
        raise HTTPException(422, "emit the recipe first: galaxy.py openapi <spec> --name %s --emit" % body.recipe)
    if body.telemetry_method or body.dashboard_gnet:
        dash = {"gnet": body.dashboard_gnet, "name": body.key} if body.dashboard_gnet else None
        plan = attach_observability(plan, telemetry_method=body.telemetry_method, dashboard=dash,
                                    telemetry_methods=cat.telemetry_methods)
        if plan.get("error") == "unknown_method":
            raise HTTPException(422, "no telemetry method '%s'" % plan["unknown_method"])
    if not body.apply:
        return {"applied": False, "plan": _service_plan_view(plan), "run_id": principal.run_id}
    audit_action(request, principal, "service-onboard-apply",
                 "recipe=%s key=%s url=%s method=%s creds=%s push=%s" % (
                     body.recipe, body.key, body.service_url, body.telemetry_method or "none",
                     ",".join(sorted(plan["creds_to_set"])) or "none", body.push))
    applied = apply_onboard_plan(plan)
    # ... commit_and_push exactly as onboard.py:76-88, with the service commit message ...
    return {"applied": True, "changed": applied["changed"], "enact": applied["enact"],
            "host_name": plan["host_name"], "run_id": principal.run_id,
            "next": "exporter provisioning + dashboard fetch are deploy-stack/Semaphore steps"}
```

`cat.telemetry_methods` requires adding a `telemetry_methods` field to the `Catalog` dataclass (`api/deps.py:16`) loaded once in `load_catalog` (`api/deps.py:23`) — owned by the telemetry dimension, consumed here.

**Rate-limit tripwire (must land in the same commit):** add `/service-onboard` to `api/ratelimit.py:53` `_PRIVILEGED` and to the asserted set in `tests/integration/test_api_ratelimit.py:182` (`{'/refresh','/capture-exceptions','/onboard','/audit/log','/service-onboard'}`). The guard `test_every_privileged_route_is_classified` (reads tags from `app.openapi()`) **fails** otherwise — this is the deliberate tripwire, not a bug.

### 6.3 GUI — a "service" track in the form (LAST phase)

The GUI gets a second onboarding mode. New form (`gui/templates/index.html`, rendered when the operator picks the "service" tab) with these **new data-testid values** (all recorded in `tests/testid_reference.md` in the same commit):

| `data-testid` | Element |
|---|---|
| `onboard-root-toggle` | the device⇄service tab switch |
| `service-form` | the service `<form>` root |
| `field-recipe` | the recipe-name input |
| `field-service-url` | the service base-URL input |
| `field-service-secrets` | the secrets-domain input (default `services`) |
| `field-service-api-key` | the API-key input (`type=password`) |
| `field-telemetry-method` | the `<select>` (none / host_node / exportarr / snmp / blackbox) — the observability opt-in |
| `field-dashboard-gnet` | the optional curated-dashboard gnet input |
| `field-service-apply` / `field-service-commit` / `field-service-push` | the actuation checkboxes (apply unchecked = dry-run) |
| `service-run` | the Run button |
| `service-output` | the `<pre>` plan/result pane (must render the ENACT steps) |

The GUI posts to `/api/service-onboard`, which shells to `galaxy.py service-onboard` (mirroring `gui/app.py:158-191`) — **and audits the chosen telemetry method, never the API key** (the `_audit` line records `method=exportarr`, not the key). This phase depends on the GUI getting a run_id-correlated path (route through the API, which mints+threads `run_id` at the boundary, rather than the fail-open 4-field GUI `_audit`) — the self-review's audit-silo concern, resolved by mandating the API path for any apply.

---

## 7. First-class deliverables (same commit as the behaviour)

### 7.1 Tests

**`tests/unit/test_service_onboard.py`** (uses the `tmp_repo` fixture for the apply variants):

| Test | Docstring (what it verifies + the failure it guards) |
|---|---|
| `test_build_service_plan_emits_kind_service_module` | "A service plan's module dict carries kind: service, role: backend_api, backend: api, collections: [], and the recipe/service_url fields — guards a service module being indistinguishable from a device module (the two-root data-model split)." |
| `test_build_service_plan_no_recipe_fails_closed` | "build_service_plan with a missing recipe returns error: no_recipe and writes nothing — guards onboarding a service whose recipe was never emitted (the dangling-reference failure)." |
| `test_service_plan_shape_matches_onboard_plan_keys` | "build_service_plan returns the SAME top-level keys as build_onboard_plan, so the shared apply_onboard_plan/_plan_view work on both roots — guards the two builders silently diverging (a fork)." |
| `test_service_host_is_localhost_delegated` | "The service inventory host is ansible_host 127.0.0.1 + ansible_connection local + backend_api_recipe + an inline-SOPS api-key lookup — guards a service being given a real device connection it cannot use." |
| `test_service_creds_are_var_names_in_plan_view` | "_service_plan_view exposes only the API-key VAR NAME, never the value — guards a service API key leaking into a plan response/log (the secret-safety invariant)." |
| `test_apply_service_plan_writes_module_inventory_fleet` | "apply_onboard_plan on a service plan writes modules/<key>/module.yml + onboarded-<key>.yml, enables it in fleet, and sops-sets the key — guards the shared mutator not handling a service plan." |

**`tests/unit/test_observe.py`**:

| Test | Docstring |
|---|---|
| `test_attach_observability_injects_metrics_block` | "attach_observability puts a {method, params} metrics: block into the plan's module dict and re-renders module_text — guards the observability opt-in not actually reaching the module (gap e: onboarding never generated observability blocks)." |
| `test_attach_observability_unknown_method_fails_closed` | "A telemetry_method with no telemetry/<name>.yml descriptor sets error: unknown_method and writes no metrics: block — guards a typo'd method silently producing an empty/wrong target." |
| `test_attach_observability_records_enact_not_runs_it` | "attach_observability records the exporter-provision/dashboard-fetch as enact STRINGS and runs nothing — guards the network-surface propose-only boundary (the API/GUI must not actuate)." |
| `test_attach_observability_optout_is_noop` | "With no method/dashboard/widget, plan[observability] is None and the module is unchanged — guards onboarding-without-observability still working (back-compat)." |
| `test_attach_observability_widget_drives_homepage` | "A widget arg injects a widget: block and lists gen-homepage.py in regenerate — guards the documented-but-unwired widget: block (modules/README.md:34) finally reaching the portal." |

**`tests/integration/test_api_service_onboard.py`** (mirrors `test_api_auth.py`'s `priv` fixture battery):

| Test | Docstring |
|---|---|
| `test_service_onboard_503_when_token_unconfigured` | "POST /service-onboard 503s when KONTROLL_API_TOKEN is unset — guards the privileged service-onboard surface being open by default (fail-closed auth)." |
| `test_service_onboard_dryrun_returns_plan_and_enact` | "A dry-run returns the plan + the enact steps and mutates nothing — guards apply-by-default on the highest-blast-radius new route." |
| `test_service_onboard_apply_audited_with_run_id` | "An apply writes a service-onboard-apply audit line carrying the run_id and the method, and the API key value is ABSENT from the line — guards both the audit-before-mutate contract and the secret-exclusion invariant." |
| `test_service_onboard_unknown_method_422` | "An unknown telemetry_method 422s before any write — guards a bad observability opt-in corrupting the module." |

**`tests/integration/test_api_ratelimit.py`** — update `test_every_privileged_route_is_classified`'s expected set to include `/service-onboard` (docstring rationale: "the new privileged service-onboard route is classified privileged, not the cheap inquiry budget").

**`tests/unit/test_gen_homepage.py`**:

| Test | Docstring |
|---|---|
| `test_gen_homepage_renders_widget_tile` | "A module with a widget: block produces a Homepage tile (href + widget type/url/key-name) in the generated region — guards the widget generator never emitting a tile." |
| `test_gen_homepage_preserves_hand_authored_tiles` | "gen-homepage only rewrites the marked generated region, leaving hand-authored tiles untouched — guards the generator clobbering the composition-only services.yaml." |
| `test_gen_homepage_check_detects_stale` | "--check returns non-zero when the generated region is stale — guards the lockfile-honesty rule for the new artifact (mirrors gen-observability --check)." |
| `test_gen_homepage_widget_secret_is_name_not_value` | "A rendered tile's key is the HOMEPAGE_VAR_* env NAME, never a secret value — guards a portal secret being committed." |

**E2E** `tests/e2e/test_service_onboard_flow.py` (data-testid only, `run_galaxy` mocked per `tests/e2e/conftest.py`): `test_service_onboard_dryrun_renders_plan` — "switching to the service tab, filling recipe/url, and Run with apply unchecked renders the plan + enact steps and mutates nothing — guards the service root's dry-run-by-default UX."

### 7.2 Log + audit lines (run_id-correlated; never the credential)

- **API apply (`api/auth.py audit_action`, 6-field TSV):**
  `…⇥service-onboard-apply⇥<run_id>⇥recipe=radarr key=radarr url=http://192.0.2.20:7878 method=exportarr creds=radarr_api_key push=false`
  (the cred VAR NAME, never the key value).
- **GUI (`gui/app.py _audit`, 4-field):** `…⇥service-onboard-dryrun⇥recipe=radarr key=radarr method=exportarr flags=none` — and `service-onboard-result⇥key=radarr rc=0 applied=true`.
- **Commit body** (the run_id stitch): `Onboarded service radarr via the api backend + exportarr telemetry (run_id <id>). Staged — verify live + deploy-stack to provision the exporter.`

### 7.3 SECURITY.md — C9 amendment (new secret domain + new trust surface)

Add to control **C9** (or a sibling) — the `services` SOPS domain holds service API keys (radarr/sonarr), which are **read-and-write capable** on the *service* (unlike pve-exporter's read-only token), so the blast radius is different and must be stated:

> **Implements:** `instance/secrets/services.sops.yml` (a new domain, base_recipients only via the `.sops.yaml` catch-all — control VM + break-glass, **not** the scoped Semaphore key, since the exporter reads it at scrape time, not a runner at job time). Rendered `no_log` into `docker/.env` by `deploy-stack` (mirroring `PVE_EXPORTER_*`); consumed by exportarr via `${RADARR_API_KEY:-}`. The exporter publishes **no host port** (kontroll net only, never WAN — C3).
> **Proves:** `tests/unit/test_service_onboard.py::test_service_creds_are_var_names_in_plan_view` + `test_api_service_onboard.py::test_service_onboard_apply_audited_with_run_id` (the key value never appears in a plan view or an audit line) + a `tests/validate` assertion that `docker/services/*exporter*.yaml` carries no host `ports:` mapping.
> **Residual (tracked):** a Servarr API key is read/write on that app (not read-only like the PVE Audit token) — a leak permits library manipulation, not lab actuation; scoped to the `services` domain, mgmt-VLAN-only.

This trips the SECURITY.md "Update this document when… a new credential field or secret domain is added" trigger and the `.sops.yaml`/`instance/secrets/README.md` doc-sync rows.

### 7.4 Documentation-Sync rows (same commit per the CLAUDE.md table)

| Changed | Update |
|---|---|
| `module.yml` gains `kind`/`recipe`/`service_url`; service modules | `modules/README.md` schema table (new rows) + the catalog table (add `radarr` row, `kind=service`) |
| New `services` SOPS domain + `.sops.yaml` coverage | `instance/secrets/README.md` (new domain row) + `SECURITY.md` C1/C9 |
| New `services` inventory group + host membership | `PLAN.md §6` + `instance/inventory/hosts.yml` (empty `services` leaf) |
| The service onboarding lane (new track) | `docs/api-architecture.md §11` — flip "Backlog" to "implemented (service-onboard lane)"; `docs/capability-matrix.md §5.2` (the OpenAPI tail now has a managed-service consumer) |
| New `gen-homepage.py` + the Homepage `widget:` wiring | `modules/README.md` (mark `widget:` as wired) + `dashboards/README.md` |
| New GUI service-form elements | `tests/testid_reference.md` (the §6.3 table, grep-verified) |
| New tests/fixtures | each test's docstring + `tests/README.md` if a new seam (the `service_onboard`/`observe` service seams) |
| Behaviour-affecting | `CHANGELOG.md [Unreleased]` — "feat(onboard): a second onboarding root — composed services via their OpenAPI spec (radarr → api recipe + exportarr telemetry + Homepage tile), converging on the shared observability opt-in" |
| New `--check` artifact (`gen-homepage.py`) | add a `step gen-homepage 0 "$PY" scripts/gen-homepage.py --check` to `tests/validate.sh` next to the `gen-observability --check` at line 72 |
| `_plan_view`/`OnboardIn` peers | reciprocal "See also" between `api/routes/onboard.py` and `api/routes/service_onboard.py` |

---

## 8. Phasing (smallest-landable-first; honors the self-review)

- **Phase A (data model + service plan, CLI-only, zero new trust boundary):** `kind:` field + `modules/README.md` schema; `build_service_plan` + `cmd_service_onboard`; the `services` inventory group + SOPS domain + `.sops.yaml` + SECURITY C9 amendment; `tests/unit/test_service_onboard.py`. **No observability, no GUI, no API route.** Acceptance: `galaxy.py service-onboard --recipe radarr --key radarr --service-url … --apply` writes a `kind: service` module + a localhost host + encrypts the key; `tests/validate` green; `build_service_plan` shares `apply_onboard_plan` with zero device-path change.
- **Phase B (shared observability opt-in, propose-only):** `attach_observability` + `tests/unit/test_observe.py`; `apply_onboard_plan` observability tail; `gen-homepage.py` + `--check` + `test_gen_homepage.py`; the Homepage radarr tile moves to the generated region. **Depends on the telemetry-method-registry phase** for `catalog.load_telemetry_methods()` and the exportarr descriptor — until then `--telemetry-method` returns `unknown_method` (fail closed) and a service onboards without metrics. Acceptance: `--telemetry-method exportarr --dashboard-gnet 12896 --apply` injects the `metrics:`/`dashboards:`/`widget:` blocks and regenerates the target + tile; the enact steps are *listed*, not run.
- **Phase C (API route, privileged, propose-only):** `api/routes/service_onboard.py` + the rate-limit tripwire update + `Catalog.telemetry_methods` + `test_api_service_onboard.py`. Acceptance: dry-run returns plan+enact; apply audits `service-onboard-apply` with run_id and no key value; the privileged-route guard passes.
- **Phase D (GUI service track, last):** the device⇄service tab + the §6.3 testids + `testid_reference.md` + `test_service_onboard_flow.py`. Mandates the API path for any apply (run_id stitch + fail-closed audit). Acceptance: the e2e dry-run renders the plan and mutates nothing.

## 9. Acceptance criteria (the whole dimension)

1. A composed service (radarr) onboards end-to-end from `galaxy.py openapi … --emit` → `service-onboard` → `deploy-stack`, producing a `kind: service` module, a localhost-delegated inventory host, an encrypted API key in the `services` domain, an exportarr `via:proxy` Prometheus target, a fetched dashboard, and a generated Homepage tile — **without editing any hub file** (no `gen-observability.py` `if/else` edit, no `prometheus.yml` scrape-config edit, no hand-authored target).
2. `build_service_plan` and `build_onboard_plan` are **separate files** returning the **same plan shape**; `apply_onboard_plan` is the single shared mutator (proven by `test_service_plan_shape_matches_onboard_plan_keys`).
3. The network surfaces (API/GUI) **propose only** — they never run Ansible, provision a container, render a secret into `.env`, or reload Prometheus; the enact steps are returned as strings (`test_attach_observability_records_enact_not_runs_it`).
4. No service API key value ever appears in a plan view, an audit line, a commit, or a Homepage tile (the secret-exclusion tests).
5. `git diff prometheus/targets/` and `dashboards/homepage/services.yaml` after a no-op re-onboard are empty (the generators are idempotent lockfiles; both `--check` steps green in `tests/validate`).
6. `docs/api-architecture.md §11` no longer reads "Backlog" for the services lane; the radarr row is in the `modules/README.md` catalog with `kind=service`.

---

### Files touched (summary, absolute paths)

**New:** `scripts/kontroll/service/service_onboard.py`, `scripts/kontroll/service/observe.py`, `scripts/gen-homepage.py`, `modules/radarr/module.yml`, `ansible/inventory/onboarded-radarr.yml`, `ansible/secrets/services.sops.yml`, `api/routes/service_onboard.py`, `tests/unit/test_service_onboard.py`, `tests/unit/test_observe.py`, `tests/unit/test_gen_homepage.py`, `tests/integration/test_api_service_onboard.py`, `tests/e2e/test_service_onboard_flow.py`.

**Modified:** `scripts/kontroll/service/onboard.py` (observability tail in `apply_onboard_plan`), `scripts/galaxy.py` (`cmd_service_onboard` + subparser), `api/main.py` (router registration), `api/deps.py` (`Catalog.telemetry_methods`), `api/ratelimit.py` (`_PRIVILEGED`), `gui/app.py` + `gui/templates/index.html` (service track), `ansible/inventory/hosts.yml` (`services` group), `config/fleet.yml` (radarr enabled), `dashboards/homepage/services.yaml` (radarr tile → generated region), `modules/README.md`, `tests/testid_reference.md`, `tests/validate.sh`, `tests/integration/test_api_ratelimit.py`, `SECURITY.md`, `ansible/secrets/README.md`, `PLAN.md`, `docs/api-architecture.md`, `docs/capability-matrix.md`, `dashboards/README.md`, `CHANGELOG.md`.

**Dependency owned by the telemetry dimension (not this one):** `telemetry/exportarr.yml`, `docker/services/exportarr.yaml`, `catalog.load_telemetry_methods()`, the `gen-observability.py` method dispatcher. This dimension's contract with it is the `{method, params}` metrics-entry shape only.
