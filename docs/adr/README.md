# docs/adr/ — decision records

A decision record (ADR) is one page that says what was decided, why, and what would change it. It exists so that
a later reader can find a decision without reading the review that led to it, and so that reversing it is a
visible act — a new record — and not a quiet edit.

## When one is written

When a decision was made that someone could later reverse: a boundary drawn, an approach chosen over a named
alternative, a default set. Not for a bug fix, a drop-in, or a finding that needed no choice — the pull request
and the CHANGELOG are the record of those. A full review run ([../agent-workflow.md](../agent-workflow.md)) that
ends in a decision gets one, and its synthesis names it.

## The rules

- **One number, once.** A record is `NNNN-<slug>.md`, numbered in order. A number is never reused.
- **A record is not edited to match the present.** A later decision supersedes it under a new number, and the
  old record's status line says by which. A correction of fact is a dated note appended at the end.
- **Status** is one of `PROPOSED`, `ACCEPTED` or `SUPERSEDED` (`SUPERSEDED by ADR-NNNN`), on a line that starts
  with `**Status:**`.
- **Every record has a row in the index below.**
- A record quotes example data only — never an address, a host name or a domain of a deployment. The
  identifier gate scans this directory like any other.

`tests/unit/test_review_loop.py` fails on a record without an index row, on two records with one number, and on a
status line that holds none of the three words.

## The template

```markdown
# ADR-NNNN — <the decision, as a sentence>

**Status:** PROPOSED (<UTC-date>)
**Decided by:** <the maintainer / the main thread>
**Supersedes:** <ADR-NNNN, or nothing>
**Evidence:** <docs/reviews/<UTC-date>-<slug>/99-synthesis.md, a pull request, a live run>

## The question

## Decision

## Why

## What would change this decision
```

The blackboard runner returns this stub pre-filled for the run it just finished (`bookends.adr_stub` —
[../../.claude/workflows/README.md](../../.claude/workflows/README.md)); the main thread writes the file.

## Index of decisions

| ADR | Decision | Status | Date | Rests on |
|---|---|---|---|---|
| 0001 | Reconcile status is shown on the object it belongs to, not in a separate "Installed" index | ACCEPTED | 2026-07-25 | recorded in the homelab edition, whose install-on-onboard flow it concerns — not a file in this tree |
| 0002 | The composed-plane boundary: what kontroll builds itself, and what it leaves to the tools it composes | not yet written | — | awaits the maintainer's decision |
| 0003 | How far the generic onboarding path goes: a service root, a module SDK, the kinds of actuation unit | not yet written | — | awaits the maintainer's decision, after the next full re-onboard of a real fleet |
| 0004 | The default promotion posture of the homelab edition | not yet written | — | awaits the maintainer's decision; its subject is homelab-edition code, so the record will live there |
| 0005 | The upgrade story: an in-place update, or rebuild only | not yet written | — | awaits the maintainer's decision |

Numbers 0002 to 0005 are reserved for those four subjects. They are the open decisions the 2026-10 project
review named; reserving the numbers lets another document point at a decision before it is written. A new
decision takes the next number after them.

## See also
- [../agent-workflow.md](../agent-workflow.md) — which review loop a change gets, and how a run's synthesis points at its record
- [../reviews/README.md](../reviews/README.md) — the dossiers a record rests on, and their index
- [../../SECURITY.md](../../SECURITY.md) — controls and accepted risks; a decision that changes one is recorded there as well
