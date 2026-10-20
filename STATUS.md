# kontroll — status

<!-- One page (tests/check-status.py refuses a longer one). Every line is written by hand; the three dated lines
     are compared with the tree by that check, which warns when the page is a month old and refuses a release
     then. No tally of anything in the tree: name the registry, never a count. Arcs are named, never numbered. A
     line that names a deployment belongs in that deployment's instance/OPERATIONS.md, and the leak gate refuses
     it here. A "What works" line names its proof; a line that cannot moves to "never run live". -->

**As of:** 2026-10-19
**Images:** v0.1.2 — the release whose published images this tree deploys (`docker/images.lock.yml`)
**Last proven live:** 2026-07-28 — a control node rebuilt from scratch, and the fleet's backups, on the reference lab

The dated record is [CHANGELOG.md](CHANGELOG.md); decisions are in [docs/adr/](docs/adr/README.md). A deployment's
own state (its boxes, keys and standing rules) is that deployment's `instance/OPERATIONS.md`, never this file.
Ids such as T16 or S22 are items of the maintainer's plan, which is not published; each line says what is wrong.

## What works

- The composed stack deploys from one command and previews with `--check --diff`, a brand-new node included —
  `tests/unit/test_deploy_stack_check_mode.py`, `tests/unit/test_fresh_node_preview.py`.
- Device classes are data: the fleet file selects them and the collection list is generated —
  `tests/unit/test_deploy_stack_requirements.py`, `tests/unit/test_gen_*.py`.
- A device is onboarded from the GUI or the API with no task code of its own — `tests/e2e/test_onboard_flow.py`,
  `tests/integration/test_api_onboard.py`.
- Every network-reachable change is a proposal; promoting it to `main` is a second, separate key, and the gate
  judges the one commit it lands — `tests/unit/test_kontroll_promote.py`, `tests/unit/test_canon.py`. Every other
  way into `main` (the update scripts, the CLI, the deploy) lands through the same gate; none pushes —
  `tests/unit/test_land.py`.
- Secrets are SOPS-encrypted per domain; the secret and onboard routes never return or audit a value —
  `tests/integration/test_api_secrets.py`, `tests/integration/test_api_onboard.py`,
  `tests/integration/test_plaintext_guard_sops.py`.
- Nothing that names a deployment ships — `tests/unit/test_leak_guard.py` and the PII-guard workflow.

## Known broken, or never run live

- Everything merged since v0.1.2 has run in CI only, on no live box → the next release (T30).
- A collection the classifier cannot place is onboarded onto the raw-SSH backend without saying so → T16.
- A backend with no role can be chosen at onboard → T15.
- `backend_params` are written at onboard and read by nothing → the parameter channel (S22).
- Every host is probed on port 22 → T17.
- The onboard form's "install + verify (after promote)" checkbox changes nothing → not yet scheduled.
- There is no break-glass recipient (SECURITY.md, "Break-glass") → T11.
- No gate checks that a task handling a credential carries `no_log` → SECURITY.md C2 (planned).
- A second apply reporting `0 changed` is checked by hand, and no run of it is recorded here → not yet scheduled.

## Next

1. The onboarding lane says what is true: refuse a backend nothing can run, then a fail-closed classifier with a
   generic SSH card (T15, T16).
2. A break-glass recipient from an instance's first commit (T11).
3. A release after v0.1.2, with the CHANGELOG cut (T30).

## Waiting on a decision

- The composed-plane boundary → ADR-0002 ([docs/adr/README.md](docs/adr/README.md)).
- How far the generic onboarding path goes → ADR-0003, after the next full re-onboard of a real fleet.
- The upgrade story: in-place update, or rebuild only → ADR-0005.

## Leading indicators

| Indicator | How to read it | Red when |
|---|---|---|
| Work on no remote | `git rev-list --count --branches --not --remotes`, in each working clone | anything, for longer than a day |
| Fixes not released | `git log --oneline "$(git describe --tags --abbrev=0)"..origin/main` | a fix unreleased for longer than a month |
| Last proven live | the date at the top of this page | older than two weeks while "Next" is not empty |

## Where the rest is

[CHANGELOG.md](CHANGELOG.md) · [docs/adr/](docs/adr/README.md) · [docs/reviews/README.md](docs/reviews/README.md) ·
[SECURITY.md](SECURITY.md) · [docs/SETUP.md](docs/SETUP.md) · [PLAN.md](PLAN.md) · [README.md](README.md)
