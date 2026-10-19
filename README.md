# kontroll

Central control panel for a homelab. A **composed control plane** — not a
monolith — that provides Ansible automation, a service dashboard, infra
metrics, and encrypted secrets, each as an independently restartable component.

> **Read [PLAN.md](PLAN.md) first.** It holds the architecture, the locked
> decisions, the modularity doctrine, and the phased rollout. This README is
> just the orientation + quickstart.

## What's here

| Path | What it is |
|---|---|
| [PLAN.md](PLAN.md) | Architecture, decisions, rollout. Source of truth for *why*. |
| [CLAUDE.md](CLAUDE.md) | Contributor directives + Hard Rules (Never Break). |
| [SECURITY.md](SECURITY.md) | Threat model, controls (each mapped to a check), accepted risks. |
| [CHANGELOG.md](CHANGELOG.md) | Behaviour-affecting changes, `[Unreleased]` on top. |
| [START-HERE.md](START-HERE.md) | **Installing from a bundle? Start here** — points at the full fresh-VM path. |
| [docs/install-from-scratch.md](docs/install-from-scratch.md) | **The complete "stock VM → working instance" install** (bundle flow; no GitHub needed). |
| [docs/SETUP.md](docs/SETUP.md) | First-run + continual config for developers with **repo/clone** access. |
| [docs/engineering-standards.md](docs/engineering-standards.md) | Testing/docs/logging/security doctrine, distilled from the NetConfig project. |
| [docs/agent-workflow.md](docs/agent-workflow.md) | How we run multi-agent reviews: read-only agents report into `docs/reviews/`, the main thread actuates. Which review loop a change gets. |
| [docs/adr/](docs/adr/README.md) | Decision records: what was decided, why, and what would change it — with the index of decisions, written and pending. |
| [docs/observability/](docs/observability/README.md), `docs/capability-matrix.md`, `docs/api-architecture.md`, `docs/qa-and-release-pipeline.md` | Design history: how the capability track, the API and CI were designed. Each says so on its first line, and says what was built. |
| `modules/` | Device-class registry — one self-describing `module.yml` per class. The fleet catalog. |
| `instance/fleet.yml` | **The one knob:** which modules you run (pick-and-choose). |
| `bootstrap.sh` | Stage 0 shim for `scripts/install-prereqs.sh` (Docker + git). |
| `docker/` | Compose stack: Semaphore (Ansible UI), Homepage (portal), Prometheus + Grafana. One file per service via `include:`. |
| `ansible/` | Inventory (functional groups), one role per device class, thin playbooks (incl. `bootstrap.yml`), SOPS-encrypted secrets. |
| `dashboards/` | Homepage config + Grafana dashboards-as-code. |
| `prometheus/` | Scrape config + file-based service-discovery targets. |
| `tests/` | The test pyramid: `validate` (L1/L2), the pytest and browser suites, the mock inventory. |
| `docs/` | SETUP, engineering standards, VM-creation recipe. |

## Design rule (non-negotiable)

**Deeply modular. No god files.** Adding a device, service, dashboard, or
metrics target means *creating a new file*, never editing a central hub file.
See PLAN.md §2.5 for the "add an X" test that every change must pass.

## Current status

**The infrastructure track (PLAN.md §10, phases 0–5) is live on the reference lab:** a
Debian control VM on a Proxmox node (mgmt VLAN, `<mgmt-ip>`) runs the composed stack —
Semaphore, Homepage, Prometheus + Grafana, Loki + Vector — with the fleet onboarded through
the `:8443` GUI and every credential SOPS-encrypted. The dated record is
[CHANGELOG.md](CHANGELOG.md); the capability track (telemetry, logging, backup, actuation)
is summarised in PLAN.md §13.

Build order (PLAN.md §10): 0 scaffold → 1 control node + connectivity → 2 secrets +
config-backup job → 3 Semaphore → 4 Homepage → 5 metrics — all delivered.

## Quickstart

**From a clone, a release bundle or a launch kit, on a fresh Linux node** (no GitHub account needed for the
last two): the complete, tested sequence is the quickstart at the top of
**[docs/install-from-scratch.md](docs/install-from-scratch.md)**. The host needs only Docker and git; everything
else runs in the installer container — which is also how a play is run later; that page says how.

The first-run reference, continual configuration and the rebuild procedure are in
**[docs/SETUP.md](docs/SETUP.md)**.
