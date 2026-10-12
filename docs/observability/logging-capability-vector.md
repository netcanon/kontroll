# The logging capability vector (instance #3)

The **logging** capability lets an already-onboarded device/service class **export its logs into Loki via
Vector**, added through the same standalone capability dialog as telemetry/backup (PROPOSE→PROMOTE→ENACT) with
**zero spine/shell/route edit**. This is the sibling of
[telemetry-capability-vector.md](telemetry-capability-vector.md); read that first for the shared vector model.
The store/collector architecture is [../logging-architecture.md](../logging-architecture.md) (§3.5 is the
dual-purpose plan this implements).

## 0. The one decision that drives everything: what the vector can and cannot see

A capability **vector** matches a *collection's probe facts* (modules/plugins), **not a live host**. "Does this
thing emit logs?" is **almost universally true** and is mostly a **host/runtime fact** (journald on a host,
`docker_logs` on a container, a file on disk) — which is **not** a collection-plugin fact. So most log sources
are **operator-DECLARED** in `modules/<key>/module.yml` (`logs: [...]`), never probe-detected — exactly like
`host_node` for telemetry.

The **one** collection-visible signal is "this box has a CLI/NETCONF management plane" (`cliconf`/`netconf`):
such gear can be *configured* to ship syslog to us. So [`vectors/logging.yml`](../../vectors/logging.yml) only
ever suggests **`syslog_push`**, at **low** confidence — never high/medium. The DECLARED `logs:` block is the
source of truth; the vector is a non-binding hint.

## 1. The DECLARED ↔ DETECTED relationship

- **DETECTED** (`classify.suggest_logging`): reads the `logging` vector cell + the thin `_LOGGING_HINT`
  (`cliconf`/`netconf` → `syslog_push`). Returns `{cell, candidates, note}`, PURE, writes nothing. A drift-pin
  test holds `_LOGGING_HINT == capabilities/logging.yml suggester.hint`.
- **DECLARED** (`classify.declared_logs_methods`): reads each enabled module's `logs:` LIST. The picker UNIONs
  `applicable ∪ declared`, so a declared method (journald on a host) always shows even when the vector is silent.

### 1.1 The graceful-export probe — optional, read-only, NON-GATING (INVARIANT-D)

`suggest_logging` takes an **optional** `evidence` argument (default `None`). When `None` — the default and the
**only** value the onboard/promote spine ever passes (`capability.suggest()` calls it with three positional
args) — the output is **byte-identical** to before: the probe plumbing is invisible to onboarding. INVARIANT-D
is preserved *by construction*, and `tests/unit/test_suggest_logging_probe.py` pins exactly that.

A *separate, explicit, post-onboard* "sharpen this suggestion" action MAY run
`probe.logging_export_probe(addr, conn, run_show)` — a **read-only** (`show logging` / `show running-config |
include logging host`), **fail-soft** (unreachable/unknown-command degrades to `{state: maybe}`, never raises)
detection — and feed its `{state, note}` to `suggest_logging(evidence=…)`. The evidence can only **re-word the
note** of a candidate the vector+hint **already** produced; it can **never add, remove, or gate** a candidate
(an evidence-with-no-candidate is silently ignored — the class stays offerable). So the probe is structurally
incapable of becoming a de-facto onboarding gate. The per-plugin read command is **config-as-data** in
`capabilities/logging.yml` (`probe.show.{cliconf,netconf}`), never logic in `classify.py`; the **live read**
against a real device is S7 (`needs_real_device`) — the S6 plumbing + fixture-tested fail-soft helper ship now.

## 2. The method registry — `logging/<method>.yml`

Four drop-in methods (`catalog.load_logging()`, sorted by `order`). Each declares its Vector **source kind**,
**direction** (push = device ships to us, a wiring play configures it; pull = Vector reaches out), the Loki
stream **labels** it contributes (a CLOSED subset of the canonical non-secret set), and an optional
`secret_domain`. Schema: [logging/README.md](../../logging/README.md).

| Method | kind | direction | wiring | secret_domain |
|---|---|---|---|---|
| `syslog_push` | syslog | push | `logging_syslog_push` | none (TLS-syslog would) |
| `journald_remote` | journald | push | `logging_journald_remote` | none |
| `rest_pull` | http_client | pull | — | `logging_rest` (API token) |
| `file_tail_ssh` | file | pull | `logging_file_tail_ssh` | none |

## 3. Generation — `scripts/gen-logging.py`

Fans enabled modules × their `logs:` entries × the inventory hosts → one Vector **source + shaping transform**
fragment per `(method, key)` + one aggregate **Loki sink** under `docker/vector/generated/` (a config-dir
**disjoint** from the hand-authored `config.d/` tree, with explicit component ids — so the implicit-namespacing
ids never collide and what Vector loads is what the file says). **Fail-closed:** an unknown method, a param outside the
method's allow-list, or a label outside the canonical non-secret set exits non-zero — the C12 label-hygiene +
config-injection guards. A `gen-logging --check` validate step keeps the committed tree honest
(generated-never-hand-maintained).

**Retention is ENACTED (not just declared).** The `retention` method param (`7d`/`30d`/`90d`) is no longer an
inert knob: the same generator emits `docker/loki/overrides/retention.generated.yaml` — a Loki **`runtime_config`
overrides** file (keyed on tenant `fake`, hot-reloaded every 10s, no restart) whose `retention_stream` selects
`{source="capability", device="<key>"}` at the chosen window, carrying the static debug/audit baseline forward.
**Fail-closed:** a class without `retention` contributes no override (its streams inherit the bounded global
`LOKI_RETENTION_PERIOD`); no class with retention ⇒ the file is `overrides: {}`. The seam was dogfood-verified on
Loki 3.4.2 (tenant `fake` not `*`; 24h period floor; an invalid file is fatal at startup so only valid ≥24h
periods + an always-valid default are emitted): see
[../reviews/2026-06-16-storage-logging/40-m1-loki-retention-dogfood.md](../reviews/2026-06-16-storage-logging/40-m1-loki-retention-dogfood.md).

## 4. The canonical label set (C12)

`source` · `host` · `service` · `level` · `run_id` · `device` — low-cardinality, structural, **never a
credential**. High-cardinality/sensitive fields stay in the JSON body (LogQL-filterable, not indexed). The
generated capability sink labels by these only; internal streams use the same set minus `device`. Enforced at
the sink choke-point AND fail-closed in `gen-logging`.

## 5. PROPOSE → PROMOTE → ENACT (zero spine edit)

`capabilities/logging.yml` names `service.module: logsvc`; the neutral spine
([`capability.py`](../../scripts/kontroll/service/capability.py)) dispatches by import-by-convention to
`logsvc.build_plan`/`apply_plan` with **no `if cap=="logging"`**. PROPOSE is a pure diff; PROMOTE writes the
`logs:` block (comment-preserving, via the shared `service/_blockwrite.py`) and regenerates the Vector
fragments from it; ENACT is hand-off strings (`logsvc.logging_enact_commands`) — a Vector reload + (for push
methods) the device-side `wire-logging.yml` play. `enact_kind: vector_reload` is descriptive data (no consumer
switches on it); registering the capability auto-extends the generic registry/INVARIANT-D pins.

The device-side **wiring roles** are built: [`logging_syslog_push`](../../ansible/roles/logging_syslog_push/README.md)
(the end-to-end-provable path — points a CLI device's syslog at the mgmt-bound `:5514` ingress via the
`backend_netcommon_cli` `configure` seam), [`logging_journald_remote`](../../ansible/roles/logging_journald_remote/README.md),
and [`logging_file_tail_ssh`](../../ansible/roles/logging_file_tail_ssh/README.md), fanned by the thin
[`wire-logging.yml`](../../ansible/playbooks/wire-logging.yml) (`-e role= -e target=`, `--check --diff` first).

## 6. Worked example

`modules/cisco_ios/module.yml` declares `logs: [{method: syslog_push}]` — a cliconf device, so the `logging`
vector auto-suggests it (the suggested path, not just declared). The committed
`docker/vector/generated/syslog_push_cisco_ios.generated.yaml` is what `gen-logging` produced.

## See also
- [telemetry-capability-vector.md](telemetry-capability-vector.md) — the sibling capability (instance #1) + the shared vector model
- [secondary-capability-dialog.md](secondary-capability-dialog.md) — the generalized dialog/route/promote spine
- [../logging-architecture.md](../logging-architecture.md) §3.5 — the dual-purpose store/collector plan this implements
- [../../logging/README.md](../../logging/README.md) — the method-descriptor schema
- [../../ansible/roles/logging_syslog_push/README.md](../../ansible/roles/logging_syslog_push/README.md) — the device-side wiring roles (the ENACT actuation)
