# kontroll test suite

kontroll is a *composed* control plane, so it has two test halves:

- **The IaC half** — the test pyramid below (lint → syntax → live-smoke →
  idempotence), distilled from the NetConfig project's four-layer suite.
- **The code half** — a `pytest` suite over the real Python surfaces
  (`scripts/galaxy.py` + the `gui/` Flask app). See [Code-half pytest](#code-half-pytest).

Full rationale: [../docs/engineering-standards.md](../docs/engineering-standards.md) §1
and [../docs/qa-and-release-pipeline.md](../docs/qa-and-release-pipeline.md) §1. The binding
test-doc + data-testid + test-after-change discipline is
[../docs/testing-standards.md](../docs/testing-standards.md); the GUI selector inventory is
[testid_reference.md](testid_reference.md).

## The pyramid (IaC half)

| Layer | What | Tooling | When |
|---|---|---|---|
| **L1 lint** | YAML/Ansible/compose/Prometheus syntax + secret hygiene | `validate.ps1` / `validate.sh` | every commit (pre-commit + CI) |
| **L2 validate** | playbooks parse against a mock inventory | `--syntax-check` | on change, CI |
| **L2 pytest** | `galaxy.py` + GUI logic, shell-outs mocked (no lab) | `pytest` (`unit/` + `integration/`) | on change, CI |
| **L2 as root** | the canonical's git as the service uid, with real uids on a canonical laid out as the play leaves one (`integration/test_canon_drop.py`), and the deploy's content-clone tasks run by ansible-playbook as root (`integration/test_content_clone_as_service.py`); skipped unless root on Linux. A row there that starts a child as ANOTHER uid starts `sh` or a system program, or picks an account that can run the suite's interpreter (`_a_third_account`): a runner's Python may live where only its owner goes | `validate.sh` step `canon-drop-as-root` (directly as root, or through `sudo -n`; a skipped row fails it) | CI (`validate --strict`), the gate container |
| **L3 GUI e2e** | the GUI in a headless browser over the real booted app | Playwright (`tests/e2e/`, `e2e` marker) | a dedicated CI job |
| **L3 live smoke** | real fleet reachable; services respond | `playbooks/ping.yml`, post-deploy asserts | manual / Semaphore schedule |
| **L4 idempotence** | second apply = `0 changed` | re-run the play against a scratch host (by hand — no automated harness) | per role |

## Running

```powershell
# Windows workstation (runs whatever tools are installed; skips the rest)
pwsh tests/validate.ps1                 # Windows: a shim over validate.sh (Git for Windows' bash) — the same gate
```
```bash
# Linux control VM / CI (use --strict so missing tools fail the run)
bash tests/validate.sh --strict
```
A step can also **WARN**: `status-as-of` (`tests/check-status.py`) warns when `STATUS.md` or its last live run
is more than a month old. A warning is counted and shown in the last line (`VALIDATE: PASS (0 skipped, 1
warned)`) and never fails the run, also under `--strict` — a date must not turn `main` red; the publish
workflow runs the same check with `--release`, where it does.
`validate.sh` runs one step as root — `canon-drop-as-root` — directly when it is root, else through `sudo -n`
when that needs no password (it says which); with neither it is a SKIP, which `--strict` counts as a failure.
```bash
# L2 dry-run a playbook against fake hosts (no real device touched)
ansible-playbook -i tests/mock-inventory.yml ../ansible/playbooks/backup-configs.yml --check --diff
```

## Isolation (per layer)

| Layer | Isolation mechanism |
|---|---|
| L1 | none needed — pure static analysis of files |
| L2 | `mock-inventory.yml` with `ansible_connection: local` + RFC-5737 doc IPs — the single seam for real↔fake (our `FakeCollector`) |
| L3 | runs against the live fleet; read-only plays only unless gated |
| L4 | a scratch host or VM, destroyed after |

## The single mock seam

Mirroring NetConfig's rule ("patch `get_collector`, never `ConnectHandler`"):
tests never hard-code a real device. They target `tests/mock-inventory.yml`,
whose hosts dispatch through the same `device_role` seam as production. To swap
real ↔ fake you change the inventory, nothing else.

## The suite never reads a private overlay

`kontroll.paths` resolves instance config overlay-first, and the same suite runs on a public clone, on a CI
runner and on a maintainer's tree that carries a real `instance/`. So `tests/conftest.py` installs a switch at
import time ([_hermetic.py](_hermetic.py)): at the REAL checkout the resolver's two overlay probes answer
"absent", and every reader lands on the shipped `instance.example/`. A tmp root (`tmp_repo`, a monkeypatched
`paths.ROOT`, `KONTROLL_WRITE_ROOT`) is untouched, so the overlay-layout tests still see the `instance/` they
build. The generators follow the same rule from the other side: `--check` validates the committed file against
`instance.example/`, `--example` regenerates it, and only the bare run (what a deploy calls on a node) reads that
node's overlay. A test therefore never asserts on the content of `instance/`.

A few guards read the overlay on purpose, by its own path and not through the resolver, because their job is to
check it: the leak guard's instance-token list, the vendor-neutral `group_vars` lint, the topology-leak test, and
validate's `sops-encrypted` step.

## Adding tests

1. **L1**: nothing to add — new YAML is linted automatically. Keep it valid.
2. **L2 syntax**: a new playbook is picked up by `validate` automatically.
3. **L2 pytest**: drop a `test_*.py` under `unit/` (pure logic) or `integration/`
   (a command driven as a black box). Mark it `@pytest.mark.unit`/`integration`.
4. **L2 check mode**: give the role's class a host in `mock-inventory.yml` and dry-run
   the play against it (the `--check --diff` command above).
5. **L3 smoke**: add a read-only assertion play (service responds / target `up`)
   under `ansible/playbooks/` and tag it `smoke`.

## Code-half pytest

`scripts/galaxy.py` (a thin CLI over the `scripts/kontroll/` **service package** — the pure
logic, probers, sources, and repo-mutators, one `service/<domain>.py` per verb), `gui/app.py`
(the Flask shell), and `api/` (the typed FastAPI over the same service layer) are real code, tested
with `pytest`. The suite is **fully offline** — it touches
no ansible, no network, and no lab — because the I/O seams are mocked **in their home modules**
(patch there, not on `galaxy`):

| Surface | I/O seam (the mock point) | What it shells to |
|---|---|---|
| deep probe | `kontroll.probe._ad`, `kontroll.probe.deep_probe` | `ansible-doc` |
| shallow local probe | `kontroll.probe.shallow_from_local`, `kontroll.catalog.local_shallow` | the installed collection's files (`plugins/` walk + `MANIFEST.json`) — no `ansible-doc` |
| local list | `kontroll.catalog.local_installed` | `ansible-galaxy collection list` |
| Galaxy search | `kontroll.catalog.galaxy_search` | `galaxy.ansible.com` (urllib) |
| repo mutation | `kontroll.gitio._run` | `git` / `sops` |
| `gui/app.py` onboard | service seams (`build_onboard_plan`/`apply_onboard_plan` + `kontroll.gitio._run`) | in-process, mirrors `api/routes/onboard.py` — search/classify/onboard are ALL in-process now (`run_galaxy` retired) |

The core (`eval_pred`/`eval_vector`/`classify`/`build_record`/`derive_*`/
`classify_endpoints`, now in `kontroll.predicate`/`record`/`service.openapi` and re-exported
off `galaxy` for call-style tests) is **pure dict→dict** and tested directly against the real
`vectors/*.yml` + `ansible/backends/*/backend.yml`. The file-mutating paths
(`enable_in_fleet`, onboard `--apply`) run against a throwaway repo (`tmp_repo`
repoints `kontroll.paths.ROOT`), so they never touch the real tree, and credentials are
omitted so the SOPS shell-out is never invoked.

```
tests/unit/          # pure functions, no I/O
  test_predicate_engine.py   # three-valued eval_pred/eval_vector truth tables
  test_classify.py           # backend dispatch from synthetic facts
  test_build_record.py       # record schema + overrides (incl. YAML yes/no→state)
  test_telemetry_vector.py   # the telemetry capability vector (4th vector): 3-state + depth-stable
  test_telemetry_suggest.py  # suggest_telemetry + declared_metrics_methods (detected/declared bridge)
  test_fleet_edit.py         # enable_in_fleet idempotent, comment-preserving
  test_openapi.py            # derive_auth/derive_server/classify_endpoints heuristics
  test_service_layer.py      # service_* return structured data directly (no stdout-parsing)
  test_units.py              # runnable-unit prober + service_units resolver + SEC-3 traversal guard + validate_unit_config (R3 configure gate)
  test_actuation_registry.py # actuation/ registry: resolve_pins (exact-wins/fail-closed) + gen-actuation validation (incl. R3 curated knobs) + overlay loader
  test_actuation_service.py  # R4 preview + R5 write verb: build_actuation_plan render/staging-metadata (read-only) + apply_actuation_plan (idempotent write) + stage_plan drift gate
  test_discovery.py          # discovery inbox (Rung 1a/2/3): registry load + declared offers reach opnsense/fortigate/routeros/cisco_ios + json_rows (results-key & bare-array, Bearer/Basic) + snmp arp_table (transport dispatch, snmp_walk byte-cap, MAC/suffix parse) + read-only 2-primitive fetcher surface + sweep parse/cap + read_inbox IP-diff/normalize + read-only-pin canary
  test_request_boundary.py   # C22: the request-field validators' accept/reject tables, the joins (the probe's argv, every plan builder), refusals that never quote the value
  test_api_audit.py          # one request value is one cell of one audit row: the shared flattener, and the API writer read back through a real file
  test_repository_data.py    # C22: a name adopted from a module file, the reserved names at each seam, the scaffold's arguments, and the refusal that is the repository's own (409), never a field's
  test_yamlscalar.py         # may a string stand unquoted in a hand-formatted YAML line: every position a writer uses, exhaustively for short strings; the quoting fallback of the generators
  test_staging_isolation.py  # C10: the staging clone returns to canonical main after each stage; and the canonical's update hook — the committed file run over an argument table under every POSIX shell installed (one that runs its own `id` before the PATH's — Debian's busybox — is left out: the table says who pushes with an `id` of its own), its run-id rule against canon.valid_run_id, real pushes into a repository that carries it (the hook's execution tests need POSIX sh: skipped on Windows; the hook only ever runs on Linux, and CI does)
  test_promote_cas.py        # C10: the promote is one ref transaction over the two ids that were read — the races injected between the read and the move, against real git
  test_canon_tree.py         # C10: one commit's tree read object by object and re-hashed — attributes, filters, a swapped object, a link, the caps; the export command (a process stood in for git starts in the test's own environment, not the hardened one: `_in_gits_place`)
  test_canon.py              # C10: how the canonical is read — refs by full name, one canary test (with its control) per entry of the git environment a planted file can show, the whole entry list held by name, bytes to text, the seam pin
  test_canonical_audit.py    # C10: the audit of a canonical — a canonical the play made, the tool used and git collected garbage in is clean; each planted thing named by kind and key and never by value; names escaped and cut; no link followed, no exchanged directory listed, no pipe waited on; the one process held by a closed list; every kind in both tables; the play's tasks and their conditions, evaluated
  test_canonical_normalise.py # C10 / F-CANON: the play's ownership pass (`--normalise`) — `--check` names and changes nothing; applied, the bits are set and a second run has nothing to do; a link to a file and to a directory outside the tree named and what they point at left as it was; a pipe named and not waited on; hooks/ passed over; a directory exchanged for a link after it was looked at not gone into; the refusal where no owner or mode can be changed through a descriptor; one walker for the audit and the pass
  test_content_clone_tasks.py # C10: the deploy's git in a content clone — one task file for both clones; root's git only unarmed, the service uid's (setsid/setpriv/env -i) armed; a clone held by the wrong uid removed, never chowned; refs by full name
  test_fleet_line.py         # C22: a fleet line that is not a class key at every deploy-time reader — skipped and named by what renders, refused by what installs and by the promote gate, checked first by the plays
tests/integration/   # galaxy.py / gui as black boxes, shell-outs mocked
  test_canonical_normalise_as_root.py # C10 / F-CANON, AS ROOT: the ownership pass chowns a root-made canonical to `<operator>:1001` outside hooks/ — every regular file and directory, through its descriptor; a link's own owner, what a link points at outside the tree, and hooks/ stay root's; the second run has nothing to do
  test_content_clone_as_service.py # C10, AS ROOT: the content-clone tasks by ansible-playbook holding a terminal — planted hooks/fsmonitor run as uid 1001 with no group, privilege, terminal or deploy variable; arming/disarming makes the clone again; a clone's mode, a third uid's clone, `local`, the system file, a discard and HEAD reported; a parent or storage root the service could write, or a storage root closed to it, refused (and the printed fix opens it); deploy-stack.yml's own storage-root, store and host-dir tasks on a node with no storage root; the restart's condition evaluated
  test_onboard_dryrun.py     # dry-run plans nothing; --apply mutates tmp repo idempotently
  test_search_local.py       # source→record→filter pipeline + --vector filter
  test_gui_api.py            # Flask test client: auth gate (401s) + /api/* passthrough
  test_api.py                # the FastAPI read-only routes via TestClient (service seams mocked)
  test_capture_exception_e2e.py  # the capture-exception add subcommand (dry-run/--commit)
tests/e2e/           # GUI in a headless browser over the REAL booted Flask app (opt-in: e2e marker)
  conftest.py                # boots gui/app.py in a thread, service seams mocked, http_credentials
  test_onboard_flow.py       # search → result card → fill form → dry-run → plan renders (Playwright)
  test_paint_rule.py         # C19 in a browser: the panels and dialogs that paint foreign values show a hostile one as text
```

```bash
pip install -r tests/requirements-dev.txt          # tiny: pytest + pyyaml + flask
pytest -m "not e2e and not slow"                   # the hermetic default (CI runs this)
pytest --cov=scripts --cov=gui --cov-report=term-missing
```

`validate` runs this as its L2 step automatically when `pytest` is installed (it
skips gracefully on a box without it); CI runs it as a dedicated matrix job
(py3.11–3.13). `e2e`/`slow` are excluded from the default — the live lab and any
Playwright GUI flow are never in the hermetic gate.

## Identifier-leak gate (`tests/_leak_guard.py`)

`validate` step `pii-guard` (and the PII-guard workflow, and the pytest twin
`tests/unit/test_leak_guard.py`) scans every git-tracked file outside the private strip set
(`instance/`, `docs/reviews/`, `local/`) for anything that identifies a real deployment: an
RFC-1918 address in any spelling (dotted, dashed as capture filenames spell it, underscored),
a non-documentation MAC or global IPv6, a real-length age key, a personal e-mail or an
operator-machine path — plus the instance's own names from `instance/leak-tokens.txt` (never
shipped; the shipped `instance.example/leak-tokens.example.txt` holds canaries so the layer is
always exercised). Fixtures and docs use the documentation ranges — `192.0.2.x`,
`198.51.100.x`, `203.0.113.x`, `00:00:5e:00:53:xx`, `2001:db8::/32`, `example.com`. A test that
*must* hold a private address marks the line `pii-guard: allow <reason>`. SECURITY.md C21.

## Document-link gate (`tests/check-doc-links.py`)

`validate` step `doc-links` (pytest twin: `tests/unit/test_check_doc_links.py`) fails on a relative link in a
tracked Markdown file that does not resolve to a TRACKED file, or a directory holding one, inside the repository.
The spelling is compared exactly, so a link that only works on a case-insensitive disk, or to a file that is on
your disk and not tracked (`git add` it first), fails on every machine; and a target that climbs out with `../`
is a finding even where a file happens to exist there. It is hermetic (no URL is fetched) and does not check
`#anchors`; fenced code blocks and code spans are skipped, so a document may quote a broken link. A target that
cannot exist in the shipped tree by design is exempt: `instance/…`, and a dated run folder under `docs/reviews/`
that this tree does not carry (a link into a run folder the tree does carry is checked). Write any other such
target as a code span, not as a link — as `SECURITY.md` does even for its design records: the control document
should not carry a link its public reader cannot follow. `CHANGELOG.md`, the files inside a dated run folder and
an instance repository's own `instance/` documents are not read. In a git tree that git cannot list, the step
says so and fails (exit 2) rather than walking the directory.

## Secret-hygiene gate

`validate` fails if any `instance/secrets/*.sops.yml` is not encrypted, or if a
key artifact (`keys.txt`, `*.agekey`, `*.dec`) is staged. This is the
config-as-code analog of NetConfig's `test_logging_config.py` credential
guarantee.

## Read-only-by-construction gate (`tests/unit/_readonly_pins.py`)

The shared AST pins guard the read-only / C10 two-key guarantee in CI: `assert_read_only`
(a named read function reaches no write, #133) + `assert_no_git_write` (a service that shells
git picks a read-only subcommand on every argv, #131).

`assert_read_only` is **transitive**. From the pinned function it follows, by name, every
helper of the same module (called, or handed on as a callback) and every function of another
module of this repository reached through an import — `alias.func`, `pkg.mod.func`, a
`from module import func` name, an import inside a function body. In each function on the way
it looks for a registered write verb called by name **and** for the write primitives themselves
(a write-mode `open`, `write_text`, `os.replace`, a mutating git argv, a SOPS write), so a
write that no verb names is seen as well. A failure prints the path, e.g.
`fleet_provenance -> paths.write_root -> (a direct write)`. It cannot follow what no static
reading can — `getattr()` with a computed name, a function kept in a dict or passed in as an
argument, a method reached through another module's object. The fix for a hit is to stop
writing from the read view, never to trim the closure.

The name check alone fails **open** on a mutator the registry does not know, so
`assert_write_verbs_complete` (P0b/#134) scans the whole service+gitio layer and fails CI if any
**direct** mutator (write-open / git-mutating argv / SOPS write) isn't in `WRITE_VERBS` —
registering a new write verb in the same commit is an enforced gate, not a convention. Both
halves are proven against planted violations (`test_readonly_completeness.py`): a helper one
and two calls away, a callback, a sibling module under each import spelling the closure knows,
a primitive no verb names — plus a check on the real tree that the closure still resolves the
GUI's and the service layer's imports. Add a new read view? Add its `assert_read_only` test.
Add a new writer? CI tells you to register it.

`assert_git_runs_in_the_seam` is the pin for a module whose git runs on the **canonical**
(`scripts/kontroll/canon.py`, `service/pending.py`, `service/discard.py`): every process it
starts is one git argv literal whose second element is an allowed verb — each module is given
the verbs it runs today, not the read-only list, because the seam is proven for those verbs
and no others — passing `**canon.git_kwargs(<repo>)`, a `timeout`, and no `env`, `cwd`, `shell`,
`executable`, second positional argument or second `**` of its own; no `-C`, no `-c`, no verb
built at run time, no `subprocess` under another name, no process through `os`. The verb pins
say *which* git a read view may run; this one says *in what*. It is proven against one planted
module per way around it (`test_canon.py`: the call-shaped ways, and the ways that are not a
call).

### The throwaway canonical (`tests/unit/_canon_repo.py`)

The fixture seam for everything that reads the canonical. `build()` makes a bare repository
shaped like a node's — `main`, a proposal that fast-forwards it, one it has moved past — out of
objects written from **bytes** (`hash-object`, `mktree -z`), so a file name, an author or a
subject that no filesystem or porcelain of the host would take still gets in. One `plant_*`
function per thing a writer of the repository can leave behind: the other meanings of the
short names (a tag `main`, `refs/main`, a file `main`), a replace ref, a graft line, a
`shallow` file, a commit-graph that lies about a parent or a tree, a `commondir`, a hook.
`snapshot()` turns a flat `{path: bytes}` into a tree; `raw_tree()` writes a tree from raw
`(mode, name, id)` triples — a link, a submodule entry, a name twice, whatever `mktree` would
refuse; `swap_loose()` replaces an object FILE with a well-formed object of other content, which
git reads back without complaint.
The pattern of a test: plant one thing, show that the host's plain git **is** fooled (the
control — a planted thing that fools nobody proves nothing), then ask the code under test
what it would ask of a clean repository. `seam_without()` gives the seam's environment minus
named entries, for showing that one entry is the one that answers a planted thing. These
tests run real git and take the suite's `isolated_git` fixture.

## Playbook and role hygiene sweeps

Two faults that pass `--syntax-check` and `ansible-lint` and fail only on a real run are swept
statically, over every playbook **and every role** — task and handler files at any depth, the
copy-me `.role-template` included, because role files are what every device play executes:

- `tests/unit/test_playbook_setfact_hygiene.py` — no `set_fact` key templates a sibling key of
  the same task (the sibling is undefined when the task renders).
- `tests/unit/test_playbook_vars_hygiene.py` — no variable is defined in terms of itself: play,
  block and task `vars:`, a role's `defaults/` and `vars/` files, the playbook-adjacent
  `group_vars`.

A role YAML file in a place neither sweep reads (a role's root, an unknown subdirectory) fails
`test_every_role_yaml_file_is_of_a_kind_the_sweeps_read`: teach the sweeps the new kind rather
than leave the file unread.
