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
| `modules/` | Device-class registry — one self-describing `module.yml` per class. The fleet catalog. |
| `instance/fleet.yml` | **The one knob:** which modules you run (pick-and-choose). |
| `bootstrap.sh` | Stage 0: install ansible+git on a fresh node. |
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

**From a release bundle on a fresh Debian node** (no GitHub account needed). The complete, tested
procedure is **[docs/install-from-scratch.md](docs/install-from-scratch.md)** — the short version:

```bash
sudo bash scripts/install-prereqs.sh                  # §0: docker, ansible, sops, age, …
bash scripts/install.sh kontroll-<version>.tar.gz     # extract + init the local working tree, then: cd kontroll
python3 scripts/kontroll-init.py --fresh --mgmt-ip <your-ip> --domain <your-domain> --trust-mode separated   # scaffold YOUR overlay + keys
# …then seed the canonical, deploy the stack, wire Semaphore, and open the GUI — install-from-scratch.md §3–§8.
```

Developers with **repo/clone** access can instead follow the full procedure (first-run + continual
+ rebuild) in **[docs/SETUP.md](docs/SETUP.md)**.
