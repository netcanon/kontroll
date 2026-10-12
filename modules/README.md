# Modules — the device-class registry

This directory is the **source of truth for what hardware kontroll can manage**.
It extends the modularity doctrine (PLAN.md §2.5) from *content*-modular ("drop a
role file") to *process*-modular ("pick your fleet, the app configures itself").

## The contract

A **module** is one device class, described as data in `modules/<key>/module.yml`.
A **fleet profile** (`instance/fleet.yml`) lists the modules you actually run. The
**bootstrap** (`ansible/playbooks/bootstrap.yml`) reads the profile and:

1. merges `_core.yml` collections with every enabled module's collections (every collection the install
   playbooks themselves import is listed in `_core.yml`, so they resolve on any fleet — which is why
   `community.docker` is there although `docker_host` declares it too: a class describes itself, and the resolver
   de-duplicates),
2. generates `ansible/collections/requirements.generated.yml` and installs **only
   that set** (← the pick-and-choose: disabled modules cost nothing),
3. installs the toolchain, sets up the age key, and reports which secrets domains
   the enabled modules require.

So a different person with different gear edits **one file** (`instance/fleet.yml`)
and runs **one command** (`ansible-playbook playbooks/bootstrap.yml`). They never touch a hardcoded list.

## `module.yml` schema

| Field | Meaning |
|---|---|
| `key` | unique id; must equal the directory name and the name used in `instance/fleet.yml`. One path component of `[a-z0-9_-]` that YAML reads as a string: not a keyword (`null`, `true`, `on`, `no`), not a number in any YAML spelling (all digits, digits with underscores, `0x…`), not shaped like a date — the key stands unquoted in the fleet list. The same holds for `inventory_group`, `secrets_domain` and `backend` at onboard. |
| `description` | human label |
| `status` | `active` (default-runnable now) or `staged` (available, enable when ready — e.g. post-cutover) |
| `collections` | list of `{name, version}` Ansible collections this class needs (`[]` if raw SSH) |
| `role` | the `ansible/roles/<role>` that implements it. A name like `key` |
| `inventory_group` | the functional group in `instance/inventory/hosts.yml` it feeds. A name like `key` (one path component YAML reads as a string), and not one Ansible reserves: `all`, `ungrouped`, `localhost` — a class's group is a play's `hosts:` pattern |
| `secrets_domain` | which `instance/secrets/<domain>.sops.yml` holds its credentials. A name like `key`, as `backend` is. An onboard of a second device ADOPTS the group, the domain, the backend and the role from this file and refuses (409, naming the key and the rule) a value that is not a name |
| `vendor_defaults` *(opt)* | PUBLIC device-class facts captured ONCE so no consumer hardcodes them (no-bespoke-config tenet) — currently `{tls_posture: self_signed\|ca_signed}`, the connection trust posture BOTH the logging pull (`logging/<m>.yml` `tls_from_class`) AND the telemetry exporter derive from via `scripts/kontroll/endpoints.py`. A vendor change is a one-line edit here (a reviewable diff), not a hunt through descriptors. The operator's CA-pin DECISION is instance data (`instance.yml` `device_trust`), NOT a vendor fact. |
| `metrics` *(opt)* | observability: a **list** of scrape sources, each referencing a **telemetry method by name** (`- {method: host_node}` / `- {method: pve}`). `scripts/gen-observability.py` dispatches over the telemetry registry ([`telemetry/<name>.yml`](../telemetry/README.md)) and fans each over the class's inventory hosts to **generate** `prometheus/targets/<job>/<key>.generated.yml`. The method supplies the job/port/target-shape/labels (`host_node` = host-agent `node_exporter` `:9100`; `pve` = a control-node proxy-exporter querying the device). **Omit** if the class has no exporter (SNMP-only) — no fake targets. |
| `homepage` *(opt)* | portal-tile customisation `{scheme, port, icon}` for this class's Fleet tiles. `scripts/gen-homepage.py` fans the inventory hosts in this class's group into the generated **Fleet** group of `instance/dashboards/homepage/services.yaml` (one `href:`+`description:` tile per onboarded host — never a secret, C11). **Omit** and a tile derives `https://<mgmt-addr>` with no icon — the block only OVERRIDES the scheme/port/icon. |
| `widget` *(opt)* | RESERVED — a live Homepage *widget* for this class (`{type, secret}`, secret-bearing, `{{HOMEPAGE_VAR_*}}`); distinct from the secret-free `homepage:` tile above. Not yet generated. |
| `dashboards` *(opt)* | Grafana.com dashboards (`[{gnet, name}]`: `gnet` a positive integer, `name` one path component of `[a-z0-9_-]` — it is the file name — and one name never with two ids; `fetch-dashboards.py` refuses to run otherwise) — `scripts/fetch-dashboards.py` fetches each by id from grafana.com/grafana/dashboards, pins the Prometheus datasource, and provisions it. No hand-authored JSON. Entries are either **curated** (hand-listed, like proxmox's `{10347}`) or a **DERIVED floor** (`derived: true`, auto-emitted from a telemetry method's `derive_dashboard:` selector — the id itself derived; Rung 4a, F2 below). |
| `provisioning` *(opt)* | credential-provisioning PREREQUISITES — a **list** of `{grant, note, docs_url?}` records naming the manual step(s) beyond "IP + creds" a blind operator must do (e.g. proxmox → the API token must carry the `PVEAuditor` role). **Surfaced at onboarding** (`scripts/kontroll/service/onboard.py` reads it via [`catalog.module_provisioning(collection)`](../scripts/kontroll/catalog.py)), so it's never buried in a doc (the blind-joe constraint), and strictly **non-gating** (the operator can still onboard). `grant` is the structured role NAME — the **same datum** the online-validate seam's token-scope check reads ([prevent + detect](../scripts/gen-validate-live.py)); `note` is rendered verbatim. NAMES only, never a credential. |
| `auth` *(opt)* | self-describing CREDENTIAL SHAPE (F1 seam S1) — a **list** of `CredField` templates declaring the EXACT credential fields the onboard form should show for this class, OVERRIDING the generic per-backend shape on the reuse path ([`authspec.derive_auth(…, module=…)`](../scripts/kontroll/authspec.py)). Each entry: `{field, kind, label, sops_stem?, required?, secret?, auth_set?, shared?, maps_to?, inline?, help?}`. `kind` ∈ `{ssh_key, password, token, host, identity, port, bool, ca_cert}` (drives the widget + secret routing). Fields sharing an **`auth_set`** are ONE coherent credential (the planner refuses a partially-filled required set; the GUI groups them in an `auth-set-<name>` fieldset). **`shared: true`** = a DOMAIN-level secret (one per SOPS domain, not per host) stored FLAT under `sops_stem` — so e.g. proxmox's `{api_user, api_token_id, api_token_secret}` land as `proxmox_api_user`/… exactly the names [`telemetry/pve.yml`](../telemetry/README.md)'s `secret_env_map` reads, feeding the exporter (the empty-Grafana fix). **Omit** and the form derives from the backend's `auth:` block, then the generic union (never blocked, INVARIANT D*). Pure schema — NAMES only, no values; the MF-S2 domain guard still applies. A `sops_stem` is an identifier (it is formatted into the host var's lookup expression), and an `inline` field's value is a bool, an int or one plain word — the planner refuses anything else (SECURITY.md C22). |

## Adding support for new hardware (the "add a module" test)

1. `mkdir modules/<key>/` and write `module.yml` (copy an existing one).
2. Add `ansible/roles/<role>/` (copy `roles/.role-template/`).
3. Reference `<key>` in `instance/fleet.yml`.
4. `ansible-playbook playbooks/bootstrap.yml` — its collection installs, its role activates.

You edit **no hub file** — not `requirements.yml` (it's generated), not a central
switch statement. That is the actuateable-modularity guarantee.

Declaring a `metrics:` block in step 1 is all it takes for the device's **scrape target to be generated** —
`scripts/gen-observability.py` (run by `deploy-stack`) emits `prometheus/targets/<job>/<key>.generated.yml` from
the module + inventory. The device's **portal Fleet tile** is likewise generated — `scripts/gen-homepage.py` (also
run by `deploy-stack`) fans the class's inventory hosts (plus the optional `homepage:` block) into the **Fleet**
group of `instance/dashboards/homepage/services.yaml`. No per-device target file, portal tile, or dashboard is
hand-written: the same "declare the class, the app configures itself" guarantee. The collection list
(`gen-requirements.py`), the scrape targets (`gen-observability.py`), and the portal tiles (`gen-homepage.py`) are
all generated from this one self-describing `module.yml` + the inventory.

Every generator whose output is committed has the same three modes. The committed file is the public,
example-derived reference: `--check` validates it against `instance.example/` and `--example` regenerates it from
there, on any checkout. The bare run reads the instance overlay — it is what `deploy-stack` and the capability
regen call on a node, and it writes that node's real file. (This holds for `gen-observability`, `gen-logging`,
`gen-homepage`, `gen-backup`, `gen-prometheus-jobs`, `gen-exporters` and `gen-secret-env`.
`gen-class-capabilities --check` reads the example fleet as well; it only checks. The collection lockfile is
per-node and gitignored, so `gen-requirements` has no example mode.)

### Self-describing capability blocks

Each capability is a drop-in `module.yml` block, turned into actuation config by a generator (none hand-written):

| Block | Shape | Generator → artifact | Capability |
|---|---|---|---|
| `metrics:` | list (`- {method, params}`) | `gen-observability.py` → `prometheus/targets/<job>/<key>.generated.yml` | telemetry (instance #1) |
| `dashboards:` | list (`- {gnet, name}`) | `fetch-dashboards.py` → provisioned Grafana dashboards | telemetry |
| `backup:` | mapping (`{capable, schedule, retention?, destination?}`) | `gen-backup.py` → `config/semaphore/schedules.generated.yml` | backup (instance #2) |
| `logs:` | list (`- {method, params}`) | `gen-logging.py` → `docker/vector/generated/<method>_<key>.generated.yaml` | logging (instance #3) |
| `homepage:` *(opt)* | mapping (`{scheme, port, icon}`) — tiles generate from the inventory regardless; the block only overrides | `gen-homepage.py` → the `# >>> kontroll fleet` span in `instance/dashboards/homepage/services.yaml` | portal Fleet tiles |
| `discovery:` *(opt)* | list (`- {method}`) — the class opts into being read for its OWN lease/neighbour view read-only (declared, not facts-derived) | `scripts/kontroll-discover.py` → the git-ignored `local/discovery-inbox.generated.json` (read by `service/discovery.read_inbox`) | discovery inbox (Rung 1a) — see [discovery/README.md](../discovery/README.md), SECURITY C18 |

A `logs:` block declares how the class's logs reach Loki via Vector — each entry references a `logging/<method>.yml`
(e.g. `syslog_push` / `journald_remote` / `rest_pull` / `file_tail_ssh` / `proxmox_api`); `gen-logging.py` fans it into a Vector source
+ shaping transform drop-in (the canonical non-secret label set `source/host/service/level/run_id/device`, C12).
Adding a class's log source is a `logs:` drop-in + a re-gen — never a hub edit. See
[capabilities/logging.yml](../capabilities/logging.yml) + [logging/README.md](../logging/README.md).

A `backup:` block declares whether the class is backup-capable (mirrors `roles/<role>/defaults/main.yml
backup_capable`), its cron `schedule`, and the optional knobs `retention` (`keep-all`|`30d`|`90d`|`365d` — how
much capture history to keep; carried to the run as `kontroll_backup_retention` and ENFORCED by the
retention-prune play in `backup-configs.yml` — #125; `keep-all` = no prune)
and `destination` (`local`; `offsite` is offered-but-refused, SECURITY.md C8). `gen-backup.py` turns it into a
per-class Semaphore schedule (`--limit <inventory_group>`), which `configure-semaphore.py` registers.
Adding/retiming a class's backup is a `backup:` drop-in + a re-gen — never an edit to `configure-semaphore.py`. The `metrics:`/`backup:` blocks are
each added through the standalone [secondary-capability dialog](../docs/observability/secondary-capability-dialog.md)
(or by hand); they are **secondary** capabilities — neither gates onboarding (INVARIANT D\*).

### Auto-derived capability floor (F2 de-bespoke)

A blind onboard does **not** leave `metrics:`/`logs:` empty: it AUTO-DERIVES the universal agent-less floor (snmp +
blackbox metrics, syslog logs) from the classified backend's `confers.telemetry`/`confers.logs`
(`ansible/backends/<name>/backend.yml`) — so an arbitrary device is monitored + log-shippable with zero curation.
The derivation (`classify.derive_class_capabilities`, PURE/offline) writes those entries marked **`derived: true`**
and flags the class `derived: true` at the top level. A class with a derived floor commits a small
**`facts.pinned.yml`** (the offline classify-signal facts: plugins + module names) so the floor is re-derivable
hermetically: `scripts/gen-class-capabilities.py --check` (a `tests/validate` step) re-derives from the pinned facts
+ the registries and fails CI if the module's `derived: true` entries drift — the no-bespoke teeth for the floor.
A method opts into auto-derivation by publishing a `derive_default` (its universal param) on its
`telemetry/<m>.yml` / `logging/<m>.yml`; a method with a **per-class secret** never opts in (MF-S5 — the floor is
creds-free by construction). The vendor-specific richness a blind class can't derive (a `pve` exporter, a
credentialed method) stays operator-declared as **bare** (non-`derived`) entries — the curated override tail — and
is named in the onboard plan's `capability_gap` (advisory, never gates). The **`dashboards:` block is a DERIVED
floor too** (north-star Rung 4a): a derived telemetry method carrying a `derive_dashboard:` selector auto-carries the
grafana.com board its floor resolved to — a `{gnet, name}` marked `derived: true`, owned by `gen-class-capabilities
--check` from the `dashboards/derived/<method>.lock.yml` pins (`scripts/gen-dashboard-floor.py --resolve` derives even
the id from a search — never a typed `gnet`). A blind onboard writes the metrics/logs floor but **NOT** a `dashboards:`
block (a board rides the out-of-band-resolved locks — INVARIANT-D); the operator re-derives it (the honest gap names
it). See [../dashboards/derived/README.md](../dashboards/derived/README.md). The curated classes migrate to a derived
floor under a **byte-identical** gate — their floor is `derived: true` but the generated targets/drop-ins are
unchanged (the generators read `method`/`params` only, ignoring the marker). Two proof shapes: `cisco_ios` (cliconf →
`netcommon_cli`) derives its **full** floor (snmp + blackbox + syslog); `fortigate` (httpapi → `api`) is the
**residual-isolation** proof — only `blackbox` derives, and `snmp` stays a **bare** curated override because an
httpapi device's plugin shape can't confer SNMP (never faked onto it). A **staged** class (one commented out of the
fleet, e.g. `routeros`/`opnsense`) can carry a `derived: true` floor too, but since `gen-class-capabilities --check`
only visits the ENABLED example fleet, its floor is gated by a **frozen-oracle unit test** (`test_derive_class_
capabilities.py`) until it is enabled at cutover — the same drift protection, per-class.

## Catalog (shipped modules)

| key | status | collection | role | group |
|---|---|---|---|---|
| `proxmox` | active | community.proxmox | proxmox | hypervisors |
| `docker_host` | active | community.docker | docker_host | docker_hosts |
| `fortigate` | active | fortinet.fortios | fortigate | edge_firewall |
| `cisco_ios` | active | cisco.ios | cisco_ios | core_switch |
| `openwrt` | active | — (raw SSH) | openwrt | wireless_ap |
| `opnsense` | staged | ansibleguy.opnsense | opnsense | edge_firewall |
| `routeros` | staged | community.routeros | routeros | core_switch |
| `ios_xe_auto` | staged | cisco.ios | backend_netcommon_cli | core_switch |

> `ios_xe_auto` is **machine-scaffolded** (`galaxy.py scaffold`) and uses the generic
> `backend_netcommon_cli` backend instead of a hand-written role — kept as the
> non-human onboarding proof (see `tests/e2e/`).

## Backup capability

Whether a config backup is **meaningful** for a class is a per-class trait, not a
universal assumption. Each role declares it as a default —
`ansible/roles/<role>/defaults/main.yml` → `backup_capable: true|false` — which is
the single source of truth (runtime-available, overridable per host/group via
Ansible var precedence). `backup-configs.yml` runs the capture for capable
classes and records a **logged skip** for the rest (never a forced empty file);
its final play prints a per-host `captured / skipped / not-run` summary.

| class | `backup_capable` | capture |
|---|---|---|
| `cisco_ios` | true | `show running-config` |
| `fortigate` | true | FortiOS backup API → full-configuration |
| `openwrt` | true | `uci export` (raw SSH) |
| `proxmox` | true | guest `.conf` + node network/storage (SSH; config, not vzdump) |
| `docker_host` | **false** (default) | none — app-managed state; opt in per host for a manifest |
| `opnsense` | true (staged) | `/conf/config.xml` — written, unverified until cutover |
| `routeros` | true (staged) | `/export show-sensitive` — written, unverified until cutover |

To make a managed-but-not-backed-up host explicit, or to enable a capture for a
normally-skipped one, set `backup_capable` in `host_vars/<host>.yml`.

## See also
- [../instance/fleet.yml](../instance/fleet.yml) — the enabled-modules knob
- [../actuation/README.md](../actuation/README.md) — the **app-store unit registry** (the 3rd registry sibling): an
  active unit's pin ALSO unions into `requirements.generated.yml`, so the lockfile now derives from enabled modules
  **and active actuation units** (CLAUDE.md hard rule)
- [../docs/capability-matrix.md](../docs/capability-matrix.md) — design: programmatic
  Galaxy+local capability search + backends that feed this registry
- [../docs/SETUP.md](../docs/SETUP.md) — first-run + continual config
- [../PLAN.md](../PLAN.md) §2.5 — the modularity doctrine these modules implement
- [../instance/secrets/README.md](../instance/secrets/README.md) — secret domains
- [../docs/observability-onboarding-flow.md](../docs/observability-onboarding-flow.md) — Option-A design: the self-describing telemetry-method registry + the generalized secondary-capability seam (**built** — Capability-track Phases 1–8; `metrics:` = instance #1, `backup:` = instance #2)
