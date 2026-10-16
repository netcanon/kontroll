# kontroll — Contributor Directives

Durable instructions for *how* to work in this repo. Loaded every session.
Modeled on the NetConfig project's directive style. Full rationale lives in
[docs/engineering-standards.md](docs/engineering-standards.md); this file is the
binding summary + the never-break rules.

## What this is

A **composed control plane** (not a monolith) for the homelab: Semaphore
(Ansible UI), Homepage (portal), Prometheus + Grafana (metrics), SOPS + age
(secrets). See [PLAN.md](PLAN.md) for architecture and rollout. Your lab's inventory
lives in the private `instance/` overlay (template: `instance.example/`), never in the tool.

## Core doctrine

- **Deeply modular. No god files.** Adding a device/service/dashboard/metric =
  create a new file, never edit a hub file. Every change passes the "add an X"
  test in PLAN.md §2.5.
- **One dispatch seam.** Functional inventory groups (`edge_firewall`,
  `core_switch`, …) + per-host `device_role` are the *only* place that maps a
  host to its role/collection. This is our `get_collector()`.
- **Config-as-data, validated.** Inventory, group_vars, Prometheus targets, and
  dashboard config are data — they must pass `tests/validate` before they run.
- **Fleet-driven config.** Device classes are self-describing modules
  (`modules/<key>/module.yml`); `instance/fleet.yml` selects which are active; the
  bootstrap (`ansible/playbooks/bootstrap.yml`) installs only those. The
  collection list is **generated**, never hand-maintained. See
  [modules/README.md](modules/README.md) + [docs/SETUP.md](docs/SETUP.md).

## Hard Rules (Never Break)

- **Never** commit a secret, age private key, real device password, or API
  token. Secrets live SOPS-encrypted under `instance/secrets/`; keys are
  `.gitignore`d. (See [SECURITY.md](SECURITY.md).)
- **Never** log a secret. Every Ansible task that handles a credential or a
  decrypted SOPS value carries `no_log: true`. Never run scheduled jobs at
  `-vvvv`.
- **Never** apply a state-changing play to a device without a `--check --diff`
  dry-run first — especially the edge firewall and core switch (highest blast
  radius).
- **Never** ship a non-idempotent role. A second apply must report `0 changed`.
- **Never** let one unreachable host fail a fleet-wide run. Use
  `ignore_unreachable: true` (a down host is tolerated, not fatal).
- **Never** scatter per-vendor branching across playbooks. The mapping lives at
  the `device_role` seam only.
- **Never** add a managed device, service, dashboard, or metrics target by
  editing a central hub file when a drop-in file would do.
- **Never** hand-edit `ansible/collections/requirements.generated.yml` (or its
  never-brick trust sidecar `ansible/collections/trust.generated.yml`) or
  re-introduce a hardcoded collection list. Collections derive from enabled
  modules **and active actuation units** (`actuation/<key>/unit.yml`, R2) — change them
  via `modules/` / `instance/fleet.yml` / an actuation unit, then re-run bootstrap.
  Both derive from the **same** `gen-requirements.py`, which stays **offline** — no
  network call in an install-gating generator (the never-brick BRICK-1 invariant).
- **Never** write an identifier of a real deployment — an RFC-1918 address, a MAC, a
  hostname, a domain, an operator-machine path — anywhere but `instance/`. The shipped
  tree uses RFC-5737 TEST-NET / RFC-7042 examples only; `tests/validate` (`pii-guard`,
  `tests/_leak_guard.py`) and the PII-guard workflow enforce it, with your instance's
  own names listed in `instance/leak-tokens.txt`.
- **Never** let a research/review agent edit source or actuate. Agents are
  **read-only** and write **only their one report file** (and, where a run's
  seed lets them measure, files in a scratch copy of their own outside every
  worktree); the **main thread is the only actor that validates and actuates**
  (edits, commits, runs Ansible in a worktree or against the lab, touches the
  lab). See [docs/agent-workflow.md](docs/agent-workflow.md). And
  **never dispatch an agent on a model weaker than the task needs** — set
  `model: opus` explicitly for design/audit/research; `Explore` defaults to Haiku.
- **Never** add or change a test without a docstring stating **what it verifies and
  why** (the failure it guards against), nor add an interactive/asserted GUI element
  without a stable `data-testid` recorded in [tests/testid_reference.md](tests/testid_reference.md).
  E2E selectors key off `data-testid` **only** — never CSS classes or DOM structure.
- **Never** treat tests, test-docs, and logging as optional follow-ups. They are
  **first-class deliverables**: a behaviour change isn't *done* until the test that
  proves it, its docstring, and a `kontroll_run_id`-correlated log line (an audit
  entry for the GUI — **never** the credential) land in the **same commit**. These are
  what let the next iteration (AI or human) continue development safely. Full standard:
  [docs/testing-standards.md](docs/testing-standards.md).
- **Never** land a change without updating the docs it renders stale — **stale
  docs are a bug.** Use the Documentation Sync Checklist below.
- **Never** hard-code a count, file tally, or LOC figure in *prose* docs unless
  the same commit adds a check that keeps it honest. Numbers in test/validate
  assertions fail loudly; numbers in prose rot silently. Prefer "the device-class
  modules" over "the 7 modules."

## Distributed agent work

Multi-agent reviews/audits follow the netcanon two-stage process:
**read-only agents write reports into `docs/reviews/<UTC-date>/` → the main thread
synthesizes, validates, and actuates.** Full protocol, directory layout, cluster
taxonomy, and dispatch heuristics: [docs/agent-workflow.md](docs/agent-workflow.md).

**Ultracode runs use the reusable file-per-agent blackboard runner**
([.claude/workflows/blackboard.js](.claude/workflows/blackboard.js), via the Workflow tool with
`scriptPath: .claude/workflows/blackboard.js`) — never hand-rolled. Each agent writes ONE long-form report under
`docs/reviews/<UTC-date>-<slug>/`, reads peers' reports for comms, and returns only a pointer; the
main thread writes the `00-blackboard.md` seed up front and the `99-synthesis.md` reconciliation after,
then builds. The read-only/one-report-file/main-thread-actuates contract is baked into the runner so it
can't drift. See [docs/agent-workflow.md](docs/agent-workflow.md) § Ultracode runs.

## Documentation Sync Checklist

Treat docs as part of "done" — update in the **same commit** as the change.
Every row exists because someone shipped drift by forgetting it.

| If you change… | …then update |
|---|---|
| A device-class module (`modules/<key>/`) | `modules/README.md` catalog, the role + `secrets_domain`, `config/fleet.example.yml` |
| A role's entrypoints or blast radius | that `roles/<role>/README.md` |
| The collection set / `requirements` mechanism | `modules/`, never a hand-edited list; CLAUDE.md hard rule if changed |
| A secret domain or `.sops.yaml` rule | `instance/secrets/README.md`, SECURITY.md C1 |
| A security control, secret, or trust boundary | `SECURITY.md` (control→file→covering-check + accepted-risks) |
| The bootstrap / setup flow | `docs/SETUP.md`, `docs/bootstrap-control-vm.md` |
| Inventory groups, host membership, IPs | `PLAN.md §6`, `instance/inventory/`, the affected group_vars |
| Anything behaviour-affecting | `CHANGELOG.md [Unreleased]` |
| A `test_*.py` or a fixture | the test's own **docstring** (what it verifies + why); `tests/README.md` if a layer/marker/seam changed |
| An interactive/asserted GUI element | [tests/testid_reference.md](tests/testid_reference.md) — add the `data-testid` (grep-verify it's there before committing) |
| A log surface, `run_id` wiring, audit field, or rotation | [docs/logging-architecture.md](docs/logging-architecture.md); `SECURITY.md` if a trust boundary moved |
| A doc with peers | add the reciprocal "See also" link in the peers **same commit** |
| Anything naming YOUR deployment (a host, domain, address, user) | it belongs in `instance/` — and the name goes into `instance/leak-tokens.txt` so `pii-guard` keeps it out of the shipped tree |
| A vendored third-party file (MIB, dashboard JSON, registry data) | `THIRD-PARTY-NOTICES.md` |

Rule of thumb: if a future contributor could plausibly search for the thing you
added and not find its rationale, there's a doc gap to close.

## Before you commit (checklist)

- [ ] `tests/validate` passes (yamllint + ansible-lint + syntax-check + compose
      config + promtool).
- [ ] New/changed roles have a `backup` entrypoint and an access-chain header.
- [ ] Any state-changing play was run in `--check` and is idempotent.
- [ ] Secret-touching tasks have `no_log: true`.
- [ ] Touched directory's `README.md` updated (Extending + error table if
      behaviour changed).
- [ ] `CHANGELOG.md` `[Unreleased]` has an entry for behaviour-affecting changes.
- [ ] `SECURITY.md` updated if a control, secret, or trust boundary changed.
- [ ] New/changed tests carry a **docstring** (what they verify + why); `pytest -m "not e2e
      and not slow"` is green. (`tests/validate` fails on any undocumented test.)
- [ ] New interactive/asserted GUI elements have a `data-testid` in `tests/testid_reference.md`.
- [ ] Actuating paths emit a `kontroll_run_id`-correlated log line; GUI actions hit the audit
      log (no creds).

## Access-chain discipline

Any state-changing play — in a role or ad-hoc — annotates, in a header comment:

```
# Access chain used:   <how the control node reaches the target>
# May break:           <what path this play can sever>
# Fallback required:   <yes/no — e.g. OPNsense HDMI console, CRS310 MAC-Winbox>
# Blast radius:        <this device / VLAN / LAN / WAN / all>
```

## Commit conventions

Conventional Commits, scope = component (`ansible`, `docker`, `dashboards`,
`prometheus`, `docs`, `repo`). Behaviour-affecting commits get a `CHANGELOG.md`
entry. Tag milestones `vX.Y.Z` (semver).

**Commit body = rationale-first** (netcanon convention): open with *why*, not
*what* — the diff already shows the what. Cite the relevant delta (e.g. "validate
now green at production profile", "ping.yml: false-positive fixed"). End with the
`Co-Authored-By:` trailer. One logical theme per commit.
