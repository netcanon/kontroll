# kontroll — homelab control panel: architecture & rollout plan

Status: **DECISIONS LOCKED / scaffolding** — §11 forks resolved 2026-06-12.
Author: drafted with Claude, 2026-06-12.

### Locked decisions (2026-06-12)

| # | Decision | Choice |
|---|---|---|
| Ansible UI | engine | **Semaphore** |
| Secrets | manager | **SOPS + age** |
| Dashboard | order | **Homepage first, then Prometheus+Grafana** |
| Node type | LXC vs VM | **VM** (friction-free Docker) |
| Host | which node | **a Proxmox compute node** (not the storage box) |
| Timing | edge targets | **Build against the edge you run today** (the reference lab: a FortiGate firewall + a Cisco Catalyst switch), with a deeply-modular design so a later edge-hardware swap (e.g. to OPNsense / MikroTik) is a config change, not a refactor |
| Repo | hosting | **Private remote** (Gitea/GitHub) — SOPS keeps encrypted secrets safe to push |
| Design | doctrine | **Deeply modular at every level. No god files. Adding a device/service/dashboard = drop a new file, never edit a hub file.** See §2.5. |

This document is the single source of truth for *what* we are building and
*why*. It is grounded in a real homelab (the maintainer's reference instance);
your own lab's specifics live in your private `instance/` overlay, never here. When
the reference lab's state changes (e.g. an edge-hardware cutover completes), update
the "Lab assumptions" section here rather than scattering the change.

---

## 1. Purpose & scope

A **central control panel for the homelab** that does four things:

1. **Automation frontend** — a web UI for creating, running, and scheduling
   Ansible jobs against lab infrastructure.
2. **Connectivity** — those jobs reach every device class in the lab
   (OPNsense, MikroTik, OpenWrt, Proxmox, Docker hosts).
3. **Dashboard** — a single pane for service status + server metrics.
4. **Secrets** — Ansible-integrated secret management so credentials never
   live in plaintext in the repo.

### Explicitly out of scope (v1)

- Replacing NPM (Nginx Proxy Manager) as the reverse proxy — we ride behind it.
- Replacing OPNsense DHCP/DNS/firewall — Ansible *configures* these, it does
  not supplant them.
- SSO / identity provider buildout (Authelia/Authentik) — deferred to Phase 5,
  optional.
- Driving the media apps' internal logic (arr automation rules, Plex library
  management) — those are surfaced as **dashboard widgets**, not Ansible jobs.

---

## 2. Core architectural decision: composed control plane, NOT a monolith

**Decision:** Build a small set of best-in-class FOSS tools, each in its own
container, tied together by a thin portal. Reject the single-app/monolith
approach.

### Rationale

- **A monitor must be more reliable than what it monitors.** If the dashboard,
  the automation runner, and the secret store are one process, upgrading the
  Ansible UI can simultaneously blind monitoring and lock secrets. This is the
  "mixed plane ownership multiplies failure modes" principle from the
  migration overview, applied to tooling.
- **Independent restart / independent ownership.** Each concern must be
  separately restartable and upgradable — the same discipline as the lab's
  access-chain rule (never edit the box carrying the session you'd use to fix it).
- **Every concern already has a mature purpose-built tool.** Custom code is a
  liability to own forever and would be worse than the tool it replaces. The
  genuinely-custom surface here is near zero — it's *integration*, not *building*.

### What "central panel" means here

The panel is a **portal that aggregates** (links + status + deep-links into
each tool), **not a god-app that contains**. Single pane of glass, not single
process.

---

## 2.5 Modularity doctrine (load-bearing constraint)

The user's hard requirement: **deeply modular at all levels — no god files,
and adding capability must be additive.** Every layer is designed so that
*expansion = create a new file*, never *edit a central hub file*. This is the
test every structural choice must pass.

### The "add an X" test

| To add a… | You create… | You must NOT edit… |
|---|---|---|
| Managed device | a host entry + (if new class) a role under `ansible/roles/` | existing playbooks or other roles |
| Device *class* (e.g. a new switch vendor) | a new role + a functional group mapping | the inventory of unrelated groups |
| Dashboard service tile | one Homepage group/service fragment | other services' config |
| Metrics target | one file in `prometheus/targets/*.yml` (file-based SD) | `prometheus.yml` |
| Grafana dashboard | one JSON in `dashboards/grafana/dashboards/` | the datasource or provisioning config |
| Stack service | one compose fragment in `docker/services/*.yaml` (compose `include:`) | the other services' compose |
| Secret domain | one `*.sops.yml` under `instance/secrets/` | the SOPS ruleset for other domains |
| A secondary capability (monitoring / backup / future secrets-rotation) | a `capabilities/<cap>.yml` descriptor + its `vectors/<cap>.yml` + a `gen-<cap>.py` + a `suggest_<cap>`/`declared_<cap>` pair in `classify.py` | the capability dialog shell, the `/api/capability` route, `scripts/kontroll/service/promote.py`, the generic decoupling pins, or `scripts/configure-semaphore.py` |

### Concrete mechanisms

- **Functional inventory groups, not hardware names.** Groups are *roles in the
  network* — `edge_firewall`, `core_switch`, `wireless_ap`, `hypervisors`,
  `docker_hosts`. Today `edge_firewall` → FortiGate 100E and `core_switch` →
  Cat 9300; at cutover those map to OPNsense and CRS310 by editing **one
  membership line**, not rewriting playbooks. This is exactly what makes
  "build now on the current edge, swap later" cheap. See §6.
- **Thin playbooks, fat roles.** Playbooks are orchestrators that `import_role`;
  all logic lives in roles. No playbook exceeds orchestration.
- **Layered group_vars / host_vars** — one file per group, never a monolith.
- **Prometheus file-based service discovery** (`file_sd_configs`) so targets are
  data files, not edits to the scrape config.
- **Grafana + Homepage provisioned from directories** — drop-in files, auto-loaded.
- **Docker Compose `include:`** — one file per service, composed at the top.
- **Per-domain SOPS files** — `network.sops.yml`, `proxmox.sops.yml`, etc.

### Process-modularity: the module registry + fleet profile

Content-modularity (above) is necessary but not sufficient — the *setup process*
must also be modular, so a different operator (or a future agent) can **select**
their hardware rather than inherit this lab's. Mechanism:

- **`modules/<key>/module.yml`** — each device class is a self-describing data
  unit (its collections, role, inventory group, secrets domain). Source of truth.
- **`instance/fleet.yml`** — the one knob: `enabled_modules: [...]`. A new person
  edits only this.
- **`ansible/playbooks/bootstrap.yml`** — reads the profile, **generates** the
  collection set from enabled modules, installs the toolchain + age key, verifies.
  Idempotent; codifies what was once manual.

Result: "pick-and-choose" is real — enable a module → its collection installs and
role activates; disable it → it costs nothing. `requirements.yml` is no longer
hand-maintained (it's generated). See [modules/README.md](modules/README.md) and
[docs/SETUP.md](docs/SETUP.md). This doctrine is restated as acceptance criteria
in each phase (§10).

---

## 3. Component selection (locked decisions)

| Concern | Tool | Decision basis |
|---|---|---|
| Ansible frontend | **Semaphore UI** (`semaphoreui/semaphore`) | Locked 2026-06-12. Lightweight Go container; inventories, environments, scheduled task templates, run history, built-in key store, RBAC. Right-sized vs. AWX's k8s heft. |
| Secrets | **SOPS + age** | Locked 2026-06-12. Secrets encrypted *in git*, decrypted at runtime. GitOps-friendly, minimal ops. Vault deferred unless dynamic secrets are later needed. |
| Service portal | **Homepage** (`gethomepage/homepage`) | Locked 2026-06-12. First-class widgets for Proxmox, Plex, the arr stack, NPM. This is the "single pane." Built **first** for fast value. |
| Infra metrics | **Prometheus + node_exporter + Grafana** | Locked 2026-06-12. De-facto homelab metrics stack. Built **second**, layered under the portal. |
| Automation engine | **Ansible** (core + collections) | The runner behind Semaphore. Collections per device class — see §6. |
| Source of truth | **git** (this `kontroll` repo) | Playbooks, inventory, dashboards-as-code, encrypted secrets all version-controlled. |

### Deferred / Phase-5 candidates (not v1)

- **HashiCorp Vault** — only if we want dynamic/leased credentials + a secrets API.
- **Authelia** (SSO in front of the web UIs).
- **Blackbox exporter** (synthetic uptime probes) — easy add once Prometheus exists.
- **Loki + Vector** (log aggregation) — natural Grafana companion; the operator wants
  full-fidelity log troubleshooting, so it's now planned (deploys alongside Phase-5
  Grafana). Collector = Vector (DECIDED 2026-06-16; internal stack built on `feat/logging-vector`).
  Plan of record: [docs/logging-architecture.md](docs/logging-architecture.md).

---

## 4. Where it runs

A **dedicated management VM on the Proxmox cluster, on the management VLAN**.

- The management VLAN (device mgmt + hypervisor mgmt + reverse-proxy egress) is
  where the control node belongs.
- It is the **Ansible control node** *and* the Docker Compose host for
  Semaphore + Homepage + Prometheus + Grafana.
- Reached by users via **NPM Internal** at e.g. `control.${KONTROLL_DOMAIN}`
  (Unbound host override → NPM Internal → control VM), matching how every
  other internal service is published.

### Proposed placement

| Attribute | Value | Notes |
|---|---|---|
| Host | **a Proxmox compute node** (locked) | A healthy compute node; keeps the control plane off the storage box. |
| Kind | **VM** — Debian 12/13 (locked) | Friction-free Docker; avoids LXC nesting/keyctl edge cases. |
| VLAN | the management VLAN | mgmt plane |
| IP | `<mgmt-ip>` (static; the shipped example overlay uses `192.0.2.10`) | Pick from your mgmt subnet's DHCP-exempt range; add DNS + reverse-proxy entries. |
| Hostname | `control` / `kontroll` | `control.${KONTROLL_DOMAIN}` internal name |

> **Access-chain note (carried over from the lab's discipline):** the control
> node sits on the mgmt VLAN and must reach the server VLAN (docker hosts, the
> reverse proxy, media) and the AP VLAN. This requires edge-firewall inter-VLAN allow
> rules sourced from the control node. Any playbook that reconfigures the edge itself
> can sever the path the control node uses — those playbooks carry the access-chain
> annotation header (see §9).

---

## 5. Repository layout (this `kontroll` repo)

```
kontroll/
├─ PLAN.md                      ← this file
├─ README.md                    ← short pointer + quickstart
├─ CLAUDE.md                    ← contributor directives + Hard Rules (Never Break)
├─ SECURITY.md                  ← threat model, controls→checks, accepted risks
├─ CHANGELOG.md                 ← behaviour-affecting changes, [Unreleased] on top
├─ .sops.yaml                   ← SOPS creation rules (which keys encrypt what)
├─ .yamllint / .ansible-lint    ← L1 lint configs
├─ bootstrap.sh                 ← Stage 0 shim: scripts/install-prereqs.sh (Docker + git)
├─ modules/                     ← device-class registry (the fleet catalog)
│  ├─ _core.yml                 ← always-installed collections
│  ├─ <key>/module.yml          ← one self-describing module per device class
│  └─ README.md
├─ config/
│  ├─ fleet.yml                 ← THE knob: which modules you run (pick-and-choose)
│  └─ fleet.example.yml
├─ docs/
│  ├─ SETUP.md                  ← canonical first-run + continual config (agent-runnable)
│  ├─ engineering-standards.md  ← testing/docs/logging/security doctrine (distilled)
│  ├─ agent-workflow.md         ← distributed-agent review protocol (netcanon)
│  ├─ bootstrap-control-vm.md   ← Proxmox VM-creation recipe only
│  └─ reviews/<UTC-date>/       ← frozen evidence trail of each multi-agent review
├─ tests/                       ← the IaC test pyramid (L1→L4)
│  ├─ README.md                 ← pyramid, how to run, how to add
│  ├─ validate.ps1 / validate.sh← L1+L2 gate (lint, syntax, secret hygiene)
│  └─ mock-inventory.yml        ← the single real↔fake seam for check-mode
├─ docker/
│  ├─ compose.yaml              ← composition only (include: per service)
│  ├─ services/<name>.yaml      ← one file per service (Semaphore/Homepage/Prom/Grafana)
│  └─ README.md
├─ ansible/
│  ├─ ansible.cfg
│  ├─ inventory/
│  │  ├─ hosts.yml              ← functional groups (edge_firewall, core_switch, …)
│  │  └─ group_vars/            ← one file per group (not per host)
│  │     ├─ all.yml
│  │     ├─ edge_firewall.yml
│  │     ├─ core_switch.yml
│  │     ├─ hypervisors.yml
│  │     ├─ docker_hosts.yml
│  │     └─ wireless_ap.yml
│  ├─ host_vars/
│  ├─ secrets/                  ← SOPS-encrypted vars (*.sops.yml), one per domain
│  ├─ collections/
│  │  └─ requirements.generated.yml  ← built by bootstrap from enabled modules (gitignored)
│  ├─ roles/                    ← one per device class (cisco_ios worked; rest stubbed)
│  │  ├─ .role-template/
│  │  └─ {cisco_ios,fortigate,proxmox,docker_host,openwrt,opnsense,routeros}/
│  └─ playbooks/
│     ├─ bootstrap.yml          ← fleet-driven control-node setup (Stage 1)
│     ├─ ping.yml               ← connectivity smoke test
│     └─ backup-configs.yml     ← pull device configs (per-class `backup` entrypoint)
├─ dashboards/
│  ├─ homepage/                 ← services.yaml, widgets.yaml, settings.yaml
│  └─ grafana/                  ← provisioned dashboards + datasources (as code)
└─ prometheus/
   ├─ prometheus.yml            ← scrape jobs (targets via file_sd, not inline)
   ├─ targets/<job>/*.yml       ← file-based service-discovery targets
   └─ README.md
```

GitOps posture: the repo is the source of truth. Semaphore pulls playbooks
from it; Grafana/Prometheus/Homepage configs are provisioned from it.

---

## 6. Ansible design — connecting to *this* lab

Ansible owns the **infrastructure tier**. The **application tier** (arr/Plex)
is surfaced via dashboard widgets, not Ansible.

### Functional groups (the key to a clean cutover swap)

Inventory groups are **roles in the network**, not hardware names. The current
hardware is mapped in today; at cutover the mapping moves to the target hardware
by editing one membership block — playbooks and roles are untouched.

| Functional group | TODAY (current edge — build target) | POST-CUTOVER (staged) | Connection / collection |
|---|---|---|---|
| `edge_firewall` | `my-firewall` `192.0.2.1` — a FortiGate (class `fortigate`) | e.g. OPNsense (class `opnsense`) | today: `fortinet.fortios` (httpapi) or SSH → role `fortigate`. later: `ansibleguy.opnsense` → role `opnsense` |
| `core_switch` | `my-switch` `192.0.2.2` — a Cisco Catalyst (class `cisco_ios`) | e.g. MikroTik CRS (class `routeros`) | today: `cisco.ios` (network_cli) → role `cisco_ios`. later: `community.routeros` → role `routeros` |
| `wireless_ap` | `my-ap` `192.0.2.3` — an OpenWrt AP (class `openwrt`) | (unchanged) | raw SSH → role `openwrt` |
| `hypervisors` | `my-hypervisor` `192.0.2.11`, `my-hypervisor-2` `192.0.2.12` (class `proxmox`) | (unchanged) | SSH + API token → role `proxmox` |
| `docker_hosts` | `my-docker-host` `192.0.2.20`, `my-proxy` `192.0.2.21`, `my-proxy-2` `192.0.2.22` (class `docker_host`) | (unchanged) | SSH → role `docker_host` (`community.docker`) |

> **Why this matters for §11.3 (build now / current edge):** because the edge
> is referenced everywhere by the *function* `edge_firewall`, not by
> "fortigate", a playbook written today against FortiOS keeps working after we
> add the OPNsense role and flip the group membership. The hardware swap is a
> data change. Each device-class role is self-contained (`roles/fortigate`,
> `roles/opnsense`, `roles/cisco_ios`, `roles/routeros`, …) so the migration
> repo's eventual cutover is mirrored here as "activate the staged role," never
> a rewrite.

Per-class connection caveats (current edge):
- **FortiGate** — `fortinet.fortios` over httpapi (REST), or SSH for
  `show`/`execute`. This is the live firewall — **high blast radius**;
  read-only/backup plays first, config plays gated behind `--check` + §9 headers.
- **Cisco Catalyst** — `cisco.ios` over network_cli (SSH). `ios_command` for
  read/backup; `ios_config` for changes (idempotent, `--check`-able).
- **At a hardware cutover** keep the old devices racked + powered for the overlap
  window (warm rollback), so they remain valid Ansible targets until the swap is proven.

Notes on the staged classes:
- **OPNsense is the edge.** Treat its playbooks as high-blast-radius. Prefer
  the API over SSH; never run a rule-flush that could orphan the control node's
  own path (§9).
- **CRS310 has no serial console.** A bad RouterOS push is recovered only via
  MAC-Winbox / reset+import. Playbooks must be idempotent and tested in
  `--check` first; consider a "safe-mode"-style two-step for risky changes.
- **An unreachable host must not fail the whole run.** `backup-configs.yml` uses
  `ignore_unreachable`; `ping.yml` uses an explicit TCP-probe block/rescue so a
  down host is reported honestly, not as a false "ok".
- **Config backup playbook** — per-class pull logic (FortiGate/Cisco today; OPNsense
  `config.xml`, RouterOS `/export`, Proxmox configs for the staged classes). This is the first
  genuinely useful job and a safe Phase-2 deliverable.

### Inventory provenance

`instance/inventory/hosts.yml` (your private overlay) is the fleet's single source of
truth for kontroll. If you mirror it from an external inventory document, **it is a
copy, not a link** — when the lab changes, update both, or (better) treat the overlay as
canonical and keep the external copy in sync.

---

## 7. Secrets design — SOPS + age

- **One age keypair** for the control node. Public key in `.sops.yaml`; private
  key on the control VM at `~/.config/sops/age/keys.txt` (mode 600), **never
  committed**. Back it up offline (this is the master key — lose it and all
  encrypted secrets are unrecoverable).
- `.sops.yaml` creation rules encrypt anything under `instance/secrets/**` and
  any `*.sops.yml`.
- Playbooks consume secrets via the `community.sops` lookup/vars plugin, so
  decryption happens at runtime, in memory.
- **Semaphore** runs as the control user and inherits the age key, so UI-driven
  runs decrypt transparently. Semaphore's own key store holds the *connection*
  creds (SSH keys, API tokens) for reaching devices; SOPS holds the
  *playbook-level* secrets (service passwords, tokens injected into configs).
- Git pre-commit hook (optional, Phase 3) to reject any unencrypted secret file.

> Encryption at rest is mandatory here. A frozen, local-only, single-shot
> translation repo might tolerate plaintext credentials; a live automation control
> plane with standing credentials to every device cannot.

---

## 8. Dashboard design

### 8a. Homepage (portal) — built first

Static, fast, widget-driven landing page. Widgets pulled from the lab:

| Group | Widgets |
|---|---|
| Infrastructure | Proxmox (cluster widget), the edge firewall, the reverse proxy (internal + external) |
| Media | e.g. a media server `192.0.2.30:32400` + its request portal |
| Apps (`my-docker-host` `192.0.2.20`) | e.g. the download/automation stack behind the media server |
| Management | Semaphore, Grafana, Portainer (`portainer.example.com:9443`) |

Widget API keys/tokens → SOPS-encrypted, injected into Homepage config at
container start.

### 8b. Prometheus + Grafana (metrics) — built second

| Target | Exporter |
|---|---|
| Proxmox hosts + docker hosts (host metrics) | `node_exporter` |
| Proxmox cluster (VM/LXC/storage) | `prometheus-pve-exporter` |
| OPNsense | `os-node_exporter` plugin (or node_exporter) |
| MikroTik CRS310 | `mikrotik-exporter` or SNMP |
| Service uptime (Phase 5) | `blackbox_exporter` |

Grafana datasources + dashboards **provisioned as code** from
`dashboards/grafana/` — no click-ops, version-controlled, reproducible.

> Proxmox has a **native** metric exporter (InfluxDB/Graphite under
> Datacenter → Metric Server). We use the Prometheus pve-exporter instead to
> keep one TSDB (Prometheus), but the native path is a fallback if the exporter
> is fussy.

---

## 9. Networking, access & safety

- **Publish:** `control.${KONTROLL_DOMAIN}` → Unbound host override → NPM Internal
  → control VM. Same pattern as every other internal service.
- **Firewall:** edge allow rules from the control VM (mgmt VLAN) → the server VLAN
  (docker hosts / reverse proxy / exporters) and → the AP VLAN (the OpenWrt AP). Scoped
  to the specific ports, not any/any.
- **Access-chain header for risky playbooks.** Any playbook that can sever the
  control node's own management path (OPNsense rule changes, CRS310 VLAN/bridge
  changes) carries an access-chain header:

  ```
  # Access chain used:   <how the control node reaches the target>
  # May break:           <what path this play can sever>
  # Fallback required:   <yes/no — e.g. OPNsense HDMI console, CRS310 MAC-Winbox>
  # Blast radius:        <this device / VLAN / LAN / WAN / all>
  ```

- **Backups of the control plane itself:** the git repo + the age private key +
  Semaphore's database. Proxmox-level snapshot/backup of the control VM covers
  the runtime; git covers config; the age key is backed up offline separately.

---

## 10. Phased rollout

Each phase is independently shippable and leaves the lab in a working state.

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0. Scaffold** | git repo, directory skeleton, `.sops.yaml`, README, compose file stubs | `git log` shows initial structure; nothing deployed |
| **1. Control node + connectivity** | Provision control VM; install Ansible; `inventory/hosts.yml`; `ping.yml` reaches every up host | `ping.yml` green across proxmox/network/docker groups (down hosts tolerated) |
| **2. Secrets + first real job** | SOPS+age wired; `backup-configs.yml` pulls device configs into the repo | A scheduled config backup runs and commits, encrypted secrets decrypt at runtime. **Exit action: graduate `ansible-lint` profile `moderate`→`safety`** (roles now carry real logic — see engineering-standards.md §1) |
| **3. Semaphore** | Semaphore container; repo linked; `ping` + `backup` as task templates; schedule the backup | Backup runs on a Semaphore schedule; run history visible in UI |
| **4. Homepage** | Portal with all widgets (§8a); published at `control.${KONTROLL_DOMAIN}` | Single page shows live status for Proxmox, media, arr, mgmt tools — ◑ **portal LIVE** (4 groups, tiles + bookmarks, `:3000`); live widgets (4b) pending per-service SOPS tokens; external publish pending |
| **5. Metrics** | Prometheus + Grafana + exporters; provisioned dashboards | Grafana shows host + Proxmox + OPNsense + CRS310 metrics |
| **6. Hardening (optional)** | SSO (Authelia), blackbox probes, log aggregation, pre-commit secret guard, **full CI + QA** (see [docs/qa-and-release-pipeline.md](docs/qa-and-release-pipeline.md)) | As scoped when we get there |

### Quality gates apply to EVERY phase

Per [docs/engineering-standards.md](docs/engineering-standards.md), each phase's
exit criteria implicitly include:

- **L1+L2 green** — `tests/validate` passes (yamllint, ansible-lint,
  `--syntax-check` vs. the mock inventory, compose config, promtool, secret
  hygiene). No commit without it.
- **Docs current** — touched directories' READMEs (Extending + error tables),
  `CHANGELOG.md [Unreleased]`, and `SECURITY.md` if a control/secret/boundary
  changed.
- **Secret discipline** — `no_log: true` on secret-handling tasks; nothing
  unencrypted under `instance/secrets/`.
- **Idempotence (L4)** — any state-changing role re-applies as `0 changed`.

Live smoke (L3) runs against the real fleet only in Phase 1+ and on Semaphore
schedules — never in blind CI.

**Recommended first session of building:** Phases 0 → 1 (scaffold + prove
connectivity). Everything downstream depends on Ansible actually reaching the
fleet, so we de-risk that first.

---

## 11. Decisions — RESOLVED 2026-06-12

1. **Control node: LXC or VM?** → **VM** (Debian, friction-free Docker).
2. **Which Proxmox host?** → **a compute node that is not the storage box**.
3. **Build now or after cutover?** → **Build now against the current
   FortiGate/Cisco edge**, using functional inventory groups (§6) and per-class
   roles so an edge-hardware swap at cutover is a membership flip, not a
   refactor. The deep-modularity doctrine (§2.5) exists largely to make this
   cheap.
4. **Static IP for the control node** → a static **`<mgmt-ip>`** (confirm an
   unused address in your mgmt subnet before Phase 1).
5. **Repo remote?** → **Private remote** (Gitea/GitHub). SOPS encrypts secrets
   at rest, so pushing is safe. Provider TBD — note in README once chosen.

### Remaining small confirmations before Phase 1 (live work)

- Confirm `<mgmt-ip>` is free on the mgmt VLAN.
- Which private remote host (self-hosted Gitea vs. GitHub private)?
- Credentials path: do you have/want a dedicated `ansible` SSH key + per-device
  API tokens, or reuse existing admin creds for v1?

---

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Control node becomes a single point of control failure | It controls but does not *serve* prod traffic; if it's down the lab still runs. Snapshot + git + offline key backup. |
| A bad OPNsense/CRS310 playbook severs management | Access-chain headers; API over SSH; `--check` first; CRS310 MAC-Winbox / OPNsense HDMI console fallbacks documented. |
| age private key loss | Offline backup of the key at creation; documented in README. Without it, all secrets are unrecoverable. |
| Inventory drift vs. the lab's real state | Treat this repo's inventory as canonical post-cutover; backup playbook detects config drift on devices. |
| Scope creep into a monolith | This document. Each tool stays in its lane; "panel" = portal, not god-app. |

---

## 13. Current state & next step

The tool's current state — what is released, what works, what is known broken, what is next and which decisions are
open — is [STATUS.md](STATUS.md), one dated page that `tests/check-status.py` compares with the tree. The dated
record is [CHANGELOG.md](CHANGELOG.md). This section kept that state until 2026-10-19; the git history has it.

### Wanted (planning + development needed — not yet designed)

- **Port-aware address fields, modularly, everywhere an IP is configured.** A legitimate reality: SSH on a
  non-standard port, an off-norm SNMP/HTTP port, etc. Wanted: (a) **any** address field accepts the
  `x.x.x.x:y` form (`y` = port), and (b) **every** such field ships with a **per-field default port** (SSH 22,
  SNMP 161, the exporter's probe port, …). Implement it the same way as everything else here — a **single
  reusable "address+port" field type** (one validator + one renderer + one parse helper, declaring its default
  port) that every form reuses, so adding it to a new form is a drop-in, never a per-field reimplementation.
  It must flow end-to-end: the GUI field → the onboard/capability payload → the inventory host
  (`ansible_host` + the connection port var) → the telemetry target (`<addr>:<port>`). **Needs design**
  (where the parse/default lives — likely a small `kontroll` helper + a GUI field component; how each existing
  IP field opts in; how the port threads into each backend's connection vars and each telemetry method's
  target render) **and development.** Touches: the onboard `field-host`, any future address field, the
  telemetry `target_shape`/`port` render, and the backends' connection params.

Operational source of truth for "how do I stand this up / continue it" is
[docs/SETUP.md](docs/SETUP.md); a deployment's live state is its own `instance/OPERATIONS.md`
(template: [instance.example/OPERATIONS.md](instance.example/OPERATIONS.md)).
