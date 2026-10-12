# .claude/workflows — reusable ultracode workflows

Reusable `Workflow`-tool scripts for this repo. **Invoke them by `scriptPath`** (not `name:` — the `name:`
registry is reserved for built-in/plugin workflows like `deep-research` / `code-review`; custom files here are
addressed by their path). The binding process they implement is
[docs/agent-workflow.md](../../docs/agent-workflow.md).

## `blackboard.js` — the file-per-agent blackboard (the consistent ultracode pattern)

Every ultracode run uses this runner so the discipline never has to be re-hand-rolled (and can't drift). It
bakes in the load-bearing contract:

- **Read-only agents, one report file each.** Each agent writes EXACTLY ONE long-form report (its `id`.md under
  the run dir) and is otherwise strictly read-only — no source/config/test edits, no git/ansible/docker, no
  actuation. The contract preamble is prepended to every agent prompt automatically.
- **File-based peer comms.** Later-phase agents read earlier phases' report files (`readsPrior: true`, or an
  explicit per-agent `reads: [id,…]`).
- **Short returns.** Agents return only a pointer/summary (`report_file`, `headline`, `key_points`; reviewers
  return `verdict` + `must_fixes`). The depth lives in the markdown files, so the main thread's context stays
  light.
- **Opus by default** for every agent (override per-agent/phase with `model`).

### The main thread owns the bookends (NOT the runner — it has no filesystem access)

1. **Before invoking:** write `<dir>/00-blackboard.md` — the seed: mission, hard constraints, file roster. Use
   the template in [docs/agent-workflow.md](../../docs/agent-workflow.md) (§ Ultracode runs). Add the run's
   **index row** in the same step — in `docs/reviews/README.md` for a published dossier, in
   `docs/reviews/INDEX.md` in an instance repository — so the folder is findable from the moment it exists (a
   dated run folder without a row fails the suite).
2. **Invoke** the runner (see below).
3. **After it returns:** read the `NN-*.md` report files, then write the following — the runner returns the text
   to start each from, in `bookends`:
   - `<dir>/99-synthesis.md` (reconciled decisions + the buildable-now contract, applying the review's
     must-fixes), opening with the `Decision / Status / ADR / Review verdict` lines of `bookends.synthesis.header`;
   - the run's **index row**, completed: its decision and its record (`bookends.index_row` is the row in full);
   - a **decision record** from `bookends.adr_stub`, when the run ended in a decision someone could later
     reverse ([docs/adr/README.md](../../docs/adr/README.md)) — and only then.

   Then build/validate/commit. Agents NEVER actuate — this is the netcanon rule.

### Invocation

```
// main thread, before the call: Write <repo>/docs/reviews/<UTC-date>-<slug>/00-blackboard.md (the seed)
Workflow({ scriptPath: "<repo>/.claude/workflows/blackboard.js", args: {
  dir: "/abs/path/to/kontroll/docs/reviews/2026-06-16-storage-logging",  // ABSOLUTE; date-stamped + slug
  slug: "storage-logging",
  mission: "one-line mission (logged at start)",
  phases: [
    { title: "Research", agents: [
      { id: "10-research-x", label: "R1:x", task: "<run-specific prompt body>" },
      { id: "11-research-y", label: "R2:y", task: "…" },
    ] },
    { title: "Design", readsPrior: true, agents: [
      { id: "20-design-a", label: "D1:a", task: "…" },
    ] },
    { title: "Review", readsPrior: true, review: true, agents: [
      { id: "30-review", label: "V1", task: "…" },
    ] },
  ],
} })
```

### `args` contract

| key | required | meaning |
|---|---|---|
| `dir` | yes | ABSOLUTE run-folder path (`<repo>/docs/reviews/<UTC-date>-<slug>`). The runner has no FS access, so the caller computes it (matches the caller's path separator automatically). |
| `slug` | no | short topic label for the progress narrator. Also the slug of the decision-record path in `bookends` when the run folder's name carries none. |
| `mission` | no | one-line mission, `log()`-ed at start. |
| `boundary` | no | which security boundary the run is about — or, for an audit or a planning round, its scope. A run of several agents is the full loop ([docs/agent-workflow.md](../../docs/agent-workflow.md), "Which loop a change gets"); when absent on such a run, one line is `log()`-ed. Never enforced; a single-reader run is asked nothing. |
| `adr` | no | the next free decision-record number (the caller lists `docs/adr/`), e.g. `"0006"`; digits are padded to four. Pre-fills `bookends`; default the placeholder `NNNN` — a stub written with the placeholder still in its name fails `tests/unit/test_review_loop.py`. |
| `oversize_reason` | no | why the run needs more agents than the runner's guideline (`AGENT_GUIDELINE` in the script). When absent on a larger run, one line is `log()`-ed. |
| `phases[]` | yes | ordered phases; each runs as a `parallel()` barrier. |
| `phases[].title` | yes | progress group + the phase tag. |
| `phases[].readsPrior` | no | `true` → every agent reads ALL earlier-phase report files. |
| `phases[].review` | no | `true` → agents use the `REVIEW_SUMMARY` schema (verdict + must_fixes). |
| `phases[].model` | no | model for the whole phase (default `opus`). |
| `phases[].effort` | no | reasoning effort for the whole phase (`low`/`medium`/`high`/`xhigh`/`max`). Omit to inherit the session effort; use `max` for adversarial/security passes. |
| `phases[].agents` | yes | the phase's agents (at least one). |
| `agents[].id` | yes | report filename stem; numeric-prefixed + **unique** (it IS the agent's only write target). |
| `agents[].task` | yes | the run-specific prompt body (the read-only contract is prepended for you). |
| `agents[].label` | no | progress label (default = `id`). |
| `agents[].reads` | no | explicit `[id,…]` peer reports to read (overrides `readsPrior`). |
| `agents[].model` | no | per-agent model override. |
| `agents[].effort` | no | per-agent reasoning-effort override (wins over `phases[].effort`). |

Returns `{ dir, files:{id→path}, phases:[{title, results:[{id,file,summary}]}], review, bookends }`.

`bookends` is `{ synthesis:{file, header}, adr_stub:{path, text}, index_row:{file, text} }` — the texts the
main thread writes after a run, pre-filled with what the runner knows: the date and slug (parsed from the run
folder's name), the mission, and the review phase's verdict. They describe **the run that returned them**: a
single reader sent into an existing run folder gets a row that says `light` and a verdict that is that reader's,
not the folder's. The index row's loop cell is `full` for a run of several agents and `light` for one, and its
link points at the synthesis or, for a one-agent note, at `00-note.md`. It is **advisory**: nothing forces the
main thread to use it. What is checked is the result — a dated run folder with no index row, and a decision
record with no row, a reused number or a name that is not `NNNN-<slug>.md`, fail
`tests/unit/test_review_loop.py`.

### Iterate / resume

Edit `blackboard.js` (or the per-run caller) and re-invoke with `scriptPath` + `resumeFromRunId` — unchanged
agents return cached results. Note: `Date.now()`/`new Date()` are unavailable in workflow scripts (they break
resume), which is why the caller stamps the date into `dir` — and why the runner reads the date for `bookends`
back out of the folder's name.

A workflow script cannot be executed by the test gate (its `args`, `agent`, `parallel`, `phase` and `log` are
globals of the harness), so the tests pin its text. The first run after an edit is its real test: make that run
a small one.
