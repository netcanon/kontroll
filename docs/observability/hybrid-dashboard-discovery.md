# Hybrid dashboard discovery & UX

> **DESIGN HISTORY — not a description of the tree.** This is the design record of hybrid dashboard discovery, written in June 2026. It was not built; a different design shipped ([dashboards/derived/README.md](../../dashboards/derived/README.md)). Where it disagrees with the code or with `SECURITY.md`, they are right.

> **Part of the [Option-A observability + onboarding flow](../observability-onboarding-flow.md) design (baseline HEAD `63de129`).**
>
> ⚠️ **Binding contracts (the catalog loader name, the flat `telemetry/<method>.yml` schema, the single clean-cutover dispatcher, the `network` job-vs-inventory-group distinction, the `.env` secret-render mechanism) are pinned in the master design's §4.0 SHARED CONTRACTS.** Where this section names a symbol, schema, or sequencing differently, **§4.0 wins** and this section is reconciled to it before implementation. See the master design's §8 “Resolved conflicts” for the per-section reconciliation list.

The following is my complete design section for the **Hybrid dashboard discovery & UX** dimension. It is grounded in the real code I read; every file path, schema, code sketch, test, log/audit line, security control, and doc-sync row is spelled out.

---

# Hybrid dashboard discovery & UX

## 0. Scope, framing, and the one load-bearing decision

This section designs the **dashboard discovery and selection layer** that `scripts/fetch-dashboards.py` lacks today. The fetcher (`scripts/fetch-dashboards.py:47-56`, `declared_dashboards`) reads a **hand-listed** `dashboards: [{gnet, name}]` block from each module's `module.yml` and fetches those exact ids from `grafana.com/api/dashboards`. There is **no discovery** — an operator must already know the gnet id (10347 for Proxmox, 1860 for Node Exporter) before they can declare it. Its own docstring admits the gap: *"This is the curated/pinned tier; the onboarding live-search pick-list rides the same fetch path next"* (`scripts/fetch-dashboards.py:19`).

The hybrid experience has two tiers, both of which already half-exist:

- **Curated tier (default)** — a per-method *suggested-dashboards* map that ships **inside each `telemetry/<method>.yml` descriptor** (the registry the modularity dimension introduces). A `host_node` method suggests gnet 1860; a `pve` method suggests 10347; an `snmp` method suggests a switch board. This is "fetch the known-good board for this exporter type" with zero operator knowledge required.
- **Live tier (manual)** — a `grafana.com/api/dashboards` **search** call (`?search=<text>`), surfaced as a dynamic pick-list. This is the discovery the codebase has never had.

**The one load-bearing scope decision** (forced by the self-review's two blockers on the security lens and the implementability lens): **the discovery and selection layer is READ-ONLY inquiry.** The live grafana.com search route is an **inquiry** route (`tags=["inquiry"]`, no token), exactly like `GET /search` (`api/routes/search.py:8`). Choosing a dashboard **writes nothing by itself** — it appends `{gnet, name}` to the module's `dashboards:` block as part of the **existing onboard propose/apply flow** (the same `apply` gate at `api/routes/onboard.py:64`). The actual `fetch-dashboards.py` run + Grafana reload stays **operator-CLI / deploy-stack**, never the network service (this honors `api/routes/onboard.py:5-6`: *"the API does not run Ansible"*, and the `dashboards/README.md:30` warning that a bad datasource uid is a fatal provisioning event). This keeps the whole dimension shippable **without** the deferred privileged-mutation enablement.

A second hard constraint, stated up front because the brief calls it out: **the grafana.com registry endpoints are undocumented** — they are not in any OpenAPI/Swagger spec. `fetch-dashboards.py` already reverse-engineered them (`GNET = "https://grafana.com/api/dashboards"`, then `/<gnet>` for metadata and `/<gnet>/revisions/<rev>/download` for the JSON). Because there is no spec, **the fetch stays vendored** (committed JSON under `dashboards/grafana/dashboards/`) and **cannot be tracked by the OpenAPI service-onboarding root** (`scripts/kontroll/service/openapi.py`). The live-search tier therefore rides the *same hand-written URL contract* `fetch-dashboards.py` established — it is not, and cannot be, derived from a spec. This is recorded as an accepted risk (§9) and a code comment requirement (§3.2).

> **Naming reconciliation — `suggested_dashboards` (this design, UNBUILT) vs `derive_dashboard` (Rung 4a, BUILT).** This
> section's descriptor key is `suggested_dashboards:` — the **live operator PICKER** tier: a curated *suggestion list*
> an operator chooses from during onboarding (design-only, deferred). The north-star **Rung 4a** shipped a DIFFERENT,
> sibling key — `derive_dashboard:` on the same `telemetry/<method>.yml` — the pinned **auto-derived FLOOR** board: a
> `derived: true` class auto-carries the board its floor method derives, with the id itself DERIVED from a search +
> deterministically pinned (`dashboards/derived/<method>.lock.yml`, `scripts/gen-dashboard-floor.py`). They are two
> distinct tiers that coexist (floor = the always-on default; picker = the operator's opt-in extra) — **do NOT overload
> one key onto the other.** The floor is documented in [dashboards/derived/README.md](../../dashboards/derived/README.md).

---

## 1. The four UX states

The onboarding flow surfaces a **dashboard picker** when (and only when) a telemetry method has been chosen for the device/service. The picker is a state machine with exactly four states. These map 1:1 to the brief's "suggested-default, manual search, one-click-trusted, degrade-to-manual-if-none".

| State | Trigger | What the operator sees | What a "pick" does |
|---|---|---|---|
| **`suggested`** (default) | A telemetry method is chosen AND its descriptor declares `suggested_dashboards` | A short pre-filled list of curated boards (e.g. "Node Exporter Full · gnet 1860", "Proxmox via Prometheus · gnet 10347"), each one-click-selectable | Stages `{gnet, name}` into the plan's `dashboards:` block |
| **`manual_search`** | Operator clicks "search grafana.com" (always available as an escape hatch) | A text input + a results list populated live from `GET /dashboards/search?q=…` | Same: stages the chosen `{gnet, name}` |
| **`trusted`** (one-click) | The suggested entry is marked `trusted: true` in the descriptor (a vendor-canonical board the operator pre-blessed) | The suggested board is **pre-checked**; a single confirm carries it | Auto-staged unless the operator unchecks it |
| **`degrade_manual`** | A method has **no** `suggested_dashboards` (or the live search returns nothing / grafana.com is unreachable) | "No curated dashboard for this method — search grafana.com or skip" + the manual search box | Operator may pick a searched board, or proceed with **no dashboard** (a valid, honest outcome — metrics still flow; the board is optional) |

**Soundness rule (mirrors the three-valued capability honesty at `scripts/kontroll/predicate.py:38-41`):** the picker NEVER fabricates a dashboard. `degrade_manual` with "skip" is a first-class, non-error path — a device can be fully observable (scrape target generated, exporter up) with **zero** dashboards, exactly as `modules/cisco_ios/module.yml` is honestly metrics-less today. "No board" is never silently turned into a fake board.

**Trust gating** (`trusted`): a board is only auto-checked if the *descriptor author* (the operator who wrote `telemetry/<method>.yml`, a committed file under review) marked it trusted. A **live-searched** board is **never** `trusted` — it always requires an explicit pick. This means the only one-click path is over operator-vetted, in-repo data; the external-registry path always needs a human selection. This is the dashboard analogue of "actuation is opt-in, dry-run by default".

---

## 2. The descriptor schema: `suggested_dashboards` inside `telemetry/<method>.yml`

The curated tier ships **with the method**, not with the device class. This is the schema migration that fixes the brief's GAP (g) ("dashboard search is manual-only") and consideration 5/6 (hybrid + suggest-by-exporter-type): the *device class* still owns the final `dashboards:` list (so a class can pin extra boards), but the *method* owns the **suggestions** (so any device using `host_node` gets the Node Exporter board offered without re-declaring it).

### NEW field on the telemetry-method descriptor

The modularity dimension defines `telemetry/<method>.yml`. This dimension adds **one optional field** to that schema:

```yaml
# telemetry/host_node.yml  (the modularity dimension owns the rest of this file)
name: host_node
kind: host-agent
# … (job, port, labels, agent_role — owned by the modularity dimension) …

# Curated Grafana.com dashboards suggested for THIS exporter type. Optional.
# OMIT entirely when the method has no canonical community board (honest — like a
# metrics-less module). Each entry is the same {gnet, name} shape fetch-dashboards.py
# already consumes (scripts/fetch-dashboards.py:54-55), plus picker-only metadata.
suggested_dashboards:
  - gnet: 1860                 # grafana.com/grafana/dashboards/1860
    name: node                 # the output filename stem: dashboards/grafana/dashboards/node.json
    title: Node Exporter Full  # human label for the picker (the grafana.com board name)
    trusted: true              # pre-check in the picker (one-click); vendor-canonical, operator-blessed
```

```yaml
# telemetry/pve.yml
name: pve
kind: proxy-exporter
# …
suggested_dashboards:
  - {gnet: 10347, name: proxmox, title: "Proxmox via Prometheus", trusted: true}
  - {gnet: 1860,  name: node,    title: "Node Exporter Full",     trusted: false}  # secondary, not pre-checked
```

```yaml
# telemetry/snmp.yml   (closes GAP d for the cisco_ios core switch)
name: snmp
kind: proxy-exporter
# …
suggested_dashboards:
  - {gnet: 11169, name: snmp_iftraffic, title: "SNMP Interface Traffic", trusted: false}
```

### Field semantics

| Field | Type | Required | Semantics |
|---|---|---|---|
| `gnet` | int | yes | grafana.com dashboard id; the key `fetch-dashboards.py` fetches by (`scripts/fetch-dashboards.py:79`) |
| `name` | str | yes | output filename stem (`dashboards/grafana/dashboards/<name>.json`); also the dedup key paired with gnet in `declared_dashboards` (`scripts/fetch-dashboards.py:55`) |
| `title` | str | no | human label shown in the picker; defaults to `name` if absent. **Display only** — never written to `module.yml` |
| `trusted` | bool | no (default `false`) | pre-check this entry in the picker (the `trusted` UX state). Only descriptor-authored entries may be trusted; live-searched entries are forced `false` |

The `{gnet, name}` pair written into `module.yml dashboards:` is **byte-identical** to the existing hand-authored shape (`modules/proxmox/module.yml:22-24`), so `declared_dashboards` (`scripts/fetch-dashboards.py:47`) and `pin_datasource` (`:59`) consume it **with zero change**. `title`/`trusted` are picker-only and are *dropped* on the way into `module.yml`.

### NEW catalog loader: `load_telemetry_methods()` (shared with the modularity dimension)

The modularity dimension adds `paths.TELEMETRY_DIR` and `catalog.load_telemetry_methods()` mirroring `load_backends()` (`scripts/kontroll/catalog.py:50-59`). This dimension **reuses** that loader to get at `suggested_dashboards`; it adds a thin helper alongside it so the discovery layer doesn't re-glob:

```python
# scripts/kontroll/catalog.py  (ADD, below load_backends)

def suggested_dashboards_for(method_name, methods=None):
    """The curated `suggested_dashboards` entries for one telemetry method (by name), or [] if the
    method declares none / doesn't exist. Picker-facing helper over the telemetry-method registry —
    the curated tier of the hybrid dashboard UX. Read-only; the registry is the in-repo source of truth."""
    methods = methods if methods is not None else load_telemetry_methods()
    for m in methods:
        if m.get("name") == method_name:
            return m.get("suggested_dashboards") or []
    return []
```

---

## 3. The live grafana.com search tier

### 3.1 New generator function in `scripts/fetch-dashboards.py`

The live tier rides the **same fetch path** (`_get`, `fetch`, `pin_datasource`) the curated tier uses — the file's docstring promised exactly this (`scripts/fetch-dashboards.py:19`). I add **one** function, `search_dashboards`, next to `declared_dashboards` (`scripts/fetch-dashboards.py:47`), and one CLI flag. The function does **not** download dashboards — it returns lightweight *candidates* (id/title/downloads/datasource) for the pick-list. Download happens only when a candidate is chosen and `fetch-dashboards.py` runs over the resulting `module.yml`.

```python
# scripts/fetch-dashboards.py  (ADD)

SEARCH_URL = "https://grafana.com/api/dashboards"   # ?search=<text>&orderBy=downloads — UNDOCUMENTED registry
SEARCH_TIMEOUT = 8        # s; a slow/unreachable grafana.com must degrade to "no suggestions", never hang
SEARCH_CAP = 12           # max candidates surfaced to the pick-list (keeps the UI + the response bounded)


def search_dashboards(text, datasource="prometheus", limit=SEARCH_CAP):
    """Live grafana.com dashboard SEARCH -> a bounded list of candidate dicts for the onboarding pick-list:
    [{gnet, title, downloads, datasource, reviewsCount}]. The discovery tier fetch-dashboards.py lacked
    (its docstring: 'the onboarding live-search pick-list rides the same fetch path next'). NOTE: the
    grafana.com registry has NO published OpenAPI spec, so this URL contract is reverse-engineered and
    VENDORED (kept in code, not derived) — same posture as fetch()/_get above. Degrades to [] on any
    network/parse error (short timeout + caught errors) so a slow registry never blocks onboarding —
    mirrors catalog.galaxy_search's best-effort posture (scripts/kontroll/catalog.py:111-125)."""
    q = urllib.parse.urlencode({"search": text, "orderBy": "downloads", "direction": "desc",
                                "dataSourceSlugIn": datasource, "page": 1, "pageSize": limit})
    try:
        raw = _get("%s?%s" % (SEARCH_URL, q), timeout=SEARCH_TIMEOUT)
        items = (json.loads(raw) or {}).get("items") or []
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        # best-effort: a slow/broken registry degrades the picker to manual-id entry, never a 500
        sys.stderr.write("grafana.com dashboard search unavailable (%s) -- returning no candidates\n" % e)
        return []
    out = []
    for it in items[:limit]:
        gnet = it.get("id")
        if not isinstance(gnet, int):
            continue
        out.append({
            "gnet": gnet,
            "title": it.get("name") or it.get("slug") or str(gnet),
            "downloads": int(it.get("downloads") or 0),
            "datasource": datasource,
            "reviewsCount": int(it.get("reviewsCount") or 0),
        })
    return out
```

This requires adding `import socket`, `import urllib.error`, `import urllib.parse` to `fetch-dashboards.py` (it currently imports only `urllib.request` at `:26`), and threading a `timeout` kwarg into `_get` (it hardcodes `timeout=30` at `:43`):

```python
# scripts/fetch-dashboards.py:41-44  (MODIFY _get to accept a timeout)
def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "kontroll-fetch-dashboards"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")
```

### 3.2 CLI surface (the operator escape hatch + the route's offline test seam)

```python
# scripts/fetch-dashboards.py  main()  (ADD a --search branch, before the fetch loop)
    if "--search" in argv:
        i = argv.index("--search")
        text = argv[i + 1] if i + 1 < len(argv) else ""
        print(json.dumps(search_dashboards(text), indent=2))
        return 0
```

Usage updated in the module docstring: `python3 scripts/fetch-dashboards.py [--list] [--search <text>]`. The `--search` branch is JSON-emitting so an operator can discover an id from the CLI, and so the API route (§4) and its tests can exercise the same code path. **A code comment on `SEARCH_URL` records that the contract is undocumented/vendored** — this is the brief's explicit requirement and the place a future maintainer learns why this isn't spec-derived.

---

## 4. API route(s) for live discovery

### 4.1 New inquiry route: `GET /dashboards/search`

A **new drop-in router** at `api/routes/dashboards.py`, registered in the `include_router` loop (`api/main.py:51`). It is **inquiry-class** (`tags=["inquiry"]`, no token) — discovery reads the public registry and writes nothing, exactly like `GET /search` (`api/routes/search.py:8`). This is deliberately **not** privileged: it does not touch the repo, the fleet, or any secret. Because it is inquiry-tagged, the rate-limiter classifies it as inquiry automatically (`api/ratelimit.py:67-75`) — **no edit to `_PRIVILEGED`** (`api/ratelimit.py:53`) and **no change to the privileged-route set assertion** (`tests/integration/test_api_ratelimit.py:182`). That assertion stays `{"/refresh", "/capture-exceptions", "/onboard", "/audit/log"}` and remains green.

```python
# api/routes/dashboards.py  (NEW)
"""Dashboards route — GET /dashboards/search. The live grafana.com discovery tier of the hybrid
dashboard UX: a read-only search over the public Grafana.com registry, returning candidate boards
for the onboarding pick-list. INQUIRY-class (no token) — it reads a public registry and writes
nothing. The curated tier (suggested-by-method) ships in telemetry/<method>.yml and needs no route;
the CHOSEN board is staged into module.yml by the existing onboard apply flow, not here. The
grafana.com registry has NO OpenAPI spec, so the fetch stays vendored (scripts/fetch-dashboards.py)."""
import importlib.util
import os

from fastapi import APIRouter, Query

from api.models import DashboardCandidate

router = APIRouter(tags=["inquiry"])

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_fetch_dashboards():
    """Load the hyphenated scripts/fetch-dashboards.py by path (not importable as a module) — the same
    importlib-by-path seam tests/unit/test_fetch_dashboards.py uses (test_fetch_dashboards.py:18-23).
    A module-level cache avoids re-reading the file per request."""
    global _FD
    try:
        return _FD
    except NameError:
        pass
    spec = importlib.util.spec_from_file_location(
        "fetch_dashboards", os.path.join(_ROOT, "scripts", "fetch-dashboards.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    globals()["_FD"] = mod
    return mod


@router.get("/dashboards/search", response_model=list[DashboardCandidate],
            summary="Search grafana.com community dashboards (live discovery)")
def dashboards_search(
    q: str = Query(..., min_length=2, max_length=64,
                   description="search text, e.g. 'node exporter' or 'mikrotik'"),
    datasource: str = Query("prometheus", pattern="^[a-z0-9_-]{1,32}$",
                            description="datasource slug filter (default: prometheus)"),
    limit: int = Query(12, ge=1, le=25, description="max candidates"),
) -> list:
    """Return up to `limit` grafana.com dashboard CANDIDATES matching `q`, ordered by downloads —
    the live tier of the hybrid dashboard picker. Read-only over the public registry; degrades to an
    EMPTY list (never a 5xx) when grafana.com is slow/unreachable, so a flaky external registry never
    breaks onboarding. The chosen candidate's {gnet, name} is staged via the onboard flow, not here."""
    fd = _load_fetch_dashboards()
    return fd.search_dashboards(q, datasource=datasource, limit=limit)
```

### 4.2 New response model in `api/models.py`

```python
# api/models.py  (ADD — keeps /openapi.json the typed contract, the convention at models.py:1-6)
class DashboardCandidate(BaseModel):
    """One grafana.com search hit for the dashboard picker: the gnet id (what gets staged into
    module.yml dashboards:), its title, and popularity signals (downloads/reviews) so the operator
    can judge it. `name` is derived client-side from `title` when the operator picks it."""
    gnet: int
    title: str
    downloads: int = 0
    datasource: str = "prometheus"
    reviewsCount: int = 0
```

### 4.3 Registration

```python
# api/main.py:21-22  (ADD the import)
from api.routes import dashboards
# api/main.py:51  (ADD `dashboards` to the include loop tuple)
for module in (health, search, probe, classify, refresh, exceptions, onboard, audit_routes, dashboards):
    app.include_router(module.router)
```

### 4.4 Caching, timeout, and external-service safety

grafana.com is an **external dependency**; the design treats it with the same defensive posture `catalog.galaxy_search` uses for galaxy.ansible.com (`scripts/kontroll/catalog.py:111-125`):

- **Short timeout, caught errors, graceful degrade.** `SEARCH_TIMEOUT = 8s` (matching `_GALAXY_TIMEOUT = 8` at `catalog.py:24`); any `URLError`/`timeout`/`ValueError` returns `[]`. The route therefore **never 5xx's on a flaky registry** — it returns an empty candidate list, which the GUI renders as the `degrade_manual` state (§1). This is the brief's "degrade-to-manual-if-none" wired end to end.
- **In-process TTL cache** to avoid hammering grafana.com when an operator types/retries. A tiny module-level cache in `fetch-dashboards.py`, keyed on `(q, datasource, limit)`, TTL 300 s, capped at 64 entries (LRU-ish prune), guarded by a lock — the same shape and justification as the rate-limiter's in-process store (`api/ratelimit.py:129-154`, "one uvicorn container today"):

```python
# scripts/fetch-dashboards.py  (ADD — a small TTL cache so retyping doesn't spam grafana.com)
import threading, time as _time
_SEARCH_CACHE, _SEARCH_LOCK, _SEARCH_TTL, _SEARCH_MAX = {}, threading.Lock(), 300, 64

def _cache_get(key):
    with _SEARCH_LOCK:
        ent = _SEARCH_CACHE.get(key)
        if ent and ent[0] > _time.time():
            return ent[1]
        _SEARCH_CACHE.pop(key, None)
        return None

def _cache_put(key, value):
    with _SEARCH_LOCK:
        if len(_SEARCH_CACHE) > _SEARCH_MAX:
            now = _time.time()
            for k in [k for k, v in _SEARCH_CACHE.items() if v[0] <= now] or list(_SEARCH_CACHE)[:_SEARCH_MAX // 2]:
                _SEARCH_CACHE.pop(k, None)
        _SEARCH_CACHE[key] = (_time.time() + _SEARCH_TTL, value)
```

`search_dashboards` checks `_cache_get((text, datasource, limit))` first and `_cache_put`s before returning. The cache is an **injected/swappable seam** (module-level, like `api.ratelimit._clock` at `:61`) so tests freeze it trivially.

- **Rate-limited by class.** The inquiry budget (`_DEFAULTS["inquiry"] = (30, 60)`, `api/ratelimit.py:51`) covers `/dashboards/search` automatically — a runaway picker can't flood grafana.com through us. **No code change** to the limiter.
- **No SSRF surface.** The route takes a free-text `q` and a slug-pattern-validated `datasource`, and interpolates them **only** into `urlencode`d query params against the **fixed** `SEARCH_URL` host (`grafana.com`). The operator never supplies a URL or host — `gnet` ids selected later are integers fetched against the same fixed host (`fetch-dashboards.py:79`). There is no path for caller input to redirect the fetch to an arbitrary address.

---

## 5. How a chosen dashboard flows back into `module.yml` → fetch → provisioning

This is the "closes the loop" path. It threads the picker selection through the **existing** onboard plan/apply machinery (`scripts/kontroll/service/onboard.py`), the **existing** fetch (`scripts/fetch-dashboards.py`), and the **existing** Grafana file provider (`dashboards/grafana/provisioning/dashboards/providers.yml`) — adding data, not new actuation paths.

### 5.1 Selection → plan (PURE)

The modularity/onboarding dimension extends `build_onboard_plan` (`scripts/kontroll/service/onboard.py:46-54`) to inject a `metrics:` block when a telemetry method is chosen. **This dimension adds the parallel `dashboards:` injection**, staying in the same PURE plan builder:

```python
# scripts/kontroll/service/onboard.py  build_onboard_plan  (ADD, alongside the metrics injection)
# `dashboards` arrives as a list of {gnet, name} the caller assembled from the picker
# (curated suggestions and/or live-searched picks). PURE: we only shape the plan dict.
if dashboards:
    module["dashboards"] = [{"gnet": int(d["gnet"]), "name": str(d["name"])} for d in dashboards]
```

Field discipline: only `{gnet, name}` reach `module.yml` (the shape `fetch-dashboards.py` consumes). `title`/`trusted`/`downloads` are picker metadata and are **dropped here**. `int()`/`str()` coercion + the picker's own validation (gnet from a chosen candidate, never free text) keep this injection-safe — the values are already constrained ints/slugs from the registry, not arbitrary operator strings, and `module.yml` is written via `gitio._write_new` (`scripts/kontroll/gitio.py`) then YAML-parsed by the generators, never string-templated into config.

The `_plan_view` (`api/routes/onboard.py:38-45`) gains the staged dashboards so a dry-run shows them:

```python
# api/routes/onboard.py  _plan_view  (ADD a key)
"dashboards": plan["module"].get("dashboards", []),
```

### 5.2 Apply → vendored fetch (operator-CLI / deploy-stack, NOT the API)

On `apply`, `apply_onboard_plan` (`scripts/kontroll/service/onboard.py:89-107`) writes the module with its new `dashboards:` block (via the existing `gitio._write_new`) and commits. **It does NOT call `fetch-dashboards.py`** — the actual download + Grafana reload remains the operator's deliberate step, for three grounded reasons:

1. `fetch-dashboards.py` is **online and explicitly not hermetic** (`scripts/fetch-dashboards.py:18`: *"ONLINE (needs grafana.com)… NOT a hermetic --check"*) — it must not run inside a request handler or be gated by CI.
2. A bad datasource/JSON is a **fatal Grafana provisioning event** requiring a volume recreate (`dashboards/README.md:30`) — too blast-y to trigger from a network click.
3. The API **does not run Ansible/Docker** by design (`api/routes/onboard.py:5-6`).

The operator (or a Semaphore task) runs the existing one-liner after the onboard commit lands — and `deploy-stack` already regenerates observability before bringing the stack up. The CHANGELOG already anticipates this seam (`CHANGELOG.md`: *"This is the installable primitive the onboarding observability opt-in (the next step) triggers"*). The chosen-dashboard flow is:

```
picker → plan.dashboards → (apply) module.yml dashboards:  ──┐
                                                             │  (operator / Semaphore / deploy-stack)
  python3 scripts/fetch-dashboards.py    ←────────────────────┘
      → declared_dashboards() picks up the new {gnet,name}  (fetch-dashboards.py:47-56)
      → fetch() + pin_datasource()                          (fetch-dashboards.py:77-82, :59-74)
      → writes dashboards/grafana/dashboards/<name>.json    (fetch-dashboards.py:92-97)
  → Grafana file provider auto-loads it (updateIntervalSeconds 30)  (providers.yml)
```

`declared_dashboards` already **dedups by gnet across all enabled modules** (`fetch-dashboards.py:54-55`, `setdefault`), so two devices picking gnet 1860 fetch it **once** — the picker can suggest the same board for many devices without duplication. The provisioning side needs **no change**: the file provider already auto-loads any JSON dropped in the dir (`providers.yml`, `foldersFromFilesStructure: true`), so a newly fetched `<name>.json` appears with no edit to a hub file.

### 5.3 Datasource pinning (single-datasource today; Loki is out of scope)

`pin_datasource` (`scripts/fetch-dashboards.py:59-74`) hardwires `DS_UID = "prometheus"` (`:32`), pinned in `dashboards/grafana/provisioning/datasources/prometheus.yml`. The hybrid picker passes `datasource="prometheus"` (the route default) so searched boards are filtered to Prometheus-backed ones and pin cleanly. **Loki/multi-datasource pinning is explicitly deferred** (the brief's GAP h; `dashboards/README.md:41` already TODOs a `loki.yml` datasource drop-in). The route exposes the `datasource` param for forward-compat, but only `prometheus` is wired end-to-end today; a Loki value would search-filter correctly but `pin_datasource` would not yet pin a second uid — recorded as out-of-scope (§9).

---

## 6. GUI: the dashboard picker

The picker is added to the onboard form (`gui/templates/index.html:99-120`, `onboardForm`) and the submit body (`:122-133`, `submit`). It renders **only when a telemetry method is selected** (the modularity/onboarding dimension adds the method `<select>`; this dimension keys the picker off it). Every interactive element gets a stable `data-testid` (the SOP at `tests/testid_reference.md:9`), and the picker forwards the chosen `{gnet, name}` list in the POST body.

### 6.1 New form section (inserted into the `onboardForm` template literal)

```html
<!-- gui/templates/index.html  onboardForm()  — appended after the actuation flags block -->
<div class="full" data-testid="dashboard-picker" style="display:none">
  <label>dashboards (observability)</label>
  <div data-testid="dashboard-suggested"></div>          <!-- curated, by method (state: suggested/trusted) -->
  <div class="row">
    <input name="dash_q" data-testid="dashboard-search-input" placeholder="search grafana.com…">
    <button type="button" data-testid="dashboard-search-btn">Search</button>
  </div>
  <div data-testid="dashboard-results"></div>            <!-- live grafana.com candidates -->
  <div class="warn" data-testid="dashboard-none" style="display:none">
    No curated dashboard for this method — search grafana.com or skip (metrics still flow).
  </div>
</div>
```

### 6.2 New JS (curated render + live search + chosen-list assembly)

```js
// gui/templates/index.html  <script>  (ADD)

// Curated suggestions per method ship in telemetry/<method>.yml; the GUI fetches them once via a
// tiny inquiry passthrough OR (simpler, no new route) inlines them from the method <select>'s
// data-suggested attribute the onboarding dimension stamps. Chosen boards accumulate on the form.
function renderDashboardPicker(f, suggested) {
  const wrap = f.querySelector('[data-testid="dashboard-picker"]');
  const sug  = f.querySelector('[data-testid="dashboard-suggested"]');
  const none = f.querySelector('[data-testid="dashboard-none"]');
  wrap.style.display = 'block'; sug.innerHTML = '';
  f._chosenDashboards = [];                                   // {gnet,name} list forwarded on submit
  if (!suggested || !suggested.length) { none.style.display = 'block'; return; }
  none.style.display = 'none';
  suggested.forEach(d => {
    const row = E('label','row',
      `<input type="checkbox" data-testid="dashboard-suggested-item" data-gnet="${d.gnet}" `
      + `data-name="${d.name}" ${d.trusted ? 'checked' : ''}> ${d.title||d.name} · gnet ${d.gnet}`
      + (d.trusted ? ' <span class="badge yes">trusted</span>' : ''));
    sug.appendChild(row);
  });
}

async function dashboardSearch(f) {
  const q = f.querySelector('[data-testid="dashboard-search-input"]').value.trim();
  const box = f.querySelector('[data-testid="dashboard-results"]'); box.innerHTML = '';
  if (q.length < 2) return;
  box.appendChild(E('div','spin','Searching grafana.com…','dashboard-search-loading'));
  let data;
  try { data = await (await fetch('/dashboards/search?q='+encodeURIComponent(q))).json(); }
  catch(e){ box.innerHTML=''; box.appendChild(E('div','warn','search error','dashboard-search-error')); return; }
  box.innerHTML='';
  if (!data.length){ box.appendChild(E('div','spin','no dashboards found','dashboard-no-results')); return; }
  data.forEach(c => {
    const slug = (c.title||('board-'+c.gnet)).replace(/[^a-z0-9]+/gi,'_').toLowerCase().replace(/^_|_$/g,'');
    const row = E('label','row',
      `<input type="checkbox" data-testid="dashboard-result-item" data-gnet="${c.gnet}" `
      + `data-name="${slug}"> ${c.title} · gnet ${c.gnet} · ${c.downloads.toLocaleString()} downloads`);
    box.appendChild(row);
  });
}

function collectDashboards(f) {
  const picked = [];
  f.querySelectorAll('[data-testid="dashboard-suggested-item"],[data-testid="dashboard-result-item"]')
   .forEach(cb => { if (cb.checked) picked.push({gnet: +cb.dataset.gnet, name: cb.dataset.name}); });
  // dedup by gnet (mirrors declared_dashboards' setdefault, fetch-dashboards.py:55)
  const seen = new Set();
  return picked.filter(d => !seen.has(d.gnet) && seen.add(d.gnet));
}
```

The search button wires to `dashboardSearch(f)`; `submit` (`index.html:122`) adds `dashboards: collectDashboards(f)` to the POST `body`. The GUI `/api/onboard` shell-out (`gui/app.py:170-187`) threads the chosen list to the CLI (`galaxy.py onboard … --dashboard <gnet>:<name>` repeated), and the `_audit` line (`gui/app.py:166`) gains `dashboards=<count>` (a **count, never** the board contents — there's no secret here, but the audit stays metadata-only by convention).

### 6.3 `data-testid` rows to add to `tests/testid_reference.md` (same commit)

A new section under "Onboard form":

| `data-testid` | Element |
|---|---|
| `dashboard-picker` | the picker container (shown when a telemetry method is chosen) |
| `dashboard-suggested` | curated-suggestions container |
| `dashboard-suggested-item` | one curated board checkbox; `data-gnet` + `data-name` discriminators; `trusted` ones are pre-checked |
| `dashboard-search-input` / `dashboard-search-btn` | the live grafana.com search box + trigger |
| `dashboard-results` | live-candidate container |
| `dashboard-result-item` | one live candidate checkbox; `data-gnet` + `data-name` |
| `dashboard-search-loading` / `dashboard-search-error` / `dashboard-no-results` | the three live-search states (in-flight / transport error / empty) |
| `dashboard-none` | the `degrade_manual` notice ("no curated dashboard… or skip") |

Grep-verify each before commit, per `tests/testid_reference.md:11`.

---

## 7. Tests (each with its what-and-why docstring)

All test functions carry a docstring — machine-enforced by `tests/check-test-docs.py` via `tests/validate.sh:67`. Unit/integration run hermetically (`-m "not e2e and not slow"`, `validate.sh:60`); the grafana.com network call is **mocked at its home seam** (`fetch_dashboards._get`), never reaching the registry in CI — the same discipline `test_fetch_dashboards.py:6` states ("the network fetch itself is exercised live, not in the hermetic suite").

### 7.1 `tests/unit/test_fetch_dashboards.py` (EXTEND the existing file)

```python
def test_search_dashboards_maps_registry_items_to_candidates(monkeypatch):
    """search_dashboards turns a grafana.com search response into bounded {gnet,title,downloads}
    candidates for the picker — proves the live discovery tier shapes the registry's `items` into the
    pick-list contract. Guards against a registry field-name drift silently emptying the picker."""
    monkeypatch.setattr(fd, "_get", lambda url, timeout=8:
        '{"items":[{"id":1860,"name":"Node Exporter Full","downloads":99,"reviewsCount":3}]}')
    out = fd.search_dashboards("node exporter")
    assert out and out[0]["gnet"] == 1860 and out[0]["title"] == "Node Exporter Full"
    assert out[0]["downloads"] == 99

def test_search_dashboards_degrades_to_empty_on_network_error(monkeypatch):
    """A slow/unreachable grafana.com yields [] (not an exception) — the 'degrade-to-manual-if-none'
    invariant: the external registry can never break onboarding, it just offers no live candidates.
    Mirrors catalog.galaxy_search's best-effort posture (catalog.py:120-122)."""
    def boom(url, timeout=8): raise OSError("registry down")
    monkeypatch.setattr(fd, "_get", boom)
    assert fd.search_dashboards("anything") == []

def test_search_dashboards_caps_results(monkeypatch):
    """search_dashboards never returns more than `limit` candidates — keeps the pick-list and the API
    response bounded regardless of how many the registry returns. Guards a pathological wide query."""
    items = [{"id": i, "name": "b%d" % i, "downloads": i} for i in range(100)]
    monkeypatch.setattr(fd, "_get", lambda url, timeout=8: __import__("json").dumps({"items": items}))
    assert len(fd.search_dashboards("x", limit=5)) == 5

def test_search_dashboards_is_cached(monkeypatch):
    """A repeated (q,datasource,limit) hits the in-process TTL cache, not the registry — proves the
    cache shields grafana.com from a retyping operator. Guards against every keystroke fanning out."""
    calls = []
    monkeypatch.setattr(fd, "_get", lambda url, timeout=8: calls.append(1) or '{"items":[]}')
    fd._SEARCH_CACHE.clear()
    fd.search_dashboards("dup"); fd.search_dashboards("dup")
    assert len(calls) == 1
```

### 7.2 `tests/unit/test_telemetry_dashboards.py` (NEW — the curated tier)

```python
def test_suggested_dashboards_for_returns_method_curation():
    """suggested_dashboards_for(method) returns that telemetry method's curated {gnet,name,title,trusted}
    list — proves the curated tier is sourced FROM the method descriptor (host_node -> 1860, pve -> 10347),
    so any device using the method is offered its board without re-declaring it. Guards the method->board map."""
    methods = [{"name": "host_node", "suggested_dashboards": [{"gnet": 1860, "name": "node", "trusted": True}]}]
    got = catalog.suggested_dashboards_for("host_node", methods=methods)
    assert got and got[0]["gnet"] == 1860 and got[0]["trusted"] is True

def test_suggested_dashboards_for_empty_when_method_declares_none():
    """A method with no suggested_dashboards (or an unknown method) yields [] — the 'degrade_manual'
    source-of-truth: no curation means the picker honestly offers manual search, never a fake board.
    Preserves the metrics-less-is-honest property (cf. test_gen_observability no-block=>no-target)."""
    assert catalog.suggested_dashboards_for("snmp", methods=[{"name": "snmp"}]) == []
    assert catalog.suggested_dashboards_for("nope", methods=[]) == []
```

### 7.3 `tests/integration/test_api_dashboards.py` (NEW — the route)

```python
def test_dashboards_search_returns_candidates(monkeypatch):
    """GET /dashboards/search returns the live candidate list as DashboardCandidate models — proves the
    inquiry route wires the search seam to the typed response (the picker's data source). _get is mocked,
    so CI never hits grafana.com (the hermetic discipline test_fetch_dashboards states)."""
    # patch the loaded fetch-dashboards module's _get via the route's loader, then assert 200 + shape

def test_dashboards_search_needs_no_token(client_no_token):
    """GET /dashboards/search is INQUIRY (no token) — unlike the privileged routes it serves with no
    KONTROLL_API_TOKEN configured. Guards against the discovery tier being mis-tagged privileged (which
    would 503 it on the read-only deployment) and confirms it stays out of the privileged budget."""

def test_dashboards_search_empty_on_registry_failure(monkeypatch):
    """When the search seam returns [] (registry down), the route returns 200 + [] — never a 5xx. Proves
    the external-service safety contract end to end: a flaky grafana.com degrades the picker, not the API."""

def test_dashboards_search_validates_query(client):
    """A too-short/over-long q is a 422 (Query min_length/max_length) BEFORE any network call — guards
    the input bound + keeps a junk query from reaching the registry. Mirrors the request-validation
    convention (api/routes/exceptions.py field_validator)."""
```

A guard to ADD to `tests/integration/test_api_ratelimit.py` (extend `test_every_privileged_route_is_classified`, `:185-186`): assert `ratelimit._classify("/dashboards/search") == "inquiry"` — proves the new route is inquiry-classed and the privileged set assertion (`:182`) is **unchanged**.

### 7.4 `tests/unit/test_onboard_dashboards.py` (NEW — the flow-back, with `tmp_repo`)

```python
def test_plan_injects_dashboards_block(tmp_repo):
    """build_onboard_plan with chosen {gnet,name} dashboards puts a dashboards: block into the module
    dict (title/trusted dropped) — proves a picked board flows into module.yml in the SAME {gnet,name}
    shape fetch-dashboards.py consumes. Guards the picker->module.yml contract (GAP g closure)."""

def test_plan_dashboards_omitted_when_none_chosen(tmp_repo):
    """No chosen dashboards => no dashboards: key in the module dict (not an empty list) — keeps the
    honest 'omit when none' shape the schema documents (modules/README.md:35), so a metrics-only onboard
    stays board-less rather than carrying a fake/empty declaration."""
```

### 7.5 `tests/e2e/test_onboard_flow.py` (EXTEND — the rendered picker)

```python
def test_dashboard_picker_renders_suggested_and_searches(page):
    """Choosing a telemetry method reveals the dashboard picker with the method's curated board
    pre-listed (trusted => pre-checked), and the live search box renders grafana.com candidates by
    data-testid. Guards the hybrid UX render path (suggested-default + manual-search) — selectors are
    data-testid only. The /dashboards/search call is served by the live server's mocked search seam."""
```

This requires the e2e harness (`tests/e2e/conftest.py:66-73`) to mock the new search seam offline — add `fd.search_dashboards = lambda q, **k: [{"gnet": 1860, "title": "Node Exporter Full", "downloads": 9}]` to `_mock_service_seams` (the same throwaway-assignment pattern used for `_catalog.local_shallow` at `:71`), and a canned `suggested_dashboards` on the mocked method.

---

## 8. Logging & audit (run_id-correlated; action, never a credential)

**There is no credential anywhere in this dimension** — dashboard ids and titles are public registry data. The logging obligations are therefore about *correlation* and *honesty*, not redaction.

- **Live-search route (inquiry, unaudited like all inquiry).** `GET /dashboards/search` writes no audit line (consistent with `GET /search`/`/classify`, which don't). A rate-limit breach on it writes the existing best-effort `rate-limited` audit line via the middleware (`api/ratelimit.py:202-220`) — **no new code**, carrying only `path=/dashboards/search class=inquiry` (never a query/token).
- **Onboard-with-dashboards (the apply path that DOES write).** The dashboard choice rides the existing `onboard-apply` audit line. **Extend** the detail string at `api/routes/onboard.py:67-70` to append `dashboards=<count>`:

  ```
  onboard-apply   collection=community.docker key=my-docker-host group=docker_hosts host=192.0.2.20  creds=... push=False dashboards=1
  ```

  A **count, not the contents** — keeps the audit metadata-only and bounded (the boards aren't secret, but the audit line stays a fixed-width summary, matching the existing `creds=<names>` discipline at `:70`). The `kontroll_run_id` is the existing minted id (`api/auth.py:40`), so the dashboard choice is greppable with the onboard it belongs to.
- **GUI shell-out audit.** `gui/app.py:166-169` `_audit("onboard-…")` gains `dashboards=<count>` in its detail string (same metadata-only rule; the GUI line is the 4-field TSV at `gui/app.py:91`).
- **The vendored fetch run (operator/Semaphore/deploy-stack).** When `fetch-dashboards.py` runs, it prints one stdout line per board (`scripts/fetch-dashboards.py:97`: `fetched '<title>' (gnet N rev R) -> dashboards/grafana/dashboards/<name>.json`). Under deploy-stack this is captured by the run-log foundation; when triggered as a Semaphore task it carries the `-e kontroll_run_id=<id>` the onboard minted (the documented stitch at `api/audit.py:18-19`), so the fetch is correlated to the onboard that staged it. **No new logging code** — the existing print + run-log capture suffice.

---

## 9. SECURITY.md — control text + accepted risks

**No new secret, no new credential, no new trust boundary that opens the fleet.** The live-search route is **read-only inquiry over a public registry** — it serves on the existing read-only deployment with no token (it is *not* one of the gated privileged routes). It does, however, add a **new outbound external dependency (`grafana.com`)** and a small SSRF-shaped surface to reason about, so SECURITY.md gets an **amendment under C9** and two accepted-risk rows.

### C9 amendment (add a bullet under the metrics/agent bullets)

> - **Dashboard discovery (`GET /dashboards/search`): read-only, inquiry-class, egress-only, no credential.** The hybrid dashboard picker's live tier queries the **public** Grafana.com community registry (`grafana.com/api/dashboards`, an **undocumented** API with no OpenAPI spec — the contract is vendored in `scripts/fetch-dashboards.py`, never spec-derived). It is **inquiry-tagged** (no Bearer token; serves on the read-only deployment) because it reads a public registry and **writes nothing** — the chosen board's `{gnet, name}` is staged into `module.yml` only by the **existing audited `onboard` apply** path, and the actual download + Grafana provisioning stays **operator-CLI/deploy-stack** (`fetch-dashboards.py` is online/non-hermetic and a bad board is a fatal provisioning event — `dashboards/README.md`). Caller input (`q`, `datasource`) is interpolated **only** into `urlencode`d query params against the **fixed `grafana.com` host** — there is no operator-supplied URL/host, so no SSRF redirect. Short timeout (8 s) + caught errors degrade to an empty pick-list, never a hang or a 5xx; the inquiry rate-limit budget caps fan-out to the registry. Covering checks: `tests/integration/test_api_dashboards.py` (no-token serve, degrade-to-empty, 422 on bad query), `tests/unit/test_fetch_dashboards.py` (search mapping, network-error degrade, cap, cache).

### Accepted-risks rows (add to the table)

| Item | Risk | Accepted? | Rationale |
|---|---|---|---|
| Outbound dependency on grafana.com (dashboard discovery) | The registry could be slow, down, or return junk | Yes | Egress-only, public data, no secret; short timeout + caught errors degrade to a manual-id pick-list; the fetch is vendored (committed JSON), so a registry outage never affects a deployed Grafana — only *new* discovery |
| grafana.com registry API is undocumented | A silent field/endpoint change breaks search | Yes | Same posture `fetch-dashboards.py` already accepts for the download endpoints; covered by a mapping test that fails loudly on field drift; manual gnet-id entry remains a fallback |

### "Update this document when…" — already covered

The existing trigger *"A new privileged surface (UI or API) is added, or its auth/audit/exposure changes"* is the closest; this route is **not** privileged, but it is a new API surface with a new outbound exposure, so the C9 amendment + the egress note land in the same commit. (Loki/multi-datasource pinning is **out of scope** — the route's `datasource` param defaults to and is only wired for `prometheus`; a Loki value would search-filter but not pin a second uid, deferred per `dashboards/README.md:41`.)

---

## 10. Documentation-Sync rows (same commit as the change)

Per the CLAUDE.md Documentation Sync Checklist:

| Changed | Doc to update | Specific edit |
|---|---|---|
| New API route (`/dashboards/search`) | `docs/api-architecture.md` | Add the route to the inquiry-routes list + a §11 note that the dashboard live-search tier is built (was backlog); note it rides the vendored grafana.com contract |
| New `suggested_dashboards` field on the telemetry-method descriptor | `modules/README.md` | In the `module.yml` schema table (`:35`), note the `dashboards:` block can now be **auto-suggested by telemetry method** (curated tier) in addition to hand-listed; add a row/sentence to the new `telemetry/README.md` (owned by the modularity dimension) for `suggested_dashboards` |
| Hybrid discovery / live-search tier | `dashboards/README.md` | Replace the "manual selection + auto fetch" framing: add a "Discovery" subsection describing the curated (by-method) + live (grafana.com search) tiers, the `--search` CLI flag, and that the registry is undocumented/vendored. Add reciprocal "See also" → `api/routes/dashboards.py` |
| New search function + CLI flag in `fetch-dashboards.py` | `scripts/fetch-dashboards.py` docstring | Update the usage line to `[--list] [--search <text>]`; the "rides the same fetch path next" sentence (`:19`) becomes "the live-search pick-list rides this fetch path" |
| New GUI elements | `tests/testid_reference.md` | Add the dashboard-picker rows (§6.3), grep-verified |
| New test files / extended fixtures | `tests/README.md` | Add `test_api_dashboards.py`, `test_telemetry_dashboards.py`, `test_onboard_dashboards.py` to the inventory; add `fetch_dashboards._get` / `fetch_dashboards.search_dashboards` to the mock-seam table (the home-module seam tests patch) |
| New response model | (none — `api/models.py` is self-documenting via `/openapi.json`) | `DashboardCandidate` is the typed contract; the dogfood ingester reads it automatically |
| Behaviour-affecting change | `CHANGELOG.md [Unreleased]` | A `feat(dashboards)` entry: "hybrid dashboard discovery — curated suggestions per telemetry method + a live grafana.com search tier (read-only `GET /dashboards/search`); chosen boards flow into `module.yml dashboards:` via the existing onboard apply, fetched/provisioned by deploy-stack" |
| New control surface | `SECURITY.md` | The C9 amendment + two accepted-risk rows (§9) |
| Doc with peers | reciprocal "See also" | `dashboards/README.md` ↔ `api/routes/dashboards.py`; `modules/README.md` ↔ `telemetry/README.md` |

---

## 11. Acceptance criteria

The implementer is done when **all** of the following hold:

1. **Curated tier.** `telemetry/host_node.yml` and `telemetry/pve.yml` carry a `suggested_dashboards` block reproducing today's live picks (1860 for node, 10347+1860 for pve); `catalog.suggested_dashboards_for("host_node")` returns gnet 1860; an unknown/uncurated method returns `[]`.
2. **Live tier.** `scripts/fetch-dashboards.py --search "node exporter"` prints JSON candidates from grafana.com; the function degrades to `[]` on network error; results are capped and cached. `GET /dashboards/search?q=node%20exporter` returns `list[DashboardCandidate]`, **serves with no token** (inquiry-class), and returns `200 []` (never 5xx) when the registry is unreachable.
3. **Rate-limit/privileged invariants intact.** `ratelimit._classify("/dashboards/search") == "inquiry"`; `test_every_privileged_route_is_classified` still asserts exactly `{"/refresh", "/capture-exceptions", "/onboard", "/audit/log"}` — **unchanged and green**.
4. **Flow-back.** A picked `{gnet, name}` (curated or searched) appears in the onboard plan's `dashboards:` view (dry-run shows it), is written **byte-compatibly** into `module.yml dashboards:` on apply (title/trusted dropped), and a subsequent `python3 scripts/fetch-dashboards.py` fetches it via the **unchanged** `declared_dashboards`/`fetch`/`pin_datasource` path into `dashboards/grafana/dashboards/<name>.json`. No edit to `providers.yml`/`prometheus.yml`.
5. **GUI states.** The four UX states render and are addressable by the `data-testid`s in §6.3: `suggested` (curated list), `manual_search` (live results), `trusted` (pre-checked suggestion), `degrade_manual` (the `dashboard-none` notice when no curation / empty search). The e2e test drives the picker by data-testid only.
6. **No new actuation/trust boundary.** The API never downloads a board, never runs `fetch-dashboards.py`/Ansible/Docker, and the route requires no token; the SECURITY.md C9 amendment + accepted-risk rows land in the same commit.
7. **First-class deliverables in the same commit.** Every new test has a what-and-why docstring (`tests/check-test-docs.py` green); the `onboard-apply` audit detail gains `dashboards=<count>` (count, never contents); all doc-sync rows (§10) are updated; `bash tests/validate.sh` and `pytest -m "not e2e and not slow"` are green; the existing `gen-observability --check` (`validate.sh:72`) is unaffected (this dimension generates no new lockfile artifact — it writes only the human-authored `dashboards:` block, fetched on demand, exactly as today).
8. **Vendored-fetch honesty preserved.** `SEARCH_URL` carries a code comment recording the registry is undocumented/spec-less; the fetch stays vendored (committed JSON); no attempt is made to onboard the dashboard registry via the OpenAPI service root.

**Out of scope (explicitly deferred):** Loki/multi-datasource pinning (GAP h — `pin_datasource` stays single-uid `prometheus`); the GUI-actuated *download+reload* (stays operator-CLI/deploy-stack); the OpenAPI/exportarr service-root convergence for radarr (a *consumer* of this picker, designed in the two-roots dimension); any privileged dashboard-actuation route (none is needed — discovery is inquiry, staging rides the existing onboard apply).

---

**Files touched (summary for the implementer):**
- NEW: `api/routes/dashboards.py`, `tests/unit/test_telemetry_dashboards.py`, `tests/integration/test_api_dashboards.py`, `tests/unit/test_onboard_dashboards.py`
- MODIFIED: `scripts/fetch-dashboards.py` (+`search_dashboards`, `_get` timeout kwarg, TTL cache, `--search`), `scripts/kontroll/catalog.py` (+`suggested_dashboards_for`), `api/models.py` (+`DashboardCandidate`), `api/main.py` (router registration), `api/routes/onboard.py` (`_plan_view` + audit detail), `scripts/kontroll/service/onboard.py` (`dashboards:` injection), `gui/templates/index.html` (picker + JS), `gui/app.py` (forward `dashboards`, audit count), `telemetry/host_node.yml` + `telemetry/pve.yml` + `telemetry/snmp.yml` (`suggested_dashboards`), `tests/unit/test_fetch_dashboards.py` (+4 tests), `tests/e2e/test_onboard_flow.py` + `tests/e2e/conftest.py` (picker e2e + seam mock), `tests/integration/test_api_ratelimit.py` (inquiry-class assertion)
- DOCS: `dashboards/README.md`, `modules/README.md`, `tests/testid_reference.md`, `tests/README.md`, `docs/api-architecture.md`, `CHANGELOG.md`, `SECURITY.md`
