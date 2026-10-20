# snmp_exporter config — generated `if_mib` modules + SOPS-templated auths

The control-node `snmp-exporter` (agent-less SNMPv3 read-only walks of the Cisco switch / FortiGate, on
Prometheus's behalf) reads `snmp.yml` — which kontroll **composes at deploy** from two halves with a crisp
secret boundary:

| File | Tracked? | Holds | Produced by |
|---|---|---|---|
| `generator.yml` | ✅ committed | the `if_mib` **input spec** (which MIB objects to walk) | hand-edited (the drop-in seam) |
| `mibs/*.txt` | ✅ committed | the vendored **public IETF MIB closure** (pinned) | `mibs/` recipe (net-snmp v5.9 + IANA) |
| `modules.generated.yml` | ✅ committed | the **generated** `modules:` block (no secret) | `scripts/gen-snmp.py` (CGO generator) |
| `snmp.yml.j2` | ✅ committed | the `auths:` block (SOPS placeholders) **+** `{% include 'modules.generated.yml' %}` | hand-edited |
| `snmp.yml` | 🚫 gitignored, 0600 | the **rendered, creds-bearing** final config | `deploy-stack.yml` (`no_log`) |

## Why generated (the no-bespoke tenet)

The `if_mib` OID set is **derived from public IETF MIBs**, not hand-authored — so MIB / vendor churn becomes a
reviewable diff (re-run the generator, see what changed, validate, commit), never silent rot. This is the same
`committed input spec → generated artifact → --check` shape as every other `gen-*` in the repo; see
[no-bespoke-config tenet](../../../docs/engineering-standards.md) and [99-synthesis](../../../docs/reviews/2026-06-17-snmp-generator/99-synthesis.md).

The artifact reproduces the **exact** 6-metric / 7-OID scrape the previously hand-authored block produced
(behaviour-preserving migration) — pinned by [`tests/unit/test_gen_snmp.py`](../../../tests/unit/test_gen_snmp.py)
against a frozen oracle.

## The secret boundary

`modules.generated.yml` is **modules-only and PUBLIC** (IETF OIDs — not a secret). The SNMPv3 USM credentials
(`instance/secrets/snmp_observability.sops.yml`, the `v3_kontroll` auth) are SOPS-decrypted and templated into
the **gitignored, 0600** `snmp.yml` by the `deploy-stack.yml` render task (`no_log`). `gen-snmp.py` strips the
generator's verbatim-copied `auths:` block, so no credential can ever reach the committed artifact. The generated
module carries no auth reference; auth binds at scrape time via `__param_auth=v3_kontroll` (`telemetry/snmp.yml`).

Editing `snmp.yml.j2`: the template module renders it with `trim_blocks`, so never put a `-` on a tag next to
`auths:` — one that also ate the newline before it joined `auths:` onto the comment line above, and every deploy
from 2026-07-28 to 2026-10-20 rendered the credentials commented out. The credentials go in through `to_json`, so a
password holding `#`, `!`, `: ` or `yes` reads back as itself. A render that changed the file restarts a running
`snmp-exporter` after `up`: the file bind holds the file the container started with.
[`tests/unit/test_snmp_config_render.py`](../../../tests/unit/test_snmp_config_render.py) renders with the module's
settings.

## Pinned versions (bump together)

- **snmp_exporter generator: `v0.30.1`** (built from source — it is NetSNMP/CGO, no static binary / no reliable
  image, upstream #944).
- **runtime image `prom/snmp-exporter:v0.30.1`** (`telemetry/snmp.yml exporter.image`) — generator version MUST
  equal exporter version (the config format has breaking changes across releases). Bump both in one commit.

## Regenerating `modules.generated.yml` (CI / VM only — NOT Windows)

The generator is CGO (libsnmp), so regeneration runs in CI or on the control VM, never on the Windows author box
(same tier as `promtool`/`ansible-lint`/`vector`):

```bash
# 1. build the pinned generator (Linux, needs libsnmp-dev + Go)
sudo apt-get install -y libsnmp-dev build-essential
git clone --branch v0.30.1 --depth 1 https://github.com/prometheus/snmp_exporter /tmp/snmp_exporter
( cd /tmp/snmp_exporter/generator && CGO_ENABLED=1 go build -o /usr/local/bin/snmp-generator . )

# 2. regenerate (gen-snmp.py strips the banner + auths:, re-dumps modules-only, LF)
KONTROLL_SNMP_GENERATOR=/usr/local/bin/snmp-generator python3 scripts/gen-snmp.py
```

Then commit the regenerated `modules.generated.yml`. CI enforces freshness two ways:

- **`gen-snmp.py --check`** (binary-free, in `tests/validate.sh`) — present, modules-only, no secret, `if_mib`.
- **the `snmp-modules` CI job** (`.github/workflows/ci.yml`) — rebuilds with the pinned generator + the vendored
  MIBs and `git diff --exit-code` (the byte-exact "matches a fresh rebuild" gate).

## Errors

| Symptom | Cause | Fix |
|---|---|---|
| `validate` `gen-snmp` STALE/INVALID | `modules.generated.yml` missing / not modules-only / a credential leaked | regenerate (above); never hand-edit |
| `snmp-modules` CI job red (diff) | `generator.yml` or a MIB changed without regenerating | regenerate + commit the artifact |
| generator `fail-on-parse-errors` | a missing MIB import (e.g. `SNMPv2-CONF`) in `mibs/` | vendor the full closure (`mibs/README.md`) |
| `test_gen_snmp` faithful-migration fail | the generated module drifted from the 7-OID oracle | fix `generator.yml` (walk/lookups/overrides), don't weaken the test |

See also: [`mibs/README.md`](mibs/README.md) · [`telemetry/snmp.yml`](../../../telemetry/snmp.yml) ·
[`docs/observability/agent-less-monitoring.md`](../../../docs/observability/agent-less-monitoring.md).
