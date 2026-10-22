# Design: capability-matrix onboarding (Galaxy + local, programmatically derived)

> **DESIGN HISTORY — not a description of the tree.** This is the design record of capability-matrix onboarding (the search, the classifier, the backends), written from May 2026. It was built — the backends, the onboard orchestrator and the GUI included; [scripts/README.md](../scripts/README.md) (`galaxy.py`) and [modules/README.md](../modules/README.md) describe what exists. Where it disagrees with the code or with `SECURITY.md`, they are right.

> This doc is the contract for *how* it stays
> extensible — the feature that must not be gotten wrong. Numbers/labels are
> illustrative; the system derives them.

## 1. Purpose

Turn "add a device/server/service" from *"know the right collection, hand-write
two task files, wire it up"* into *"search, see what it can do, click add, verify."*
Anything Ansible can talk to should be a candidate, surfaced through one search
bar that spans **Ansible Galaxy** and **what we already have locally**, each result
labelled by what it can actually *do*.

The non-negotiable design goal is **extensibility**: adding a new capability
vector, a new execution backend, or a new device must be a drop-in, never an edit
to a hub file. Same doctrine as `modules/` (PLAN.md §2.5), extended to onboarding.

## 2. Capability vectors (an extensible set, not a fixed three)

Every result — Galaxy or local — is labelled along a set of **capability vectors**.
The launch set:

| vector | meaning | primary machine signal |
|---|---|---|
| `actuate` (R/W) | can change device state | config/resource modules; modules with a `state:` param; `cli_config`/cliconf plugin |
| `backup` | can capture config read-only | a `*_config` module exposing `backup:` (machine-readable); cliconf "get"; NAPALM `get_config`; `*_facts` |
| `bespoke` | needs local/custom logic (no standard backend fits) | residual: collection is empty (raw SSH) **or** matched no backend → flagged |
| `telemetry` (scrape) | looks scrapable for metrics (a proxy/API exporter could pull it) | `httpapi` (medium); `cliconf`/`netconf` (low); `*_facts` (low) — a SUGGESTER from collection signals, **never** a host-agent detector |

Vectors are themselves a **registry** (`vectors/<name>.yml`): each defines its label,
its probe signal(s), and how confidence is scored. `telemetry` (the scrape-suitability
suggester) is the **shipped 4th vector**; adding a 5th (e.g. `secrets-broker`) = drop a
vector file + its signal. The search UI renders whatever vectors exist; nothing hard-codes a count.

## 3. Feasibility: can the matrix be generated programmatically? **Mostly yes.**

This is the question that decides whether you need a separate AI-maintained repo.
Evidence (verified against the live runner):

- Module **naming conventions** are enforced by Ansible's content guidelines, so
  `*_config` / `*_facts` / `*_command` are reliable signals. `cisco.ios` exposes
  exactly that triple.
- `ansible-doc -j <module>` returns options as **structured JSON** — e.g.
  `cisco.ios.ios_config` advertises `backup: true` and `backup_options`. So
  "can this back up?" is *detected*, not read by a human.
- **Plugin enumeration** (`ansible-doc -t cliconf/httpapi/netconf -l`) reveals the
  connection model and whether the generic `ansible.netcommon.cli_command` /
  `cli_config` backbone applies.
- Galaxy's content API exposes a collection version's **`contents`** (module/plugin
  list) *without installing* — enough for a fast preliminary label.

**Derivability by vector:**

| vector | programmatic confidence | the residual (needs annotation) |
|---|---|---|
| `actuate` | high | rare — collections that read but genuinely can't write |
| `backup` | high for network_cli / resource-module / NAPALM | whether backup is *meaningful* (the `backup_capable` judgment, e.g. docker hosts) |
| `bespoke` | high (it's the negative space) | the actual capture command for a non-collection device (e.g. OpenWrt `uci export`) |

So ~most of the matrix is machine-derived **with a confidence score**; a thin
residual needs human/AI annotation. That residual lives in a **drop-in override
file in *this* repo** (`overrides/*.yml`), optionally maintained by an AI agent via
the existing agent-workflow — **not** a separate service.

### 3.1 Decision: programmatic + local overrides (skip the external repo)

The "AI-maintained capability-matrix repo the stack calls out to" is real work and
real ops surface, and for a personal fleet it buys little: the override file *is*
your annotation layer, and an agent can maintain it in-repo. Reserve the external
repo for one future scenario only — **publishing a shared/community matrix** beyond
your own fleet. Until then: derive locally, annotate locally, no network dependency,
no extra repo to babysit. This is the recommended path and the one this doc designs.

## 4. Architecture

```
search bar ──► search index ──► merge(local, galaxy) ──► faceted results
                   │                 │        │
                   │            local registry  Galaxy v3 API
                   │            (already probed) (shallow-probe contents)
                   ▼
           PROBER ──► capability matrix (derived cache) + overrides/ (drop-in)
                   │
   on "add": DEEP probe ──► classifier ──► BACKEND ──► scaffold module decl
                                              │
                                       enable in fleet ─► bootstrap ─► (you run ping/backup once)
```

Components:

- **Prober** — given a collection (local-installed *or* Galaxy metadata), emit
  vector labels + confidence. Two depths: **shallow** (from the installed collection's
  files *or* Galaxy `contents`/names — no `ansible-doc`, for fast search breadth) and
  **deep** (`ansible-doc -j`, on `--deep`/selection). Search is shallow by default; the
  deep-only `*_config backup:` signal reads `?` until deep-probed (shallow ⊑ deep).
- **Capability matrix** — a *derived* cache (`capability-matrix.generated.*`,
  gitignored like the requirements lockfile) recomputed by the prober, layered with
  hand/AI **`overrides/`** (drop-in, committed) for the residual + corrections.
- **Backends** — the generic execution implementations the labels map to (§5).
- **Unified search** — one query fans out to local + Galaxy, merges, dedupes
  (local-installed that also exists on Galaxy → shown `local`, "update available"),
  labels each result by **origin** and **vectors**, and supports **facet filters**
  (`backup:yes`, `actuate:rw`, `origin:galaxy`, certified-only, …).
- **Onboard pipeline** — select → deep-probe → classifier picks a backend →
  scaffold the module declaration (zero task code for standard backends; a marked
  stub for bespoke) → enable in `instance/fleet.yml` → bootstrap; then the operator runs ping/backup
  against the host once (§6).

### 4.1 The onboarding flow (the operator's path, end to end)

The target experience: *"Joe searches `cisco ios`, sees he can back it up, supplies
credentials, and ends up with a backup — then can schedule it and put it in groups."*
Each step maps to a built piece; the gap is one orchestrator that chains them.

| operator step | mechanism |
|---|---|
| search "cisco ios" | `galaxy.py search` (Galaxy + local, one bar) |
| sees it can back up | the `backup:✓` label + `→<backend>` suggestion on the record |
| **clicks "add"** | a GUI calling the **`onboard` orchestrator** (below) — the only gap |
| supplies credentials | a cred form → SOPS domain (or Semaphore Key Store) |
| gets a backup | `scaffold` + the generic backend run it (proven in `tests/e2e/`) |
| schedules / groups it | **Semaphore** (templates, schedules, inventory groups) — already built |

**The `onboard` orchestrator** is the missing composition — every step already works
alone:

```
onboard(collection, host, creds, group):
  1. scaffold the module declaration            (galaxy.py scaffold — done)
  2. register the host in instance/inventory/     (under the group)
  3. store creds in the right SOPS domain        (proven pattern)
  4. commit + push + run bootstrap               (installs the collection;
                                                   Semaphore re-clones)
  5. → the host is now in ping/backup-configs AND the nightly schedule;
       Semaphore runs/schedules it per-device or by group
```

A **GUI is the last layer** — a thin presentation over `search` + `onboard` (the
operator never sees YAML). Build the orchestrator first; render it later. This is
also where "execution environment" resolves: the chosen **backend** is the
execution *method*, and the Semaphore runner image is the execution *environment*
(collections + `sops` present) — onboarding ensures both fit the device.

## 5. Backends — the extensibility crux

Today: one role per vendor with hand-written `check`/`backup`. The leverage is to
generalize into a few **capability backends**. A backend is two drop-in parts (split
so the classifier metadata stays plain data and the tasks lint as a real role):

```
ansible/backends/<name>/backend.yml      # classifier metadata (classify predicate,
                                         #   requires, confers) — read by galaxy.py
ansible/roles/backend_<name>/tasks/      # generic check.yml / backup.yml (execution)
```

`backend.yml` is data the classifier evaluates against probe output (same predicate
engine as `vectors/*.yml`):

```yaml
name: netcommon_cli
classify:
  any_of:
    - {plugin: cliconf}
confers: {actuate: true, backup: true}
requires: [network_os]           # supplied per-device in module.yml
role: backend_netcommon_cli      # the execution role: what a class's device_role names
```

**Adding a backend = drop the dir + role.** The classifier auto-discovers it. The
entry's `role:` names the role; an entry without one may still classify (a search
record lists it among its candidates) but is never the source of a role — a new class
is never planned with it (the onboard planner and the scaffold choose only a backend
that declares a role, and refuse a request that names any other); only a reused class
that brings its own role can be (S26). A new
Arista/Juniper device then needs **zero new task code** — `modules/<key>/module.yml`
declares `role: backend_netcommon_cli` + `network_os`. `cisco_ios` is already
migrated to this shape (live e2e: byte-identical capture), so it's generalization,
not a rewrite.

### 5.1 How many backends? ~a handful — bounded by execution *paradigm*, not vendor

Fleet diversity is absorbed by **data** (per-device params/recipes), not new code:

| backend | covers | new-device lift | runs today? |
|---|---|---|---|
| `netcommon_cli` ✅ | **all** CLI network gear (Cisco/Arista/Juniper/MikroTik…) | one declaration line | yes |
| `raw_ssh` | bespoke SSH appliances + plain Linux | one line + a command | yes |
| `api` ✅ | **all** REST devices *and* services | one line + a recipe (§5.2) | yes |
| `vendor_config` | gear with a vendor `*_config` module that takes `backup` | one line | classifier only — never the source of a role |
| `module_sdk` | SDK/socket modules (community.docker/proxmox, kubernetes.core, cloud) | one line | not built |
| `netconf` | NETCONF devices | 0–1 (only if present) | not built |
| `napalm` | optional cross-vendor network | 0–1 (manual/driver) | classifier only — never the source of a role |

"Runs today?" is the registry's answer: **yes** — the backend's `backend.yml` declares a `role:`;
**classifier only** — it ships a `backend.yml` with no role, so a search record may list it among its
candidates (not `napalm`, which is manual) but the onboard planner never plans a new class with it (S26);
**not built** — no
`ansible/backends/<name>/` yet.
`tests/unit/test_backend_registry.py` holds this column to the registry.

You will **not** need a backend per vendor — five or six cover essentially anything
Ansible can talk to. New backends are a rare, one-time, reusable cost.

### 5.2 The API tail — one backend + OpenAPI-derived recipes

REST is the bulk of a service/device fleet and the one paradigm with **no universal
abstraction** (unlike cliconf). The answer is *not* a backend per vendor but **one
parameterized `api` backend driven by a per-API recipe** (data, ~10 lines):

```yaml
api recipe:
  base_url, auth: {type: bearer|basic|apikey_header, ...}
  capabilities:
    backup:  {method: GET, path: /api/.../config/backup, extract: body}
    actuate: {...}
```

Recipes are **derived, not hand-written**, from machine-readable API specs — the
same prober→classify→overrides pattern, applied to OpenAPI. Enrichment sources, in
order of usefulness for a self-hosted fleet:

1. **Live-instance OpenAPI** — many services publish their full spec at runtime
   (`/openapi.json`, `/api/v3/openapi.json`, `/swagger.json`; e.g. the Servarr apps).
   The spec comes from the *actual running thing* — best source, no registry needed.
2. **`apis.guru`** — a directory of **~2,500 machine-readable OpenAPI specs**
   (cisco, docker, gitlab, kubernetes, netbox, …) for mainstream APIs.
3. **Vendor schemas** — FortiGate (FNDN), Proxmox (`/pve-docs/api-viewer`), fetched
   per-vendor for niche gear not in a central index.

OpenAPI is to APIs what Galaxy is to collections: the machine-readable source you
classify (endpoint named `/backup`|`/export`|`/config` + `GET` → likely `backup:✓`)
and turn into a recipe. **Honest limits:** central registries cover public APIs, not
niche homelab gear (FortiGate/Proxmox/Servarr aren't in apis.guru — fetch their
specs from the device); and endpoint→capability mapping needs light heuristics +
the `overrides/` tail. Net: **API management becomes per-API data, mostly
auto-derived, with a thin curated remainder** — exactly the existing model.

This is the answer to "can the per-device lift be programmatic?": for everything
that fits a backend, **yes — one declaration line (+ a derived recipe for APIs) +
verification.** Only genuinely novel paradigms need a new backend.

## 6. Confidence, and what is *not* verified

Each label carries a `confidence` (`high`/`medium`/`low`, from the vector rule that
matched; the probe behind it was `shallow` or `deep`) and a provenance (`derived` /
`override`). **No step of the tool checks a newly
onboarded class against the device:** a class runs the moment `instance/fleet.yml`
lists it, and the onboard enables it in the same change. The check is the
operator's: `ansible/playbooks/ping.yml --limit <host>` (or the `ping-fleet`
Semaphore template) and the first backup run, after the promote. Generation gets
you 70–95%; that first run is where the last mile shows.

## 7. What stays human/AI (honest boundary)

- **Novel devices** outside any backend → write a new backend (reusable thereafter).
- **The `backup_capable` *meaning* judgment** (technically-possible ≠ worth doing).
- **Trust/quality go-no-go** on a collection (Galaxy gives signals — downloads,
  certified, last-updated — but the call is yours).
- **The operator's live check** (§6). None of these are things you'd *want* fully automated.

## 8. Build phases

1. **Prober + matrix (read-only).** ✅ *done* — `galaxy.py probe`/`refresh` +
   `vectors/*.yml`; deep (`ansible-doc -j`) + shallow (Galaxy `contents`) labels.
2. **Unified search.** ✅ *done* — `galaxy.py search` spans installed + Galaxy,
   structured records, origin/depth + vector labels, `--vector` facets, `--json`;
   plus `audit` (declared vs installed). *Nice-to-have remaining:* `modules/` as an
   explicit local source; "update available" dedupe.
3. **Classifier + scaffold.** ✅ *done* — `galaxy.py classify` (probe → backend) +
   `scaffold` (emit `modules/<key>/module.yml`, staged, derives params).
4. **Backends — execution.** ✅ *core done* — `netcommon_cli` + `raw_ssh` executable
   + live-verified (`cisco_ios` + `openwrt` migrated; full-fleet backup green, captures
   byte-identical; non-human e2e). *Remaining:* `module_sdk` backend; the `api` tail (§6).
5. **Overrides.** ✅ *built* (`overrides/*.yml`; corrects FortiGate API backup).
   Agent-workflow upkeep is process. (External shared-matrix repo: deferred, §3.1.)
6. **The `api` backend** (§5.2) — ✅ *executable* — `roles/backend_api` does generic
   REST `check`/`backup` via plain `uri` (no module/collection), driven by per-API
   recipes (`backends/api/recipes/<name>.yml`: auth + endpoints, secret-free). FortiGate
   migrated onto it (`fortios` recipe); verified **byte-identical** full-configuration
   capture (differs only in FortiOS's per-export re-encrypted secret blobs). Folds
   FortiGate-likes + services into data. The **OpenAPI ingester** is also done —
   `galaxy.py openapi <url|file> [--emit]` fetches a spec (live `/openapi.json` /
   apis.guru / vendor), reads `securitySchemes`→auth + `servers`→port, classifies
   endpoints by heuristic (GET `/backup|/export|/config`→backup; write verbs→actuate;
   GET `/status|/health`→check) and emits a **staged** recipe (guessed auth + unmatched
   capabilities flagged). Recipe authorship is now automated; the operator's live check +
   `overrides/` own the heuristic's last mile.
7. **The `onboard` orchestrator** (§4.1) — ✅ *done* — `galaxy.py onboard` is the
   one-shot. Dry-run prints the plan; `--apply` performs the three repo mutations
   idempotently (module decl + an **additive drop-in inventory host**
   `instance/inventory/onboarded-<key>.yml` — enabled by the directory-inventory
   refactor, never a hub edit — + the `instance/fleet.yml` enable); `--commit` commits
   (rationale-first + trailer); `--bootstrap` installs the collection + live-verifies
   via `ping --limit`. Creds (SOPS, on the VM) + the final `git push` stay deliberate
   manual gates by design. Existing group → inherits `group_vars`; new group → flagged.
8. **GUI** — ✅ *alpha* — `gui/` is a thin Flask shell over `search` + `onboard`
   (carries no logic; every action shells to `galaxy.py`). Search → capability badges →
   "Onboard" → connection-details form → managed device + backup. The onboard engine
   now **self-completes** (creds → SOPS via `sops --set` + inline per-host lookups, no
   `group_vars` hand-edit; `--push` so Semaphore sees it), so the **only** manual inputs
   are the search selection and the connection details. Verified VM-direct; container
   fragment staged. Pre-beta: auth + TLS + audit log ([gui/README.md](../gui/README.md)).
   QA/CI to harden it: [docs/qa-and-release-pipeline.md](qa-and-release-pipeline.md).

## 9. Risks / open questions

- **Galaxy shallow-probe accuracy** — names/contents may mislabel; mitigated by
  confidence scoring + deep-probe-on-select.
- **NAPALM currency** — its Ansible integration is older/driver-limited; treat as
  one backend among several, not the foundation.
- **Backend/vector schema churn** — get `backend.yml`/`vectors/*.yml` schemas right
  early (they're the extensibility contract); validate them in `tests/validate`.
- **OpenAPI coverage/quality** (§5.2) — central registries skew to public APIs;
  niche gear needs live/vendor spec fetch, and endpoint→capability is heuristic +
  `overrides/`. The `api` backend must degrade to "actuate only / backup via
  override" when no recipe can be derived.
- **Search UX surface** — CLI first (fits the toolchain); the GUI (§8.8) is the last
  layer, not a blocker.

## See also
- [PLAN.md](../PLAN.md) §2.5 — the modularity doctrine this extends
- [modules/README.md](../modules/README.md) — the device-class registry it feeds
- [scripts/README.md](../scripts/README.md) — the `galaxy.py` CLI (search/classify/scaffold)
- [tests/e2e/README.md](../tests/e2e/README.md) — the non-human onboarding proof
- [docs/agent-workflow.md](agent-workflow.md) — how an agent maintains `overrides/`
- [observability-onboarding-flow.md](observability-onboarding-flow.md) — Option-A design: the **telemetry capability vector** (a 4th vector, parallel to actuate/backup/bespoke) + the telemetry-method registry **(design only)**
