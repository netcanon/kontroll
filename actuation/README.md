# `actuation/` — the app-store unit registry

The **third instance of the kontroll drop-in registry idiom** (the others: `modules/<key>/module.yml` =
device classes; `capabilities/<cap>.yml` = post-onboard capabilities). An **actuation unit** is one chosen,
version-pinned **runnable** thing — a collection-shipped playbook, a role, or an in-repo standalone play — that
the GUI app-store flow stages and an operator promotes. Adding a unit touches **zero hub or generated file**: it
passes the PLAN.md §2.5 "add an X" test, exactly like `modules/` and `capabilities/` do.

> **Where the real units live.** A unit is **instance-specific** operator data (a chosen install), so it lives in
> the **private `instance/actuation/<key>/unit.yml` overlay** — resolved via `paths.resolve("actuation")`, stripped
> from any public tree by `make-bundle.sh`'s `instance/` strip. The shipped `actuation/` (this directory) carries
> only this README; until the first unit is promoted into `instance/actuation/`, the registry is empty and the
> generated lockfile is unchanged.

## What a unit DERIVES (and what stays hand-authored)

| Artifact | How it's produced | Freshness |
|---|---|---|
| `ansible/collections/requirements.generated.yml` | `gen-requirements.py` **unions** each active unit's `install.collections` (after `_core` + enabled modules), `resolve_pins` deduping | gitignored, regenerated per-node — never hand-edited |
| `ansible/collections/trust.generated.yml` (the never-brick trust sidecar) | `gen-requirements.py` materializes each collection's `signature` policy + `sha256` anchor (the lockfile is policy-free) — **offline** (the wrapper records the real digest) | gitignored, regenerated per-node — never hand-edited |
| the Semaphore template + role-wrapper play | `gen-actuation.py` (the **push** stage) | committed + `--check`-gated — **lands at R4/R5**, not yet emitted |

> **Never-brick provenance (C15-a).** `provenance.signature` defaults to **`adaptive`** when absent (verify a
> signature where the source serves one, proceed where it doesn't, fail only on a signature that *fails to
> verify*) — so a unit from an unsigned source (all of public Galaxy today) installs rather than bricks. A unit
> with unverified provenance (`signature != required`) may **not** target `edge_firewall`/`core_switch` (the
> Tier-cap, fail-closed at generate). Design of record:
> [docs/reviews/2026-06-25-never-brick-supply-chain/](../docs/reviews/2026-06-25-never-brick-supply-chain/).

The descriptor (`unit.yml`) is the **only** hand-authored/GUI-authored file; the generated artifacts are never
hand-edited (CLAUDE.md hard rule). The descriptor **schema + its fail-closed validator** live in
`kontroll/service/_actuation_schema.py` (`validate_unit`/`validate_knobs` + the closed-enum constants) — the **one
contract** two consumers share: `gen-actuation.py` (the CLI wired into `tests/validate`, so a malformed committed/
staged descriptor never reaches `ansible-galaxy` / never unions a pin) and the app-store **create** seam
(`service/actuation.build_create_unit_plan`), which validates a freshly-rendered descriptor through the **same**
rules *before* it stages (validate-before-stage — a GUI-authored unit is held to exactly the bar a hand-authored
one is).

## Schema — `instance/actuation/<key>/unit.yml`

```yaml
schema: 1                         # descriptor schema version (gen-actuation fails closed on an unknown version)
key: community-docker-prune       # == the directory name; the template/wrapper stem. [a-z][a-z0-9-]*
status: active                    # active | disabled — disabled keeps the file (audit) but emits/unions nothing

unit:
  kind: role                      # collection_playbook | role | standalone   (CLOSED enum)
  collection: community.docker    # namespace.name, lowercase — REQUIRED for collection_playbook/role
  name: prune                     # playbook stem (collection_playbook) | role name (role): [a-z0-9_], at most 64
  # kind: standalone -> drop collection/name; instead:  repo_playbook: ansible/playbooks/<x>.yml  (no install)

install:                          # ABSENT for kind: standalone (nothing to install)
  collections:                    # SAME {name, version} shape modules emit -> unions into requirements
    - name: community.docker
      version: "==3.10.4"         # EXACT '==' pin (NOT a floor) — an app-store install pins a REVIEWED version
  provenance:                     # metadata the never-brick install step consumes — NEVER the lockfile (C15-a)
    source: galaxy                # galaxy | git   (MVP: galaxy only; git is rejected)
    signature: adaptive           # required | adaptive | none  — ABSENT ⇒ adaptive (verify-if-served, pass-if-
                                  #   absent, fail-on-invalid; the brick-proof default). 'required' = fail-on-absence
                                  #   (Tier-cap REQUIRES it for edge_firewall/core_switch — which is why this
                                  #   example targets a group that is neither). 'none' = drop L3.
    keyring: null                 # OPTIONAL host path; null ⇒ the auto-provisioned default keyring (absence ≠ error)
    sha256: null                  # OPTIONAL 64-hex L2b digest; null ⇒ the wrapper records it at first install
    captured_at: "2026-06-24T00:00:00Z"
    captured_by_run: "a1b2c3d4e5f6"   # the kontroll_run_id that staged this unit (audit correlation)

target:
  device_class: docker_host       # modules/<key> — MUST be an ENABLED module (instance/fleet.yml) or fail closed
  inventory_group: docker_hosts   # == module.yml inventory_group; the wrapper play's hosts: (the one dispatch seam)
  blast_radius: LAN               # this-device | VLAN | LAN | WAN | all  (the access-chain SEVERITY, not a group)

knobs:                            # OPTIONAL (R3) — the CURATED configure form (operator-declared, NOT argspec)
  - key: mode                     # ^[a-z][a-z0-9_]*$ (an ansible var name), unique within the unit
    type: enum                    # enum | bool | int | ipv4 | hostname | text  (== service/_validate's set)
    label: "Run mode"            # optional display label (defaults to key)
    allowed: [fast, safe]         # REQUIRED for enum (a non-empty allow-list)
    required: true                # optional; a required knob with no value is rejected at validate
    default: safe                 # optional; a curated default MUST itself validate (can't smuggle a bad value)
    help: "speed vs. safety"     # optional one-liner the renderer shows
  - key: retries
    type: int
    range: {min: 0, max: 5}       # optional bounds for int (min<=max)
  - key: tag
    type: text
    pattern: "[a-z][a-z0-9-]*"   # REQUIRED for text — an unvalidated free field is rejected (no injection)
```

## Lifecycle (rides the existing two-key spine — no new write path)

`GUI stages instance/actuation/<key>/unit.yml under proposed/<run_id>` → human/Semaphore **promotes FF-only** →
`gen-requirements.py` re-derives the lockfile (now with the unit's exact pin) → the next deploy/bootstrap runs the
(R5) **hardened** `ansible-galaxy collection install`. The GUI/API **install nothing themselves**.

**Pin conflict policy** (`resolve_pins`, R2 / report 21 §4.3): a unit's exact `==` pin **wins** over a module
floor for the same collection; **two different exact pins fail closed** (re-pin one). String-equality only — no
SemVer floor math (deferred). **Removal** = `status: disabled` (keeps the audit trail; drops the pin unless a
module/another unit still needs the collection) or deleting the dir.

## Add a unit

1. (Normally) use the GUI app-store flow — **search → create → configure → review → stage**. The **create** stage
   (`POST /actuation/create` → `service/actuation.build_create_unit_plan`/`apply_create_unit`) AUTHORS the
   descriptor for you: it derives the `key` (a `<collection>-<name>` slug), resolves `target.device_class` +
   `inventory_group` from the **declaring module** (`catalog.module_for_collection`), pins the **exact reviewed
   version**, sets `provenance.signature: adaptive`, validates the result through `_actuation_schema.validate_unit`,
   and stages `instance/actuation/<key>/unit.yml` to `proposed/<run_id>` (the C10 spine — never `main`). It **never
   clobbers** an existing unit (409 on collision) and **refuses (403)** to author a unit for a class targeting
   `edge_firewall`/`core_switch` from an unsigned source (the create-time **Tier-cap**; SECURITY.md C15-a). MVP =
   **zero curated knobs** (the worked-extraction mandate blesses a knob-less unit); add a `knobs:` block by hand to
   expose configure-form levers.
2. By hand: drop `instance/actuation/<key>/unit.yml`, run `python3 scripts/gen-actuation.py` (validates) +
   `python3 scripts/gen-requirements.py` (re-derives the lockfile). `tests/validate` runs both.

See also: [modules/README.md](../modules/README.md) (the device-class registry this mirrors),
[docs/reviews/2026-06-24-gui-actuation-design/](../docs/reviews/2026-06-24-gui-actuation-design/) (the design of
record — report 21 owns this registry; report 40 the worked-extraction scope).
