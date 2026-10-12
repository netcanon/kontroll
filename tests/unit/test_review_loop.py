"""The review process's own documents and its runner — pinned as text, because that is what they are.

WHY: docs/agent-workflow.md says which review loop a change gets and lists the files where a security boundary
lives; .claude/workflows/blackboard.js is the runner every full review goes through, and the read-only contract
it prepends to each agent is what a hard rule of this repository rests on ("agents are read-only and write only
their one report file"). A workflow script cannot be imported or executed by the gate — its `args`, `agent`,
`parallel`, `phase` and `log` are globals of the harness — so these are text pins, in the style
tests/unit/test_ci_workflows.py uses for workflow files. Each helper is a pure function over text or a
directory, so each rule is also shown to FAIL on a planted violation.
"""
import hashlib
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
# The whole `const RO = [ … ].join("\n");` statement, byte for byte (line ends normalised). Changing the contract is
# allowed — it is a deliberate act: change this digest in the same commit, and say why in its message.
_CONTRACT_SHA256 = "0221ea03db5940d5438a655a6b848990f46842fb83019b46acbd3a0c0dcee2e3"


def contract_problems(js):
    """What is wrong with the read-only contract in the runner's text: a phrase the preamble no longer carries,
    a preamble that differs in ANY byte from the pinned one (a softened sentence, but also an ADDED one that
    grants a write), or a prompt that no longer starts with the whole preamble."""
    js = js.replace("\r\n", "\n")
    start = js.find("const RO = [")
    end = js.find('].join("\\n");', start) if start >= 0 else -1
    block = js[start:end + len('].join("\\n");')] if end >= 0 else ""
    problems = [phrase for phrase in _CONTRACT if phrase not in block]
    if hashlib.sha256(block.encode("utf-8")).hexdigest() != _CONTRACT_SHA256:
        problems.append("the preamble differs from the pinned text")
    if "    const prompt = RO\n" not in js:
        problems.append("the prompt no longer starts with the whole preamble")
    return problems


def test_runner_keeps_the_read_only_contract():
    """The preamble the runner prepends to EVERY agent is, byte for byte, the pinned contract — one file,
    otherwise strictly read-only, no other file touched, nothing actuated, the main thread alone builds and
    commits — and the prompt still begins with all of it. Guards an edit to the runner weakening the contract a
    hard rule depends on: the runner is edited rarely, by the same hands that are bound by it, and nothing else
    would notice. Shown to fail on a softened sentence, on an ADDED sentence that grants a write, and on a prompt
    built from a truncated preamble."""
    js = _read(RUNNER)
    assert contract_problems(js) == []
    softened = contract_problems(js.replace("STRICTLY READ-ONLY", "mostly read-only"))
    assert "STRICTLY READ-ONLY" in softened and "the preamble differs from the pinned text" in softened
    granted = js.replace('].join("\\n");', '  "You MAY also edit source files when it is quicker.",\n].join("\\n");', 1)
    assert contract_problems(granted) == ["the preamble differs from the pinned text"]
    truncated = js.replace("    const prompt = RO\n", "    const prompt = RO.slice(0, 0)\n")
    assert contract_problems(truncated) == ["the prompt no longer starts with the whole preamble"]
    assert set(_CONTRACT) <= set(contract_problems("const nothing = 1;"))


# --- the bookends, and no clock ----------------------------------------------------------------------------------

_BOOKENDS = ("synthesis:", "adr_stub:", "index_row:", "**Decision:**", "**Status:**", "**ADR:**",
             "## The question", "## What would change this decision")
_CLOCK = re.compile(r"\bDate\s*\.\s*now\b|\bnew\s+Date\b|\bMath\s*\.\s*random\b|\bperformance\s*\.\s*now\b")


def bookends_problems(js):
    """What is wrong with the runner's return: no `bookends`, a member or a line of its texts missing, the date
    not parsed from the run folder's name, a clock or a random source read anywhere, a script that does not end
    by returning its result."""
    code = _code(js)
    at = code.find("out.bookends = {")
    if at < 0:
        return ["the runner no longer returns bookends"]
    problems = ["bookends lacks %s" % part for part in _BOOKENDS if part not in code[at:]]
    if not re.search(r"\\d\{4\}-\\d\{2\}-\\d\{2\}", code):
        problems.append("the date is no longer parsed from the run folder's name")
    if _CLOCK.search(code):
        problems.append("the script reads a clock or a random source")
    if not code.rstrip().endswith("return out;"):
        problems.append("the script does not end by returning its result")
    return problems


def test_runner_returns_the_bookends_and_reads_no_clock():
    """The runner builds `bookends` with a synthesis header, a decision-record stub and an index row, takes the
    run's date from the run folder's NAME, and reads no clock and no random source. Guards those texts being
    dropped from the return, and a `Date.now()` slipping in — a workflow script that reads a clock cannot be
    resumed, which is why the caller stamps the date into the folder name in the first place. Shown to fail on a
    script with a member removed, with the return removed, and with a clock read in each of its spellings."""
    js = _read(RUNNER)
    assert bookends_problems(js) == []
    assert bookends_problems(js.replace("  index_row: {", "  row: {")) == ["bookends lacks index_row:"]
    assert bookends_problems(js.replace("out.bookends = {", "const unused = {")) == [
        "the runner no longer returns bookends"]
    for clock in ("const t = Date.now();", "const t = Date.now ();", "const d = new Date;", "const r = Math.random();",
                  "const p = performance.now();"):
        assert bookends_problems(js.replace("return out;", clock + "\nreturn out;")) == [
            "the script reads a clock or a random source"], clock
    assert bookends_problems(js.replace("return out;", "")) == ["the script does not end by returning its result"]


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
    missing = [name for _key, name in wanted if name not in rows]
    # The scan above knows three spellings: `A.x`, `ph.x`, `a.x`. Any other way of reading an argument would be
    # invisible to it, so the other ways are refused outright.
    for hidden, what in ((r"\bA\s*\[", "an argument read as A[…]"), (r"\}\s*=\s*A\b", "arguments destructured from A"),
                         (r"\bfor\s*\(\s*const\s+(?!ph\b|a\b)\w+\s+of\s+(?:phases|list|ph\.agents)\b",
                          "a loop over phases or agents with another variable than ph / a"),
                         (r"(?:phases|list|ph\.agents)\s*\.\s*(?:map|forEach|some|filter|reduce)\s*\(\s*\(?\s*(?:\w+\s*,\s*)?"
                          r"(?!ph\b|a\b|n\b)\w+\s*\)?\s*=>", "a callback over phases or agents with another variable")):
        if re.search(hidden, code):
            missing.append(what)
    return missing


def test_every_runner_argument_is_documented():
    """Every key the runner reads from its arguments, from a phase and from an agent has a row in the arguments
    table of .claude/workflows/README.md. Guards an option added to the script and not to its contract — how
    `effort` came to exist in one fork of this file only, for months. A script that reads one more key is shown
    to fail."""
    js, readme = _read(RUNNER), _read(RUNNER_README)
    assert undocumented_arguments(js, readme) == []
    assert undocumented_arguments(js + "\nconst x = A.brand_new_option;\n", readme) == ["brand_new_option"]
    assert undocumented_arguments(js + "\nconst y = a.secret_knob;\n", readme) == ["agents[].secret_knob"]
    # ...and the spellings the scan could not see are refused, so an option cannot hide in one of them
    assert undocumented_arguments(js + '\nconst z = A["hidden"];\n', readme) == ["an argument read as A[…]"]
    assert undocumented_arguments(js + "\nconst { hidden } = A;\n", readme) == ["arguments destructured from A"]
    assert undocumented_arguments(js + "\nfor (const p of phases) { log(p.hidden); }\n", readme) == [
        "a loop over phases or agents with another variable than ph / a"]
    assert undocumented_arguments(js + "\nphases.forEach((p) => log(p.hidden));\n", readme) == [
        "a callback over phases or agents with another variable"]


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
    # a row names its run as the link text of a table line: `| <date> | [<folder>](…) | …` — not merely
    # somewhere in the file, where a longer folder name, a date cell or a comment would satisfy it
    named = set(re.findall(r"^\|[^\n]*?\[([^\]\n]+)\]\(", text, flags=re.M))
    missing = []
    for name in sorted(os.listdir(reviews_dir)):
        m = re.match(r"(\d{4}-\d{2}-\d{2})(-.+)?$", name)
        if m and os.path.isdir(os.path.join(reviews_dir, name)) and m.group(1) >= since and name not in named:
            missing.append(name)
    return missing


def test_every_dated_run_folder_has_an_index_row(tmp_path):
    """Each run folder under docs/reviews/ dated on or after the day the index began is named in the index of
    that tree — README.md here, INDEX.md in an instance repository. Guards a run nobody can find: the row is what
    makes a run's decision visible and supersedable. On a tree with no run folder the check passes with nothing
    to check, so a planted tree shows it fail — for a new folder with no row, and not for an older one."""
    assert runs_without_an_index_row(REVIEWS) == []
    for name in ("2026-10-12-with-a-row", "2026-10-12-with", "2026-10-12", "2026-11-03-no-row",
                 "2026-07-01-before-the-index", "notes"):
        (tmp_path / name).mkdir()
    (tmp_path / "README.md").write_text(
        "<!-- 2026-11-03-no-row is mentioned in a comment, which is not a row -->\n"
        "| 2026-10-12 | [2026-10-12-with-a-row](2026-10-12-with-a-row/99-synthesis.md) | full | d | none |\n",
        encoding="utf-8")
    # a shorter folder name, a bare-date folder and a name in a comment are NOT satisfied by that one row
    assert runs_without_an_index_row(str(tmp_path)) == ["2026-10-12", "2026-10-12-with", "2026-11-03-no-row"]
    (tmp_path / "INDEX.md").write_text("| 2026-11-03 | [2026-11-03-no-row](x) | light | d | none |\n",
                                       encoding="utf-8")                              # an instance repository's index
    assert runs_without_an_index_row(str(tmp_path)) == ["2026-10-12", "2026-10-12-with", "2026-10-12-with-a-row"]


# --- decision records ---------------------------------------------------------------------------------------------------

_STATUS = re.compile(r"^\*\*Status:\*\* (PROPOSED|ACCEPTED|SUPERSEDED)\b", re.M)
_FENCE = re.compile(r"^```.*?^```[^\n]*$", re.M | re.S)


def decision_record_problems(adr_dir):
    """What is wrong with the records under adr_dir: a Markdown file that is not `NNNN-<slug>.md` (a stub saved
    with its placeholder number, a three-digit number), a number used twice, a status line — outside any code
    fence — that holds none of the allowed words, a record the README's index has no row for, a row whose status
    cell does not hold the record's status."""
    if not os.path.isdir(adr_dir):
        return ["%s does not exist" % adr_dir]
    readme = os.path.join(adr_dir, "README.md")
    index = _read(readme) if os.path.exists(readme) else ""
    problems, seen = [], {}
    for name in sorted(os.listdir(adr_dir)):
        if not name.endswith(".md") or name == "README.md":
            continue
        m = re.match(r"(\d{4})-[a-z0-9][a-z0-9-]*\.md$", name)
        if not m:
            problems.append("%s is not named NNNN-<slug>.md" % name)
            continue
        number = m.group(1)
        if number in seen:
            problems.append("%s reuses the number of %s" % (name, seen[number]))
        seen[number] = name
        status = _STATUS.search(_FENCE.sub("", _read(os.path.join(adr_dir, name))))
        if not status:
            problems.append("%s has no `**Status:**` line with PROPOSED, ACCEPTED or SUPERSEDED" % name)
        row = re.search(r"^\| %s \|[^\n]*$" % number, index, flags=re.M)
        if not row:
            problems.append("%s has no row in README.md's index" % name)
        elif status and status.group(1) not in row.group(0):
            problems.append("%s is %s but its index row does not say so" % (name, status.group(1)))
    return problems


def test_decision_records_are_numbered_once_and_indexed(tmp_path):
    """docs/adr/ exists with its README, and every record in it is named `NNNN-<slug>.md`, has a number of its
    own, a status line holding one of the allowed words, and a row in the README's index that says the same
    status. Guards two records sharing a number (it happened while this directory was being planned), a record
    whose status nobody can tell, a record nobody can find, a row still saying "not yet written" beside an
    accepted record, and a stub saved under its placeholder name. The directory may hold no record yet, so a
    planted directory shows each of them fail."""
    assert os.path.exists(os.path.join(ADR, "README.md"))
    assert decision_record_problems(ADR) == []
    (tmp_path / "README.md").write_text(
        "| ADR | Decision | Status |\n|---|---|---|\n| 0007 | fine | ACCEPTED |\n| 0009 | later | not yet written |\n",
        encoding="utf-8")
    (tmp_path / "0007-fine.md").write_text("# ADR-0007\n\n**Status:** ACCEPTED (2026-10-12)\n", encoding="utf-8")
    assert decision_record_problems(str(tmp_path)) == []
    (tmp_path / "0007-again.md").write_text("# ADR-0007\n\n**Status:** ACCEPTED (2026-10-12)\n", encoding="utf-8")
    (tmp_path / "0008-vague.md").write_text("# ADR-0008\n\n**Status:** under discussion\n", encoding="utf-8")
    (tmp_path / "0009-later.md").write_text("# ADR-0009\n\n**Status:** ACCEPTED (2026-11-01)\n", encoding="utf-8")
    (tmp_path / "0010-fenced.md").write_text("# ADR-0010\n\n```markdown\n**Status:** ACCEPTED\n```\n", encoding="utf-8")
    (tmp_path / "NNNN-plan-round-1.md").write_text("# ADR-NNNN\n\n**Status:** PROPOSED (2026-10-12)\n", encoding="utf-8")
    (tmp_path / "007-short.md").write_text("# ADR-007\n\n**Status:** PROPOSED (2026-10-12)\n", encoding="utf-8")
    problems = decision_record_problems(str(tmp_path))
    for expected in ("0007-fine.md reuses the number of 0007-again.md",
                     "0008-vague.md has no `**Status:**` line with PROPOSED, ACCEPTED or SUPERSEDED",
                     "0008-vague.md has no row in README.md's index",
                     "0009-later.md is ACCEPTED but its index row does not say so",
                     "0010-fenced.md has no `**Status:**` line with PROPOSED, ACCEPTED or SUPERSEDED",
                     "NNNN-plan-round-1.md is not named NNNN-<slug>.md", "007-short.md is not named NNNN-<slug>.md"):
        assert expected in problems, (expected, problems)
    assert decision_record_problems(str(tmp_path / "absent")) != []
