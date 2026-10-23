# Testing & test-documentation standards

Binding standard for how kontroll tests, documents its tests, tags its GUI for testing, and keeps
changes diagnosable. The never-break subset is mirrored in [/CLAUDE.md](../CLAUDE.md) Hard Rules;
the test *pyramid* and its IaC rationale live in [engineering-standards.md](engineering-standards.md)
§1. This doc is the detail those two point at.

**Why this is codified this hard:** tests, test-docs, and logging are the substrate that lets a
*later* iteration — a different AI, a new contributor — continue development without re-deriving
intent. A change that lands code but not its test, its test's rationale, or its log line is
half-finished: it works today and is unmaintainable tomorrow. Distilled from the netcanon project
(the netcanon repository, read-only reference), which uses exactly this discipline to
keep an AI-assisted codebase honest.

---

## 1. The test-layer map

kontroll is a *composed* control plane, so it has two test halves. Both are first-class.

| Half | Layer | What it exercises | Tooling | When |
|---|---|---|---|---|
| IaC | **L1 lint** | YAML/Ansible/compose/Prometheus syntax + secret hygiene | `tests/validate.ps1` / `.sh` | every commit (pre-commit + CI) |
| IaC | **L2 validate** | playbooks parse vs a mock inventory | `--syntax-check` | on change, CI |
| Code | **L2 pytest (unit)** | pure `dict→dict` logic + service layer in `scripts/kontroll/` | `pytest tests/unit` | on change, CI |
| Code | **L2 pytest (integration)** | `galaxy.py` + `gui/app.py` as black boxes, shell-outs mocked | `pytest tests/integration` | on change, CI |
| Code | **L3 GUI e2e** | the real booted Flask GUI in a headless browser | Playwright (`tests/e2e/`, `e2e` marker) | a dedicated CI job |
| IaC | **L3 live smoke** | real fleet reachable; services respond | `playbooks/ping.yml` (the `run-smoke-gate.sh` wrapper) | manual / post-deploy |
| IaC | **L4 idempotence** | second apply = `0 changed` | re-run the play against a scratch host (by hand — no automated harness) | per role |

The single mock seam (IaC: `tests/mock-inventory.yml` via `device_role`; code: the I/O seams in their
home modules — `kontroll.probe.{_ad,deep_probe}`, `kontroll.catalog.{local_installed,galaxy_search}`,
`kontroll.gitio._run`, and the GUI's in-process service seams) is described in [../tests/README.md](../tests/README.md).
Never patch below the seam. A dict served in place of a real call — a canned return, a `page.route` body —
belongs once in `tests/_canned.py`, with a row in `tests/unit/test_mock_conformance.py` that compares its shape with
the real function's answer on a fixture: a mock shaped by belief keeps a UI test green while the server emits
something else (S33). The e2e server's seams are enforced; a body or a route test's fake is held only by review, and
the ones still written by hand are listed in [../tests/README.md](../tests/README.md). The live lab and the Playwright flows are never in the blind hermetic gate
(`pytest -m "not e2e and not slow"`) — see [qa-and-release-pipeline.md](qa-and-release-pipeline.md).

---

## 2. Every test is documented — the docstring rule

**Rule:** every test module opens with a docstring saying *what surface it covers and how it
isolates*; every test function states *what it verifies and the failure it guards against*. No
exceptions — an undocumented test is a liability, because the next contributor can't tell a real
regression from a stale assertion, and can't safely delete it. **`tests/validate` fails on any test
function without a docstring** — the rule is machine-enforced, not aspirational.

**Good** (intent + guard are explicit — the house style):

```python
def test_onboard_dryrun_renders_the_plan(page):
    """A dry-run must PLAN but never write: the apply box stays unchecked, and the rendered
    plan is what the operator sees before actuating."""
    ...
    expect(card.get_by_test_id("field-apply")).not_to_be_checked()   # dry-run by default — no write
```

**Bad** (no intent — a later reader can't tell what breakage this catches):

```python
def test_onboard(page):
    page.goto("/")
    page.get_by_test_id("run").click()
    assert page.get_by_test_id("output")   # asserts *something* rendered — but what regression?
```

Per-module docstrings that **enumerate** a surface (endpoints, markers, seams) are updated in the
same commit when that surface changes (Documentation Sync Checklist) — a docstring that says
"covers /api/search, /api/onboard" becomes a lie the instant a third endpoint lands.

---

## 3. The `data-testid` SOP

E2E tests select on `data-testid` **exclusively** — never CSS classes, never DOM structure — so
refactoring the markup never breaks a test. This is a Hard Rule.

**Which elements carry one:** every element a test interacts with or asserts on — buttons, inputs,
links, form fields, result cards, output panes, status/empty/error states. If a Playwright test
needs to find it, it has a `data-testid`.

**Naming convention:** lowercase, hyphen-separated, named by role/action (not appearance):
`search-input`, `onboard-toggle`, `run`, `output`, `card`. For repeated rows, the container carries
the id and a `data-*` attribute disambiguates (`[data-testid="card"][data-collection="cisco.ios"]`;
`[data-testid="capability-badge"][data-cap="backup"][data-state="yes"]`). `name=` attributes on
form inputs stay (they're the submit payload contract) — the `data-testid` is **additive**.

**Both emission forms are first-class** (`gui/templates/index.html` uses both):

```html
<button class="go" data-testid="search-btn">Search</button>          <!-- static -->
```
```javascript
const c = E('div','card'); c.dataset.testid = 'card';                // dynamic — same data-testid
```

**The inventory is [../tests/testid_reference.md](../tests/testid_reference.md)** — a per-section
table of every `data-testid`. It must not fall behind the template. Self-check before commit:

```bash
grep -r 'data-testid="<new-id>"' tests/testid_reference.md   # no hit ⇒ the inventory is stale
```

---

## 4. The test-after-change loop

Enforced locally (pre-commit) and in CI:

1. **Change** code/config.
2. **Document** the test you're about to add/adjust — intent first (§2).
3. **Add/adjust** the test at the lowest layer that proves it: pure logic → `tests/unit`; a
   CLI/API black box → `tests/integration`; a UI flow → `tests/e2e` (+ a `data-testid` and its
   inventory row, §3); a role → a `--check --diff` run against `tests/mock-inventory.yml`, then a second
   apply on a scratch host (`0 changed`).
4. **Run the hermetic gate:** `pytest -m "not e2e and not slow"` then `bash tests/validate.sh`. Hermetic includes
   the overlay: a test never asserts on the content of `instance/` — the suite reads the shipped
   `instance.example/` on every checkout ([tests/README.md](../tests/README.md), "The suite never reads a private
   overlay"), and a generator's `--check` validates the committed file against that example.
5. **For IaC actuation:** `--check --diff` before any state-changing play; a second apply reports
   `0 changed`.
6. **Re-run after edits you think are cosmetic** (renames, sanitization) — verify, don't assume.

A change isn't done until step 3's artifacts (test + docstring) and the log line (§5) are in the
**same commit** as the code.

---

## 5. How logging ties in — diagnosability is a deliverable

A test proves a change *works*; a log proves *why it broke* in production. Both ship with the
change. kontroll's correlation model (full detail in [logging-architecture.md](logging-architecture.md)
Layer 0):

- **`kontroll_run_id`** — the shared correlation key (`_log-run-id.yml`, imported first by the
  fleet playbooks) that stitches one operation across the ansible log, the Semaphore job output,
  and script run-logs. Any new actuating path emits it.
- **GUI audit log** — every onboard action + auth-denial: timestamp, client IP, what
  (collection/key/host/flags). **Never** a credential. New GUI actions extend it, and a test
  asserts the cred-exclusion (`tests/integration/test_gui_api.py`).
- **Script run-logs** (`scripts/lib/run-log.sh`) — a script's output teed to a timestamped, pruned,
  operator-writable trail. New operator scripts source it.
- **Secret discipline** — `no_log: true` on every secret-touching task; no secret-valued log
  labels; scheduled jobs never run `-vvvv`.

Rule of thumb: if a future operator would have to reconstruct "what ran, when, and did it succeed?"
by hand across silos, the change is missing a log line.

## See also
- [/CLAUDE.md](../CLAUDE.md) — the binding Hard Rules + checklists this doc backs
- [engineering-standards.md](engineering-standards.md) §1 — the test pyramid + IaC rationale
- [../tests/README.md](../tests/README.md) — test-suite layout, the mock seam, how to run
- [../tests/testid_reference.md](../tests/testid_reference.md) — the `data-testid` inventory
- [qa-and-release-pipeline.md](qa-and-release-pipeline.md) — CI gating, security scans, release
- [logging-architecture.md](logging-architecture.md) — the run_id / audit / run-log model §5 ties into
- netcanon reference (read-only): the netcanon repository — `tests/testid_reference.md`, `docs/HOW_WE_TEST.md`, `AGENTS.md`
