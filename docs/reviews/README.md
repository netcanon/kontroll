# docs/reviews/ — design and review dossiers

Multi-agent reviews, audits and design runs write their reports here, one folder per run
(`<UTC-date>-<slug>/`), following [docs/agent-workflow.md](../agent-workflow.md): read-only
agents each write one report, the main thread writes the `00-blackboard.md` seed and the
`99-synthesis.md` reconciliation, and only the main thread acts on the result.

**Where the pre-release dossiers are.** Everything dated before the public release
(2026-10-08) was produced against the maintainer's private instance and quotes its real
topology, so those folders live only in the private development repository. Links into
them from `CHANGELOG.md` and the design docs are kept for provenance and will not resolve
here. A dossier written after the release **may** be published in this directory; none has
been yet, and one that quotes a deployment stays in that instance's repository. A published
dossier must pass the same identifier-leak gate as every other tracked file
(`tests/_leak_guard.py`, run by `tests/validate.sh` and the PII-guard workflow; on a public
tree the scanner covers this directory, and skips it only on an instance repository, whose
own dossiers quote its deployment) — a report may quote example data, never a real address
or hostname.

## Index

One row per run, added by the main thread when it writes the run's seed and completed when it
writes the synthesis ([../agent-workflow.md](../agent-workflow.md)). The row is what makes a
run findable and its decision supersedable; `tests/unit/test_review_loop.py` fails on a run
folder dated 2026-10-12 or later that has none (earlier runs are not backfilled). This table
lists **published** dossiers. An instance repository keeps its own index in
`docs/reviews/INDEX.md` — a file that never exists in the public tree — so that the two
repositories never edit the same table.

| Date | Run | Loop | Decision | ADR |
|---|---|---|---|---|

## See also
- [../agent-workflow.md](../agent-workflow.md) — the process: which loop a change gets, the run folder, the synthesis
- [../adr/README.md](../adr/README.md) — decision records and their index
