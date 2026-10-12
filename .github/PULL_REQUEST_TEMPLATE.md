## Summary

<!-- One or two sentences: what changed and WHY (rationale first — the diff shows the what). -->

## Scope

- [ ] Control-plane code (`scripts/kontroll/`, `api/`, `gui/`)
- [ ] A device-class module / role (`modules/`, `ansible/roles/`)
- [ ] A registry descriptor (`telemetry/`, `logging/`, `discovery/`, `capabilities/`, `actuation/`)
- [ ] Generated artifacts regenerated (`gen-*.py`, never hand-edited)
- [ ] Documentation only

## Review loop

Which loop this change gets — `docs/agent-workflow.md`, "Which loop a change gets". Tick one.

- [ ] **Light** — it adds no route, request field, outbound client, container privilege, secret domain or
      recipient rule; it does not change what reaches `main` without a human; `SECURITY.md` stays true as it is.
      If it edits a file of the boundary table, say in one line why none of that applies:
- [ ] **Full** — a security-boundary change. Boundary:

## Doc-sync checklist

Audit CLAUDE.md's "Documentation Sync Checklist" — every row that applies to this change has
its target doc updated **in this same PR**.

- [ ] A hard rule / doctrine changed → `CLAUDE.md`
- [ ] A security control, secret or trust boundary changed → `SECURITY.md`
- [ ] A module / role / collection mechanism changed → `modules/README.md`, the role README
- [ ] An interactive GUI element was added → `tests/testid_reference.md` (`data-testid`)
- [ ] A log surface / `run_id` wiring changed → `docs/logging-architecture.md`
- [ ] Behaviour-affecting → `CHANGELOG.md [Unreleased]`

## Tests

- [ ] `bash tests/validate.sh` is green locally (lint · secrets · compose · generators · pytest · pii-guard)
- [ ] New/changed tests carry a docstring (what they verify + why)
- [ ] `pytest tests/e2e -m e2e` run — only if the GUI changed
- [ ] CI is green

## Identifier hygiene

- [ ] No real address, MAC, hostname, domain or operator path outside `instance/` (TEST-NET / RFC-7042 /
      `example.com` only); any deliberate exception carries `pii-guard: allow <reason>`
- [ ] No hard-coded count or LOC figure in prose docs (or a CI guard was added)

## Developer Certificate of Origin

- [ ] I certify the [DCO 1.1](https://developercertificate.org/) for this contribution and my commits are signed off (`git commit -s`).

## Linked issues

Closes #
