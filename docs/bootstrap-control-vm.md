# Bootstrap: the control VM (Phase 1)

Stand up the control node that runs Ansible + the Docker stack. One-time.

> Access chain used:   workstation → Proxmox node GUI/SSH (mgmt VLAN)
> May break:           none (creating a new VM, no change to existing services)
> Fallback required:   no
> Blast radius:        this VM only

## 1. Create the VM on your Proxmox node

| Setting | Value |
|---|---|
| Host | **your Proxmox node** (e.g. `my-hypervisor`, `192.0.2.11`) |
| OS | Debian 12 or 13, minimal |
| vCPU / RAM | 2 vCPU / 4 GB (Docker stack baseline; bump if Grafana/Prometheus grow) |
| Disk | 32 GB (Prometheus retention lives here — grow later if needed) |
| NIC | bridge on your trunk, **the management VLAN tag** |
| IP | static **<mgmt-ip>/24**, gw `<gateway-ip>`, DNS `<dns-ip>` *(confirm the address is free first)* |
| Hostname | `control` (`control.${KONTROLL_DOMAIN}`, e.g. `control.example.com`) |

## 2. Control-node configuration → see SETUP.md

Everything after the VM exists — Stage 0 prerequisites, the read-only deploy-key
clone, declaring your fleet, installing the right collections, the age key,
secrets, and proving connectivity — is **automated and documented in
[SETUP.md](SETUP.md)**.

Do **not** follow per-package steps here (they drift). Once the VM exists, the commands are the quickstart at the
top of [install-from-scratch.md](install-from-scratch.md) — the one written install sequence.

## 7. Publish behind NPM (when ready)

- Unbound host override on the edge: `control.${KONTROLL_DOMAIN}` → `${KONTROLL_MGMT_IP}`.
- NPM Internal proxy host: `control.${KONTROLL_DOMAIN}` → `${KONTROLL_MGMT_IP}:3000`
  (Homepage). Add Semaphore/Grafana as their own subdomains or path routes.
- Edge firewall: allow control (mgmt VLAN) → server-VLAN service ports
  + the AP VLAN, scoped (not any/any).
