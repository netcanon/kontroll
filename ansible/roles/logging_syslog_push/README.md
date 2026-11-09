# roles/logging_syslog_push

Device-side **wiring role** for the `syslog_push` logging method (the secondary-capability `logs:` block,
instance #3). Points a network_cli-managed device's syslog at the kontroll collector so its logs flow into
Vector's `syslog` source → Loki. The Vector source + its shaping transform are **generated** from the class's
`logs:` block (`docker/vector/generated/syslog_push_<key>.generated.yaml`, via `scripts/gen-logging.py`); this
role configures the **device** to ship to it. Registry descriptor: [`logging/syslog_push.yml`](../../../logging/syslog_push.yml).

```
# Access chain used:   control VM (mgmt VLAN) -> device mgmt (network_cli) sets `logging host <collector>`
# May break:           the device's syslog config ONLY (a wrong collector host = logs go nowhere)
# Fallback required:   console for edge_firewall/core_switch (OPNsense HDMI / CRS310 MAC-Winbox); else no
# Blast radius:        this device's syslog config (reversible — re-run with the prior host, or `no logging host`)
```

## How it runs

It is **never** run directly. `logsvc.logging_enact_commands` emits it as the ENACT step after a capability
promote; the operator runs it through the dispatcher with a **dry-run first** (CLAUDE.md hard rule — this
touches the switch):

```bash
ansible-playbook ansible/playbooks/wire-logging.yml -e role=logging_syslog_push -e target=cisco_ios --check --diff
ansible-playbook ansible/playbooks/wire-logging.yml -e role=logging_syslog_push -e target=cisco_ios   # then apply
```

`wire-logging.yml` resolves the device target group from `modules/<key>/module.yml`'s `inventory_group` and the
collector host from `instance/instance.yml`'s `mgmt_ip` (override with `-e collector_host=<ip>`).
`wire-logging.yml` runs this role only over its method's `wiring_connections`
(`logging/syslog_push.yml`); a host on another connection is refused first (SECURITY.md C4).

## Entrypoints

| `tasks_from:` | Does |
|---|---|
| `main` (default) | Push the syslog-host config line(s) via the `backend_netcommon_cli` `configure` entrypoint (idempotent `cli_config`, `--check`-safe). |
| `backup` | Capture `show logging` as the rollback reference before a change (read-only). |

## Extending / overriding

The default config line is **IOS-family** (`logging host <ip> transport <tcp> port <5514>`). A divergent
vendor overrides `logging_syslog_lines` (a list) in its `group_vars`/module — **no per-vendor branching** lives
in this role (the mapping stays at the `device_role` seam, which dispatches the backend). Knobs (all read by
the shared wiring playbook + backend, so no role-name prefix — the cross-role contract convention):

| var | default | meaning |
|---|---|---|
| `logging_collector_host` | `""` (required) | the collector's mgmt IP; `wire-logging.yml` supplies it |
| `logging_transport` | `tcp` | `tcp`/`udp` — must match the Vector source mode |
| `logging_port` | `5514` | the published, mgmt-bound Vector syslog port |
| `logging_syslog_lines` | `["logging host …"]` | the vendor config line(s) to push |

## Error table

| Symptom | Cause | Fix |
|---|---|---|
| `logging_syslog_push needs logging_collector_host` | no collector resolved | set `instance/instance.yml` `mgmt_ip`, or pass `-e collector_host=<ip>` |
| Logs never arrive in Loki | the ingress port isn't published / mgmt-bound elsewhere | confirm `docker/services/vector.yaml` publishes `${KONTROLL_MGMT_IP}:5514`; the device can reach it on the mgmt VLAN |
| `changed` on every run | the device rewrites the line (ordering/normalization) | pin `logging_syslog_lines` to the device's normalized form |

See also: [`capabilities/logging.yml`](../../../capabilities/logging.yml),
[`docs/observability/logging-capability-vector.md`](../../../docs/observability/logging-capability-vector.md),
[`docs/logging-architecture.md`](../../../docs/logging-architecture.md).
