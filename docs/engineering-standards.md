# Engineering standards

These standards are **distilled from the NetConfig project**
(the netcanon repository, read-only reference) and translated from
a Python application to this infrastructure-as-code project. They are binding:
follow them every session without being asked. The hard, never-break subset is
mirrored in [/CLAUDE.md](../CLAUDE.md).

The throughline that makes the translation clean: **both projects are
config-as-data with a single dispatch seam.** NetConfig loads YAML device
*definitions* and dispatches to a collector via the `get_collector()` factory;
kontroll loads YAML *inventory* and dispatches to a role via `device_role`.
Everything below follows from treating config as a tested, validated, documented
artifact — not as throwaway glue.

---

## 1. Testing — the IaC test pyramid

NetConfig uses a four-layer pyramid (unit → integration → e2e → desktop) with
strict markers, fail-fast, and isolation per layer. We have no Python app to
unit-test, but the *shape* maps directly onto infrastructure code:

| NetConfig layer | kontroll layer | What runs | Speed / when |
|---|---|---|---|
| unit (pure, no I/O) | **L1 lint** | `yamllint`, `ansible-lint`, `--syntax-check`, `docker compose config`, `promtool check config`, `sops --verify` | sub-second; every commit (pre-commit + CI) |
| integration (TestClient, mocked SSH) | **L2 validate** | playbooks parsed against a **mock inventory** (`--syntax-check`); the pytest suite. Evaluating role/playbook logic with `--check --diff` in CI is the open item (docs/qa-and-release-pipeline.md §1) | seconds–minutes; CI on change |
| e2e (live server, Playwright) | **L3 live smoke** | `ping.yml` reaches the real fleet; post-deploy asserts (service responds, Prometheus target `up`, Homepage 200) | live; manually / Semaphore schedule, never in blind CI |
| desktop (mocked shell) | **L4 idempotence** | run the play **twice**; second run must report **0 changed** | per role; the IaC analog of determinism |

### Rules (mirroring NetConfig's hard rules)

- **Mock at ONE seam.** NetConfig forbids patching `ConnectHandler`/`SSHClient`
  directly — tests patch `get_collector` only. Our seam is the **functional
  inventory group + `device_role`**: tests/dry-runs target a `mock` inventory
  or `--limit`, never hard-coded IPs. One place to swap real ↔ fake.
- **Check-mode before live.** Any play that changes a device runs
  `--check --diff` first. No exceptions for edge devices (OPNsense/FortiGate)
  or the switch — those are highest blast radius (see [SECURITY.md](../SECURITY.md)).
- **Idempotence is a test, not a hope.** A role is "done" only when a second
  apply is a no-op. Non-idempotent tasks are bugs.
- **One bad host never fails the fleet.** `ignore_unreachable: true` on
  fleet-wide plays (a down host is tolerated). NetConfig's loader does the
  same: one bad definition file → WARN + skip, never crash the load.
- **Markers = tags.** Use Ansible `--tags`/`--skip-tags` to slice runs the way
  pytest markers slice the suite (`backup`, `check`, `risky`). A `risky` tag
  gates anything that can sever the control path.

### Lint strictness — graduated, not all-at-once

`ansible-lint` runs at the **`moderate`** profile while the repo is mostly
scaffold — stricter profiles flag premature gaps (missing role meta, etc.) that
are noise on a young repo and train you to ignore the linter. **Graduation
trigger: flip to `safety` at the Phase 2 exit**, once the device-class roles
carry real logic worth holding to the correctness-class rules (unsafe shell
interpolation, swallowed pipe failures, `changed_when` lies). Go to `shared`
only if a role is ever published. NetConfig runs a strict suite because it is a
mature codebase that already paid that cost; we pay it as the roles land.

### Layout

```
tests/
├── README.md            this pyramid, how to run, how to add
├── mock-inventory.yml   fake hosts for L2 check-mode (no real IPs)
└── validate.ps1 / .sh   L1+L2 runner (lint + syntax + config-validate)
```

---

## 2. Documentation

NetConfig documents at three altitudes; we adopt all three.

### 2a. Internal (for whoever edits the repo)

Every meaningful directory carries a `README.md` with the same skeleton
NetConfig uses:

1. **What it is** (one paragraph).
2. **Architecture** (the files + the single entry point/seam).
3. **Extending — "Adding an X"** (the additive, drop-a-file recipe — this is
   also the modularity test in PLAN.md §2.5).
4. **Error-handling / behaviour table** (every notable condition → behaviour),
   exactly like the loader README's table.
5. **Testing notes** (how this piece is exercised; which L1–L4 layers apply).

Roles additionally document their **entrypoints + blast radius** (the §9 access-chain header is the role-level analog of a method docstring's `Raises:`).

### 2b. Code-level (docstring equivalent)

NetConfig puts a module docstring + Google-style `Args/Returns/Raises` on every
public function. The IaC equivalent:

- **Every playbook and role `tasks/*.yml` opens with a header comment**: purpose,
  the access-chain block (used / may-break / fallback / blast-radius), and an
  **idempotence note**. This is non-optional for any play that changes state.
- Variables carry inline comments where intent isn't obvious; secrets are
  *referenced*, never inlined (see §3).

### 2c. Security (`SECURITY.md`)

We keep a [SECURITY.md](../SECURITY.md) modelled on NetConfig's: a threat model,
each control mapped to **the file that implements it and the check that proves
it**, an accepted-risks table, and an explicit **"update this document when…"**
trigger list. Security claims without a covering check are not claims.

### 2d. Changelog

[CHANGELOG.md](../CHANGELOG.md), `[Unreleased]` at top, one entry per
behaviour-affecting change — same discipline as the migration repo and
NetConfig.

### 2e. Drift prevention (netcanon discipline)

- **Documentation Sync Checklist** ([CLAUDE.md](../CLAUDE.md)) — a `change X →
  touch Y` table, updated in the *same commit*. Stale docs are a bug.
- **Cross-reference reciprocity** — when you add a doc, add the reciprocal "See
  also" link in its peers in the same commit (one-way links rot fastest).
- **No hard-coded counts in prose** unless a check keeps them honest (test/validate
  numbers fail loudly; prose numbers rot silently).

### 2f. Distributed agent work

Multi-agent reviews follow [agent-workflow.md](agent-workflow.md): read-only
agents write reports into `docs/reviews/<UTC-date>/`; the **main thread is the
only actor that validates and actuates.** This is the documentation-altitude
analog of the access-chain rule — fan-out is cheap and safe; mutation is
centralized and verified.

---

## 3. Logging & secret redaction

NetConfig's `logging_config.py` is the model. Two contexts apply to kontroll.

### 3a. Ansible runs

- **Central log path**: `ansible.cfg` sets `log_path` so every run is captured
  with timestamps (the analog of NetConfig's structured formatter).
- **`no_log: true` on every task that touches a secret** — a device password,
  an API token, a decrypted SOPS value. This is the direct translation of
  NetConfig's hardest logging rule: *credentials are never logged* (they
  enforce it with `tests/unit/test_logging_config.py`; we enforce it by lint
  convention + review, and a grep check in `validate`).
- **Level discipline** mirrors NetConfig (INFO = lifecycle/connect/result,
  DEBUG = per-command). Ansible: default output = INFO-ish; `-v/-vv` = DEBUG.
  Never run scheduled jobs at `-vvvv` (it can echo secrets despite `no_log`).

### 3b. The Docker stack + any glue code

- Each service (Semaphore, Grafana, Prometheus) gets an explicit **log level and
  rotation** policy in its compose fragment or config — don't inherit unbounded
  defaults. NetConfig rotates at 5 MB × 3; match that order of magnitude.
- **Suppress third-party noise** the way NetConfig pins paramiko/uvicorn.access
  to WARNING (e.g. Prometheus/Grafana debug chatter stays off in normal ops).
- **Any custom Python glue** we ever add copies `logging_config.py` verbatim in
  spirit: one idempotent `configure_logging()`, `logger = getLogger(__name__)`
  per module, structured format, rotation, no secrets.

> The full current-state map + the layered target (durable/bounded/structured
> foundations → Vector → Loki → Grafana, secret-safe) lives in the plan of record:
> [logging-architecture.md](logging-architecture.md).

---

## 4. General engineering principles

Carried over from NetConfig, stated as kontroll rules:

- **Config-as-data, schema-validated.** Inventory, group_vars, Prometheus
  targets, and Homepage/Grafana config are *data*. They are validated in CI
  (L1) before they can break a run — NetConfig validates every definition
  against a Pydantic schema on load; we validate every YAML against its schema
  in `validate`.
- **Fail soft on load, fail closed on secrets.** A malformed *target* file
  warns and is skipped (availability); a missing/invalid *secret* aborts the
  run (safety). NetConfig does exactly this split.
- **One dispatch seam.** New device class = new role + one membership line;
  the dispatch (`device_role`) is the only place that knows the mapping —
  mirroring `get_collector()` as the sole collector factory. Never scatter
  per-vendor branching across playbooks.
- **Defence in depth.** NetConfig gates file access with regex *and*
  path-resolution. We gate the control plane with SOPS-at-rest *and* scoped
  firewall rules *and* `no_log` *and* `.gitignore` for keys — no single
  control is load-bearing alone.
- **Additive growth, no god files.** The PLAN.md §2.5 "add an X" test is the
  same instinct as NetConfig's per-README "Extending" sections: capability is
  added by creating a file, never by editing a hub.

---

## 5. What we deliberately did NOT adopt

NetConfig practices that don't fit an IaC control plane:

- **Two-platform (web/desktop) parity** — kontroll has one deployment target.
- **`data-testid` selectors / Playwright for *third-party* UIs** (Homepage, Grafana,
  Semaphore) — those remain HTTP/up-ness smoke probes (L3), not DOM testing.
  **Note (since-adopted):** kontroll's *own* GUI (`gui/`) is first-party and now DOES
  carry `data-testid` selectors with a Playwright e2e suite (`tests/e2e/`) — see
  [testing-standards.md](testing-standards.md) §3. This exclusion is scoped to the
  third-party panes only.
- **Fernet + OS keyring credential store** — that's NetConfig's app-internal
  secret model; ours is SOPS + age (a deliberate, documented choice in
  PLAN.md §7). The *principle* (encrypt at rest, never log, never commit) is
  identical; the mechanism differs.

## See also
- [/CLAUDE.md](../CLAUDE.md) — the never-break subset of these standards
- [testing-standards.md](testing-standards.md) — tests, test-docs, the data-testid SOP, the test-after-change loop
- [qa-and-release-pipeline.md](qa-and-release-pipeline.md) — CI/QA + build/dissemination plan
- [logging-architecture.md](logging-architecture.md) — the logging plan §3 implements
- [../tests/README.md](../tests/README.md) — test-suite layout and how to run it
</content>
