# SIEM integration plan — where log normalization belongs

> **DESIGN HISTORY — not a description of the tree.** This is the design record of where log normalization belongs for a SIEM, written in June 2026. It is parked until a SIEM destination exists. Where it disagrees with the code or with `SECURITY.md`, they are right.

> **STATUS: PLANNED — not built. Do not build any of this until a real SIEM destination is on the table.**
> Today (Loki-only) the standard envelope transform is the *correct* answer, not a stopgap — see §3. This doc is
> the durable synthesis of a 2026-06-16 research pass (three independent web surveys) answering one question:
> *for easier ingestion into a SIEM, where should log normalization happen — and does it belong upstream at all?*
> It exists so a future contributor (AI or human) with no memory of that research can act correctly when the
> SIEM question becomes real, instead of re-deriving it or building the wrong thing.

Peer of [logging-capability-vector.md](logging-capability-vector.md) (the BUILT logging capability) and
[../logging-architecture.md](../logging-architecture.md) §3.5 (the dual-purpose Vector/Loki design).

---

## 0. TL;DR — the decision

1. **Deep per-vendor body parsing does NOT belong in kontroll.** Cisco IOS mnemonics, FortiGate `key=value`,
   OPNsense `filterlog` CSV, MikroTik topics → structured fields is the **SIEM tier's** job (its TA / integration
   / ASIM / UDM parser pack), because keeping per-vendor, **per-firmware** parsers current is a maintenance
   treadmill the SIEM vendors specialize in absorbing, and a hand-rolled upstream parser **silently breaks** on a
   firmware bump (§2).
2. **The Loki path stays exactly as it is** — envelope shaping (severity→`level`), the canonical low-cardinality
   label set, body left raw, structure extracted at **query time** via LogQL. Ingest-time deep parsing is both
   wasted and actively dangerous here (label-cardinality blow-up) (§3).
3. **The one seam worth building — when, and only when, a SIEM destination exists — is a `destinations/` sink
   branch + stable identity tags, NOT a parser.** "Branch at the sink, not the parse": emit the canonical event
   once, ship **raw body + vendor/product identity tags** to the SIEM, and let the SIEM's parser pack fire off
   those tags. The identity tags are *derivable from data the registry already holds* (`collections: cisco.ios`
   → `vendor=cisco`), are drift-proof (identity, not format), and even help Loki query-time filtering (§4).
4. **Keep raw, always.** Whatever a SIEM sink maps, the original message body ships intact — the schema-on-read
   hedge that lets you reparse history when (not if) a mapping turns out wrong (§2, §4.3).

If you take nothing else: **don't build vendor body-parsers upstream; build a sink branch + tags, and only once a
SIEM is real.** Until then, change nothing.

---

## 1. Why this doc exists

The logging capability ([logging-capability-vector.md](logging-capability-vector.md)) ships Vector → Loki. Loki
is label-indexed log *storage*, not a SIEM. A natural future ask is "send these logs somewhere that does
detection/correlation" — a SIEM (Splunk, Elastic Security, Microsoft Sentinel, Google SecOps/Chronicle) or an
OCSF-native security lake. That raises the normalization question: a SIEM ingests best when events are parsed
into a known schema, so **where does that parsing happen** — at our collector (Vector), at the SIEM, or at query
time? Getting the placement wrong is expensive: parse too much upstream and you inherit a maintenance treadmill;
parse too little and the SIEM can't correlate. This doc records the answer and the build that follows from it.

---

## 2. The research verdict (the evidence base)

**Where production pipelines put normalization** — surveyed across the major platforms:

| System | Common schema | Where normalization runs | Model |
|---|---|---|---|
| **Splunk** | CIM | **Search-time**, in Technology Add-ons (TAs) on the search tier | schema-on-read (store raw, parse on query) |
| **Elastic** | ECS | **Ingest-time**, via Integrations / Beats modules / ingest pipelines | schema-on-write |
| **Microsoft Sentinel** | ASIM | **Mostly query-time** KQL parsers; optional ingest-time DCR transforms | hybrid |
| **Google SecOps / Chronicle** | UDM | **Ingest-time** parsers (raw also retained; reparse exists) | schema-on-write |
| **OCSF** | *is the schema* | A vendor-neutral **target** (ingest-normalize-to / storage), e.g. Amazon Security Lake | write-target standard |
| **Cribl / observability-pipeline tier** | routes to any | **Upstream of the SIEM** — normalize/reduce/route before storage | pre-SIEM layer |

**Two findings decide the placement for kontroll:**

- **The maintenance-treadmill crux (the decisive one).** For *network-device syslog* specifically, the dominant
  architectures push vendor field-extraction **into the SIEM tier**, and the collector does metadata + routing
  only. Splunk's own reference design (Splunk Connect for Syslog) deliberately limits the syslog layer to
  *metadata* (index/sourcetype/host/timestamp) and leaves content parsing to the TA. The reason: log formats
  drift constantly — FortiOS renames/adds keys across releases (Elastic's module is validated only against
  specific firmware; CEF vs JSON need *different* parsers), `filterlog`'s tail fields shift by L4 protocol, and
  vendors actively re-cut parsers (Chronicle overhauled its Checkpoint parser in Feb 2026). A hand-rolled
  upstream VRL parser is brittle to exactly this: a firmware upgrade that adds a field silently breaks a
  positional/regex parser, and the break surfaces as missing data, not an error. The SIEM vendors maintain that
  currency centrally as a product; you would be signing up to chase it by hand.
- **The upstream-normalize pattern is real but its drivers don't apply here.** Cribl/Datadog-style "normalize to
  OCSF at the edge, then route" is a genuine and growing pattern — but it's an *enterprise cost* play (30–70%
  SIEM-ingest reduction across TB/day) running on **managed** parser content. A homelab control plane has neither
  the volume that justifies the reduction nor a team to maintain the parsers — so it would absorb the full cost
  (the treadmill) for none of the benefit.

**Schema-on-read vs schema-on-write, the durable tradeoff:** schema-on-write (parse at ingest) buys query speed
and portable structure but is **lossy if you discard raw** and a wrong mapping can be permanent; schema-on-read
(store raw, parse on query) is slower at scale but **preserves reparse-ability forever**. The industry hedge is
*keep raw, normalize a view* — which maps directly onto kontroll's "keep the body, shape a canonical envelope."

Key sources (full list in §8): Splunk CIM search-time normalization; Elastic ECS ingest pipelines; Microsoft
ASIM query-vs-ingest; Chronicle UDM parsing; OCSF (Linux Foundation); Splunk Connect for Syslog (metadata-only
upstream); Vector remap/VRL positioning; Loki label-cardinality + LogQL query-time parsing; Datadog Observability
Pipelines OCSF-before-SIEM.

---

## 3. What this means for kontroll — the layer map

| Concern | Layer it belongs in | kontroll status |
|---|---|---|
| RFC5424 envelope parse, `severity`→`level`, source/host/service tags, noise-drop, mgmt-bound transport | **Collector (Vector remap)** | **DONE** — the per-method `transform_vrl` shaping + the syslog/journald sources |
| Low-cardinality stream labels; body left raw; field extraction at query time | **Storage (Loki + LogQL)** | **DONE** — canonical label set, query-time `| logfmt`/`| json`/`| pattern` |
| **Deep vendor-body field extraction** (mnemonic/kv/CSV → structured fields) | **SIEM tier** (TA / integration / ASIM / UDM) | **N/A — not ours to build** |
| Vendor/product **identity tags** + raw fan-out to a SIEM | **Collector (sink branch)** | **PLANNED — §4, gated on a SIEM existing** |

**Why nothing changes for the Loki path:** Loki indexes labels only and is explicitly *not* a SIEM. Promoting
parsed, high-cardinality fields (IP, port, MSGID, user) to stream **labels** at ingest is the documented Loki
footgun (index blow-up, thousands of tiny chunks). The correct Loki pattern — already in place — is a small
bounded label set at ingest and LogQL parsing at query time (with Loki *structured metadata* available for
high-cardinality context if ever needed). So for the destination we actually have today, ingest-time deep
normalization is the wrong tool. The envelope transform is the right and final answer for Loki.

**Note on a prior idea (superseded):** an earlier sketch floated a `normalizers/<format>.yml` *parser* registry
behind `gen-logging`. The research above rules that out — the parser part is precisely the treadmill, and it's
the SIEM's job. The modular instinct was right; the seam is at the **sink and the tags**, not a body-parser. See
§4 for the version that survives the evidence, and §6 for the explicit non-goal.

---

## 4. The architecture to build — *when a SIEM destination is added*

Principle (from the Vector design itself): **normalize once, fan out — branch at the sink, not the parse.** The
canonical event already exists (the `transform_vrl` shaping). Adding a SIEM means adding a *sink* that consumes
the same shaped transforms, ships the **raw body + identity tags**, and delegates the deep parse downstream. Loki
sink is untouched.

### 4.1 SIEM-readiness identity tags (data, derived — not a parser)

Emit a few **stable, low-cardinality** identity fields on each capability log event so a downstream SIEM parser
pack can route and fire. These are *identity*, not *format*, so they never drift with firmware.

- **Derive from data the registry already holds.** `modules/<class>/module.yml` carries `collections:` and `key:`
  — a clean vendor signal. `gen-logging` maps it; an explicit override is optional:
  - `collections: [cisco.ios]` / `key: cisco_ios` → `vendor=cisco`, `product=ios`, `observer_type=network`
  - `key: fortigate` → `vendor=fortinet`, `product=fortigate`, `observer_type=network`
  - `key: opnsense` → `vendor=opnsense`, `product=filterlog`, `observer_type=network`
  - `key: routeros` → `vendor=mikrotik`, `product=routeros`, `observer_type=network`
  - `key: docker_host` / journald → `vendor=linux`, `observer_type=host`
- **Emit as EVENT FIELDS, never Loki stream labels** (cardinality rule). e.g. in the shaping transform:
  `.observer.vendor = "cisco"`, `.observer.product = "ios"`, `.observer.type = "network"`. Vendor/product are
  bounded (one per class), so they're even safe as a Loki query-time filter or structured metadata — a free win
  independent of any SIEM.
- **Schema-neutral.** These tags are identity, not a commitment to ECS/OCSF/CIM. The *sink* (§4.2) does the light,
  destination-specific shaping; the tags are what let it (and the SIEM's parser) do so.

Build this **first** when triggered — it's cheap, drift-proof, helps Loki too, and is a prerequisite for any sink.

### 4.2 The `destinations/<siem>.yml` sink registry (the drop-in seam)

Mirror the existing `logging/<method>.yml` registry, one level down at the sink: a drop-in descriptor per SIEM
destination that `gen-logging` turns into a Vector sink consuming the existing generated transforms.

```yaml
# destinations/splunk.yml   (ILLUSTRATIVE — not built)
name: splunk
sink_type: splunk_hec_logs            # a NATIVE Vector SIEM sink (also: elasticsearch, azure_monitor_logs)
endpoint_env: KONTROLL_SIEM_SPLUNK_ENDPOINT     # injected at deploy, never committed
secret_domain: logging_siem_splunk    # the HEC token — SOPS-encrypted, no_log, env-injected, NEVER a label
encoding: {codec: json}
ship_raw: true                        # the raw body always ships (keep-raw, §4.3)
routing:                              # map identity tags -> the SIEM's routing keys (NOT a body parse)
  sourcetype_from: observer.product   # e.g. Splunk sourcetype = the product tag; the TA keys off it
  index: kontroll_network
mgmt_bound: true                      # egress stays on the mgmt path (C3)
```

`gen-logging` (extended) emits, alongside the existing `loki_capability` sink, a second sink (id e.g.
`siem_splunk`) with `inputs:` = the same generated transform ids, shipping raw + tags. **Zero spine edit** — same
generated-not-hand-maintained, `--check`-gated, drop-in discipline as `logging/<method>.yml`. Which destinations
are active is an `instance/`-overlay selection (mirrors `fleet.yml` enabling modules), so the public tool ships
the registry, an instance opts in.

Per-SIEM sink specifics to honour when building each: **Splunk** `splunk_hec_logs` → HEC token + sourcetype/index
from tags (pair with the vendor TA); **Elastic** `elasticsearch` → a data-stream + light ECS field aliases;
**Sentinel** `azure_monitor_logs` → DCR/table, ASIM parsers run query-side; **OCSF lake** → `aws_s3`/`http` with
an OCSF `class_uid` set from the identity tags. In every case the sink does **light routing/aliasing**, never a
body parse.

### 4.3 Keep-raw invariant

Every sink ships the original message body intact. Upstream shaping only *adds* canonical fields + identity tags;
it never strips or rewrites the body. This is the schema-on-read hedge: if a SIEM-side mapping is later found
wrong, the raw is present to reparse. A test should pin that the SIEM sink encoding includes the raw message.

---

## 5. Modularity, fail-closed & security (C12) contract

The seam inherits the existing logging-capability guards — the build must preserve them:

- **Zero spine edit / drop-in:** a new SIEM = one `destinations/<siem>.yml` file + an instance opt-in + re-run
  `gen-logging`. Never a hub edit. Passes the PLAN.md §2.5 "add an X" test.
- **Fail-closed (mirror `gen-logging`'s existing exits):** an unknown `sink_type`, a missing required sink param,
  or an identity tag/derivation that can't resolve → `sys.exit` at generate time, never a silent/empty sink.
- **C12 secret hygiene:** a SIEM credential (HEC token, API key) is a **new SOPS domain** (`logging_siem_<name>`),
  injected as `${ENV}` at deploy under `no_log`, **never committed and never a label/field**. The identity tags
  are non-secret by construction. Egress stays mgmt-bound (C3). A `secrets_domain` audit-by-name only.
- **First-class deliverables (same commit):** generator tests (a `destinations/` fixture renders a sink with the
  right inputs + keep-raw + no secret in any label), `--check` staleness, doc-sync
  ([logging-capability-vector.md](logging-capability-vector.md), [../logging-architecture.md](../logging-architecture.md),
  SECURITY.md if a trust boundary moves, CHANGELOG), and a `kontroll_run_id`-correlated audit line for any
  GUI/actuated path.

---

## 6. Explicit NON-goals — do **not** build these

- ❌ **No `normalizers/<vendor>.yml` VRL body-parser registry.** This is the firmware-drift treadmill; it's the
  SIEM tier's job. (Superseded sketch — see §3.)
- ❌ **No runtime template/registry pulls.** All composition is build-time in `gen-logging`; a runtime fetch adds a
  hot-path network dependency, latency, and an SSRF/trust surface, and loses `vector validate`/`--check` as gates.
- ❌ **No promoting parsed/high-cardinality fields to Loki stream labels.** Cardinality footgun.
- ❌ **No discarding raw.** Keep-raw is non-negotiable (§4.3).
- ❌ **No SIEM tags emitted with no SIEM present** beyond the cheap, independently-useful vendor/product fields
  (§4.1) — full sink wiring is YAGNI until a destination is real.

---

## 7. Trigger condition & build order

**Trigger:** an operator decision to ship logs to a specific SIEM/security-lake destination. Until then this stays
a plan; the live system keeps the standard envelope transform (which is correct, §3).

When triggered, build in this order (each its own themed commit, test + doc-sync + CHANGELOG same-commit):

1. **Identity tags (§4.1)** — `gen-logging` derives `vendor`/`product`/`observer_type` from `collections:`/`key:`,
   emits them as low-cardinality event fields. Cheap, drift-proof, helps Loki query-time filtering even with no
   SIEM. Tests: derivation map + fields-not-labels pin.
2. **The `destinations/` registry + sink-branch (§4.2)** — the drop-in descriptor, the `gen-logging` second-sink
   emission, the instance opt-in, the new SOPS domain. Tests: fixture renders correct `inputs` + keep-raw + no
   secret leak; `--check` staleness.
3. **Per-SIEM sink hardening** — the one destination actually chosen (HEC/ECS/DCR specifics), then live-verify the
   SIEM ingests + its parser pack fires (device/SIEM-gated, like all live logging verification).

Touch points (no spine files): `scripts/gen-logging.py`, a new `destinations/` registry + loader in
`scripts/kontroll/catalog.py` (mirrors `load_logging`), the instance overlay selection, `tests/unit/`,
`instance/secrets/` (new domain), and the docs in §5. Estimated as a small capability addition, isomorphic to how
`logging/<method>.yml` was added.

---

## 8. Sources

Research pass 2026-06-16 (three independent web surveys). Representative sources:

- Splunk CIM — search-time normalization via TAs: <https://help.splunk.com/en/data-management/common-information-model/6.0/using-the-common-information-model/use-the-common-information-model/use-the-cim-to-normalize-data-at-search-time>
- Elastic ECS — ingest-time normalization: <https://www.elastic.co/elasticsearch/common-schema>
- Microsoft Sentinel ASIM — query-time vs ingest-time: <https://learn.microsoft.com/en-us/azure/sentinel/normalization> · <https://learn.microsoft.com/en-us/azure/sentinel/normalization-ingest-time>
- Google SecOps / Chronicle UDM — ingest-time parsing: <https://docs.cloud.google.com/chronicle/docs/event-processing/parsing-overview>
- OCSF (Linux Foundation): <https://www.linuxfoundation.org/press/open-cybersecurity-schema-framework-ocsf-joins-the-linux-foundation-to-optimize-critical-security-data>
- Splunk Connect for Syslog — metadata-only upstream, TA does parsing: <https://splunk.github.io/splunk-connect-for-syslog/main/sources/Fortinet/>
- Vector — remap/VRL as the transform seam; sinks reference: <https://vector.dev/docs/reference/configuration/transforms/remap/> · <https://vector.dev/docs/reference/configuration/sinks/>
- Loki — label cardinality + LogQL query-time parsing: <https://grafana.com/docs/loki/latest/get-started/labels/> · <https://grafana.com/docs/loki/latest/query/log_queries/>
- Datadog Observability Pipelines — normalize/OCSF before the SIEM: <https://www.datadoghq.com/blog/observability-pipelines-route-logs-microsoft-sentinel/>
- Network syslog formats: Cisco IOS-XE syslog (`%FAC-SEV-MNEM`), FortiOS `key=value`, pfSense/OPNsense `filterlog`
  CSV, MikroTik RouterOS topics — see the per-vendor docs cited in the 2026-06-16 research briefs.

---

## See also
- [logging-capability-vector.md](logging-capability-vector.md) — the BUILT logging capability this extends (the
  `logging/<method>.yml` registry, `gen-logging.py`, the `logs:` block) — the sink-branch is the next ring out
- [../logging-architecture.md](../logging-architecture.md) §3.5 — the dual-purpose Vector/Loki design + the
  canonical label set the keep-raw/cardinality rules here build on
- [telemetry-capability-vector.md](telemetry-capability-vector.md) — the capability-vector model; a `destinations/`
  registry is the same drop-in pattern one layer down
- [../../SECURITY.md](../../SECURITY.md) — C3 (mgmt-bound egress), C8 (at-rest), C12 (label/secret hygiene) — a
  SIEM credential lands as a new SOPS domain here
