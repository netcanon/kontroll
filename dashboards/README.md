# dashboards — Homepage portal + Grafana (as code)

The two UI surfaces, config-as-code. Homepage is **live** (Phase 4, `:3000`);
Grafana is **deployed** (Phase 5, `${KONTROLL_MGMT_IP}:3002`).

## Architecture
- `instance/dashboards/homepage/` — the portal / single pane (per-instance overlay; template at `instance.example/dashboards/homepage/`). Split by concern: `settings.yaml`
  (layout), `services.yaml` (tiles + widgets), `widgets.yaml` (header strip),
  `bookmarks.yaml` (external links). Adding a tile = a list item under the right
  group in `services.yaml`. The dir is mounted **read-write** (current Homepage
  initializes/loginto it); its generated runtime files are gitignored.
- `grafana/provisioning/` — datasources + dashboard *providers* + **alerting** (auto-loaded).
- `grafana/provisioning/alerting/` — Grafana-managed **alert rules** (`logging-rules.yaml`:
  container restart, backup failure, onboard error, `auth-denied` spike) + a default no-op
  contact point (`contactpoints.yaml`). NO separate Loki ruler — add a rule = a drop-in here.
- `grafana/dashboards/` — one JSON per dashboard, auto-loaded by the provider. Most are
  **community-sourced**: a device class declares Grafana.com dashboard ids in its
  `module.yml` (`dashboards: [{gnet, name}]`) and `scripts/fetch-dashboards.py` fetches +
  pins them here (the dashboard analogue of the generated scrape targets). A hand-authored
  JSON dropped here works too — e.g. `logs.json` (the Loki logs pane, Layer 3); pin every
  panel/target to the Loki datasource `uid: loki` (a uid rename breaks provisioning fatally).
  A fetched file says which board it is (`gnetId`), and the fetch **never writes a download over
  a file that is not that board**: declaring a board under the name of a hand-authored JSON is
  refused at the request, and stops `fetch-dashboards.py` with the file named.
- `derived/` — the **DERIVED dashboard-floor** pins (north-star Rung 4a). A telemetry method
  carries a `derive_dashboard:` SELECTOR (a search query, not an id); `scripts/gen-dashboard-floor.py
  --resolve` derives the grafana.com board — **even its id** — from the parseable registry and pins
  `{id, revision, content_sha256}` into `derived/<method>.lock.yml`, so the board obeys the no-bespoke
  tenet (derive → pin → offline `--check`). The board JSON still lands in `grafana/dashboards/` and is
  fetched by `fetch-dashboards.py` like any other. See [derived/README.md](derived/README.md).

## Adding an X (the "add an X" test)
- Homepage tile → add to `instance/dashboards/homepage/services.yaml`; widget tokens are `homepage_var_<name>`
  keys of the `dashboards` SOPS domain, rendered by deploy-stack into `docker/.env.homepage` (Homepage's OWN env —
  it never sees the stack `.env`) as `HOMEPAGE_VAR_<NAME>`.
- Grafana dashboard → declare its Grafana.com id in the device's `module.yml`
  (`dashboards:` — a positive integer id and a `name` of `[a-z0-9_-]`, which is the file name; one name, one id)
  + run `scripts/fetch-dashboards.py` (or drop a hand-authored JSON).
- Derived floor board → add a `derive_dashboard:` selector to the telemetry method
  (`telemetry/<method>.yml`) + run `scripts/gen-dashboard-floor.py --resolve` (derives the id,
  pins the lock + board). A `derived: true` class then auto-carries it — see
  [derived/README.md](derived/README.md) + [../modules/README.md](../modules/README.md).

## Errors / behaviour
| Condition | Behaviour |
|---|---|
| Missing widget token | Homepage renders the tile without live data |
| Bad dashboard JSON | Grafana logs and skips it; others load |
| Changed a datasource `uid` on an existing Grafana | provisioning fails fatally ("data source not found") — recreate the `grafana-data` volume (state is all provisioned, nothing lost). A first-time deploy is unaffected. |

## Testing
YAML is linted by `tests/validate` (yamllint). Live rendering is verified
post-deploy (L3), not in CI.

## See also
- [../PLAN.md](../PLAN.md) §8 (dashboard design)
- [../prometheus/README.md](../prometheus/README.md) — Grafana's datasource
- [../modules/README.md](../modules/README.md) — the `dashboards:` block that sources these
- [../scripts/fetch-dashboards.py](../scripts/fetch-dashboards.py) — the Grafana.com fetcher
- [derived/README.md](derived/README.md) — the DERIVED dashboard-floor tier (Rung 4a: `derive_dashboard:` selector → pinned lock)
- [../scripts/gen-dashboard-floor.py](../scripts/gen-dashboard-floor.py) — the derive/check engine (`--resolve` network / `--check` offline)
- A Loki datasource (`provisioning/datasources/loki.yml`) drops in here for the 3a logging layer.
- [../docs/observability-onboarding-flow.md](../docs/observability-onboarding-flow.md) — Option-A design: the hybrid dashboard-discovery UX (curated-by-method + live grafana.com search) that extends this fetch path **(design only)**
