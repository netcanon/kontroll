# `capabilities/` — the secondary-capability registry (drop-ins)

Each `capabilities/<cap>.yml` is a **secondary-capability descriptor**: a flat drop-in that parameterizes
the *one* shared post-onboard dialog / route / promote spine for a capability — **telemetry** (instance #1),
**backup** (instance #2), and any future capability (secrets-rotation, compliance-scan). The shell dispatches
over these descriptors with **zero `if cap=="…"` branching**; that is the seam.

A *secondary* capability is one added **after** onboarding, with its own standalone dialog, that **never gates
onboarding** (INVARIANT D\*). Contrast the *primary* capability — making a device Ansible-usable — which the
onboard path owns. See the design of record:
[docs/observability/secondary-capability-dialog.md](../docs/observability/secondary-capability-dialog.md).

## The four-part shape every capability has

1. a **vector** (`vectors/<cap>.yml`) detects suitability — a read-only, non-binding suggester;
2. a **declared block** in `modules/<key>/module.yml` is the source of truth (`metrics:` / `backup:`);
3. a **generator** (`gen-<cap>.py`) turns the block into the actuation artifact;
4. a **standalone post-onboard dialog** (propose-then-promote) actuates it.

The descriptor wires those four together. The schema (every field, with telemetry + backup worked examples)
is **§2 of the DoR**; the loaders are `catalog.load_capabilities()` (sorted by `order`) and
`catalog.registered_capabilities()` (the registry keys the route loop + the INVARIANT D\* parametrized pins
iterate).

## Adding a capability (the "add an X" test — PLAN.md §2.5)

Adding a capability is **only**:

| New / changed | What |
|---|---|
| `capabilities/<cap>.yml` | this descriptor (a Stage-3 `resource_stage.testid` is `cap-<cap>-…`; the server drops any other — T22) |
| `vectors/<cap>.yml` | the detection vector |
| `scripts/gen-<cap>.py` | the generator (block → actuation artifact) |
| `classify.py` | a `suggest_<cap>` / `declared_<cap>` pair (resolved by import-by-convention) |
| `modules/<key>/module.yml` | the per-class declared block, when an operator wires that class |

It is **never** an edit to: the dialog shell (`openCapabilityDialog`), `/api/capability/<cap>`, `promote.py`,
the generic INVARIANT D\* pins, the privileged tripwire, or `configure-semaphore.py`. If registering a new
capability forces one of those edits, the seam has failed its own modularity test.

## Current registry

| `<cap>` | order | block | status |
|---|---|---|---|
| [`telemetry.yml`](telemetry.yml) | 10 | `metrics:` (list) | instance #1 — built Capability-track Phase 7 |
| `backup.yml` | 20 | `backup:` (mapping) | instance #2 — Capability-track Phase 8 (not yet) |
