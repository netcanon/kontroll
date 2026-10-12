# `logging/` — the log-method registry (the logging capability)

Drop-in descriptors that tell `scripts/gen-logging.py` **how a device-class's logs reach Loki via Vector**.
Peer of [`telemetry/`](../telemetry/) — adding a log ingestion method is **one new file here**, never an edit
to a hub. The capability dialog (`capabilities/logging.yml`) dispatches over this registry exactly as telemetry
dispatches over `telemetry/`. Full design: [docs/observability/logging-capability-vector.md](../docs/observability/logging-capability-vector.md)
+ [docs/logging-architecture.md §3.5](../docs/logging-architecture.md).

## Schema (one `logging/<method>.yml` per ingestion method)

| Field | Meaning |
|---|---|
| `name` | registry key == filename stem == the `- {method: <name>}` reference in a module's `logs:` block |
| `label` / `order` | human label / `load_logging()` sort order |
| `kind` | the Vector SOURCE type: `journald` \| `file` \| `docker_logs` \| `syslog` \| `http_client` \| `http_server` \| `socket` \| `exec` |
| `direction` | `push` (device ships to us — a wiring play configures it) \| `pull` (Vector reaches out — no device push) |
| `source` | the Vector source block `gen-logging` templates (device addr / file globs / mgmt-bind filled per host) |
| `transform_vrl` | OPTIONAL list of VRL lines — the method's **data-driven field derivation** (e.g. syslog `.severity` → `.level`, journald `._SYSTEMD_UNIT` → `.service`). `gen-logging` splices them into the shaping transform **before** the canonical `.service`/`.host`/`.level` defaults, so a real derived value wins and the default is the fallback. The generator never branches on the method name; a new derivation is an edit here. Same trust boundary as `source:` (registry-authored); shape-validated at generate time; nothing compiles the rendered VRL before Vector loads it (SECURITY.md C22, known gaps). |
| `pull_unnest` | OPTIONAL (pull only) — the array field in a pulled `{<field>:[...]}` body to **fan to one event per element**. Renders a pre-transform `. = unnest!(.<field>)` `is_array`-guarded (the abort-on-error `!` is required — `is_array` doesn't narrow the field's type, so a plain `unnest(.x)` is a fallible assignment, VRL E103; the guard makes the abort unreachable); each output event then carries the element under `.<field>`, which `transform_vrl` maps. A safe dotted field path (closed allow-list); fail-closed on a non-pull method or a metacharacter. |
| `dedupe_key` | OPTIONAL (pull only) — the unique field PATH a Vector `dedupe` transform keys on to **drop a re-ingested entry** across polls (a recent-window endpoint with no since-cursor re-reads each cycle). Renders `dedupe` with `fields.match: [<path>]`. Same closed-path allow-list + fail-closed guard. |
| `labels` | the Loki stream labels this method contributes — a CLOSED subset of the canonical non-secret set `{source, host, service, level, run_id, device}` (C12). `gen-logging` fail-closes on any label outside it. |
| `applies_when` | OPTIONAL collection-fact predicate scoping the picker (mostly ABSENT — the UNIVERSAL bucket, like `host_node`) |
| `params` | the CLOSED per-entry allow-list (config-injection guard); the Stage-3 retention/parse policy rides here as `retention` |
| `wiring_role` | `roles/logging_<method>/` that configures the DEVICE to export (push methods), or `null` (pull) |
| `tls_from_class` | OPTIONAL (a TLS-bearing pull source) — `true` ⇒ `gen-logging` injects `source.tls` from the device class's `vendor_defaults.tls_posture` (`modules/<key>`) ⊕ the instance CA pin (`device_trust.<key>.tls_ca_file` in `instance.yml`), via `scripts/kontroll/endpoints.py`, instead of a literal `tls:` here. The vendor FACT (self-signed default) lives ONCE in the module + is shared with telemetry (no drift); the operator's CA-pin DECISION is instance data (no-bespoke-config tenet). Fail-closed: a missing/unknown class posture exits non-zero (never silently `verify=false`) |
| `secret_domain` | OPTIONAL SOPS domain (a credentialed method, e.g. `rest_pull`'s API token), audited by NAME only; absent for journald/syslog. `file_tail_ssh` names a domain whose secret is an SSH **key** that deploy-stack renders to a FILE + bind-mounts RO into Vector (the 3rd injection shape) — NOT an env token, so it has **no** `secret_env_map` |
| `secret_env_map` | OPTIONAL (credentialed pull) — how the token is composed, as DATA: `{ENV_VAR: "<str.format template over the secret_domain's SOPS field NAMES>"}`. The SAME unified block a telemetry exporter uses (a metrics exporter's 1-field map is the degenerate `"{field}"` case of a logging method's N-field token; ONE filter renders both). The ENV key MUST be the `${ENV}` the `source.auth.value` references (single source of truth, fail-closed). `gen-secret-env.py` collects every telemetry + logging `secret_env_map` into ONE value-free manifest (`config/secret-env.manifest.generated.yml`); deploy-stack decrypts the referenced domain by name and composes the token into `.env` generically (`kontroll_render_token`, `no_log`, fail-soft), so the token SHAPE never lives in a hub. (Vector's env passthrough is a one-line `${VAR:-}` entry in `vector.yaml` — compose `include` can't merge it — carrying no secret/shape.) |
| `access_chain` | the wiring role's access-chain header as data (`chain`/`may_break`/`fallback`/`blast_radius`) |

## The shipped methods

(The set is pinned by `tests/unit/test_logging_methods.py::test_registry_loads_and_sorts_by_order` — a count in
prose would rot; the test fails loudly instead.)

| Method | kind | direction | wiring role | typical class |
|---|---|---|---|---|
| `syslog_push` | syslog | push | `logging_syslog_push` | a switch / firewall (cliconf/netconf) — the only AUTO-suggested method |
| `journald_remote` | exec | push | `logging_journald_remote` (sender) + `logging_journald_receiver` (collector) | a remote Linux host's journal: systemd-journal-upload → mgmt `:19532` systemd-journal-remote → Vector `exec` journalctl on `/var/log/journal-remote` (declared; the control VM's own journal is the internal `journald` source, not this) |
| `rest_pull` | http_client | pull | — (token grant only) | the GENERIC log-API pull pattern (declared; SOPS `logging_rest`) |
| `file_tail_ssh` | exec | pull | `logging_file_tail_ssh` (sender) | a remote Linux host's journald PULLED over SSH: Vector `exec` `ssh root@host` → the host's forced `journalctl -f -o json` → Loki (declared; reads the `logging_file_tail` SOPS keypair mounted RO; uses the gated `kontroll/vector` image with openssh-client). The PULL twin of `journald_remote`'s push |
| `proxmox_api` | http_client | pull | — (reuses the proxmox token) | the CONCRETE credentialed pull: PVE `/cluster/tasks` audit history read-only via a `PVEAPIToken` header (declared; reuses SOPS `proxmox`) |

## Extending — add a log method

1. Drop a `logging/<method>.yml` here (copy the nearest method as the template).
2. If it's `push`, add a `roles/logging_<method>/` wiring role (idempotent, `--check`-safe, `no_log` on creds,
   access-chain header, a `backup` entrypoint).
3. A device-class opts in by declaring `logs: [{method: <method>}]` in its `modules/<key>/module.yml`.
4. `gen-logging.py` fans it into `docker/vector/generated/` (a second `--config-dir`, disjoint from the
   hand-authored `config.d/` tree — NOT config.d namespacing) — no hub edit. A `retention` param additionally
   emits the per-class Loki retention into `docker/loki/overrides/retention.generated.yaml`. `tests/validate`'s
   `gen-logging --check` keeps both generated trees honest.
