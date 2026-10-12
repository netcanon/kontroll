// Reusable file-per-agent BLACKBOARD runner for kontroll ultracode runs.
//
// Why this exists: every ultracode run should follow the SAME netcanon blackboard discipline
// (docs/agent-workflow.md) instead of being hand-rolled each time. This runner bakes in the
// load-bearing contract so it can't drift: each agent is READ-ONLY except its ONE long-form
// report file; agents read peers' reports for cross-phase comms; agents return only a short
// pointer/summary; the MAIN THREAD seeds 00-blackboard.md and (after the run) synthesizes +
// builds + commits. Parameterized entirely by `args` (see .claude/workflows/README.md).
//
// Invoke:  Workflow({ scriptPath: "<repo>/.claude/workflows/blackboard.js", args: { dir, slug, mission, phases:[...] } })
//   (by scriptPath -- the `name:` registry is reserved for built-in and plugin workflows)
//   - The MAIN THREAD writes <dir>/00-blackboard.md (the seed: mission + constraints + roster)
//     BEFORE invoking, and reads the <dir>/NN-*.md reports + writes 99-synthesis.md AFTER.
//
// args = {
//   dir:     ABSOLUTE path of the run folder, e.g. "<repo>/docs/reviews/<UTC-date>-<slug>"  (REQUIRED)
//   slug:    short topic label (for the progress narrator)                                   (optional)
//   mission: one-line mission (logged at start)                                              (optional)
//   boundary: which security boundary this run is about -- a FULL run is for a boundary change -- or,
//             for an audit or a planning round, its scope (docs/agent-workflow.md, "Which loop a
//             change gets"); logged, never enforced                                          (optional)
//   adr:     the next free decision-record number, e.g. "0006" (the caller lists docs/adr/);
//             pre-fills the returned stub (digits are padded to four), default "NNNN"         (optional)
//   oversize_reason: why this run needs more agents than the guideline; logged               (optional)
//   phases: [ {                                                                              (REQUIRED, >=1)
//     title:      "Research" | "Design" | "Review" | ...  (progress group + the phase tag)
//     readsPrior: true  -> every agent in this phase is told to read ALL earlier-phase reports
//     review:     true  -> agents use the REVIEW_SUMMARY schema (verdict + must_fixes)
//     model:      override model for the whole phase (default per-agent / 'opus')
//     effort:     reasoning effort for the whole phase ('low'|'medium'|'high'|'xhigh'|'max');
//                 omit to inherit the session effort. Use 'max' for adversarial/security passes.
//     agents: [ {
//       id:    report filename stem, numeric-prefixed + unique, e.g. "10-research-metrics"
//              (-> <dir>/10-research-metrics.md ; this is the agent's ONLY write)
//       task:  the run-specific prompt body (the READ-ONLY contract is prepended automatically)
//       label: progress label (default = id)
//       reads: explicit [id,...] peer reports to read (overrides phase.readsPrior)
//       model: per-agent model override
//       effort: per-agent reasoning-effort override (wins over phase.effort)
//     }, ... ]
//   }, ... ]
// }
//
// Returns: { dir, files:{id->path}, phases:[{title, results:[{id,file,summary}]}], review, bookends }
//   bookends = the text of the three things the MAIN THREAD writes after a run (the runner cannot write a file):
//     { synthesis:{file, header}, adr_stub:{path, text}, index_row:{file, text} }

export const meta = {
  name: 'blackboard',
  description: 'Reusable file-per-agent blackboard runner for ultracode runs: read-only agents each write ONE long-form report under the run dir, read peers reports for cross-phase comms, return a short pointer/summary. Main thread seeds 00-blackboard.md then synthesizes + builds. Parameterized by args (.claude/workflows/README.md).',
}

// ---- the ENFORCED, consistent contract (baked in so every ultracode run is identical) ----
const RO = [
  "You are an agent in kontroll's netcanon ultracode BLACKBOARD process (docs/agent-workflow.md).",
  "OUTPUT RULE: you write EXACTLY ONE file -- your own long-form report at the path given below. That report file IS your deliverable; write it thoroughly (headings, tables, rationale, citations, concrete names/config/code sketches). Otherwise you are STRICTLY READ-ONLY: do NOT edit/create/delete ANY other file, do NOT touch source/config/tests, do NOT run git/ansible/docker or any state-changing command, do NOT actuate anything. The MAIN THREAD alone synthesizes, validates, builds, and commits.",
  "After writing your report file, RETURN only the small structured summary the schema asks for (a pointer + a few key points). The depth lives in the FILE; the return is for orchestration only.",
  "FIRST read CLAUDE.md (the repo doctrine) and the run's seed (00-blackboard.md, path below) for the mission + hard constraints + file roster. Then read the source/peer files you need.",
].join("\n");

const SUMMARY = { type:"object", additionalProperties:false, properties:{
  report_file:{type:"string"},
  headline:{type:"string"},
  key_points:{type:"array", items:{type:"string"}},
  buildable_now:{type:"array", items:{type:"string"}},
  over_engineering_flags:{type:"array", items:{type:"string"}}
}, required:["report_file","headline","key_points"] };

const REVIEW_SUMMARY = { type:"object", additionalProperties:false, properties:{
  report_file:{type:"string"},
  verdict:{type:"string", enum:["GO","GO-WITH-FIXES","NO-GO"]},
  must_fixes:{type:"array", items:{type:"object", additionalProperties:false, properties:{
    id:{type:"string"}, target:{type:"string"}, issue:{type:"string"}, fix:{type:"string"},
    severity:{type:"string", enum:["blocker","major","minor"]} }, required:["issue","fix","severity"] }},
  over_engineering_flags:{type:"array", items:{type:"string"}},
  buildable_now_confirmed:{type:"array", items:{type:"string"}}
}, required:["report_file","verdict","must_fixes","buildable_now_confirmed"] };

// ---- arg validation (LOUD + synchronous, BEFORE any agent spawns) ----
// NB: validation must NOT live inside a parallel() thunk -- a thunk that throws resolves to null
// silently, so a bad config would be swallowed instead of failing the run. Validate up front.
// `args` may arrive as a real object OR (depending on how the caller passed it) as a JSON STRING -- the
// Workflow tool warns a stringified value reaches the script as one string. Tolerate both so the runner is
// robust regardless of how it is invoked.
let A = args;
if (typeof A === 'string') {
  try { A = JSON.parse(A); }
  catch (e) { throw new Error("blackboard: args arrived as a string that is not valid JSON: " + e.message); }
}
if (!A || typeof A !== 'object' || Array.isArray(A)) throw new Error("blackboard: args object required (see .claude/workflows/README.md)");
const dir = A.dir;
if (!dir || typeof dir !== 'string') throw new Error("blackboard: args.dir (absolute run-folder path) is required");
const phases = A.phases || [];
if (!Array.isArray(phases) || !phases.length) throw new Error("blackboard: args.phases (>= 1 phase) is required");
const seen = {};
for (const ph of phases) {
  if (!ph || !ph.title) throw new Error("blackboard: each phase needs a title");
  const list = ph.agents || [];
  if (!Array.isArray(list) || !list.length) throw new Error("blackboard: phase '" + ph.title + "' has no agents");
  for (const a of list) {
    if (!a || !a.id) throw new Error("blackboard: every agent needs an id (it is the report filename)");
    if (!a.task) throw new Error("blackboard: agent '" + a.id + "' needs a task");
    if (seen[a.id]) throw new Error("blackboard: duplicate agent id '" + a.id + "' -- ids are report filenames, must be unique");
    seen[a.id] = true;
  }
}

const SEP = dir.indexOf("\\") >= 0 ? "\\" : "/";        // match the caller's path style (Windows vs POSIX)
const pathFor = (id) => dir + SEP + id + ".md";
const seed = pathFor("00-blackboard");

if (A.mission) log("blackboard [" + (A.slug || "run") + "]: " + A.mission);

// ---- two nudges, asked at the moment a run is launched. They are LOGGED; the runner never refuses. ----
// A run of several agents is the full loop, and the full loop is for a security-boundary change. A single
// reader (the light loop's one independent read of a diff) is asked nothing.
const AGENT_GUIDELINE = 4;
const agentCount = phases.reduce((n, ph) => n + ph.agents.length, 0);
if (agentCount > 1 && !A.boundary) {
  log("blackboard: no args.boundary given -- a run of several agents is for a security-boundary change, an "
    + "audit or a planning round (docs/agent-workflow.md, 'Which loop a change gets'). Name the boundary or "
    + "the scope in args.boundary, or use the light loop.");
}
if (agentCount > AGENT_GUIDELINE && !A.oversize_reason) {
  log("blackboard: " + agentCount + " agents and no args.oversize_reason -- keep a run to about "
    + AGENT_GUIDELINE + " agents unless the seed argues for more.");
}

const prior = [];                                        // accumulated report paths for peer-reads
const out = { dir: dir, files: {}, phases: [], review: null };

for (const ph of phases) {
  phase(ph.title);
  const reading = prior.slice();                         // snapshot of all earlier-phase report files
  const list = ph.agents;
  const thunks = list.map((a) => () => {
    const file = pathFor(a.id);
    out.files[a.id] = file;
    let reads = [];
    if (Array.isArray(a.reads)) reads = a.reads.map(pathFor);
    else if (ph.readsPrior) reads = reading;
    const prompt = RO
      + "\n\nSEED (read first): " + seed
      + "\n\nYOUR REPORT FILE (your ONLY write): " + file
      + (reads.length ? "\n\nFIRST read these peer reports for context: " + reads.join(" , ") : "")
      + "\n\nTASK:\n" + a.task
      + "\n\nDELIVERABLE: the full long-form report written to your report file above; THEN return the summary.";
    const opts = {
      label: a.label || a.id,
      phase: ph.title,
      model: a.model || ph.model || 'opus',
      schema: ph.review ? REVIEW_SUMMARY : SUMMARY,
    };
    const effort = a.effort || ph.effort;                  // omitted -> inherit the session effort
    if (effort) opts.effort = effort;
    return agent(prompt, opts).then((summary) => ({ id: a.id, file: file, summary: summary }));
  });
  const results = await parallel(thunks);
  out.phases.push({ title: ph.title, results: results });
  for (const a of list) prior.push(pathFor(a.id));       // visible to the next phase's peer-reads
  if (ph.review) {
    const got = results.filter(Boolean).map((x) => x.summary).filter(Boolean);
    if (got.length) out.review = got.length === 1 ? got[0] : got;
  }
}

// ---- the bookends the MAIN THREAD writes (the runner has no filesystem access: it returns their TEXT) ----
// The date is parsed from the run folder's name (<UTC-date>-<slug>): a workflow script has no clock -- a call to
// one would break resume -- which is why the caller stamps the date into `dir`. What the runner cannot know is
// the decision itself, and the next free decision-record number (args.adr; a placeholder when absent).
const base = dir.split(/[\\/]/).filter(Boolean).pop() || "";
const dm = /^(\d{4}-\d{2}-\d{2})(?:-(.+))?$/.exec(base);
if (!dm) log("blackboard: run folder '" + base + "' is not <UTC-date>-<slug> -- the index row needs the date");
const runDate = dm ? dm[1] : "<UTC-date>";
const runSlug = String((dm && dm[2]) || A.slug || "run").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "run";
const adrRaw = String(A.adr || "NNNN");
const adrNo = /^\d+$/.test(adrRaw) ? adrRaw.padStart(4, "0") : adrRaw;     // 6 -> 0006: a record is NNNN-<slug>.md
const hadReview = phases.some((ph) => ph.review);
const verdicts = [].concat(out.review || []).map((r) => r && r.verdict).filter(Boolean).join(" / ")
  || (hadReview ? "no verdict returned" : "no review phase");
const adrPath = "docs/adr/" + adrNo + "-" + runSlug + ".md";
// One agent is the light loop's single reader (or a one-agent note); several are a full run.
const loop = agentCount > 1 ? "full" : "light";
const opening = agentCount > 1 ? "99-synthesis.md" : "00-note.md";
out.bookends = {
  synthesis: {
    file: pathFor("99-synthesis"),
    header: [
      "# Synthesis -- " + (A.mission || runSlug) + " (" + runDate + ")",
      "",
      "**Decision:** <one sentence, or 'none -- findings only'>",
      "**Status:** <decided / awaiting the maintainer / superseded by ...>",
      "**ADR:** " + adrPath + "  <or 'none' when no decision was made>",
      "**Review verdict:** " + verdicts,
    ].join("\n"),
  },
  adr_stub: {
    path: adrPath,
    text: [
      "# ADR-" + adrNo + " -- <the decision, as a sentence>",
      "",
      "**Status:** PROPOSED (" + runDate + ")",
      "**Decided by:** <the maintainer / the main thread>",
      "**Supersedes:** <ADR-NNNN, or nothing>",
      "**Evidence:** docs/reviews/" + base + "/" + opening + " (review verdict: " + verdicts + ")  <or a pull request, a live run>",
      "",
      "## The question",
      "",
      "## Decision",
      "",
      "## Why",
      "",
      "## What would change this decision",
      "",
    ].join("\n"),
  },
  index_row: {
    file: "docs/reviews/README.md (a published dossier) or docs/reviews/INDEX.md (an instance repository)",
    text: "| " + runDate + " | [" + base + "](" + base + "/" + opening + ") | " + loop + " | <the decision, one line> | <ADR-" + adrNo + ", or none> |",
  },
};

return out;
