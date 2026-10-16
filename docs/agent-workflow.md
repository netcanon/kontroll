# Distributed agent work — how we run reviews & audits

Replicated from the **netcanon** project's methodology (security-triage /
docs-audit sister-processes), adapted to this IaC control-plane repo. This is the
binding process for any multi-agent review, audit, or research task here.

The one-line shape:

> **snapshot → cluster scope files → parallel read-only agents → orchestrator
> synthesis → fix-plan → main-thread actuation → evidence-trail commit**

## The load-bearing rule: read-only agents, main thread actuates

- **Stage-1 agents are strictly read-only on the repo and the lab.** An agent may
  read anything; it may **write ONLY its one designated report file** under the
  run folder (and, where the seed lets it measure, files inside its own scratch
  copy — below). No source edits, no `git add`/commit, no `ansible-playbook` in
  a worktree or against the lab, no touching the live fleet. No worktree
  isolation is needed precisely *because* they edit no worktree (saves disk +
  dispatch cost).
- **Measuring is done on a copy, in containers named for the agent.** A run's seed (or a single reader's task)
  may let an agent measure — run the tests, a single-fix reversion, a probe. The agent then works on a scratch
  copy of the tree it reads (`git archive` of the frozen worktree into `<the seed's scratch dir>/<report id>/`,
  outside every worktree), never in a worktree and never against the lab; read-only git (log, show, diff,
  archive) may read a worktree, writing only to stdout or into the copy. Every container it starts carries a
  name of its own (`--name <report id>-…`), mounts only that copy (never a worktree, `instance/`, a key or the
  docker socket), and is stopped or removed by that name only, never by image or by a pattern over `docker ps`;
  it prunes nothing, and an image it builds carries a tag of its own. Readers run side by side: one reader's
  filter on an image name once stopped another reader's mutation containers, and a stopped container's exit
  status read as "caught". The runner's preamble says the same.
- **The orchestrator (main thread) is the only actor that validates and
  actuates.** It reads the agents' reports, reconciles them into a synthesis +
  fix-plan, and then applies fixes, commits, and runs anything against the lab —
  itself, or by dispatching **Stage-2 implementation agents** *only* where the
  fix scope genuinely warrants it (and those use worktree isolation if they edit
  shared files in parallel).

This is the same split as our existing discipline (`PLAN.md` access-chain rule):
research fans out cheaply and safely; mutation is centralized and verified.

## Ultracode runs — the Workflow-tool blackboard (the consistent mechanism)

Ultracode runs (the `ultracode` keyword opts a turn into multi-agent orchestration) operationalize the process
above through the **`Workflow` tool** + the reusable runner
[../.claude/workflows/blackboard.js](../.claude/workflows/blackboard.js) (invoked by **`scriptPath`** —
`Workflow({ scriptPath: "<repo>/.claude/workflows/blackboard.js", args })`; the `name:` registry is reserved
for built-in/plugin workflows). **This runner is the only sanctioned shape** — it bakes the
load-bearing rule in (read-only agents, one report file each, main thread actuates) so the discipline can't be
re-hand-rolled or drift. The `args` contract + invocation recipe live in
[../.claude/workflows/README.md](../.claude/workflows/README.md); this section is the convention it implements.

**Run folder** = `docs/reviews/<UTC-date>-<slug>/` (the topical `-<slug>` distinguishes a research+design run
from a whole-repo audit, which may drop the slug — same family, phase-appropriate file names):

```
docs/reviews/<UTC-date>-<slug>/
  00-blackboard.md            # SEED — the MAIN THREAD writes this BEFORE the run: mission, hard constraints, roster
  10-research-<x>.md          # Stage-1 research agents (10s) — each its ONLY write
  11-research-<y>.md
  20-design-<a>.md            # Stage-1 design agents (20s) — read the 10s for peer comms
  21-design-<b>.md
  30-review-adversarial.md    # adversarial review (30s) — reads all 10s + 20s
  99-synthesis.md             # SYNTHESIS — the MAIN THREAD writes this AFTER: reconciled decisions + buildable-now
```

- **Numeric prefixes encode phase order** (10s research → 20s design → 30s review); the prefix is the agent's
  `id` *and* its report filename. Phases run as `parallel()` barriers; later phases read earlier reports for comms.
- **The runner has NO filesystem access** — it only orchestrates agents. So the **main thread owns the
  bookends**: write `00-blackboard.md` (the seed) and the run's **index row**
  ([reviews/README.md](reviews/README.md)) before invoking; after the run write `99-synthesis.md` and — when the
  run ended in a decision someone could later reverse — a **decision record** ([adr/README.md](adr/README.md));
  then build / validate / commit — the sole actuator. The runner returns the text to start each from
  (`bookends`). Agents write only their one `NN-*.md`.
- **Agents return only a pointer/summary**; the long-form analysis lives in the file (keeps the main thread
  light). Reviewers return a verdict + severity-tagged must-fixes.
- **Opus for every agent** (the runner's default) — long-context retention + design/audit quality (CLAUDE.md:
  never under-model a design/audit/research agent).

### The `00-blackboard.md` seed template (the main thread fills + writes this before invoking)

```markdown
# Blackboard — <mission title> (<UTC-date>)

**Process:** netcanon file-per-agent blackboard. Read-only agents each write EXACTLY ONE report in this dir; the
main thread seeds this file + writes 99-synthesis.md + is the sole actor that builds/commits.

## Mission
<what this run examines/decides — 1-3 bullets>

## Hard constraints (apply to every report)
<the non-negotiables for THIS run — e.g. homelab scale / no over-engineering; no god files / drop-ins /
zero-spine-edit; INVARIANT-D; C8/C12 at-rest; fail-closed knobs>

## Measuring (omit when this run does not measure)
<what an agent may run — the suite, single-fix reversions, a probe; the image; the scratch dir, outside every
worktree, one subdirectory per report id>

## File roster
| File | Phase | Author | Covers |
|---|---|---|---|
| 00-blackboard.md | seed | main thread | this protocol + mission + constraints |
| 10-research-<x>.md | research | R1 | ... |
| 30-review-adversarial.md | review | V1 | GO/NO-GO + must-fixes |
| 99-synthesis.md | synthesis | main thread | reconciled decisions + buildable-now contract |

## Decisions already locked (context)
<prior decisions the agents should treat as fixed>
```

### The `99-synthesis.md` opening (the main thread writes this after the run)

The runner returns these lines pre-filled (`bookends.synthesis.header`). A light-loop note that records a
decision opens with the same four.

```markdown
# Synthesis — <mission> (<UTC-date>)

**Decision:** <one sentence, or "none — findings only">
**Status:** <decided / awaiting the maintainer / superseded by …>
**ADR:** <docs/adr/NNNN-<slug>.md, or "none" when no decision was made>
**Review verdict:** <GO / GO-WITH-FIXES / NO-GO, or "no review phase">
```

## Directory structure (the evidence trail)

Every run gets a **dated evidence folder**, frozen and reproducible:

```
docs/reviews/<UTC-DATE>/
  00-snapshot.md            # repo/state inventory at the start of the run
  cluster-<X>-scope.md      # the focused input handed to agent X
  01-investigation-<X>.md   # agent X's report (its ONLY write) — a verdict table
  99-synthesis.md           # orchestrator: consolidated findings (CONFIRMED / DISMISSED)
  fix-plan.md               # orchestrator: fixes grouped by file/theme
```

Conventions:
- Folder name = **UTC date** of the snapshot (matches commit/GitHub timestamps).
- Numeric prefixes encode pipeline order: `00-` snapshot, `01-` Stage-1 outputs,
  `99-` synthesis.
- These folders are **EXPECTED-STALE**: a later audit must never flag a past
  run's evidence as drift. They are a frozen record, not live docs.
- Agents hand off purely via their `01-investigation-<X>.md`; the orchestrator
  reconciles in `99-synthesis.md`.

## Agent report format (verdict table)

Each Stage-1 agent's report is a table so the orchestrator can reconcile fast:

```
| # | Path:Line | Severity | Finding | Verdict (CONFIRMED/DISMISSED) | Fix shape |
```

Severity tags for a scaffolding/docs review: **WRONG / MISSING / INCOMPLETE /
STYLE / EXPECTED-STALE** (the last = a deliberate pattern, not a defect).

## Cluster taxonomy (how work is scoped & parallelized)

Group findings by the **kind of investigation** they need, not by file type — that
is what makes it parallelizable. Labels are **stable across runs** so future
searches find prior investigations under the same name. Each agent owns a
**non-overlapping scope**. Standard clusters for a kontroll scaffolding review:

| Cluster | Owns |
|---|---|
| **A — Interlinking & structure** | every internal markdown link resolves; required README sections present; "See also" reciprocity |
| **B — Testing & contracts** | test pyramid coverage; single dispatch seam (`device_role`/`mock-inventory`); idempotence; module→role→group→secrets contract complete |
| **C — Docs accuracy** | every state-claim matches reality; SECURITY control→file→covering-check; no hard-coded counts in prose; CHANGELOG current |
| **D — Logging & secrets & hard rules** | `no_log` on every secret-handling task; nothing unencrypted/committed; CLAUDE.md hard rules actually upheld in code |
| **E — Modularity / altitude** | no god files; additive growth; generated-vs-hand-maintained correct; "add an X" recipes accurate |

## Dispatch heuristics

- **Model:** Opus for the read-only investigation agents (long-context retention
  across many files); the orchestrator runs on the main thread.
- **Parallelism:** one agent per cluster, dispatched together. More agents ≠
  faster when each cluster is file-bounded — and harder to reconcile.
- **File-overlap check before dispatch:** if two agents would need to *edit* the
  same file (Stage 2), serialize them or split the file. Stage-1 is read-only so
  overlap is harmless.
- **Keep findings in files, not the main thread.** Agents write full detail to
  their report file and return only a short summary + the path, so the main
  thread's context stays light (this is the whole point of off-thread research).

## Which loop a change gets

A change gets one of two loops. Decide before you start, and say which in the pull request.

**The full loop — a security-boundary change.** A change is one if **any** of these is true:

1. it adds a network route; a request field that reaches a path, an inventory key, a command or a commit; an
   outbound client or a transport; a container privilege (a socket, a host bind, a root user, a published port);
   a secret domain or a recipient rule;
2. it changes what reaches `main` without a human;
3. to stay true, `SECURITY.md` would need a control's claim or a row of the accepted-risks table changed — or
   the change rewrites the validator, the allow-list or the gate that a control names as its mechanism. (An
   added proof, or a stronger description of a claim that was already true, is not this test; a sentence that
   would otherwise be false is.)

When in doubt it is a boundary change.

An audit, a pre-release gate or a planning round is not a change: it is a run of the same runner, and it names
its scope where a change names its boundary.

The table below is a **prompt**, not one more test. These are the files where a boundary lives, so a change to
one of them says, in one line of the pull request, why none of the tests above applies — or takes the full loop.
Most changes to these files fix a write target, add a generator flag or move a declaration, and are the light
loop; a rule that decided by path alone would send them all through the full one.

<!-- boundary-paths:begin -->
| Boundary | Paths |
|---|---|
| Propose → promote, and the canonical | `scripts/kontroll-promote.py`, `scripts/kontroll/service/promote.py`, `scripts/kontroll/service/discard.py`, `scripts/kontroll/service/pending.py`, `scripts/kontroll/canon.py`, `scripts/kontroll/gitio.py`, `ansible/playbooks/promote.yml`, `ansible/playbooks/local-canonical.yml`, `ansible/playbooks/files/canonical-update-hook.sh`, `scripts/lib/plaintext-guard.sh` (what a blanket commit may take into the canonical), `tests/unit/_readonly_pins.py` |
| The request boundary | `api/auth.py`, `api/deps.py`, `api/ratelimit.py`, `api/routes/`, `gui/app.py`, `gui/templates/index.html`, `scripts/kontroll/paths.py`, `scripts/kontroll/yamlscalar.py`, `scripts/kontroll/probe.py`, `scripts/kontroll/service/onboard.py`, `scripts/kontroll/service/_validate.py`, `scripts/kontroll/service/_blockwrite.py`, `scripts/kontroll/envguard.py` |
| Secrets and keys | `scripts/kontroll/service/secrets.py`, `scripts/kontroll/service/keygen.py`, `scripts/gen-secret-env.py`, `secret-forms/`, `key-roles/`, `instance.example/.sops.yaml`, and any task that handles a decrypted value |
| The supply chain | `scripts/gen-requirements.py`, `scripts/install-collections.py`, `scripts/gen-image-digests.py`, `scripts/gen-code-manifest.py`, `ansible/playbooks/bootstrap.yml`, `docker/images.lock.yml`, `docker/code-manifest.lock.yml`, `instance.example/trust/`, `docker/installer/Dockerfile`, `docker/semaphore-runner/Dockerfile`, `.github/workflows/`, `.github/dependabot.yml` |
| Posture, privilege and exposure | `scripts/kontroll-init.py`, `ansible/playbooks/deploy-stack.yml`, `docker/compose.yaml`, `docker/services/`, `tests/check-mgmt-port-binding.py`, `tests/check-storage-chown.py`, `tests/unit/test_deploy_stack_check_mode.py` |
| What may leave the instance | `tests/_leak_guard.py`, `.gitignore`, `instance.example/.gitignore`, `.gitleaks.toml`, `.dockerignore`, `scripts/make-bundle.sh`, `scripts/make-launch-kit.sh` |
| Clients that reach a device or the internet | `scripts/gen-validate-live.py`, `scripts/kontroll-discover.py`, `scripts/kontroll/catalog.py` |
| The audit trail | `scripts/kontroll/service/audit.py`, `scripts/kontroll/auditfmt.py`, `api/audit.py` |
| The review process itself | `.claude/workflows/blackboard.js` (the read-only contract it prepends to every agent) |
<!-- boundary-paths:end -->

An edition that adds an automaton on the trusted side of the wall — the homelab edition's auto-promoter — lists
its own paths in its own tree (its `docs/homelab-edition.md`, "Boundary paths of this edition").

**The full loop** is: the blackboard runner (seed → read-only research and design → an adversarial review → the
main thread's synthesis), **then build, then run it** — a `--check --diff`, a fresh-node deploy, the browser
suite — **before any further review**. An as-built review is written after the run, in two cases: the run found
a fault at the boundary, or the build added a rule of its own — an accept-or-refuse decision (a validator, an
allow-list, a pattern, a gate) that the design the review read did not contain. A fix round is a build: the
rules it adds are unreviewed until somebody reads them. Keep a run small — the runner logs a line above its
guideline. The synthesis opens with its `Decision / Status / ADR` lines, the run gets its index row, and a
decision gets a record ([adr/README.md](adr/README.md)).

**The light loop — everything else** (a correctness fix, a drop-in, a generator, a test, a document):

1. **One note, not a dossier.** The pull request's description is the note: what, why, and what could go wrong.
   If it needs more than a screen, one file `docs/reviews/<UTC-date>-<slug>/00-note.md`, written by the main
   thread or by one read-only agent.
2. **Build it with its tests** — the test, its docstring and the doc sync in the same commit, as always.
3. **Run it first.** If the behaviour only shows when it runs (templating, variable scoping, YAML folding, volume
   ownership, an image build), run it before anyone reviews it further. That class of fault is what reaches a
   live box, and reading does not find it.
4. **One independent read of the diff** — one reviewer agent, or the code-review command — when the change
   touches a file of the table above. It is a read of a diff, not a review of a design.
5. **A decision record only if a decision was made that someone could later reverse.**

The read-only rule does not change with the loop: a research or review agent writes its one file (and, measuring,
only inside its own scratch copy) and nothing else, and the main thread is the only actor that edits, commits or
touches the lab.

## Stage 2 (actuation)

The orchestrator executes fixes **commit-by-commit, one logical theme per
commit**, with rationale-first commit messages (see commit conventions in
[CLAUDE.md](../CLAUDE.md)). It re-runs `tests/validate` (and live checks where relevant) after
each theme. Stage-2 agents are spawned only for genuinely multi-file fixes.

## See also
- [adr/README.md](adr/README.md) — decision records: the template, the rules, the index of decisions
- [reviews/README.md](reviews/README.md) — where a run's dossier lives, and the index of runs
- [../CLAUDE.md](../CLAUDE.md) — hard rules + the Documentation Sync Checklist
- [engineering-standards.md](engineering-standards.md) — testing/docs/logging doctrine
- [../PLAN.md](../PLAN.md) §2.5 — the modularity doctrine the audits enforce
