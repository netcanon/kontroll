"""The review process's own documents and its runner — pinned as text, because that is what they are.

WHY: docs/agent-workflow.md says which review loop a change gets and lists the files where a security boundary
lives; .claude/workflows/blackboard.js is the runner every full review goes through, and the read-only contract
it prepends to each agent is what a hard rule of this repository rests on ("agents are read-only and write only
their one report file"). A workflow script cannot be imported or executed by the gate — its `args`, `agent`,
`parallel`, `phase` and `log` are globals of the harness — so these are text pins, in the style
tests/unit/test_ci_workflows.py uses for workflow files. Each helper is a pure function over text or a
directory, so each rule is also shown to FAIL on a planted violation.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

RUNNER = os.path.join(ROOT, ".claude", "workflows", "blackboard.js")
RUNNER_README = os.path.join(ROOT, ".claude", "workflows", "README.md")
WORKFLOW_DOC = os.path.join(ROOT, "docs", "agent-workflow.md")
REVIEWS = os.path.join(ROOT, "docs", "reviews")
ADR = os.path.join(ROOT, "docs", "adr")

# Run folders dated on or after this day need an index row. Earlier runs are not backfilled (the project's own
# review ruled that out); an instance repository starts its index here as well.
INDEXED_FROM = "2026-10-12"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(js):
    """The script without its `//` comment lines — so a rule about the code is not satisfied, or broken, by prose."""
    return "\n".join(ln for ln in js.splitlines() if not ln.lstrip().startswith("//"))


# --- the read-only contract ------------------------------------------------------------------------------------

_CONTRACT = ("you write EXACTLY ONE file", "STRICTLY READ-ONLY", "do NOT edit/create/delete ANY other file",
             "do NOT actuate anything", "The MAIN THREAD alone synthesizes, validates, builds, and commits")


def contract_problems(js):
    """The phrases of the read-only contract that the runner's preamble no longer carries."""
    start = js.find("const RO = [")
    preamble = js[start:js.find("].join", start)] if start >= 0 else ""
    return [phrase for phrase in _CONTRACT if phrase not in preamble]


def test_runner_keeps_the_read_only_contract():
    """The preamble the runner prepends to EVERY agent still says: one file, otherwise strictly read-only, no
    other file touched, nothing actuated, the main thread alone builds and commits. Guards an edit to the runner
    weakening the contract a hard rule depends on — the runner is edited rarely, by the same hands that are
    bound by it, and nothing else would notice. A copy with one sentence softened is shown to fail."""
    js = _read(RUNNER)
    assert contract_problems(js) == []
    softened = js.replace("STRICTLY READ-ONLY", "mostly read-only")
    assert contract_problems(softened) == ["STRICTLY READ-ONLY"]
    assert contract_problems("const nothing = 1;") == list(_CONTRACT)
    assert "prompt = RO" in _code(js).replace("const prompt", "prompt"), "the preamble is no longer prepended"


# --- the bookends, and no clock ----------------------------------------------------------------------------------

def test_runner_returns_the_bookends_and_reads_no_clock():
    """The runner builds `bookends` with a synthesis header, a decision-record stub and an index row, takes the
    run's date from the run folder's NAME, and calls no clock and no random source. Guards the three texts being
    dropped from the return, and a `Date.now()` slipping in — a workflow script that reads a clock cannot be
    resumed, which is why the caller stamps the date into the folder name in the first place."""
    code = _code(_read(RUNNER))
    block = code[code.find("out.bookends = {"):]
    assert block, "the runner no longer returns bookends"
    for member in ("synthesis:", "adr_stub:", "index_row:"):
        assert member in block, member
    for line in ("**Decision:**", "**Status:**", "**ADR:**", "## The question", "## What would change this decision"):
        assert line in block, line
    assert re.search(r"\\d\{4\}-\\d\{2\}-\\d\{2\}", code), "the date is no longer parsed from the folder name"
    for clock in ("Date.now(", "new Date(", "Math.random("):
        assert clock not in code, clock
    assert code.rstrip().endswith("return out;")


# --- every argument the script reads is in the contract --------------------------------------------------------------

def undocumented_arguments(js, readme):
    """The argument keys the script reads (`A.x`, a phase's `ph.x`, an agent's `a.x`) that the README's arguments
    table has no row for."""
    code = _code(js)
    wanted = [(key, key) for key in sorted(set(re.findall(r"\bA\.([A-Za-z_]\w*)", code)))]
    wanted += [(key, "phases[].%s" % key) for key in sorted(set(re.findall(r"\bph\.([A-Za-z_]\w*)", code)))]
    wanted += [(key, "agents[].%s" % key) for key in sorted(set(re.findall(r"\ba\.([A-Za-z_]\w*)", code)))]
    rows = set(re.findall(r"^\| `([^`]+)` \|", readme, flags=re.M))
    rows |= {r[:-2] for r in rows if r.endswith("[]")}                     # the `phases[]` row documents `phases`
    return [name for _key, name in wanted if name not in rows]


def test_every_runner_argument_is_documented():
    """Every key the runner reads from its arguments, from a phase and from an agent has a row in the arguments
    table of .claude/workflows/README.md. Guards an option added to the script and not to its contract — how
    `effort` came to exist in one fork of this file only, for months. A script that reads one more key is shown
    to fail."""
    js, readme = _read(RUNNER), _read(RUNNER_README)
    assert undocumented_arguments(js, readme) == []
    assert undocumented_arguments(js + "\nconst x = A.brand_new_option;\n", readme) == ["brand_new_option"]
    assert undocumented_arguments(js + "\nconst y = a.secret_knob;\n", readme) == ["agents[].secret_knob"]


# --- the boundary list names real paths --------------------------------------------------------------------------------

def boundary_paths(doc):
    """The back-ticked repository paths between the two markers of the boundary table."""
    begin, end = doc.find("<!-- boundary-paths:begin -->"), doc.find("<!-- boundary-paths:end -->")
    if begin < 0 or end < begin:
        return None
    return [p for p in re.findall(r"`([^`\s]+)`", doc[begin:end]) if "/" in p or p.startswith(".")]


def test_boundary_list_names_only_paths_that_exist():
    """Every path in the boundary table of docs/agent-workflow.md exists in this tree, and the table is there.
    Guards the list rotting: a renamed promote module would silently leave the list, and the next change to it
    would no longer be asked the boundary question. A list holding one path that does not exist is shown to
    fail."""
    paths = boundary_paths(_read(WORKFLOW_DOC))
    assert paths, "the boundary table (or its markers) is gone from docs/agent-workflow.md"
    assert [p for p in paths if not os.path.exists(os.path.join(ROOT, p))] == []
    for must in ("scripts/kontroll/gitio.py", "scripts/kontroll/paths.py", ".claude/workflows/blackboard.js"):
        assert must in paths, must
    planted = "<!-- boundary-paths:begin -->\n| x | `scripts/no-such-file.py`, `README.md` |\n<!-- boundary-paths:end -->"
    assert [p for p in boundary_paths(planted) if not os.path.exists(os.path.join(ROOT, p))] == ["scripts/no-such-file.py"]
    assert boundary_paths("no markers here") is None


# --- every dated run has an index row -----------------------------------------------------------------------------------

def runs_without_an_index_row(reviews_dir, since=INDEXED_FROM):
    """The run folders `<UTC-date>[-<slug>]` under reviews_dir, dated `since` or later, that the index does not
    name. The index is INDEX.md when the directory has one (an instance repository), else README.md."""
    if not os.path.isdir(reviews_dir):
        return []
    index = os.path.join(reviews_dir, "INDEX.md")
    if not os.path.exists(index):
        index = os.path.join(reviews_dir, "README.md")
    text = _read(index) if os.path.exists(index) else ""
    missing = []
    for name in sorted(os.listdir(reviews_dir)):
        m = re.match(r"(\d{4}-\d{2}-\d{2})(-.+)?$", name)
        if m and os.path.isdir(os.path.join(reviews_dir, name)) and m.group(1) >= since and name not in text:
            missing.append(name)
    return missing


def test_every_dated_run_folder_has_an_index_row(tmp_path):
    """Each run folder under docs/reviews/ dated on or after the day the index began is named in the index of
    that tree — README.md here, INDEX.md in an instance repository. Guards a run nobody can find: the row is what
    makes a run's decision visible and supersedable. On a tree with no run folder the check passes with nothing
    to check, so a planted tree shows it fail — for a new folder with no row, and not for an older one."""
    assert runs_without_an_index_row(REVIEWS) == []
    for name in ("2026-10-12-with-a-row", "2026-11-03-no-row", "2026-07-01-before-the-index", "notes"):
        (tmp_path / name).mkdir()
    (tmp_path / "README.md").write_text("| 2026-10-12 | [2026-10-12-with-a-row](x) | full | d | none |\n",
                                        encoding="utf-8")
    assert runs_without_an_index_row(str(tmp_path)) == ["2026-11-03-no-row"]
    (tmp_path / "INDEX.md").write_text("2026-11-03-no-row\n", encoding="utf-8")      # an instance repository's index
    assert runs_without_an_index_row(str(tmp_path)) == ["2026-10-12-with-a-row"]     # ...and it is the one read


# --- decision records ---------------------------------------------------------------------------------------------------

_STATUS = re.compile(r"^\*\*Status:\*\* (PROPOSED|ACCEPTED|SUPERSEDED)\b", re.M)


def decision_record_problems(adr_dir):
    """What is wrong with the records `NNNN-<slug>.md` under adr_dir: a number used twice, a status line that
    holds none of the three words, a record the README's index has no row for."""
    if not os.path.isdir(adr_dir):
        return ["%s does not exist" % adr_dir]
    readme = os.path.join(adr_dir, "README.md")
    index = _read(readme) if os.path.exists(readme) else ""
    problems, seen = [], {}
    for name in sorted(os.listdir(adr_dir)):
        m = re.match(r"(\d{4})-.+\.md$", name)
        if not m:
            continue
        number = m.group(1)
        if number in seen:
            problems.append("%s reuses the number of %s" % (name, seen[number]))
        seen[number] = name
        if not _STATUS.search(_read(os.path.join(adr_dir, name))):
            problems.append("%s has no `**Status:**` line with PROPOSED, ACCEPTED or SUPERSEDED" % name)
        if not re.search(r"^\| %s \|" % number, index, flags=re.M):
            problems.append("%s has no row in README.md's index" % name)
    return problems


def test_decision_records_are_numbered_once_and_indexed(tmp_path):
    """docs/adr/ exists with its README, and every record in it has a number of its own, a status line holding
    one of the three allowed words, and a row in the README's index. Guards two records sharing a number (it
    happened while this directory was being planned), a record whose status nobody can tell, and a record nobody
    can find. The directory may hold no record yet, so a planted directory shows each of the three fail."""
    assert os.path.exists(os.path.join(ADR, "README.md"))
    assert decision_record_problems(ADR) == []
    (tmp_path / "README.md").write_text("| ADR | Decision |\n|---|---|\n| 0007 | fine |\n", encoding="utf-8")
    (tmp_path / "0007-fine.md").write_text("# ADR-0007\n\n**Status:** ACCEPTED (2026-10-12)\n", encoding="utf-8")
    assert decision_record_problems(str(tmp_path)) == []
    (tmp_path / "0007-again.md").write_text("# ADR-0007\n\n**Status:** PROPOSED (2026-10-12)\n", encoding="utf-8")
    (tmp_path / "0008-vague.md").write_text("# ADR-0008\n\n**Status:** under discussion\n", encoding="utf-8")
    problems = decision_record_problems(str(tmp_path))
    assert any("reuses the number" in p for p in problems)
    assert any("0008-vague.md has no `**Status:**`" in p for p in problems)
    assert any("0008-vague.md has no row" in p for p in problems)
    assert decision_record_problems(str(tmp_path / "absent")) != []
